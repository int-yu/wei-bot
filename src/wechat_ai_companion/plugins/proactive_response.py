from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from datetime import time as time_type
from typing import Any

from ..models import utc_now_iso
from ..wechat_openclaw import WeChatInboundMessage
from .base import CompanionPlugin, PluginContext, PluginResult


class ProactiveResponsePlugin(CompanionPlugin):
    name = "proactive_response"
    description = "Decides whether the AI should send proactive WeChat messages based on user habits and memory."
    default_config = {
        "check_interval_seconds": 300,
        "min_inactive_minutes": 30,
        "cooldown_minutes": 180,
        "max_messages_per_day": 3,
        "allow_context_token_reuse": True,
        "quiet_hours_start": "23:30",
        "quiet_hours_end": "08:00",
        "opt_out_patterns": ["/mute", "别主动", "不要主动", "不要再主动", "别再主动"],
    }

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._last_contacts: dict[str, tuple[str, str]] = {}

    async def on_start(self, context: PluginContext) -> list[PluginResult]:
        return [
            PluginResult(
                self.name,
                "started",
                "主动响应插件已启动；重启后需要用户先发一条消息，才有可用的最近会话 token。",
            )
        ]

    async def on_message_received(
        self,
        context: PluginContext,
        message: WeChatInboundMessage,
    ) -> list[PluginResult]:
        self._last_contacts[message.from_user_id] = (message.from_user_id, message.context_token)
        text = message.text.strip().lower()
        if any(str(pattern).lower() in text for pattern in self.config.get("opt_out_patterns", [])):
            self.set_muted(context, message.from_user_id, True)
            return [
                PluginResult(self.name, "tracked_last_contact", message.from_user_id),
                PluginResult(self.name, "muted_by_user_message", message.text.strip()),
            ]
        return [PluginResult(self.name, "tracked_last_contact", message.from_user_id)]

    async def after_ai_reply(
        self,
        context: PluginContext,
        message: WeChatInboundMessage,
        reply: str,
    ) -> list[PluginResult]:
        context.memory.set_plugin_state(self.name, message.from_user_id, "last_normal_reply_at", utc_now_iso())
        return []

    async def background_loop(self, context: PluginContext, stop_event: asyncio.Event) -> None:
        interval = int(self.config["check_interval_seconds"])
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                await self._check_all_users(context)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("[plugin:%s] background loop failed", self.name)

    async def _check_all_users(self, context: PluginContext) -> None:
        for wx_user_id in context.memory.list_known_users():
            try:
                await self._maybe_send(context, wx_user_id)
            except Exception:
                logging.exception("[plugin:%s] proactive check failed for %s", self.name, wx_user_id)

    async def _maybe_send(self, context: PluginContext, wx_user_id: str) -> None:
        if self.is_muted(context, wx_user_id):
            logging.info("[plugin:%s] skip %s: proactive muted", self.name, wx_user_id)
            return
        if self._in_quiet_hours():
            logging.info("[plugin:%s] skip %s: quiet hours", self.name, wx_user_id)
            return
        if wx_user_id not in self._last_contacts:
            logging.info("[plugin:%s] skip %s: no runtime context_token yet", self.name, wx_user_id)
            return
        if not self._inactive_enough(context, wx_user_id):
            return
        if not self._cooldown_ok(context, wx_user_id):
            return
        if not self._daily_quota_ok(context, wx_user_id):
            return

        decision = await self._ask_llm_for_decision(context, wx_user_id)
        if not decision.get("should_send"):
            logging.info(
                "[plugin:%s] decision=no user=%s reason=%s",
                self.name,
                wx_user_id,
                decision.get("reason", ""),
            )
            return

        message = str(decision.get("message", "")).strip()
        if not message:
            logging.info("[plugin:%s] decision=yes but empty message for %s", self.name, wx_user_id)
            return
        _, context_token = self._last_contacts[wx_user_id]
        if not bool(self.config["allow_context_token_reuse"]):
            logging.info("[plugin:%s] prepared proactive message but sending is disabled by config", self.name)
            return

        await context.wechat.send_text(wx_user_id, context_token, message)
        context.memory.add_message(wx_user_id, "assistant", f"[proactive] {message}")
        now = utc_now_iso()
        next_daily_count = self._today_count(context, wx_user_id) + 1
        context.memory.set_plugin_state(self.name, wx_user_id, "last_sent_at", now)
        context.memory.set_plugin_state(self.name, wx_user_id, "daily_count_date", now[:10])
        context.memory.set_plugin_state(
            self.name,
            wx_user_id,
            "daily_count",
            str(next_daily_count),
        )
        logging.info("[plugin:%s] sent proactive message to %s: %s", self.name, wx_user_id, message)

    def _inactive_enough(self, context: PluginContext, wx_user_id: str) -> bool:
        latest = context.memory.latest_message_at(wx_user_id, "user")
        if not latest:
            return False
        minutes = _minutes_since(latest)
        return minutes >= float(self.config["min_inactive_minutes"])

    def _cooldown_ok(self, context: PluginContext, wx_user_id: str) -> bool:
        latest = context.memory.get_plugin_state(self.name, wx_user_id, "last_sent_at")
        if not latest:
            return True
        return _minutes_since(latest) >= float(self.config["cooldown_minutes"])

    def _daily_quota_ok(self, context: PluginContext, wx_user_id: str) -> bool:
        return self._today_count(context, wx_user_id) < int(self.config["max_messages_per_day"])

    def _today_count(self, context: PluginContext, wx_user_id: str) -> int:
        today = utc_now_iso()[:10]
        state_date = context.memory.get_plugin_state(self.name, wx_user_id, "daily_count_date")
        if state_date != today:
            return 0
        raw = context.memory.get_plugin_state(self.name, wx_user_id, "daily_count", "0")
        try:
            return int(raw)
        except ValueError:
            return 0

    def is_muted(self, context: PluginContext, wx_user_id: str) -> bool:
        return context.memory.get_plugin_state(self.name, wx_user_id, "muted", "false").lower() == "true"

    def set_muted(self, context: PluginContext, wx_user_id: str, muted: bool) -> None:
        context.memory.set_plugin_state(self.name, wx_user_id, "muted", "true" if muted else "false")

    def _in_quiet_hours(self) -> bool:
        start = _parse_hhmm(str(self.config.get("quiet_hours_start", "23:30")))
        end = _parse_hhmm(str(self.config.get("quiet_hours_end", "08:00")))
        if not start or not end:
            return False
        now = datetime.now().time()
        if start <= end:
            return start <= now < end
        return now >= start or now < end

    async def _ask_llm_for_decision(self, context: PluginContext, wx_user_id: str) -> dict[str, Any]:
        agent = context.memory.get_or_create_agent(
            wx_user_id,
            context.settings.bot.default_ai_name,
            context.settings.bot.default_persona,
        )
        recent = context.memory.recent_messages(wx_user_id, limit_messages=12)
        structured = context.memory.list_structured(wx_user_id)
        summary = context.memory.latest_summary(wx_user_id) or "暂无中期摘要"
        transcript = "\n".join(f"{m.created_at} {m.role}: {m.content}" for m in recent)
        memories = "\n".join(f"- {m.kind}.{m.key}: {m.value}" for m in structured) or "- 暂无长期结构化记忆"

        prompt = (
            "你是微信 AI 联系人的主动响应决策器。根据用户长期偏好、最近对话和不活跃时间，判断现在是否应该主动发一条微信消息。"
            "必须克制，不要打扰用户；只有用户明确喜欢提醒、关心、陪伴、复盘、计划推进，或者最近事件强相关时才建议发送。"
            "只返回 JSON："
            '{"should_send":true|false,"reason":"简短原因","message":"要发送的消息；不发送则为空"}'
        )
        user_payload = (
            f"AI 名称：{agent.ai_name}\n"
            f"AI 人设：{agent.persona}\n"
            f"距离用户最后发消息约 {_minutes_since(context.memory.latest_message_at(wx_user_id, 'user') or utc_now_iso()):.0f} 分钟。\n\n"
            f"中期摘要：\n{summary}\n\n长期结构化记忆：\n{memories}\n\n最近对话：\n{transcript}"
        )
        response = await context.llm.chat(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_payload},
            ],
            max_tokens=400,
        )
        return _parse_json_object(response.content)


def _minutes_since(iso_time: str) -> float:
    try:
        dt = datetime.fromisoformat(iso_time)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 60)


def _parse_hhmm(value: str) -> time_type | None:
    try:
        hour, minute = value.split(":", 1)
        return time_type(hour=int(hour), minute=int(minute))
    except Exception:
        return None


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
            return {"should_send": False, "reason": "invalid_json", "message": ""}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {"should_send": False, "reason": "invalid_json", "message": ""}
    if not isinstance(data, dict):
        return {"should_send": False, "reason": "not_object", "message": ""}
    return data
