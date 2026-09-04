from __future__ import annotations

import asyncio
import importlib.metadata
import inspect
import math
import os
import re
import threading
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any, AsyncContextManager, ClassVar, Protocol

from researchops_completion_telemetry.sanitization import (
    SanitizedCompletionCapture,
    sanitize_completion_capture,
)


_OPENAI_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OPENAI_BASE_URL = "https://api.openai.com/v1"
_DEEPSEEK_MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})
_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_ALLOWED_MODEL_IDS = (
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-haiku-4-5-20251001",
)
_ANTHROPIC_MODELS = frozenset(ANTHROPIC_ALLOWED_MODEL_IDS)
_RESPONSES_TRANSPORT_ID = "openai_responses"
_LITELLM_TRANSPORT_ID = "litellm_anthropic_chat_completions"
_RESPONSES_API_SURFACE = "responses"
_ANTHROPIC_API_SURFACE = "messages"
_OPENAI_ADAPTER_VERSION = "openai-responses-adapter/1.0"
_DEEPSEEK_ADAPTER_VERSION = "deepseek-responses-adapter/1.0"
_ANTHROPIC_ADAPTER_VERSION = "anthropic-litellm-adapter/1.0"
_LITELLM_COMPATIBILITY_VERSION = "1.83.0"
_OPENAI_AGENTS_COMPATIBILITY_VERSION = "0.21.0"
_RAW_RESPONSE_CLEANUP_TIMEOUT_SECONDS = 5.0
_DEEPSEEK_POST_REQUEST_CLEANUP_TIMEOUT_SECONDS = 5.0
SUPPORTED_PROVIDER_IDS = ("openai", "deepseek", "anthropic")
ANTHROPIC_GENERIC_ONLINE_DISABLED_CODE = (
    "anthropic_generic_online_entrypoint_disabled"
)
_LITELLM_PROCESS_LOCK = threading.Lock()


def _anthropic_online_transport_authorized(authorization: object | None) -> bool:
    """Hard-disable the generic transport; offline tests patch this exact gate."""

    del authorization
    return False


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


class CompletionTelemetrySession(Protocol):
    """Per-case attempt ledger; implementations own indices and reconciliation."""

    provider_id: str
    api_surface: str
    transport_id: str
    adapter_version: str

    def begin_attempt(self) -> object: ...

    def finalize_response_accepted(
        self, handle: object, capture: SanitizedCompletionCapture
    ) -> None: ...

    def finalize_response_rejected(
        self, handle: object, error_code: str
    ) -> None: ...

    def finalize_http_error(self, handle: object, error_code: str) -> None: ...

    def finalize_no_response(self, handle: object, error_code: str) -> None: ...

    def finalize_cancelled(self, handle: object) -> None: ...

    def finalize_outcome_unknown(self, handle: object, error_code: str) -> None: ...


@dataclass(frozen=True)
class ProviderModel:
    """One explicitly bound provider/model/transport combination."""

    provider_id: str
    model_id: str
    transport_id: str
    sdk_model: Any = field(repr=False)
    api_surface: str = "not_persisted"
    adapter_version: str = "not_persisted"
    completion_telemetry_session: CompletionTelemetrySession | None = field(
        default=None, repr=False, compare=False
    )


class ProviderAdapter(Protocol):
    """Factory contract for an isolated Agents SDK model transport."""

    provider_id: str
    api_key_env: str
    transport_id: str
    api_surface: str
    adapter_version: str

    def validate_model(self, model_id: str) -> str: ...

    def open_model(
        self,
        *,
        model_id: str,
        api_key: str,
        timeout_seconds: float = 120.0,
        completion_telemetry_session: CompletionTelemetrySession | None = None,
    ) -> AsyncContextManager[ProviderModel]: ...


@dataclass(frozen=True, slots=True)
class OpenAIProvider:
    provider_id: ClassVar[str] = "openai"
    api_key_env: ClassVar[str] = "OPENAI_API_KEY"
    transport_id: ClassVar[str] = _RESPONSES_TRANSPORT_ID
    api_surface: ClassVar[str] = _RESPONSES_API_SURFACE
    adapter_version: ClassVar[str] = _OPENAI_ADAPTER_VERSION

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
        self,
        *,
        model_id: str,
        api_key: str,
        timeout_seconds: float = 120.0,
        completion_telemetry_session: CompletionTelemetrySession | None = None,
    ) -> AsyncIterator[ProviderModel]:
        normalized_model = self.validate_model(model_id)
        normalized_key = _require_api_key(api_key, self.api_key_env)
        normalized_timeout = _validate_timeout(timeout_seconds)
        _validate_completion_session(
            completion_telemetry_session,
            provider_id=self.provider_id,
            api_surface=self.api_surface,
            transport_id=self.transport_id,
            adapter_version=self.adapter_version,
        )
        _validate_capture_canary(normalized_key, completion_telemetry_session)
        AsyncOpenAI, OpenAIResponsesModel, AsyncHTTPClient = (
            _load_responses_transport()
        )
        http_client = AsyncHTTPClient(
            timeout=normalized_timeout,
            follow_redirects=False,
            trust_env=False,
        )
        client = None
        try:
            client = AsyncOpenAI(
                api_key=normalized_key,
                base_url=_OPENAI_BASE_URL,
                timeout=normalized_timeout,
                max_retries=0,
                http_client=http_client,
            )
            sdk_model = _responses_model(
                OpenAIResponsesModel,
                model=normalized_model,
                openai_client=client,
                session=completion_telemetry_session,
                api_key=normalized_key,
            )
            yield ProviderModel(
                provider_id=self.provider_id,
                model_id=normalized_model,
                transport_id=self.transport_id,
                api_surface=self.api_surface,
                adapter_version=self.adapter_version,
                sdk_model=sdk_model,
                completion_telemetry_session=completion_telemetry_session,
            )
        finally:
            try:
                if client is not None:
                    await client.close()
            finally:
                await http_client.aclose()


@dataclass(frozen=True, slots=True)
class DeepSeekProvider:
    provider_id: ClassVar[str] = "deepseek"
    api_key_env: ClassVar[str] = "DEEPSEEK_API_KEY"
    transport_id: ClassVar[str] = "openai_compatible_responses"
    api_surface: ClassVar[str] = _RESPONSES_API_SURFACE
    adapter_version: ClassVar[str] = _DEEPSEEK_ADAPTER_VERSION
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
        self,
        *,
        model_id: str,
        api_key: str,
        timeout_seconds: float = 120.0,
        completion_telemetry_session: CompletionTelemetrySession | None = None,
    ) -> AsyncIterator[ProviderModel]:
        normalized_model = self.validate_model(model_id)
        normalized_key = _require_api_key(api_key, self.api_key_env)
        normalized_timeout = _validate_timeout(timeout_seconds)
        _validate_completion_session(
            completion_telemetry_session,
            provider_id=self.provider_id,
            api_surface=self.api_surface,
            transport_id=self.transport_id,
            adapter_version=self.adapter_version,
        )
        _validate_capture_canary(normalized_key, completion_telemetry_session)
        AsyncOpenAI, OpenAIResponsesModel, AsyncHTTPClient = (
            _load_responses_transport()
        )
        client_options: dict[str, object] = {
            "timeout": normalized_timeout,
            "follow_redirects": False,
            "trust_env": False,
        }
        transport_observer = getattr(
            completion_telemetry_session,
            "observe_deepseek_transport_send",
            None,
        )
        arm_transport_observer = getattr(
            completion_telemetry_session,
            "arm_deepseek_transport_observation",
            None,
        )
        if transport_observer is not None or arm_transport_observer is not None:
            if not callable(transport_observer) or not callable(arm_transport_observer):
                raise ProviderConfigurationError(
                    "provider_completion_transport_observer_invalid",
                    "DeepSeek validation transport observer 无效。",
                )
            client_options["event_hooks"] = {"request": [transport_observer]}
        http_client = AsyncHTTPClient(
            **client_options,
        )
        if callable(arm_transport_observer):
            try:
                arm_transport_observer()
            except BaseException:
                await _close_deepseek_transport_resources(None, http_client)
                raise
        client = None
        try:
            client = AsyncOpenAI(
                api_key=normalized_key,
                admin_api_key="",
                organization="",
                project="",
                webhook_secret="",
                base_url=self.base_url,
                timeout=normalized_timeout,
                max_retries=0,
                default_headers={},
                http_client=http_client,
            )
            _isolate_openai_compatible_client(client)
            sdk_model = _responses_model(
                OpenAIResponsesModel,
                model=normalized_model,
                openai_client=client,
                session=completion_telemetry_session,
                api_key=normalized_key,
            )
            yield ProviderModel(
                provider_id=self.provider_id,
                model_id=normalized_model,
                transport_id=self.transport_id,
                api_surface=self.api_surface,
                adapter_version=self.adapter_version,
                sdk_model=sdk_model,
                completion_telemetry_session=completion_telemetry_session,
            )
        finally:
            await _close_deepseek_transport_resources(client, http_client)


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
    api_surface: ClassVar[str] = _ANTHROPIC_API_SURFACE
    adapter_version: ClassVar[str] = _ANTHROPIC_ADAPTER_VERSION

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
        self,
        *,
        model_id: str,
        api_key: str,
        timeout_seconds: float = 120.0,
        completion_telemetry_session: CompletionTelemetrySession | None = None,
        _authorization: object | None = None,
    ) -> AsyncIterator[ProviderModel]:
        if not _anthropic_online_transport_authorized(_authorization):
            raise ProviderConfigurationError(
                ANTHROPIC_GENERIC_ONLINE_DISABLED_CODE,
                "Anthropic online transport 未获同进程受控 pilot capability。",
            )
        normalized_model = self.validate_model(model_id)
        normalized_key = _require_api_key(api_key, self.api_key_env)
        normalized_timeout = _validate_timeout(timeout_seconds)
        _validate_completion_session(
            completion_telemetry_session,
            provider_id=self.provider_id,
            api_surface=self.api_surface,
            transport_id=self.transport_id,
            adapter_version=self.adapter_version,
        )
        _validate_capture_canary(normalized_key, completion_telemetry_session)
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
            owned_handler = _anthropic_http_handler(
                AsyncHTTPHandler,
                timeout_seconds=normalized_timeout,
                session=completion_telemetry_session,
                api_key=normalized_key,
            )

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
                    if completion_telemetry_session is None:
                        raise ProviderConfigurationError(
                            "provider_completion_telemetry_session_required",
                            "Anthropic 调用缺少 case-scoped completion telemetry session。",
                        )
                    handle = (
                        _begin_completion_attempt(completion_telemetry_session)
                        if completion_telemetry_session is not None
                        else None
                    )
                    if completion_telemetry_session is not None and stream:
                        _finalize_completion_attempt(
                            completion_telemetry_session,
                            "finalize_no_response",
                            handle,
                            "provider_completion_stream_unsupported",
                        )
                        raise ProviderConfigurationError(
                            "provider_completion_stream_unsupported",
                            "启用 completion telemetry 时不支持流式 Anthropic 调用。",
                        )
                    if completion_telemetry_session is not None:
                        set_attempt = getattr(
                            owned_handler,
                            "set_completion_attempt",
                            None,
                        )
                        if not callable(set_attempt):
                            _finalize_completion_attempt(
                                completion_telemetry_session,
                                "finalize_no_response",
                                handle,
                                "provider_completion_capture_failed",
                            )
                            raise ProviderConfigurationError(
                                "provider_completion_capture_failed",
                                "Anthropic completion capture handler 无效。",
                            )
                        try:
                            set_attempt(
                                handle, _requested_output_cap(model_settings)
                            )
                        except Exception:
                            _finalize_completion_attempt(
                                completion_telemetry_session,
                                "finalize_no_response",
                                handle,
                                "provider_completion_capture_failed",
                            )
                            raise ProviderConfigurationError(
                                "provider_completion_capture_failed",
                                "Anthropic completion attempt 绑定失败。",
                            ) from None
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
                    try:
                        result = await super()._fetch_response(
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
                        if completion_telemetry_session is not None:
                            finalize_success = getattr(
                                owned_handler, "finalize_transform_success", None
                            )
                            if not callable(finalize_success):
                                if not _anthropic_attempt_finalized(
                                    owned_handler, handle
                                ):
                                    _finalize_completion_attempt(
                                        completion_telemetry_session,
                                        "finalize_no_response",
                                        handle,
                                        "provider_completion_capture_failed",
                                    )
                                raise ProviderConfigurationError(
                                    "provider_completion_capture_failed",
                                    "Anthropic completion capture handler 无有效终结接口。",
                                )
                            await finalize_success(handle)
                        return result
                    except asyncio.CancelledError:
                        if completion_telemetry_session is not None:
                            finalize_failure = getattr(
                                owned_handler, "finalize_transform_failure", None
                            )
                            if callable(finalize_failure):
                                await finalize_failure(handle, cancelled=True)
                            elif not _anthropic_attempt_finalized(
                                owned_handler, handle
                            ):
                                _finalize_completion_attempt(
                                    completion_telemetry_session,
                                    "finalize_cancelled",
                                    handle,
                                )
                        raise
                    except ProviderConfigurationError as exc:
                        if completion_telemetry_session is None:
                            raise
                        if _anthropic_attempt_finalized(owned_handler, handle):
                            raise
                        finalize_failure = getattr(
                            owned_handler, "finalize_transform_failure", None
                        )
                        if callable(finalize_failure):
                            code = await finalize_failure(handle, cancelled=False)
                        else:
                            code = "provider_completion_capture_failed"
                            _finalize_completion_attempt(
                                completion_telemetry_session,
                                "finalize_no_response",
                                handle,
                                code,
                            )
                        raise ProviderConfigurationError(
                            code,
                            "Anthropic completion transform 后元数据终结失败。",
                        ) from None
                    except Exception:
                        if completion_telemetry_session is None:
                            raise
                        if _anthropic_attempt_finalized(owned_handler, handle):
                            raise ProviderConfigurationError(
                                "provider_completion_capture_failed",
                                "Anthropic completion transport 已以稳定终态失败。",
                            ) from None
                        finalize_failure = getattr(
                            owned_handler, "finalize_transform_failure", None
                        )
                        if callable(finalize_failure):
                            code = await finalize_failure(handle, cancelled=False)
                        else:
                            code = "provider_completion_capture_failed"
                            _finalize_completion_attempt(
                                completion_telemetry_session,
                                "finalize_no_response",
                                handle,
                                code,
                            )
                        raise ProviderConfigurationError(
                            code,
                            "Anthropic completion capture 未产生可接受响应记录。",
                        ) from None

            sdk_model = ControlledAnthropicLitellmModel(
                model=f"anthropic/{normalized_model}",
                base_url=_ANTHROPIC_BASE_URL,
                api_key=normalized_key,
            )
            yield ProviderModel(
                provider_id=self.provider_id,
                model_id=normalized_model,
                transport_id=self.transport_id,
                api_surface=self.api_surface,
                adapter_version=self.adapter_version,
                sdk_model=sdk_model,
                completion_telemetry_session=completion_telemetry_session,
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


def _validate_completion_session(
    session: CompletionTelemetrySession | None,
    *,
    provider_id: str,
    api_surface: str,
    transport_id: str,
    adapter_version: str,
) -> None:
    if session is None:
        return
    try:
        # Delayed import avoids making provider discovery import the audit stack.
        # Every non-null session on a Provider entrypoint is nevertheless the exact
        # append-only ledger bridge; test-only duck sessions must exercise a lower
        # layer or construct a real temporary ledger.
        from .completion_telemetry_ledger import LedgerCompletionTelemetrySession

        exact_session_type = type(session) is LedgerCompletionTelemetrySession
        if not exact_session_type:
            from .deepseek_completion_first_live_validation import (
                _DeepSeekFirstLiveValidationLedgerSession,
            )

            exact_session_type = (
                provider_id == "deepseek"
                and type(session) is _DeepSeekFirstLiveValidationLedgerSession
            )
        if not exact_session_type:
            raise TypeError("live completion session must be an exact ledger bridge")
        session.assert_provider_telemetry_authority()
        valid = (
            getattr(session, "provider_id", None) == provider_id
            and getattr(session, "api_surface", None) == api_surface
            and getattr(session, "transport_id", None) == transport_id
            and getattr(session, "adapter_version", None) == adapter_version
            and all(
                callable(getattr(session, method_name, None))
                for method_name in (
                    "begin_attempt",
                    "finalize_response_accepted",
                    "finalize_response_rejected",
                    "finalize_http_error",
                    "finalize_no_response",
                    "finalize_cancelled",
                    "finalize_outcome_unknown",
                )
            )
        )
    except Exception:
        valid = False
    if not valid:
        raise ProviderConfigurationError(
            "provider_completion_session_binding_mismatch",
            "Completion telemetry session 与 Provider 绑定不一致。",
        )


def _validate_capture_canary(
    api_key: str, session: CompletionTelemetrySession | None
) -> None:
    if session is None:
        return
    try:
        sanitize_completion_capture({}, sensitive_canaries=(api_key,))
    except Exception:
        raise ProviderConfigurationError(
            "provider_completion_canary_invalid",
            "Provider credential 无法安全绑定到 completion 脱敏扫描。",
        ) from None


def _isolate_openai_compatible_client(client: object) -> None:
    """Remove OpenAI-only account/header state before a DeepSeek request can run."""

    try:
        # AsyncOpenAI reads these values (and OPENAI_CUSTOM_HEADERS) from the
        # process environment during construction.  They are valid for the
        # official OpenAI client, but must never cross the DeepSeek boundary.
        setattr(client, "organization", None)
        setattr(client, "project", None)
        setattr(client, "admin_api_key", None)
        setattr(client, "webhook_secret", None)
        setattr(client, "_custom_headers", {})
        isolated = (
            getattr(client, "organization", object()) is None
            and getattr(client, "project", object()) is None
            and getattr(client, "admin_api_key", object()) is None
            and getattr(client, "webhook_secret", object()) is None
            and getattr(client, "_custom_headers", object()) == {}
        )
    except Exception:
        isolated = False
    if not isolated:
        raise ProviderConfigurationError(
            "provider_client_environment_isolation_failed",
            "DeepSeek client 无法隔离 OpenAI account/header 环境状态。",
        )


def _field_present(value: object, field_name: str) -> bool:
    if isinstance(value, Mapping):
        return field_name in value
    fields_set = getattr(value, "model_fields_set", None)
    if isinstance(fields_set, (set, frozenset)):
        return field_name in fields_set
    return hasattr(value, field_name)


def _field_value(value: object, field_name: str) -> object:
    if isinstance(value, Mapping):
        return value[field_name]
    return getattr(value, field_name)


def _plain_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        result = model_dump(mode="python", exclude_unset=True)
        if isinstance(result, Mapping):
            return dict(result)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return dict(result)
    raise ProviderConfigurationError(
        "provider_completion_capture_failed",
        "Provider completion metadata 不是受支持的对象。",
    )


def _optional_counter(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _nested_counter(value: Mapping[str, Any], *path: str) -> int | None:
    current: object = value
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return _optional_counter(current)


def _responses_normalized_usage(raw_usage: Mapping[str, Any]) -> dict[str, int | None]:
    return {
        "requests": 1,
        "input_tokens": _optional_counter(raw_usage.get("input_tokens")),
        "output_tokens": _optional_counter(raw_usage.get("output_tokens")),
        "total_tokens": _optional_counter(raw_usage.get("total_tokens")),
        "cached_input_tokens": _nested_counter(
            raw_usage, "input_tokens_details", "cached_tokens"
        ),
        "cache_write_tokens": _nested_counter(
            raw_usage, "input_tokens_details", "cache_write_tokens"
        ),
        "reasoning_tokens": _nested_counter(
            raw_usage, "output_tokens_details", "reasoning_tokens"
        ),
    }


def _anthropic_normalized_usage(raw_usage: Mapping[str, Any]) -> dict[str, int | None]:
    input_tokens = _optional_counter(raw_usage.get("input_tokens"))
    output_tokens = _optional_counter(raw_usage.get("output_tokens"))
    total_tokens = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    return {
        "requests": 1,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": _optional_counter(
            raw_usage.get("cache_read_input_tokens")
        ),
        "cache_write_tokens": _optional_counter(
            raw_usage.get("cache_creation_input_tokens")
        ),
        "reasoning_tokens": None,
    }


def _requested_output_cap(model_settings: object) -> int | None:
    value = getattr(model_settings, "max_tokens", None)
    return value if type(value) is int and value >= 0 else None


def _exception_http_status(error: BaseException) -> int | None:
    direct = getattr(error, "status_code", None)
    if type(direct) is int:
        return direct
    response = getattr(error, "response", None)
    nested = getattr(response, "status_code", None)
    return nested if type(nested) is int else None


def _responses_capture(
    response: object,
    raw_wrapper: object,
    model_settings: object,
    *,
    api_key: str,
) -> SanitizedCompletionCapture:
    raw_capture: dict[str, Any] = {}
    for field_name in ("status", "incomplete_details", "usage"):
        if not _field_present(response, field_name):
            continue
        child = _field_value(response, field_name)
        if field_name in {"incomplete_details", "usage"} and child is not None:
            child = _plain_mapping(child)
        raw_capture[field_name] = child
    request_id = getattr(raw_wrapper, "request_id", None)
    if request_id is not None:
        raw_capture["provider_request_id"] = request_id
    http_status = getattr(raw_wrapper, "status_code", None)
    if http_status is not None:
        raw_capture["http_status"] = http_status
    cap = _requested_output_cap(model_settings)
    if cap is not None:
        raw_capture["requested_output_token_cap"] = cap
    raw_usage = raw_capture.get("usage")
    normalized_usage = (
        _responses_normalized_usage(raw_usage)
        if isinstance(raw_usage, Mapping)
        else None
    )
    try:
        return sanitize_completion_capture(
            raw_capture,
            normalized_usage=normalized_usage,
            sensitive_canaries=(api_key,),
        )
    finally:
        raw_capture.clear()
        normalized_usage = None


def _anthropic_capture(
    response: object,
    *,
    requested_output_cap: int | None,
    api_key: str,
) -> SanitizedCompletionCapture:
    try:
        payload = response.json()
    except Exception:
        raise ProviderConfigurationError(
            "provider_completion_capture_failed",
            "Anthropic completion metadata 无法解析。",
        ) from None
    if not isinstance(payload, Mapping):
        raise ProviderConfigurationError(
            "provider_completion_capture_failed",
            "Anthropic completion metadata 不是对象。",
        )
    raw_capture: dict[str, Any] = {}
    for field_name in ("stop_reason", "stop_sequence", "usage"):
        if field_name not in payload:
            continue
        child = payload[field_name]
        if field_name == "usage" and child is not None:
            child = _plain_mapping(child)
        raw_capture[field_name] = child
    headers = getattr(response, "headers", None)
    header_get = getattr(headers, "get", None)
    if callable(header_get):
        request_id = header_get("request-id")
        if request_id is not None:
            raw_capture["provider_request_id"] = request_id
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        raw_capture["http_status"] = status_code
    if requested_output_cap is not None:
        raw_capture["requested_output_token_cap"] = requested_output_cap
    raw_usage = raw_capture.get("usage")
    normalized_usage = (
        _anthropic_normalized_usage(raw_usage)
        if isinstance(raw_usage, Mapping)
        else None
    )
    try:
        return sanitize_completion_capture(
            raw_capture,
            normalized_usage=normalized_usage,
            sensitive_canaries=(api_key,),
        )
    finally:
        payload = None
        raw_capture = {}


async def _close_response(value: object) -> None:
    close = getattr(value, "aclose", None)
    if not callable(close):
        close = getattr(value, "close", None)
    if not callable(close):
        raise ProviderConfigurationError(
            "provider_completion_raw_cleanup_failed",
            "Provider raw response 缺少清理接口。",
        )
    try:
        result = close()
        if inspect.isawaitable(result):
            async with asyncio.timeout(_RAW_RESPONSE_CLEANUP_TIMEOUT_SECONDS):
                await result
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ProviderConfigurationError(
            "provider_completion_raw_cleanup_failed",
            "Provider raw response 清理失败或超时。",
        ) from None


async def _close_deepseek_transport_resource(
    value: object,
    *,
    method_name: str,
) -> None:
    close = getattr(value, method_name, None)
    if not callable(close):
        raise ProviderConfigurationError(
            "provider_completion_post_request_cleanup_failed",
            "DeepSeek Provider transport 资源缺少清理接口。",
        )
    try:
        result = close()
        if inspect.isawaitable(result):
            async with asyncio.timeout(
                _DEEPSEEK_POST_REQUEST_CLEANUP_TIMEOUT_SECONDS
            ):
                await result
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ProviderConfigurationError(
            "provider_completion_post_request_cleanup_failed",
            "DeepSeek Provider transport 资源清理失败或超时。",
        ) from None


async def _close_deepseek_transport_resources(
    client: object | None,
    http_client: object,
) -> None:
    cleanup_error: ProviderConfigurationError | None = None
    cancelled: asyncio.CancelledError | None = None
    resources = (
        ((client, "close"),) if client is not None else ()
    ) + ((http_client, "aclose"),)
    for resource, method_name in resources:
        try:
            await _close_deepseek_transport_resource(
                resource,
                method_name=method_name,
            )
        except asyncio.CancelledError as exc:
            if cancelled is None:
                cancelled = exc
        except ProviderConfigurationError as exc:
            if cleanup_error is None:
                cleanup_error = exc
    if cancelled is not None:
        raise cancelled
    if cleanup_error is not None:
        raise cleanup_error


def _response_is_closed(value: object) -> bool:
    for field_name in ("is_closed", "closed"):
        state = getattr(value, field_name, None)
        if type(state) is bool and state:
            return True
    return False


async def _parse_raw_response(value: object) -> object:
    parse = getattr(value, "parse", None)
    if not callable(parse):
        raise ProviderConfigurationError(
            "provider_completion_capture_failed",
            "Provider raw response 缺少 typed parse 接口。",
        )
    result = parse()
    return await result if inspect.isawaitable(result) else result


def _begin_completion_attempt(session: CompletionTelemetrySession) -> object:
    try:
        handle = session.begin_attempt()
    except Exception:
        raise ProviderConfigurationError(
            "provider_completion_attempt_begin_failed",
            "Provider completion attempt 无法在网络前登记。",
        ) from None
    if handle is None:
        raise ProviderConfigurationError(
            "provider_completion_attempt_begin_failed",
            "Provider completion attempt handle 不能为空。",
        )
    return handle


def _finalize_completion_attempt(
    session: CompletionTelemetrySession,
    method_name: str,
    handle: object,
    *args: object,
) -> None:
    try:
        method = getattr(session, method_name)
        method(handle, *args)
    except Exception:
        raise ProviderConfigurationError(
            "provider_completion_attempt_finalize_failed",
            "Provider completion attempt 无法进入唯一终态。",
        ) from None


def _anthropic_attempt_finalized(handler: object, handle: object) -> bool:
    try:
        check = getattr(handler, "completion_attempt_finalized", None)
        return bool(callable(check) and check(handle))
    except Exception:
        return False


def _responses_model(
    model_class: type,
    *,
    model: str,
    openai_client: object,
    session: CompletionTelemetrySession | None,
    api_key: str,
) -> object:
    class ControlledResponsesModel(model_class):
        async def _fetch_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            previous_response_id=None,
            conversation_id=None,
            stream=False,
            prompt=None,
        ):
            if session is None:
                raise ProviderConfigurationError(
                    "provider_completion_telemetry_session_required",
                    "Provider Responses 调用缺少 case-scoped completion telemetry session。",
                )
            handle = _begin_completion_attempt(session)
            if stream:
                _finalize_completion_attempt(
                    session,
                    "finalize_no_response",
                    handle,
                    "provider_completion_stream_unsupported",
                )
                raise ProviderConfigurationError(
                    "provider_completion_stream_unsupported",
                    "启用 completion telemetry 时不支持流式 Responses 调用。",
                )
            raw_wrapper = None
            response = None
            capture = None
            primary_error: Exception | None = None
            cancelled: asyncio.CancelledError | None = None
            cleanup_failed = False
            network_started = False
            response_received = False
            try:
                create_kwargs = self._build_response_create_kwargs(
                    system_instructions=system_instructions,
                    input=input,
                    model_settings=model_settings,
                    tools=tools,
                    output_schema=output_schema,
                    handoffs=handoffs,
                    previous_response_id=previous_response_id,
                    conversation_id=conversation_id,
                    stream=False,
                    prompt=prompt,
                )
                client = self._get_client()
                network_started = True
                raw_wrapper = await client.responses.with_raw_response.create(
                    **create_kwargs
                )
                response_received = True
                response = await _parse_raw_response(raw_wrapper)
                capture = _responses_capture(
                    response,
                    raw_wrapper,
                    model_settings,
                    api_key=api_key,
                )
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception as exc:
                primary_error = exc
            finally:
                if raw_wrapper is not None:
                    try:
                        await _close_response(
                            getattr(raw_wrapper, "http_response", raw_wrapper)
                        )
                    except asyncio.CancelledError as exc:
                        if cancelled is None:
                            cancelled = exc
                    except Exception:
                        cleanup_failed = True
                raw_wrapper = None

            if cancelled is not None or primary_error is not None or cleanup_failed:
                code = (
                    "provider_completion_raw_cleanup_failed"
                    if cleanup_failed
                    else "provider_completion_capture_failed"
                )
                if cancelled is not None and response_received:
                    terminal_method = "finalize_response_rejected"
                    terminal_args = ("provider_completion_capture_failed",)
                elif cancelled is not None:
                    terminal_method = "finalize_cancelled"
                    terminal_args: tuple[object, ...] = ()
                elif cleanup_failed or response_received:
                    terminal_method = "finalize_response_rejected"
                    terminal_args = (code,)
                elif (
                    primary_error is not None
                    and _exception_http_status(primary_error) is not None
                ):
                    terminal_method = "finalize_http_error"
                    terminal_args = ("provider_completion_http_error",)
                    code = "provider_completion_http_error"
                elif network_started:
                    terminal_method = "finalize_outcome_unknown"
                    code = "provider_completion_outcome_unknown"
                    terminal_args = (code,)
                else:
                    terminal_method = "finalize_no_response"
                    code = "provider_completion_no_response"
                    terminal_args = (code,)
                _finalize_completion_attempt(
                    session, terminal_method, handle, *terminal_args
                )
                response = None
                capture = None
                primary_error = None
                if cancelled is not None:
                    raise cancelled
                raise ProviderConfigurationError(
                    code, "Provider completion metadata 捕获失败。"
                ) from None
            if response is None or type(capture) is not SanitizedCompletionCapture:
                _finalize_completion_attempt(
                    session,
                    "finalize_response_rejected",
                    handle,
                    "provider_completion_capture_failed",
                )
                raise ProviderConfigurationError(
                    "provider_completion_capture_failed",
                    "Provider completion metadata 捕获失败。",
                )
            try:
                _finalize_completion_attempt(
                    session, "finalize_response_accepted", handle, capture
                )
            except Exception:
                raise
            return response

    return ControlledResponsesModel(model=model, openai_client=openai_client)


def _anthropic_http_handler(
    handler_class: type,
    *,
    timeout_seconds: float,
    session: CompletionTelemetrySession | None,
    api_key: str,
) -> object:
    class ControlledAnthropicHTTPHandler(handler_class):
        def create_client(
            self,
            timeout,
            event_hooks,
            ssl_verify=None,
            shared_session=None,
        ):
            if shared_session is not None:
                raise ProviderConfigurationError(
                    "provider_completion_http_client_invalid",
                    "Anthropic completion telemetry 不接受共享 HTTP session。",
                )
            try:
                import httpx

                return httpx.AsyncClient(
                    timeout=timeout,
                    event_hooks=event_hooks,
                    verify=True if ssl_verify is None else ssl_verify,
                    follow_redirects=False,
                    trust_env=False,
                )
            except Exception:
                raise ProviderConfigurationError(
                    "provider_completion_http_client_invalid",
                    "Anthropic owned HTTP client 构造失败。",
                ) from None

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._completion_requested_output_cap = None
            self._completion_attempt_handle = None
            self._completion_attempt_finalized = False
            self._completion_response_observed = False
            self._completion_pending_capture = None
            self._completion_pending_response = None
            self._completion_terminal_code = None

        def set_completion_attempt(
            self, handle: object, requested_output_cap: int | None
        ) -> None:
            if (
                self._completion_attempt_handle is not None
                and not self._completion_attempt_finalized
            ):
                raise ProviderConfigurationError(
                    "provider_completion_attempt_begin_failed",
                    "Anthropic HTTP handler 尚有未终结 attempt。",
                )
            self._completion_attempt_handle = handle
            self._completion_requested_output_cap = requested_output_cap
            self._completion_attempt_finalized = False
            self._completion_response_observed = False
            self._completion_pending_capture = None
            self._completion_pending_response = None
            self._completion_terminal_code = None

        def completion_attempt_finalized(self, handle: object) -> bool:
            return (
                self._completion_attempt_handle is handle
                and self._completion_attempt_finalized
            )

        def _require_active_handle(self, handle: object | None = None) -> object:
            active = self._completion_attempt_handle
            if (
                active is None
                or self._completion_attempt_finalized
                or (handle is not None and active is not handle)
            ):
                raise ProviderConfigurationError(
                    "provider_completion_attempt_begin_failed",
                    "Anthropic HTTP 请求缺少预先登记的 attempt。",
                )
            return active

        def _terminalize(
            self,
            method_name: str,
            handle: object,
            *args: object,
        ) -> None:
            self._completion_terminal_code = (
                args[0] if args and isinstance(args[0], str) else method_name
            )
            try:
                _finalize_completion_attempt(session, method_name, handle, *args)
            finally:
                # A sink failure poisons the campaign and must never trigger a
                # second terminal attempt against a possibly-mutated tracker.
                self._completion_attempt_finalized = True
                self._completion_pending_capture = None
                self._completion_pending_response = None

        async def _close_pending_response(self) -> None:
            response = self._completion_pending_response
            if response is not None and not _response_is_closed(response):
                await _close_response(response)

        async def finalize_transform_success(self, handle: object) -> None:
            self._require_active_handle(handle)
            capture = self._completion_pending_capture
            if (
                not self._completion_response_observed
                or type(capture) is not SanitizedCompletionCapture
            ):
                self._terminalize(
                    "finalize_no_response",
                    handle,
                    "provider_completion_no_response",
                )
                raise ProviderConfigurationError(
                    "provider_completion_no_response",
                    "Anthropic transform 返回但未观察到原生 HTTP 响应。",
                )
            try:
                await self._close_pending_response()
            except asyncio.CancelledError:
                self._terminalize(
                    "finalize_response_rejected",
                    handle,
                    "provider_completion_raw_cleanup_failed",
                )
                raise
            except Exception:
                self._terminalize(
                    "finalize_response_rejected",
                    handle,
                    "provider_completion_raw_cleanup_failed",
                )
                raise ProviderConfigurationError(
                    "provider_completion_raw_cleanup_failed",
                    "Anthropic 原生响应清理失败。",
                ) from None
            self._terminalize(
                "finalize_response_accepted", handle, capture
            )

        async def finalize_transform_failure(
            self, handle: object, *, cancelled: bool
        ) -> str:
            if self.completion_attempt_finalized(handle):
                return str(
                    self._completion_terminal_code
                    or "provider_completion_capture_failed"
                )
            self._require_active_handle(handle)
            if self._completion_response_observed:
                code = "provider_completion_transform_failed"
                try:
                    await self._close_pending_response()
                except BaseException:
                    code = "provider_completion_raw_cleanup_failed"
                self._terminalize(
                    "finalize_response_rejected", handle, code
                )
                return code
            if cancelled:
                self._terminalize("finalize_cancelled", handle)
                return "provider_completion_cancelled"
            code = "provider_completion_no_response"
            self._terminalize("finalize_no_response", handle, code)
            return code

        async def post(
            self,
            url: str,
            data=None,
            json=None,
            params=None,
            headers=None,
            timeout=None,
            stream: bool = False,
            logging_obj=None,
            files=None,
            content=None,
        ):
            del logging_obj
            if session is None:
                raise ProviderConfigurationError(
                    "provider_completion_telemetry_session_required",
                    "Anthropic HTTP 调用缺少 case-scoped completion telemetry session。",
                )
            handle = self._require_active_handle()
            if (
                self._completion_response_observed
                or self._completion_pending_response is not None
                or self._completion_pending_capture is not None
            ):
                raise ProviderConfigurationError(
                    "provider_completion_duplicate_transport_send",
                    "Anthropic attempt 已观察到一次 HTTP 响应；禁止第二次发送。",
                )
            response = None
            send_started = False
            try:
                request_data = data
                request_content = content
                if isinstance(data, (bytes, str)):
                    request_data = None
                    if content is None:
                        request_content = data
                if timeout is None:
                    timeout = self.timeout
                request = self.client.build_request(
                    "POST",
                    url,
                    data=request_data,
                    json=json,
                    params=params,
                    headers=headers,
                    timeout=timeout,
                    files=files,
                    content=request_content,
                )
                send_started = True
                response = await self.client.send(request, stream=stream)
                self._completion_response_observed = True
                self._completion_pending_response = response
                response.raise_for_status()
            except asyncio.CancelledError:
                if response is not None:
                    self._completion_pending_response = response
                    await self.finalize_transform_failure(
                        handle, cancelled=True
                    )
                else:
                    self._terminalize("finalize_cancelled", handle)
                raise
            except Exception as exc:
                cleanup_failed = False
                if response is not None:
                    try:
                        await _close_response(response)
                    except asyncio.CancelledError:
                        self._terminalize(
                            "finalize_response_rejected",
                            handle,
                            "provider_completion_raw_cleanup_failed",
                        )
                        raise
                    except Exception:
                        cleanup_failed = True
                if cleanup_failed:
                    method = "finalize_response_rejected"
                    code = "provider_completion_raw_cleanup_failed"
                elif _exception_http_status(exc) is not None:
                    method = "finalize_http_error"
                    code = "provider_completion_http_error"
                elif send_started:
                    method = "finalize_outcome_unknown"
                    code = "provider_completion_outcome_unknown"
                else:
                    method = "finalize_no_response"
                    code = "provider_completion_no_response"
                self._terminalize(method, handle, code)
                raise ProviderConfigurationError(
                    code,
                    "Anthropic response 在原生元数据捕获前失败。",
                ) from None

            self._completion_pending_response = response
            try:
                capture = _anthropic_capture(
                    response,
                    requested_output_cap=self._completion_requested_output_cap,
                    api_key=api_key,
                )
                self._completion_pending_capture = capture
            except asyncio.CancelledError:
                try:
                    await _close_response(response)
                finally:
                    self._terminalize(
                        "finalize_response_rejected",
                        handle,
                        "provider_completion_capture_failed",
                    )
                raise
            except Exception:
                cleanup_failed = False
                try:
                    await _close_response(response)
                except Exception:
                    cleanup_failed = True
                code = (
                    "provider_completion_raw_cleanup_failed"
                    if cleanup_failed
                    else "provider_completion_capture_failed"
                )
                self._terminalize(
                    "finalize_response_rejected", handle, code
                )
                raise ProviderConfigurationError(
                    code, "Anthropic 原生 completion metadata 捕获失败。"
                ) from None
            return response

    return ControlledAnthropicHTTPHandler(timeout=timeout_seconds)


def _load_responses_transport() -> tuple[Any, Any, Any]:
    """Delay optional SDK imports so status checks stay side-effect free."""

    try:
        from agents import OpenAIResponsesModel
        from openai import AsyncOpenAI, DefaultAsyncHttpxClient
    except ImportError as exc:
        raise ProviderConfigurationError(
            "provider_sdk_not_installed", "未安装 OpenAI Agents SDK provider transport。"
        ) from exc
    return AsyncOpenAI, OpenAIResponsesModel, DefaultAsyncHttpxClient


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

    environment_name = "LITELLM_LOCAL_MODEL_COST_MAP"
    environment_was_present = environment_name in os.environ
    previous_value = os.environ.get(environment_name)
    os.environ[environment_name] = "True"
    try:
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
    finally:
        if environment_was_present:
            assert previous_value is not None
            os.environ[environment_name] = previous_value
        else:
            os.environ.pop(environment_name, None)
    return LitellmModel, AsyncHTTPHandler, litellm
