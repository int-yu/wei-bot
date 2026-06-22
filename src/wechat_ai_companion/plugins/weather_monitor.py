from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as time_type
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from .base import CompanionPlugin, PluginContext, PluginResult


WEATHER_CODE_TEXT = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "中等毛毛雨",
    55: "大毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "阵雨",
    81: "中等阵雨",
    82: "强阵雨",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴强冰雹",
}


class WeatherMonitorPlugin(CompanionPlugin):
    name = "weather_monitor"
    description = "Fetches daily weather and writes it into each user's hot context."
    default_config = {
        "check_interval_seconds": 3600,
        "run_at": "07:30",
        "timezone": "Asia/Shanghai",
        "provider": "open_meteo",
        "location_name": "北京",
        "latitude": 39.9042,
        "longitude": 116.4074,
        "forecast_days": 1,
        "request_timeout_seconds": 20,
        "write_on_message_if_cached": True,
    }

    async def on_start(self, context: PluginContext) -> list[PluginResult]:
        return [
            PluginResult(
                self.name,
                "started",
                f"location={self.config.get('location_name')} run_at={self.config.get('run_at')}",
            )
        ]

    async def on_message_received(
        self,
        context: PluginContext,
        message,
    ) -> list[PluginResult]:
        if not bool(self.config.get("write_on_message_if_cached", True)):
            return []
        if self._write_cached_weather_for_user(context, message.from_user_id):
            return [PluginResult(self.name, "cached_weather_added", message.from_user_id)]
        return []

    async def background_loop(self, context: PluginContext, stop_event: asyncio.Event) -> None:
        interval = max(60, int(self.config.get("check_interval_seconds", 3600)))
        while not stop_event.is_set():
            try:
                await self._check_daily(context)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("[plugin:%s] daily weather check failed", self.name)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _check_daily(self, context: PluginContext) -> None:
        today = self._today()
        last_run_date = context.memory.get_plugin_state(self.name, "__global__", "last_run_date")
        if last_run_date == today:
            return
        if not self._should_run_now():
            return

        weather_text = await self._fetch_weather_text()
        context.memory.set_plugin_state(self.name, "__global__", "cached_weather_date", today)
        context.memory.set_plugin_state(self.name, "__global__", "cached_weather_text", weather_text)
        context.memory.set_plugin_state(self.name, "__global__", "last_run_date", today)

        users = context.memory.list_known_users()
        for wx_user_id in users:
            self._write_weather_for_user(context, wx_user_id, today, weather_text)
        logging.info("[plugin:%s] weather fetched date=%s users=%s", self.name, today, len(users))

    def _write_cached_weather_for_user(self, context: PluginContext, wx_user_id: str) -> bool:
        today = self._today()
        cached_date = context.memory.get_plugin_state(self.name, "__global__", "cached_weather_date")
        if cached_date != today:
            return False
        weather_text = context.memory.get_plugin_state(self.name, "__global__", "cached_weather_text")
        if not weather_text:
            return False
        return self._write_weather_for_user(context, wx_user_id, today, weather_text)

    def _write_weather_for_user(self, context: PluginContext, wx_user_id: str, today: str, weather_text: str) -> bool:
        last_user_date = context.memory.get_plugin_state(self.name, wx_user_id, "last_weather_date")
        if last_user_date == today:
            return False
        context.memory.add_message(wx_user_id, "system", weather_text)
        context.memory.set_plugin_state(self.name, wx_user_id, "last_weather_date", today)
        logging.info("[plugin:%s] hot_context_weather_added user=%s date=%s", self.name, wx_user_id, today)
        return True

    async def _fetch_weather_text(self) -> str:
        provider = str(self.config.get("provider", "open_meteo"))
        if provider != "open_meteo":
            raise ValueError(f"Unsupported weather provider: {provider}")

        latitude = float(self.config["latitude"])
        longitude = float(self.config["longitude"])
        timezone_name = str(self.config.get("timezone", "Asia/Shanghai"))
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "current": ",".join(
                [
                    "temperature_2m",
                    "relative_humidity_2m",
                    "apparent_temperature",
                    "precipitation",
                    "weather_code",
                    "wind_speed_10m",
                ]
            ),
            "daily": ",".join(
                [
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_probability_max",
                ]
            ),
            "forecast_days": int(self.config.get("forecast_days", 1)),
            "timezone": timezone_name,
        }
        url = f"https://api.open-meteo.com/v1/forecast?{urlencode(params)}"
        timeout = aiohttp.ClientTimeout(total=int(self.config.get("request_timeout_seconds", 20)))
        connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=True) as session:
            async with session.get(url) as response:
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"weather HTTP {response.status}: {text[:300]}")
                data = await response.json()
        return self._format_weather(data)

    def _format_weather(self, data: dict[str, Any]) -> str:
        current = dict(data.get("current") or {})
        daily = dict(data.get("daily") or {})
        location = str(self.config.get("location_name", "未命名位置"))
        current_code = _safe_int(current.get("weather_code"))
        daily_code = _first_int(daily.get("weather_code"))
        current_desc = WEATHER_CODE_TEXT.get(current_code, f"天气代码 {current_code}") if current_code is not None else "未知天气"
        daily_desc = WEATHER_CODE_TEXT.get(daily_code, f"天气代码 {daily_code}") if daily_code is not None else current_desc

        parts = [
            f"[weather] {self._today()} {location} 天气：当前{current_desc}",
            f"气温{_fmt(current.get('temperature_2m'), '℃')}",
            f"体感{_fmt(current.get('apparent_temperature'), '℃')}",
            f"湿度{_fmt(current.get('relative_humidity_2m'), '%')}",
            f"降水{_fmt(current.get('precipitation'), 'mm')}",
            f"风速{_fmt(current.get('wind_speed_10m'), 'km/h')}",
            f"今日{daily_desc}",
            f"最高{_fmt(_first(daily.get('temperature_2m_max')), '℃')}",
            f"最低{_fmt(_first(daily.get('temperature_2m_min')), '℃')}",
            f"最大降水概率{_fmt(_first(daily.get('precipitation_probability_max')), '%')}",
        ]
        return "；".join(parts) + "。回复用户时可自然参考这条短期天气记忆；不要编造未提供的天气信息。"

    def _should_run_now(self) -> bool:
        run_at = _parse_hhmm(str(self.config.get("run_at", "")))
        if run_at is None:
            return True
        return self._now().time() >= run_at

    def _today(self) -> str:
        return self._now().date().isoformat()

    def _now(self) -> datetime:
        timezone_name = str(self.config.get("timezone", "Asia/Shanghai"))
        try:
            tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            tz = None
        return datetime.now(tz)


def _parse_hhmm(value: str) -> time_type | None:
    try:
        hour, minute = value.split(":", 1)
        return time_type(hour=int(hour), minute=int(minute))
    except Exception:
        return None


def _fmt(value: Any, suffix: str) -> str:
    if value is None:
        return "未知"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "未知"
    if number.is_integer():
        return f"{int(number)}{suffix}"
    return f"{number:.1f}{suffix}"


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_int(value: Any) -> int | None:
    return _safe_int(_first(value))
