#!/usr/bin/env python3
"""Per-user character configuration and prompt helpers."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional


PERSONA_PRESETS: Dict[str, Dict[str, Any]] = {
    "default": {
        "label": "温柔陪伴",
        "personality": "温柔、耐心、善于倾听，有同理心但不过度说教",
        "language_style": "自然、简短、口语化，先回应感受再给建议",
        "catchphrases": [],
    },
    "calm_boy": {
        "label": "沉稳理性",
        "personality": "沉稳、理性、可靠，遇到问题会帮助用户梳理思路",
        "language_style": "克制清晰、语气平和，建议具体但不命令用户",
        "catchphrases": [],
    },
    "gentle_girl": {
        "label": "温暖活泼",
        "personality": "温暖、活泼、细腻，擅长鼓励并关注用户情绪",
        "language_style": "轻松自然、有亲和力，不过分卖萌",
        "catchphrases": [],
    },
}


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _split_catchphrases(value: Any) -> List[str]:
    if isinstance(value, str):
        values: Iterable[Any] = re.split(r"[\n,，;；]+", value)
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        values = []
    result: List[str] = []
    seen = set()
    for item in values:
        text = _clean_text(item, 30)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
        if len(result) >= 5:
            break
    return result


def default_persona_config() -> Dict[str, Any]:
    preset = PERSONA_PRESETS["default"]
    return {
        "persona_id": "default",
        "personality": preset["personality"],
        "language_style": preset["language_style"],
        "catchphrases": [],
    }


def sanitize_persona_config(
    value: Optional[Mapping[str, Any]],
    current: Optional[Mapping[str, Any]] = None,
    apply_preset: bool = False,
) -> Dict[str, Any]:
    """Validate a partial update and return a complete safe configuration."""
    data = dict(value or {})
    result = default_persona_config()
    if current:
        result.update({
            "persona_id": _clean_text(current.get("persona_id", "default"), 32),
            "personality": _clean_text(current.get("personality", ""), 200),
            "language_style": _clean_text(current.get("language_style", ""), 160),
            "catchphrases": _split_catchphrases(current.get("catchphrases", [])),
        })

    persona_id = _clean_text(data.get("persona_id", result["persona_id"]), 32)
    if persona_id not in PERSONA_PRESETS and persona_id != "custom":
        persona_id = "custom"
    if apply_preset and persona_id in PERSONA_PRESETS:
        preset = PERSONA_PRESETS[persona_id]
        result.update({
            "personality": preset["personality"],
            "language_style": preset["language_style"],
            "catchphrases": list(preset["catchphrases"]),
        })
    result["persona_id"] = persona_id

    if "personality" in data:
        result["personality"] = _clean_text(data["personality"], 200)
    if "language_style" in data:
        result["language_style"] = _clean_text(data["language_style"], 160)
    if "catchphrases" in data:
        result["catchphrases"] = _split_catchphrases(data["catchphrases"])

    if not result["personality"]:
        raise ValueError("请填写角色性格")
    if not result["language_style"]:
        raise ValueError("请填写说话风格")
    return result


def public_presets() -> List[Dict[str, Any]]:
    return [
        {
            "persona_id": persona_id,
            "label": preset["label"],
            "personality": preset["personality"],
            "language_style": preset["language_style"],
            "catchphrases": list(preset["catchphrases"]),
        }
        for persona_id, preset in PERSONA_PRESETS.items()
    ]


def build_user_persona_prompt(
    config: Mapping[str, Any],
    seed: str,
    catchphrase_probability: float = 0.22,
) -> str:
    """Build personality instructions with deterministic low-frequency phrases."""
    personality = _clean_text(config.get("personality", ""), 200)
    language_style = _clean_text(config.get("language_style", ""), 160)
    phrases = _split_catchphrases(config.get("catchphrases", []))
    parts = [
        "当前账号的角色设定如下，回答时保持一致。",
        "性格：" + personality + "。",
        "说话风格：" + language_style + "。",
        "不要机械重复固定句式，不要为了体现人设而牺牲自然交流。",
    ]

    if phrases:
        digest = hashlib.sha256(str(seed).encode("utf-8")).digest()
        threshold = max(0, min(255, int(float(catchphrase_probability) * 256)))
        if digest[0] < threshold:
            phrase = phrases[digest[1] % len(phrases)]
            parts.append(
                "本轮若语境自然，可以最多使用一次口头禅“%s”；"
                "不合适就不要使用，绝不能把它当作每句结尾。" % phrase
            )
        else:
            parts.append("本轮不要使用口头禅，保持表达自然多样。")
    return "".join(parts)
