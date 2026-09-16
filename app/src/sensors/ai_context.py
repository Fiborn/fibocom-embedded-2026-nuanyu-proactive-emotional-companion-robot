# -*- coding: utf-8 -*-
"""Turn live board sensor readings into safe LLM context and announcements."""

import os
import threading
import time
from typing import Any, Dict, Optional

from src.sensors.sensor_state import get_sensor_state


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _available(health: Dict[str, Any], key: str) -> bool:
    return bool((health.get("available") or {}).get(key, False))


def build_sensor_context(
    snapshot: Optional[Dict[str, Any]] = None,
    health: Optional[Dict[str, Any]] = None,
) -> str:
    """Return concise, factual context for the LLM; never invent missing data."""
    state = get_sensor_state()
    snapshot = dict(snapshot if snapshot is not None else state.snapshot())
    health = dict(health if health is not None else state.health())

    if not health.get("connected"):
        return "板端传感器当前离线或数据已过期；不得编造环境和人体传感器数值。"

    parts = []
    fields = (
        ("temperature_c", "温度", "°C", 1),
        ("humidity_rh", "湿度", "%RH", 1),
        ("air_ppb", "空气质量", "", -1),  # text mode
        ("light", "光照", "lx", 0),
        ("heart_bpm", "心率", "次/分", 0),
        ("breath_bpm", "呼吸", "次/分", 0),
    )
    for key, label, unit, digits in fields:
        if not _available(health, key):
            continue
        value = _number(snapshot.get(key))
        if value is None:
            continue
        if key == "air_ppb":
            v = int(value)
            desc = "优" if v < 30 else "一般" if v < 150 else "较差"
            parts.append(f"{label}：{desc}")
        else:
            parts.append(f"{label}{value:.{digits}f}{unit}")

    if _available(health, "presence"):
        parts.append("毫米波检测到有人" if snapshot.get("presence") else "毫米波未检测到人")
    if _available(health, "risk_level"):
        risk = int(_number(snapshot.get("risk_level")) or 0)
        parts.append(f"风险等级{risk}")

    age = health.get("last_update_sec")
    age_num = _number(age)
    freshness = (
        f"，数据约{age_num:.0f}秒前更新"
        if age_num is not None and age_num >= 1
        else ""
    )
    if not parts:
        return "板端传感器在线，但当前没有可用测量字段；不得编造数值。"
    return (
        "板端传感器实时数据：" + "，".join(parts) + freshness + "。"
        "回答环境、空气质量、人体存在或生命体征问题时只能依据这些数据，并明确这是传感器测量值。"
    )


class SensorAnnouncementMonitor:
    """Emit one initial report and later only meaningful sensor changes."""

    def __init__(self, cooldown_seconds: Optional[float] = None):
        self.cooldown_seconds = float(
            cooldown_seconds
            if cooldown_seconds is not None
            else os.environ.get("SENSOR_ANNOUNCE_COOLDOWN_SECONDS", "1800")
        )
        self._lock = threading.Lock()
        self._last_snapshot: Optional[Dict[str, Any]] = None
        self._last_announcement = 0.0

    def poll(
        self,
        snapshot: Dict[str, Any],
        health: Dict[str, Any],
        now: Optional[float] = None,
    ) -> Optional[str]:
        now = time.time() if now is None else float(now)
        # Only live readings are worth speaking.  Announcing on stale or
        # disconnected data would present old numbers as current measurements,
        # and would monopolise TTS on every restart.
        if not health.get("connected"):
            return None

        with self._lock:
            previous = self._last_snapshot
            self._last_snapshot = dict(snapshot)

            if previous is None:
                self._last_announcement = now
                return None

            if now - self._last_announcement < self.cooldown_seconds:
                return None

            reasons = []
            temp = _number(snapshot.get("temperature_c"))
            old_temp = _number(previous.get("temperature_c"))
            if _available(health, "temperature_c") and temp is not None:
                if temp >= 30 and old_temp is not None and old_temp < 30:
                    reasons.append("温度偏高")
                elif temp <= 15 and old_temp is not None and old_temp > 15:
                    reasons.append("温度偏低")
                elif old_temp is not None and abs(temp - old_temp) >= 2:
                    reasons.append("温度变化明显")

            humidity = _number(snapshot.get("humidity_rh"))
            old_humidity = _number(previous.get("humidity_rh"))
            if _available(health, "humidity_rh") and humidity is not None:
                if humidity >= 75 and old_humidity is not None and old_humidity < 75:
                    reasons.append("湿度偏高")
                elif humidity <= 30 and old_humidity is not None and old_humidity > 30:
                    reasons.append("湿度偏低")
                elif old_humidity is not None and abs(humidity - old_humidity) >= 10:
                    reasons.append("湿度变化明显")

            air = _number(snapshot.get("air_ppb"))
            old_air = _number(previous.get("air_ppb"))
            if _available(health, "air_ppb") and air is not None:
                old_band = 2 if old_air is not None and old_air >= 500 else 1 if old_air is not None and old_air >= 150 else 0
                new_band = 2 if air >= 500 else 1 if air >= 150 else 0
                if new_band > old_band or (new_band == 2 and old_air is not None and air - old_air >= 300):
                    reasons.append("TVOC 空气质量变差")

            risk = int(_number(snapshot.get("risk_level")) or 0)
            old_risk = int(_number(previous.get("risk_level")) or 0)
            if (_available(health, "risk_level") and risk > 0
                    and risk > old_risk):
                reasons.append(f"雷达风险等级变为{risk}")

            if not reasons:
                return None
            self._last_announcement = now
            return (
                "传感器检测到" + "、".join(dict.fromkeys(reasons)) + "。"
                "请依据实时传感器数据，用一句自然中文播报变化并给出简短建议，不要夸大风险。"
            )


_ANNOUNCEMENT_MONITOR = SensorAnnouncementMonitor()


def poll_sensor_announcement() -> Optional[str]:
    if os.environ.get("SENSOR_ANNOUNCE_ENABLED", "true").strip().lower() != "true":
        return None
    state = get_sensor_state()
    return _ANNOUNCEMENT_MONITOR.poll(state.snapshot(), state.health())
