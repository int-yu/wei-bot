from __future__ import annotations

from wechat_ai_companion.plugins import (
    CompanionPlugin,
    PluginContext,
    PluginEvent,
    PluginEvents,
    PluginResult,
)
from wechat_ai_companion.wechat_openclaw import WeChatInboundMessage


class HelloPlugin(CompanionPlugin):
    name = "hello_plugin"
    description = "Minimal installable third-party plugin example."
    version = "0.1.0"
    author = "The One contributors"
    default_config = {
        "reply_text": "Hello from an external plugin.",
    }
    config_schema = {
        "type": "object",
        "properties": {
            "reply_text": {
                "type": "string",
                "label": "Reply text",
                "description": "Text returned for the /hello-plugin command.",
                "default": "Hello from an external plugin.",
                "minLength": 1,
                "maxLength": 500,
            }
        },
    }

    async def handle_command(
        self,
        context: PluginContext,
        message: WeChatInboundMessage,
    ) -> str | None:
        if message.text.strip() == "/hello-plugin":
            return str(self.config["reply_text"])
        return None

    async def on_event(
        self,
        context: PluginContext,
        event: PluginEvent,
    ) -> list[PluginResult]:
        if event.event_type == PluginEvents.REPLY_INTERRUPTED:
            return [PluginResult(self.name, "observed_interruption")]
        return []
