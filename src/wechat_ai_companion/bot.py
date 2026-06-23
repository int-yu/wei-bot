from __future__ import annotations

import asyncio
import json
import logging
import time

from .config import Settings
from .llm import ModelRouter
from .memory import MemoryStore
from .plugins import PluginManager
from .reply_format import DEFAULT_MAX_REPLY_SEGMENTS, build_reply_format_rules, split_reply_segments
from .wechat_openclaw import OpenClawWeChatClient, WeChatInboundMessage


HELP_TEXT = (
    "可用指令：\n"
    "/help - 查看指令\n"
    "/persona - 查看当前 AI 人设\n"
    "/persona 人设内容 - 更新当前微信号绑定的 AI 人设\n"
    "/memory - 查看中期摘要和长期结构化记忆\n"
    "/model - 查看当前模型\n"
    "/model list - 查看可切换模型\n"
    "/model switch 名称 - 切换模型提供商\n"
    "/mute - 关闭主动消息\n"
    "/unmute - 恢复主动消息\n"
    "/remind 时间 内容 - 添加提醒，例如 /remind 明天 09:00 交作业\n"
    "/todo 内容 - 添加无截止时间任务\n"
    "/tasks - 查看任务和提醒\n"
    "/schedule add 周一 08:00-09:40 课程名 - 添加课表\n"
    "/reset_hot - 清空当前热上下文窗口"
)


class CompanionBot:
    reply_segment_delay_seconds = 0.8

    def __init__(
        self,
        settings: Settings,
        wechat: OpenClawWeChatClient,
        memory: MemoryStore,
        llm: ModelRouter,
        plugin_manager: PluginManager | None = None,
    ) -> None:
        self.settings = settings
        self.wechat = wechat
        self.memory = memory
        self.llm = llm
        self.plugin_manager = plugin_manager
        self._welcomed_users: set[str] = set()
        self._interrupt_versions: dict[str, int] = {}
        if self.plugin_manager:
            self.plugin_manager.set_deferred_reply_handler(self.handle_deferred_reply)

    async def run_forever(self) -> None:
        restored = await self._restore_wechat_session()
        if not restored:
            login = await self.wechat.login()
            logging.info("[wechat] login succeeded base_url=%s bot_id=%s", login.base_url, login.bot_id)
            self._save_wechat_session()
        print("微信连接已建立。向这个微信联系人发送消息即可开始对话。")
        if self.plugin_manager:
            await self.plugin_manager.start()

        poll_failures = 0
        while True:
            try:
                for message in await self.wechat.get_updates():
                    await self.handle_message(message)
                self._save_wechat_session()
                poll_failures = 0
            except Exception:
                poll_failures += 1
                logging.exception("[wechat] polling loop failed; retrying in 3 seconds")
                if restored and poll_failures >= 3:
                    logging.info("[wechat] restored session failed repeatedly; falling back to QR login")
                    restored = False
                    login = await self.wechat.login()
                    logging.info("[wechat] login succeeded base_url=%s bot_id=%s", login.base_url, login.bot_id)
                    self._save_wechat_session()
                    poll_failures = 0
                await asyncio.sleep(3)

    async def _restore_wechat_session(self) -> bool:
        if not self.settings.wechat.restore_session:
            logging.info("[wechat] session restore disabled")
            return False
        raw = self.memory.get_plugin_state("core_wechat_session", "__global__", "session_json")
        if not raw:
            logging.info("[wechat] no saved session; QR login required")
            return False
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logging.info("[wechat] saved session is invalid JSON; QR login required")
            return False

        login_at = float(data.get("login_at") or data.get("saved_at") or 0)
        age = max(0, time.time() - login_at)
        if age >= self.settings.wechat.session_duration_seconds:
            logging.info("[wechat] saved session expired age_seconds=%.0f; QR login required", age)
            return False

        bot_token = str(data.get("bot_token") or "")
        base_url = str(data.get("base_url") or self.settings.wechat.base_url)
        get_updates_buf = str(data.get("get_updates_buf") or "")
        if not bot_token:
            logging.info("[wechat] saved session has no token; QR login required")
            return False

        self.wechat.attach_session(bot_token=bot_token, base_url=base_url, get_updates_buf=get_updates_buf)
        logging.info("[wechat] probing saved session age_seconds=%.0f base_url=%s", age, base_url)
        ok = await self.wechat.probe_session(self.settings.wechat.restore_probe_timeout_seconds)
        if ok:
            logging.info("[wechat] restored saved session")
            self._save_wechat_session()
            return True
        logging.info("[wechat] saved session rejected; QR login required")
        return False

    def _save_wechat_session(self) -> None:
        if not self.wechat.bot_token:
            return
        now = time.time()
        login_at = now
        raw = self.memory.get_plugin_state("core_wechat_session", "__global__", "session_json")
        if raw:
            try:
                old = json.loads(raw)
                if old.get("bot_token") == self.wechat.bot_token:
                    login_at = float(old.get("login_at") or old.get("saved_at") or now)
            except (json.JSONDecodeError, TypeError, ValueError):
                login_at = now
        session = self.wechat.export_session()
        session["login_at"] = login_at
        session["saved_at"] = now
        self.memory.set_plugin_state(
            "core_wechat_session",
            "__global__",
            "session_json",
            json.dumps(session, ensure_ascii=False),
        )

    async def handle_message(self, message: WeChatInboundMessage) -> None:
        user_id = message.from_user_id
        text = message.text.strip()
        self._mark_incoming(user_id)
        logging.info("[message:in] user=%s text=%s", user_id, text)
        if self.plugin_manager:
            await self.plugin_manager.on_message_received(message)

        agent = self.memory.get_or_create_agent(
            user_id,
            self.settings.bot.default_ai_name,
            self.settings.bot.default_persona,
        )

        if user_id not in self._welcomed_users:
            self._welcomed_users.add(user_id)
            logging.info("[command] first_contact user=%s send_help=true", user_id)
            await self.wechat.send_text(user_id, message.context_token, HELP_TEXT)
            logging.info("[message:out] user=%s text=%s", user_id, HELP_TEXT)
            return

        command_reply = await self._handle_command(message, agent.persona)
        if command_reply is not None:
            logging.info("[command] handled user=%s command=%s", user_id, text.split(maxsplit=1)[0])
            await self.wechat.send_text(user_id, message.context_token, command_reply)
            logging.info("[message:out] user=%s text=%s", user_id, command_reply)
            return

        if self.plugin_manager:
            plugin_reply = await self.plugin_manager.handle_command(message)
            if plugin_reply is not None:
                await self.wechat.send_text(user_id, message.context_token, plugin_reply)
                logging.info("[message:out] user=%s text=%s", user_id, plugin_reply)
                return

        if self.plugin_manager and await self.plugin_manager.maybe_defer_reply(message):
            return

        await self._reply_to_user(message, text, source="normal")

    async def handle_deferred_reply(self, message: WeChatInboundMessage, user_text: str) -> None:
        await self._reply_to_user(message, user_text, source="flow_state")

    async def _reply_to_user(self, message: WeChatInboundMessage, user_text: str, *, source: str) -> None:
        user_id = message.from_user_id
        agent = self.memory.get_or_create_agent(
            user_id,
            self.settings.bot.default_ai_name,
            self.settings.bot.default_persona,
        )
        user_message_id = self.memory.add_message(user_id, "user", user_text)
        self.memory.relation_delta(user_id, familiarity_delta=1)
        logging.info("[memory] hot_context appended user=%s message_id=%s", user_id, user_message_id)

        reply_version = self._interrupt_versions.get(user_id, 0)
        sent_segments: list[str] = []
        await self.wechat.set_typing(user_id, message.context_token, 1)
        try:
            prompt_messages = self.memory.build_prompt_messages(
                agent,
                f"{self.settings.bot.system_rules}\n\n{build_reply_format_rules(DEFAULT_MAX_REPLY_SEGMENTS)}",
                user_text,
            )
            logging.info("[ai] request user=%s source=%s prompt_messages=%s", user_id, source, len(prompt_messages))
            response = await self.llm.chat(prompt_messages)
            segments = split_reply_segments(response.content)
            if not segments:
                logging.info("[ai] empty reply after segmentation user=%s", user_id)
            for index, segment in enumerate(segments):
                if index > 0:
                    await asyncio.sleep(self.reply_segment_delay_seconds)
                if self._was_interrupted(user_id, reply_version):
                    logging.info(
                        "[message:out] interrupted user=%s sent=%s remaining=%s",
                        user_id,
                        len(sent_segments),
                        len(segments) - len(sent_segments),
                    )
                    break
                await self.wechat.send_text(user_id, message.context_token, segment)
                assistant_message_id = self.memory.add_message(user_id, "assistant", segment)
                sent_segments.append(segment)
                logging.info(
                    "[message:out] user=%s segment=%s/%s text=%s",
                    user_id,
                    index + 1,
                    len(segments),
                    segment,
                )
                logging.info("[memory] hot_context appended user=%s message_id=%s", user_id, assistant_message_id)
            if sent_segments and self.plugin_manager:
                await self.plugin_manager.after_ai_reply(message, "\n".join(sent_segments))
        finally:
            await self.wechat.set_typing(user_id, message.context_token, 2)

        extracted = await self.memory.extract_long_term_if_due(user_id, user_message_id, self.llm)
        if extracted:
            logging.info(
                "[memory] long_term_extracted user=%s count=%s items=%s",
                user_id,
                len(extracted),
                "; ".join(f"{item.kind}.{item.key}={item.value}" for item in extracted),
            )
        else:
            logging.info("[memory] long_term_extract skipped_or_empty user=%s", user_id)

        compression = await self.memory.compress_if_needed(user_id, self.llm)
        if compression:
            logging.info(
                "[memory] compressed user=%s messages=%s range=%s-%s summary=%s",
                user_id,
                compression["message_count"],
                compression["from_message_id"],
                compression["to_message_id"],
                compression["summary_preview"],
            )
        else:
            logging.info("[memory] compression not_needed user=%s", user_id)

        if self.plugin_manager:
            await self.plugin_manager.after_memory_maintenance(
                user_id,
                extracted_count=len(extracted),
                compressed=bool(compression),
            )

    def _mark_incoming(self, user_id: str) -> None:
        self._interrupt_versions[user_id] = self._interrupt_versions.get(user_id, 0) + 1

    def _was_interrupted(self, user_id: str, reply_version: int) -> bool:
        return self._interrupt_versions.get(user_id, 0) != reply_version

    async def _handle_command(self, message: WeChatInboundMessage, current_persona: str) -> str | None:
        text = message.text.strip()
        user_id = message.from_user_id
        if text in {"/help", "/指令"}:
            return HELP_TEXT
        if text == "/persona":
            return f"当前 AI 人设：\n{current_persona}"
        if text.startswith("/persona "):
            persona = text[len("/persona ") :].strip()
            if not persona:
                return "人设不能为空。用法：/persona 你希望 AI 成为什么样的人设"
            self.memory.update_persona(user_id, None, persona)
            return "已更新这个微信号绑定的 AI 人设。"
        if text == "/memory":
            summary = self.memory.latest_summary(user_id) or "暂无中期摘要。"
            structured = self.memory.list_structured(user_id)
            structured_text = "\n".join(f"- {m.kind}.{m.key}: {m.value}" for m in structured) or "暂无长期结构化记忆。"
            return f"中期摘要：\n{summary}\n\n长期结构化记忆：\n{structured_text}"
        if text == "/model":
            return f"当前模型：\n{self.llm.describe_current()}"
        if text == "/model list":
            lines = []
            for name in self.llm.list_provider_names():
                provider = self.llm.providers[name]
                marker = "*" if name == self.llm.active_provider else " "
                lines.append(f"{marker} {name}: {provider.model} @ {provider.base_url}")
            return "可用模型提供商：\n" + "\n".join(lines)
        if text.startswith("/model switch "):
            name = text[len("/model switch ") :].strip()
            if not name:
                return "用法：/model switch 提供商名称"
            try:
                provider = self.llm.switch_provider(name)
            except ValueError as exc:
                return str(exc)
            self.memory.set_plugin_state("core_model", "__global__", "active_provider", provider.name)
            logging.info("[model] switched provider=%s model=%s", provider.name, provider.model)
            return f"已切换模型：{provider.name}\nmodel={provider.model}\nbase_url={provider.base_url}"
        if text == "/mute":
            self.memory.set_plugin_state("proactive_response", user_id, "muted", "true")
            return "已关闭主动消息。你仍然可以正常给我发消息；需要恢复时发送 /unmute。"
        if text == "/unmute":
            self.memory.set_plugin_state("proactive_response", user_id, "muted", "false")
            return "已恢复主动消息。系统仍会遵守安静时间、每日上限和冷却时间。"
        if text == "/reset_hot":
            self.memory.conn.execute(
                "UPDATE messages SET compressed = 1 WHERE wx_user_id = ? AND compressed = 0",
                (user_id,),
            )
            self.memory.conn.commit()
            return "已清空热上下文窗口；长期记忆和中期摘要不受影响。"
        return None
