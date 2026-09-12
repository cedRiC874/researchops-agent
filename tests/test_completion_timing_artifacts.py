"""Synthetic seven-file bundles backed by actual temporary SQLite audit rows."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from researchops.audit import AuditLedger, COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION, COMPLETION_TELEMETRY_TERMINAL_EVENT_TYPES
from researchops.completion_telemetry_ledger import LedgerCompletionTelemetrySession
from researchops_completion_telemetry.capture import CompletionTelemetryCollector
from researchops_completion_telemetry.sanitization import build_completion_record
from researchops_completion_telemetry.surface_mapping import load_and_select_surface_mapping
from researchops_completion_timing.artifacts import PROFILE_SHA256, load_artifact_contract, verify_timing_artifact_bundle
from researchops_completion_timing.contract import CONTRACT_SHA256, TimingContractError, commitment, digest
from researchops_completion_timing.ledger_event import LEDGER_CONTRACT_SHA256
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests.test_completion_telemetry_ledger import _test_runtime_binding, _plan_binding, _capture
from tests import test_completion_timing_contract as timing_authoring
from tests.manual_completion_denominator import manual_denominator


ROOT = Path(__file__).resolve().parents[1]


class TimingArtifactTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.socket", side_effect=AssertionError("network forbidden"))
        self.network.start(); self.addCleanup(self.network.stop)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.profile, _ = load_artifact_contract(ROOT)
        helper = timing_authoring.TimingAuthoringTests()
        self.data = helper.fixture()
        plan, evidence = self.data["plan"], self.data["evidence"]
        self.case = plan["planned_case_handles"][0]
        self.run = "PCERUN-" + "A" * 32
        plan.update(planned_case_handles=[self.case], max_attempts=2, max_attempts_per_case=2)
        evidence["segments"] = evidence["segments"][:1]
        self.ledger = AuditLedger(self.directory / "phase6_audit.sqlite3")
        self.ledger.start_run(mode="timing-bundle-fixture", request_summary={"purpose": "synthetic"}, run_id=self.run)
        runtime = _test_runtime_binding()
        binding = runtime.runtime_snapshot()
        plan_binding = _plan_binding(runtime, case_ids=(self.case,), max_turns=2, request_cap=2)
        tracker = CompletionTelemetryCollector.for_runtime(plan_binding)
        session = tracker.bind_case(self.case)
        bridge = LedgerCompletionTelemetrySession(session, ledger=self.ledger, run_id=self.run, runtime_plan_binding=plan_binding)
        handle = bridge.begin_attempt()
        terminal = session.finalize_response_accepted(handle, _capture())
        record = build_completion_record(terminal._capture._collector_snapshot(), binding=runtime, response_index=0, request_index=0)
        segment = evidence["segments"][0]
        segment.update(completion_record_sha256=digest(raw(record)), usage_sha256=digest(raw(record["usage"])))
        helper.seal(self.data)
        payload = dict(schema_version=COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION, case_id=self.case,
            attempt_index=0, case_attempt_index=0, terminal_kind="response_accepted", response_index=0,
            error_code=None, binding=binding, completion_record=record)
        AuditLedger.append_timed_completion_telemetry_event(self.ledger, "model_response_telemetry_recorded", payload,
            capability=bridge._capability, attempt_handle=handle, terminal=terminal, segment_bytes=raw(segment),
            expected_timing_binding={key: segment[key] for key in ("plan_commitment_sha256", "execution_binding_sha256", "clock_domain_id")})
        self.ledger.set_run_status(self.run, "completed")
        events = self.ledger.export_run(self.run)["events"]
        event_bridge = bridge.event_commitment()
        event_bridge["terminals"] = [{"attempt_index": 0, "event_hash": next(row["event_hash"] for row in events if row["event_type"] in COMPLETION_TELEMETRY_TERMINAL_EVENT_TYPES)}]
        event_bridge["all_started_attempts_terminal"] = True
        event_bridge.pop("commitment_sha256")
        event_bridge["commitment_sha256"] = digest(raw(event_bridge))
        chain = asdict(self.ledger.verify_chain(self.run))
        self.index = {"schema_version": "1.1", "runs": [{"task_id": self.case, "run_id": self.run,
            "provider": "deepseek", "transport": "openai_compatible_responses", "chain_verification": chain,
            "completion_telemetry_event_commitment": event_bridge}]}
        heads = [{"run_id": self.run, "final_chain_head_sha256": chain["chain_head"]}]
        evidence["audit_chain_heads_sha256"] = digest(self.profile["ordered_audit_heads_domain"].encode() + b"\0" + b"final_chain_heads\0" + raw(heads))
        selection = load_and_select_surface_mapping(ROOT, "deepseek", "responses", "openai_compatible_responses", purpose="offline_validation")
        self.denominator_plan, denominator = manual_denominator(selection, binding, [self.case], observed_cases=1)
        denominator["records"] = [record]
        self.telemetry = {"schema_version": "phase6-completion-telemetry/1.0", "status": "recorded",
            "runtime_plan": self.denominator_plan, "runtime_denominator": denominator,
            "ledger_reconciliation": {}, "closure": {}, "historical_backfill_performed": False}
        self.reseal()

    def reseal(self):
        # Only test-owned fixtures are rewritten. Production verification is readonly.
        evidence = self.data["evidence"]
        evidence["evidence_commitment_sha256"] = commitment("evidence", evidence)
        payloads = {"phase6_audit_index.json": raw(self.index), "phase6_completion_telemetry.json": raw(self.telemetry),
                    "completion_timing_plan.json": raw(self.data["plan"]), "completion_timing_evidence.json": raw(evidence)}
        for name, value in payloads.items():
            (self.directory / name).write_bytes(value)
        manifest = dict(schema_version="provider-completion-timed-bundle-manifest/1.0", status="sealed_payloads",
            campaign_id=self.data["plan"]["binding"]["campaign_id"], artifact_contract_sha256=PROFILE_SHA256,
            timing_contract_sha256=CONTRACT_SHA256, timed_ledger_contract_sha256=LEDGER_CONTRACT_SHA256,
            plan_commitment_sha256=self.data["plan"]["plan_commitment_sha256"], execution_binding_sha256=evidence["execution_binding_sha256"],
            files={name: {"bytes": len((self.directory / name).read_bytes()), "sha256": digest((self.directory / name).read_bytes())} for name in self.profile["payload_files"]},
            manifest_self_hash_included=False, raw_provider_content_persisted=False, task_content_persisted=False,
            api_key_persisted=False, online_execution_authorized=False)
        manifest_bytes = raw(manifest)
        (self.directory / self.profile["manifest_file"]).write_bytes(manifest_bytes)
        publication = self.data["publication"]
        publication["bundle_manifest_file_sha256"] = digest(manifest_bytes)
        publication["timing_evidence_file_sha256"] = digest(raw(evidence))
        publication["publication_commitment_sha256"] = commitment("publication", publication)
        (self.directory / self.profile["publication_file"]).write_bytes(raw(publication))
        self.expected = dict(expected_manifest_sha256=digest(manifest_bytes),
            expected_plan_commitment_sha256=self.data["plan"]["plan_commitment_sha256"],
            expected_execution_binding_sha256=evidence["execution_binding_sha256"],
            expected_denominator_plan_bytes=raw(self.denominator_plan), expected_case_run_ids={self.case: self.run})

    def verify(self, **overrides):
        return verify_timing_artifact_bundle(ROOT, self.directory, **(self.expected | overrides))

    def test_complete_bundle_verifies_actual_chains_without_granting_authority(self):
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        result = self.verify()
        self.assertTrue(result["audit_hash_chains_verified"])
        self.assertTrue(result["timing_constraints_complete"])
        self.assertEqual((result["file_count"], result["run_count"], result["segment_count"]), (7, 1, 1))
        for key in ("signed_expectations_verified", "consumption_verified", "source_budget_tool_semantics_verified",
                    "actual_clock_capture_independently_proved", "runtime_authority_granted", "current_closure_claim_allowed"):
            self.assertFalse(result[key])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory.iterdir()})

    def test_independent_expectations_are_not_replaced_by_manifest_claims(self):
        for field in ("expected_manifest_sha256", "expected_plan_commitment_sha256", "expected_execution_binding_sha256"):
            with self.subTest(field=field), self.assertRaises(TimingContractError):
                self.verify(**{field: "f" * 64})
        with self.assertRaises(TimingContractError):
            self.verify(expected_case_run_ids={self.case: "PCERUN-" + "B" * 32})

    def test_extra_file_or_sidecar_is_not_ignored(self):
        (self.directory / "phase6_audit.sqlite3-wal").write_bytes(b"synthetic")
        with self.assertRaises(TimingContractError):
            self.verify()

    def test_privacy_scan_precedes_sqlite_open(self):
        path = self.directory / "phase6_audit.sqlite3"
        path.write_bytes(path.read_bytes() + b"Authorization: Bearer FAKECANARY")
        with patch("researchops_completion_timing.artifacts._verify_audit_database", side_effect=AssertionError("database must not open")):
            with self.assertRaisesRegex(TimingContractError, "timing_sensitive_content"):
                self.verify()

    def test_rehashed_segment_still_must_match_real_terminal_reference(self):
        segment = self.data["evidence"]["segments"][0]
        segment["send_started_ns"] += 1
        segment["segment_commitment_sha256"] = commitment("segment", segment)
        self.reseal()
        with self.assertRaisesRegex(TimingContractError, "timing_audit_projection_mismatch"):
            self.verify()

    def test_rehashed_phase_overrun_fails_numeric_gate(self):
        self.data["evidence"]["phase"]["phase_terminal_ns"] = 201
        self.reseal()
        with self.assertRaisesRegex(TimingContractError, "timing_phase_timeout_exceeded"):
            self.verify()

    def test_fake_audit_head_is_rejected(self):
        self.data["evidence"]["audit_chain_heads_sha256"] = "f" * 64
        self.reseal()
        with self.assertRaisesRegex(TimingContractError, "timing_audit_heads_mismatch"):
            self.verify()

    def test_denominator_cannot_disagree_with_same_event_record(self):
        # Another valid opaque request-id hash leaves actual event bytes intact.
        record = self.telemetry["runtime_denominator"]["records"][0]
        record["provider_request_id_sha256"]["value"] = "f" * 64
        self.reseal()
        with self.assertRaisesRegex(TimingContractError, "timing_bundle_denominator_mismatch"):
            self.verify()

    def test_actual_database_row_tamper_fails_after_manifest_hash_is_recomputed(self):
        path = self.directory / "phase6_audit.sqlite3"
        before = path.read_bytes()
        needle = self.data["evidence"]["segments"][0]["segment_commitment_sha256"].encode()
        self.assertEqual(before.count(needle), 1)
        path.write_bytes(before.replace(needle, b"f" * 64, 1))
        self.reseal()
        with self.assertRaisesRegex(TimingContractError, "timing_audit_chain_mismatch"):
            self.verify()

    def test_missing_segment_is_not_hidden_by_a_valid_audit_chain(self):
        self.data["evidence"]["segments"] = []
        self.reseal()
        with self.assertRaisesRegex(TimingContractError, "timing_audit_terminal_coverage_missing"):
            self.verify()

    def test_connection_image_swap_is_rejected_even_if_path_bytes_are_unchanged(self):
        original = sqlite3.connect
        with tempfile.TemporaryDirectory() as other:
            shadow = Path(other) / "shadow.sqlite3"
            AuditLedger(shadow)
            calls = []
            def connect(path, **kwargs):
                calls.append(True)
                if len(calls) == 2:
                    return original(shadow.as_uri() + "?mode=ro&immutable=1", **kwargs)
                return original(path, **kwargs)
            with patch("researchops_external_closure.artifact_inputs.sqlite3.connect", side_effect=connect):
                with self.assertRaisesRegex(TimingContractError, "timing_artifact_invalid"):
                    self.verify()
            self.assertEqual(len(calls), 2)

    def test_schema_inventory_query_is_bounded_before_fetching_rows(self):
        original = sqlite3.connect
        queries = []
        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection
        with patch("researchops_external_closure.artifacts.sqlite3.connect", side_effect=connect):
            self.verify()
        inventory = [query for query in queries if query.startswith("SELECT type,name,tbl_name,sql FROM sqlite_master")]
        self.assertEqual(len(inventory), 1)
        self.assertTrue(inventory[0].endswith("LIMIT 12"))

    def test_late_file_change_is_rejected_on_final_snapshot(self):
        from researchops_completion_timing import artifacts
        original = artifacts.read_exact_artifact_directory
        calls = []
        def read(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                path = self.directory / self.profile["publication_file"]
                path.write_bytes(path.read_bytes() + b" ")
            return original(*args, **kwargs)
        with patch.object(artifacts, "read_exact_artifact_directory", side_effect=read):
            with self.assertRaisesRegex(TimingContractError, "timing_bundle_bytes_changed"):
                self.verify()
        self.assertEqual(len(calls), 2)

    def test_publication_missing_does_not_fall_back_to_old_four_file_profile(self):
        (self.directory / self.profile["publication_file"]).unlink()
        with self.assertRaises(TimingContractError):
            self.verify()


if __name__ == "__main__":
    unittest.main()
