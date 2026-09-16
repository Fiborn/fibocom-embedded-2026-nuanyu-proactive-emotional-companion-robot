#!/usr/bin/env python3
"""PersonaService — JSON-file-backed character personality management.

Phase 2B: real implementation replacing Phase 2A NoOp placeholder.
Agent B owns this file.
"""

from __future__ import annotations
import json
import os
from src.ports.persona import Persona, PersonaService, VoiceProfile

# The application's own directory (this file lives at app/src/services/).
APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PERSONAS_DIR = os.path.join(APP_DIR, "data", "personas")

_DEFAULT_PERSONA = Persona(
    persona_id="default", name="陪伴助手",
    identity="温柔中文桌面陪伴机器人", tone="温柔",
    language_style="简短中文", catchphrases=[],
    knowledge_background="", proactivity_level=5,
    encourage_style="温柔鼓励",
    default_voice=VoiceProfile(voice_id="default_drizzle",
        display_name="Drizzle 默认女声", tts_backend="drizzle"))

class NoOpPersonaService(PersonaService):
    """Always returns the built-in default persona (Phase 2A stub)."""
    def __init__(self):
        self._personas = {"default": _DEFAULT_PERSONA}
        self._voice_bindings = {"default": _DEFAULT_PERSONA.default_voice}
    def get_persona(self, persona_id="default"): return self._personas.get(persona_id, _DEFAULT_PERSONA)
    def update_persona(self, persona_id, **kwargs):
        p = self._personas.get(persona_id, _DEFAULT_PERSONA)
        for f, v in kwargs.items():
            if hasattr(p, f): setattr(p, f, v)
        self._personas[persona_id] = p; return p
    def list_personas(self): return list(self._personas.values())
    def get_voice_for_persona(self, pid):
        return self._voice_bindings.get(pid, VoiceProfile(voice_id="default_drizzle"))
    def bind_voice(self, pid, vp): self._voice_bindings[pid] = vp
    def build_system_prompt(self, persona_id="default", extra_context=None):
        p = self.get_persona(persona_id); e = extra_context or {}
        role_name = str(e.get("role_name") or p.name or "陪伴助手").strip()
        prompt = f"你的名字叫{role_name}。你是{p.identity}。你必须自称'{role_name}'或'我'。回复1-2句中文，口语化，不要Markdown。语气：{p.tone}。风格：{p.language_style}。"
        if p.catchphrases: prompt += f"口头禅：{'；'.join(p.catchphrases)}。"
        if p.knowledge_background: prompt += f"背景：{p.knowledge_background}。"
        if e.get("mood"): prompt += f"用户心情：{e['mood']}。"
        if e.get("goal"): prompt += f"当前目标：{e['goal']}。"
        return prompt
    def health(self): return {"backend": "noop", "persona_count": len(self._personas)}
    def close(self): pass

class JsonFilePersonaService(PersonaService):
    """JSON-file-backed persona persistence."""
    def __init__(self, personas_dir=PERSONAS_DIR):
        self._dir = personas_dir; os.makedirs(self._dir, exist_ok=True)
        self._voice_bindings = {}; self._ensure_default()
    def _ensure_default(self):
        p = os.path.join(self._dir, "default.json")
        if not os.path.exists(p): self._save("default", _DEFAULT_PERSONA)
    def _path(self, pid):
        if ".." in pid or "/" in pid or "\\" in pid:
            raise ValueError(f"Invalid persona_id: {pid}")
        return os.path.join(self._dir, f"{pid}.json")
    def _load(self, pid):
        p = self._path(pid)
        if not os.path.exists(p): return None
        try:
            with open(p, "r", encoding="utf-8") as f: data = json.load(f)
            vp_d = data.pop("default_voice", None)
            vp = VoiceProfile(**vp_d) if vp_d else None
            per = Persona(**data); per.default_voice = vp; return per
        except Exception: return None
    def _save(self, pid, per):
        with open(self._path(pid), "w", encoding="utf-8") as f:
            json.dump(per.to_dict(), f, ensure_ascii=False, indent=2)
    def get_persona(self, persona_id="default"):
        p = self._load(persona_id); return p if p else _DEFAULT_PERSONA
    def update_persona(self, persona_id, **kwargs):
        p = self.get_persona(persona_id)
        allowed = {"name","identity","tone","language_style","catchphrases","knowledge_background","proactivity_level","encourage_style"}
        for f, v in kwargs.items():
            if f in allowed and hasattr(p, f): setattr(p, f, v)
        self._save(persona_id, p); return p
    def list_personas(self):
        ps = []
        try:
            for fn in os.listdir(self._dir):
                if fn.startswith("_") or not fn.endswith(".json"): continue
                p = self._load(fn[:-5])
                if p: ps.append(p)
        except OSError: pass
        return ps if ps else [_DEFAULT_PERSONA]
    def get_voice_for_persona(self, pid):
        vp = self._voice_bindings.get(pid)
        if vp: return vp
        p = self.get_persona(pid)
        return p.default_voice or VoiceProfile(voice_id="default_drizzle")
    def bind_voice(self, pid, vp): self._voice_bindings[pid] = vp
    def build_system_prompt(self, persona_id="default", extra_context=None):
        p = self.get_persona(persona_id); e = extra_context or {}
        role_name = str(e.get("role_name") or p.name or "陪伴助手").strip()
        parts = [f"你的名字叫{role_name}。你是{p.identity}。", f"你必须自称'{role_name}'或'我'。",
                 "回复1-2句中文，口语化，不要Markdown。", "用户难过/焦虑/疲惫时先安慰，再给一个小建议。", "不要说教，不要暴露模型身份。"]
        if p.tone: parts.append(f"语气：{p.tone}。")
        if p.language_style: parts.append(f"风格：{p.language_style}。")
        if p.catchphrases: parts.append(f"口头禅：{'；'.join(p.catchphrases)}。")
        if p.knowledge_background: parts.append(f"背景：{p.knowledge_background}。")
        if e.get("mood"): parts.append(f"用户心情：{e['mood']}。")
        if e.get("goal"): parts.append(f"当前目标：{e['goal']}。")
        return "".join(parts)
    def health(self): return {"backend": "json_file", "persona_count": len(self.list_personas())}
    def close(self): pass
