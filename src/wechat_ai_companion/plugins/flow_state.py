from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..wechat_openclaw import WeChatInboundMessage
from .base import CompanionPlugin, PluginContext, PluginResult


@dataclass(slots=True)
class FlowBuffer:
    from_user_id: str
    context_token: str
    raw: dict[str, Any]
    texts: list[str] = field(default_factory=list)
    first_seen: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    generation: int = 0
    deciding: bool = False


class FlowStatePlugin(CompanionPlugin):
    name = "flow_state"
    description = "Waits for multi-part user messages and replies after the user appears to have paused."
    default_config = {
        "check_interval_seconds": 1,
        "min_silence_seconds": 6,
        "max_wait_seconds": 45,
        "max_buffer_messages": 8,
        "decision_model_enabled": True,
        "decision_max_tokens": 180,
    }

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._buffers: dict[str, FlowBuffer] = {}

    async def on_start(self, context: PluginContext) -> list[PluginResult]:
        return [
            PluginResult(
                self.name,
                "started",
                f"silence={self.config.get('min_silence_seconds')}s max_wait={self.config.get('max_wait_seconds')}s",
            )
        ]

    async def maybe_defer_reply(
        self,
        context: PluginContext,
        message: WeChatInboundMessage,
    ) -> bool:
        text = message.text.strip()
        if not text or text.startswith("/"):
            return False

        now = time.monotonic()
        buffer = self._buffers.get(message.from_user_id)
        if buffer is None:
            buffer = FlowBuffer(
                from_user_id=message.from_user_id,
                context_token=message.context_token,
                raw=message.raw,
                first_seen=now,
                last_seen=now,
            )
            self._buffers[message.from_user_id] = buffer

        buffer.texts.append(text)
        max_messages = max(1, int(self.config.get("max_buffer_messages", 8)))
        if len(buffer.texts) > max_messages:
            buffer.texts = buffer.texts[-max_messages:]
        buffer.context_token = message.context_token
        buffer.raw = message.raw
        buffer.last_seen = now
        buffer.generation += 1
        return True

    async def background_loop(self, context: PluginContext, stop_event: asyncio.Event) -> None:
        interval = max(0.2, float(self.config.get("check_interval_seconds", 1)))
        while not stop_event.is_set():
            try:
                ready = await self.pop_ready_batches(context)
                for message, combined_text in ready:
                    if context.deferred_reply_handler is None:
                        logging.warning("[plugin:%s] no deferred reply handler configured", self.name)
                        continue
                    await context.deferred_reply_handler(message, combined_text)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("[plugin:%s] background loop failed", self.name)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def pop_ready_batches(self, context: PluginContext) -> list[tuple[WeChatInboundMessage, str]]:
        ready: list[tuple[WeChatInboundMessage, str]] = []
        for user_id in list(self._buffers):
            item = await self._maybe_pop_ready(context, user_id)
            if item is not None:
                ready.append(item)
        return ready

    async def _maybe_pop_ready(
        self,
        context: PluginContext,
        user_id: str,
    ) -> tuple[WeChatInboundMessage, str] | None:
        buffer = self._buffers.get(user_id)
        if buffer is None or buffer.deciding:
            return None

        now = time.monotonic()
        silence = now - buffer.last_seen
        min_silence = float(self.config.get("min_silence_seconds", 6))
        max_wait = float(self.config.get("max_wait_seconds", 45))
        if silence < min_silence:
            return None

        generation = buffer.generation
        texts = list(buffer.texts)
        force_reply = now - buffer.first_seen >= max_wait
        buffer.deciding = True
        try:
            should_reply = force_reply or await self._should_reply_now(context, texts)
        finally:
            latest = self._buffers.get(user_id)
            if latest is not None:
                latest.deciding = False

        latest = self._buffers.get(user_id)
        if latest is None or latest.generation != generation:
            return None
        if not should_reply:
            latest.last_seen = time.monotonic()
            return None

        popped = self._buffers.pop(user_id)
        combined_text = "\n".join(popped.texts).strip()
        message = WeChatInboundMessage(
            from_user_id=popped.from_user_id,
            context_token=popped.context_token,
            text=combined_text,
            raw={"flow_state": True, "messages": popped.texts, "last_raw": popped.raw},
        )
        logging.info(
            "[plugin:%s] flow_ready user=%s messages=%s force=%s",
            self.name,
            user_id,
            len(popped.texts),
            force_reply,
        )
        return message, combined_text

    async def _should_reply_now(self, context: PluginContext, texts: list[str]) -> bool:
        if not bool(self.config.get("decision_model_enabled", True)):
            return True
        prompt = (
            "你是微信对话的心流判断器。用户可能连续发送多条短消息。"
            "判断用户是否已经暂时讲完，AI 现在是否应该回复。"
            "如果最后一条像未完成的铺垫、还有明显下文、只是在输入过程中的半句话，返回 false。"
            "如果用户已经提出问题、表达完整情绪、给出完整事项，或已经停顿到足够自然，返回 true。"
            '只返回 JSON：{"should_reply":true|false,"reason":"简短原因"}'
        )
        payload = "\n".join(f"{index + 1}. {text}" for index, text in enumerate(texts))
        try:
            response = await context.llm.chat(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": payload},
                ],
                max_tokens=int(self.config.get("decision_max_tokens", 180)),
            )
        except Exception:
            logging.exception("[plugin:%s] flow decision failed; fallback to reply", self.name)
            return True
        decision = _parse_json_object(response.content)
        return bool(decision.get("should_reply", True))


def _parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").strip()
        text = text.removesuffix("```").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            return {"should_reply": True, "reason": "invalid_json"}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {"should_reply": True, "reason": "invalid_json"}
    return data if isinstance(data, dict) else {"should_reply": True, "reason": "not_object"}
