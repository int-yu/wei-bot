from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import wechat_ai_companion.plugins.manager as plugin_manager_module
from wechat_ai_companion.config import MemorySettings
from wechat_ai_companion.memory import MemoryStore
from wechat_ai_companion.plugins.base import CompanionPlugin, PluginContext, PluginEvent, PluginEvents
from wechat_ai_companion.plugins.flow_state import FlowStatePlugin
from wechat_ai_companion.plugins.manager import PluginManager, _coerce_config, _public_config
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


def test_plugin_config_validation_and_secret_redaction() -> None:
    schema = {
        "type": "object",
        "properties": {
            "interval": {"type": "integer", "minimum": 1, "maximum": 60},
            "mode": {"type": "string", "enum": ["safe", "fast"]},
            "api_key": {"type": "string", "secret": True},
        },
    }
    current = {"interval": 10, "mode": "safe", "api_key": "secret-value"}

    updated = _coerce_config(
        schema,
        current,
        {"interval": "20", "mode": "fast", "api_key": ""},
    )

    assert updated == {"interval": 20, "mode": "fast", "api_key": "secret-value"}
    assert _public_config(schema, updated)["api_key"] == ""

    with pytest.raises(ValueError, match="at most 60"):
        _coerce_config(schema, current, {"interval": 61})
    with pytest.raises(ValueError, match="Unknown plugin config"):
        _coerce_config(schema, current, {"not_declared": True})


class RecordingPlugin(CompanionPlugin):
    name = "recording_plugin"

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    async def on_event(self, context: PluginContext, event: PluginEvent):
        self.events.append(event.event_type)
        return []


class BrokenPlugin(CompanionPlugin):
    name = "broken_plugin"

    async def on_event(self, context: PluginContext, event: PluginEvent):
        raise RuntimeError("expected test failure")


class ExternalPlugin(CompanionPlugin):
    name = "external_plugin"


class FakeEntryPoint:
    name = "external_plugin"

    @staticmethod
    def load():
        return ExternalPlugin


def test_external_plugin_entry_point_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        plugin_manager_module,
        "entry_points",
        lambda **kwargs: [FakeEntryPoint()],
    )

    discovered = plugin_manager_module.discover_plugin_classes()

    assert discovered["external_plugin"] is ExternalPlugin


def test_event_bus_isolates_plugin_failure(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "test.db", settings())
    plugin_settings = SimpleNamespace(plugins=SimpleNamespace(enabled={}, config={}))
    manager = PluginManager(plugin_settings, None, store, None)  # type: ignore[arg-type]
    recorder = RecordingPlugin()
    manager.plugins = {"broken_plugin": BrokenPlugin(), "recording_plugin": recorder}
    manager._enabled = {"broken_plugin": True, "recording_plugin": True}

    asyncio.run(manager.emit_event(PluginEvents.MESSAGE_RECEIVED, payload={"value": 1}))

    assert recorder.events == [PluginEvents.MESSAGE_RECEIVED]
    store.close()


def test_event_only_plugin_reports_running_after_manager_start(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "test.db", settings())
    plugin_settings = SimpleNamespace(plugins=SimpleNamespace(enabled={}, config={}))
    manager = PluginManager(plugin_settings, None, store, None)  # type: ignore[arg-type]
    manager.plugins = {"recording_plugin": RecordingPlugin()}
    manager._enabled = {"recording_plugin": True}

    async def run() -> list[dict]:
        await manager.start()
        status = manager.list_status()
        await manager.stop()
        return status

    status = asyncio.run(run())

    assert status[0]["running"] is True
    assert status[0]["has_background_task"] is False
    store.close()


def test_plugin_web_config_persists_across_manager_restart(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "test.db", settings())
    plugin_settings = SimpleNamespace(
        plugins=SimpleNamespace(
            enabled={"flow_state": False},
            config={"flow_state": {"min_silence_seconds": 6}},
        )
    )
    manager = PluginManager(plugin_settings, None, store, None)  # type: ignore[arg-type]

    saved = asyncio.run(
        manager.set_config(
            "flow_state",
            {"min_silence_seconds": 12, "decision_model_enabled": False},
        )
    )
    reloaded = PluginManager(plugin_settings, None, store, None)  # type: ignore[arg-type]

    assert saved["min_silence_seconds"] == 12
    assert reloaded.plugins["flow_state"].config["min_silence_seconds"] == 12
    assert reloaded.plugins["flow_state"].config["decision_model_enabled"] is False
    store.close()
