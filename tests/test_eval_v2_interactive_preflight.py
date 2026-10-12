from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from researchops import eval_v2_interactive_telemetry as telemetry
from researchops.audit import AuditLedger
from researchops.model_providers import DeepSeekProvider, OpenAIProvider
from tests.test_completion_telemetry_ledger import _capture


ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = "synthetic-preflight-only-key"


class InteractivePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="交互预检-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        for target in ("socket.socket.connect", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("offline test forbids sockets and DNS"))
            guard.start()
            self.addCleanup(guard.stop)
        safe_environment = {name: os.environ[name] for name in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT") if name in os.environ}
        safe_environment["DEEPSEEK_API_KEY"] = FAKE_KEY
        fake = patch.object(os, "environ", safe_environment)
        fake.start()
        self.addCleanup(fake.stop)

    def prepare(self, case_id="PREFLIGHT-001"):
        with patch.object(telemetry, "_verify_loaded_sources"), patch.object(telemetry, "_verify_clean_commit", return_value="b" * 40):
            return telemetry._prepare_interactive_case(root=ROOT, session_dir=self.directory,
                case_id=case_id, provider=DeepSeekProvider(), model_id="deepseek-v4-flash", max_turns=3)

    def complete(self, owner, *, failed=False):
        attempt = owner.session.begin_attempt()
        owner.session.finalize_response_accepted(attempt, _capture())
        owner.session._record_sdk_result(SimpleNamespace(raw_responses=[SimpleNamespace(usage=None)],
            context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=1))))
        owner._failed = failed
        return owner.finish(completion_status="complete")

    def run_verify(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = telemetry.main(["verify", "--session-dir", str(self.directory)])
        self.assertNotIn(str(self.directory), output.getvalue())
        self.assertNotIn(FAKE_KEY, output.getvalue())
        return code, json.loads(output.getvalue())

    def test_preflight_checks_in_order_without_key_file_or_binding(self):
        order = []
        class NoKeyEnvironment(dict):
            def __getitem__(self, name):
                if name == "DEEPSEEK_API_KEY":
                    raise AssertionError("preflight must not read Key")
                return super().__getitem__(name)
            def get(self, name, default=None):
                return self[name] if name in self else default
        with patch.object(os, "environ", NoKeyEnvironment(os.environ)), patch.object(
            telemetry, "_verify_loaded_sources", side_effect=lambda *a, **k: order.append("source")
        ), patch.object(telemetry, "_verify_clean_commit", side_effect=lambda *a: order.append("git") or "b" * 40), patch.object(
            telemetry, "_binding_for_claim", side_effect=AssertionError("preflight cannot mint")
        ), patch.object(AuditLedger, "__init__", side_effect=AssertionError("preflight cannot write")):
            result = telemetry.preflight_interactive_environment(root=ROOT, provider=DeepSeekProvider(),
                model_id="deepseek-v4-flash", expert_review=True)
        self.assertEqual(order, ["source", "git"])
        self.assertEqual(result, {"git_commit": "b" * 40, "provider": "deepseek",
                                  "model": "deepseek-v4-flash", "module_version": telemetry.MODULE_VERSION})
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_provider_and_model_reject_before_source_or_git(self):
        for provider, model in ((OpenAIProvider(), "deepseek-v4-flash"), (DeepSeekProvider(), "unknown")):
            with self.subTest(provider=provider.provider_id), patch.object(telemetry, "_verify_loaded_sources") as source, patch.object(telemetry, "_verify_clean_commit") as git:
                with self.assertRaises(telemetry.InteractiveTelemetryError) as caught:
                    telemetry.preflight_interactive_environment(root=ROOT, provider=provider, model_id=model)
                self.assertFalse(caught.exception.case_reserved)
                self.assertIsNone(caught.exception.telemetry_summary)
                source.assert_not_called()
                git.assert_not_called()

    def test_cli_preflight_success_and_safe_failure(self):
        output = io.StringIO()
        with patch.object(telemetry, "_verify_loaded_sources") as sources, patch.object(telemetry, "_verify_clean_commit", return_value="b" * 40), contextlib.redirect_stdout(output):
            self.assertEqual(telemetry.main(["preflight", "--expert-review"]), 0)
        sources.assert_called_once_with(ROOT, expert_review=True)
        self.assertEqual(json.loads(output.getvalue())["git_commit"], "b" * 40)
        output = io.StringIO()
        with patch.object(telemetry, "_verify_loaded_sources") as sources, contextlib.redirect_stdout(output):
            self.assertEqual(telemetry.main(["preflight", "--provider", "openai"]), 2)
        sources.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["error_code"], "eval_v2_interactive_provider_denied")
        self.assertNotIn(FAKE_KEY, output.getvalue())

    def test_chinese_git_path_clean_commit_ledger_finish_and_sqlite_uri(self):
        git_root = self.directory / "中文仓库"
        git_root.mkdir()
        def git(*args):
            result = subprocess.run(["git", "-C", str(git_root), *args], capture_output=True)
            self.assertEqual(result.returncode, 0)
        git("init")
        (git_root / "src").mkdir()
        (git_root / "src/synthetic.py").write_text("VALUE = 1\n", encoding="utf-8")
        git("add", "src/synthetic.py")
        git("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid", "commit", "-m", "synthetic")
        before = {p.relative_to(git_root).as_posix(): p.read_bytes() for p in git_root.rglob("*") if p.is_file()}
        with patch.object(telemetry, "_verify_loaded_sources"):
            checked = telemetry.preflight_interactive_environment(root=git_root, provider=DeepSeekProvider(), model_id="deepseek-v4-flash")
        after = {p.relative_to(git_root).as_posix(): p.read_bytes() for p in git_root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertRegex(checked["git_commit"], r"^[a-f0-9]{40}$")
        session = git_root / "会话目录"
        session.mkdir()
        offline = telemetry.mapping.load_and_select_surface_mapping(ROOT, "deepseek", "responses", "openai_compatible_responses", purpose="offline_validation")
        with patch.object(telemetry, "_verify_loaded_sources"), patch.object(telemetry.mapping, "load_and_select_surface_mapping", return_value=offline):
            owner = telemetry._prepare_interactive_case(root=git_root, session_dir=session,
                case_id="CHINESE-PATH-001", provider=DeepSeekProvider(), model_id="deepseek-v4-flash", max_turns=3)
        self.assertEqual(owner.commit, checked["git_commit"])
        self.assertTrue(self.complete(owner)["normal_result"])
        rows, _ = telemetry._read_rows(owner.database)
        self.assertTrue(rows)
        cases, valid = telemetry.verify_session(session)
        self.assertTrue(valid)
        self.assertTrue(cases[0]["ledger_intact"])
        self.assertTrue(cases[0]["case_closed"])
        self.assertEqual(cases[0]["git_commit"], checked["git_commit"])

    def test_receipt_summary_cannot_override_trusted_case_ordinal(self):
        owner = self.prepare("PREFLIGHT-ORDINAL")
        original = telemetry._summarize_rows
        canary = "UNTRUSTED-RECEIPT-TEXT-CANARY"
        def extra_field(*args, **kwargs):
            return {**original(*args, **kwargs), "case_ordinal": canary}
        # Model an extra persisted summary field with internally consistent
        # event/receipt hashes; output must still use the reader's ordinal.
        with patch.object(telemetry, "_summarize_rows", side_effect=extra_field):
            self.assertTrue(self.complete(owner)["normal_result"])
        cases, complete = telemetry.verify_session(self.directory)
        self.assertTrue(complete)
        self.assertEqual(cases[0]["case_ordinal"], 1)
        self.assertNotIn(canary, json.dumps(cases))

    def test_setup_failures_after_reservation_have_readonly_safe_summary(self):
        for target in ("__init__", "start_run", "append_event"):
            case_id = "FAIL-" + target.strip("_")
            with self.subTest(stage=target), patch.object(AuditLedger, target, side_effect=OSError("PRIVATE-CANARY-PATH-KEY")):
                with self.assertRaises(telemetry.InteractiveTelemetryError) as caught:
                    self.prepare(case_id)
            error = caught.exception
            self.assertTrue(error.case_reserved)
            self.assertTrue(error.telemetry_summary["write_failed"])
            self.assertFalse(error.telemetry_summary["normal_result"])
            self.assertNotIn("PRIVATE-CANARY-PATH-KEY", str(error) + json.dumps(error.telemetry_summary))
            with self.assertRaises(AttributeError):
                error.case_reserved = False
            with self.assertRaises(AttributeError):
                error.telemetry_summary = {}
            copy = error.telemetry_summary
            copy["usage"]["input_tokens"] = 99
            self.assertIsNone(error.telemetry_summary["usage"]["input_tokens"])
            with self.assertRaises(telemetry.InteractiveTelemetryError) as repeated:
                self.prepare(case_id)
            self.assertTrue(repeated.exception.case_reserved)
            self.assertEqual(repeated.exception.code, "eval_v2_interactive_case_already_run")

    def test_failure_before_file_creation_does_not_claim_reservation(self):
        with patch.object(telemetry, "_verify_loaded_sources", side_effect=telemetry._error("source", "来源错误")):
            with self.assertRaises(telemetry.InteractiveTelemetryError) as caught:
                telemetry._prepare_interactive_case(root=ROOT, session_dir=self.directory, case_id="BEFORE-FILE",
                    provider=DeepSeekProvider(), model_id="deepseek-v4-flash", max_turns=3)
        self.assertFalse(caught.exception.case_reserved)
        self.assertIsNone(caught.exception.telemetry_summary)
        (self.directory / "interactive-telemetry").write_text("synthetic blocker", encoding="utf-8")
        with self.assertRaises(telemetry.InteractiveTelemetryError) as caught:
            self.prepare()
        self.assertFalse(caught.exception.case_reserved)
        self.assertIsNone(caught.exception.telemetry_summary)

    def test_existing_failed_owner_cannot_become_normal_on_complete_status(self):
        owner = self.prepare()
        summary = self.complete(owner, failed=True)
        self.assertTrue(summary["closure_passed"])
        self.assertFalse(summary["normal_result"])
        cases, old_boolean = telemetry.verify_session(self.directory)
        self.assertTrue(old_boolean)
        self.assertTrue(cases[0]["ledger_intact"])
        self.assertTrue(cases[0]["case_closed"])
        self.assertEqual(self.run_verify()[0], 1)

    def test_closed_success_and_intact_failed_case_have_distinct_exit_codes(self):
        owner = self.prepare()
        self.complete(owner)
        self.assertEqual(self.run_verify()[0], 0)
        self.assertEqual(self.run_verify()[1]["cases"][0]["integrity_reason"], "ok")
        failed = self.prepare("HTTP-FAIL")
        attempt = failed.session.begin_attempt()
        failed.session.finalize_http_error(attempt, "provider_failed")
        failed.finish(completion_status="provider_failed")
        code, output = self.run_verify()
        self.assertEqual(code, 1)
        self.assertTrue(all(case["ledger_intact"] for case in output["cases"]))
        self.assertTrue(any(not case["case_closed"] for case in output["cases"]))

    def test_missing_receipt_and_tampered_ledger_exit_two(self):
        owner = self.prepare()
        owner.session.begin_attempt()
        code, output = self.run_verify()
        self.assertEqual(code, 2)
        self.assertFalse(output["cases"][0]["ledger_intact"])
        self.assertEqual(output["cases"][0]["attempts"], 1)
        self.assertEqual(output["cases"][0]["integrity_reason"], "receipt_missing_unsealed")
        owner.finish(completion_status="failed")
        with contextlib.closing(sqlite3.connect(owner.database)) as database:
            with database:
                database.execute("PRAGMA user_version=123")
        self.assertEqual(self.run_verify()[0], 2)

    def test_missing_sealed_receipt_has_fixed_reason(self):
        owner = self.prepare()
        self.complete(owner)
        owner.database.with_suffix(".json").unlink()
        code, output = self.run_verify()
        self.assertEqual(code, 2)
        self.assertEqual(output["cases"][0]["integrity_reason"], "receipt_missing_sealed")

    def test_modified_event_chain_has_fixed_reason(self):
        owner = self.prepare()
        self.complete(owner)
        with contextlib.closing(sqlite3.connect(owner.database)) as database:
            with database:
                database.execute("DROP TRIGGER audit_events_no_update")
                database.execute("UPDATE audit_events SET safe_payload_json='{}' WHERE event_type='model_request_started'")
        code, output = self.run_verify()
        self.assertEqual(code, 2)
        self.assertEqual(output["cases"][0]["integrity_reason"], "chain_invalid")

    def test_unchanged_chain_changed_database_has_hash_mismatch_reason(self):
        owner = self.prepare()
        self.complete(owner)
        with contextlib.closing(sqlite3.connect(owner.database)) as database:
            with database:
                database.execute("PRAGMA user_version=456")
        code, output = self.run_verify()
        self.assertEqual(code, 2)
        self.assertEqual(output["cases"][0]["integrity_reason"], "hash_mismatch")

    def test_unreadable_database_has_fixed_reason(self):
        owner = self.prepare()
        self.complete(owner)
        owner.database.write_bytes(b"synthetic invalid SQLite database")
        code, output = self.run_verify()
        self.assertEqual(code, 2)
        self.assertEqual(output["cases"][0]["integrity_reason"], "unreadable")

    def test_missing_plan_has_other_integrity_failure_reason(self):
        directory = self.directory / "interactive-telemetry"
        directory.mkdir()
        ledger = AuditLedger(directory / "synthetic-no-plan.sqlite3")
        ledger.start_run(mode=telemetry.RUNTIME_SCOPE, request_summary={"kind": "synthetic"})
        code, output = self.run_verify()
        self.assertEqual(code, 2)
        self.assertEqual(output["cases"][0]["integrity_reason"], "other_integrity_failure")

    def test_old_module_receipt_remains_readable_without_rewrite(self):
        with patch.object(telemetry, "MODULE_VERSION", "eval-v2-interactive-telemetry/1.0"):
            owner = self.prepare()
            self.complete(owner)
        receipt = owner.database.with_suffix(".json")
        before = receipt.read_bytes()
        cases, valid = telemetry.verify_session(self.directory)
        self.assertTrue(valid)
        self.assertEqual(cases[0]["module_version"], "eval-v2-interactive-telemetry/1.0")
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(self.run_verify()[0], 0)


if __name__ == "__main__":
    unittest.main()
