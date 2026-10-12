from __future__ import annotations

import asyncio
import io
import json
import socket
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import researchops.cli as cli
import researchops.eval_v2_interactive_telemetry as telemetry_module
import researchops.self_pilot as pilot
from researchops.audit import AuditError, AuditLedger
from researchops.eval_v2_contracts import EvalV2ContractError
from researchops.model_providers import DeepSeekProvider
from researchops.self_pilot_web import SelfPilotWebController
from tests import test_self_pilot as existing_fixtures
from tests.test_eval_v2_interactive_executor import (
    mock_responses_transport,
    response_payload,
)


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "evals/v2/public_tasks.jsonl"
DATASETS = ROOT / "evals/v2/external_datasets.json"
TRANSLATIONS = ROOT / "evals/v2/self_pilot_translations.zh-CN.json"
FAKE_KEY = "sk-synthetic-self-pilot-interactive-canary"


class _Environment(dict):
    def __init__(self, on_key):
        super().__init__()
        self.on_key = on_key

    def get(self, name, default=None):
        if name == "DEEPSEEK_API_KEY":
            self.on_key()
            return FAKE_KEY
        return default


class SelfPilotInteractiveTelemetryTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "artifacts").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "artifacts", prefix="offline-interactive-")
        self.addCleanup(temporary.cleanup)
        fixture_root = Path(temporary.name)
        (fixture_root / "artifacts").mkdir()
        self.registry = existing_fixtures.SelfPilotTests._write_registry(
            SimpleNamespace(root=fixture_root)
        )
        self.session = fixture_root / "session"
        pilot.create_self_pilot_session(
            project_root=ROOT,
            output_directory=self.session,
            tasks_path=TASKS,
            dataset_manifest_path=DATASETS,
            task_count=1,
        )
        self.task_id = pilot.get_next_self_pilot_task(
            project_root=ROOT, session_directory=self.session
        )["task"]["task_id"]
        self.key_reads = 0
        self._start_patch(patch.object(telemetry_module, "_verify_clean_commit", return_value="a" * 40))
        self._start_patch(patch.object(pilot.os, "environ", _Environment(self._assert_plan_before_key)))
        original_connect = socket.socket.connect
        original_resolve = socket.getaddrinfo

        def guarded_connect(sock, address):
            if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
                raise AssertionError("offline test forbids external sockets")
            return original_connect(sock, address)

        def guarded_resolve(host, *args, **kwargs):
            if host not in {"127.0.0.1", "::1", "localhost", None}:
                raise AssertionError("offline test forbids external DNS")
            return original_resolve(host, *args, **kwargs)

        self._start_patch(patch.object(socket.socket, "connect", guarded_connect))
        self._start_patch(patch.object(socket, "getaddrinfo", guarded_resolve))

    def _start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def _assert_plan_before_key(self):
        databases = list((self.session / "interactive-telemetry").glob("*.sqlite3"))
        self.assertEqual(len(databases), 1)
        with closing(sqlite3.connect(databases[0])) as connection:
            events = [row[0] for row in connection.execute("SELECT event_type FROM audit_events")]
        self.assertIn("interactive_plan_committed", events)
        state = json.loads((self.session / "pilot_state.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(state["records"][0]["provider_run"])
        self.key_reads += 1

    def _run(self, **overrides):
        arguments = dict(
            project_root=ROOT, session_directory=self.session,
            tasks_path=TASKS, dataset_manifest_path=DATASETS,
            registry_path=self.registry, provider=DeepSeekProvider(),
            model_id="deepseek-v4-flash", task_id=self.task_id,
            confirm_online=True, max_turns=2,
        )
        arguments.update(overrides)
        return pilot.run_self_pilot_task(**arguments)

    def _cli_arguments(self):
        return [
            "self-pilot-run", "--session-dir", str(self.session),
            "--registry", str(self.registry), "--tasks", str(TASKS),
            "--datasets", str(DATASETS), "--provider", "deepseek",
            "--model", "deepseek-v4-flash", "--confirm-online", "--max-turns", "2",
        ]

    def test_cli_online_path_uses_real_ledger_after_plan_and_preserves_privacy(self):
        blinded_before = (self.session / "pilot_tasks.json").read_bytes()
        output = io.StringIO()
        error_output = io.StringIO()
        parsed = cli.build_parser().parse_args(self._cli_arguments())
        with (
            patch.object(cli, "build_parser", return_value=SimpleNamespace(parse_args=lambda: parsed)),
            mock_responses_transport(payload=response_payload(text="Synthetic aggregate result")) as calls,
            redirect_stdout(output), redirect_stderr(error_output),
        ):
            exit_code = cli.main()
        self.assertEqual(exit_code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "ran")
        self.assertEqual(result["completion_telemetry"]["attempts"], 1)
        self.assertEqual(result["completion_telemetry"]["usage"]["total_tokens"], 42)
        self.assertTrue(result["completion_telemetry"]["closure_passed"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.key_reads, 1)
        state = json.loads((self.session / "pilot_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["schema_version"], "1.2")
        self.assertEqual((self.session / "pilot_tasks.json").read_bytes(), blinded_before)
        self.assertEqual(state["records"][0]["provider_run"]["completion_status"], "complete")
        summaries, passed = telemetry_module.verify_session(self.session)
        self.assertTrue(passed)
        self.assertEqual(summaries[0]["attempts"], 1)
        for path in self.session.rglob("*"):
            if path.is_file():
                self.assertNotIn(FAKE_KEY.encode(), path.read_bytes())
                self.assertNotIn(str(self.session).encode(), path.read_bytes())
        self.assertNotIn(FAKE_KEY, output.getvalue() + error_output.getvalue())
        self.assertNotIn(str(self.session), output.getvalue() + error_output.getvalue())
        with self.assertRaises(EvalV2ContractError) as caught:
            self._run()
        self.assertEqual(caught.exception.code, "self_pilot_task_already_run")
        self.assertEqual(self.key_reads, 1)

    def test_web_online_path_uses_real_ledger_without_process_key(self):
        controller = self._web_controller()
        with mock_responses_transport(payload=response_payload(text="## English\nSynthetic answer\n## 中文\n合成回答")) as calls:
            state = controller.run_current_task()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.key_reads, 0)
        self.assertTrue(state["answer"]["completion_telemetry"]["closure_passed"])
        self.assertFalse(state["answer"]["controlled_failure"])
        self.assertIn("合成回答", state["answer"]["chinese"])
        with self.assertRaises(EvalV2ContractError) as caught:
            controller.run_current_task()
        self.assertEqual(caught.exception.code, "self_pilot_web_task_not_runnable")

    def _web_controller(self):
        return SelfPilotWebController(
            project_root=ROOT, session_directory=self.session,
            tasks_path=TASKS, dataset_manifest_path=DATASETS,
            registry_path=self.registry, translations_path=TRANSLATIONS,
            confirm_online=True, provider=DeepSeekProvider(),
            model_id="deepseek-v4-flash", api_key=FAKE_KEY, max_turns=2,
        )

    def test_web_truncated_response_has_failure_card_and_no_answer(self):
        controller = self._web_controller()
        with mock_responses_transport(payload=response_payload(status="incomplete", text="PRIVATE-WEB-BODY", output_tokens=10000)) as calls:
            state = controller.run_current_task()
        self.assertEqual(len(calls), 1)
        self.assertTrue(state["answer"]["controlled_failure"])
        self.assertEqual(state["answer"]["english"], "")
        self.assertNotIn("PRIVATE-WEB-BODY", json.dumps(state))
        with self.assertRaises(EvalV2ContractError):
            controller.run_current_task()

    def test_incomplete_response_is_not_displayed_and_cannot_rerun(self):
        with mock_responses_transport(payload=response_payload(status="incomplete", text="PRIVATE-INCOMPLETE-BODY", output_tokens=2000)) as calls:
            result = self._run()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "controlled_failure")
        self.assertEqual(result["agent_output"], "")
        self.assertNotIn("PRIVATE-INCOMPLETE-BODY", json.dumps(result))
        self.assertEqual(result["error_code"], "eval_v2_interactive_telemetry_incomplete")
        with self.assertRaises(EvalV2ContractError) as caught:
            self._run()
        self.assertEqual(caught.exception.code, "self_pilot_task_already_run")
        self.assertEqual(self.key_reads, 1)

    def test_same_task_in_separate_sessions_has_unique_case_and_run_ids(self):
        with mock_responses_transport():
            first = self._run()
        self.session = self.session.with_name("second-session")
        pilot.create_self_pilot_session(
            project_root=ROOT, output_directory=self.session,
            tasks_path=TASKS, dataset_manifest_path=DATASETS, task_count=1,
        )
        with mock_responses_transport():
            second = self._run()
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertNotEqual(first["completion_telemetry"]["case_id"], second["completion_telemetry"]["case_id"])
        self.assertNotEqual(first["completion_telemetry"]["run_id"], second["completion_telemetry"]["run_id"])
        self.assertLessEqual(len(first["completion_telemetry"]["case_id"]), 64)

    def test_plaintext_key_canary_in_complete_response_is_redacted(self):
        plaintext_key = "SYNTHETIC-PLAIN-INTERACTIVE-KEY-CANARY"
        output = io.StringIO()
        with (
            mock_responses_transport(payload=response_payload(text="Synthetic response " + plaintext_key)) as calls,
            redirect_stdout(output), redirect_stderr(output),
        ):
            result = self._run(api_key=plaintext_key)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result["completion_telemetry"]["closure_passed"])
        self.assertTrue(result["output_redacted"])
        self.assertEqual(result["agent_output"], "[OUTPUT_REDACTED_BY_SELF_PILOT_SAFETY_FILTER]")
        raw = json.dumps(result).encode() + output.getvalue().encode() + b"".join(
            path.read_bytes() for path in self.session.rglob("*") if path.is_file()
        )
        self.assertNotIn(plaintext_key.encode(), raw)

    def test_http_error_is_controlled_failure_without_retry(self):
        with mock_responses_transport(status_code=503, payload={"error": {"message": "synthetic failure", "type": "server_error"}}) as calls:
            result = self._run()
        self.assertEqual(result["status"], "controlled_failure")
        self.assertEqual(result["agent_output"], "")
        self.assertEqual(result["completion_telemetry"]["terminal_states"], ["http_error"])
        self.assertIsNone(result["completion_telemetry"]["usage"]["input_tokens"])
        self.assertEqual(len(calls), 1)
        with self.assertRaises(EvalV2ContractError):
            self._run()

    def test_cancelled_request_is_controlled_failure_without_retry(self):
        with mock_responses_transport(failure=asyncio.CancelledError()) as calls:
            result = self._run()
        self.assertEqual(result["status"], "controlled_failure")
        self.assertEqual(result["agent_output"], "")
        self.assertEqual(result["completion_telemetry"]["terminal_states"], ["cancelled"])
        self.assertTrue(result["completion_telemetry"]["all_terminal"])
        self.assertEqual(len(calls), 1)
        with self.assertRaises(EvalV2ContractError):
            self._run()

    def test_terminal_ledger_write_failure_suppresses_answer_and_retry(self):
        original = AuditLedger.append_completion_telemetry_event

        def fail_terminal(ledger, event_type, *args, **kwargs):
            if event_type != "model_request_started":
                raise AuditError("synthetic_write_failed", FAKE_KEY + " SYNTHETIC-QUESTION " + str(self.session))
            return original(ledger, event_type, *args, **kwargs)

        output = io.StringIO()
        with (
            mock_responses_transport(payload=response_payload(text="PRIVATE-FAILED-LEDGER-BODY")) as calls,
            patch.object(AuditLedger, "append_completion_telemetry_event", new=fail_terminal),
            redirect_stdout(output), redirect_stderr(output),
        ):
            result = self._run()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "controlled_failure")
        self.assertEqual(result["agent_output"], "")
        self.assertTrue(result["completion_telemetry"]["write_failed"])
        self.assertFalse(result["completion_telemetry"]["closure_passed"])
        self.assertEqual(result["completion_telemetry"]["attempts"], 1)
        serialized = json.dumps(result).encode() + output.getvalue().encode() + b"".join(
            path.read_bytes() for path in self.session.rglob("*") if path.is_file()
        )
        for canary in (FAKE_KEY, "SYNTHETIC-QUESTION", str(self.session), "PRIVATE-FAILED-LEDGER-BODY"):
            self.assertNotIn(canary.encode(), serialized)
        with self.assertRaises(EvalV2ContractError):
            self._run()

    def test_source_rejection_precedes_key_and_provider(self):
        with (
            patch.object(telemetry_module, "_verify_loaded_sources", side_effect=EvalV2ContractError("eval_v2_interactive_source_required", "仓库来源不符。")),
            patch.object(DeepSeekProvider, "open_model", side_effect=AssertionError("must not open provider")),
            self.assertRaises(EvalV2ContractError) as caught,
        ):
            self._run()
        self.assertEqual(caught.exception.code, "eval_v2_interactive_source_required")
        self.assertEqual(self.key_reads, 0)

    def test_unauthorized_dataset_rejected_before_claim_key_and_provider(self):
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        current = pilot.get_next_self_pilot_task(project_root=ROOT, session_directory=self.session)
        dataset_id = current["task"]["context"]["dataset_id"]
        registry["entries"] = [entry for entry in registry["entries"] if entry["dataset_id"] != dataset_id]
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        with (
            patch.object(pilot, "_prepare_interactive_case", side_effect=AssertionError("must not prepare")),
            patch.object(DeepSeekProvider, "open_model", side_effect=AssertionError("must not open provider")),
            self.assertRaises(EvalV2ContractError) as caught,
        ):
            self._run()
        self.assertEqual(caught.exception.code, "eval_v2_dataset_not_authorized")
        self.assertEqual(self.key_reads, 0)

    def test_modified_dataset_rejected_before_claim_key_and_provider(self):
        current = pilot.get_next_self_pilot_task(project_root=ROOT, session_directory=self.session)
        dataset_id = current["task"]["context"]["dataset_id"]
        (self.registry.parent / f"{dataset_id}.csv").write_text("value\nchanged\n", encoding="utf-8")
        with (
            patch.object(pilot, "_prepare_interactive_case", side_effect=AssertionError("must not prepare")),
            patch.object(DeepSeekProvider, "open_model", side_effect=AssertionError("must not open provider")),
            self.assertRaises(EvalV2ContractError) as caught,
        ):
            self._run()
        self.assertEqual(caught.exception.code, "eval_v2_prepared_dataset_tampered")
        self.assertEqual(self.key_reads, 0)

    def test_dirty_tree_rejection_precedes_key_and_provider(self):
        with (
            patch.object(telemetry_module, "_verify_clean_commit", side_effect=EvalV2ContractError("eval_v2_interactive_dirty_tree", "请提交代码改动。")),
            patch.object(DeepSeekProvider, "open_model", side_effect=AssertionError("must not open provider")),
            self.assertRaises(EvalV2ContractError) as caught,
        ):
            self._run()
        self.assertEqual(caught.exception.code, "eval_v2_interactive_dirty_tree")
        self.assertEqual(self.key_reads, 0)

    def test_cli_does_not_read_key_when_plan_cannot_be_committed(self):
        parsed = cli.build_parser().parse_args(self._cli_arguments())
        output = io.StringIO()
        with (
            patch.object(cli, "build_parser", return_value=SimpleNamespace(parse_args=lambda: parsed)),
            patch.object(pilot, "_prepare_interactive_case", side_effect=EvalV2ContractError("eval_v2_interactive_telemetry_incomplete", "计划未写入。")),
            patch.object(DeepSeekProvider, "open_model", side_effect=AssertionError("must not open provider")),
            redirect_stdout(output),
        ):
            exit_code = cli.main()
        self.assertEqual(exit_code, 4)
        self.assertEqual(self.key_reads, 0)
        self.assertEqual(json.loads(output.getvalue())["error_code"], "eval_v2_interactive_telemetry_incomplete")

    def test_cli_confirmation_rejection_precedes_preparation_and_key(self):
        with (
            patch.object(pilot, "_prepare_interactive_case", side_effect=AssertionError("must not prepare")),
            self.assertRaises(EvalV2ContractError) as caught,
        ):
            self._run(confirm_online=False)
        self.assertEqual(caught.exception.code, "eval_v2_online_confirmation_required")
        self.assertEqual(self.key_reads, 0)


if __name__ == "__main__":
    unittest.main()
