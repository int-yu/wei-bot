from __future__ import annotations

import asyncio
import logging

from ..config import Settings
from ..llm import ModelRouter
from ..memory import MemoryStore
from ..wechat_openclaw import OpenClawWeChatClient, WeChatInboundMessage
from .base import CompanionPlugin, PluginContext, PluginResult
from .proactive_response import ProactiveResponsePlugin


BUILTIN_PLUGINS: dict[str, type[CompanionPlugin]] = {
    ProactiveResponsePlugin.name: ProactiveResponsePlugin,
}


class PluginManager:
    def __init__(
        self,
        settings: Settings,
        wechat: OpenClawWeChatClient,
        memory: MemoryStore,
        llm: ModelRouter,
    ) -> None:
        self.context = PluginContext(settings=settings, wechat=wechat, memory=memory, llm=llm)
        self.plugins = self._load_plugins(settings)
        self._enabled = self._load_enabled(settings)
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_event = asyncio.Event()
        self._started = False

    def _load_plugins(self, settings: Settings) -> dict[str, CompanionPlugin]:
        loaded: dict[str, CompanionPlugin] = {}
        for name, plugin_cls in BUILTIN_PLUGINS.items():
            plugin_config = dict(settings.plugins.config.get(name, {}))
            plugin = plugin_cls(plugin_config)
            loaded[name] = plugin
        return loaded

    def _load_enabled(self, settings: Settings) -> dict[str, bool]:
        enabled: dict[str, bool] = {}
        for name in BUILTIN_PLUGINS:
            saved = self.context.memory.get_plugin_state("core_plugins", "__global__", name)
            enabled[name] = (saved.lower() == "true") if saved else settings.plugins.enabled.get(name, False)
            logging.info("[plugin:%s] %s", name, "enabled" if enabled[name] else "disabled")
        return enabled

    async def start(self) -> None:
        self._started = True
        for name, plugin in self.plugins.items():
            if self._enabled.get(name, False):
                self._log_results(await plugin.on_start(self.context))
                self._tasks[name] = asyncio.create_task(plugin.background_loop(self.context, self._stop_event))

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    def list_status(self) -> list[dict]:
        return [
            {
                "name": name,
                "description": plugin.description,
                "enabled": bool(self._enabled.get(name, False)),
                "running": name in self._tasks and not self._tasks[name].done(),
                "config": plugin.config,
            }
            for name, plugin in self.plugins.items()
        ]

    def is_enabled(self, name: str) -> bool:
        return bool(self._enabled.get(name, False))

    async def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in self.plugins:
            raise ValueError(f"Unknown plugin: {name}")
        self._enabled[name] = enabled
        self.context.memory.set_plugin_state("core_plugins", "__global__", name, "true" if enabled else "false")
        if not self._started:
            return
        if enabled and name not in self._tasks:
            plugin = self.plugins[name]
            self._log_results(await plugin.on_start(self.context))
            self._tasks[name] = asyncio.create_task(plugin.background_loop(self.context, self._stop_event))
            logging.info("[plugin:%s] enabled at runtime", name)
        elif not enabled and name in self._tasks:
            self._tasks[name].cancel()
            await asyncio.gather(self._tasks[name], return_exceptions=True)
            del self._tasks[name]
            logging.info("[plugin:%s] disabled at runtime", name)

    async def on_message_received(self, message: WeChatInboundMessage) -> None:
        for name, plugin in self.plugins.items():
            if not self.is_enabled(name):
                continue
            self._log_results(await plugin.on_message_received(self.context, message))

    async def after_ai_reply(self, message: WeChatInboundMessage, reply: str) -> None:
        for name, plugin in self.plugins.items():
            if not self.is_enabled(name):
                continue
            self._log_results(await plugin.after_ai_reply(self.context, message, reply))

    async def after_memory_maintenance(
        self,
        wx_user_id: str,
        *,
        extracted_count: int,
        compressed: bool,
    ) -> None:
        for name, plugin in self.plugins.items():
            if not self.is_enabled(name):
                continue
            self._log_results(
                await plugin.after_memory_maintenance(
                    self.context,
                    wx_user_id,
                    extracted_count,
                    compressed,
                )
            )

    @staticmethod
    def _log_results(results: list[PluginResult]) -> None:
        for result in results:
            if result.detail:
                logging.info("[plugin:%s] %s - %s", result.plugin, result.action, result.detail)
            else:
                logging.info("[plugin:%s] %s", result.plugin, result.action)
