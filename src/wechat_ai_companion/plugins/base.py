from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..llm import ModelRouter
from ..memory import MemoryStore
from ..wechat_openclaw import OpenClawWeChatClient, WeChatInboundMessage


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
