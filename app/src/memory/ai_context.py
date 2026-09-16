#!/usr/bin/env python3
"""Build user-scoped memory context for AI requests."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional


def _compact(value: Any, limit: int = 240) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split()).strip()
    return text[:limit]


def _unique_texts(values: Iterable[Any], limit: int) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = _compact(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result[-limit:]


def build_memory_context(
    legacy_memory: Optional[Mapping[str, Any]],
    long_term_memories: Optional[Mapping[str, Any]] = None,
) -> str:
    """Return a bounded, prompt-ready summary of one user's memories."""
    memory = legacy_memory or {}
    sections: List[str] = []

    nickname = _compact(memory.get("nickname", ""))
    if nickname:
        sections.append("用户希望被称为：" + nickname)

    notes = _unique_texts(memory.get("notes", []), 20)
    if notes:
        sections.append("用户明确要求记住的内容：" + "；".join(notes))

    goals = _unique_texts(memory.get("favorite_goals", []), 5)
    if goals:
        sections.append("用户常见的学习目标：" + "；".join(goals))

    moods = _unique_texts(memory.get("recent_moods", []), 5)
    if moods:
        sections.append("用户近期心情（从旧到新）：" + "、".join(moods))

    sessions = []
    for session in list(memory.get("recent_sessions", []))[-3:]:
        if not isinstance(session, Mapping):
            continue
        goal = _compact(session.get("goal", "未知"))
        duration = _compact(session.get("duration_text", "未知"))
        sessions.append(f"{goal}（{duration}）")
    if sessions:
        sections.append("用户近期学习记录：" + "；".join(sessions))

    note_set = set(notes)
    stored_values = []
    for value in (long_term_memories or {}).values():
        text = _compact(value)
        if text and text not in note_set:
            stored_values.append(text)
    stored_values = _unique_texts(stored_values, 20)
    if stored_values:
        sections.append("其他长期记忆：" + "；".join(stored_values))

    if not sections:
        return "当前账号尚无已保存的用户记忆。"
    return (
        "以下是当前登录账号的持久记忆。回答与用户有关的问题时必须参考；"
        "不要声称记得其中没有的信息，也不要向用户泄露内部存储键。\n"
        + "\n".join("- " + section for section in sections)
    )


def build_ai_messages(
    system_prompt: str,
    scene_hint: str,
    sensor_hint: str,
    memory_context: str,
    recent_messages: Optional[Iterable[Mapping[str, Any]]],
    user_text: str,
    history_limit: int = 24,
) -> List[Dict[str, str]]:
    """Compose an AI request and remove the current turn's persisted duplicate."""
    history: List[Dict[str, str]] = []
    for message in recent_messages or []:
        role = str(message.get("role", "")).strip()
        content = _compact(message.get("content", ""), limit=1200)
        if role in ("user", "assistant") and content:
            history.append({"role": role, "content": content})
    history = history[-max(0, int(history_limit)):]

    current = _compact(user_text, limit=4000)
    if history and history[-1]["role"] == "user" and history[-1]["content"] == current:
        history.pop()

    system_parts = [
        str(system_prompt).strip(),
        str(memory_context).strip(),
        str(scene_hint).strip(),
        str(sensor_hint).strip(),
    ]
    messages = [{
        "role": "system",
        "content": "\n".join(part for part in system_parts if part),
    }]
    messages.extend(history)
    messages.append({"role": "user", "content": current})
    return messages
