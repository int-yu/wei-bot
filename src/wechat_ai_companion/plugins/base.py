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


@dataclass(slots=True)
class PluginResult:
    plugin: str
    action: str
    detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


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
    default_config: dict[str, Any] = {}

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        merged = dict(self.default_config)
        if config:
            merged.update(config)
        self.config = merged

    async def on_start(self, context: PluginContext) -> list[PluginResult]:
        return []

    async def on_message_received(
        self,
        context: PluginContext,
        message: WeChatInboundMessage,
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
