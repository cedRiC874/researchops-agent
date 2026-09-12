"""Signed synthetic two-case graph joined to real temporary SQLite artifacts."""
from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from researchops.audit import AuditLedger, COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION
from researchops.completion_telemetry_ledger import LedgerCompletionTelemetrySession
from researchops_completion_telemetry.capture import CompletionTelemetryCollector
from researchops_completion_telemetry.sanitization import build_completion_record
from researchops_completion_telemetry.surface_mapping import load_and_select_surface_mapping
from researchops_completion_timing.artifacts import PROFILE_SHA256, load_artifact_contract
from researchops_completion_timing.contract import CONTRACT_SHA256, commitment, digest, runtime_plan_core_commitment, TimingContractError
from researchops_completion_timing.ledger_event import LEDGER_CONTRACT_SHA256
from researchops_completion_timing.postrun_link import verify_signed_timing_artifact_link
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import test_completion_timing_contract as authoring
from tests.test_completion_timing_pre_execution import signed_fixture, resign, documents
from tests.test_completion_telemetry_ledger import _test_runtime_binding, _plan_binding, _capture
from tests.test_external_closure_documents import AUDIT_RUN_DOMAIN, _domain_hash
from tests.manual_completion_denominator import manual_denominator


ROOT = Path(__file__).resolve().parents[1]


class SignedTimingBundle:
    def __init__(self, directory, *, overlap=False, execution_scope=None):
        self.directory = directory
        self.fixture, self.plan = signed_fixture()
        self.profile, _ = load_artifact_contract(ROOT)
        docs = self.fixture.documents
        runtime_binding = _test_runtime_binding()
        binding = runtime_binding.runtime_snapshot()
        envelope = docs["preregistration_envelope"]
        runtime, execution = envelope["runtime_plan"], envelope["execution_binding"]
        if execution_scope is not None:
            runtime["execution_scope"] = copy.deepcopy(execution_scope)
        self.denominator_plan = runtime["denominator_plan"]
        for key, value in binding.items():
            if key in self.denominator_plan:
                self.denominator_plan[key] = value
            if key in execution:
                execution[key] = value
        denominator_hash = digest(raw(self.denominator_plan))
        runtime["denominator_plan_commitment_sha256"] = denominator_hash
        docs["authorization_grant"]["denominator_plan_commitment_sha256"] = denominator_hash
        docs["consumption_receipt"]["denominator_plan_commitment_sha256"] = denominator_hash
        self.plan["binding"].update({key: binding[key] for key in ("provider_id", "api_surface", "transport_id", "adapter_version")})
        self.plan["binding"]["runtime_plan_core_sha256"] = runtime_plan_core_commitment(runtime)
        resign(self.fixture, self.plan, envelope_version=2 if execution_scope is None else 3)
        self.cases = self.denominator_plan["case_ids"]
        assert len(self.cases) == 2
        self.run_ids = {case: "PCERUN-" + _domain_hash(AUDIT_RUN_DOMAIN, envelope["envelope_id"].encode(), case.encode())[:32].upper() for case in self.cases}
        execution_binding = dict(self.plan["binding"], authorization_grant_sha256=docs["authorization_grant"]["document_sha256"],
            external_consumption_anchor_sha256=digest(raw(self.fixture.observation["authorization_consumed_anchor"])),
            runtime_plan_commitment_sha256=runtime["external_plan_binding_sha256"])
        author = authoring.TimingAuthoringTests().fixture()
        self.evidence = author["evidence"]
        self.evidence.update(binding=execution_binding, plan_commitment_sha256=self.plan["plan_commitment_sha256"],
                             execution_binding_sha256=commitment("execution_binding", execution_binding))
        if overlap:
            self.evidence["segments"][1]["attempt_started_ns"] = self.evidence["segments"][0]["lifetime_ended_ns"] - 1
        self.publication = author["publication"]
        self.publication.update(plan_commitment_sha256=self.plan["plan_commitment_sha256"],
                               execution_binding_sha256=self.evidence["execution_binding_sha256"])
        ledger = AuditLedger(directory / "phase6_audit.sqlite3", clock=lambda: datetime(2026, 9, 4, 0, 0, 20, tzinfo=timezone.utc))
        plan_binding = _plan_binding(runtime_binding, case_ids=tuple(self.cases), max_turns=self.denominator_plan["max_turns_per_case"],
                                     request_cap=self.denominator_plan["total_model_request_cap"])
        tracker = CompletionTelemetryCollector.for_runtime(plan_binding)
        index_runs, records, heads = [], [], []
        for ordinal, case in enumerate(self.cases):
            run_id = self.run_ids[case]
            ledger.start_run(mode="signed-timing-fixture", request_summary={"purpose": "synthetic"}, run_id=run_id)
            session = tracker.bind_case(case)
            bridge = LedgerCompletionTelemetrySession(session, ledger=ledger, run_id=run_id, runtime_plan_binding=plan_binding)
            handle = bridge.begin_attempt()
            terminal = session.finalize_response_accepted(handle, _capture())
            record = build_completion_record(terminal._capture._collector_snapshot(), binding=runtime_binding,
                                             response_index=ordinal, request_index=ordinal)
            records.append(record)
            segment = self.evidence["segments"][ordinal]
            segment.update(case_handle=case, plan_commitment_sha256=self.plan["plan_commitment_sha256"],
                           execution_binding_sha256=self.evidence["execution_binding_sha256"],
                           completion_record_sha256=digest(raw(record)), usage_sha256=digest(raw(record["usage"])))
            segment["segment_commitment_sha256"] = commitment("segment", segment)
            payload = dict(schema_version=COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION, case_id=case,
                attempt_index=ordinal, case_attempt_index=0, terminal_kind="response_accepted", response_index=ordinal,
                error_code=None, binding=binding, completion_record=record)
            event_hash = AuditLedger.append_timed_completion_telemetry_event(ledger, "model_response_telemetry_recorded", payload,
                capability=bridge._capability, attempt_handle=handle, terminal=terminal, segment_bytes=raw(segment),
                expected_timing_binding={key: segment[key] for key in ("plan_commitment_sha256", "execution_binding_sha256", "clock_domain_id")})
            event_bridge = bridge.event_commitment()
            event_bridge.update(terminals=[{"attempt_index": ordinal, "event_hash": event_hash}], all_started_attempts_terminal=True)
            event_bridge.pop("commitment_sha256")
            event_bridge["commitment_sha256"] = digest(raw(event_bridge))
            ledger.set_run_status(run_id, "completed")
            chain = asdict(ledger.verify_chain(run_id))
            index_runs.append(dict(task_id=case, run_id=run_id, provider="deepseek", transport="openai_compatible_responses",
                                   chain_verification=chain, completion_telemetry_event_commitment=event_bridge))
            heads.append({"run_id": run_id, "final_chain_head_sha256": chain["chain_head"]})
        self.evidence["audit_chain_heads_sha256"] = digest(self.profile["ordered_audit_heads_domain"].encode() + b"\0final_chain_heads\0" + raw(heads))
        selection = load_and_select_surface_mapping(ROOT, "deepseek", "responses", "openai_compatible_responses", purpose="offline_validation")
        _, denominator = manual_denominator(selection, binding, self.cases)
        denominator.update(records=records, preregistration_commitment=denominator_hash,
                           max_turns_per_case=self.denominator_plan["max_turns_per_case"], total_model_request_cap=self.denominator_plan["total_model_request_cap"])
        self.telemetry = dict(schema_version="phase6-completion-telemetry/1.0", status="recorded", runtime_plan=self.denominator_plan,
            runtime_denominator=denominator, ledger_reconciliation={}, closure={}, historical_backfill_performed=False)
        self.index = {"schema_version": "1.1", "runs": index_runs}
        self.post = copy.deepcopy(self.fixture.observation)
        self.post.update(schema_version="provider-completion-external-observation-bundle/1.0", mode="pre_receipt",
            closure_receipt_observation=None, closure_evidence_anchor=None,
            manifest_observation=dict(state="present", manifest_sha256="0" * 64, observed_at_utc="2026-09-04T00:01:00Z",
                observation_source_commitment_sha256=digest(b"postrun synthetic observation"), last_completed_artifact_write_stage="manifest_written"))
        self.reseal()

    def reseal(self):
        self.evidence["evidence_commitment_sha256"] = commitment("evidence", self.evidence)
        for name, value in {"phase6_audit_index.json": self.index, "phase6_completion_telemetry.json": self.telemetry,
                            "completion_timing_plan.json": self.plan, "completion_timing_evidence.json": self.evidence}.items():
            (self.directory / name).write_bytes(raw(value))
        manifest = dict(schema_version="provider-completion-timed-bundle-manifest/1.0", status="sealed_payloads",
            campaign_id=self.plan["binding"]["campaign_id"], artifact_contract_sha256=PROFILE_SHA256, timing_contract_sha256=CONTRACT_SHA256,
            timed_ledger_contract_sha256=LEDGER_CONTRACT_SHA256, plan_commitment_sha256=self.plan["plan_commitment_sha256"],
            execution_binding_sha256=self.evidence["execution_binding_sha256"],
            files={name: {"bytes": len((self.directory / name).read_bytes()), "sha256": digest((self.directory / name).read_bytes())} for name in self.profile["payload_files"]},
            manifest_self_hash_included=False, raw_provider_content_persisted=False, task_content_persisted=False, api_key_persisted=False,
            online_execution_authorized=False)
        manifest_bytes = raw(manifest)
        (self.directory / self.profile["manifest_file"]).write_bytes(manifest_bytes)
        self.publication.update(bundle_manifest_file_sha256=digest(manifest_bytes), timing_evidence_file_sha256=digest(raw(self.evidence)))
        self.publication["publication_commitment_sha256"] = commitment("publication", self.publication)
        (self.directory / self.profile["publication_file"]).write_bytes(raw(self.publication))
        self.post["manifest_observation"]["manifest_sha256"] = digest(manifest_bytes)

    def check(self):
        return verify_signed_timing_artifact_link(ROOT, self.directory, documents(self.fixture),
            pre_execution_observation=raw(self.fixture.observation), postrun_observation=raw(self.post),
            timing_plan_bytes=raw(self.plan), verification_time_utc="2026-09-04T00:00:17Z")


class SignedTimingLinkTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.socket", side_effect=AssertionError("network forbidden"))
        self.network.start(); self.addCleanup(self.network.stop)
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.bundle = SignedTimingBundle(Path(self.temporary.name))

    def test_real_signatures_and_two_case_sqlite_link_without_verifier_stubs(self):
        result = self.bundle.check()
        self.assertTrue(result["signed_timing_graph_verified"])
        self.assertTrue(result["audit_hash_chains_verified"])
        self.assertTrue(result["timing_constraints_complete"])
        self.assertEqual((result["run_count"], result["segment_count"]), (2, 2))
        for key in ("runtime_authority_granted", "current_closure_claim_allowed", "current_source_review_admission_verified",
                    "global_one_shot_execution_proved", "oob_origin_independently_proved"):
            self.assertFalse(result[key])

    def test_postrun_cannot_rewrite_pre_execution_observation_history(self):
        self.bundle.post["authorization_consumed_anchor"]["observation_source_commitment_sha256"] = "f" * 64
        with self.assertRaisesRegex(TimingContractError, "timing_postrun_observation_history_mismatch"):
            self.bundle.check()

    def test_equal_python_numbers_do_not_hide_changed_canonical_observation_bytes(self):
        anchor = self.bundle.post["authorization_consumed_anchor"]
        anchor["sequence"] = float(anchor["sequence"])
        with self.assertRaisesRegex(TimingContractError, "timing_postrun_observation_history_mismatch"):
            self.bundle.check()

    def test_wrong_manifest_observation_is_not_overridden_by_directory(self):
        self.bundle.post["manifest_observation"]["manifest_sha256"] = "f" * 64
        with self.assertRaisesRegex(TimingContractError, "timing_bundle_manifest_hash_mismatch"):
            self.bundle.check()

    def test_manifest_observation_must_follow_consumption_and_as_of_proof(self):
        self.bundle.post["manifest_observation"]["observed_at_utc"] = "2026-09-04T00:00:16Z"
        with self.assertRaisesRegex(TimingContractError, "timing_postrun_observation_time_invalid"):
            self.bundle.check()

    def test_rehashed_phase_overrun_remains_rejected_through_signed_entrypoint(self):
        self.bundle.evidence["phase"]["phase_terminal_ns"] = self.bundle.plan["phase_timeout_ns"] + 1
        self.bundle.publication.update(sealing_started_ns=self.bundle.plan["phase_timeout_ns"] + 2,
                                       sealing_finished_ns=self.bundle.plan["phase_timeout_ns"] + 3)
        self.bundle.reseal()
        with self.assertRaisesRegex(TimingContractError, "timing_phase_timeout_exceeded"):
            self.bundle.check()

    def test_overlapping_lifetimes_reject_even_with_matching_signatures_and_audit_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            overlapping = SignedTimingBundle(Path(temporary), overlap=True)
            with self.assertRaisesRegex(TimingContractError, "timing_concurrency_overlap"):
                overlapping.check()


if __name__ == "__main__":
    unittest.main()
