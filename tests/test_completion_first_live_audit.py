"""Real SQLite with synthetic captures; neither Provider nor live provenance."""
from __future__ import annotations

import inspect
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

from researchops.audit import AuditLedger, COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION
from researchops.completion_telemetry_ledger import LedgerCompletionTelemetrySession
from researchops_completion_telemetry.capture import CompletionTelemetryCollector
from researchops_completion_telemetry.sanitization import sanitize_completion_capture, build_completion_record
from researchops_completion_timing import first_live, first_live_audit as audit
from researchops_completion_timing import first_live_control as control, local_claim
from researchops_completion_timing.artifacts import load_artifact_contract
from researchops_completion_timing.contract import TimingContractError, digest
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import test_completion_first_live_timing as timing_helpers
from tests import test_completion_first_live_control as control_helpers
from tests.test_completion_telemetry_ledger import _test_runtime_binding, _plan_binding


ROOT = Path(__file__).resolve().parents[1]


def TimingHelper():
    # Importing the TestCase class directly makes unittest collect it twice.
    return timing_helpers.FirstLiveTimingTests()


class FirstLiveAuditTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.socket", side_effect=AssertionError("network forbidden"))
        self.network.start(); self.addCleanup(self.network.stop)
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "audit.sqlite3"
        self.data = TimingHelper().fixture()
        self.build_database()

    def build_database(self):
        plan, evidence = self.data["plan"], self.data["evidence"]
        self.run_id = plan["binding"]["validation_run_id"]
        self.ledger = AuditLedger(self.database)
        self.ledger.start_run(mode="deepseek_first_live_timed_validation", run_id=self.run_id,
            request_summary={"plan_commitment_sha256": plan["plan_commitment_sha256"],
                             "execution_binding_sha256": evidence["execution_binding_sha256"]})
        runtime = _test_runtime_binding()
        plan_binding = _plan_binding(runtime, case_ids=tuple(plan["planned_case_handles"]), max_turns=1, request_cap=2)
        tracker = CompletionTelemetryCollector.for_runtime(plan_binding)
        for index, (case, segment) in enumerate(zip(plan["planned_case_handles"], evidence["segments"])):
            session = tracker.bind_case(case)
            bridge = LedgerCompletionTelemetrySession(session, ledger=self.ledger, run_id=self.run_id, runtime_plan_binding=plan_binding)
            handle = bridge.begin_attempt()
            cap, output = (256, 1) if index == 0 else (16, 16)
            capture = sanitize_completion_capture({"status": "completed" if index == 0 else "incomplete",
                "incomplete_details": None if index == 0 else {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 5, "output_tokens": output, "total_tokens": 5 + output},
                "http_status": 200, "requested_output_token_cap": cap},
                normalized_usage={"requests": 1, "input_tokens": 5, "output_tokens": output, "total_tokens": 5 + output,
                                  "cached_input_tokens": None, "cache_write_tokens": None, "reasoning_tokens": None})
            terminal = session.finalize_response_accepted(handle, capture)
            record = build_completion_record(terminal._capture._collector_snapshot(), binding=runtime, response_index=index, request_index=index)
            segment.update(completion_record_sha256=digest(raw(record)), usage_sha256=digest(raw(record["usage"])))
            TimingHelper().seal(self.data)
            value = dict(schema_version=COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION, case_id=case,
                attempt_index=index, case_attempt_index=0, terminal_kind="response_accepted", response_index=index,
                error_code=None, binding=runtime.runtime_snapshot(), completion_record=record)
            self.ledger.append_timed_completion_telemetry_event("model_response_telemetry_recorded", value,
                capability=bridge._capability, attempt_handle=handle, terminal=terminal, segment_bytes=raw(segment),
                expected_timing_binding={name: segment[name] for name in ("plan_commitment_sha256", "execution_binding_sha256", "clock_domain_id")})
        self.ledger.set_run_status(self.run_id, "completed")
        artifact, _ = load_artifact_contract(ROOT)
        heads = [{"run_id": self.run_id, "final_chain_head_sha256": self.ledger.verify_chain(self.run_id).chain_head}]
        evidence["audit_chain_heads_sha256"] = digest(artifact["ordered_audit_heads_domain"].encode() + b"\0" + b"final_chain_heads\0" + raw(heads))
        TimingHelper().seal(self.data)
        self.database_hash = digest(self.database.read_bytes())

    def verify(self, **overrides):
        values = dict(expected_database_sha256=self.database_hash, plan_bytes=raw(self.data["plan"]),
            evidence_bytes=raw(self.data["evidence"]), publication_bytes=raw(self.data["publication"]),
            expected_manifest_file_sha256=self.data["publication"]["bundle_manifest_file_sha256"])
        values.update(overrides)
        return audit.verify_first_live_audit(ROOT, self.database, **values)

    def test_actual_rows_prove_linkage_not_live_provenance_or_authority(self):
        before = self.database.read_bytes()
        checked = self.verify()
        self.assertTrue(checked["audit_hash_chain_verified"])
        self.assertTrue(checked["timing_gate_complete"])
        self.assertEqual(checked["record_count"], 2)
        for field in ("control_artifacts_verified", "local_store_entry_independently_verified", "source_admission_verified",
                      "actual_clock_capture_proved", "runtime_authority_granted", "first_live_success_verified", "current_closure_claim_allowed"):
            self.assertIs(checked[field], False, field)
        self.assertEqual(before, self.database.read_bytes())
        signature = inspect.signature(audit.verify_first_live_audit)
        self.assertNotIn("terminal_projection_bytes", signature.parameters)
        self.assertNotIn("record_bytes", signature.parameters)

    def test_independent_database_hash_cannot_be_replaced_by_local_claims(self):
        with self.assertRaisesRegex(TimingContractError, "first_live_audit_database_hash_mismatch"):
            self.verify(expected_database_sha256="f" * 64)

    def test_sidecar_is_rejected_before_sqlite_open(self):
        Path(str(self.database) + "-wal").write_bytes(b"synthetic")
        with patch.object(audit, "_verify_audit_database", side_effect=AssertionError("must not open")):
            with self.assertRaisesRegex(TimingContractError, "first_live_audit_sidecar_present"):
                self.verify()

    def test_privacy_scan_covers_database_before_open(self):
        self.database.write_bytes(self.database.read_bytes() + b"Authorization: Bearer FAKECANARY")
        with patch.object(audit, "_verify_audit_database", side_effect=AssertionError("must not open")):
            with self.assertRaisesRegex(TimingContractError, "timing_sensitive_content"):
                self.verify()

    def test_row_tamper_is_rejected_even_after_database_rehash(self):
        before = self.database.read_bytes()
        needle = self.data["evidence"]["segments"][0]["segment_commitment_sha256"].encode()
        self.assertEqual(before.count(needle), 1)
        self.database.write_bytes(before.replace(needle, b"f" * 64))
        with self.assertRaisesRegex(TimingContractError, "first_live_audit_chain_mismatch"):
            self.verify(expected_database_sha256=digest(self.database.read_bytes()))

    def test_rehashed_timing_reference_must_match_same_terminal(self):
        self.data["evidence"]["segments"][0]["send_started_ns"] += 1
        TimingHelper().seal(self.data)
        with self.assertRaisesRegex(TimingContractError, "first_live_audit_segment_mismatch"):
            self.verify()

    def test_missing_segment_cannot_be_hidden_by_a_valid_chain(self):
        self.data["evidence"]["segments"].pop()
        TimingHelper().seal(self.data)
        with self.assertRaisesRegex(TimingContractError, "first_live_audit_segment_coverage_invalid"):
            self.verify()

    def test_extra_event_is_not_ignored(self):
        self.ledger.append_event(self.run_id, "unexpected_fixture_event", {"synthetic": True})
        with self.assertRaisesRegex(TimingContractError, "first_live_audit_inventory_invalid"):
            self.verify(expected_database_sha256=digest(self.database.read_bytes()))

    def test_late_database_change_is_rejected_after_shape_validation(self):
        original = first_live.verify_first_live_record_shapes
        def verify(*args, **kwargs):
            result = original(*args, **kwargs)
            self.database.write_bytes(self.database.read_bytes() + b" ")
            return result
        with patch.object(first_live, "verify_first_live_record_shapes", side_effect=verify):
            with self.assertRaisesRegex(TimingContractError, "first_live_audit_database_changed"):
                self.verify()

    def test_connection_image_swap_is_not_masked_by_unchanged_file_hash(self):
        original = sqlite3.connect
        other = Path(self.temporary.name) / "shadow.sqlite3"
        AuditLedger(other)
        calls = []
        def connect(path, **kwargs):
            calls.append(True)
            return original(other.as_uri() + "?mode=ro&immutable=1" if len(calls) == 2 else path, **kwargs)
        with patch("researchops_external_closure.artifact_inputs.sqlite3.connect", side_effect=connect):
            with self.assertRaisesRegex(TimingContractError, "first_live_audit_invalid"):
                self.verify()
        self.assertEqual(len(calls), 2)

    def test_additional_model_call_row_is_not_ignored(self):
        self.ledger.record_model_call(self.run_id, provider="deepseek", model="synthetic",
            started_at_utc="2026-09-06T00:00:00Z", latency_ms=1, input_tokens=1, output_tokens=1,
            cached_tokens=None, cost_usd=None, outcome="completed")
        with self.assertRaisesRegex(TimingContractError, "first_live_audit_inventory_invalid"):
            self.verify(expected_database_sha256=digest(self.database.read_bytes()))

    def test_audit_utc_suffix_is_interpreted_without_accepting_other_offsets(self):
        self.assertEqual(audit._audit_utc("2026-09-06T00:00:00+00:00"), audit._audit_utc("2026-09-06T00:00:00Z"))
        with self.assertRaises(ValueError):
            audit._audit_utc("2026-09-06T01:00:00+01:00")


@unittest.skipUnless(os.name == "nt", "fixed Windows store integration")
class ControlledFirstLiveAuditTests(unittest.TestCase):
    def setUp(self):
        self.helper = FirstLiveAuditTests()
        self.helper.setUp(); self.addCleanup(self.helper.doCleanups)
        base = Path(self.helper.temporary.name)
        self.locator = patch.object(local_claim, "_windows_local_app_data", return_value=base)
        self.locator.start(); self.addCleanup(self.locator.stop)
        environment_id = local_claim.provision_local_claim_store()["execution_environment_id"]
        plan, self.auth = control_helpers.FirstLiveControlTests().fixture(environment_id=environment_id, now=datetime.now(timezone.utc))
        proof = control_helpers.FirstLiveControlTests().verify(plan, self.auth)
        self.intent, request = control.build_first_live_claim_intent(ROOT, proof,
            clock_domain_id=self.helper.data["evidence"]["clock_domain_id"], intended_at_utc=self.auth["authorized_at_utc"])
        reserved = local_claim.reserve_local_claim(request)
        self.claim_hash = reserved["receipt_sha256"]
        self.marker = base / "ResearchOpsAgent/completion-claims-v1/claims" / (proof.authorization_id_sha256 + ".json")
        self.helper.data["plan"] = plan
        self.helper.data["evidence"]["binding"] = dict(plan["binding"], authorization_binding_sha256=proof.authorization_binding_sha256,
            local_claim_receipt_sha256=self.claim_hash)
        TimingHelper().seal(self.helper.data)
        # Preserve the earlier test fixture. Build the controlled graph in a
        # distinct, previously absent temporary database, not by overwriting it.
        self.helper.database = base / "controlled.sqlite3"
        self.helper.build_database()

    def verify(self):
        data = self.helper.data
        return audit.verify_first_live_controlled_audit(ROOT, self.helper.database,
            expected_database_sha256=self.helper.database_hash, plan_bytes=raw(data["plan"]), authorization_bytes=raw(self.auth),
            expected_authorization_binding_sha256=self.auth["authorization_binding_sha256"], verification_time_utc=self.auth["authorized_at_utc"],
            intent_bytes=self.intent, expected_claim_receipt_sha256=self.claim_hash, evidence_bytes=raw(data["evidence"]),
            publication_bytes=raw(data["publication"]), expected_manifest_file_sha256=data["publication"]["bundle_manifest_file_sha256"])

    def test_real_temporary_store_and_sqlite_join_without_minting_execution_authority(self):
        result = self.verify()
        self.assertTrue(result["authoritative_ledger_verified"])
        self.assertTrue(result["local_store_entry_independently_verified"])
        self.assertTrue(result["timing_gate_complete"])
        for field in ("first_live_success_verified", "runtime_authority_granted", "fresh_execution_time_verified",
                      "source_admission_verified", "actual_clock_capture_proved", "retry_or_resume_authorized",
                      "successful_reservation_return_proved", "current_closure_claim_allowed"):
            self.assertIs(result[field], False, field)
        parameters = inspect.signature(audit.verify_first_live_controlled_audit).parameters
        for name in ("record_bytes", "terminal_projection_bytes", "claim_receipt_bytes", "store_path"):
            self.assertNotIn(name, parameters)

    def test_late_store_change_cannot_survive_the_final_read(self):
        original = control.verify_first_live_controlled_timing
        def checked(*args, **kwargs):
            result = original(*args, **kwargs)
            value = json.loads(self.marker.read_bytes()); value["clock_domain_id"] = "PCECLOCK-" + "F" * 32
            self.marker.write_bytes(raw(value))
            return result
        with patch.object(control, "verify_first_live_controlled_timing", side_effect=checked):
            with self.assertRaisesRegex(local_claim.LocalClaimError, "local_claim_receipt_hash_mismatch"):
                self.verify()


if __name__ == "__main__":
    unittest.main()
