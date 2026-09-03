from __future__ import annotations

import asyncio
import json
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
    get_provider,
)
from researchops import model_providers as provider_module
from researchops_completion_telemetry.capture import (
    CompletionTelemetryCollector,
    RuntimeDenominatorTracker,
)
from researchops_completion_telemetry.sanitization import SanitizedCompletionCapture
from researchops_completion_telemetry.surface_mapping import (
    SurfaceMappingError,
    VerifiedSurfaceSelection,
    load_and_select_surface_mapping,
)
from tests.test_completion_telemetry_ledger import (
    _plan_binding,
    _test_runtime_binding,
)


ROOT = Path(__file__).resolve().parents[1]
_TEMPORARY_DIRECTORIES: list[tempfile.TemporaryDirectory] = []


def _anthropic_test_authorization():
    return object()


def _selection(provider_id: str) -> VerifiedSurfaceSelection:
    triples = {
        "deepseek": ("responses", "openai_compatible_responses"),
        "openai": ("responses", "openai_responses"),
        "anthropic": ("messages", "litellm_anthropic_chat_completions"),
        "moonshot_kimi": (
            "openai_compatible_chat_completions",
            "moonshot_direct_chat_completions_sse_v3",
        ),
    }
    surface, transport = triples[provider_id]
    return load_and_select_surface_mapping(
        ROOT,
        provider_id,
        surface,
        transport,
        purpose="offline_validation",
    )


class _RuntimeObservation:
    def __init__(self, selection, tracker: RuntimeDenominatorTracker) -> None:
        self.selection = selection
        self.tracker = tracker

    @property
    def mapping_results(self) -> list[tuple[str, str, object, str]]:
        results = []
        for _, terminal in sorted(self.tracker._terminals.items()):
            if terminal._capture is not None:
                results.append(
                    self.selection.resolve_mapping(
                        terminal._capture.mapping_projection()
                    )
                )
        return results


def _runtime_binding(provider_id: str):
    offline = _selection(provider_id)
    selection = surface_mapping.VerifiedSurfaceSelection._create(
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
    return selection.create_runtime_binding()


def _offline_collector_session(provider_id: str, count: int):
    selection = _selection(provider_id)
    runtime = _runtime_binding(provider_id)
    plan = _plan_binding(runtime, max_turns=count, request_cap=count)
    collector = CompletionTelemetryCollector.for_runtime(plan)
    if not isinstance(collector, RuntimeDenominatorTracker):
        raise AssertionError("expected runtime denominator tracker")
    log: list[str] = []
    temporary_directory = tempfile.TemporaryDirectory()
    _TEMPORARY_DIRECTORIES.append(temporary_directory)
    ledger = AuditLedger(Path(temporary_directory.name) / "audit.sqlite3")
    run_id = ledger.start_run(
        mode="provider-adapter-matrix",
        request_summary={"objective": "body-free adapter matrix"},
    )
    bridge = LedgerCompletionTelemetrySession(
        collector.bind_case("CASE-001"),
        ledger=ledger,
        run_id=run_id,
        runtime_plan_binding=plan,
    )
    return bridge, _RuntimeObservation(selection, collector), collector, log


def _runtime_record_artifact(tracker: RuntimeDenominatorTracker) -> dict[str, object]:
    observed = [
        terminal
        for _, terminal in sorted(tracker._terminals.items())
        if terminal.provider_response_observed
    ]
    tracker.seal_case(
        "CASE-001",
        sdk_raw_response_count=len(observed),
        sdk_usage_request_count=len(tracker._terminals),
        sdk_request_usage_indices_by_response={
            index: (0,) for index in range(len(observed))
        },
    )
    artifact = tracker.seal_runtime()
    return {"records": list(artifact.response_records())}


class _OwnedHTTPClient:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.is_closed = False

    async def aclose(self) -> None:
        self.is_closed = True


class _RawWrapper:
    def __init__(
        self,
        response: object,
        log: list[str],
        *,
        request_id: str,
    ) -> None:
        self.response = response
        self.log = log
        self.request_id = request_id
        self.status_code = 200
        self.http_response = self
        self.closed = False

    def parse(self):
        self.log.append("sync_parse")
        return self.response

    async def aclose(self) -> None:
        self.closed = True
        self.log.append("raw_response_closed")


class _AsyncParseSyncCloseWrapper:
    def __init__(self, response: object, log: list[str]) -> None:
        self.response = response
        self.log = log
        self.request_id = "req_async_parse_sync_close"
        self.status_code = 200
        self.http_response = self
        self.closed = False

    async def parse(self):
        self.log.append("async_parse")
        return self.response

    def close(self) -> None:
        self.closed = True
        self.log.append("sync_close")


class _RawResponses:
    def __init__(self, owner: "_FakeAsyncOpenAI") -> None:
        self.owner = owner

    async def create(self, **kwargs):
        del kwargs
        self.owner.log.append("network_create")
        return self.owner.outcomes.pop(0)


class _FakeAsyncOpenAI:
    configured_outcomes: list[object] = []
    configured_log: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.outcomes = list(self.configured_outcomes)
        self.log = self.configured_log
        self.responses = SimpleNamespace(
            with_raw_response=_RawResponses(self),
        )

    async def close(self) -> None:
        return None


class _FakeResponsesModel:
    def __init__(self, *, model: str, openai_client: _FakeAsyncOpenAI) -> None:
        self.model = model
        self.client = openai_client

    def _get_client(self) -> _FakeAsyncOpenAI:
        return self.client

    def _build_response_create_kwargs(self, **kwargs):
        return {
            "model": self.model,
            "max_output_tokens": kwargs["model_settings"].max_tokens,
            "stream": kwargs["stream"],
        }


def _responses_projection(
    *,
    status: object = "completed",
    incomplete_details: object = None,
    include_status: bool = True,
    include_details: bool = True,
    output_tokens: int = 1,
) -> object:
    values: dict[str, object] = {
        "usage": {
            "input_tokens": 1,
            "output_tokens": output_tokens,
            "total_tokens": 1 + output_tokens,
        },
        # These are deliberately outside model_fields_set and must never be captured.
        "output": [{"content": "PRIVATE-BODY", "arguments": "PRIVATE-ARGS"}],
        "system_prompt": "PRIVATE-SYSTEM",
    }
    fields = {"usage", "output", "system_prompt"}
    if include_status:
        values["status"] = status
        fields.add("status")
    if include_details:
        values["incomplete_details"] = incomplete_details
        fields.add("incomplete_details")
    return SimpleNamespace(model_fields_set=fields, **values)


class _AnthropicResponse:
    def __init__(self, payload: dict[str, object], log: list[str], index: int) -> None:
        self.payload = payload
        self.log = log
        self.headers = {"request-id": f"req_anthropic_matrix_{index}"}
        self.status_code = 200
        self.is_closed = False

    def json(self) -> dict[str, object]:
        return self.payload

    def raise_for_status(self) -> None:
        return None

    async def aclose(self) -> None:
        self.is_closed = True
        self.log.append("raw_response_closed")


class _AnthropicClient:
    def __init__(self, owner: "_AnthropicBaseHandler") -> None:
        self.owner = owner
        self.send_count = 0

    def build_request(self, method: str, url: str, **kwargs):
        return {"method": method, "url": url, **kwargs}

    async def send(self, request: object, *, stream: bool):
        del request, stream
        self.send_count += 1
        self.owner.log.append("network_post")
        return self.owner.responses.pop(0)


class _AnthropicBaseHandler:
    configured_responses: list[object] = []
    configured_log: list[str] = []
    instances: list["_AnthropicBaseHandler"] = []

    def __init__(self, **kwargs) -> None:
        self.timeout = kwargs.get("timeout")
        self.log = self.configured_log
        self.responses = list(self.configured_responses)
        self.client = _AnthropicClient(self)
        self.closed = False
        self.__class__.instances.append(self)

    async def close(self) -> None:
        self.closed = True


class _AnthropicLitellmModel:
    def __init__(self, **kwargs) -> None:
        self.api_key = kwargs.get("api_key")

    async def _fetch_response(self, **kwargs):
        handler = kwargs["model_settings"].extra_args["client"]
        response = await handler.post("https://api.anthropic.com/v1/messages")
        payload = response.json()
        handler.log.append("litellm_transform")
        await response.aclose()
        # LiteLLM's converted result intentionally does not retain native
        # stop_sequence. The collector assertions below prove capture preceded it.
        return {"converted_finish_reason": payload.get("stop_reason")}


class _LitellmModule:
    error_logs = {}
    turn_off_message_logging = False
    suppress_debug_info = False


class ProviderCompletionAdapterMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        authorization = patch(
            "researchops.model_providers._anthropic_online_transport_authorized",
            return_value=True,
        )
        authorization.start()
        self.addCleanup(authorization.stop)

    def tearDown(self) -> None:
        while _TEMPORARY_DIRECTORIES:
            _TEMPORARY_DIRECTORIES.pop().cleanup()

    def test_openai_responses_four_shapes_reach_sanitizer_mapping_and_collector(
        self,
    ) -> None:
        session, raw_session, collector, log = _offline_collector_session(
            "openai", 4
        )
        responses = (
            _responses_projection(),
            _responses_projection(
                status="incomplete",
                incomplete_details={"reason": "max_output_tokens"},
                output_tokens=16,
            ),
            _responses_projection(include_status=False, include_details=False),
            _responses_projection(status="future_terminal"),
        )
        wrappers = [
            _RawWrapper(item, log, request_id=f"req_openai_matrix_{index}")
            for index, item in enumerate(responses)
        ]
        _FakeAsyncOpenAI.configured_outcomes = list(wrappers)
        _FakeAsyncOpenAI.configured_log = log

        async def exercise() -> list[object]:
            async with OpenAIProvider().open_model(
                model_id="gpt-5.4-mini",
                api_key="SAFE-OPENAI-MATRIX-KEY",
                completion_telemetry_session=session,
            ) as bound:
                return [
                    await bound.sdk_model._fetch_response(
                        None,
                        "synthetic",
                        SimpleNamespace(max_tokens=16),
                        [],
                        None,
                        [],
                    )
                    for _ in responses
                ]

        with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "openai.DefaultAsyncHttpxClient", _OwnedHTTPClient
        ), patch("agents.OpenAIResponsesModel", _FakeResponsesModel):
            results = asyncio.run(exercise())

        self.assertEqual(results, list(responses))
        self.assertTrue(all(item.closed for item in wrappers))
        self.assertEqual(
            [item[:2] for item in raw_session.mapping_results],
            [
                ("completed", "native_status"),
                ("incomplete_length", "native_status"),
                ("not_provided", "none"),
                ("unmapped", "native_status"),
            ],
        )
        artifact = _runtime_record_artifact(collector)
        self.assertEqual(
            [item["normalized_completion_state"] for item in artifact["records"]],
            ["completed", "incomplete_length", "not_provided", "unmapped"],
        )
        self.assertEqual(
            artifact["records"][1]["usage"]["normalized"]["output_tokens"],
            16,
        )
        self.assertEqual(
            artifact["records"][1]["output_token_cap"],
            {"availability": "provided", "value": 16},
        )
        self.assertEqual(
            artifact["records"][1]["native_incomplete_details"]["value"],
            {"reason": "max_output_tokens"},
        )
        self.assertEqual(
            artifact["records"][2]["native_status"]["availability"],
            "not_provided",
        )
        self.assertEqual(
            artifact["records"][3]["native_status"]["value"],
            "future_terminal",
        )
        self.assertNotIn("PRIVATE-BODY", json.dumps(artifact, ensure_ascii=False))
        self.assertNotIn("PRIVATE-ARGS", json.dumps(artifact, ensure_ascii=False))
        self.assertNotIn("PRIVATE-SYSTEM", json.dumps(artifact, ensure_ascii=False))

    def test_anthropic_messages_four_native_shapes_precede_litellm_conversion(
        self,
    ) -> None:
        from agents import ModelSettings

        session, raw_session, collector, log = _offline_collector_session(
            "anthropic", 4
        )
        payloads = (
            {
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{"text": "PRIVATE-ANTHROPIC-BODY"}],
            },
            {
                "stop_reason": "max_tokens",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 16},
                "content": [{"text": "PRIVATE-ANTHROPIC-BODY"}],
            },
            {
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{"text": "PRIVATE-ANTHROPIC-BODY"}],
            },
            {
                "stop_reason": "future_stop_reason",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{"text": "PRIVATE-ANTHROPIC-BODY"}],
            },
        )
        responses = [
            _AnthropicResponse(payload, log, index)
            for index, payload in enumerate(payloads)
        ]
        _AnthropicBaseHandler.configured_responses = list(responses)
        _AnthropicBaseHandler.configured_log = log
        _AnthropicBaseHandler.instances.clear()

        async def exercise() -> list[object]:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="SAFE-ANTHROPIC-MATRIX-KEY",
                completion_telemetry_session=session,
                _authorization=_anthropic_test_authorization(),
            ) as bound:
                return [
                    await bound.sdk_model._fetch_response(
                        None,
                        "synthetic",
                        ModelSettings(max_tokens=16),
                        [],
                        None,
                        [],
                        None,
                        None,
                    )
                    for _ in payloads
                ]

        with patch(
            "researchops.model_providers.provider_transport_status",
            return_value={"installed": True, "compatible": True},
        ), patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                _AnthropicLitellmModel,
                _AnthropicBaseHandler,
                _LitellmModule,
            ),
        ):
            converted = asyncio.run(exercise())

        self.assertTrue(all(item.is_closed for item in responses))
        self.assertTrue(all("stop_sequence" not in item for item in converted))
        self.assertEqual(
            [item[:2] for item in raw_session.mapping_results],
            [
                ("completed", "native_status"),
                ("incomplete_length", "native_status"),
                ("not_provided", "none"),
                ("unmapped", "native_status"),
            ],
        )
        artifact = _runtime_record_artifact(collector)
        records = artifact["records"]
        self.assertEqual(records[0]["native_stop_reason"]["value"], "end_turn")
        self.assertEqual(records[1]["native_stop_reason"]["value"], "max_tokens")
        self.assertEqual(records[2]["native_stop_reason"]["availability"], "not_provided")
        self.assertEqual(records[3]["native_stop_reason"]["value"], "future_stop_reason")
        self.assertNotIn(
            "PRIVATE-ANTHROPIC-BODY", json.dumps(artifact, ensure_ascii=False)
        )

    def test_kimi_four_doc_prose_shapes_are_offline_only_and_runtime_denied(
        self,
    ) -> None:
        selection = _selection("moonshot_kimi")
        mapping = json.loads(
            (
                ROOT
                / "evals/provider_completion_telemetry_v1/provider_completion_mapping_v1.json"
            ).read_text(encoding="utf-8")
        )
        fixtures = []
        for scenario in ("completed", "length_capped", "missing_fields", "unknown_value"):
            fixture = json.loads(
                (
                    ROOT
                    / "evals/provider_completion_telemetry_v1/fixtures"
                    / f"kimi_{scenario}_20260902.json"
                ).read_text(encoding="utf-8")
            )
            fixtures.append(fixture)
            expected = mapping["fixture_expectations"][fixture["fixture_id"]]
            self.assertEqual(fixture["provider_id"], "moonshot_kimi")
            self.assertEqual(fixture["provenance"]["tier"], "doc_prose")
            self.assertTrue(fixture["provenance"]["unverified_shape"])
            self.assertFalse(
                fixture["provenance"]["source"]["successful_handshake_observed"]
            )
            self.assertEqual(
                selection.resolve_mapping(fixture["response_projection"]),
                (
                    expected["normalized_completion_state"],
                    expected["truncation_signal_source"],
                    expected["preserved_native_value"],
                    expected["matched_rule_id"],
                ),
            )

        self.assertEqual({item["scenario"] for item in fixtures}, {
            "completed",
            "length_capped",
            "missing_fields",
            "unknown_value",
        })
        metadata = selection.mapping_snapshot()["surface_selection"]
        self.assertEqual(metadata["provenance_tier"], "doc_prose")
        self.assertTrue(metadata["unverified_shape"])
        self.assertTrue(metadata["first_live_validation_required"])
        self.assertFalse(metadata["runtime_binding_allowed"])
        with self.assertRaises(SurfaceMappingError) as runtime:
            load_and_select_surface_mapping(
                ROOT,
                "moonshot_kimi",
                "openai_compatible_chat_completions",
                "moonshot_direct_chat_completions_sse_v3",
                purpose="runtime_binding",
            )
        self.assertEqual(
            runtime.exception.code, "surface_mapping_runtime_binding_blocked"
        )
        with self.assertRaises(ProviderConfigurationError) as provider:
            get_provider("moonshot_kimi")
        self.assertEqual(provider.exception.code, "provider_invalid")

    def test_deepseek_incomplete_details_sensitive_matrix_never_reaches_ledger(
        self,
    ) -> None:
        values = (
            ("opaque_canary", "ZEBRA.GLASS.MARBLE.7391"),
            ("api_key", "sk-t4-secret-12345678"),
            ("authorization", "Authorization: Bearer t4-secret"),
            ("windows_path", r"C:\Users\analyst\private.csv"),
            ("posix_path", "/home/analyst/private.csv"),
            (
                "traceback",
                'Traceback (most recent call last): File "private.py", line 7',
            ),
        )
        runtime = _test_runtime_binding()
        case_ids = tuple(f"CASE-T4-{index:02d}" for index in range(len(values)))
        plan = _plan_binding(
            runtime,
            case_ids=case_ids,
            max_turns=1,
            request_cap=len(case_ids),
        )
        tracker = CompletionTelemetryCollector.for_runtime(plan)
        self.assertIsInstance(tracker, RuntimeDenominatorTracker)

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            ledger = AuditLedger(database)
            run_id = ledger.start_run(
                mode="completion-telemetry-adapter-privacy-matrix",
                request_summary={"objective": "offline privacy matrix"},
            )
            wrappers: list[_RawWrapper] = []

            async def exercise() -> None:
                for index, (label, sensitive) in enumerate(values):
                    response = _responses_projection(
                        status="incomplete",
                        incomplete_details={"reason": sensitive},
                    )
                    wrapper = _RawWrapper(
                        response,
                        [],
                        request_id=f"req_deepseek_privacy_{index}",
                    )
                    wrappers.append(wrapper)
                    _FakeAsyncOpenAI.configured_outcomes = [wrapper]
                    _FakeAsyncOpenAI.configured_log = wrapper.log
                    bridge = LedgerCompletionTelemetrySession(
                        tracker.bind_case(case_ids[index]),
                        ledger=ledger,
                        run_id=run_id,
                        runtime_plan_binding=plan,
                    )
                    api_key = sensitive if label == "opaque_canary" else "SAFE-T4-KEY"
                    with self.subTest(label=label), self.assertRaises(
                        ProviderConfigurationError
                    ) as caught:
                        async with DeepSeekProvider().open_model(
                            model_id="deepseek-v4-flash",
                            api_key=api_key,
                            completion_telemetry_session=bridge,
                        ) as bound:
                            await bound.sdk_model._fetch_response(
                                None,
                                "synthetic",
                                SimpleNamespace(max_tokens=16),
                                [],
                                None,
                                [],
                            )
                    self.assertEqual(
                        caught.exception.code, "provider_completion_capture_failed"
                    )
                    self.assertTrue(wrapper.closed)
                    commitment = bridge.event_commitment()
                    self.assertTrue(commitment["all_started_attempts_terminal"])
                    self.assertFalse(commitment["write_failed"])

            with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
                "openai.DefaultAsyncHttpxClient", _OwnedHTTPClient
            ), patch("agents.OpenAIResponsesModel", _FakeResponsesModel):
                asyncio.run(exercise())

            exported = ledger.export_run(run_id)
            serialized = json.dumps(exported, ensure_ascii=False)
            database_bytes = b"".join(
                path.read_bytes()
                for suffix in ("", "-wal", "-shm")
                if (path := Path(str(database) + suffix)).exists()
            )
            for label, sensitive in values:
                with self.subTest(persisted_value=label):
                    self.assertNotIn(sensitive, serialized)
                    self.assertNotIn(sensitive.encode("utf-8"), database_bytes)
            terminals = [
                event
                for event in exported["events"]
                if event["event_type"] == "model_response_telemetry_rejected"
            ]
            self.assertEqual(len(terminals), len(values))
            self.assertTrue(
                all(
                    item["safe_payload"]["error_code"]
                    == "provider_completion_capture_failed"
                    for item in terminals
                )
            )
            self.assertTrue(all(item.closed for item in wrappers))
            self.assertTrue(ledger.verify_chain(run_id).valid)

    def test_responses_async_parse_and_sync_close_remain_ordered(self) -> None:
        session, raw_session, collector, log = _offline_collector_session(
            "openai", 1
        )
        wrapper = _AsyncParseSyncCloseWrapper(_responses_projection(), log)
        _FakeAsyncOpenAI.configured_outcomes = [wrapper]
        _FakeAsyncOpenAI.configured_log = log

        async def exercise() -> None:
            async with OpenAIProvider().open_model(
                model_id="gpt-5.4-mini",
                api_key="SAFE-ASYNC-PARSE-KEY",
                completion_telemetry_session=session,
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None,
                    "synthetic",
                    SimpleNamespace(max_tokens=16),
                    [],
                    None,
                    [],
                )

        with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "openai.DefaultAsyncHttpxClient", _OwnedHTTPClient
        ), patch("agents.OpenAIResponsesModel", _FakeResponsesModel):
            asyncio.run(exercise())

        self.assertEqual(
            log,
            [
                "network_create",
                "async_parse",
                "sync_close",
            ],
        )
        self.assertTrue(wrapper.closed)
        self.assertEqual(raw_session.mapping_results[0][:2], ("completed", "native_status"))
        self.assertEqual(
            _runtime_record_artifact(collector)["records"][0][
                "normalized_completion_state"
            ],
            "completed",
        )


if __name__ == "__main__":
    unittest.main()
