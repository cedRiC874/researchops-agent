from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import researchops_completion_telemetry.surface_mapping as surface_mapping
from researchops.audit import (
    COMPLETION_TELEMETRY_RESERVED_EVENT_TYPES,
    COMPLETION_TELEMETRY_UNMAPPED_EVENT,
    AuditError,
    AuditLedger,
)
from researchops.completion_telemetry_ledger import (
    LedgerCompletionTelemetrySession,
)
from researchops.model_providers import DeepSeekProvider, ProviderConfigurationError
from researchops_completion_telemetry.capture import (
    CompletionTelemetryCollector,
    RuntimeAttemptHandle,
    RuntimeDenominatorTracker,
    verify_runtime_denominator_plan,
)
from researchops_completion_telemetry.sanitization import (
    CompletionTelemetryError,
    sanitize_completion_capture,
)
from researchops_completion_telemetry.surface_mapping import (
    VerifiedRuntimeCompletionBinding,
    load_and_select_surface_mapping,
)


ROOT = Path(__file__).resolve().parents[1]


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _test_runtime_binding() -> VerifiedRuntimeCompletionBinding:
    offline = load_and_select_surface_mapping(
        ROOT,
        "deepseek",
        "responses",
        "openai_compatible_responses",
        purpose="offline_validation",
    )
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


def _tracker(
    runtime: VerifiedRuntimeCompletionBinding,
    *,
    max_turns: int = 8,
    request_cap: int = 8,
) -> RuntimeDenominatorTracker:
    verified = _plan_binding(
        runtime,
        max_turns=max_turns,
        request_cap=request_cap,
    )
    tracker = CompletionTelemetryCollector.for_runtime(verified)
    if not isinstance(tracker, RuntimeDenominatorTracker):
        raise AssertionError("expected dynamic runtime tracker")
    return tracker


def _plan_binding(
    runtime: VerifiedRuntimeCompletionBinding,
    *,
    case_ids: tuple[str, ...] = ("CASE-001",),
    max_turns: int = 8,
    request_cap: int = 8,
):
    snapshot = runtime.runtime_snapshot()
    plan = {
        "schema_version": "provider-completion-runtime-denominator-plan/1.0",
        "provider_id": snapshot["provider_id"],
        "api_surface": snapshot["api_surface"],
        "transport_id": snapshot["transport_id"],
        "adapter_version": snapshot["adapter_version"],
        "telemetry_schema_sha256": snapshot["telemetry_schema_sha256"],
        "mapping_schema_version": snapshot["mapping_schema_version"],
        "mapping_version": snapshot["mapping_version"],
        "mapping_sha256": snapshot["mapping_sha256"],
        "case_ids": list(case_ids),
        "case_ids_sha256": _canonical_sha256(list(case_ids)),
        "max_turns_per_case": max_turns,
        "total_model_request_cap": request_cap,
        "agents_sdk_retries": 0,
        "http_client_retries": 0,
        "denominator_algorithm": "transport-response-finalization-v1",
        "exact_response_count_preregistered": False,
    }
    return verify_runtime_denominator_plan(
        runtime,
        plan,
        preregistration_commitment=_canonical_sha256(plan),
    )


def _capture(status: str = "completed"):
    return sanitize_completion_capture(
        {
            "status": status,
            "incomplete_details": (
                None if status == "completed" else {"reason": "max_output_tokens"}
            ),
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
            },
            "provider_request_id": "req_safe_ledger_1",
            "http_status": 200,
            "requested_output_token_cap": 2000,
        },
        normalized_usage={
            "requests": 1,
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "cached_input_tokens": 0,
            "cache_write_tokens": None,
            "reasoning_tokens": None,
        },
    )


class CompletionTelemetryLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "audit.sqlite3"
        self.ledger = AuditLedger(self.database)
        self.run_id = self.ledger.start_run(
            mode="completion-telemetry-test",
            request_summary={"objective": "offline bridge verification"},
        )
        self.runtime = _test_runtime_binding()
        self.plan = _plan_binding(self.runtime)
        self.tracker = CompletionTelemetryCollector.for_runtime(self.plan)
        self.assertIsInstance(self.tracker, RuntimeDenominatorTracker)
        self.bridge = LedgerCompletionTelemetrySession(
            self.tracker.bind_case("CASE-001"),
            ledger=self.ledger,
            run_id=self.run_id,
            runtime_plan_binding=self.plan,
        )

    def _events(self) -> list[dict[str, object]]:
        return self.ledger.export_run(self.run_id)["events"]

    def test_begin_event_is_durable_before_fake_network_runs(self) -> None:
        self.assertEqual(self.bridge.provider_id, "deepseek")
        self.assertEqual(self.bridge.api_surface, "responses")
        self.assertEqual(self.bridge.transport_id, "openai_compatible_responses")
        self.assertEqual(
            self.bridge.adapter_version, "deepseek-responses-adapter/1.0"
        )
        handle = self.bridge.begin_attempt()
        network_observation: list[bool] = []

        def fake_network() -> None:
            network_observation.append(
                any(
                    event["event_type"] == "model_request_started"
                    and event["safe_payload"]["attempt_index"]
                    == handle.attempt_index
                    for event in self._events()
                )
            )

        fake_network()
        self.assertEqual(network_observation, [True])
        self.bridge.finalize_no_response(handle, "provider_no_response")

    def test_ledger_subclass_and_instance_method_override_cannot_bypass_writes(
        self,
    ) -> None:
        class NoOpLedger(AuditLedger):
            def append_completion_telemetry_event(self, *args, **kwargs):
                del args, kwargs
                return "0" * 64

        subclass_database = Path(self.temp.name) / "subclass.sqlite3"
        subclass_ledger = NoOpLedger(subclass_database)
        subclass_run = subclass_ledger.start_run(
            mode="completion-telemetry-test-subclass",
            request_summary={"objective": "reject ledger override"},
        )
        with self.assertRaises(AuditError) as subclass_error:
            LedgerCompletionTelemetrySession(
                CompletionTelemetryCollector.for_runtime(self.plan).bind_case(
                    "CASE-001"
                ),
                ledger=subclass_ledger,
                run_id=subclass_run,
                runtime_plan_binding=self.plan,
            )
        self.assertEqual(
            subclass_error.exception.code, "audit_completion_ledger_required"
        )

        override_called = False

        def no_op_override(*args, **kwargs):
            nonlocal override_called
            del args, kwargs
            override_called = True
            return "0" * 64

        self.ledger.append_completion_telemetry_event = no_op_override
        handle = self.bridge.begin_attempt()
        self.bridge.finalize_no_response(handle, "provider_no_response")
        self.assertFalse(override_called)
        reserved = [
            event
            for event in self._events()
            if event["event_type"] in COMPLETION_TELEMETRY_RESERVED_EVENT_TYPES
        ]
        self.assertEqual(len(reserved), 2)
        self.assertTrue(self.ledger.verify_chain(self.run_id).valid)

    def test_opaque_key_echo_is_rejected_closed_and_absent_from_ledger(self) -> None:
        canary = "ZEBRA.GLASS.MARBLE.7391"
        log: list[str] = []

        class RawWrapper:
            request_id = "req_safe_privacy_test"
            status_code = 200

            def __init__(self) -> None:
                self.http_response = self
                self.closed = False

            def parse(self):
                return type(
                    "ResponseProjection",
                    (),
                    {
                        "model_fields_set": {
                            "status",
                            "incomplete_details",
                            "usage",
                        },
                        "status": canary,
                        "incomplete_details": None,
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                )()

            async def close(self) -> None:
                self.closed = True
                log.append("raw_closed")

        wrapper = RawWrapper()

        class RawResponses:
            async def create(self, **kwargs):
                del kwargs
                log.append("network")
                return wrapper

        class FakeAsyncOpenAI:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs
                self.responses = type(
                    "Responses",
                    (),
                    {"with_raw_response": RawResponses()},
                )()

            async def close(self) -> None:
                return None

        class FakeResponsesModel:
            def __init__(self, *, model, openai_client) -> None:
                self.model = model
                self.client = openai_client

            def _get_client(self):
                return self.client

            def _build_response_create_kwargs(self, **kwargs):
                return {
                    "model": self.model,
                    "max_output_tokens": kwargs["model_settings"].max_tokens,
                    "stream": False,
                }

        async def exercise() -> None:
            async with DeepSeekProvider().open_model(
                model_id="deepseek-v4-flash",
                api_key=canary,
                completion_telemetry_session=self.bridge,
            ) as bound:
                await bound.sdk_model._fetch_response(
                    None,
                    "synthetic",
                    type("Settings", (), {"max_tokens": 16})(),
                    [],
                    None,
                    [],
                )

        with patch("openai.AsyncOpenAI", FakeAsyncOpenAI), patch(
            "agents.OpenAIResponsesModel", FakeResponsesModel
        ), self.assertRaises(ProviderConfigurationError) as caught:
            asyncio.run(exercise())
        self.assertEqual(caught.exception.code, "provider_completion_capture_failed")
        self.assertEqual(log, ["network", "raw_closed"])
        self.assertTrue(wrapper.closed)
        exported = self.ledger.export_run(self.run_id)
        self.assertNotIn(canary, json.dumps(exported, ensure_ascii=False))
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.database) + suffix)
            if path.exists():
                self.assertNotIn(canary.encode("utf-8"), path.read_bytes())
        self.assertTrue(self.ledger.verify_chain(self.run_id).valid)
        terminals = [
            event
            for event in exported["events"]
            if event["event_type"] == "model_response_telemetry_rejected"
        ]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(
            terminals[0]["safe_payload"]["error_code"],
            "provider_completion_capture_failed",
        )
    def test_six_terminal_kinds_are_unique_and_usage_shares_accepted_event(self) -> None:
        actions = (
            lambda handle: self.bridge.finalize_response_accepted(handle, _capture()),
            lambda handle: self.bridge.finalize_response_rejected(
                handle, "completion_projection_rejected"
            ),
            lambda handle: self.bridge.finalize_http_error(
                handle, "provider_http_error"
            ),
            lambda handle: self.bridge.finalize_no_response(
                handle, "provider_no_response"
            ),
            lambda handle: self.bridge.finalize_cancelled(handle),
            lambda handle: self.bridge.finalize_outcome_unknown(
                handle, "provider_outcome_unknown"
            ),
        )
        for action in actions:
            action(self.bridge.begin_attempt())

        events = self._events()
        event_types = [event["event_type"] for event in events]
        self.assertTrue(
            all(
                event["actor_kind"] == "provider_adapter"
                for event in events
                if event["event_type"] in COMPLETION_TELEMETRY_RESERVED_EVENT_TYPES
            )
        )
        for event_type in COMPLETION_TELEMETRY_RESERVED_EVENT_TYPES:
            expected_count = (
                6
                if event_type == "model_request_started"
                else 0
                if event_type == COMPLETION_TELEMETRY_UNMAPPED_EVENT
                else 1
            )
            self.assertEqual(event_types.count(event_type), expected_count)
        accepted = next(
            event
            for event in events
            if event["event_type"] == "model_response_telemetry_recorded"
        )["safe_payload"]
        record = accepted["completion_record"]
        self.assertEqual(record["telemetry_schema_version"], "provider-completion-record/1.0")
        self.assertEqual(record["normalized_completion_state"], "completed")
        self.assertEqual(record["truncation_signal_source"], "native_status")
        self.assertEqual(record["usage"]["normalized"]["output_tokens"], 2)
        self.assertTrue(record["usage"]["complete"])
        self.assertEqual(
            record["output_token_cap"],
            {"availability": "provided", "value": 2000},
        )
        self.assertNotIn("model_response_usage_recorded", event_types)
        self.assertTrue(self.ledger.verify_chain(self.run_id).valid)
        commitment = self.bridge.event_commitment()
        self.assertTrue(commitment["all_started_attempts_terminal"])
        self.assertFalse(commitment["write_failed"])
        self.assertEqual(len(commitment["started"]), 6)
        self.assertEqual(len(commitment["terminals"]), 6)
        commitment_body = dict(commitment)
        observed_hash = commitment_body.pop("commitment_sha256")
        self.assertEqual(observed_hash, _canonical_sha256(commitment_body))

    def test_sensitive_incomplete_details_never_enter_database_or_export(self) -> None:
        sensitive_values = (
            "sk-ledger-secret-12345678",
            "Authorization: Bearer ledger-secret",
            r"C:\\Users\\analyst\\private.csv",
            'Traceback (most recent call last): File "private.py", line 7',
        )
        for value in sensitive_values:
            handle = self.bridge.begin_attempt()
            with self.assertRaises(CompletionTelemetryError):
                sanitize_completion_capture(
                    {
                        "status": "incomplete",
                        "incomplete_details": {"reason": value},
                    }
                )
            self.bridge.finalize_response_rejected(
                handle, "completion_telemetry_sensitive_value"
            )
        exported = json.dumps(self.ledger.export_run(self.run_id), ensure_ascii=False)
        raw_database = "".join(
            path.read_bytes().decode("utf-8", errors="ignore")
            for path in self.database.parent.glob(self.database.name + "*")
            if path.is_file()
        )
        for value in sensitive_values:
            self.assertNotIn(value, exported)
            self.assertNotIn(value, raw_database)

    def test_generic_reserved_spoof_and_terminal_duplicate_are_rejected(self) -> None:
        for event_type in COMPLETION_TELEMETRY_RESERVED_EVENT_TYPES:
            with self.subTest(event_type=event_type):
                with self.assertRaises(AuditError) as caught:
                    self.ledger.append_event(self.run_id, event_type, {})
                self.assertEqual(
                    caught.exception.code, "audit_completion_reserved_event_type"
                )

        with self.assertRaises(AuditError) as authority:
            self.ledger.append_completion_telemetry_event(
                "model_request_started",
                {},
                capability=self.runtime,
                attempt_handle=object(),
            )
        self.assertEqual(
            authority.exception.code,
            "audit_completion_write_capability_required",
        )

        forged_payload = {
            "schema_version": "provider-completion-ledger-event/1.1",
            "case_id": "FORGED-CASE",
            "attempt_index": 77,
            "case_attempt_index": 42,
            "binding": self.runtime.runtime_snapshot(),
        }
        forged_handle = RuntimeAttemptHandle(
            case_id="FORGED-CASE",
            attempt_index=77,
            case_attempt_index=42,
            _tracker_token=self.tracker._tracker_token,
        )
        with self.assertRaises(AuditError) as forged_case:
            self.ledger.append_completion_telemetry_event(
                "model_request_started",
                forged_payload,
                capability=self.bridge._capability,
                attempt_handle=forged_handle,
            )
        self.assertEqual(
            forged_case.exception.code,
            "audit_completion_attempt_handle_invalid",
        )

        handle = self.bridge.begin_attempt()
        terminal = self.bridge.finalize_response_accepted(handle, _capture())
        with self.assertRaises(AuditError) as duplicate:
            self.bridge.finalize_cancelled(handle)
        self.assertEqual(
            duplicate.exception.code, "audit_completion_attempt_handle_invalid"
        )
        accepted = next(
            event["safe_payload"]
            for event in self._events()
            if event["event_type"] == "model_response_telemetry_recorded"
        )
        with self.assertRaises(AuditError) as ledger_duplicate:
            self.ledger.append_completion_telemetry_event(
                "model_response_telemetry_recorded",
                accepted,
                capability=self.bridge._capability,
                attempt_handle=handle,
                terminal=terminal,
            )
        self.assertEqual(
            ledger_duplicate.exception.code,
            "audit_completion_terminal_identity_invalid",
        )

    def test_error_terminal_is_exact_and_never_persists_exception_text(self) -> None:
        handle = self.bridge.begin_attempt()
        secret_exception = "provider exploded: sk-never-persist-12345678"
        self.bridge.finalize_http_error(handle, "provider_http_error")
        exported = json.dumps(self.ledger.export_run(self.run_id), ensure_ascii=False)
        self.assertNotIn(secret_exception, exported)
        terminal = next(
            event["safe_payload"]
            for event in self._events()
            if event["event_type"] == "model_request_http_error"
        )
        self.assertEqual(
            set(terminal),
            {
                "schema_version",
                "case_id",
                "attempt_index",
                "case_attempt_index",
                "terminal_kind",
                "response_index",
                "error_code",
                "binding",
            },
        )
        self.assertEqual(terminal["error_code"], "provider_http_error")

    def test_capability_rejects_cross_run_cross_case_and_actor_override(self) -> None:
        signature = inspect.signature(
            self.ledger.append_completion_telemetry_event
        ).parameters
        self.assertNotIn("run_id", signature)
        self.assertNotIn("actor_kind", signature)
        self.assertNotIn("runtime_binding", signature)

        with self.assertRaises(AuditError) as cross_plan:
            LedgerCompletionTelemetrySession(
                self.tracker.bind_case("CASE-001"),
                ledger=self.ledger,
                run_id=self.run_id,
                runtime_plan_binding=_plan_binding(self.runtime),
            )
        self.assertEqual(
            cross_plan.exception.code,
            "audit_completion_write_capability_required",
        )

        handle = self.bridge.begin_attempt()
        second_database = Path(self.temp.name) / "audit-second.sqlite3"
        second_ledger = AuditLedger(second_database)
        second_ledger.start_run(
            mode="cross-run", request_summary={"objective": "cross-run"}
        )
        payload = next(
            event["safe_payload"]
            for event in self._events()
            if event["event_type"] == "model_request_started"
        )
        with self.assertRaises(AuditError) as cross_run:
            second_ledger.append_completion_telemetry_event(
                "model_request_started",
                payload,
                capability=self.bridge._capability,
                attempt_handle=handle,
            )
        self.assertEqual(
            cross_run.exception.code,
            "audit_completion_write_capability_required",
        )
        self.bridge.finalize_no_response(handle, "provider_no_response")

        plan = _plan_binding(
            self.runtime,
            case_ids=("CASE-001", "CASE-002"),
            max_turns=2,
            request_cap=4,
        )
        tracker = CompletionTelemetryCollector.for_runtime(plan)
        case_run_id = self.ledger.start_run(
            mode="cross-case", request_summary={"objective": "cross-case"}
        )
        first = LedgerCompletionTelemetrySession(
            tracker.bind_case("CASE-001"),
            ledger=self.ledger,
            run_id=case_run_id,
            runtime_plan_binding=plan,
        )
        second = LedgerCompletionTelemetrySession(
            tracker.bind_case("CASE-002"),
            ledger=self.ledger,
            run_id=case_run_id,
            runtime_plan_binding=plan,
        )
        first_handle = first.begin_attempt()
        second_handle = second.begin_attempt()
        second_payload = next(
            event["safe_payload"]
            for event in self.ledger.export_run(case_run_id)["events"]
            if event["event_type"] == "model_request_started"
            and event["safe_payload"]["attempt_index"]
            == second_handle.attempt_index
        )
        with self.assertRaises(AuditError) as cross_case:
            self.ledger.append_completion_telemetry_event(
                "model_request_started",
                second_payload,
                capability=first._capability,
                attempt_handle=second_handle,
            )
        self.assertEqual(
            cross_case.exception.code,
            "audit_completion_attempt_handle_invalid",
        )
        first.finalize_no_response(first_handle, "provider_no_response")
        second.finalize_no_response(second_handle, "provider_no_response")

    def test_write_failure_poisoning_forbids_retry(self) -> None:
        failing_plan = _plan_binding(self.runtime)
        failing_tracker = CompletionTelemetryCollector.for_runtime(failing_plan)
        failing = LedgerCompletionTelemetrySession(
            failing_tracker.bind_case("CASE-001"),
            ledger=self.ledger,
            run_id=self.run_id,
            runtime_plan_binding=failing_plan,
        )
        with patch.object(
            AuditLedger,
            "append_completion_telemetry_event",
            side_effect=AuditError(
                "simulated_start_write_failure", "fixed test failure"
            ),
        ):
            with self.assertRaises(AuditError):
                failing.begin_attempt()
        self.assertTrue(failing.failed)
        with self.assertRaises(AuditError) as retry:
            failing.begin_attempt()
        self.assertEqual(retry.exception.code, "audit_completion_session_failed")

        handle = self.bridge.begin_attempt()
        original = AuditLedger.append_completion_telemetry_event

        def fail_terminal(
            ledger,
            event_type,
            payload,
            *,
            capability,
            attempt_handle,
            terminal=None,
        ):
            if event_type != "model_request_started":
                raise AuditError("simulated_write_failure", "fixed test failure")
            return original(
                ledger,
                event_type,
                payload,
                capability=capability,
                attempt_handle=attempt_handle,
                terminal=terminal,
            )

        with patch.object(
            AuditLedger,
            "append_completion_telemetry_event",
            new=fail_terminal,
        ):
            with self.assertRaises(AuditError) as failed:
                self.bridge.finalize_no_response(handle, "provider_no_response")
        self.assertEqual(failed.exception.code, "simulated_write_failure")
        self.assertTrue(self.bridge.failed)
        with self.assertRaises(AuditError) as retry_terminal:
            self.bridge.finalize_no_response(handle, "provider_no_response")
        self.assertEqual(
            retry_terminal.exception.code, "audit_completion_session_failed"
        )
        replacement = LedgerCompletionTelemetrySession(
            self.tracker.bind_case("CASE-001"),
            ledger=self.ledger,
            run_id=self.run_id,
            runtime_plan_binding=self.plan,
        )
        with self.assertRaises(AuditError) as replacement_retry:
            replacement.begin_attempt()
        self.assertEqual(
            replacement_retry.exception.code,
            "audit_completion_attempt_duplicate",
        )
        self.assertTrue(replacement.failed)

    def test_each_completion_field_tamper_breaks_hash_chain(self) -> None:
        mutations = {
            "native": lambda record: record["native_status"].__setitem__(
                "value", "incomplete"
            ),
            "status": lambda record: record.__setitem__(
                "normalized_completion_state", "error"
            ),
            "source": lambda record: record.__setitem__(
                "truncation_signal_source", "none"
            ),
            "usage": lambda record: record["usage"]["normalized"].__setitem__(
                "output_tokens", 3
            ),
            "version": lambda record: record.__setitem__(
                "telemetry_schema_version", "provider-completion-record/9.9"
            ),
            "hash": lambda record: record.__setitem__("mapping_sha256", "0" * 64),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "audit.sqlite3"
                ledger = AuditLedger(database)
                run_id = ledger.start_run(
                    mode="tamper-test", request_summary={"objective": "tamper"}
                )
                runtime = _test_runtime_binding()
                plan = _plan_binding(runtime)
                tracker = CompletionTelemetryCollector.for_runtime(plan)
                bridge = LedgerCompletionTelemetrySession(
                    tracker.bind_case("CASE-001"),
                    ledger=ledger,
                    run_id=run_id,
                    runtime_plan_binding=plan,
                )
                bridge.finalize_response_accepted(bridge.begin_attempt(), _capture())
                with closing(sqlite3.connect(database)) as connection:
                    row = connection.execute(
                        "SELECT safe_payload_json FROM audit_events "
                        "WHERE run_id = ? AND event_type = ?",
                        (run_id, "model_response_telemetry_recorded"),
                    ).fetchone()
                    self.assertIsNotNone(row)
                    payload = json.loads(row[0])
                    mutate(payload["completion_record"])
                    connection.execute("DROP TRIGGER audit_events_no_update")
                    connection.execute(
                        "UPDATE audit_events SET safe_payload_json = ? "
                        "WHERE run_id = ? AND event_type = ?",
                        (
                            json.dumps(
                                payload,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            run_id,
                            "model_response_telemetry_recorded",
                        ),
                    )
                    connection.commit()
                verification = ledger.verify_chain(run_id)
                self.assertFalse(verification.valid)
                self.assertEqual(
                    verification.error_code, "audit_event_hash_mismatch"
                )


if __name__ == "__main__":
    unittest.main()
