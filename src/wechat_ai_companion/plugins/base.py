from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import Settings
from ..llm import ModelRouter
from ..memory import MemoryStore
from ..wechat_openclaw import OpenClawWeChatClient, WeChatInboundMessage


DeferredReplyHandler = Callable[[WeChatInboundMessage, str], Awaitable[None]]


class PluginEvents:
    MESSAGE_RECEIVED = "message.received"
    COMMAND_HANDLED = "command.handled"
    REPLY_STARTED = "reply.started"
    REPLY_DEFERRED = "reply.deferred"
    REPLY_SEGMENT_SENT = "reply.segment_sent"
    REPLY_INTERRUPTED = "reply.interrupted"
    REPLY_SENT = "reply.sent"
    MEMORY_MAINTENANCE = "memory.maintenance"
    PLUGIN_ENABLED_CHANGED = "plugin.enabled_changed"
    PLUGIN_CONFIG_CHANGED = "plugin.config_changed"


@dataclass(slots=True)
class PluginResult:
    plugin: str
    action: str
    detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PluginEvent:
    event_type: str
    source: str = "core"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PluginContext:
    settings: Settings
    wechat: OpenClawWeChatClient
    memory: MemoryStore
    llm: ModelRouter
    deferred_reply_handler: DeferredReplyHandler | None = None


class CompanionPlugin:
    name = "base"
    description = "Base plugin"
    version = "0.1.0"
    author = ""
    homepage = ""
    hook_timeout_seconds = 30.0
    default_config: dict[str, Any] = {}
    config_schema: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        merged = dict(self.default_config)
        if config:
            merged.update(config)
        self.config = merged

    def update_config(self, config: dict[str, Any]) -> None:
        self.config.update(config)

    def public_config_schema(self) -> dict[str, Any]:
        return self.config_schema

    async def on_start(self, context: PluginContext) -> list[PluginResult]:
        return []

    async def on_stop(self, context: PluginContext) -> list[PluginResult]:
        return []

    async def on_message_received(
        self,
        context: PluginContext,
        message: WeChatInboundMessage,
    ) -> list[PluginResult]:
        return []

    async def on_event(
        self,
        context: PluginContext,
        event: PluginEvent,
    ) -> list[PluginResult]:
        return []

    async def handle_command(
        self,
        context: PluginContext,
        message: WeChatInboundMessage,
    ) -> str | None:
        return None

    async def maybe_defer_reply(
        self,
        context: PluginContext,
        message: WeChatInboundMessage,
    ) -> bool:
        return False

    async def after_ai_reply(
        self,
        context: PluginContext,
        message: WeChatInboundMessage,
        reply: str,
    ) -> list[PluginResult]:
        return []

    async def after_memory_maintenance(
        self,
        context: PluginContext,
        wx_user_id: str,
        extracted_count: int,
        compressed: bool,
    ) -> list[PluginResult]:
        return []

    async def background_loop(self, context: PluginContext, stop_event: asyncio.Event) -> None:
        return None
