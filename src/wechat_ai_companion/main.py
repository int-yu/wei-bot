from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .admin import AdminServer, InMemoryLogHandler
from .bot import CompanionBot
from .config import load_settings
from .llm import ModelProviderConfig, ModelRouter
from .memory import MemoryStore
from .plugins import PluginManager
from .wechat_openclaw import OpenClawWeChatClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WeChat OpenClaw AI companion.")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config. Defaults to config.yaml.")
    return parser


async def async_main() -> None:
    args = build_parser().parse_args()
    settings = load_settings(args.config)
    log_handler = InMemoryLogHandler()
    log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), log_handler],
    )
    memory = MemoryStore(settings.database_path, settings.memory)
    llm = ModelRouter(
        {
            name: ModelProviderConfig(
                name=provider.name,
                api_format=provider.api_format,
                api_key=provider.api_key,
                base_url=provider.base_url,
                model=provider.model,
                endpoint_path=provider.endpoint_path,
                max_tokens=provider.max_tokens,
                temperature=provider.temperature,
                timeout_seconds=provider.timeout_seconds,
                headers=provider.headers,
                extra_body=provider.extra_body,
            )
            for name, provider in settings.models.providers.items()
        },
        settings.models.active_provider,
    )
    for row in memory.list_plugin_state("core_model_provider", "config"):
        try:
            raw = json.loads(row["state_value"])
            provider = ModelProviderConfig(
                name=str(raw["name"]),
                api_format=str(raw.get("api_format", "openai_compatible")),
                api_key=str(raw.get("api_key", "")),
                base_url=str(raw.get("base_url", "")).rstrip("/"),
                model=str(raw.get("model", "")),
                endpoint_path=str(raw.get("endpoint_path", "/chat/completions")),
                max_tokens=int(raw.get("max_tokens", 1024)),
                temperature=float(raw.get("temperature", 0.7)),
                timeout_seconds=int(raw.get("timeout_seconds", 60)),
                headers=dict(raw.get("headers", {})),
                extra_body=dict(raw.get("extra_body", {})),
            )
            llm.upsert_provider(provider)
            logging.info("[model] loaded provider override from database: %s", provider.name)
        except Exception:
            logging.exception("[model] failed to load provider override: %s", row)
    saved_provider = memory.get_plugin_state("core_model", "__global__", "active_provider")
    if saved_provider:
        try:
            llm.switch_provider(saved_provider)
            logging.info("[model] restored active provider from database: %s", saved_provider)
        except ValueError as exc:
            logging.warning("[model] ignored saved provider %s: %s", saved_provider, exc)
    wechat = OpenClawWeChatClient(settings.wechat)
    plugin_manager = PluginManager(settings, wechat, memory, llm)
    admin_server = AdminServer(settings, memory, llm, plugin_manager, log_handler) if settings.admin.enabled else None
    bot = CompanionBot(settings, wechat, memory, llm, plugin_manager)
    try:
        if admin_server:
            await admin_server.start()
        await bot.run_forever()
    finally:
        if admin_server:
            await admin_server.stop()
        await plugin_manager.stop()
        memory.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
