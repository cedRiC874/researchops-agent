from __future__ import annotations

import asyncio
import hashlib
import http.client
import io
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx

from researchops import eval_v2_interactive_telemetry as telemetry
from researchops.audit import AuditError, AuditLedger
from researchops.model_providers import DeepSeekProvider
from researchops_expert_review import __main__ as cli, core, web
from tests.test_eval_v2_interactive_executor import mock_responses_transport, response_payload


ROOT = Path(__file__).resolve().parents[3]
DATASET = "synthetic_interactive_service"
KEY = "SYNTHETIC-SERVICE-CREDENTIAL-CANARY"
QUESTION = "Summarize the aggregate count. SYNTHETIC-SERVICE-QUESTION-CANARY"
ANSWER = "## English\nThe dataset contains two rows.\n## 中文\n数据集包含两行。"


class InteractiveServiceTests(unittest.TestCase):
    def setUp(self):
        connect = socket.socket.connect

        def local_only(sock, address):
            if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
                raise AssertionError("Offline test forbids external sockets")
            return connect(sock, address)

        guard = patch.object(socket.socket, "connect", local_only)
        guard.start()
        self.addCleanup(guard.stop)
        artifacts = ROOT / "artifacts"
        artifacts.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts, prefix="interactive-service-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.registry = self._registry()
        self.session = self.directory / "session"
        core.create_session(project_root=ROOT, output_directory=self.session)
        clean = patch.object(telemetry, "_verify_clean_commit", return_value="a" * 40)
        clean.start()
        self.addCleanup(clean.stop)

    def _registry(self):
        data = self.directory / (DATASET + ".csv")
        data.write_text("value,group\n1,A\n2,B\n", encoding="utf-8")
        path = self.directory / "logical_dataset_registry.json"
        entry = {
            "dataset_id": DATASET, "relative_path": data.name,
            "prepared_sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
            "prepared_bytes": data.stat().st_size, "row_count": 2, "column_count": 2,
            "source_asset_sha256": "b" * 64, "preparation_version": "1.0",
            "privacy_class": "public_test_data", "model_access": "aggregate_tools_only",
            "domain": "offline_fixture", "repeated_subjects": False,
            "analysis_boundaries": ["test_only", "no_row_access"], "transformations": ["offline_fixture"],
        }
        path.write_text(json.dumps({"schema_version": "1.0", "registry_id": "interactive-service-test",
                                    "dataset_manifest_sha256": "c" * 64, "entries": [entry]}), encoding="utf-8")
        return path

    def review(self):
        return core.ExpertReview(project_root=ROOT, session_directory=self.session,
                                 registry_path=self.registry, provider_id="deepseek", model_id="deepseek-v4-flash")

    @contextmanager
    def online(self, **transport):
        # Never read a real credential, including when SDK code checks the environment.
        with patch.object(core.os.environ, "get", side_effect=lambda name, default=None: KEY if name == "DEEPSEEK_API_KEY" else default):
            with mock_responses_transport(**transport) as calls:
                yield calls

    def assert_private(self, output=""):
        raw = b"\n".join(path.read_bytes() for path in self.session.rglob("*") if path.is_file())
        raw += output.encode("utf-8")
        for canary in (KEY, QUESTION, str(self.directory), str(self.registry)):
            self.assertNotIn(canary.encode("utf-8"), raw)

    def test_cli_real_ledger_and_controlled_responses_model_complete_once(self):
        question_file = self.directory / "question.txt"
        question_file.write_text(QUESTION, encoding="utf-8")
        output = io.StringIO()
        with self.online(payload=response_payload(text=ANSWER)) as calls, redirect_stdout(output), redirect_stderr(output):
            result = cli.main([
                "ask", "--session-dir", str(self.session), "--registry", str(self.registry),
                "--dataset-id", DATASET, "--question-file", str(question_file),
                "--provider", "deepseek", "--model", "deepseek-v4-flash", "--confirm-online",
            ])
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("模型调用次数：1；是否完整：是", output.getvalue())
        self.assertIn("合计：42", output.getvalue())
        record = self.review()._records()[0]
        self.assertEqual(record["completion_status"], "complete")
        self.assertTrue(record["completion_telemetry"]["closure_passed"])
        self.assertEqual(record["completion_telemetry"]["terminal_states"], ["completed"])
        with patch.object(core.os.environ, "get", side_effect=AssertionError("No repeat key read")):
            with self.assertRaises(core.ExpertReviewError) as raised:
                self.review().ask(confirm_online=True, dataset_id=DATASET, question=QUESTION)
        self.assertEqual(raised.exception.code, "question_already_run")
        verified, complete = telemetry.verify_session(self.session)
        self.assertTrue(complete)
        self.assertEqual(verified[0]["usage"], {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42})
        self.assert_private(output.getvalue())

    def test_web_real_online_path_displays_model_summary_and_rejects_repeat(self):
        review = self.review()
        server = ThreadingHTTPServer(("127.0.0.1", 0), web._build_handler(review, "test-token", 1))
        port = server.server_address[1]
        server.RequestHandlerClass = web._build_handler(review, "test-token", port)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()

        def submit():
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("POST", "/api/ask", json.dumps({
                    "confirm_online": True, "dataset_id": DATASET, "question": QUESTION,
                }), {"Content-Type": "application/json", "X-ResearchOps-Token": "test-token"})
                response = connection.getresponse()
                return response.status, response.read().decode()
            finally:
                connection.close()

        try:
            with self.online(payload=response_payload(text=ANSWER)) as calls:
                status, body = submit()
                repeated_status, repeated = submit()
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["status"], "完成")
            self.assertIn("模型调用次数：1；是否完整：是", json.loads(body)["completion_summary"])
            self.assertEqual(repeated_status, 400)
            self.assertIn("question_already_run", repeated)
            self.assertEqual(len(calls), 1)
            self.assert_private(body + repeated)
            self.assertTrue(telemetry.verify_session(self.session)[1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_plan_is_persisted_before_key_read_or_provider_open(self):
        observed = []

        def key(name, default=None):
            if name != "DEEPSEEK_API_KEY":
                return default
            databases = list((self.session / "interactive-telemetry").glob("*.sqlite3"))
            self.assertEqual(len(databases), 1)
            with sqlite3.connect(databases[0]) as db:
                events = [row[0] for row in db.execute("SELECT event_type FROM audit_events")]
            self.assertIn("interactive_plan_committed", events)
            observed.append("key_after_plan")
            return KEY

        open_model = DeepSeekProvider.open_model

        def opened(provider, **kwargs):
            self.assertEqual(observed, ["key_after_plan"])
            observed.append("provider_after_key")
            return open_model(provider, **kwargs)

        with patch.object(core.os.environ, "get", side_effect=key), patch.object(DeepSeekProvider, "open_model", opened):
            with mock_responses_transport(payload=response_payload(text=ANSWER)):
                result = self.review().ask(confirm_online=True, dataset_id=DATASET, question=QUESTION)
        self.assertEqual(result["status"], "完成")
        self.assertEqual(observed, ["key_after_plan", "provider_after_key"])

    def test_source_and_dirty_commit_rejections_do_not_read_key(self):
        for index, (function, code) in enumerate((
            ("_verify_loaded_sources", "eval_v2_interactive_source_required"),
            ("_verify_clean_commit", "eval_v2_interactive_dirty_tree"),
        )):
            # The environment can change after the initial read-only preflight;
            # private preparation must still repeat its original admission gate.
            with self.subTest(code=code), patch.object(core, "preflight_interactive_environment", return_value={"git_commit": "a" * 40}), patch.object(telemetry, function, side_effect=telemetry.InteractiveTelemetryError(code, "源码准入拒绝。")):
                with patch.object(core.os.environ, "get", side_effect=AssertionError("No key read")) as key:
                    with patch.object(DeepSeekProvider, "open_model") as opened:
                        record = self.review().ask(confirm_online=True, dataset_id=DATASET, question=QUESTION + str(index))
                self.assertEqual(record["error_code"], code)
                self.assertEqual(record["status"], "出错")
                key.assert_not_called()
                opened.assert_not_called()

    def test_incomplete_http_no_response_and_cancelled_are_controlled_failures_without_retry(self):
        failures = (
            {"payload": response_payload(status="incomplete", output_tokens=10000, text=ANSWER)},
            {"status_code": 503, "payload": {"error": {"message": KEY, "type": "service_unavailable"}}},
            {"failure": httpx.ConnectError(KEY)},
            {"failure": asyncio.CancelledError()},
        )
        for index, transport in enumerate(failures):
            with self.subTest(index=index):
                question = QUESTION + str(index)
                with self.online(**transport) as calls:
                    record = self.review().ask(confirm_online=True, dataset_id=DATASET, question=question)
                self.assertEqual(len(calls), 1)
                self.assertEqual(record["status"], "出错")
                self.assertEqual(record["outcome"], "controlled_failure")
                self.assertNotIn(ANSWER, json.dumps(record, ensure_ascii=False))
                self.assertEqual(record["error_code"], "eval_v2_interactive_telemetry_incomplete")
                self.assertEqual(record["completion_telemetry"]["attempts"], 1)
                with self.assertRaises(core.ExpertReviewError) as raised:
                    self.review().ask(confirm_online=True, dataset_id=DATASET, question=question)
                self.assertEqual(raised.exception.code, "question_already_run")
        self.assert_private()

    def test_terminal_ledger_write_failure_never_displays_normal_answer(self):
        append = AuditLedger.append_completion_telemetry_event

        def fail_terminal(ledger, event_type, payload, **kwargs):
            if event_type.startswith("model_response_telemetry_"):
                raise AuditError("synthetic_write_failure", "Private error must not escape " + KEY)
            return append(ledger, event_type, payload, **kwargs)

        with self.online(payload=response_payload(text=ANSWER)) as calls, patch.object(AuditLedger, "append_completion_telemetry_event", fail_terminal):
            record = self.review().ask(confirm_online=True, dataset_id=DATASET, question=QUESTION)
        self.assertEqual(len(calls), 1)
        self.assertEqual(record["status"], "出错")
        self.assertTrue(record["completion_telemetry"]["write_failed"])
        self.assertFalse(record["completion_telemetry"]["closure_passed"])
        self.assertNotIn(ANSWER, json.dumps(record, ensure_ascii=False))
        self.assert_private()


if __name__ == "__main__":
    unittest.main()
