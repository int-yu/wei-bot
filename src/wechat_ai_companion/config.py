from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


_ENV_PATTERN = re.compile(r"^\$\{([A-Z0-9_]+)(?::([^}]*))?}$")


@dataclass(slots=True)
class DeepSeekSettings:
    api_key: str
    base_url: str
    model: str
    max_tokens: int
    temperature: float
    timeout_seconds: int


@dataclass(slots=True)
class ModelProviderSettings:
    name: str
    api_format: str
    api_key: str
    base_url: str
    model: str
    endpoint_path: str = "/chat/completions"
    max_tokens: int = 1024
    temperature: float = 0.7
    timeout_seconds: int = 60
    headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelsSettings:
    active_provider: str
    providers: dict[str, ModelProviderSettings]


@dataclass(slots=True)
class MemorySettings:
    hot_min_turns: int
    hot_max_turns: int
    context_token_budget: int
    compression_trigger_ratio: float
    long_term_extract_every_turns: int


@dataclass(slots=True)
class WeChatSettings:
    base_url: str
    channel_version: str
    app_client_version: str
    bot_agent: str
    qrcode_scan_timeout_seconds: int
    show_typing: bool
    restore_session: bool
    session_duration_seconds: int
    restore_probe_timeout_seconds: int


@dataclass(slots=True)
class BotSettings:
    default_ai_name: str
    default_persona: str
    system_rules: str


@dataclass(slots=True)
class PluginsSettings:
    enabled: dict[str, bool]
    config: dict[str, Any]


@dataclass(slots=True)
class AdminSettings:
    enabled: bool
    host: str
    port: int
    username: str
    password: str


@dataclass(slots=True)
class Settings:
    data_dir: Path
    database_path: Path
    log_level: str
    deepseek: DeepSeekSettings
    models: ModelsSettings
    memory: MemorySettings
    wechat: WeChatSettings
    bot: BotSettings
    plugins: PluginsSettings
    admin: AdminSettings


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str):
        match = _ENV_PATTERN.match(value.strip())
        if match:
            name, default = match.groups()
            return os.getenv(name, default or "")
    if isinstance(value, dict):
        return {key: _resolve_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_env(item) for item in value]
    return value


def load_settings(config_path: str | Path = "config.yaml") -> Settings:
    load_dotenv()
    path = Path(config_path)
    if not path.exists():
        path = Path("config.example.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = _resolve_env(raw)

    app = cfg["app"]
    deepseek = cfg.get("deepseek", {})
    models = cfg.get("models")
    memory = cfg["memory"]
    wechat = cfg["wechat"]
    bot = cfg["bot"]
    plugins = cfg.get("plugins", {})
    enabled_plugins = {
        "flow_state": True,
        "proactive_response": True,
        "weather_monitor": True,
        "task_reminder": True,
    }
    enabled_plugins.update({str(key): bool(value) for key, value in plugins.get("enabled", {}).items()})

    data_dir = Path(app["data_dir"])
    database_path = Path(app["database_path"])
    data_dir.mkdir(parents=True, exist_ok=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    deepseek_settings = DeepSeekSettings(
        api_key=str(deepseek.get("api_key", "")),
        base_url=str(deepseek.get("base_url", "https://api.deepseek.com")).rstrip("/"),
        model=str(deepseek.get("model", "deepseek-v4-flash")),
        max_tokens=int(deepseek.get("max_tokens", 1024)),
        temperature=float(deepseek.get("temperature", 0.7)),
        timeout_seconds=int(deepseek.get("timeout_seconds", 60)),
    )
    models_settings = _load_models_settings(models, deepseek_settings)

    return Settings(
        data_dir=data_dir,
        database_path=database_path,
        log_level=str(app.get("log_level", "INFO")),
        deepseek=deepseek_settings,
        models=models_settings,
        memory=MemorySettings(
            hot_min_turns=int(memory["hot_min_turns"]),
            hot_max_turns=int(memory["hot_max_turns"]),
            context_token_budget=int(memory["context_token_budget"]),
            compression_trigger_ratio=float(memory["compression_trigger_ratio"]),
            long_term_extract_every_turns=int(memory["long_term_extract_every_turns"]),
        ),
        wechat=WeChatSettings(
            base_url=str(wechat["base_url"]).rstrip("/"),
            channel_version=str(wechat["channel_version"]),
            app_client_version=str(wechat["app_client_version"]),
            bot_agent=str(wechat["bot_agent"]),
            qrcode_scan_timeout_seconds=int(wechat["qrcode_scan_timeout_seconds"]),
            show_typing=bool(wechat["show_typing"]),
            restore_session=bool(wechat.get("restore_session", True)),
            session_duration_seconds=int(wechat.get("session_duration_seconds", 24 * 3600)),
            restore_probe_timeout_seconds=int(wechat.get("restore_probe_timeout_seconds", 8)),
        ),
        bot=BotSettings(
            default_ai_name=str(bot["default_ai_name"]),
            default_persona=str(bot["default_persona"]).strip(),
            system_rules=str(bot["system_rules"]).strip(),
        ),
        plugins=PluginsSettings(
            enabled=enabled_plugins,
            config=dict(plugins.get("config", {})),
        ),
        admin=AdminSettings(
            enabled=bool(app.get("admin_enabled", True)),
            host=str(app.get("admin_host", "127.0.0.1")),
            port=int(app.get("admin_port", 8765)),
            username=str(app.get("admin_username", "admin")),
            password=str(app.get("admin_password", "admin")),
        ),
    )


def _load_models_settings(raw_models: dict[str, Any] | None, legacy_deepseek: DeepSeekSettings) -> ModelsSettings:
    if not raw_models:
        provider = ModelProviderSettings(
            name="deepseek",
            api_format="openai_compatible",
            api_key=legacy_deepseek.api_key,
            base_url=legacy_deepseek.base_url,
            model=legacy_deepseek.model,
            max_tokens=legacy_deepseek.max_tokens,
            temperature=legacy_deepseek.temperature,
            timeout_seconds=legacy_deepseek.timeout_seconds,
            extra_body={"thinking": {"type": "disabled"}}
            if legacy_deepseek.model == "deepseek-v4-flash"
            else {},
        )
        return ModelsSettings(active_provider="deepseek", providers={"deepseek": provider})

    active_provider = str(raw_models.get("active_provider", "deepseek"))
    raw_providers = raw_models.get("providers", {})
    providers: dict[str, ModelProviderSettings] = {}
    for name, raw in raw_providers.items():
        item = dict(raw or {})
        providers[str(name)] = ModelProviderSettings(
            name=str(name),
            api_format=str(item.get("api_format", "openai_compatible")),
            api_key=str(item.get("api_key", "")),
            base_url=str(item.get("base_url", "")).rstrip("/"),
            model=str(item.get("model", "")),
            endpoint_path=str(item.get("endpoint_path", "/chat/completions")),
            max_tokens=int(item.get("max_tokens", raw_models.get("max_tokens", 1024))),
            temperature=float(item.get("temperature", raw_models.get("temperature", 0.7))),
            timeout_seconds=int(item.get("timeout_seconds", raw_models.get("timeout_seconds", 60))),
            headers={str(k): str(v) for k, v in dict(item.get("headers", {})).items()},
            extra_body=dict(item.get("extra_body", {})),
        )

    if "deepseek" not in providers and legacy_deepseek.api_key:
        providers["deepseek"] = ModelProviderSettings(
            name="deepseek",
            api_format="openai_compatible",
            api_key=legacy_deepseek.api_key,
            base_url=legacy_deepseek.base_url,
            model=legacy_deepseek.model,
            max_tokens=legacy_deepseek.max_tokens,
            temperature=legacy_deepseek.temperature,
            timeout_seconds=legacy_deepseek.timeout_seconds,
            extra_body={"thinking": {"type": "disabled"}}
            if legacy_deepseek.model == "deepseek-v4-flash"
            else {},
        )
    return ModelsSettings(active_provider=active_provider, providers=providers)
