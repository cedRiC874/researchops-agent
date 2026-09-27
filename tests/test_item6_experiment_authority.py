import copy
import pickle
import unittest
from researchops_item6_experiment_v1.authority import Owner, Prepared
from researchops_completion_telemetry.surface_mapping import create_item6_experiment_binding
from researchops.model_providers import DeepSeekProvider, ProviderConfigurationError
from . import item6_experiment_fixture as f


class AuthorityTests(unittest.TestCase):
    def test_manifest_digest_mismatch_rejected_before_claim(self):
        result=f.run_case("manifest_hash_mismatch")
        self.assertEqual(result["process_exit_code"],2)
        self.assertEqual(result["error"],"item6_source_manifest_drift")
        self.assertEqual(result["claim_files"],0)
        self.assertEqual(result["calls"],[])
        self.assertEqual(result["admission_observations"]["planned"],32)
        self.assertTrue(all(r["execution_state"]=="not_executed" for r in result["admission_observations"]["records"]))

    def test_forged_owner_dictionary_or_same_class_has_no_authority(self):
        for owner in ({"receipt": "success"}, object.__new__(Owner)):
            with self.subTest(owner_type=type(owner).__name__), self.assertRaises(Exception):
                create_item6_experiment_binding(owner)
        for value in (object.__new__(Owner), object.__new__(Prepared)):
            with self.assertRaises(TypeError): copy.copy(value)
            with self.assertRaises(TypeError): pickle.dumps(value)

    def test_legacy_model_allowlist_is_not_expanded(self):
        with self.assertRaises(ProviderConfigurationError): DeepSeekProvider().validate_model("deepseek-flash")
        self.assertEqual(DeepSeekProvider().validate_model("deepseek-v4-flash"), "deepseek-v4-flash")

    def test_offline_capability_cannot_enter_live_entry(self):
        result = f.run_case("offline_to_live")
        self.assertEqual(result["process_exit_code"], 2)
        self.assertEqual(result["error"], "item6_entry_mode_mismatch")
        self.assertEqual(result["claim_files"], 0)
        self.assertEqual(result["calls"], [])
        self.assertIn("offline_to_live", result["hits"])

    def test_receipt_dictionary_cannot_claim(self):
        result = f.run_case("forged_prepared")
        self.assertEqual(result["error"], "item6_prepared_required")
        self.assertEqual(result["claim_files"], 0)
        self.assertEqual(result["calls"], [])

    def test_expired_approval_rejected_before_claim(self):
        result = f.run_case("expired")
        self.assertEqual(result["error"], "item6_approval_window")
        self.assertEqual(result["claim_files"], 0)
        self.assertEqual(result["calls"], [])

    def test_same_grant_cannot_repeat(self):
        result = f.run_case("duplicate")
        self.assertEqual(result["process_exit_code"], 2)
        self.assertEqual(result["run"]["status"], "completed")
        self.assertEqual(result["error"], "item6_output_exists")
        self.assertEqual(result["claim_files"], 1)
        self.assertEqual(len(result["calls"]), 30)

    def test_claim_partial_write_fsync_and_close_failures_never_issue_owner(self):
        for mode in ("claim_write", "claim_fsync", "claim_close"):
            with self.subTest(mode=mode):
                result = f.run_case(mode)
                self.assertEqual(result["process_exit_code"], 2)
                self.assertTrue(result["claim_may_exist"])
                self.assertEqual(result["claim_files"], 1)
                self.assertEqual(result["calls"], [])
                self.assertIn(mode, result["hits"])
                fault = result["post_checks"]["claim_fault"]
                self.assertEqual(
                    fault["target"],
                    {"claim_write": "write", "claim_fsync": "fsync", "claim_close": "close"}[mode],
                )
                self.assertGreaterEqual(fault["call_count"], 1)
                self.assertTrue(fault["claim_parent_matched"])
                self.assertTrue(fault["module_os_isolated"])
                self.assertTrue(fault["module_os_restored"])
                self.assertTrue(fault["process_os_unchanged"])

    def test_source_changes_and_expiry_after_io_stop_dispatch(self):
        for mode, calls in (("source_drift", 1), ("expiry_after_source", 0)):
            with self.subTest(mode=mode):
                result = f.run_case(mode)
                self.assertEqual(result["process_exit_code"], 2)
                self.assertEqual(len(result["calls"]), calls)
                self.assertIn(mode, result["hits"])

    def test_cloned_factory_is_not_the_consuming_factory(self):
        result = f.run_case("factory_clone")
        self.assertEqual(result["error"], "item6_factory_identity")
        self.assertEqual(result["calls"], [])
        self.assertIn("factory_clone", result["hits"])

    def test_foreign_case_handle_cannot_finalize_another_case(self):
        result = f.unfinished_case("cross_case_handle")
        self.assertEqual(result["process_exit_code"], 2)
        self.assertIn("cross_case_handle", result["hits"])
        self.assertEqual(len(result["calls"]), 1)
        self.assertTrue(all(e["event_type"] != "model_response_telemetry_recorded" for e in result["artifact"]["audit"]["events"]))
        self.assertEqual(result["run"]["archive_kind"], "unfinished_failure")
        self.assertFalse(result["run"]["execution_completed"])
        self.assertEqual(result["attempt_failures"][0]["exception_type"], "AuditError")
        self.assertEqual(result["attempt_failures"][0]["code"], "audit_completion_attempt_handle_invalid")
        self.assertEqual(result["attempt_failures"][0]["code_status"], "allowlisted")
        self.assertEqual(len(result["attempt_failures"]), 1)
        phase = result["artifact"]["phase"]
        self.assertFalse(phase["closed"])
        self.assertIsNotNone(phase["active_attempt"])
        self.assertIsNone(phase["phase"]["phase_terminal_ns"])
        for path in ("agent", "fixed_workflow"):
            self.assertEqual(len(result["artifact"]["business"][path]), 16)
            self.assertEqual(result["artifact"]["scores"][path]["raw_report"]["counts"]["planned"], 16)
        checked = f.verify_unfinished(result)
        self.assertTrue(checked["failure_archive_verified"])
        self.assertFalse(checked["execution_completed"])
        self.assertFalse(checked["phase_completed"])

    def test_attempt_diagnostic_exact_type_whitelist_and_no_exception_text(self):
        from researchops.audit import AuditError
        from researchops_completion_telemetry.sanitization import CompletionTelemetryError
        from researchops_item6_experiment_v1.attempt_failures import classify
        class Impostor(Exception):
            @property
            def code(self): raise AssertionError("untrusted property must not be read")
        class Subclass(AuditError): pass
        allowed = "audit_completion_attempt_handle_invalid"
        for error in (Impostor("secret"), Subclass(allowed, "secret"), AuditError("secret", "secret")):
            projected = classify(error)
            self.assertIsNone(projected["code"])
            self.assertEqual(projected["code_status"], "unknown")
            self.assertNotIn("secret", str(projected))
        self.assertEqual(classify(AuditError(allowed, "secret"))["code"], allowed)
        self.assertEqual(classify(CompletionTelemetryError("completion_telemetry_comparable_counter_invalid"))["code_status"], "allowlisted")

    def test_finalize_observer_rethrows_once_without_retry_or_handle_substitution(self):
        from unittest.mock import patch
        from researchops.audit import AuditError
        from researchops_item6_experiment_v1.session import ExperimentSession, _PendingTimedLedgerSession
        original = AuditError("audit_completion_attempt_handle_invalid", "secret")
        handle = object()
        session = object.__new__(ExperimentSession)
        with patch.object(_PendingTimedLedgerSession, "_finalize", side_effect=original) as call, \
             patch("researchops_item6_experiment_v1.attempt_failures.observe") as observe:
            with self.assertRaises(AuditError) as raised:
                session._finalize(handle, "response_accepted")
        self.assertIs(raised.exception, original)
        call.assert_called_once_with(handle, "response_accepted", capture=None, error_code=None)
        observe.assert_called_once_with(session, original)

    def test_two_processes_share_one_atomic_claim_winner(self):
        result = f.concurrent_claims()
        self.assertEqual(result["claim_files"], 1)
        self.assertEqual(sum(row["owner_created"] for row in result["contenders"]), 1)
        self.assertEqual(sorted(row["actual_exit_code"] for row in result["contenders"]), [0, 2])
        self.assertTrue(all(row["dispatches"] == 0 for row in result["contenders"]))
        loser = next(row for row in result["contenders"] if not row["owner_created"])
        self.assertEqual(loser["error"], "local_claim_already_exists")

    def test_noncanonical_documents_rejected_without_claim(self):
        result = f.run_case("noncanonical_documents")
        self.assertEqual(result["error"], "item6_canonical_documents_required")
        self.assertEqual(result["claim_files"], 0)
        self.assertEqual(result["admission_observations"]["planned"], 32)
        self.assertTrue(all(row["execution_state"] == "not_executed" for row in result["admission_observations"]["records"]))

    def test_mutable_factory_or_budget_cannot_replace_owner_frozen_policy(self):
        for mode in ("factory_freeze_drift", "budget_policy_drift"):
            with self.subTest(mode=mode):
                result = f.run_case(mode)
                self.assertEqual(result["process_exit_code"], 2)
                self.assertEqual(result["calls"], [])
                self.assertIn(mode, result["hits"])
                self.assertEqual(result["artifact"]["authority"]["stopped"], "item6_" + mode)

    def test_future_approval_observation_and_window_are_rejected(self):
        from datetime import timedelta
        from researchops_item6_experiment_v1 import contract as c
        authority = f.normal()["artifact"]["authority"]
        freeze = authority["freeze"]
        original = authority["approval"]
        instant = c.utc(original["observation"]["observed_at_utc"])
        for field, code in (("observed_at_utc", "future_approval"), ("not_before_utc", "approval_window")):
            with self.subTest(field=field):
                approval = copy.deepcopy(original)
                target = approval["observation"] if field == "observed_at_utc" else approval["candidate"]
                target[field] = (instant + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
                digest = c.commit("approval-candidate", approval["candidate"])
                approval["observation"]["approved_digest"] = digest
                with self.assertRaisesRegex(c.ExperimentError, "item6_" + code):
                    c.validate_approval(approval, freeze, digest, at=instant)
        f.RESULTS.append(dict(kind="future_approval_boundaries", observations_checked=2, provider_calls=0, real_store_touched=False))

    def test_fresh_owner_utc_and_identity_boundaries(self):
        from datetime import timedelta
        import uuid
        from unittest.mock import patch
        from researchops.audit import AuditLedger
        from researchops_completion_timing import local_claim
        from researchops_item6_experiment_v1 import authority as a, contract as c, runner, session
        from researchops_item6_experiment_v1.observations import Case
        known = f.allocate("item6-test-store-")
        with patch.object(local_claim, "_windows_local_app_data", return_value=known):
            status = local_claim.provision_local_claim_store()
            freeze = c.build_freeze(mode="offline_test", environment_id=status["execution_environment_id"], pricing=dict(
                kind="synthetic_fixture", input_per_million="1", output_per_million="2", cost_limit="10",
                input_accounting="synthetic_bytes", provider_bill_hard_cap=False, evidence_sha256=None))
            instant = c.now()
            candidate = dict(scope=c.SCOPE, mode="offline_test", authorization_id="boundary-fixture-" + uuid.uuid4().hex,
                freeze_sha256=c.commit("freeze", freeze), environment_id=status["execution_environment_id"],
                not_before_utc=(instant-timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
                expires_at_utc=(instant+timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
            digest = c.commit("approval-candidate", candidate)
            approval = dict(schema_version="item6-experiment-approval/1.0", candidate=candidate,
                observation=dict(kind="synthetic_approval_fixture", approved_digest=digest,
                    observed_at_utc=instant.isoformat().replace("+00:00", "Z"), message_reference="synthetic-boundary-only"))
            prepared = a.validate_experiment(freeze_bytes=c.raw(freeze), approval_bytes=c.raw(approval),
                approved_digest=digest, expected_mode="offline_test")
            owner = a._claim_experiment(prepared)
            hits = []
            try:
                with patch.object(c, "now", return_value=owner._state()["last_utc"]-timedelta(microseconds=1)):
                    with self.assertRaisesRegex(c.ExperimentError, "item6_utc_rollback"):
                        owner.check()
                    hits.append("utc_rollback")
                with self.assertRaisesRegex(c.ExperimentError, "item6_prepared_consumed"):
                    a._claim_experiment(prepared)
                with self.assertRaisesRegex(c.ExperimentError, "item6_prepared_required"):
                    a._claim_experiment(owner.snapshots())
                with self.assertRaises(TypeError): pickle.dumps(owner)
                with self.assertRaises(TypeError): copy.copy(owner)
                runner._exclusive_directory(owner.output)
                ledger = AuditLedger(owner.output / "audit.sqlite3", timestamp_format="utc_z")
                factory = session.ExperimentFactory(owner, ledger)
                with self.assertRaisesRegex(c.ExperimentError, "item6_factory_reused"):
                    session.ExperimentFactory(owner, ledger)
                with self.assertRaisesRegex(c.ExperimentError, "item6_mapping_owner_required"):
                    create_item6_experiment_binding(owner)
                first = freeze["business_plan"][0]
                task = next(t for t in c.decode(c.read(c.TASKS))["tasks"] if t["task_id"] == first["task_id"])
                case = Case(factory, task, first["path_kind"])
                factory.open_case(case)
                with self.assertRaisesRegex(c.ExperimentError, "item6_owner_case_identity"):
                    owner.assert_case(factory, copy.copy(case))
                for field in ("run_id", "path", "task_id"):
                    with self.subTest(field=field):
                        record, original_path, original_task = copy.deepcopy(case.record), case.path, copy.deepcopy(case.task)
                        try:
                            if field == "run_id": case.record["run_id"] += "-foreign"
                            elif field == "path": case.path = "agent"
                            else: case.task["task_id"] = "IC-16"
                            with self.assertRaisesRegex(c.ExperimentError, "item6_case_memory_drift"):
                                owner.assert_case(factory, case)
                            hits.append(field)
                        finally:
                            case.record, case.path, case.task = record, original_path, original_task
                self.assertEqual(factory.dispatches, 0)
                self.assertEqual(len(list((known / "ResearchOpsAgent/completion-claims-v1/claims").glob("*.json"))), 1)
            finally:
                owner.stop("item6_fixture_boundary_done")
                owner.close()
        self.assertEqual(hits, ["utc_rollback", "run_id", "path", "task_id"])
        f.RESULTS.append(dict(kind="fresh_owner_boundaries", hits=hits, claim_files=1, dispatches=0,
            fixture_store=str(known), provider_calls=0, real_store_touched=False, complete_archive_claimed=False))
