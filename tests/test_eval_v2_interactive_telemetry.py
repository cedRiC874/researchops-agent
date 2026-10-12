from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from researchops import eval_v2_interactive_telemetry as telemetry
from researchops.audit import AuditLedger
from researchops.model_providers import DeepSeekProvider
from tests.test_completion_telemetry_ledger import _capture

ROOT = Path(__file__).resolve().parents[1]


class InteractiveTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def prepare(self, case_id="CASE-001"):
        with patch.object(telemetry, "_verify_loaded_sources"), patch.object(telemetry, "_verify_clean_commit", return_value="a" * 40):
            return telemetry._prepare_interactive_case(root=ROOT, session_dir=self.directory,
                case_id=case_id, provider=DeepSeekProvider(), model_id="deepseek-v4-flash", max_turns=3)

    def complete(self, owner, status="completed"):
        handle = owner.session.begin_attempt()
        owner.session.finalize_response_accepted(handle, _capture(status))
        owner.session._record_sdk_result(SimpleNamespace(raw_responses=[SimpleNamespace(usage=None)],
            context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=1))))
        return owner.finish(completion_status="complete" if status == "completed" else "output_truncated")

    def test_real_ledger_closes_and_readonly_replay_matches(self):
        owner = self.prepare()
        summary = self.complete(owner)
        self.assertTrue(summary["closure_passed"])
        self.assertTrue(summary["normal_result"])
        self.assertEqual(summary["usage"], {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
        self.assertEqual(summary["terminal_states"], ["completed"])
        rows, _ = telemetry._read_rows(owner.database)
        self.assertIn("model_request_started", {row["event_type"] for row in rows})
        self.assertIn("model_response_telemetry_recorded", {row["event_type"] for row in rows})
        before = {p.name: p.read_bytes() for p in owner.database.parent.iterdir()}
        with patch.object(AuditLedger, "__init__", side_effect=AssertionError("read-only")), patch.object(telemetry, "_binding_for_claim", side_effect=AssertionError("no runtime authority")):
            cases, ok = telemetry.verify_session(self.directory)
        self.assertTrue(ok, cases)
        after = {p.name: p.read_bytes() for p in owner.database.parent.iterdir()}
        self.assertEqual(before, after)
        with self.assertRaisesRegex(Exception, "已封存或失败"):
            owner.session.assert_provider_telemetry_authority()

    def test_truncated_capture_is_not_normal_answer(self):
        owner = self.prepare()
        summary = self.complete(owner, "incomplete")
        self.assertEqual(summary["terminal_states"], ["incomplete_length"])
        self.assertTrue(summary["closure_passed"])
        self.assertFalse(summary["normal_result"])

    def test_pending_attempt_is_detectable_and_cannot_be_reused(self):
        owner = self.prepare()
        owner.session.begin_attempt()
        cases, ok = telemetry.verify_session(self.directory)
        self.assertFalse(ok)
        self.assertEqual(cases[0]["attempts"], 1)
        self.assertFalse(cases[0]["all_terminal"])
        with self.assertRaises(telemetry.InteractiveTelemetryError) as raised:
            self.prepare()
        self.assertEqual(raised.exception.code, "eval_v2_interactive_case_already_run")
        summary = owner.finish(completion_status="failed")
        self.assertFalse(summary["closure_passed"])
        self.assertFalse(summary["write_failed"])

    def test_failure_terminal_preserves_unknown_usage_and_no_retry(self):
        for terminal in ("http_error", "no_response", "cancelled"):
            with self.subTest(terminal=terminal):
                owner = self.prepare("CASE-" + terminal)
                handle = owner.session.begin_attempt()
                method = getattr(owner.session, "finalize_" + terminal)
                method(handle, *(() if terminal == "cancelled" else ("provider_failed",)))
                with self.assertRaises(Exception):
                    owner.session.begin_attempt()
                summary = owner.finish(completion_status="provider_failed")
                self.assertTrue(summary["all_terminal"])
                self.assertFalse(summary["closure_passed"])
                self.assertEqual(summary["usage"], dict(input_tokens=None, output_tokens=None, total_tokens=None))

    def test_write_failure_is_controlled_and_never_retried(self):
        owner = self.prepare()
        with patch.object(AuditLedger, "append_completion_telemetry_event", side_effect=OSError("synthetic-secret-path-canary")):
            with self.assertRaises(OSError):
                owner.session.begin_attempt()
        summary = owner.finish(completion_status="provider_failed")
        self.assertTrue(summary["write_failed"])
        self.assertFalse(summary["normal_result"])
        self.assertNotIn("synthetic-secret-path-canary", json.dumps(summary))

    def test_tampered_chain_fails_replay(self):
        owner = self.prepare()
        self.complete(owner)
        with contextlib.closing(sqlite3.connect(owner.database)) as database:
            with database:
                database.execute("DROP TRIGGER audit_events_no_update")
                database.execute("UPDATE audit_events SET safe_payload_json='{}' WHERE event_type='model_request_started'")
        _, ok = telemetry.verify_session(self.directory)
        self.assertFalse(ok)

    def test_tampered_receipt_fails_replay(self):
        owner = self.prepare()
        self.complete(owner)
        path = owner.database.with_suffix(".json")
        receipt = json.loads(path.read_text())
        receipt["payload"]["summary"]["attempts"] = 2
        path.write_text(json.dumps(receipt))
        self.assertFalse(telemetry.verify_session(self.directory)[1])

    def test_corrupt_metadata_is_never_echoed_and_cli_exit_is_nonzero(self):
        owner = self.prepare()
        self.complete(owner)
        canary = "PRIVATE QUESTION AND LOCAL PATH CANARY"
        with contextlib.closing(sqlite3.connect(owner.database)) as database:
            with database:
                database.execute("DROP TRIGGER audit_events_no_update")
                payload = dict(owner.metadata, git_commit=canary, case_id=canary, module_version=canary)
                database.execute("UPDATE audit_events SET safe_payload_json=? WHERE event_type='interactive_plan_committed'", (json.dumps(payload),))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(telemetry.main(["verify", "--session-dir", str(self.directory)]), 2)
        self.assertNotIn(canary, output.getvalue())

    def test_normal_cli_exit_is_zero(self):
        self.complete(self.prepare())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(telemetry.main(["verify", "--session-dir", str(self.directory)]), 0)

    def test_readonly_cli_exit_codes_and_no_key_access(self):
        owner = self.prepare()
        owner.session.begin_attempt()
        output = io.StringIO()
        original = os._Environ.__getitem__
        def guarded(environment, name):
            if name == "DEEPSEEK_API_KEY":
                raise AssertionError("no key access")
            return original(environment, name)
        with patch.object(os._Environ, "__getitem__", guarded), contextlib.redirect_stdout(output):
            self.assertEqual(telemetry.main(["verify", "--session-dir", str(self.directory)]), 2)
        self.assertNotIn(str(self.directory), output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_sources_are_resolved_and_old_install_is_rejected(self):
        telemetry._verify_loaded_sources(ROOT, expert_review=False)
        with patch.dict("sys.modules", {"researchops": SimpleNamespace(__file__=self.directory / "site-packages/researchops/__init__.py")}):
            with self.assertRaises(telemetry.InteractiveTelemetryError) as raised:
                telemetry._verify_loaded_sources(ROOT, expert_review=False)
        self.assertEqual(raised.exception.code, "eval_v2_interactive_source_required")
        self.assertNotIn(str(self.directory), str(raised.exception))

    def test_dirty_tracked_and_untracked_paths_rejected(self):
        def git(*args):
            result = subprocess.run(["git", "-C", str(self.directory), *args], capture_output=True)
            self.assertEqual(result.returncode, 0)
        git("init")
        (self.directory / "src").mkdir()
        (self.directory / "src/base.py").write_text("x=1")
        git("add", "src/base.py")
        git("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid", "commit", "-m", "synthetic")
        self.assertRegex(telemetry._verify_clean_commit(self.directory), r"^[a-f0-9]{40}$")
        for index in range(12):
            (self.directory / "src" / f"new{index:02d}.py").write_text("x=1")
        (self.directory / "src/base.py").write_text("x=2")
        with self.assertRaises(telemetry.InteractiveTelemetryError) as raised:
            telemetry._verify_clean_commit(self.directory)
        self.assertEqual(raised.exception.code, "eval_v2_interactive_dirty_tree")
        self.assertNotIn(str(self.directory), str(raised.exception))
        self.assertEqual(str(raised.exception).count('"src/'), 10)

    def test_admission_order_rejects_without_reading_key(self):
        with patch.object(telemetry, "_verify_loaded_sources", side_effect=telemetry._error("source", "来源错误")) as source, patch.object(telemetry, "_verify_clean_commit") as commit, patch.object(AuditLedger, "start_run") as start:
            with self.assertRaises(telemetry.InteractiveTelemetryError):
                telemetry._prepare_interactive_case(root=ROOT, session_dir=self.directory, case_id="CASE-001", provider=DeepSeekProvider(), model_id="deepseek-v4-flash", max_turns=3)
            source.assert_called_once()
            commit.assert_not_called()
            start.assert_not_called()

    def test_only_deepseek_allowlist_is_supported(self):
        for provider, model in ((SimpleNamespace(provider_id="openai"), "gpt-4"), (SimpleNamespace(provider_id="kimi"), "kimi"), (SimpleNamespace(provider_id="anthropic"), "claude"), (DeepSeekProvider(), "unknown")):
            with self.subTest(provider=provider.provider_id), patch.object(telemetry, "_verify_loaded_sources") as sources:
                with self.assertRaises(telemetry.InteractiveTelemetryError):
                    telemetry._prepare_interactive_case(root=ROOT, session_dir=self.directory, case_id="CASE-001", provider=provider, model_id=model, max_turns=3)
                sources.assert_not_called()


if __name__ == "__main__":
    unittest.main()
