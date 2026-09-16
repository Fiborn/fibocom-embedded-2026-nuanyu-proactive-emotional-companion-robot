#!/usr/bin/env python3
"""PersonaService — character personality, catchphrase, tone configuration.

Agent B owns this file and src/services/persona_service.py.
"""

from __future__ import annotations
import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.ports.memory import MemoryService


# ── VoiceProfile: links a persona to a TTS voice pack ─────────

@dataclass
class VoiceProfile:
    voice_id: str = ""
    display_name: str = ""               # shown in UI
    tts_backend: str = "drizzle"         # drizzle | stream | surge
    surge_voice_id: Optional[str] = None  # only when backend=surge

    def to_dict(self) -> Dict[str, Any]:
        return {
            "voice_id": self.voice_id,
            "display_name": self.display_name,
            "tts_backend": self.tts_backend,
            "surge_voice_id": self.surge_voice_id,
        }


# ── Persona: character definition ──────────────────────────────

@dataclass
class Persona:
    persona_id: str = "default"
    name: str = "小陪"
    identity: str = "温柔中文桌面陪伴机器人"
    tone: str = "温柔"                    # 温柔 (gentle) / 活泼 (lively) / 沉稳 (calm) / 幽默 (humorous)
    language_style: str = "简短中文"       # 简短中文 (short Chinese) / 口语化 (colloquial) / …
    catchphrases: List[str] = field(default_factory=list)  # catchphrase list
    knowledge_background: str = ""        # character knowledge background
    proactivity_level: int = 5            # proactivity level, 1-10
    encourage_style: str = "温柔鼓励"      # encouragement style
    default_voice: Optional[VoiceProfile] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "persona_id": self.persona_id,
            "name": self.name, "identity": self.identity,
            "tone": self.tone, "language_style": self.language_style,
            "catchphrases": list(self.catchphrases),
            "knowledge_background": self.knowledge_background,
            "proactivity_level": self.proactivity_level,
            "encourage_style": self.encourage_style,
        }
        if self.default_voice:
            d["default_voice"] = self.default_voice.to_dict()
        return d


# ── Abstract interface ────────────────────────────────────────

class PersonaService(abc.ABC):
    """Manage character definition, tone, catchphrases, and voice binding.

    Phase 2:  load/save personas from JSON files (the existing
    memory per-user structure can be reused for storage).

    Does NOT expose a separate "change TTS engine" API — that is
    owned by the TTS manager in the main process.
    """

    @abc.abstractmethod
    def get_persona(self, persona_id: str = "default") -> Persona:
        """Return the persona definition."""
        ...

    @abc.abstractmethod
    def update_persona(self, persona_id: str, **kwargs) -> Persona:
        """Update persona fields.  Returns the updated Persona."""
        ...

    @abc.abstractmethod
    def list_personas(self) -> List[Persona]:
        """Return all defined personas."""
        ...

    @abc.abstractmethod
    def get_voice_for_persona(self, persona_id: str) -> VoiceProfile:
        """Return the voice profile bound to this persona."""
        ...

    @abc.abstractmethod
    def bind_voice(
        self, persona_id: str, voice_profile: VoiceProfile,
    ) -> None:
        """Link a TTS voice to a persona."""
        ...

    @abc.abstractmethod
    def build_system_prompt(
        self, persona_id: str = "default",
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Return the full system prompt string for this persona.

        The main process prepends this before calling the LLM.
        """
        ...

    @abc.abstractmethod
    def health(self) -> Dict[str, Any]:
        ...

    @abc.abstractmethod
    def close(self) -> None:
        ...
