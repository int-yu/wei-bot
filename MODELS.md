# 模型接入与切换

项目现在使用通用 `ModelRouter`，默认支持 OpenAI-compatible Chat Completions 格式。

## 微信指令

- `/model`：查看当前模型。
- `/model list`：查看配置里的全部模型提供商。
- `/model switch deepseek`：切换到指定提供商。

切换结果会写入 SQLite，下次重启会尝试沿用上次选择。若该提供商已从配置删除或配置无效，启动时会回退到 `config.yaml` 的 `models.active_provider`。

## 内置示例提供商

`config.yaml` 和 `config.example.yaml` 已包含这些 OpenAI-compatible 配置模板：

- `deepseek`
- `openai`
- `qwen`
- `moonshot`
- `zhipu`
- `siliconflow`
- `openrouter`
- `local_ollama`
- `custom_openai_compatible`

这些模板不会自动代表账号已可用；对应 API Key 仍需写入 `.env`。

## 自定义模型 API

如果你的服务兼容 OpenAI Chat Completions，只需要新增一个 provider：

```yaml
models:
  active_provider: deepseek
  providers:
    my_model:
      api_format: openai_compatible
      api_key: ${CUSTOM_MODEL_API_KEY}
      base_url: https://your-api-host.example/v1
      model: your-model-name
      endpoint_path: /chat/completions
      max_tokens: 1024
      temperature: 0.7
      timeout_seconds: 60
      headers:
        X-Custom-Header: value
      extra_body:
        some_vendor_option: true
```

然后在 `.env` 里设置：

```env
CUSTOM_MODEL_API_KEY=你的key
```

微信里发送：

```text
/model switch my_model
```

## 注意

当前实现只支持 `api_format: openai_compatible`。Anthropic 原生 `/v1/messages`、Gemini 原生接口等非 OpenAI 格式，需要再写对应适配器。

