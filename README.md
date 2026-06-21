# The One WeChat AI Companion

基于微信 OpenClaw / Claw iLink 协议层的个人 AI 联系人项目。运行后通过二维码连接微信，AI 在微信里表现为一个联系人。当前以文本私聊为主。

## 核心能力

- 扫码连接微信 ClawBot / OpenClaw iLink 服务。
- 一个微信号绑定一个 AI，人设、对话和记忆互相隔离。
- 支持三层记忆：热上下文、中期摘要、长期结构化记忆。
- 支持多模型配置和运行时切换，兼容 OpenAI Chat Completions 格式。
- 支持插件机制，内置主动响应插件。
- 提供本地 Web 管理后台。
- 支持重启后优先恢复旧微信连接，失败或过期再扫码。

## 启动

```powershell
cd D:\codex\the_one
python -m pip install -e .
Copy-Item .env.example .env
Copy-Item config.example.yaml config.yaml
python -m wechat_ai_companion.main
```

编辑 `.env`，填入实际 API Key。不要提交 `.env`、`config.yaml` 或 `data/`。

## 微信连接恢复

程序默认会把微信 iLink session 保存到本地 SQLite。重启时流程为：

```text
启动 -> 读取本地 session -> 探测 getupdates
  -> 成功：直接进入监听
  -> 失败或过期：显示二维码重新扫码
```

默认按 24 小时有效期处理。恢复出来的连接如果连续轮询失败 3 次，会自动放弃旧连接并重新扫码。

配置项：

```yaml
wechat:
  restore_session: true
  session_duration_seconds: 86400
  restore_probe_timeout_seconds: 8
```

`bot_token` 保存在本地 `data/companion.db`，该目录已被 `.gitignore` 排除。

## Web 管理后台

启动后打开：

```text
http://127.0.0.1:8765
```

后台需要账号密码登录。默认账号为 `admin`，默认密码来自 `.env` 的 `ADMIN_PASSWORD`，未配置时为 `admin`。

后台按 Tab 分类：

- 账号：查看绑定用户和当前选择。
- 模型：切换、编辑和新增模型 API。
- 人设：查看和编辑当前用户 AI 名称、人设。
- 插件：开关插件。
- 记忆：查看长期记忆、中期摘要、热上下文和最近消息。
- 日志：查看最近日志和错误。

后台会定期刷新运行状态；当输入框、下拉框或文本框正在编辑，或存在未保存内容时，不会自动刷新覆盖输入。

## 微信指令

- `/help`：查看指令。
- `/persona`：查看当前 AI 人设。
- `/persona 人设内容`：更新当前微信号绑定的 AI 人设。
- `/memory`：查看中期摘要和长期结构化记忆。
- `/model`：查看当前模型。
- `/model list`：查看可切换模型。
- `/model switch 名称`：切换模型提供商。
- `/mute`：关闭主动消息。
- `/unmute`：恢复主动消息。
- `/reset_hot`：清空当前热上下文窗口。

## 模型

模型配置位于 `config.yaml` 的 `models` 段。当前支持 `openai_compatible`，示例包含：

- DeepSeek
- OpenAI
- 通义千问兼容模式
- Moonshot
- 智谱
- SiliconFlow
- OpenRouter
- Ollama / local
- 自定义 OpenAI-compatible API

详细说明见 [MODELS.md](MODELS.md)。

## 插件

插件位于 `src/wechat_ai_companion/plugins/`，启停由 `config.yaml` 和后台共同控制。当前内置 `proactive_response` 主动响应插件。

详细规范见 [PLUGINS.md](PLUGINS.md)。

## 主动消息安全策略

主动响应插件支持：

- 安静时间：默认 `23:30-08:00` 不主动发。
- 每用户每日上限：默认每人每天最多 3 条主动消息。
- 冷却时间：默认同一用户 180 分钟内最多主动发一次。
- 用户静音：微信发送 `/mute` 关闭主动消息，`/unmute` 恢复。
- 明确拒绝自动静音：消息包含“别主动”“不要主动”“不要再主动”等关键词会自动关闭该用户主动消息。

后台也可以对单个用户关闭或恢复主动消息。

## 记忆规则

- `profile`：档案类事实，例如名字、年龄区间、职业方向。新事实覆盖旧事实。
- `preference`：偏好类事实，例如不喜欢鸡汤、喜欢简短回复。latest-wins。
- `event`：事件锚点，例如某天分手、面试失败。append-only，不合并。
- `relation`：关系状态，例如熟悉度、信任度。按规则递增或衰减。

## 重要边界

- 当前只处理私聊文本消息；疑似群聊消息会跳过。
- `context_token` 必须使用收到消息里的当前值，项目已在 `send_text` 中处理。
- 主动消息会复用最近一次用户消息的 `context_token`；如果服务端拒绝旧 token，发送可能失败。
- OpenClaw/iLink 的服务状态、频率限制和字段可能变化，生产使用前需要做真实账号测试。

