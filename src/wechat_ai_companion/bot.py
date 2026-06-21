from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .llm import ModelRouter
from .memory import MemoryStore
from .plugins import PluginManager
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
    "/reset_hot - 清空当前热上下文窗口"
)


class CompanionBot:
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

    async def run_forever(self) -> None:
        login = await self.wechat.login()
        logging.info("[wechat] login succeeded base_url=%s bot_id=%s", login.base_url, login.bot_id)
        print("登录成功。向这个微信联系人发送消息即可开始对话。")
        if self.plugin_manager:
            await self.plugin_manager.start()

        while True:
            try:
                for message in await self.wechat.get_updates():
                    await self.handle_message(message)
            except Exception:
                logging.exception("[wechat] polling loop failed; retrying in 3 seconds")
                await asyncio.sleep(3)

    async def handle_message(self, message: WeChatInboundMessage) -> None:
        user_id = message.from_user_id
        text = message.text.strip()
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

        user_message_id = self.memory.add_message(user_id, "user", text)
        self.memory.relation_delta(user_id, familiarity_delta=1)
        logging.info("[memory] hot_context appended user=%s message_id=%s", user_id, user_message_id)

        await self.wechat.set_typing(user_id, message.context_token, 1)
        try:
            prompt_messages = self.memory.build_prompt_messages(
                agent,
                self.settings.bot.system_rules,
                text,
            )
            logging.info("[ai] request user=%s prompt_messages=%s", user_id, len(prompt_messages))
            response = await self.llm.chat(prompt_messages)
            reply = response.content.strip()
            assistant_message_id = self.memory.add_message(user_id, "assistant", reply)
            logging.info("[message:out] user=%s text=%s", user_id, reply)
            logging.info("[memory] hot_context appended user=%s message_id=%s", user_id, assistant_message_id)
            await self.wechat.send_text(user_id, message.context_token, reply)
            if self.plugin_manager:
                await self.plugin_manager.after_ai_reply(message, reply)
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
