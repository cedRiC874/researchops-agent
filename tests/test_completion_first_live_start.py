"""Checked startup on real temporary Git/store; no Key, task or Provider use."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import first_live_start as start, first_live, first_live_control as control, local_claim
from researchops_completion_timing.clock import _TimingClock, TimingCaptureError
from researchops_completion_timing.contract import digest
from researchops_external_closure import execution_components_v3 as source
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import execution_v3_fixture as fixtures
from tests import test_completion_first_live_control as controls
from tests.test_external_closure_git_objects import Repository
from tests.test_external_closure_execution_binding import write_file_tree


@unittest.skipUnless(os.name == "nt", "designated Windows store")
class FirstLiveStartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.repository = Repository(cls.directory / "repo")
        # Exercise today's legacy-default startup code against an explicitly
        # historical v3 fixture, without widening its frozen 256-file bound.
        files, paths = fixtures.archived_first_live_files()
        module_path = 'src/researchops_completion_timing/first_live_start.py'
        files[module_path] = (Path(__file__).resolve().parents[1] / module_path).read_bytes()
        cls.source_documents = source.build_profile_documents(files, available_paths=paths, profile="first_live")
        manifest, plan = source.PROFILE_PATHS["first_live"]
        files |= {manifest: cls.source_documents.manifest, plan: cls.source_documents.plan}
        for name, payload in files.items():
            path = cls.repository.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
        cls.tree = write_file_tree(cls.repository, files); cls.commit = cls.repository.commit(cls.tree)
        cls.repository.git("update-ref", "HEAD", cls.commit)
        cls.known = cls.directory / "known-folder"; cls.known.mkdir()
        cls.locator = patch.object(local_claim, "_windows_local_app_data", return_value=cls.known)
        cls.locator.start(); cls.addClassCleanup(cls.locator.stop)
        cls.environment_id = local_claim.provision_local_claim_store()["execution_environment_id"]

    def documents(self):
        plan, auth = controls.FirstLiveControlTests().fixture(environment_id=self.environment_id,
            now=datetime.now(timezone.utc) - timedelta(seconds=1))
        identifier = digest(self._testMethodName.encode())
        plan["binding"].update(execution_commit=self.commit, execution_tree=self.tree,
            authorization_id_sha256=identifier, source_integrity_commitment_sha256=json.loads(self.source_documents.plan)["plan_commitment_sha256"])
        plan["binding"]["validation_run_id"] = first_live.validation_run_id(plan["binding"])
        plan["plan_commitment_sha256"] = first_live.commitment("plan", plan)
        auth.update(authorization_id_sha256=identifier, timing_plan_commitment_sha256=plan["plan_commitment_sha256"])
        auth["authorization_binding_sha256"] = control.authorization_binding(auth)
        return plan, auth

    def prepare(self, plan, auth):
        with patch("socket.socket", side_effect=AssertionError("no Provider")):
            return start._prepare_claimed_first_live_start(self.repository.root, plan_bytes=raw(plan), authorization_bytes=raw(auth),
                expected_authorization_binding_sha256=auth["authorization_binding_sha256"])

    def marker(self, auth):
        return self.known / "ResearchOpsAgent/completion-claims-v1/claims" / (auth["authorization_id_sha256"] + ".json")

    def test_source_and_fresh_claim_precede_clock_and_no_key_or_task_is_released(self):
        plan, auth = self.documents()
        original = start._TimingClock
        created = []
        def clock(**kwargs):
            self.assertTrue(self.marker(auth).is_file())
            created.append(True)
            return original(**kwargs)
        with patch.object(start, "_TimingClock", side_effect=clock):
            prepared = self.prepare(plan, auth)
        self.assertEqual(len(created), 1)
        summary = prepared.summary()
        self.assertTrue(summary["claim_consumed"])
        for name in ("task_released", "key_loaded", "runtime_authority_granted", "loaded_process_source_verified", "retry_authorized"):
            self.assertFalse(summary[name])
        self.assertIsNone(prepared.clock.snapshot()["phase"]["task_released_ns"])
        self.assertIsNone(prepared.clock.snapshot()["phase"]["key_loaded_ns"])
        with self.assertRaises(TypeError):
            copy.copy(prepared)
        with patch.object(start.os, "getpid", return_value=os.getpid() + 1):
            with self.assertRaisesRegex(start.FirstLiveStartError, "wrong_process"):
                prepared._take_for_runtime()
        self.assertIs(prepared._take_for_runtime(), prepared)
        with self.assertRaisesRegex(start.FirstLiveStartError, "already_taken"):
            prepared._take_for_runtime()
        prepared.abort()
        self.assertTrue(prepared.clock.snapshot()["halted"])

    def test_expired_g3_rejects_before_source_or_claim(self):
        plan, auth = self.documents()
        with patch.object(start, "_utc_now", return_value=auth["expires_at_utc"]), \
             patch.object(start, "verify_local_timed_execution_identity", side_effect=AssertionError("no source work")):
            with self.assertRaises(start.FirstLiveStartError) as caught:
                self.prepare(plan, auth)
        self.assertFalse(caught.exception.claim_consumed)
        self.assertFalse(caught.exception.retry_authorized)
        self.assertFalse(self.marker(auth).exists())

    def test_wrong_store_environment_rejects_before_source(self):
        plan, auth = self.documents()
        plan["binding"]["execution_environment_id"] = "PCEENV-" + "F" * 32
        plan["binding"]["validation_run_id"] = first_live.validation_run_id(plan["binding"])
        plan["plan_commitment_sha256"] = first_live.commitment("plan", plan)
        auth.update(execution_environment_id=plan["binding"]["execution_environment_id"], timing_plan_commitment_sha256=plan["plan_commitment_sha256"])
        auth["authorization_binding_sha256"] = control.authorization_binding(auth)
        with patch.object(start, "verify_local_timed_execution_identity", side_effect=AssertionError("no source work")):
            with self.assertRaisesRegex(start.FirstLiveStartError, "first_live_start_store_mismatch") as caught:
                self.prepare(plan, auth)
        self.assertFalse(caught.exception.claim_consumed)

    def test_source_change_during_claim_leaves_consumed_marker_and_no_prepared_result(self):
        plan, auth = self.documents()
        path = self.repository.root / "src/researchops/__init__.py"
        before = path.read_bytes()
        original = local_claim._reserve_with_ownership
        def reserve(request):
            winner = original(request)
            path.write_bytes(before + b"\n# changed during claim\n")
            return winner
        try:
            with patch.object(local_claim, "_reserve_with_ownership", side_effect=reserve):
                with self.assertRaises(start.FirstLiveStartError) as caught:
                    self.prepare(plan, auth)
            self.assertTrue(caught.exception.claim_consumed)
            self.assertFalse(caught.exception.retry_authorized)
            self.assertTrue(self.marker(auth).exists())
        finally:
            path.write_bytes(before)


class StartupBudgetTests(unittest.TestCase):
    def test_whole_budget_reserves_sealing_time(self):
        with patch.object(start, "_monotonic_ns", side_effect=[0, 300_000_000_000]), \
             patch.object(start, "_utc_now", return_value="2026-09-06T01:00:00Z"):
            budget = start._StartBudget()
            with self.assertRaisesRegex(ValueError, "budget_exhausted"):
                budget.checkpoint()

    def test_clock_reversal_and_expiry_are_not_success(self):
        with patch.object(start, "_monotonic_ns", side_effect=[100, 99]), \
             patch.object(start, "_utc_now", return_value="2026-09-06T01:00:00Z"):
            with self.assertRaisesRegex(ValueError, "clock_reversed"):
                start._StartBudget().checkpoint()
        with patch.object(start, "_utc_now", return_value="2026-09-06T01:00:00Z"):
            with self.assertRaisesRegex(ValueError, "authorization_expired"):
                start._StartBudget().checkpoint("2026-09-06T01:00:00Z")

    def test_phase_deadline_uses_actual_origin_and_can_never_extend_budget(self):
        with patch("researchops_completion_timing.clock.time.monotonic_ns", return_value=1000):
            clock = _TimingClock(request_timeout_ns=100, phase_timeout_ns=300)
            clock._tighten_phase_deadline(1200)
            self.assertEqual(clock._phase_cap, 200)
            clock._tighten_phase_deadline(2000)
            self.assertEqual(clock._phase_cap, 200)
            with self.assertRaisesRegex(TimingCaptureError, "phase_timeout_exceeded"):
                clock._tighten_phase_deadline(1000)

    def test_halted_clock_cannot_be_made_handoff_ready_by_a_future_deadline(self):
        clock = _TimingClock(request_timeout_ns=100_000_000, phase_timeout_ns=300_000_000)
        clock.abort_new_work()
        with self.assertRaisesRegex(TimingCaptureError, "timing_capture_halted"):
            clock._tighten_phase_deadline(clock._origin + 1_000_000_000)


if __name__ == "__main__":
    unittest.main()
