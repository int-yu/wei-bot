from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import aiohttp


@dataclass(slots=True)
class LLMResponse:
    content: str
    raw: dict[str, Any]


@dataclass(slots=True)
class ModelProviderConfig:
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


class ModelRouter:
    def __init__(self, providers: dict[str, ModelProviderConfig], active_provider: str) -> None:
        if not providers:
            raise ValueError("No model providers configured.")
        if active_provider not in providers:
            names = ", ".join(sorted(providers))
            raise ValueError(f"Active model provider {active_provider!r} is not configured. Available: {names}")
        self.providers = providers
        self.active_provider = active_provider

    @property
    def current(self) -> ModelProviderConfig:
        return self.providers[self.active_provider]

    def list_provider_names(self) -> list[str]:
        return sorted(self.providers)

    def describe_current(self) -> str:
        provider = self.current
        return (
            f"{provider.name} | format={provider.api_format} | "
            f"model={provider.model} | base_url={provider.base_url}"
        )

    def public_provider_dict(self, name: str) -> dict[str, Any]:
        provider = self.providers[name]
        return {
            "name": provider.name,
            "api_format": provider.api_format,
            "api_key_masked": mask_secret(provider.api_key),
            "has_api_key": bool(provider.api_key),
            "base_url": provider.base_url,
            "model": provider.model,
            "endpoint_path": provider.endpoint_path,
            "max_tokens": provider.max_tokens,
            "temperature": provider.temperature,
            "timeout_seconds": provider.timeout_seconds,
            "headers": provider.headers,
            "extra_body": provider.extra_body,
        }

    def export_provider_dict(self, name: str) -> dict[str, Any]:
        provider = self.providers[name]
        return {
            "name": provider.name,
            "api_format": provider.api_format,
            "api_key": provider.api_key,
            "base_url": provider.base_url,
            "model": provider.model,
            "endpoint_path": provider.endpoint_path,
            "max_tokens": provider.max_tokens,
            "temperature": provider.temperature,
            "timeout_seconds": provider.timeout_seconds,
            "headers": provider.headers,
            "extra_body": provider.extra_body,
        }

    def upsert_provider(self, provider: ModelProviderConfig) -> ModelProviderConfig:
        self.providers[provider.name] = provider
        return provider

    def switch_provider(self, name: str) -> ModelProviderConfig:
        if name not in self.providers:
            names = ", ".join(sorted(self.providers))
            raise ValueError(f"Unknown model provider {name!r}. Available: {names}")
        provider = self.providers[name]
        _validate_provider(provider)
        self.active_provider = name
        return provider

    async def chat(self, messages: list[dict[str, str]], *, max_tokens: int | None = None) -> LLMResponse:
        provider = self.current
        _validate_provider(provider)
        if provider.api_format != "openai_compatible":
            raise ValueError(f"Unsupported model api_format: {provider.api_format}")
        return await _chat_openai_compatible(provider, messages, max_tokens=max_tokens)


class DeepSeekClient(ModelRouter):
    """Backward-compatible wrapper around the generic model router."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        timeout_seconds: int = 60,
    ) -> None:
        provider = ModelProviderConfig(
            name="deepseek",
            api_format="openai_compatible",
            api_key=api_key,
            base_url=base_url,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            extra_body={"thinking": {"type": "disabled"}} if model == "deepseek-v4-flash" else {},
        )
        super().__init__({"deepseek": provider}, "deepseek")


async def _chat_openai_compatible(
    provider: ModelProviderConfig,
    messages: list[dict[str, str]],
    *,
    max_tokens: int | None,
) -> LLMResponse:
    payload: dict[str, Any] = {
        "model": provider.model,
        "messages": messages,
        "max_tokens": max_tokens or provider.max_tokens,
        "temperature": provider.temperature,
        "stream": False,
    }
    payload.update(provider.extra_body)

    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
        "User-Agent": "the-one-wechat-ai/0.1.0",
    }
    headers.update(provider.headers)

    endpoint = f"{provider.base_url.rstrip('/')}/{provider.endpoint_path.lstrip('/')}"
    last_error: Exception | None = None
    for attempt, delay in enumerate([0, 2, 4, 8]):
        if delay:
            await asyncio.sleep(delay)
        try:
            timeout = aiohttp.ClientTimeout(total=provider.timeout_seconds)
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
            async with aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                trust_env=True,
            ) as session:
                async with session.post(endpoint, headers=headers, json=payload) as response:
                    text = await response.text()
                    if response.status >= 400:
                        raise RuntimeError(f"{provider.name} HTTP {response.status}: {text[:500]}")
                    data = json.loads(text)
            content = data["choices"][0]["message"].get("content", "")
            if not content:
                raise RuntimeError(f"{provider.name} response does not contain message content.")
            return LLMResponse(content=content, raw=data)
        except Exception as exc:
            last_error = exc
            if attempt == 3:
                break
    raise RuntimeError(f"{provider.name} request failed after retries: {last_error}") from last_error


def _validate_provider(provider: ModelProviderConfig) -> None:
    if provider.api_format != "openai_compatible":
        raise ValueError(f"Provider {provider.name} uses unsupported api_format={provider.api_format!r}")
    if not provider.api_key and not provider.base_url.startswith(("http://localhost", "http://127.0.0.1")):
        raise ValueError(f"Model provider {provider.name} API key is empty.")
    if not provider.base_url:
        raise ValueError(f"Model provider {provider.name} base_url is empty.")
    if not provider.model:
        raise ValueError(f"Model provider {provider.name} model is empty.")


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * max(4, len(value) - 8)}{value[-4:]}"
