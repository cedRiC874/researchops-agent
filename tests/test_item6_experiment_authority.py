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
            f.record_test_location(known)
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


class ClaimDiagnosticsTests(unittest.TestCase):
    """Synthetic projection checks only: no claim, fixture process, Adapter or network."""

    @staticmethod
    def observed(mode="claim_write"):
        return dict(mode=mode, process_exit_code=2, claim_may_exist=True, claim_files=1, calls=[], hits=[mode],
            error="local_claim_write_unknown", claim_error_observation=dict(exception_type="LocalClaimError",
                code="local_claim_write_unknown", code_status="allowlisted"),
            post_checks=dict(claim_fault=dict(target=f.CLAIM_MODES[mode], call_count=1, claim_parent_matched=True,
                module_os_isolated=True, module_os_restored=True, process_os_unchanged=True)))

    def test_all_three_modes_preserve_safe_assertion_values(self):
        for mode in f.CLAIM_MODES:
            with self.subTest(mode=mode):
                row = f.claim_diagnostic(mode, self.observed(mode))
                self.assertEqual(len(row["checks"]), 11)
                self.assertTrue(all(item["matches"] is True for item in row["checks"].values()))
                self.assertFalse(row["exception_text_recorded"])
                self.assertFalse(row["exception_args_recorded"])
                self.assertFalse(row["stderr_recorded"])

    def test_mismatches_are_visible_without_changing_assertions(self):
        value = self.observed()
        value["process_exit_code"] = 0
        value["claim_may_exist"] = False
        value["post_checks"]["claim_fault"]["call_count"] = 0
        checks = f.claim_diagnostic("claim_write", value)["checks"]
        for name, actual in (("process_exit_code", 0), ("claim_may_exist", False), ("call_count", 0)):
            self.assertEqual(checks[name]["actual"]["value"], actual)
            self.assertFalse(checks[name]["matches"])

    def test_missing_null_and_false_remain_distinct(self):
        for value, status, actual in (({}, "missing", None), ({"claim_may_exist": None}, "null", None),
                                      ({"claim_may_exist": False}, "observed", False)):
            row = f.claim_diagnostic("claim_write", value)["checks"]["claim_may_exist"]["actual"]
            self.assertEqual(row, dict(status=status, value=actual))
        self.assertEqual(f.claim_diagnostic("claim_write", {"hits": None})["checks"]["mode_in_hits"]["actual"]["status"], "null")

    def test_sensitive_and_unknown_values_never_enter_projection(self):
        import json
        from researchops_external_closure.io import scan_public_artifact_bytes
        value = self.observed()
        canary = "Authorization: Bearer sk-fake-CLAIM-CANARY-1234567890"
        value.update(error=canary, stderr=canary, fixture_root="C:/Users/private-user/key.txt", calls=[canary], hits=[canary])
        value["claim_error_observation"] = dict(exception_type=canary, code=canary, code_status=canary)
        value["post_checks"]["claim_fault"].update(target=canary, call_count=True, module_os_restored=canary)
        row = f.claim_diagnostic("claim_write", value)
        raw = json.dumps(row).encode()
        self.assertNotIn(canary.encode(), raw)
        self.assertNotIn(b"private-user", raw)
        self.assertEqual(row["checks"]["calls_count"]["actual"]["value"], 1)
        self.assertEqual(row["checks"]["call_count"]["actual"]["status"], "redacted_invalid")
        self.assertEqual(row["result_error"]["status"], "redacted_invalid")
        scan_public_artifact_bytes((raw,))

    def test_error_codes_require_exact_class_and_literal_allowlist(self):
        from researchops_completion_timing.local_claim import LocalClaimError
        class ForgedError(Exception):
            code = "local_claim_write_unknown"
            def __str__(self): raise AssertionError("must not render exception")
        class Derived(LocalClaimError): pass
        self.assertEqual(f.claim_error_observation(LocalClaimError("local_claim_write_unknown"))["code_status"], "allowlisted")
        for error in (ForgedError(), Derived("local_claim_write_unknown"), LocalClaimError("unlisted_code")):
            with self.subTest(kind=type(error).__name__):
                row = f.claim_error_observation(error)
                self.assertIsNone(row["code"])
                self.assertEqual(row["code_status"], "not_allowlisted")

    def test_poison_values_are_not_stringified_or_truth_tested(self):
        class Poison:
            def __str__(self): raise AssertionError("str forbidden")
            def __bool__(self): raise AssertionError("bool forbidden")
            def __eq__(self, other): raise AssertionError("eq forbidden")
        value = dict(process_exit_code=Poison(), claim_may_exist=Poison(), claim_files=Poison(),
                     calls=[Poison()], hits=[Poison()], post_checks=Poison(), error=Poison())
        row = f.claim_diagnostic("claim_write", value)
        self.assertEqual(row["checks"]["process_exit_code"]["actual"]["status"], "redacted_invalid")
        self.assertFalse(row["checks"]["mode_in_hits"]["actual"]["value"])

    def test_report_reprojects_and_bounds_records(self):
        value = self.observed()
        value["claim_fault_diagnostic"] = {"raw": "PRIVATE_DIAGNOSTIC_SENTINEL"}
        rows = f.claim_diagnostics_for_report([value])
        self.assertEqual(rows, [f.claim_diagnostic("claim_write", value)])
        with self.assertRaisesRegex(ValueError, "claim_diagnostic_count"):
            f.claim_diagnostics_for_report([value] * 4)
        value["calls"] = [None] * 4097
        self.assertEqual(f.claim_diagnostic("claim_write", value)["checks"]["calls_count"]["actual"]["status"], "redacted_invalid")

    def test_public_log_redaction_preserves_independent_diagnostic(self):
        import json
        from types import SimpleNamespace
        from researchops_external_closure.io import scan_public_artifact_bytes
        diagnostics = f.claim_diagnostics_for_report([self.observed()])
        report = dict(log="Authorization: Bearer sk-fake-CLAIM-CANARY-1234567890", claim_fault_diagnostics=diagnostics)
        result = SimpleNamespace(failures=[("claim_write", "NOT_FOR_EXPORT_EXCEPTION_BODY")], errors=[])
        public = f.public_validation_report(report, result)
        self.assertEqual(public["claim_fault_diagnostics"], diagnostics)
        self.assertEqual(public["failure_test_ids"], ["claim_write"])
        self.assertNotEqual(public["log"], report["log"])
        self.assertNotIn("NOT_FOR_EXPORT_EXCEPTION_BODY", json.dumps(public))
        scan_public_artifact_bytes((json.dumps(public).encode(),))


class StoreDiagnosticsTests(unittest.TestCase):
    """Only owned temporary files and synthetic children; no store provisioning."""

    def test_seven_conditions_observed_for_owned_canonical_temp_directory(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory(prefix="item6-test-store-") as directory:
            row = f.observe_test_location(Path(directory).resolve())
        self.assertEqual(tuple(row["checks"]), f.STORE_CHECKS)
        self.assertTrue(all(check["status"] == "observed" and check["value"] is True for check in row["checks"].values()))
        self.assertFalse(row["authority_granted"])
        self.assertFalse(row["path_values_recorded"])

    def test_each_false_predicate_is_retained_without_admission(self):
        from pathlib import Path
        import tempfile
        from contextlib import nullcontext
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix="item6-test-store-") as directory:
            path = Path(directory).resolve()
            scenarios = (
                ("absolute", path, patch.object(type(path), "is_absolute", return_value=False)),
                ("parent_matches_temp", path, patch.object(f.tempfile, "gettempdir", return_value=str(path))),
                ("name_prefix", path / "nonprefix", nullcontext()),
                ("resolved_identity", path, patch.object(type(path), "resolve", return_value=path.parent)),
                ("not_symlink", path, patch.object(type(path), "is_symlink", return_value=True)),
                ("not_junction", path, patch.object(type(path), "is_junction", return_value=True)),
            )
            for key, subject, context in scenarios:
                with self.subTest(predicate=key), context:
                    row = f.observe_test_location(subject)
                    self.assertFalse(row["checks"][key]["value"])
                    self.assertFalse(row["authority_granted"])

    def test_wrong_path_type_is_not_inspected(self):
        class Poison:
            def __getattr__(self, name): raise AssertionError("must not inspect arbitrary path")
            def __str__(self): raise AssertionError("must not render path")
        row = f.observe_test_location(Poison())
        self.assertFalse(row["checks"]["exact_path_type"]["value"])
        self.assertTrue(all(row["checks"][name]["status"] == "not_observed" and row["checks"][name]["value"] is None for name in f.STORE_CHECKS[1:]))

    def test_observation_os_error_is_null_not_false_and_body_is_omitted(self):
        from pathlib import Path
        import json
        from unittest.mock import patch
        path = Path("C:/synthetic/item6-test-store-diagnostic")
        with patch.object(type(path), "resolve", side_effect=PermissionError("PRIVATE_ERROR_PATH_SENTINEL")):
            row = f.observe_test_location(path)
        for key in ("parent_matches_temp", "resolved_identity"):
            self.assertEqual(row["checks"][key], dict(status="error", value=None, error_type="PermissionError"))
        self.assertNotIn("PRIVATE_ERROR_PATH_SENTINEL", json.dumps(row))

    def test_store_projection_drops_paths_and_untrusted_values(self):
        import json
        from researchops_external_closure.io import scan_public_artifact_bytes
        value = dict(path="C:/Users/private-user/secret.txt", checks={name:dict(status="observed", value="Authorization: Bearer sk-synthetic-secret", error_type="private") for name in f.STORE_CHECKS})
        row = f.project_store_observation(value)
        self.assertTrue(all(check["status"] == "not_observed" and check["value"] is None for check in row["checks"].values()))
        raw = json.dumps(row).encode()
        self.assertNotIn(b"private", raw)
        self.assertNotIn(b"Bearer", raw)
        scan_public_artifact_bytes((raw,))

    def test_isolation_error_is_allowlisted_for_exact_exception_only(self):
        from researchops_item6_experiment_v1.contract import ExperimentError
        error = ExperimentError("offline_store_isolation")
        self.assertEqual(f.claim_error_observation(error)["code"], "item6_offline_store_isolation")
        class Forged(Exception): code = "item6_offline_store_isolation"
        self.assertIsNone(f.claim_error_observation(Forged())["code"])

    def test_child_wrapper_preserves_original_arguments_result_and_rejection(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from researchops_item6_experiment_v1.contract import ExperimentError
        with tempfile.TemporaryDirectory(prefix="item6-diagnostic-unit-") as directory:
            destination = Path(directory)
            known = destination / "item6-test-store-synthetic"
            with patch.object(f, "claim_fixture_child", return_value=7) as delegate:
                self.assertEqual(f.claim_child_with_diagnostics("claim-only", known, destination, "0"), 7)
                delegate.assert_called_once_with("claim-only", known, destination, "0")
            failure = ExperimentError("offline_store_isolation")
            with patch.object(f, "claim_fixture_child", side_effect=failure):
                with self.assertRaises(ExperimentError) as captured:
                    f.claim_child_with_diagnostics("claim-only", known, destination, "1")
            self.assertIs(captured.exception, failure)
            import json
            self.assertEqual(json.loads((destination / "diagnostic-1-failure.json").read_bytes())["code"], "item6_offline_store_isolation")

    def test_sidecar_is_exclusive_and_write_failure_does_not_start_delegate(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix="item6-diagnostic-unit-") as directory:
            destination = Path(directory)
            row = f.observe_test_location(destination)
            f.write_race_diagnostic(destination, "0", "location", row)
            with self.assertRaises(FileExistsError):
                f.write_race_diagnostic(destination, "0", "location", row)
            with patch.object(f, "write_race_diagnostic", side_effect=OSError("synthetic evidence failure")), patch.object(f, "claim_fixture_child") as delegate:
                with self.assertRaises(OSError):
                    f.claim_child_with_diagnostics("claim-only", destination, destination, "1")
                delegate.assert_not_called()

    def test_failure_sidecar_error_keeps_original_rejection(self):
        from pathlib import Path
        from unittest.mock import patch
        from researchops_item6_experiment_v1.contract import ExperimentError
        original = ExperimentError("offline_store_isolation")
        evidence = OSError("synthetic evidence failure")
        with patch.object(f, "write_race_diagnostic", side_effect=[None, evidence]), patch.object(f, "claim_fixture_child", side_effect=original):
            with self.assertRaises(ExperimentError) as captured:
                f.claim_child_with_diagnostics("claim-only", Path("."), Path("."), "0")
        self.assertIs(captured.exception, original)
        self.assertIs(captured.exception.__cause__, evidence)

    @staticmethod
    def child(code):
        class Child:
            def poll(self): return code
            def __getattr__(self, name): raise AssertionError("no child I/O, wait, signal or environment access")
        return Child()

    def test_race_records_actual_exit_and_keeps_running_child_null(self):
        from pathlib import Path
        import tempfile
        from researchops_item6_experiment_v1.contract import ExperimentError
        with tempfile.TemporaryDirectory(prefix="item6-diagnostic-unit-") as directory:
            destination = Path(directory)
            f.write_race_diagnostic(destination, "0", "location", f.observe_test_location(destination))
            f.write_race_diagnostic(destination, "0", "failure", f.claim_error_observation(ExperimentError("offline_store_isolation")))
            row = f.race_children_diagnostic([self.child(1), self.child(None)], destination)
        first, second = row["children"]
        self.assertEqual(first["actual_exit_code"], 1)
        self.assertEqual(first["failure_code"], "item6_offline_store_isolation")
        self.assertIsNone(second["actual_exit_code"])
        self.assertEqual(second["exit_status"], "still_running")
        self.assertIsNone(second["failure_code"])
        self.assertFalse(row["cleanup_proved"])
        self.assertFalse(row["processes_modified"])

    def test_race_missing_or_invalid_evidence_is_not_success(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory(prefix="item6-diagnostic-unit-") as directory:
            destination = Path(directory)
            (destination / "diagnostic-0-failure.json").write_text('{"code":"PRIVATE_UNLISTED_CODE"}', encoding="utf-8")
            row = f.race_children_diagnostic([self.child(2), self.child(None)], destination)
        self.assertIsNone(row["children"][0]["failure_code"])
        self.assertEqual(row["children"][0]["failure_code_status"], "not_allowlisted")
        self.assertNotEqual(row["children"][1]["failure_code_status"], "allowlisted")

    def test_race_poll_error_has_no_fabricated_exit_code(self):
        from pathlib import Path
        import tempfile
        class Broken:
            def poll(self): raise OSError("PRIVATE_POLL_BODY")
        with tempfile.TemporaryDirectory(prefix="item6-diagnostic-unit-") as directory:
            row = f.race_children_diagnostic([Broken(), self.child(None)], Path(directory))
        self.assertIsNone(row["children"][0]["actual_exit_code"])
        self.assertEqual(row["children"][0]["exit_status"], "observation_error")

    def test_race_public_diagnostic_remains_safe_without_raw_streams(self):
        from pathlib import Path
        import tempfile
        import io
        import json
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from researchops_external_closure.io import scan_public_artifact_bytes
        with tempfile.TemporaryDirectory(prefix="item6-diagnostic-unit-") as directory, patch.object(f, "RESULTS", []), redirect_stdout(io.StringIO()) as output:
            f.record_race_early_exit([self.child(1), self.child(None)], Path(directory))
            self.assertEqual(len(f.RESULTS), 1)
            row = f.RESULTS[0]["diagnostic"]
        self.assertTrue(output.getvalue().startswith("ITEM6_CLAIM_RACE_DIAGNOSTIC "))
        self.assertFalse(row["stdout_recorded"])
        self.assertFalse(row["stderr_recorded"])
        scan_public_artifact_bytes((json.dumps(row).encode(),))
