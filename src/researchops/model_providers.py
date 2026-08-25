from __future__ import annotations

import importlib.metadata
import math
import re
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any, AsyncContextManager, ClassVar, Protocol


_OPENAI_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DEEPSEEK_MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})
_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_MODELS = frozenset(
    {"claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5-20251001"}
)
_RESPONSES_TRANSPORT_ID = "openai_responses"
_LITELLM_TRANSPORT_ID = "litellm_anthropic_chat_completions"
_LITELLM_COMPATIBILITY_VERSION = "1.83.0"
_OPENAI_AGENTS_COMPATIBILITY_VERSION = "0.21.0"
SUPPORTED_PROVIDER_IDS = ("openai", "deepseek", "anthropic")
_LITELLM_PROCESS_LOCK = threading.Lock()


class _NoRetentionDict(dict):
    """A mapping-shaped sink for LiteLLM's process-global debug records."""

    def __setitem__(self, key, value) -> None:
        del key, value

    def update(self, *args, **kwargs) -> None:
        del args, kwargs

    def setdefault(self, key, default=None):
        del key
        return default


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
    Responses-API transport used by OpenAI/DeepSeek. The compatibility version
    is pinned because newer LiteLLM releases currently conflict with the
    repository's locked OpenAI SDK major version.
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
        normalized_timeout = _validate_timeout(timeout_seconds)
        dependency_status = provider_transport_status(self)
        if not dependency_status["installed"]:
            raise ProviderConfigurationError(
                "provider_sdk_not_installed",
                "未安装 Anthropic Provider 的精确 transport 依赖。",
            )
        if not dependency_status["compatible"]:
            raise ProviderConfigurationError(
                "provider_transport_dependency_drift",
                "Anthropic Provider transport 版本与精确兼容合同不一致。",
            )
        if not _LITELLM_PROCESS_LOCK.acquire(blocking=False):
            raise ProviderConfigurationError(
                "provider_concurrency_denied",
                "Anthropic LiteLLM transport 当前已有受控运行。",
            )

        litellm_module = None
        original_error_logs = None
        original_turn_off_message_logging = None
        original_suppress_debug_info = None
        global_state_replaced = False
        owned_handler = None
        sdk_model = None
        try:
            LitellmModel, AsyncHTTPHandler, litellm_module = _load_litellm_transport()
            _require_isolated_litellm_globals(litellm_module)
            original_error_logs = litellm_module.error_logs
            original_turn_off_message_logging = getattr(
                litellm_module, "turn_off_message_logging", False
            )
            original_suppress_debug_info = getattr(
                litellm_module, "suppress_debug_info", False
            )
            litellm_module.error_logs = _NoRetentionDict()
            litellm_module.turn_off_message_logging = True
            litellm_module.suppress_debug_info = True
            global_state_replaced = True
            owned_handler = AsyncHTTPHandler(timeout=normalized_timeout)

            class ControlledAnthropicLitellmModel(LitellmModel):
                async def _fetch_response(
                    self,
                    system_instructions,
                    input,
                    model_settings,
                    tools,
                    output_schema,
                    handoffs,
                    span,
                    tracing,
                    stream=False,
                    prompt=None,
                ):
                    # Inject non-serializable client ownership only after the SDK has
                    # generated its trace-safe ModelSettings projection.
                    # Do not inherit caller-supplied LiteLLM routing/retry aliases.
                    extra_args = {
                        "client": owned_handler,
                        "timeout": normalized_timeout,
                        "num_retries": 0,
                        "max_retries": 0,
                        "retry_policy": None,
                        "fallbacks": [],
                        "context_window_fallbacks": [],
                        "content_policy_fallbacks": [],
                        "context_window_fallback_dict": {},
                    }
                    return await super()._fetch_response(
                        system_instructions=system_instructions,
                        input=input,
                        model_settings=replace(model_settings, extra_args=extra_args),
                        tools=tools,
                        output_schema=output_schema,
                        handoffs=handoffs,
                        span=span,
                        tracing=tracing,
                        stream=stream,
                        prompt=prompt,
                    )

            sdk_model = ControlledAnthropicLitellmModel(
                model=f"anthropic/{normalized_model}",
                base_url=_ANTHROPIC_BASE_URL,
                api_key=normalized_key,
            )
            yield ProviderModel(
                provider_id=self.provider_id,
                model_id=normalized_model,
                transport_id=self.transport_id,
                sdk_model=sdk_model,
            )
        finally:
            try:
                if sdk_model is not None:
                    sdk_model.api_key = None
                if owned_handler is not None:
                    await owned_handler.close()
            finally:
                if litellm_module is not None and global_state_replaced:
                    litellm_module.error_logs = original_error_logs
                    litellm_module.turn_off_message_logging = (
                        original_turn_off_message_logging
                    )
                    litellm_module.suppress_debug_info = original_suppress_debug_info
                _LITELLM_PROCESS_LOCK.release()


def provider_transport_status(provider: ProviderAdapter) -> dict[str, Any]:
    """Report provider transport dependencies without importing them or networking."""

    required = ["openai-agents"]
    expected_versions: dict[str, str] = {}
    if provider.provider_id == AnthropicProvider.provider_id:
        required.append("litellm")
        expected_versions.update(
            {
                "openai-agents": _OPENAI_AGENTS_COMPATIBILITY_VERSION,
                "litellm": _LITELLM_COMPATIBILITY_VERSION,
            }
        )
    versions: dict[str, str | None] = {}
    compatible = True
    for package in required:
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = None
            compatible = False
        versions[package] = version
        expected = expected_versions.get(package)
        if expected is not None and version != expected:
            compatible = False
    return {
        "installed": all(value is not None for value in versions.values()),
        "compatible": compatible,
        "packages": versions,
        "expected_versions": expected_versions,
        "network_calls": 0,
    }


def get_provider(provider_id: str) -> ProviderAdapter:
    """Resolve an explicitly selected provider without loading an SDK or client."""

    if not isinstance(provider_id, str):
        raise ProviderConfigurationError(
            "provider_invalid", "provider 必须来自受控 allowlist。"
        )
    normalized = provider_id.strip().lower()
    if normalized == OpenAIProvider.provider_id:
        return OpenAIProvider()
    if normalized == DeepSeekProvider.provider_id:
        return DeepSeekProvider()
    if normalized == AnthropicProvider.provider_id:
        return AnthropicProvider()
    raise ProviderConfigurationError(
        "provider_invalid", "provider 必须来自受控 allowlist。"
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


def _require_isolated_litellm_globals(litellm_module: Any) -> None:
    for name in (
        "fallbacks",
        "context_window_fallbacks",
        "content_policy_fallbacks",
        "callbacks",
        "success_callback",
        "failure_callback",
        "input_callback",
        "_async_input_callback",
        "_async_success_callback",
        "_async_failure_callback",
        "service_callback",
        "audit_log_callbacks",
        "callback_settings",
        "cache",
        "num_retries",
        "retry_policy",
        "context_window_fallback_dict",
        "model_alias_map",
        "num_retries_per_request",
        "custom_prompt_dict",
        "additional_drop_params",
        "api_base",
        "api_key",
        "organization",
        "project",
        "drop_params",
        "modify_params",
        "error_logs",
    ):
        value = getattr(litellm_module, name, None)
        if value not in (None, False, 0, [], (), {}):
            raise ProviderConfigurationError(
                "provider_global_state_not_isolated",
                "LiteLLM 全局 fallback/callback/cache 必须为空。",
            )


def _load_litellm_transport() -> tuple[Any, Any, Any]:
    """Delay the optional LiteLLM extension import so status checks stay
    side-effect free, matching `_load_responses_transport`.
    """

    try:
        import litellm
        from agents.extensions.models.litellm_model import LitellmModel
        from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
    except ImportError as exc:
        raise ProviderConfigurationError(
            "provider_sdk_not_installed",
            "未安装 Agents SDK 的 LiteLLM provider transport（需要 `pip install "
            "\"openai-agents[litellm]\"`）。",
        ) from exc
    return LitellmModel, AsyncHTTPHandler, litellm
