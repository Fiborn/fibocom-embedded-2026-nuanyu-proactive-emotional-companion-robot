#!/usr/bin/env python3
"""Thread-safe SQLite store with automatic graceful degradation.

Every public method returns a sensible default on DB failure — the
voice pipeline must never be blocked by a storage error.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple


# ── helpers ──────────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _uid() -> str:
    return uuid.uuid4().hex[:12]


# ── store ────────────────────────────────────────────────────────────

class SqliteMemoryStore:
    """Thread-safe SQLite store with automatic graceful degradation.

    On any DB error the store enters *degraded* mode:  all reads return
    empty / None defaults and all writes are silently dropped.  A single
    reconnect is attempted on the next write so transient fs / lock
    issues can self-heal.
    """

    def __init__(self, db_path: str = ":memory:"):
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._healthy: bool = True
        self._closed: bool = False
        self._last_error: str = ""
        self._degraded_since: float = 0.0
        self._connect()

    # ── lifecycle ─────────────────────────────────────────────────

    def close(self) -> None:
        self._closed = True
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
        self._healthy = False

    @property
    def healthy(self) -> bool:
        return self._healthy and self._conn is not None

    @property
    def last_error(self) -> str:
        return self._last_error

    # ── internal ──────────────────────────────────────────────────

    def _connect(self) -> None:
        try:
            # Ensure parent directory exists for file-based DBs
            if self._db_path != ":memory:":
                parent = os.path.dirname(self._db_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.row_factory = sqlite3.Row
            self._create_tables()
            self._healthy = True
            self._last_error = ""
            self._degraded_since = 0.0
        except Exception as exc:
            self._healthy = False
            self._last_error = str(exc)[:200]
            self._degraded_since = _now()
            print(f"[MemoryStore] DB init failed ({self._db_path}): {exc}",
                  flush=True)

    def _try_reconnect(self) -> bool:
        """Attempt one reconnect; return True on success.
        Never reconnects after explicit close()."""
        if self._closed:
            return False
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        try:
            self._connect()
            return self._healthy
        except Exception:
            return False

    def _create_tables(self) -> None:
        assert self._conn is not None
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                message_id  TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                session_id  TEXT NOT NULL,
                role        TEXT NOT NULL
                            CHECK(role IN ('user','assistant','system')),
                content     TEXT NOT NULL,
                metadata_json TEXT DEFAULT '{}',
                created_at  REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_msg_user_time
                ON messages(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_msg_session
                ON messages(session_id);

            CREATE TABLE IF NOT EXISTS memories (
                user_id     TEXT NOT NULL,
                key         TEXT NOT NULL,
                value_json  TEXT NOT NULL,
                importance  REAL DEFAULT 0.5,
                keywords    TEXT DEFAULT '',
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL,
                PRIMARY KEY (user_id, key)
            );

            CREATE INDEX IF NOT EXISTS idx_mem_user
                ON memories(user_id);
            CREATE INDEX IF NOT EXISTS idx_mem_importance
                ON memories(user_id, importance);
        """)
        self._conn.commit()

    def _safe_read(self, default: Any, func: Callable, *args) -> Any:
        """Execute *func(conn, *args)*; return *default* on any error."""
        if not self.healthy:
            return default
        try:
            with self._lock:
                return func(self._conn, *args)
        except Exception as exc:
            self._healthy = False
            self._last_error = str(exc)[:200]
            self._degraded_since = _now()
            print(f"[MemoryStore] read error (degraded): {exc}", flush=True)
            return default

    def _safe_write(self, default: Any, func: Callable, *args) -> Any:
        """Execute *func(conn, *args)* with commit; degrade on error.

        Attempts one reconnect on the first write after a failure so
        transient problems self-heal.
        """
        if not self.healthy:
            self._try_reconnect()
            if not self.healthy:
                return default
        try:
            with self._lock:
                result = func(self._conn, *args)
                self._conn.commit()
                return result
        except Exception as exc:
            self._healthy = False
            self._last_error = str(exc)[:200]
            self._degraded_since = _now()
            print(f"[MemoryStore] write error (degraded): {exc}", flush=True)
            try:
                self._conn.rollback()
            except Exception:
                pass
            return default

    # ── messages ──────────────────────────────────────────────────

    def save_message(
        self, user_id: str, session_id: str, role: str,
        content: str, metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Persist one message.  Returns its message_id."""
        msg_id = _uid()
        ts = _now()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        def _do(conn):
            conn.execute(
                """INSERT INTO messages
                   (message_id, user_id, session_id, role, content,
                    metadata_json, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (msg_id, user_id, session_id, role, content, meta_json, ts),
            )
            return msg_id

        return self._safe_write("", _do)

    def get_recent_messages(
        self, user_id: str, limit: int = 24,
    ) -> List[Dict[str, Any]]:
        """Return most-recent *limit* messages, oldest→newest.

        Default 24 = 12 user/assistant turns.
        """
        def _do(conn):
            rows = conn.execute(
                """SELECT message_id, user_id, session_id, role, content,
                          metadata_json, created_at
                   FROM messages
                   WHERE user_id = ?
                   ORDER BY created_at ASC
                   LIMIT ?""",
                (user_id, max(1, limit)),
            ).fetchall()
            # The outer query gets the oldest N; we need the most recent.
            # Use a sub-query approach.
            return rows

        def _do_recent(conn):
            rows = conn.execute(
                """SELECT message_id, user_id, session_id, role, content,
                          metadata_json, created_at
                   FROM (
                       SELECT * FROM messages
                       WHERE user_id = ?
                       ORDER BY created_at DESC
                       LIMIT ?
                   ) ORDER BY created_at ASC""",
                (user_id, max(1, limit)),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return self._safe_read([], _do_recent)

    def get_session_messages(
        self, session_id: str,
    ) -> List[Dict[str, Any]]:
        """All messages in one session, ordered by time."""
        def _do(conn):
            rows = conn.execute(
                """SELECT message_id, user_id, session_id, role, content,
                          metadata_json, created_at
                   FROM messages
                   WHERE session_id = ?
                   ORDER BY created_at ASC""",
                (session_id,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        return self._safe_read([], _do)

    def count_messages(self, user_id: str) -> int:
        def _do(conn):
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return row[0]
        return self._safe_read(0, _do)

    def count_all_messages(self) -> int:
        def _do(conn):
            row = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
            return row[0]
        return self._safe_read(0, _do)

    # ── long-term memories ────────────────────────────────────────

    def save_memory(
        self, user_id: str, key: str, value: Any,
        importance: float = 0.5, keywords: str = "",
    ) -> bool:
        """Upsert a named memory fact."""
        ts = _now()
        value_json = json.dumps(value, ensure_ascii=False)

        def _do(conn):
            conn.execute(
                """INSERT INTO memories
                   (user_id, key, value_json, importance, keywords,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(user_id, key) DO UPDATE SET
                     value_json=excluded.value_json,
                     importance=excluded.importance,
                     keywords=excluded.keywords,
                     updated_at=excluded.updated_at""",
                (user_id, key, value_json, importance,
                 _normalize_keywords(keywords), ts, ts),
            )
            return True
        return self._safe_write(False, _do)

    def get_memory(self, user_id: str, key: str) -> Optional[Any]:
        def _do(conn):
            row = conn.execute(
                "SELECT value_json FROM memories WHERE user_id=? AND key=?",
                (user_id, key),
            ).fetchone()
            if row is None:
                return None
            return json.loads(row[0])
        return self._safe_read(None, _do)

    def list_memories(
        self, user_id: str, prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        def _do(conn):
            if prefix:
                rows = conn.execute(
                    """SELECT key, value_json FROM memories
                       WHERE user_id=? AND key LIKE ?
                       ORDER BY key""",
                    (user_id, prefix + "%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT key, value_json FROM memories
                       WHERE user_id=? ORDER BY key""",
                    (user_id,),
                ).fetchall()
            return {r[0]: json.loads(r[1]) for r in rows}
        return self._safe_read({}, _do)

    def delete_memory(self, user_id: str, key: str) -> bool:
        def _do(conn):
            cur = conn.execute(
                "DELETE FROM memories WHERE user_id=? AND key=?",
                (user_id, key),
            )
            return cur.rowcount > 0
        return self._safe_write(False, _do)

    def count_memories(self, user_id: Optional[str] = None) -> int:
        def _do(conn):
            if user_id:
                row = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE user_id=?",
                    (user_id,),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            return row[0]
        return self._safe_read(0, _do)

    # ── search ────────────────────────────────────────────────────

    def search_memories(
        self, user_id: str, query: str, limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Lightweight keyword+importance+time search.

        Tokenises *query* into words, matches against stored keywords
        and value text, then scores each candidate as:

            score = Σ(match_count × 2) + importance × 10 + recency_bonus

        where recency_bonus = max(0, 5 − age_days).

        Results are sorted by score descending.
        """
        tokens = _tokenize(query)
        if not tokens:
            return []

        def _do(conn):
            rows = conn.execute(
                """SELECT user_id, key, value_json, importance, keywords,
                          updated_at
                   FROM memories
                   WHERE user_id=?""",
                (user_id,),
            ).fetchall()

            scored: List[Tuple[float, Dict[str, Any]]] = []
            now = _now()
            for r in rows:
                row_dict = _row_to_dict(r)
                value_text = str(row_dict.get("value_json", ""))
                combined = (
                    row_dict.get("keywords", "") + " " +
                    row_dict.get("key", "") + " " + value_text
                ).lower()

                match_count = sum(
                    1 for t in tokens if t in combined
                )
                importance = float(row_dict.get("importance", 0.5))
                age_days = (now - float(row_dict.get("updated_at", now))) / 86400.0
                recency_bonus = max(0.0, 5.0 - age_days)

                score = (match_count * 2.0) + (importance * 10.0) + recency_bonus

                if match_count > 0:
                    scored.append((score, {
                        "key": row_dict["key"],
                        "value": json.loads(row_dict["value_json"]),
                        "importance": importance,
                        "keywords": row_dict.get("keywords", ""),
                        "updated_at": row_dict.get("updated_at", 0),
                        "score": round(score, 2),
                    }))

            scored.sort(key=lambda x: x[0], reverse=True)
            return [item for _, item in scored[:max(1, limit)]]

        return self._safe_read([], _do)

    # ── batch import ──────────────────────────────────────────────

    def import_user_json(
        self, user_id: str, json_path: str,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Idempotently import a ``memories/{username}.json`` file.

        Flattens the JSON into individual memory entries:
          - ``profile:name``, ``profile:identity``, …
          - ``note:0``, ``note:1``, …
          - ``meta:nickname``, ``meta:encourage_style``, …

        Returns ``{imported, skipped, errors}``.
        """
        result = {"imported": 0, "skipped": 0, "errors": []}

        if not os.path.isfile(json_path):
            result["errors"].append(f"file not found: {json_path}")
            return result

        try:
            with open(json_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            result["errors"].append(f"read error: {exc}")
            return result

        if not isinstance(data, dict):
            result["errors"].append("root is not a JSON object")
            return result

        entries: List[Tuple[str, Any, float, str]] = []

        # Profile fields → high importance
        profile = data.get("profile", {})
        if isinstance(profile, dict):
            for k, v in profile.items():
                if v:
                    entries.append(
                        (f"profile:{k}", v, 0.9,
                         f"profile {k} {v}"),
                    )

        # Top-level scalar fields → medium importance
        for field in ("nickname", "encourage_style", "health",
                       "study_mode", "reminder_style"):
            val = data.get(field)
            if val and isinstance(val, str):
                entries.append(
                    (f"meta:{field}", val, 0.7, f"{field} {val}"),
                )

        # Notes → medium-high importance (explicit "remember" items)
        notes = data.get("notes", [])
        if isinstance(notes, list):
            for i, note in enumerate(notes):
                if note and isinstance(note, str):
                    entries.append(
                        (f"note:{i}", note, 0.8,
                         _extract_keywords_from_text(note)),
                    )

        # Interests
        interests = data.get("interests", [])
        if isinstance(interests, list):
            for i, interest in enumerate(interests):
                if interest and isinstance(interest, str):
                    entries.append(
                        (f"interest:{i}", interest, 0.6, str(interest)),
                    )

        # Favorite goals
        goals = data.get("favorite_goals", [])
        if isinstance(goals, list):
            for i, goal in enumerate(goals):
                if goal and isinstance(goal, str):
                    entries.append(
                        (f"goal:{i}", goal, 0.6,
                         _extract_keywords_from_text(goal)),
                    )

        for key, value, importance, keywords in entries:
            if not overwrite and self.get_memory(user_id, key) is not None:
                result["skipped"] += 1
                continue
            ok = self.save_memory(user_id, key, value,
                                  importance=importance,
                                  keywords=keywords)
            if ok:
                result["imported"] += 1
            else:
                result["errors"].append(f"save failed: {key}")

        return result


# ── internal helpers ──────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    if "metadata_json" in d:
        try:
            d["metadata"] = json.loads(d.pop("metadata_json"))
        except (json.JSONDecodeError, TypeError):
            d["metadata"] = {}
    return d


def _tokenize(text: str) -> List[str]:
    """Simple CJK-friendly tokeniser: split on whitespace/punctuation,
    keep tokens ≥1 char."""
    import re
    tokens = re.findall(r"[\w一-鿿]+", str(text).lower())
    # Remove single-char tokens that aren't CJK
    return [
        t for t in tokens
        if len(t) >= 2 or ("一" <= t <= "鿿")
    ]


def _normalize_keywords(raw: str) -> str:
    """Deduplicate and sort space-separated keywords."""
    tokens = _tokenize(raw)
    return " ".join(sorted(set(tokens)))


def _extract_keywords_from_text(text: str) -> str:
    """Extract meaningful keywords from a note / goal string."""
    # Simple heuristic: Chinese nouns are usually 2+ characters long.
    # Return unique tokens of length ≥2.
    tokens = _tokenize(text)
    meaningful = [t for t in tokens if len(t) >= 2]
    return " ".join(sorted(set(meaningful)))
