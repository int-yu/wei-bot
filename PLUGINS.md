# 插件规范

插件目录：

```text
src/wechat_ai_companion/plugins/
```

## 配置开关

在 `config.yaml` 中控制插件启停：

```yaml
plugins:
  enabled:
    proactive_response: true
  config:
    proactive_response:
      check_interval_seconds: 300
      min_inactive_minutes: 30
      cooldown_minutes: 180
      max_messages_per_day: 3
      allow_context_token_reuse: true
```

如果旧版 `config.yaml` 没有 `plugins` 字段，项目会默认启用 `proactive_response`。要关闭它，显式写：

```yaml
plugins:
  enabled:
    proactive_response: false
```

## 插件格式

新插件继承 `CompanionPlugin`：

```python
from __future__ import annotations

from .base import CompanionPlugin, PluginContext, PluginResult
from ..wechat_openclaw import WeChatInboundMessage


class MyPlugin(CompanionPlugin):
    name = "my_plugin"
    description = "What this plugin does."
    default_config = {
        "enabled_feature": True,
    }

    async def on_message_received(
        self,
        context: PluginContext,
        message: WeChatInboundMessage,
    ) -> list[PluginResult]:
        return [PluginResult(self.name, "observed_message", message.from_user_id)]
```

然后在 `plugins/manager.py` 的 `BUILTIN_PLUGINS` 注册：

```python
BUILTIN_PLUGINS = {
    ProactiveResponsePlugin.name: ProactiveResponsePlugin,
    MyPlugin.name: MyPlugin,
}
```

## 可用 Hook

- `on_start(context)`：登录成功后调用。
- `on_message_received(context, message)`：收到微信用户消息后调用。
- `after_ai_reply(context, message, reply)`：AI 正常回复发送后调用。
- `after_memory_maintenance(context, wx_user_id, extracted_count, compressed)`：长期记忆提取和中期摘要压缩后调用。
- `background_loop(context, stop_event)`：后台循环任务，适合提醒、主动响应、定时同步。

`PluginContext` 提供：

- `settings`：全局配置。
- `wechat`：微信发送/轮询客户端。
- `memory`：SQLite 记忆存储。
- `llm`：DeepSeek 客户端。

## 主动响应插件

内置插件：`proactive_response`

工作方式：

1. 用户至少发过一条消息后，插件记录最近的 `context_token`。
2. 后台定时检查已知用户。
3. 结合长期结构化记忆、中期摘要、最近对话和不活跃时间，请 AI 判断是否应该主动发消息。
4. 满足冷却时间和每日上限后发送。

注意：微信 iLink 协议要求发送消息携带 `context_token`。主动响应只能复用最近一次用户消息的 token；如果服务端不接受旧 token，发送可能失败。要只生成决策、不主动发送，可以设置：

```yaml
plugins:
  config:
    proactive_response:
      allow_context_token_reuse: false
```

