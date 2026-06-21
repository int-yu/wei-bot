# The One WeChat AI Companion

这是一个基于微信 OpenClaw/Claw iLink 协议层的个人 AI 联系人项目。运行后通过终端二维码扫码连接微信，AI 在微信里表现为一个联系人。当前实现以文本私聊为主，主要调用 DeepSeek。

## 核心能力

- 扫码连接微信 ClawBot / OpenClaw iLink 服务。
- 一个微信号绑定一个 AI：每个 `from_user_id` 有独立人设、对话和记忆。
- 支持用户在微信里用 `/persona ...` 自定义 AI 人设。
- 三层记忆：
  - 热上下文：默认保留最近 20 到 40 轮对话。
  - 中期摘要：上下文达到预算 70% 或窗口超限时，压缩较早对话。
  - 长期结构化记忆：抽取 `profile`、`preference`、`event`、`relation`。
- SQLite 本地持久化，不依赖外部数据库。

## 目录

```text
src/wechat_ai_companion/
  bot.py                 # Bot 编排与微信指令
  config.py              # YAML/env 配置加载
  llm.py                 # DeepSeek OpenAI-compatible 客户端
  memory.py              # 三层记忆与 SQLite 存储
  models.py              # 数据模型
  wechat_openclaw.py     # 微信 OpenClaw/iLink 协议接入
  main.py                # CLI 入口
```

参考项目 `weixin-ClawBot-API-main` 保留在目录中，本项目没有直接修改它。

## 启动

```powershell
cd D:\codex\the_one
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item config.example.yaml config.yaml
```

编辑 `.env`，填入 `DEEPSEEK_API_KEY`。默认模型使用 DeepSeek 官方当前 OpenAI 格式模型名 `deepseek-v4-flash`。然后运行：

```powershell
.\.venv\Scripts\python -m wechat_ai_companion.main
```

如果使用可编辑安装：

```powershell
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\the-one
```

## 微信指令

- `/help`：查看指令。
- `/persona`：查看当前微信号绑定的 AI 人设。
- `/persona 人设内容`：更新当前微信号绑定的 AI 人设。
- `/memory`：查看中期摘要和长期结构化记忆。
- `/model`：查看当前模型。
- `/model list`：查看可切换模型。
- `/model switch 名称`：切换模型提供商，切换结果会持久化。
- `/reset_hot`：清空热上下文窗口标记，长期记忆和中期摘要不受影响。

## 模型

模型配置位于 `config.yaml` 的 `models` 段。当前支持 OpenAI-compatible Chat Completions 格式，已提供 DeepSeek、OpenAI、通义千问兼容模式、Moonshot、智谱、SiliconFlow、OpenRouter、Ollama/local 和自定义 API 模板。详细说明见 `MODELS.md`。

## 插件

插件位于 `src/wechat_ai_companion/plugins/`，启停由 `config.yaml` 的 `plugins.enabled` 控制。当前内置 `proactive_response` 主动响应插件，会基于长期记忆、用户习惯、最近对话和不活跃时间判断是否应主动发微信消息。插件开发规范见 `PLUGINS.md`。

## 管理后台

启动 Bot 后，本地后台默认同时启动：

```text
http://127.0.0.1:8765
```

后台需要账号密码登录，默认账号为 `admin`，默认密码来自 `.env` 的 `ADMIN_PASSWORD`，若未配置则为 `admin`。监听地址只绑定本机，避免默认暴露到局域网。

后台按 Tab 分类：

- 账号：查看绑定用户和当前选择。
- 模型：切换、编辑和新增模型 API。
- 人设：查看/编辑当前用户 AI 名称和人设。
- 插件：开关插件。
- 记忆：查看长期记忆、中期摘要、热上下文/最近消息。
- 日志：查看最近日志和错误。

后台会定期刷新运行状态，但当输入框、下拉框或文本框正在编辑时不会自动刷新，避免覆盖未保存内容。

模型管理区支持直接编辑模型配置：

- Provider 名称
- API 格式，目前支持 `openai_compatible`
- Base URL
- 模型名
- API Key
- Endpoint Path
- Max Tokens / Temperature / Timeout
- Headers JSON
- Extra Body JSON

API Key 不会在页面明文回显，只显示掩码。编辑已有模型时，API Key 留空表示保留原密钥；填写新值则覆盖。保存后的模型配置会立即写入运行中的模型路由器，并持久化到本地 SQLite。

可在 `config.yaml` 中调整：

```yaml
app:
  admin_enabled: true
  admin_host: 127.0.0.1
  admin_port: 8765
```

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
- OpenClaw/iLink 的服务状态、频率限制和字段可能变化，生产使用前需要做真实账号测试。
- 不要把 `.env`、`config.yaml` 或数据库提交到版本控制。
