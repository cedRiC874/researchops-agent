from __future__ import annotations

import asyncio
import hashlib
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import researchops_completion_telemetry.surface_mapping as surface_mapping
from researchops.audit import AuditLedger
from researchops.completion_telemetry_ledger import LedgerCompletionTelemetrySession
from researchops.model_providers import (
    AnthropicProvider,
    DeepSeekProvider,
    OpenAIProvider,
    ProviderConfigurationError,
)
from researchops import model_providers as provider_module
from researchops_completion_telemetry.sanitization import SanitizedCompletionCapture
from researchops_completion_telemetry.capture import CompletionTelemetryCollector
from researchops_completion_telemetry.surface_mapping import (
    load_and_select_surface_mapping,
)
from tests.test_completion_telemetry_ledger import _plan_binding


ROOT = Path(__file__).resolve().parents[1]
_TEMPORARY_DIRECTORIES: list[tempfile.TemporaryDirectory] = []


def _anthropic_test_authorization():
    return object()


class _AttemptHandle:
    def __init__(self, index: int) -> None:
        self.index = index


class _InMemorySession:
    def __init__(
        self,
        provider_id: str,
        api_surface: str,
        transport_id: str,
        adapter_version: str,
        log: list[str],
    ) -> None:
        self.provider_id = provider_id
        self.api_surface = api_surface
        self.transport_id = transport_id
        self.adapter_version = adapter_version
        self.log = log
        self.active: _AttemptHandle | None = None
        self.terminal: list[tuple[str, object]] = []
        self.captures: list[SanitizedCompletionCapture] = []

    def begin_attempt(self) -> _AttemptHandle:
        if self.active is not None:
            raise AssertionError("attempt already active")
        handle = _AttemptHandle(len(self.terminal))
        self.active = handle
        self.log.append("begin_attempt")
        return handle

    def _finish(self, handle: object, kind: str, value: object = None) -> None:
        if handle is not self.active:
            raise AssertionError("wrong or already finalized handle")
        self.terminal.append((kind, value))
        self.active = None
        self.log.append(kind)

    def finalize_response_accepted(
        self, handle: object, capture: SanitizedCompletionCapture
    ) -> None:
        if type(capture) is not SanitizedCompletionCapture:
            raise AssertionError("capture must be sanitizer-owned")
        self.captures.append(capture._collector_snapshot())
        self._finish(handle, "response_accepted")

    def finalize_response_rejected(self, handle: object, error_code: str) -> None:
        self._finish(handle, "response_rejected", error_code)

    def finalize_http_error(self, handle: object, error_code: str) -> None:
        self._finish(handle, "http_error", error_code)

    def finalize_no_response(self, handle: object, error_code: str) -> None:
        self._finish(handle, "no_response", error_code)

    def finalize_cancelled(self, handle: object) -> None:
        self._finish(handle, "cancelled")

    def finalize_outcome_unknown(self, handle: object, error_code: str) -> None:
        self._finish(handle, "outcome_unknown", error_code)


class _FakeRawWrapper:
    def __init__(
        self,
        response: object,
        *,
        request_id: str | None = "req_test_1",
        status_code: int = 200,
        parse_error: Exception | None = None,
        close_error: Exception | None = None,
        log: list[str] | None = None,
    ) -> None:
        self.response = response
        self.request_id = request_id
        self.status_code = status_code
        self.parse_error = parse_error
        self.close_error = close_error
        self.log = log
        self.parse_called = False
        self.close_called = False

    def parse(self):
        self.parse_called = True
        if self.parse_error is not None:
            raise self.parse_error
        return self.response

    async def close(self) -> None:
        self.close_called = True
        if self.log is not None:
            self.log.append("raw_response_closed")
        if self.close_error is not None:
            raise self.close_error


class _FakeWithRawResponse:
    def __init__(self, owner: "_FakeAsyncOpenAI") -> None:
        self.owner = owner

    async def create(self, **kwargs):
        self.owner.log.append("network_create")
        self.owner.create_kwargs.append(kwargs)
        outcome = self.owner.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeResponsesResource:
    def __init__(self, owner: "_FakeAsyncOpenAI") -> None:
        self.with_raw_response = _FakeWithRawResponse(owner)


class _FakeAsyncOpenAI:
    configured_outcomes: list[object] = []
    configured_log: list[str] = []
    instances: list["_FakeAsyncOpenAI"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.outcomes = list(self.configured_outcomes)
        self.log = self.configured_log
        self.create_kwargs: list[dict[str, object]] = []
        self.responses = _FakeResponsesResource(self)
        self.closed = False
        self.__class__.instances.append(self)

    async def close(self) -> None:
        self.closed = True


class _FakeResponsesModel:
    def __init__(self, *, model: str, openai_client: _FakeAsyncOpenAI) -> None:
        self.model = model
        self._client = openai_client

    def _get_client(self) -> _FakeAsyncOpenAI:
        return self._client

    def _build_response_create_kwargs(self, **kwargs):
        return {
            "model": self.model,
            "max_output_tokens": kwargs["model_settings"].max_tokens,
            "stream": kwargs["stream"],
        }


class _BuildFailResponsesModel(_FakeResponsesModel):
    def _build_response_create_kwargs(self, **kwargs):
        del kwargs
        raise RuntimeError("private pre-network details")


class _FakeHTTPError(RuntimeError):
    status_code = 429


class _FakeAnthropicResponse:
    def __init__(
        self,
        payload: dict[str, object],
        log: list[str],
        *,
        status_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.payload = payload
        self.log = log
        self.status_error = status_error
        self.close_error = close_error
        self.headers = {"request-id": "req_anthropic_test_1"}
        self.status_code = 200
        self.closed = False
        self.is_closed = False

    def json(self) -> dict[str, object]:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_error is not None:
            raise self.status_error

    async def aclose(self) -> None:
        self.closed = True
        self.is_closed = True
        self.log.append("raw_response_closed")
        if self.close_error is not None:
            raise self.close_error


class _FakeAnthropicClient:
    def __init__(self, owner: "_FakeAnthropicHTTPHandler") -> None:
        self.owner = owner
        self.send_count = 0

    def build_request(self, method: str, url: str, **kwargs):
        return {"method": method, "url": url, **kwargs}

    async def send(self, request: object, *, stream: bool):
        del request, stream
        self.send_count += 1
        self.owner.log.append("network_post")
        outcome = self.owner.responses.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeAnthropicHTTPHandler:
    configured_responses: list[object] = []
    configured_log: list[str] = []
    instances: list["_FakeAnthropicHTTPHandler"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.responses = list(self.configured_responses)
        self.log = self.configured_log
        self.client = _FakeAnthropicClient(self)
        self.timeout = kwargs.get("timeout")
        self.closed = False
        self.__class__.instances.append(self)

    async def close(self) -> None:
        self.closed = True


class _FakeAnthropicLitellmModel:
    instances: list["_FakeAnthropicLitellmModel"] = []

    def __init__(self, **kwargs) -> None:
        self.model = kwargs["model"]
        self.base_url = kwargs.get("base_url")
        self.api_key = kwargs.get("api_key")
        self.__class__.instances.append(self)

    async def _fetch_response(self, **kwargs):
        handler = kwargs["model_settings"].extra_args["client"]
        self.fetch_settings = kwargs["model_settings"]
        response = await handler.post("https://api.anthropic.com/v1/messages")
        handler.log.append("litellm_transform")
        payload = response.json()
        await response.aclose()
        return {
            "converted_finish_reason": payload.get("stop_reason"),
            "native_stop_sequence_discarded_by_conversion": True,
        }


class _FakeAnthropicPreHTTPFailureModel(_FakeAnthropicLitellmModel):
    async def _fetch_response(self, **kwargs):
        del kwargs
        raise ProviderConfigurationError(
            "provider_configuration_invalid", "safe pre-http failure"
        )


class _FakeAnthropicTransformFailureModel(_FakeAnthropicLitellmModel):
    async def _fetch_response(self, **kwargs):
        handler = kwargs["model_settings"].extra_args["client"]
        await handler.post("https://api.anthropic.com/v1/messages")
        handler.log.append("litellm_transform_failed")
        raise RuntimeError("private transformed response body")


class _FakeAnthropicNoHTTPModel(_FakeAnthropicLitellmModel):
    async def _fetch_response(self, **kwargs):
        del kwargs
        return {"converted_without_http": True}


class _FakeAnthropicDoublePostModel(_FakeAnthropicLitellmModel):
    async def _fetch_response(self, **kwargs):
        handler = kwargs["model_settings"].extra_args["client"]
        await handler.post("https://api.anthropic.com/v1/messages")
        await handler.post("https://api.anthropic.com/v1/messages")


class _FakeLitellmModule:
    fallbacks = None
    context_window_fallbacks = None
    content_policy_fallbacks = None
    callbacks = []
    success_callback = []
    failure_callback = []
    input_callback = []
    _async_input_callback = []
    _async_success_callback = []
    _async_failure_callback = []
    service_callback = []
    audit_log_callbacks = []
    callback_settings = {}
    cache = None
    num_retries = None
    retry_policy = None
    context_window_fallback_dict = None
    model_alias_map = {}
    num_retries_per_request = None
    custom_prompt_dict = {}
    additional_drop_params = None
    api_base = None
    api_key = None
    organization = None
    project = None
    drop_params = False
    modify_params = False
    error_logs = {}
    turn_off_message_logging = False
    suppress_debug_info = False


def _selection(provider: str):
    bindings = {
        "deepseek": ("responses", "openai_compatible_responses"),
        "openai": ("responses", "openai_responses"),
        "anthropic": ("messages", "litellm_anthropic_chat_completions"),
    }
    surface, transport = bindings[provider]
    return load_and_select_surface_mapping(
        ROOT,
        provider,
        surface,
        transport,
        purpose="offline_validation",
    )


def _runtime_binding(provider: str):
    offline = _selection(provider)
    selected = surface_mapping.VerifiedSurfaceSelection._create(
        surface_mapping._SELECTION_TOKEN,
        purpose="runtime_binding",
        telemetry_schema_sha256=offline.telemetry_schema_sha256,
        mapping=offline.mapping_snapshot(),
        entry={
            "adapter_version": offline.adapter_version,
            "mapping_version": offline.mapping_version,
            "output_counter_comparability": offline.output_counter_comparability,
            "output_counter_path": offline.output_counter_path,
            "runtime_binding_allowed": True,
        },
    )
    return selected.create_runtime_binding()


def _session(provider: str, log: list[str]):
    del log
    temporary_directory = tempfile.TemporaryDirectory()
    _TEMPORARY_DIRECTORIES.append(temporary_directory)
    ledger = AuditLedger(Path(temporary_directory.name) / "audit.sqlite3")
    run_id = ledger.start_run(
        mode="provider-adapter-offline-test",
        request_summary={"objective": "body-free adapter test"},
    )
    runtime = _runtime_binding(provider)
    plan = _plan_binding(runtime)
    tracker = CompletionTelemetryCollector.for_runtime(plan)
    return LedgerCompletionTelemetrySession(
        tracker.bind_case("CASE-001"),
        ledger=ledger,
        run_id=run_id,
        runtime_plan_binding=plan,
    )


def _session_terminals(
    session: LedgerCompletionTelemetrySession,
) -> list[tuple[str, object]]:
    terminals = session._session._tracker._terminals
    return [
        (item.terminal_kind, item.error_code)
        for _, item in sorted(terminals.items())
    ]


def _session_captures(
    session: LedgerCompletionTelemetrySession,
) -> list[SanitizedCompletionCapture]:
    terminals = session._session._tracker._terminals
    return [
        item._capture
        for _, item in sorted(terminals.items())
        if item._capture is not None
    ]


def _session_has_active_attempt(session: LedgerCompletionTelemetrySession) -> bool:
    return bool(session._session._tracker._pending)


def _responses_payload(
    *,
    status: object = "completed",
    details: object = None,
    include_status: bool = True,
    include_details: bool = True,
) -> object:
    values: dict[str, object] = {
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 3},
            "total_tokens": 15,
        },
        "output": [
            {
                "content": "PRIVATE-RESPONSE-BODY",
                "tool_call_arguments": "PRIVATE-TOOL-ARGS",
            }
        ],
        "system_prompt": "PRIVATE-SYSTEM-PROMPT",
    }
    fields = {"usage", "output", "system_prompt"}
    if include_status:
        values["status"] = status
        fields.add("status")
    if include_details:
        values["incomplete_details"] = details
        fields.add("incomplete_details")
    return SimpleNamespace(model_fields_set=fields, **values)


class Phase6ProviderCompletionCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeAsyncOpenAI.instances.clear()
        _FakeAnthropicHTTPHandler.instances.clear()
        _FakeAnthropicLitellmModel.instances.clear()
        _FakeLitellmModule.error_logs = {}
        authorization = patch(
            "researchops.model_providers._anthropic_online_transport_authorized",
            return_value=True,
        )
        authorization.start()
        self.addCleanup(authorization.stop)

    def tearDown(self) -> None:
        while _TEMPORARY_DIRECTORIES:
            _TEMPORARY_DIRECTORIES.pop().cleanup()

    def _run_responses(
        self,
        provider,
        response: object,
        *,
        request_id: str | None = "req_test_1",
        close_error: Exception | None = None,
        api_key: str = "SAFE-TEST-KEY",
    ):
        log: list[str] = []
        session = _session(provider.provider_id, log)
        wrapper = _FakeRawWrapper(
            response,
            request_id=request_id,
            close_error=close_error,
            log=log,
        )
        _FakeAsyncOpenAI.configured_outcomes = [wrapper]
        _FakeAsyncOpenAI.configured_log = log

        async def exercise():
            async with provider.open_model(
                model_id=(
                    "deepseek-v4-flash"
                    if provider.provider_id == "deepseek"
                    else "gpt-5.4-mini"
                ),
                api_key=api_key,
                completion_telemetry_session=session,
            ) as bound:
                result = await bound.sdk_model._fetch_response(
                    system_instructions="not persisted",
                    input="not persisted",
                    model_settings=SimpleNamespace(max_tokens=16),
                    tools=[],
                    output_schema=None,
                    handoffs=[],
                    previous_response_id="previous",
                    conversation_id="conversation",
                    stream=False,
                    prompt=None,
                )
                return bound, result

        with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "agents.OpenAIResponsesModel", _FakeResponsesModel
        ):
            bound, result = asyncio.run(exercise())
        return session, wrapper, bound, result, log

    def test_responses_signature_mirrors_locked_sdk_and_begin_precedes_network(self) -> None:
        session, wrapper, bound, result, log = self._run_responses(
            DeepSeekProvider(), _responses_payload()
        )
        parameters = list(inspect.signature(bound.sdk_model._fetch_response).parameters)
        self.assertEqual(
            parameters,
            [
                "system_instructions",
                "input",
                "model_settings",
                "tools",
                "output_schema",
                "handoffs",
                "previous_response_id",
                "conversation_id",
                "stream",
                "prompt",
            ],
        )
        self.assertIs(result, wrapper.response)
        self.assertEqual(
            log,
            [
                "network_create",
                "raw_response_closed",
            ],
        )
        self.assertTrue(wrapper.parse_called)
        self.assertTrue(wrapper.close_called)
        self.assertIs(bound.completion_telemetry_session, session)

    def test_responses_async_parse_and_sync_close_have_one_ordered_terminal(self) -> None:
        log: list[str] = []
        session = _session("deepseek", log)

        class AsyncParseSyncCloseWrapper(_FakeRawWrapper):
            async def parse(self):
                self.parse_called = True
                log.append("typed_parse")
                return self.response

            def close(self) -> None:
                self.close_called = True
                log.append("raw_response_closed")

        wrapper = AsyncParseSyncCloseWrapper(_responses_payload(), log=log)
        _FakeAsyncOpenAI.configured_outcomes = [wrapper]
        _FakeAsyncOpenAI.configured_log = log

        async def exercise() -> None:
            async with DeepSeekProvider().open_model(
                model_id="deepseek-v4-flash",
                api_key="SAFE-KEY",
                completion_telemetry_session=session,
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None,
                    "safe",
                    SimpleNamespace(max_tokens=16),
                    [],
                    None,
                    [],
                )

        with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "agents.OpenAIResponsesModel", _FakeResponsesModel
        ):
            asyncio.run(exercise())
        self.assertEqual(
            log,
            [
                "network_create",
                "typed_parse",
                "raw_response_closed",
            ],
        )
        self.assertEqual(len(_session_terminals(session)), 1)

    def test_deepseek_completed_capped_missing_and_unknown_shapes(self) -> None:
        cases = (
            (_responses_payload(), "completed", "native_status"),
            (
                _responses_payload(
                    status="incomplete",
                    details={"reason": "max_output_tokens"},
                ),
                "incomplete_length",
                "native_status",
            ),
            (
                _responses_payload(include_status=False, include_details=False),
                "not_provided",
                "none",
            ),
            (
                _responses_payload(status="future_terminal"),
                "unmapped",
                "native_status",
            ),
        )
        selection = _selection("deepseek")
        for payload, expected_state, expected_source in cases:
            with self.subTest(expected_state=expected_state):
                session, wrapper, _, _, _ = self._run_responses(
                    DeepSeekProvider(), payload
                )
                capture = _session_captures(session)[0]
                result = selection.resolve_mapping(capture.mapping_projection())
                self.assertEqual(result[:2], (expected_state, expected_source))
                serialized = repr(capture.record_components())
                self.assertNotIn("PRIVATE-RESPONSE-BODY", serialized)
                self.assertNotIn("PRIVATE-TOOL-ARGS", serialized)
                self.assertNotIn("PRIVATE-SYSTEM-PROMPT", serialized)
                self.assertTrue(wrapper.close_called)

    def test_responses_capture_http_request_id_usage_and_cap(self) -> None:
        session, _, bound, _, _ = self._run_responses(
            OpenAIProvider(), _responses_payload(), request_id="req_openai_safe_1"
        )
        components = _session_captures(session)[0].record_components()
        self.assertEqual(components["http_status"], {"availability": "provided", "value": 200})
        self.assertEqual(
            components["provider_request_id_sha256"]["value"],
            hashlib.sha256(b"req_openai_safe_1").hexdigest(),
        )
        self.assertEqual(components["output_token_cap"]["value"], 16)
        self.assertEqual(
            components["usage"]["normalized"],
            {
                "requests": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "cached_input_tokens": 2,
                "cache_write_tokens": None,
                "reasoning_tokens": 3,
            },
        )
        self.assertEqual(bound.api_surface, "responses")
        self.assertEqual(bound.adapter_version, "openai-responses-adapter/1.0")

    def test_responses_privacy_cleanup_and_stream_fail_closed(self) -> None:
        secret = "KEY-CANARY-DO-NOT-PERSIST"
        log: list[str] = []
        session = _session("deepseek", log)
        wrapper = _FakeRawWrapper(_responses_payload(status=secret), log=log)
        _FakeAsyncOpenAI.configured_outcomes = [wrapper]
        _FakeAsyncOpenAI.configured_log = log

        async def privacy_failure() -> None:
            async with DeepSeekProvider().open_model(
                model_id="deepseek-v4-flash",
                api_key=secret,
                completion_telemetry_session=session,
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None,
                    "safe",
                    SimpleNamespace(max_tokens=16),
                    [],
                    None,
                    [],
                )

        with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "agents.OpenAIResponsesModel", _FakeResponsesModel
        ), self.assertRaises(ProviderConfigurationError) as caught:
            asyncio.run(privacy_failure())
        self.assertEqual(caught.exception.code, "provider_completion_capture_failed")
        self.assertTrue(wrapper.close_called)
        self.assertEqual(_session_terminals(session)[-1][0], "response_rejected")
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, repr(_session_terminals(session)))

        stream_log: list[str] = []
        stream_session = _session("deepseek", stream_log)
        _FakeAsyncOpenAI.configured_outcomes = []
        _FakeAsyncOpenAI.configured_log = stream_log

        async def stream_failure() -> None:
            async with DeepSeekProvider().open_model(
                model_id="deepseek-v4-flash",
                api_key="SAFE-KEY",
                completion_telemetry_session=stream_session,
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None,
                    "safe",
                    SimpleNamespace(max_tokens=16),
                    [],
                    None,
                    [],
                    stream=True,
                )

        with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "agents.OpenAIResponsesModel", _FakeResponsesModel
        ), self.assertRaises(ProviderConfigurationError) as stream_error:
            asyncio.run(stream_failure())
        self.assertEqual(
            stream_error.exception.code, "provider_completion_stream_unsupported"
        )
        self.assertEqual(stream_log, [])

    def test_responses_http_and_cleanup_failures_use_distinct_attempt_terminal(self) -> None:
        log: list[str] = []
        session = _session("deepseek", log)
        _FakeAsyncOpenAI.configured_outcomes = [_FakeHTTPError("redacted")]
        _FakeAsyncOpenAI.configured_log = log

        async def http_failure() -> None:
            async with DeepSeekProvider().open_model(
                model_id="deepseek-v4-flash",
                api_key="SAFE-KEY",
                completion_telemetry_session=session,
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None, "safe", SimpleNamespace(max_tokens=16), [], None, []
                )

        with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "agents.OpenAIResponsesModel", _FakeResponsesModel
        ), self.assertRaises(ProviderConfigurationError) as http_error:
            asyncio.run(http_failure())
        self.assertEqual(
            http_error.exception.code, "provider_completion_http_error"
        )
        self.assertEqual(log, ["network_create"])

        cleanup_log: list[str] = []
        cleanup_session = _session("deepseek", cleanup_log)
        wrapper = _FakeRawWrapper(
            _responses_payload(),
            close_error=RuntimeError("private cleanup body"),
            log=cleanup_log,
        )
        _FakeAsyncOpenAI.configured_outcomes = [wrapper]
        _FakeAsyncOpenAI.configured_log = cleanup_log

        async def cleanup_failure() -> None:
            async with DeepSeekProvider().open_model(
                model_id="deepseek-v4-flash",
                api_key="SAFE-KEY",
                completion_telemetry_session=cleanup_session,
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None, "safe", SimpleNamespace(max_tokens=16), [], None, []
                )

        with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "agents.OpenAIResponsesModel", _FakeResponsesModel
        ), self.assertRaises(ProviderConfigurationError) as cleanup_error:
            asyncio.run(cleanup_failure())
        self.assertEqual(
            cleanup_error.exception.code, "provider_completion_raw_cleanup_failed"
        )
        self.assertEqual(cleanup_log[-1], "raw_response_closed")

    def test_responses_pre_network_and_unknown_outcomes_are_distinct(self) -> None:
        no_response_log: list[str] = []
        no_response_session = _session("deepseek", no_response_log)
        _FakeAsyncOpenAI.configured_outcomes = []
        _FakeAsyncOpenAI.configured_log = no_response_log

        async def pre_network_failure() -> None:
            async with DeepSeekProvider().open_model(
                model_id="deepseek-v4-flash",
                api_key="SAFE-KEY",
                completion_telemetry_session=no_response_session,
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None, "safe", SimpleNamespace(max_tokens=16), [], None, []
                )

        with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "agents.OpenAIResponsesModel", _BuildFailResponsesModel
        ), self.assertRaises(ProviderConfigurationError) as no_response_error:
            asyncio.run(pre_network_failure())
        self.assertEqual(
            no_response_error.exception.code, "provider_completion_no_response"
        )
        self.assertEqual(no_response_log, [])

        unknown_log: list[str] = []
        unknown_session = _session("deepseek", unknown_log)
        _FakeAsyncOpenAI.configured_outcomes = [RuntimeError("private network state")]
        _FakeAsyncOpenAI.configured_log = unknown_log

        async def unknown_failure() -> None:
            async with DeepSeekProvider().open_model(
                model_id="deepseek-v4-flash",
                api_key="SAFE-KEY",
                completion_telemetry_session=unknown_session,
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None, "safe", SimpleNamespace(max_tokens=16), [], None, []
                )

        with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "agents.OpenAIResponsesModel", _FakeResponsesModel
        ), self.assertRaises(ProviderConfigurationError) as caught:
            asyncio.run(unknown_failure())
        self.assertEqual(
            caught.exception.code, "provider_completion_outcome_unknown"
        )
        self.assertEqual(
            unknown_log, ["network_create"]
        )

    def test_responses_owned_http_client_disables_redirects_and_environment_proxy(self) -> None:
        self._run_responses(DeepSeekProvider(), _responses_payload())
        client = _FakeAsyncOpenAI.instances[-1]
        http_client = client.kwargs["http_client"]
        self.assertIs(http_client.follow_redirects, False)
        self.assertIs(http_client._trust_env, False)
        self.assertTrue(http_client.is_closed)

    def test_responses_cancellation_preserves_whether_response_was_observed(self) -> None:
        for observed in (False, True):
            with self.subTest(observed=observed):
                log: list[str] = []
                session = _session("deepseek", log)
                if observed:
                    wrapper = _FakeRawWrapper(
                        _responses_payload(),
                        parse_error=asyncio.CancelledError(),  # type: ignore[arg-type]
                        log=log,
                    )
                    outcome: object = wrapper
                else:
                    outcome = asyncio.CancelledError()
                _FakeAsyncOpenAI.configured_outcomes = [outcome]
                _FakeAsyncOpenAI.configured_log = log

                async def exercise() -> None:
                    async with DeepSeekProvider().open_model(
                        model_id="deepseek-v4-flash",
                        api_key="SAFE-KEY",
                        completion_telemetry_session=session,
                    ) as bound:
                        await bound.sdk_model._fetch_response(
                            None,
                            "safe",
                            SimpleNamespace(max_tokens=16),
                            [],
                            None,
                            [],
                        )

                with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
                    "agents.OpenAIResponsesModel", _FakeResponsesModel
                ), self.assertRaises(asyncio.CancelledError):
                    asyncio.run(exercise())
                self.assertEqual(
                    _session_terminals(session)[-1][0],
                    "response_rejected" if observed else "cancelled",
                )

    def test_session_binding_mismatch_stops_before_client_creation(self) -> None:
        session = _session("deepseek", [])
        with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "agents.OpenAIResponsesModel", _FakeResponsesModel
        ):
            with self.assertRaises(ProviderConfigurationError) as caught:
                asyncio.run(
                    OpenAIProvider().open_model(
                        model_id="gpt-5.4-mini",
                        api_key="SAFE-KEY",
                        completion_telemetry_session=session,
                    ).__aenter__()
                )
        self.assertEqual(
            caught.exception.code, "provider_completion_session_binding_mismatch"
        )

    def test_production_module_cannot_mint_structural_offline_session_authority(
        self,
    ) -> None:
        self.assertFalse(hasattr(provider_module, "_offline_test_completion_session"))
        self.assertFalse(hasattr(provider_module, "_anthropic_offline_test_authorization"))
        selected = _selection("deepseek")
        raw = _InMemorySession(
            selected.provider_id,
            selected.api_surface,
            selected.transport_id,
            selected.adapter_version,
            [],
        )

        class StructuralSubclass(_InMemorySession):
            pass

        subclass = StructuralSubclass(
            selected.provider_id,
            selected.api_surface,
            selected.transport_id,
            selected.adapter_version,
            [],
        )

        for candidate in (raw, subclass):
            with self.subTest(candidate=type(candidate).__name__):
                _FakeAsyncOpenAI.instances.clear()
                with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
                    "agents.OpenAIResponsesModel", _FakeResponsesModel
                ), self.assertRaises(ProviderConfigurationError) as caught:
                    asyncio.run(
                        DeepSeekProvider().open_model(
                            model_id="deepseek-v4-flash",
                            api_key="SAFE-KEY",
                            completion_telemetry_session=candidate,
                        ).__aenter__()
                    )
                self.assertEqual(
                    caught.exception.code,
                    "provider_completion_session_binding_mismatch",
                )
                self.assertEqual(_FakeAsyncOpenAI.instances, [])

    def test_responses_direct_calls_without_session_stop_before_network(self) -> None:
        for provider, model_id in (
            (OpenAIProvider(), "gpt-5.4-mini"),
            (DeepSeekProvider(), "deepseek-v4-flash"),
        ):
            with self.subTest(provider=provider.provider_id):
                log: list[str] = []
                _FakeAsyncOpenAI.configured_log = log
                _FakeAsyncOpenAI.configured_outcomes = []

                async def exercise() -> None:
                    async with provider.open_model(
                        model_id=model_id,
                        api_key="SAFE-KEY",
                    ) as bound:
                        await bound.sdk_model._fetch_response(
                            None,
                            "safe",
                            SimpleNamespace(max_tokens=16),
                            [],
                            None,
                            [],
                        )

                with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
                    "agents.OpenAIResponsesModel", _FakeResponsesModel
                ), self.assertRaises(ProviderConfigurationError) as caught:
                    asyncio.run(exercise())
                self.assertEqual(
                    caught.exception.code,
                    "provider_completion_telemetry_session_required",
                )
                self.assertEqual(log, [])

    def test_anthropic_direct_call_without_session_stops_before_network(self) -> None:
        from agents import ModelSettings

        log: list[str] = []
        _FakeAnthropicHTTPHandler.configured_log = log
        _FakeAnthropicHTTPHandler.configured_responses = []

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="SAFE-ANTHROPIC-KEY",
                _authorization=_anthropic_test_authorization(),
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None,
                    "safe",
                    ModelSettings(max_tokens=16),
                    [],
                    None,
                    [],
                    None,
                    None,
                )

        with patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                _FakeAnthropicLitellmModel,
                _FakeAnthropicHTTPHandler,
                _FakeLitellmModule,
            ),
        ), self.assertRaises(ProviderConfigurationError) as caught:
            asyncio.run(exercise())
        self.assertEqual(
            caught.exception.code,
            "provider_completion_telemetry_session_required",
        )
        self.assertEqual(log, [])
        self.assertEqual(
            _FakeAnthropicHTTPHandler.instances[-1].client.send_count, 0
        )

    def test_unsafe_canary_stops_before_client_or_network(self) -> None:
        session = _session("deepseek", [])
        with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "agents.OpenAIResponsesModel", _FakeResponsesModel
        ):
            with self.assertRaises(ProviderConfigurationError) as caught:
                asyncio.run(
                    DeepSeekProvider().open_model(
                        model_id="deepseek-v4-flash",
                        api_key="x" * 513,
                        completion_telemetry_session=session,
                    ).__aenter__()
                )
        self.assertEqual(caught.exception.code, "provider_completion_canary_invalid")
        self.assertEqual(_FakeAsyncOpenAI.instances, [])

    def test_anthropic_captures_native_fields_before_litellm_transform(self) -> None:
        from agents import ModelSettings

        log: list[str] = []
        session = _session("anthropic", log)
        payload = {
            "id": "msg_ignored",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "PRIVATE-ANTHROPIC-BODY"}],
            "model": "claude-sonnet-5",
            "stop_reason": "max_tokens",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
            },
        }
        response = _FakeAnthropicResponse(payload, log)
        _FakeAnthropicHTTPHandler.configured_responses = [response]
        _FakeAnthropicHTTPHandler.configured_log = log

        async def exercise():
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="SAFE-ANTHROPIC-KEY",
                completion_telemetry_session=session,
                _authorization=_anthropic_test_authorization(),
            ) as bound:
                result = await bound.sdk_model._fetch_response(
                    system_instructions=None,
                    input="not persisted",
                    model_settings=ModelSettings(max_tokens=32),
                    tools=[],
                    output_schema=None,
                    handoffs=[],
                    span=None,
                    tracing=None,
                    stream=False,
                    prompt=None,
                )
                return bound, result

        with patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                _FakeAnthropicLitellmModel,
                _FakeAnthropicHTTPHandler,
                _FakeLitellmModule,
            ),
        ):
            bound, result = asyncio.run(exercise())
        self.assertEqual(
            log,
            [
                "network_post",
                "litellm_transform",
                "raw_response_closed",
            ],
        )
        self.assertEqual(result["converted_finish_reason"], "max_tokens")
        capture = _session_captures(session)[0]
        self.assertEqual(
            capture.mapping_projection(),
            {"stop_reason": "max_tokens", "stop_sequence": None},
        )
        mapped = _selection("anthropic").resolve_mapping(
            capture.mapping_projection()
        )
        self.assertEqual(mapped[:2], ("incomplete_length", "native_status"))
        components = capture.record_components()
        self.assertEqual(components["http_status"]["value"], 200)
        self.assertEqual(components["output_token_cap"]["value"], 32)
        self.assertEqual(components["usage"]["normalized"]["total_tokens"], 18)
        self.assertNotIn("PRIVATE-ANTHROPIC-BODY", repr(components))
        self.assertEqual(bound.api_surface, "messages")
        self.assertTrue(response.closed)
        controlled = _FakeAnthropicLitellmModel.instances[-1].fetch_settings.extra_args
        self.assertEqual(controlled["num_retries"], 0)
        self.assertEqual(controlled["max_retries"], 0)
        self.assertEqual(controlled["fallbacks"], [])
        self.assertIsNone(controlled["retry_policy"])
        self.assertEqual(controlled["context_window_fallback_dict"], {})
        self.assertIs(
            controlled["client"], _FakeAnthropicHTTPHandler.instances[-1]
        )

    def test_anthropic_sensitive_native_value_rejects_and_closes_before_transform(self) -> None:
        from agents import ModelSettings

        secret = "ANTHROPIC-KEY-CANARY"
        log: list[str] = []
        session = _session("anthropic", log)
        response = _FakeAnthropicResponse(
            {
                "stop_reason": "stop_sequence",
                "stop_sequence": secret,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{"text": "PRIVATE-BODY"}],
            },
            log,
        )
        _FakeAnthropicHTTPHandler.configured_responses = [response]
        _FakeAnthropicHTTPHandler.configured_log = log

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key=secret,
                completion_telemetry_session=session,
                _authorization=_anthropic_test_authorization(),
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None,
                    "not persisted",
                    ModelSettings(max_tokens=16),
                    [],
                    None,
                    [],
                    None,
                    None,
                )

        with patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                _FakeAnthropicLitellmModel,
                _FakeAnthropicHTTPHandler,
                _FakeLitellmModule,
            ),
        ), self.assertRaises(ProviderConfigurationError) as caught:
            asyncio.run(exercise())
        self.assertEqual(caught.exception.code, "provider_completion_capture_failed")
        self.assertTrue(response.closed)
        self.assertNotIn("litellm_transform", log)
        self.assertEqual(_session_terminals(session)[-1][0], "response_rejected")
        self.assertNotIn(secret, repr(_session_terminals(session)))

    def test_anthropic_pre_http_provider_error_does_not_leave_attempt_pending(self) -> None:
        from agents import ModelSettings

        log: list[str] = []
        session = _session("anthropic", log)

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="SAFE-ANTHROPIC-KEY",
                completion_telemetry_session=session,
                _authorization=_anthropic_test_authorization(),
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None,
                    "safe",
                    ModelSettings(max_tokens=16),
                    [],
                    None,
                    [],
                    None,
                    None,
                )

        with patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                _FakeAnthropicPreHTTPFailureModel,
                _FakeAnthropicHTTPHandler,
                _FakeLitellmModule,
            ),
        ), self.assertRaises(ProviderConfigurationError):
            asyncio.run(exercise())
        self.assertEqual(log, [])
        self.assertFalse(_session_has_active_attempt(session))

    def test_anthropic_transform_failure_rejects_observed_response_after_cleanup(self) -> None:
        from agents import ModelSettings

        log: list[str] = []
        session = _session("anthropic", log)
        response = _FakeAnthropicResponse(
            {
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            log,
        )
        _FakeAnthropicHTTPHandler.configured_responses = [response]
        _FakeAnthropicHTTPHandler.configured_log = log

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="SAFE-ANTHROPIC-KEY",
                completion_telemetry_session=session,
                _authorization=_anthropic_test_authorization(),
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None,
                    "safe",
                    ModelSettings(max_tokens=16),
                    [],
                    None,
                    [],
                    None,
                    None,
                )

        with patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                _FakeAnthropicTransformFailureModel,
                _FakeAnthropicHTTPHandler,
                _FakeLitellmModule,
            ),
        ), self.assertRaises(ProviderConfigurationError) as caught:
            asyncio.run(exercise())
        self.assertEqual(caught.exception.code, "provider_completion_transform_failed")
        self.assertEqual(
            log,
            [
                "network_post",
                "litellm_transform_failed",
                "raw_response_closed",
            ],
        )
        self.assertTrue(response.closed)
        self.assertEqual(_session_captures(session), [])
        self.assertNotIn("private transformed", str(caught.exception))

    def test_anthropic_transform_without_handler_call_is_no_response(self) -> None:
        from agents import ModelSettings

        log: list[str] = []
        session = _session("anthropic", log)

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="SAFE-ANTHROPIC-KEY",
                completion_telemetry_session=session,
                _authorization=_anthropic_test_authorization(),
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None,
                    "safe",
                    ModelSettings(max_tokens=16),
                    [],
                    None,
                    [],
                    None,
                    None,
                )

        with patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                _FakeAnthropicNoHTTPModel,
                _FakeAnthropicHTTPHandler,
                _FakeLitellmModule,
            ),
        ), self.assertRaises(ProviderConfigurationError) as caught:
            asyncio.run(exercise())
        self.assertEqual(caught.exception.code, "provider_completion_no_response")
        self.assertEqual(log, [])
        self.assertFalse(_session_has_active_attempt(session))

    def test_anthropic_connection_failure_is_single_send_and_outcome_unknown(self) -> None:
        from agents import ModelSettings
        import httpx

        for network_error in (
            httpx.RemoteProtocolError("safe protocol failure"),
            httpx.ConnectError("safe connection failure"),
        ):
            with self.subTest(error_type=type(network_error).__name__):
                log: list[str] = []
                session = _session("anthropic", log)
                _FakeAnthropicHTTPHandler.configured_responses = [network_error]
                _FakeAnthropicHTTPHandler.configured_log = log

                async def exercise() -> None:
                    async with AnthropicProvider().open_model(
                        model_id="claude-sonnet-5",
                        api_key="SAFE-ANTHROPIC-KEY",
                        completion_telemetry_session=session,
                        _authorization=(
                            _anthropic_test_authorization()
                        ),
                    ) as bound:
                        await bound.sdk_model._fetch_response(
                            None,
                            "safe",
                            ModelSettings(max_tokens=16),
                            [],
                            None,
                            [],
                            None,
                            None,
                        )

                with patch(
                    "researchops.model_providers._load_litellm_transport",
                    return_value=(
                        _FakeAnthropicLitellmModel,
                        _FakeAnthropicHTTPHandler,
                        _FakeLitellmModule,
                    ),
                ), self.assertRaises(ProviderConfigurationError) as caught:
                    asyncio.run(exercise())
                self.assertEqual(
                    caught.exception.code, "provider_completion_outcome_unknown"
                )
                handler = _FakeAnthropicHTTPHandler.instances[-1]
                self.assertEqual(handler.client.send_count, 1)
                self.assertEqual(
                    log, ["network_post"]
                )
                self.assertFalse(_session_has_active_attempt(session))

    def test_anthropic_second_post_in_one_attempt_is_rejected_before_send(self) -> None:
        from agents import ModelSettings

        log: list[str] = []
        session = _session("anthropic", log)
        response = _FakeAnthropicResponse(
            {
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            log,
        )
        _FakeAnthropicHTTPHandler.configured_responses = [response]
        _FakeAnthropicHTTPHandler.configured_log = log

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="SAFE-ANTHROPIC-KEY",
                completion_telemetry_session=session,
                _authorization=_anthropic_test_authorization(),
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None,
                    "safe",
                    ModelSettings(max_tokens=16),
                    [],
                    None,
                    [],
                    None,
                    None,
                )

        with patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                _FakeAnthropicDoublePostModel,
                _FakeAnthropicHTTPHandler,
                _FakeLitellmModule,
            ),
        ), self.assertRaises(ProviderConfigurationError) as caught:
            asyncio.run(exercise())
        self.assertEqual(caught.exception.code, "provider_completion_transform_failed")
        handler = _FakeAnthropicHTTPHandler.instances[-1]
        self.assertEqual(handler.client.send_count, 1)
        self.assertEqual(
            log,
            [
                "network_post",
                "raw_response_closed",
            ],
        )
        self.assertTrue(response.closed)

    def test_anthropic_owned_http_client_disables_redirects_and_environment_proxy(self) -> None:
        created: list[object] = []

        class FakeOwnedHTTPClient:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs
                self.closed = False
                created.append(self)

            async def aclose(self) -> None:
                self.closed = True

        class ConstructionOnlyHandler:
            def __init__(self, *, timeout) -> None:
                self.timeout = timeout
                self.event_hooks = None
                self.client = self.create_client(timeout, None)

            async def close(self) -> None:
                await self.client.aclose()

        handler = None
        with patch("httpx.AsyncClient", FakeOwnedHTTPClient):
            handler = provider_module._anthropic_http_handler(
                ConstructionOnlyHandler,
                timeout_seconds=12.0,
                session=_session("anthropic", []),
                api_key="SAFE-ANTHROPIC-KEY",
            )
        self.assertEqual(len(created), 1)
        self.assertIs(created[0].kwargs["follow_redirects"], False)
        self.assertIs(created[0].kwargs["trust_env"], False)
        self.assertEqual(created[0].kwargs["timeout"], 12.0)
        asyncio.run(handler.close())
        self.assertTrue(created[0].closed)

    def test_anthropic_http_error_cleanup_failure_is_not_hidden(self) -> None:
        from agents import ModelSettings

        log: list[str] = []
        session = _session("anthropic", log)
        response = _FakeAnthropicResponse(
            {},
            log,
            status_error=_FakeHTTPError("safe status failure"),
            close_error=RuntimeError("private cleanup body"),
        )
        _FakeAnthropicHTTPHandler.configured_responses = [response]
        _FakeAnthropicHTTPHandler.configured_log = log

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="SAFE-ANTHROPIC-KEY",
                completion_telemetry_session=session,
                _authorization=_anthropic_test_authorization(),
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None,
                    "safe",
                    ModelSettings(max_tokens=16),
                    [],
                    None,
                    [],
                    None,
                    None,
                )

        with patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                _FakeAnthropicLitellmModel,
                _FakeAnthropicHTTPHandler,
                _FakeLitellmModule,
            ),
        ), self.assertRaises(ProviderConfigurationError) as caught:
            asyncio.run(exercise())
        self.assertEqual(
            caught.exception.code, "provider_completion_raw_cleanup_failed"
        )
        self.assertEqual(_session_terminals(session)[-1][0], "response_rejected")
        self.assertEqual(
            _session_terminals(session)[-1][1], "provider_completion_raw_cleanup_failed"
        )
        self.assertNotIn("private cleanup", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
