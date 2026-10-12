from __future__ import annotations

import asyncio
import builtins
import hashlib
import io
import json
import socket
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import researchops_completion_telemetry.surface_mapping as surface_mapping

from researchops.audit import AuditError, AuditLedger
from researchops.eval_v2_contracts import EvalV2ContractError
from researchops.eval_v2_interactive_telemetry import (
    EvalV2InteractiveTelemetrySession,
    _prepare_interactive_case,
    verify_session,
)
from tests.test_completion_telemetry_ledger import _capture
from researchops.eval_v2_provider_executor import EvalV2ProviderExecutor
from researchops.eval_v2_runner import EvalV2ToolGateway
from researchops.model_providers import (
    DeepSeekProvider,
    OpenAIProvider,
    ProviderConfigurationError,
    _validate_completion_session,
)
from tests.test_eval_v2_provider_executor import (
    FakeInspectBackend,
    synthetic_completion_diagnostic_task,
)


ROOT = Path(__file__).resolve().parents[1]
KEY = "SYNTHETIC-INTERACTIVE-SECRET-CANARY"
QUESTION = "SYNTHETIC-INTERACTIVE-QUESTION-CANARY"
LOCAL_PATH = "X:/synthetic/private-local-canary.csv"


def response_payload(*, status="completed", output_tokens=12, text="Synthetic aggregate answer."):
    return {
        "id": "resp_synthetic_interactive", "object": "response", "created_at": 0,
        "status": status, "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "model": "deepseek-v4-flash", "parallel_tool_calls": False,
        "output": [{"id": "msg_synthetic", "type": "message", "role": "assistant",
                    "status": status, "content": [{"type": "output_text", "text": text, "annotations": []}]}],
        "tool_choice": "auto", "tools": [], "temperature": 1, "top_p": 1,
        "usage": {"input_tokens": 30, "output_tokens": output_tokens,
                  "total_tokens": 30 + output_tokens,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 0}},
    }


@contextmanager
def mock_responses_transport(*, payload=None, status_code=200, failure=None):
    """Real SDK and controlled model, with only the HTTP transport replaced."""
    from agents import OpenAIResponsesModel
    from openai import AsyncOpenAI

    calls = []

    def handler(request):
        calls.append({"method": request.method, "endpoint": request.url.path})
        if failure is not None:
            raise failure
        return httpx.Response(status_code, json=response_payload() if payload is None else payload,
                              headers={"x-request-id": "req_synthetic_interactive"})

    def client(**kwargs):
        return httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(handler))

    with patch("researchops.model_providers._load_responses_transport", return_value=(AsyncOpenAI, OpenAIResponsesModel, client)):
        yield calls


@contextmanager
def prepared_case(directory, *, case_id="INTERACTIVE-SYNTHETIC-001", max_turns=1):
    with patch("researchops.eval_v2_interactive_telemetry._verify_loaded_sources"), patch(
        "researchops.eval_v2_interactive_telemetry._verify_clean_commit", return_value="a" * 40
    ):
        yield _prepare_interactive_case(root=ROOT, session_dir=directory, case_id=case_id,
                                        provider=DeepSeekProvider(), model_id="deepseek-v4-flash", max_turns=max_turns)


class _UnusableKey(str):
    def strip(self, *args, **kwargs):
        raise AssertionError("Key must not be used before admission")


class InteractiveExecutorTests(unittest.TestCase):
    def setUp(self):
        # Mock HTTP never reaches a socket. Even accidental connections are denied.
        original_connect = socket.socket.connect
        def connect(sock, address):
            if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
                raise AssertionError("Offline test forbids external network")
            return original_connect(sock, address)
        guard = patch.object(socket.socket, "connect", connect)
        guard.start()
        self.addCleanup(guard.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def _executor(self, session, **kwargs):
        options = dict(provider=DeepSeekProvider(), model_id="deepseek-v4-flash", api_key=KEY,
                       confirm_online=True, completion_telemetry_session=session, max_turns=1)
        options.update(kwargs)
        return EvalV2ProviderExecutor(**options)

    def _execute(self, session):
        task = synthetic_completion_diagnostic_task(expects_inspection=False)
        public_input = task.public_input()
        public_input["prompt"] = QUESTION + " " + LOCAL_PATH
        return self._executor(session).execute(public_input, EvalV2ToolGateway(task, FakeInspectBackend()))

    def _assert_admission_denied(self, session, **kwargs):
        with patch.object(DeepSeekProvider, "open_model", side_effect=AssertionError("Provider must not open")) as opened:
            with self.assertRaises(EvalV2ContractError) as raised:
                self._executor(session, api_key=_UnusableKey(KEY), **kwargs)
        self.assertEqual(raised.exception.code, "eval_v2_completion_telemetry_session_required")
        self.assertNotIn(KEY, str(raised.exception))
        opened.assert_not_called()

    def test_missing_wrong_and_subclass_sessions_fail_before_key_or_provider(self):
        self._assert_admission_denied(None)
        self._assert_admission_denied(SimpleNamespace())
        class Derived(EvalV2InteractiveTelemetrySession):
            pass
        self._assert_admission_denied(object.__new__(Derived))

    def test_failed_and_sealed_sessions_fail_before_key_or_provider(self):
        with prepared_case(self.directory, case_id="FAILED") as owner:
            owner.session._failed = True
            self._assert_admission_denied(owner.session)
        with prepared_case(self.directory, case_id="SEALED") as owner:
            owner.finish(completion_status="failed")
            self._assert_admission_denied(owner.session)

    def test_provider_model_surface_transport_and_adapter_must_match(self):
        with prepared_case(self.directory) as owner:
            self._assert_admission_denied(owner.session, provider=OpenAIProvider())
            self._assert_admission_denied(owner.session, model_id="deepseek-v4-pro")
            self._assert_admission_denied(owner.session, model_id="unknown-model")
            for field in ("api_surface", "transport_id", "adapter_version"):
                with self.subTest(field=field), patch.object(DeepSeekProvider, field, "mismatch"):
                    self._assert_admission_denied(owner.session)

    def test_session_invalidated_after_constructor_does_not_open_provider(self):
        with prepared_case(self.directory) as owner:
            executor = self._executor(owner.session)
            owner.session._failed = True
            task = synthetic_completion_diagnostic_task(expects_inspection=False)
            with patch.object(DeepSeekProvider, "open_model") as opened:
                with self.assertRaises(EvalV2ContractError) as raised:
                    executor.execute(task.public_input(), EvalV2ToolGateway(task, FakeInspectBackend()))
            self.assertEqual(raised.exception.code, "eval_v2_completion_telemetry_session_required")
            opened.assert_not_called()

    def test_real_sdk_controlled_model_ledger_closure_and_private_bytes(self):
        output = io.StringIO()
        with prepared_case(self.directory) as owner, redirect_stdout(output), redirect_stderr(output):
            with mock_responses_transport() as calls:
                result = self._execute(owner.session)
            summary = owner.finish(completion_status=result.completion_status)
            self.assertEqual(result.completion_status, "complete")
            self.assertEqual(len(calls), 1)
            self.assertTrue(summary["closure_passed"])
            self.assertTrue(summary["all_terminal"])
            self.assertTrue(summary["normal_result"])
            self.assertEqual(summary["attempts"], 1)
            self.assertEqual(summary["terminal_states"], ["completed"])
            self.assertEqual(summary["usage"], {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42})
            export = owner.ledger.export_run(owner.run_id)
            events = [row["event_type"] for row in export["events"]]
            self.assertIn("model_request_started", events)
            self.assertIn("model_response_telemetry_recorded", events)
            verified, passed = verify_session(self.directory)
            self.assertTrue(passed)
            self.assertEqual(verified[0]["attempts"], 1)
        raw = output.getvalue().encode() + b"".join(path.read_bytes() for path in self.directory.rglob("*") if path.is_file())
        for canary in (KEY, QUESTION, LOCAL_PATH, str(self.directory)):
            self.assertNotIn(canary.encode(), raw)

    def test_incomplete_response_is_not_a_normal_result_and_never_retried(self):
        with prepared_case(self.directory) as owner, mock_responses_transport(payload=response_payload(status="incomplete", output_tokens=2000)) as calls:
            result = self._execute(owner.session)
            summary = owner.finish(completion_status=result.completion_status)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.outcome, "controlled_failure")
        self.assertFalse(summary["normal_result"])
        self.assertEqual(summary["terminal_states"], ["incomplete_length"])

    def test_http_error_connection_loss_and_cancellation_are_terminal_no_retry(self):
        cases = [
            ("HTTP", {"status_code": 503, "payload": {"error": {"message": "synthetic error", "type": "server_error"}}}, "http_error"),
            ("NO-RESPONSE", {"failure": httpx.ConnectError("synthetic no response")}, "outcome_unknown"),
            ("CANCEL", {"failure": asyncio.CancelledError()}, "cancelled"),
        ]
        for case_id, options, terminal in cases:
            with self.subTest(case=case_id), prepared_case(self.directory, case_id=case_id) as owner:
                with mock_responses_transport(**options) as calls:
                    try:
                        result = self._execute(owner.session)
                    except EvalV2ContractError:
                        status = "failed"
                    else:
                        self.assertEqual(result.outcome, "controlled_failure")
                        self.assertEqual(result.final_output, "")
                        status = result.completion_status
                summary = owner.finish(completion_status=status)
                self.assertEqual(len(calls), 1)
                self.assertFalse(summary["normal_result"])
                self.assertTrue(summary["all_terminal"])
                self.assertEqual(summary["terminal_states"], [terminal])
                self.assertIsNone(summary["usage"]["input_tokens"])

    def test_ledger_write_failure_prevents_normal_answer_and_retry(self):
        with prepared_case(self.directory) as owner, mock_responses_transport() as calls:
            original = AuditLedger.append_completion_telemetry_event
            def fail_terminal(ledger, event_type, *args, **kwargs):
                if event_type != "model_request_started":
                    raise AuditError("synthetic_write_failed", "synthetic write failure")
                return original(ledger, event_type, *args, **kwargs)
            with patch.object(AuditLedger, "append_completion_telemetry_event", side_effect=fail_terminal):
                with self.assertRaises(EvalV2ContractError):
                    self._execute(owner.session)
            summary = owner.finish(completion_status="failed")
        self.assertFalse(summary["normal_result"])
        self.assertTrue(summary["write_failed"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(summary["attempts"], 1)
        self.assertFalse(summary["all_terminal"])
        self._assert_admission_denied(owner.session)

    def test_no_response_before_transport_is_terminal_and_exception_is_private(self):
        from agents import OpenAIResponsesModel

        output = io.StringIO()
        with prepared_case(self.directory) as owner, mock_responses_transport() as calls:
            with patch.object(OpenAIResponsesModel, "_build_response_create_kwargs", side_effect=ValueError(KEY + QUESTION + LOCAL_PATH)):
                with redirect_stdout(output), redirect_stderr(output):
                    with self.assertRaises(EvalV2ContractError) as raised:
                        self._execute(owner.session)
            summary = owner.finish(completion_status="failed")
        self.assertEqual(calls, [])
        self.assertEqual(summary["attempts"], 1)
        self.assertEqual(summary["terminal_states"], ["no_response"])
        self.assertTrue(summary["all_terminal"])
        self.assertFalse(summary["normal_result"])
        raw = (output.getvalue() + str(raised.exception)).encode() + b"".join(
            path.read_bytes() for path in self.directory.rglob("*") if path.is_file()
        )
        for canary in (KEY, QUESTION, LOCAL_PATH):
            self.assertNotIn(canary.encode(), raw)

    def test_provider_registration_does_not_import_new_module_for_other_sessions(self):
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name.endswith("eval_v2_interactive_telemetry"):
                raise AssertionError("unrelated provider path imported interactive telemetry")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=guarded):
            with self.assertRaises(ProviderConfigurationError) as raised:
                _validate_completion_session(SimpleNamespace(), provider_id="deepseek", api_surface="responses",
                                             transport_id="openai_compatible_responses", adapter_version="deepseek-responses-adapter/1.0")
        self.assertEqual(raised.exception.code, "provider_completion_session_binding_mismatch")

    def test_interactive_scope_is_exact_and_unknown_scope_still_rejected(self):
        with prepared_case(self.directory) as owner:
            binding = owner.session._binding
            binding.assert_runtime_authority(expected_scope="eval_v2_interactive_v1")
            for scope in ("campaign_runtime", "first_live_validation", "internal_telemetry_validation",
                          "item6_controlled_experiment_v1", "eval_v2_interactive_unknown"):
                with self.subTest(scope=scope), self.assertRaises(surface_mapping.SurfaceMappingError):
                    binding.assert_runtime_authority(expected_scope=scope)
            object.__setattr__(binding, "_authority_scope", "eval_v2_interactive_unknown")
            with self.assertRaises(surface_mapping.SurfaceMappingError):
                binding.assert_runtime_authority()
        offline = surface_mapping.load_and_select_surface_mapping(
            ROOT, "deepseek", "responses", "openai_compatible_responses", purpose="offline_validation"
        )
        unknown = surface_mapping.VerifiedSurfaceSelection._create(
            surface_mapping._SELECTION_TOKEN, purpose="eval_v2_interactive_unknown",
            telemetry_schema_sha256=offline.telemetry_schema_sha256, mapping=offline.mapping_snapshot(),
            entry=dict(adapter_version=offline.adapter_version, mapping_version=offline.mapping_version,
                       output_counter_comparability=offline.output_counter_comparability,
                       output_counter_path=offline.output_counter_path, runtime_binding_allowed=True),
        )
        with self.assertRaises(surface_mapping.SurfaceMappingError):
            unknown.create_runtime_binding()

    def test_noncomplete_accepted_capture_prevents_another_attempt(self):
        for status, state in (("incomplete", "incomplete_length"), ("future_terminal", "unmapped")):
            with self.subTest(status=status), prepared_case(self.directory, case_id="STATE-" + status, max_turns=3) as owner:
                handle = owner.session.begin_attempt()
                owner.session.finalize_response_accepted(handle, _capture(status))
                with self.assertRaises(AuditError):
                    owner.session.begin_attempt()
                owner.session._record_sdk_result(SimpleNamespace(raw_responses=[SimpleNamespace(usage=None)],
                    context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=1))))
                summary = owner.finish(completion_status="output_truncated")
                self.assertEqual(summary["attempts"], 1)
                self.assertEqual(summary["terminal_states"], [state])
                self.assertTrue(summary["all_terminal"])
                self.assertFalse(summary["normal_result"])
                self.assertFalse(summary["write_failed"])
                self.assertEqual(summary["closure_passed"], status == "incomplete")

    def _assert_nonrow_tamper_is_detected(self, statement):
        with prepared_case(self.directory) as owner, mock_responses_transport():
            result = self._execute(owner.session)
            self.assertTrue(owner.finish(completion_status=result.completion_status)["normal_result"])
        self.assertTrue(verify_session(self.directory)[1])
        before = owner.ledger.export_run(owner.run_id)
        with closing(sqlite3.connect(owner.database)) as database:
            with database:
                database.execute(statement)
        self.assertEqual(owner.ledger.export_run(owner.run_id), before)
        cases, valid = verify_session(self.directory)
        self.assertFalse(valid)
        self.assertFalse(cases[0]["verified"])

    def test_dropped_append_only_trigger_is_detected_by_file_commitment(self):
        self._assert_nonrow_tamper_is_detected("DROP TRIGGER audit_events_no_update")

    def test_sqlite_header_change_is_detected_by_file_commitment(self):
        self._assert_nonrow_tamper_is_detected("PRAGMA user_version=1")

    def test_uncheckpointed_wal_tamper_is_rejected_before_reading(self):
        with prepared_case(self.directory) as owner, mock_responses_transport():
            result = self._execute(owner.session)
            self.assertTrue(owner.finish(completion_status=result.completion_status)["normal_result"])
        receipt = json.loads(owner.database.with_suffix(".json").read_text())
        with closing(sqlite3.connect(owner.database)) as database:
            database.execute("PRAGMA wal_autocheckpoint=0")
            database.execute("DROP TRIGGER audit_events_no_update")
            database.commit()
            self.assertGreater(Path(str(owner.database) + "-wal").stat().st_size, 0)
            self.assertEqual(hashlib.sha256(owner.database.read_bytes()).hexdigest(), receipt["ledger_file_sha256"])
            with patch("researchops.eval_v2_interactive_telemetry._read_rows", side_effect=AssertionError("Do not read uncommitted ledger")):
                self.assertFalse(verify_session(self.directory)[1])

    def test_finish_cannot_seal_an_outstanding_wal(self):
        with prepared_case(self.directory) as owner, closing(sqlite3.connect(owner.database)) as observer:
            observer.execute("SELECT COUNT(*) FROM runs").fetchone()
            with mock_responses_transport():
                result = self._execute(owner.session)
            summary = owner.finish(completion_status=result.completion_status)
            self.assertFalse(summary["normal_result"])
            self.assertTrue(summary["write_failed"])
            self.assertFalse(owner.database.with_suffix(".json").exists())


if __name__ == "__main__":
    unittest.main()
