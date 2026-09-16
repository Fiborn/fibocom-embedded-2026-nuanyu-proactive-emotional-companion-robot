#!/usr/bin/env python3
"""Open-Meteo weather provider — free, no API key, stdlib only.

Uses:
  - Geocoding API  → city name → lat/lon
  - Forecast API   → lat/lon  → structured weather
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
import urllib.error
from typing import Any, Dict, Optional, Tuple

# ═══════════════════════════════════════════════════════════════════════
#  WMO weather-code → Chinese  (Open-Meteo standard codes)
# ═══════════════════════════════════════════════════════════════════════

_WMO_CN: Dict[int, str] = {
    0:  "晴天",
    1:  "大部晴朗",
    2:  "多云",
    3:  "阴天",
    45: "有雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "小冻毛毛雨",
    57: "大冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "小冻雨",
    67: "大冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "中阵雨",
    82: "大阵雨",
    85: "小阵雪",
    86: "大阵雪",
    95: "雷暴",
    96: "小冰雹雷暴",
    99: "大冰雹雷暴",
}


def _wmo_to_cn(code: int) -> str:
    return _WMO_CN.get(code, f"未知天气(code={code})")


# ═══════════════════════════════════════════════════════════════════════
#  Provider
# ═══════════════════════════════════════════════════════════════════════

_GEO_URL  = "https://geocoding-api.open-meteo.com/v1/search"
_FCAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT   = 5  # seconds


class OpenMeteoWeatherProvider:
    """Callable weather provider for ToolService.

    Usage::

        provider = OpenMeteoWeatherProvider()
        result  = provider("北京")   # → dict
    """

    def __init__(self, cache_ttl: float = 600.0):
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._cache_ttl = cache_ttl
        self._lock = threading.Lock()

    # ── Public callable interface ─────────────────────────────────

    def __call__(self, city: str, days: int = 1) -> Dict[str, Any]:
        """Return structured weather for *city*.

        Parameters
        ----------
        city: Chinese city name (e.g. "北京", "济南")
        days: forecast days 1-7

        Returns
        -------
        dict with keys: ok, city, province, country, temperature_c,
        feels_like_c, weather_cn, weather_code, precipitation_mm,
        wind_speed_kmh, daily_high_c, daily_low_c,
        precip_probability_max_pct, updated_at, source
        On failure: {ok: False, error: "…"}
        """
        city = str(city).strip()
        days = max(1, min(7, int(days)))
        if not city:
            return {"ok": False, "error": "城市名不能为空"}

        # Cache check
        cache_key = f"{city}:{days}"
        with self._lock:
            entry = self._cache.get(cache_key)
            if entry:
                ts, data = entry
                if time.time() - ts < self._cache_ttl:
                    return data

        # Fetch
        try:
            result = self._fetch(city, days)
        except Exception as exc:
            result = {"ok": False, "error": f"天气服务暂时不可用：{exc}"}
        if result.get("ok"):
            with self._lock:
                self._cache[cache_key] = (time.time(), result)
        return result

    # ── Network ───────────────────────────────────────────────────

    def _fetch(self, city: str, days: int) -> Dict[str, Any]:
        # Step 1: geocode
        geo = self._geocode(city)
        if not geo:
            return {"ok": False, "error": f"找不到城市「{city}」，请检查城市名"}

        lat, lon, name, country, admin1 = geo

        # Step 2: forecast
        return self._forecast(lat, lon, name, country, admin1, days)

    def _geocode(
        self, city: str,
    ) -> Optional[Tuple[float, float, str, str, str]]:
        """Resolve city name → (lat, lon, display_name, country, province).

        Returns None when the city is genuinely not found.
        Re-raises network/parse errors so the caller can surface them.
        """
        params = urllib.parse.urlencode({
            "name": city,
            "count": 1,
            "language": "zh",
            "format": "json",
        })
        url = f"{_GEO_URL}?{params}"
        # Let network / JSON errors propagate — only catch empty results
        data = self._get_json(url)
        results = data.get("results") or []
        if not results:
            return None
        r = results[0]
        return (
            float(r["latitude"]),
            float(r["longitude"]),
            r.get("name", city),
            r.get("country", ""),
            r.get("admin1", ""),
        )

    def _forecast(
        self, lat: float, lon: float,
        name: str, country: str, admin1: str, days: int,
    ) -> Dict[str, Any]:
        """Fetch forecast, build structured result."""
        params = urllib.parse.urlencode({
            "latitude": lat,
            "longitude": lon,
            "current": (
                "temperature_2m,apparent_temperature,weather_code,"
                "precipitation,wind_speed_10m"
            ),
            "daily": (
                "temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max"
            ),
            "timezone": "auto",
            "forecast_days": days,
        })
        url = f"{_FCAST_URL}?{params}"
        data = self._get_json(url)

        current = data.get("current", {})
        daily   = data.get("daily", {})

        wmo_code = int(current.get("weather_code", 0))
        weather_cn = _wmo_to_cn(wmo_code)

        daily_max = None
        daily_min = None
        precip_pct = None
        if daily:
            tmax = daily.get("temperature_2m_max") or []
            tmin = daily.get("temperature_2m_min") or []
            precip = daily.get("precipitation_probability_max") or []
            if tmax:
                daily_max = tmax[0]
            if tmin:
                daily_min = tmin[0]
            if precip:
                precip_pct = precip[0]

        return {
            "ok": True,
            "city": name,
            "province": admin1,
            "country": country,
            "temperature_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "weather_cn": weather_cn,
            "weather_code": wmo_code,
            "precipitation_mm": current.get("precipitation"),
            "wind_speed_kmh": current.get("wind_speed_10m"),
            "daily_high_c": daily_max,
            "daily_low_c": daily_min,
            "precip_probability_max_pct": precip_pct,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "source": "open-meteo",
        }

    def _get_json(self, url: str) -> Dict[str, Any]:
        """GET *url*, return parsed JSON. Raises on failure."""
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"天气服务暂时不可用：{_url_error_msg(exc)}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"天气数据解析失败") from exc
        except Exception as exc:
            raise RuntimeError(f"天气服务暂时不可用") from exc


def _url_error_msg(exc: urllib.error.URLError) -> str:
    """Human-readable message for common URLError causes."""
    reason = exc.reason
    if isinstance(reason, TimeoutError):
        return "请求超时"
    if hasattr(reason, "errno"):
        return f"网络连接失败(errno={reason.errno})"
    return str(reason)[:60]
