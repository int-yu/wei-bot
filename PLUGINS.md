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
    flow_state: true
    proactive_response: true
  config:
    flow_state:
      min_silence_seconds: 6
      max_wait_seconds: 45
      decision_model_enabled: true
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
    FlowStatePlugin.name: FlowStatePlugin,
    ProactiveResponsePlugin.name: ProactiveResponsePlugin,
    MyPlugin.name: MyPlugin,
}
```

## 可用 Hook

- `on_start(context)`：登录成功后调用。
- `on_message_received(context, message)`：收到微信用户消息后调用。
- `handle_command(context, message)`：可选命令处理；返回字符串时由主 Bot 直接发送该回复，并跳过普通 AI 回复。
- `maybe_defer_reply(context, message)`：可选心流控制；返回 `True` 时主 Bot 暂不回复，由插件后续调用延迟回复回调。
- `after_ai_reply(context, message, reply)`：AI 正常回复发送后调用。
- `after_memory_maintenance(context, wx_user_id, extracted_count, compressed)`：长期记忆提取和中期摘要压缩后调用。
- `background_loop(context, stop_event)`：后台循环任务，适合提醒、主动响应、定时同步。

`PluginContext` 提供：

- `settings`：全局配置。
- `wechat`：微信发送/轮询客户端。
- `memory`：SQLite 记忆存储。
- `llm`：DeepSeek 客户端。
- `deferred_reply_handler`：主 Bot 提供的延迟回复回调，心流类插件可以在后台判断完成后调用。

## 心流插件

内置插件：`flow_state`

工作方式：

1. 普通聊天消息先进入心流缓存，不立刻触发 AI 回复。
2. 用户停顿超过 `min_silence_seconds` 后，插件会请模型判断“用户是否暂时讲完了”。
3. 如果模型认为还没讲完，会继续等待；超过 `max_wait_seconds` 会强制回复，避免一直卡住。
4. 命令、插件命令不会进入心流缓存。
5. 主 Bot 生成回复后会拆成短消息发送；如果发送过程中收到用户新消息，剩余片段会停止发送。

配置示例：

```yaml
plugins:
  enabled:
    flow_state: true
  config:
    flow_state:
      check_interval_seconds: 1
      min_silence_seconds: 6
      max_wait_seconds: 45
      max_buffer_messages: 8
      decision_model_enabled: true
      decision_max_tokens: 180
```

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

## 天气监测插件

内置插件：`weather_monitor`

工作方式：
1. 每天到达 `run_at` 后请求一次天气接口。
2. 将天气摘要写入每个已知用户的热上下文，角色为 `system`，因此 AI 当天回复时可以自然参考。
3. 如果当天先获取了天气，后来才有新用户发消息，插件会把缓存的当天天气补写给该用户。

默认使用 Open-Meteo 经纬度接口，不需要 API Key。城市需要通过经纬度配置：

```yaml
plugins:
  enabled:
    weather_monitor: true
  config:
    weather_monitor:
      run_at: "07:30"
      timezone: Asia/Shanghai
      location_name: 北京
      latitude: 39.9042
      longitude: 116.4074
```

## 任务和提醒插件

内置插件：`task_reminder`

支持命令：

- `/remind 明天 09:00 交作业`：添加一次性提醒。
- `/todo 买牛奶`：添加无截止时间任务。
- `/task 明天 18:00 完成实验报告`：添加带截止时间的任务。
- `/tasks`：查看未完成任务和提醒。
- `/schedule add 周一 08:00-09:40 高数`：添加每周课表。
- `/schedule`：查看课表。
- `/done ID`：标记完成。
- `/cancel ID`：取消任务、提醒或课表。

也支持明确触发词，例如“提醒我明天下午3点交作业”“帮我记住周一 08:00-09:40 高数课表”。普通闲聊不会被强行猜成任务。

提醒消息由 `task_reminder` 自己发送，不会计入 `proactive_response` 的每日主动消息上限。它仍然受微信 iLink `context_token` 限制：用户至少需要先发过一条消息，插件才有可复用的发送 token。
