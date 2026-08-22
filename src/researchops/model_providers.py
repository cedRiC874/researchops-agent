from __future__ import annotations

import math
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncContextManager, ClassVar, Protocol


_OPENAI_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DEEPSEEK_MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})
_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_ANTHROPIC_MODELS = frozenset(
    {"claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5-20251001"}
)
_RESPONSES_TRANSPORT_ID = "openai_responses"
_LITELLM_TRANSPORT_ID = "litellm_anthropic"


class ProviderConfigurationError(RuntimeError):
    """A stable provider-configuration failure that never includes credentials."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict[str, str]:
        return {"status": "error", "error_code": self.code, "message": str(self)}


@dataclass(frozen=True)
class ProviderModel:
    """One explicitly bound provider/model/transport combination."""

    provider_id: str
    model_id: str
    transport_id: str
    sdk_model: Any = field(repr=False)


class ProviderAdapter(Protocol):
    """Factory contract for an isolated Agents SDK model transport."""

    provider_id: str
    api_key_env: str
    transport_id: str

    def validate_model(self, model_id: str) -> str: ...

    def open_model(
        self, *, model_id: str, api_key: str, timeout_seconds: float = 120.0
    ) -> AsyncContextManager[ProviderModel]: ...


@dataclass(frozen=True, slots=True)
class OpenAIProvider:
    provider_id: ClassVar[str] = "openai"
    api_key_env: ClassVar[str] = "OPENAI_API_KEY"
    transport_id: ClassVar[str] = _RESPONSES_TRANSPORT_ID

    def validate_model(self, model_id: str) -> str:
        if not isinstance(model_id, str):
            raise ProviderConfigurationError(
                "provider_model_invalid", "OpenAI model ID 必须是安全的非空字符串。"
            )
        normalized = model_id.strip()
        if not _OPENAI_MODEL_ID.fullmatch(normalized):
            raise ProviderConfigurationError(
                "provider_model_invalid", "OpenAI model ID 不符合安全格式。"
            )
        return normalized

    @asynccontextmanager
    async def open_model(
        self, *, model_id: str, api_key: str, timeout_seconds: float = 120.0
    ) -> AsyncIterator[ProviderModel]:
        normalized_model = self.validate_model(model_id)
        normalized_key = _require_api_key(api_key, self.api_key_env)
        normalized_timeout = _validate_timeout(timeout_seconds)
        AsyncOpenAI, OpenAIResponsesModel = _load_responses_transport()
        client = AsyncOpenAI(
            api_key=normalized_key,
            timeout=normalized_timeout,
            max_retries=0,
        )
        try:
            sdk_model = OpenAIResponsesModel(
                model=normalized_model,
                openai_client=client,
            )
            yield ProviderModel(
                provider_id=self.provider_id,
                model_id=normalized_model,
                transport_id=self.transport_id,
                sdk_model=sdk_model,
            )
        finally:
            await client.close()


@dataclass(frozen=True, slots=True)
class DeepSeekProvider:
    provider_id: ClassVar[str] = "deepseek"
    api_key_env: ClassVar[str] = "DEEPSEEK_API_KEY"
    transport_id: ClassVar[str] = "openai_compatible_responses"
    base_url: ClassVar[str] = _DEEPSEEK_BASE_URL

    def validate_model(self, model_id: str) -> str:
        if not isinstance(model_id, str):
            raise ProviderConfigurationError(
                "provider_model_invalid", "DeepSeek model ID 必须是显式字符串。"
            )
        normalized = model_id.strip()
        if normalized not in _DEEPSEEK_MODELS:
            raise ProviderConfigurationError(
                "provider_model_not_allowed",
                "DeepSeek model ID 不在受控 allowlist 中。",
            )
        return normalized

    @asynccontextmanager
    async def open_model(
        self, *, model_id: str, api_key: str, timeout_seconds: float = 120.0
    ) -> AsyncIterator[ProviderModel]:
        normalized_model = self.validate_model(model_id)
        normalized_key = _require_api_key(api_key, self.api_key_env)
        normalized_timeout = _validate_timeout(timeout_seconds)
        AsyncOpenAI, OpenAIResponsesModel = _load_responses_transport()
        client = AsyncOpenAI(
            api_key=normalized_key,
            base_url=self.base_url,
            timeout=normalized_timeout,
            max_retries=0,
        )
        try:
            sdk_model = OpenAIResponsesModel(
                model=normalized_model,
                openai_client=client,
            )
            yield ProviderModel(
                provider_id=self.provider_id,
                model_id=normalized_model,
                transport_id=self.transport_id,
                sdk_model=sdk_model,
            )
        finally:
            await client.close()


@dataclass(frozen=True, slots=True)
class AnthropicProvider:
    """Anthropic models via the Agents SDK's LiteLLM transport.

    The OpenAI Agents SDK does not speak Anthropic's API natively, so this
    adapter routes through `agents.extensions.models.litellm_model.LitellmModel`
    (LiteLLM's `anthropic/<model>` naming convention) instead of the
    Responses-API transport used by OpenAI/DeepSeek. No client object is
    constructed up front, so there is no long-lived connection to close.
    """

    provider_id: ClassVar[str] = "anthropic"
    api_key_env: ClassVar[str] = "ANTHROPIC_API_KEY"
    transport_id: ClassVar[str] = _LITELLM_TRANSPORT_ID

    def validate_model(self, model_id: str) -> str:
        if not isinstance(model_id, str):
            raise ProviderConfigurationError(
                "provider_model_invalid", "Anthropic model ID 必须是显式字符串。"
            )
        normalized = model_id.strip()
        if normalized not in _ANTHROPIC_MODELS:
            raise ProviderConfigurationError(
                "provider_model_not_allowed",
                "Anthropic model ID 不在受控 allowlist 中。",
            )
        return normalized

    @asynccontextmanager
    async def open_model(
        self, *, model_id: str, api_key: str, timeout_seconds: float = 120.0
    ) -> AsyncIterator[ProviderModel]:
        normalized_model = self.validate_model(model_id)
        normalized_key = _require_api_key(api_key, self.api_key_env)
        # timeout is validated for parity with the other adapters even though
        # LitellmModel takes it per-call rather than at construction time.
        _validate_timeout(timeout_seconds)
        LitellmModel = _load_litellm_transport()
        sdk_model = LitellmModel(
            model=f"anthropic/{normalized_model}",
            api_key=normalized_key,
        )
        yield ProviderModel(
            provider_id=self.provider_id,
            model_id=normalized_model,
            transport_id=self.transport_id,
            sdk_model=sdk_model,
        )


def get_provider(provider_id: str) -> ProviderAdapter:
    """Resolve an explicitly selected provider without loading an SDK or client."""

    if not isinstance(provider_id, str):
        raise ProviderConfigurationError(
            "provider_invalid", "provider 必须是 openai、deepseek 或 anthropic。"
        )
    normalized = provider_id.strip().lower()
    if normalized == OpenAIProvider.provider_id:
        return OpenAIProvider()
    if normalized == DeepSeekProvider.provider_id:
        return DeepSeekProvider()
    if normalized == AnthropicProvider.provider_id:
        return AnthropicProvider()
    raise ProviderConfigurationError(
        "provider_invalid", "provider 必须是 openai、deepseek 或 anthropic。"
    )


def _require_api_key(api_key: str, api_key_env: str) -> str:
    if not isinstance(api_key, str) or not api_key.strip():
        raise ProviderConfigurationError(
            "provider_api_key_missing", f"未配置 {api_key_env}；provider client 未创建。"
        )
    return api_key.strip()


def _validate_timeout(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 < float(value) <= 600
    ):
        raise ProviderConfigurationError(
            "provider_timeout_invalid", "Provider timeout 必须在 (0, 600] 秒内。"
        )
    return float(value)


def _load_responses_transport() -> tuple[Any, Any]:
    """Delay optional SDK imports so status checks stay side-effect free."""

    try:
        from agents import OpenAIResponsesModel
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise ProviderConfigurationError(
            "provider_sdk_not_installed", "未安装 OpenAI Agents SDK provider transport。"
        ) from exc
    return AsyncOpenAI, OpenAIResponsesModel


def _load_litellm_transport() -> Any:
    """Delay the optional LiteLLM extension import so status checks stay
    side-effect free, matching `_load_responses_transport`.
    """

    try:
        from agents.extensions.models.litellm_model import LitellmModel
    except ImportError as exc:
        raise ProviderConfigurationError(
            "provider_sdk_not_installed",
            "未安装 Agents SDK 的 LiteLLM provider transport（需要 `pip install "
            "\"openai-agents[litellm]\"`）。",
        ) from exc
    return LitellmModel
