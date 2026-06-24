# 插件开发规范

插件分为两类：

- 内置插件：随主项目发布，位于 `src/wechat_ai_companion/plugins/`。
- 第三方插件：独立 Python 包，通过 entry point 安装发现，不需要修改主项目源码。

插件默认按可信代码运行。当前没有进程级沙箱或权限隔离；安装第三方插件前必须审查来源和代码。

## 最小插件

```python
from wechat_ai_companion.plugins import CompanionPlugin, PluginContext
from wechat_ai_companion.wechat_openclaw import WeChatInboundMessage


class MyPlugin(CompanionPlugin):
    name = "my_plugin"
    description = "What this plugin does."
    version = "0.1.0"
    author = "Your name"
    default_config = {"reply_text": "hello"}
    config_schema = {
        "type": "object",
        "properties": {
            "reply_text": {
                "type": "string",
                "label": "回复文本",
                "description": "Web 后台会根据此配置生成输入框。",
                "default": "hello",
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
        if message.text.strip() == "/my-plugin":
            return str(self.config["reply_text"])
        return None
```

完整可安装示例见 `examples/hello_plugin/`。

## 独立包注册

第三方包的 `pyproject.toml`：

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "my-the-one-plugin"
version = "0.1.0"
requires-python = ">=3.10"

[project.entry-points."wechat_ai_companion.plugins"]
my_plugin = "my_plugin:MyPlugin"

[tool.setuptools]
py-modules = ["my_plugin"]
```

开发安装：

```powershell
python -m pip install -e .\path\to\my-the-one-plugin
```

重启主程序后，插件会出现在 Web 后台。新发现的第三方插件默认关闭，需要在后台手动启用，或在 `config.yaml` 中配置：

```yaml
plugins:
  enabled:
    my_plugin: true
```

插件名必须匹配 `[a-z][a-z0-9_]{1,63}`，并且不能与已加载插件重名。入口必须导出 `CompanionPlugin` 子类。加载失败、名称非法或重名时，主程序会记录错误并跳过，不会阻止其他插件启动。

## Web 配置 Schema

`config_schema` 使用 JSON Schema 风格的受限字段：

- `type`：`string`、`boolean`、`integer`、`number`、`array`、`object`。
- `label`、`description`：后台显示文本。
- `default`：默认值。
- `enum`：渲染下拉选项。
- `minimum`、`maximum`：数值范围。
- `minLength`、`maxLength`、`pattern`：字符串校验。
- `minItems`、`maxItems`、`items.type`：数组校验。
- `secret: true`：密码输入框；现有值不会通过 API 明文回显，留空会保留原值。

配置保存在 SQLite 的 `core_plugin_config` 命名空间中，优先级高于 `config.yaml`。保存后插件的后台任务会重启并立即使用新配置。未知字段和无效值会返回明确错误，不会静默覆盖为默认值。

## Hook

- `on_start(context)`：插件启用或配置重载时调用。
- `on_stop(context)`：插件关闭、配置重载或主程序停止时调用，用于释放资源。
- `on_message_received(context, message)`：收到微信用户消息后调用。
- `handle_command(context, message)`：返回字符串时，主 Bot 发送该内容并跳过普通 AI 回复。
- `maybe_defer_reply(context, message)`：返回 `True` 时暂缓普通回复，适合心流插件。
- `after_ai_reply(context, message, reply)`：AI 已发送至少一段回复后调用。
- `after_memory_maintenance(context, wx_user_id, extracted_count, compressed)`：长期记忆提取和摘要压缩后调用。
- `on_event(context, event)`：订阅统一事件总线。
- `background_loop(context, stop_event)`：插件后台任务；必须响应取消和 `stop_event`。

前台 Hook 默认超时为 30 秒，并由管理器隔离异常。插件可通过 `hook_timeout_seconds` 调整。只有重写 `background_loop` 的插件才会创建后台任务；纯命令或事件插件会正常显示为“事件响应”类型。后台任务异常会被记录，Web 后台会显示任务已停止，但主微信轮询不会退出。

## 事件总线

从 `PluginEvents` 引用事件名，不要在插件中重复硬编码：

- `MESSAGE_RECEIVED`
- `COMMAND_HANDLED`
- `REPLY_STARTED`
- `REPLY_DEFERRED`
- `REPLY_SEGMENT_SENT`
- `REPLY_INTERRUPTED`
- `REPLY_SENT`
- `MEMORY_MAINTENANCE`
- `PLUGIN_ENABLED_CHANGED`
- `PLUGIN_CONFIG_CHANGED`

事件对象：

```python
from wechat_ai_companion.plugins import PluginEvent, PluginEvents

async def on_event(self, context, event: PluginEvent):
    if event.event_type == PluginEvents.REPLY_INTERRUPTED:
        user_id = event.payload["message"].from_user_id
```

事件按插件加载顺序串行派发。事件处理器应保持轻量；耗时工作应转入插件自己的队列或后台任务。
同一业务不要同时写在专用 Hook 和对应事件中，例如不要同时使用 `on_message_received` 与 `MESSAGE_RECEIVED` 处理同一条消息，否则会执行两次。

## PluginContext

- `settings`：全局配置，只读使用。
- `wechat`：微信客户端，可发送消息。
- `memory`：SQLite 记忆和插件状态存储。
- `llm`：当前模型路由器。
- `deferred_reply_handler`：主 Bot 提供的延迟回复回调。

插件私有状态应使用自身名称作为命名空间：

```python
context.memory.set_plugin_state(self.name, user_id, "key", "value")
value = context.memory.get_plugin_state(self.name, user_id, "key")
```

## 内置插件

- `flow_state`：合并用户连续短消息，并判断何时开始回复。
- `proactive_response`：根据习惯、记忆、安静时间和限额决定是否主动发消息。
- `weather_monitor`：每日读取 Open-Meteo 天气并写入热上下文。
- `task_reminder`：任务、提醒和课表，到点提醒不占主动响应限额。

所有内置插件配置都可以在 Web 后台的“插件”Tab 修改。

## 开发检查

提交第三方插件前至少检查：

1. 禁用插件后不再处理消息或运行后台任务。
2. 配置保存、重启和进程重启后仍然有效。
3. Hook 抛异常时不会影响其他插件和主轮询。
4. 后台任务能正确处理 `asyncio.CancelledError`。
5. 不在日志、事件或 Web 状态中输出 API Key、token 等秘密。
6. 网络请求设置明确超时，并避免阻塞事件循环。
