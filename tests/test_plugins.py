from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from wechat_ai_companion.config import MemorySettings
from wechat_ai_companion.memory import MemoryStore
from wechat_ai_companion.plugins.base import PluginContext
from wechat_ai_companion.plugins.flow_state import FlowStatePlugin
from wechat_ai_companion.plugins.task_reminder import (
    TaskReminderPlugin,
    _parse_due_datetime,
    _parse_schedule,
)
from wechat_ai_companion.plugins.weather_monitor import WeatherMonitorPlugin
from wechat_ai_companion.wechat_openclaw import WeChatInboundMessage


def settings() -> MemorySettings:
    return MemorySettings(
        hot_min_turns=2,
        hot_max_turns=4,
        context_token_budget=200,
        compression_trigger_ratio=0.7,
        long_term_extract_every_turns=4,
    )


def test_parse_chinese_reminder_time() -> None:
    now = datetime(2026, 6, 23, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    due_at, title = _parse_due_datetime("提醒我明天下午3点交作业", now)

    assert due_at == datetime(2026, 6, 24, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert "交作业" in title


def test_parse_schedule_text() -> None:
    schedule = _parse_schedule("帮我记住周一 08:00-09:40 高数课表")

    assert schedule == {
        "title": "高数课表",
        "weekday": 1,
        "start_time": "08:00",
        "end_time": "09:40",
    }


def test_task_reminder_command_saves_item(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "test.db", settings())
    plugin = TaskReminderPlugin({"timezone": "Asia/Shanghai"})
    context = PluginContext(settings=None, wechat=None, memory=store, llm=None)  # type: ignore[arg-type]
    message = WeChatInboundMessage("user-a", "token-a", "/remind 明天 09:00 交作业", {})

    reply = asyncio.run(plugin.handle_command(context, message))

    rows = store.list_plugin_state("task_reminder")
    assert reply is not None
    assert "已记录提醒" in reply
    assert len([row for row in rows if row["wx_user_id"] == "user-a" and row["state_key"].startswith("item:")]) == 1
    store.close()


def test_weather_summary_format() -> None:
    plugin = WeatherMonitorPlugin({"location_name": "测试城市", "timezone": "Asia/Shanghai"})

    text = plugin._format_weather(
        {
            "current": {
                "temperature_2m": 22.4,
                "relative_humidity_2m": 55,
                "apparent_temperature": 23.1,
                "precipitation": 0,
                "weather_code": 1,
                "wind_speed_10m": 8.5,
            },
            "daily": {
                "weather_code": [2],
                "temperature_2m_max": [28],
                "temperature_2m_min": [18],
                "precipitation_probability_max": [30],
            },
        }
    )

    assert "[weather]" in text
    assert "测试城市" in text
    assert "气温22.4℃" in text
    assert "最大降水概率30%" in text


def test_flow_state_buffers_and_combines_messages(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "test.db", settings())
    plugin = FlowStatePlugin(
        {
            "min_silence_seconds": 0,
            "max_wait_seconds": 30,
            "decision_model_enabled": False,
        }
    )
    context = PluginContext(settings=None, wechat=None, memory=store, llm=None)  # type: ignore[arg-type]

    first = WeChatInboundMessage("user-a", "token-a", "我先说第一段", {})
    second = WeChatInboundMessage("user-a", "token-b", "还有第二段", {})

    assert asyncio.run(plugin.maybe_defer_reply(context, first)) is True
    assert asyncio.run(plugin.maybe_defer_reply(context, second)) is True
    ready = asyncio.run(plugin.pop_ready_batches(context))

    assert len(ready) == 1
    message, combined_text = ready[0]
    assert message.from_user_id == "user-a"
    assert message.context_token == "token-b"
    assert combined_text == "我先说第一段\n还有第二段"
    store.close()
