from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import re
from importlib.metadata import entry_points
from typing import Any

from ..config import Settings
from ..llm import ModelRouter
from ..memory import MemoryStore
from ..wechat_openclaw import OpenClawWeChatClient, WeChatInboundMessage
from .base import (
    CompanionPlugin,
    DeferredReplyHandler,
    PluginContext,
    PluginEvent,
    PluginEvents,
    PluginResult,
)
from .flow_state import FlowStatePlugin
from .proactive_response import ProactiveResponsePlugin
from .task_reminder import TaskReminderPlugin
from .weather_monitor import WeatherMonitorPlugin


BUILTIN_PLUGINS: dict[str, type[CompanionPlugin]] = {
    FlowStatePlugin.name: FlowStatePlugin,
    ProactiveResponsePlugin.name: ProactiveResponsePlugin,
    WeatherMonitorPlugin.name: WeatherMonitorPlugin,
    TaskReminderPlugin.name: TaskReminderPlugin,
}
PLUGIN_ENTRY_POINT_GROUP = "wechat_ai_companion.plugins"


class PluginManager:
    def __init__(
        self,
        settings: Settings,
        wechat: OpenClawWeChatClient,
        memory: MemoryStore,
        llm: ModelRouter,
    ) -> None:
        self.context = PluginContext(settings=settings, wechat=wechat, memory=memory, llm=llm)
        self.plugin_classes = discover_plugin_classes()
        self.plugins = self._load_plugins(settings)
        self._enabled = self._load_enabled(settings)
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_event = asyncio.Event()
        self._started = False

    def set_deferred_reply_handler(self, handler: DeferredReplyHandler) -> None:
        self.context.deferred_reply_handler = handler

    def _load_plugins(self, settings: Settings) -> dict[str, CompanionPlugin]:
        loaded: dict[str, CompanionPlugin] = {}
        for name, plugin_cls in self.plugin_classes.items():
            plugin_config = dict(settings.plugins.config.get(name, {}))
            saved_config = self.context.memory.get_plugin_state("core_plugin_config", "__global__", name)
            if saved_config:
                try:
                    saved = json.loads(saved_config)
                    if isinstance(saved, dict):
                        plugin_config.update(saved)
                except json.JSONDecodeError:
                    logging.warning("[plugin:%s] ignored invalid saved config", name)
            try:
                plugin = plugin_cls(plugin_config)
            except Exception:
                logging.exception("[plugin:%s] failed to initialize", name)
                continue
            loaded[name] = plugin
        return loaded

    def _load_enabled(self, settings: Settings) -> dict[str, bool]:
        enabled: dict[str, bool] = {}
        for name in self.plugins:
            saved = self.context.memory.get_plugin_state("core_plugins", "__global__", name)
            enabled[name] = (saved.lower() == "true") if saved else settings.plugins.enabled.get(name, False)
            logging.info("[plugin:%s] %s", name, "enabled" if enabled[name] else "disabled")
        return enabled

    async def start(self) -> None:
        self._stop_event = asyncio.Event()
        self._started = True
        for name, plugin in self.plugins.items():
            if self._enabled.get(name, False):
                await self._start_plugin(name, plugin)

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        for name, plugin in self.plugins.items():
            if self._enabled.get(name, False):
                await self._call_hook(name, plugin, "on_stop", self.context)
        self._started = False

    def list_status(self) -> list[dict]:
        return [
            {
                "name": name,
                "description": str(plugin.description),
                "version": str(plugin.version),
                "author": str(plugin.author),
                "homepage": str(plugin.homepage),
                "module": plugin.__class__.__module__,
                "enabled": bool(self._enabled.get(name, False)),
                "manager_started": self._started,
                "running": self._started
                and bool(self._enabled.get(name, False))
                and (
                    not _has_background_task(plugin)
                    or (name in self._tasks and not self._tasks[name].done())
                ),
                "has_background_task": _has_background_task(plugin),
                "config": _public_config(plugin.public_config_schema(), plugin.config),
                "config_schema": _public_schema(plugin.public_config_schema(), plugin.config),
            }
            for name, plugin in self.plugins.items()
        ]

    def is_enabled(self, name: str) -> bool:
        return bool(self._enabled.get(name, False))

    async def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in self.plugins:
            raise ValueError(f"Unknown plugin: {name}")
        was_enabled = self._enabled.get(name, False)
        self._enabled[name] = enabled
        self.context.memory.set_plugin_state("core_plugins", "__global__", name, "true" if enabled else "false")
        if self._started and enabled and not was_enabled:
            plugin = self.plugins[name]
            await self._start_plugin(name, plugin)
            logging.info("[plugin:%s] enabled at runtime", name)
        elif self._started and not enabled and was_enabled:
            if name in self._tasks:
                self._tasks[name].cancel()
                await asyncio.gather(self._tasks[name], return_exceptions=True)
                del self._tasks[name]
            await self._call_hook(name, self.plugins[name], "on_stop", self.context)
            logging.info("[plugin:%s] disabled at runtime", name)
        await self.emit_event(
            PluginEvents.PLUGIN_ENABLED_CHANGED,
            payload={"name": name, "enabled": enabled},
        )

    async def set_config(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        if name not in self.plugins:
            raise ValueError(f"Unknown plugin: {name}")
        plugin = self.plugins[name]
        next_config = _coerce_config(plugin.public_config_schema(), plugin.config, config)
        plugin.update_config(next_config)
        self.context.memory.set_plugin_state(
            "core_plugin_config",
            "__global__",
            name,
            json.dumps(plugin.config, ensure_ascii=False),
        )
        if self._started and self.is_enabled(name):
            if name in self._tasks:
                self._tasks[name].cancel()
                await asyncio.gather(self._tasks[name], return_exceptions=True)
                del self._tasks[name]
            await self._call_hook(name, plugin, "on_stop", self.context)
            await self._start_plugin(name, plugin)
        logging.info("[plugin:%s] config updated at runtime", name)
        await self.emit_event(
            PluginEvents.PLUGIN_CONFIG_CHANGED,
            payload={"name": name, "config": _public_config(plugin.public_config_schema(), plugin.config)},
        )
        return _public_config(plugin.public_config_schema(), plugin.config)

    async def emit_event(
        self,
        event_type: str,
        *,
        source: str = "core",
        payload: dict[str, Any] | None = None,
    ) -> None:
        event = PluginEvent(event_type=event_type, source=source, payload=payload or {})
        for name, plugin in self.plugins.items():
            if not self.is_enabled(name):
                continue
            await self._call_hook(name, plugin, "on_event", self.context, event)

    async def on_message_received(self, message: WeChatInboundMessage) -> None:
        await self.emit_event(PluginEvents.MESSAGE_RECEIVED, payload={"message": message})
        for name, plugin in self.plugins.items():
            if not self.is_enabled(name):
                continue
            await self._call_hook(name, plugin, "on_message_received", self.context, message)

    async def handle_command(self, message: WeChatInboundMessage) -> str | None:
        for name, plugin in self.plugins.items():
            if not self.is_enabled(name):
                continue
            reply = await self._call_hook(name, plugin, "handle_command", self.context, message)
            if reply is not None:
                logging.info("[plugin:%s] handled_command user=%s", name, message.from_user_id)
                await self.emit_event(
                    PluginEvents.COMMAND_HANDLED,
                    source=name,
                    payload={"message": message, "reply": reply},
                )
                return reply
        return None

    async def maybe_defer_reply(self, message: WeChatInboundMessage) -> bool:
        for name, plugin in self.plugins.items():
            if not self.is_enabled(name):
                continue
            deferred = await self._call_hook(name, plugin, "maybe_defer_reply", self.context, message)
            if deferred:
                logging.info("[plugin:%s] deferred_reply user=%s", name, message.from_user_id)
                await self.emit_event(
                    PluginEvents.REPLY_DEFERRED,
                    source=name,
                    payload={"message": message},
                )
                return True
        return False

    async def after_ai_reply(self, message: WeChatInboundMessage, reply: str) -> None:
        await self.emit_event(PluginEvents.REPLY_SENT, payload={"message": message, "reply": reply})
        for name, plugin in self.plugins.items():
            if not self.is_enabled(name):
                continue
            await self._call_hook(name, plugin, "after_ai_reply", self.context, message, reply)

    async def after_memory_maintenance(
        self,
        wx_user_id: str,
        *,
        extracted_count: int,
        compressed: bool,
    ) -> None:
        await self.emit_event(
            PluginEvents.MEMORY_MAINTENANCE,
            payload={
                "wx_user_id": wx_user_id,
                "extracted_count": extracted_count,
                "compressed": compressed,
            },
        )
        for name, plugin in self.plugins.items():
            if not self.is_enabled(name):
                continue
            await self._call_hook(
                name,
                plugin,
                "after_memory_maintenance",
                self.context,
                wx_user_id,
                extracted_count,
                compressed,
            )

    async def _start_plugin(self, name: str, plugin: CompanionPlugin) -> None:
        await self._call_hook(name, plugin, "on_start", self.context)
        if _has_background_task(plugin):
            self._tasks[name] = asyncio.create_task(
                self._run_background(name, plugin),
                name=f"plugin:{name}",
            )

    async def _run_background(self, name: str, plugin: CompanionPlugin) -> None:
        try:
            await plugin.background_loop(self.context, self._stop_event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[plugin:%s] background task failed", name)

    async def _call_hook(
        self,
        name: str,
        plugin: CompanionPlugin,
        hook_name: str,
        *args: Any,
    ) -> Any:
        hook = getattr(plugin, hook_name)
        timeout = max(0.1, float(plugin.hook_timeout_seconds))
        try:
            result = await asyncio.wait_for(hook(*args), timeout=timeout)
        except asyncio.TimeoutError:
            logging.error("[plugin:%s] hook timed out hook=%s timeout=%ss", name, hook_name, timeout)
            return None
        except Exception:
            logging.exception("[plugin:%s] hook failed hook=%s", name, hook_name)
            return None
        if isinstance(result, list):
            self._log_results(result)
        return result

    @staticmethod
    def _log_results(results: list[PluginResult]) -> None:
        for result in results:
            if result.detail:
                logging.info("[plugin:%s] %s - %s", result.plugin, result.action, result.detail)
            else:
                logging.info("[plugin:%s] %s", result.plugin, result.action)


def discover_plugin_classes() -> dict[str, type[CompanionPlugin]]:
    discovered = dict(BUILTIN_PLUGINS)
    try:
        candidates = entry_points(group=PLUGIN_ENTRY_POINT_GROUP)
    except TypeError:
        candidates = entry_points().get(PLUGIN_ENTRY_POINT_GROUP, [])

    for entry_point in candidates:
        try:
            plugin_class = entry_point.load()
        except Exception:
            logging.exception("[plugin] failed to load entry point %s", entry_point.name)
            continue
        if not isinstance(plugin_class, type) or not issubclass(plugin_class, CompanionPlugin):
            logging.error("[plugin] entry point %s must export a CompanionPlugin subclass", entry_point.name)
            continue
        name = str(plugin_class.name).strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name):
            logging.error("[plugin] ignored invalid plugin name from entry point %s: %r", entry_point.name, name)
            continue
        if name in discovered:
            logging.error("[plugin] ignored duplicate plugin name from entry point %s: %s", entry_point.name, name)
            continue
        discovered[name] = plugin_class
        logging.info("[plugin:%s] discovered entry_point=%s", name, entry_point.name)
    return discovered


def _has_background_task(plugin: CompanionPlugin) -> bool:
    return plugin.__class__.background_loop is not CompanionPlugin.background_loop


def _coerce_config(schema: dict[str, Any], current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    properties = dict(schema.get("properties", {}))
    if not properties:
        return dict(incoming)

    unknown = sorted(set(incoming) - set(properties))
    if unknown:
        raise ValueError(f"Unknown plugin config field(s): {', '.join(unknown)}")

    next_config = dict(current)
    for key, spec in properties.items():
        if key not in incoming:
            continue
        value = incoming[key]
        if bool(spec.get("secret")) and value in (None, ""):
            continue
        next_config[key] = _coerce_value(key, value, dict(spec))
    return next_config


def _coerce_value(key: str, value: Any, spec: dict[str, Any]) -> Any:
    value_type = spec.get("type")
    if value_type == "boolean":
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{key} must be a boolean")
    if value_type == "integer":
        if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
            raise ValueError(f"{key} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be an integer") from None
        return _check_numeric_range(key, parsed, spec)
    if value_type == "number":
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a number")
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number") from None
        if not math.isfinite(parsed):
            raise ValueError(f"{key} must be a finite number")
        return _check_numeric_range(key, parsed, spec)
    if value_type == "array":
        if isinstance(value, list):
            parsed = value
        elif value in (None, ""):
            parsed = []
        else:
            parsed = [line.strip() for line in str(value).splitlines() if line.strip()]
        item_type = dict(spec.get("items") or {}).get("type")
        if item_type == "string" and not all(isinstance(item, str) for item in parsed):
            raise ValueError(f"{key} must contain only strings")
        if "minItems" in spec and len(parsed) < int(spec["minItems"]):
            raise ValueError(f"{key} must contain at least {spec['minItems']} item(s)")
        if "maxItems" in spec and len(parsed) > int(spec["maxItems"]):
            raise ValueError(f"{key} must contain at most {spec['maxItems']} item(s)")
        return parsed
    if value_type == "object":
        if isinstance(value, dict):
            return value
        if value in (None, ""):
            return {}
        try:
            parsed = json.loads(str(value))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{key} must be a valid JSON object") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{key} must be a JSON object")
        return parsed

    parsed = "" if value is None else str(value)
    if "enum" in spec and parsed not in spec["enum"]:
        raise ValueError(f"{key} must be one of: {', '.join(map(str, spec['enum']))}")
    if "minLength" in spec and len(parsed) < int(spec["minLength"]):
        raise ValueError(f"{key} is too short")
    if "maxLength" in spec and len(parsed) > int(spec["maxLength"]):
        raise ValueError(f"{key} is too long")
    if pattern := spec.get("pattern"):
        if not re.fullmatch(str(pattern), parsed):
            raise ValueError(f"{key} has an invalid format")
    return parsed


def _check_numeric_range(key: str, value: int | float, spec: dict[str, Any]) -> int | float:
    if "minimum" in spec and value < spec["minimum"]:
        raise ValueError(f"{key} must be at least {spec['minimum']}")
    if "maximum" in spec and value > spec["maximum"]:
        raise ValueError(f"{key} must be at most {spec['maximum']}")
    return value


def _public_config(schema: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(config)
    for key, spec in dict(schema.get("properties", {})).items():
        if bool(spec.get("secret")) and key in result:
            result[key] = ""
    return result


def _public_schema(schema: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(schema)
    for key, spec in dict(result.get("properties", {})).items():
        if bool(spec.get("secret")):
            spec["configured"] = bool(config.get(key))
    return result
