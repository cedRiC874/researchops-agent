"""First-live authoring fixtures, not actual live observations or admission."""
from __future__ import annotations

import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import contract as core
from researchops_completion_timing import first_live as first
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import test_completion_timing_contract as authoring
from tests.test_completion_telemetry_ledger import _test_runtime_binding
from researchops_completion_telemetry.sanitization import sanitize_completion_capture, build_completion_record


ROOT = Path(__file__).resolve().parents[1]


class FirstLiveTimingTests(unittest.TestCase):
    def records(self):
        binding = _test_runtime_binding()
        records = []
        for index, (status, cap, output) in enumerate((("completed", 256, 1), ("incomplete", 16, 16))):
            capture = sanitize_completion_capture({"status": status, "incomplete_details": None if index == 0 else {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 5, "output_tokens": output, "total_tokens": 5 + output},
                "http_status": 200, "requested_output_token_cap": cap},
                normalized_usage={"requests": 1, "input_tokens": 5, "output_tokens": output, "total_tokens": 5 + output,
                                  "cached_input_tokens": None, "cache_write_tokens": None, "reasoning_tokens": None})
            records.append(build_completion_record(capture, binding=binding, response_index=index, request_index=index))
        return records

    def test_record_pair_uses_existing_readonly_validator_without_minting_authority(self):
        records = self.records()
        with patch("researchops_completion_telemetry.surface_mapping.VerifiedSurfaceSelection.create_runtime_binding", side_effect=AssertionError("verifier must not mint authority")):
            checked = first.verify_first_live_record_shapes(ROOT, raw({"records": records}))
        self.assertEqual((checked["record_count"], checked["input_tokens"], checked["output_tokens"]), (2, 10, 17))
        self.assertFalse(checked["first_live_success_verified"])
        self.assertFalse(checked["audit_linkage_verified"])

    def test_missing_reordered_or_wrong_cap_records_are_rejected(self):
        records = self.records()
        with self.assertRaisesRegex(core.TimingContractError, "timing_first_live_record_coverage_invalid"):
            first.verify_first_live_record_shapes(ROOT, raw({"records": records[:1]}))
        with self.assertRaisesRegex(core.TimingContractError, "timing_first_live_record_shape_invalid"):
            first.verify_first_live_record_shapes(ROOT, raw({"records": list(reversed(records))}))
        records[0]["output_token_cap"]["value"] = 255
        with self.assertRaisesRegex(core.TimingContractError, "timing_first_live_record_shape_invalid"):
            first.verify_first_live_record_shapes(ROOT, raw({"records": records}))

    def test_two_completed_records_cannot_satisfy_the_capped_scenario(self):
        records = self.records()
        second = copy.deepcopy(records[0])
        second.update(request_index=1, response_index=1, output_token_cap={"availability": "provided", "value": 16})
        with self.assertRaisesRegex(core.TimingContractError, "timing_first_live_record_shape_invalid"):
            first.verify_first_live_record_shapes(ROOT, raw({"records": [records[0], second]}))

    def fixture(self):
        fixture = authoring.TimingAuthoringTests().fixture()
        profile, _, _ = first.load_first_live_timing_contract(ROOT)
        plan, evidence, publication = (fixture[name] for name in ("plan", "evidence", "publication"))
        binding = {key: plan["binding"][key] for key in ("execution_commit", "execution_tree", "source_integrity_commitment_sha256",
            "provider_id", "api_surface", "transport_id", "adapter_version")}
        binding.update(authorization_id_sha256="1" * 64, execution_environment_id="PCEENV-" + "A" * 32)
        binding["validation_run_id"] = first.validation_run_id(binding)
        plan.update(binding=binding, origin_scope="after_verified_local_claim_before_key_load_and_fixed_input_use",
                    request_timeout_ns=120_000_000_000, phase_timeout_ns=300_000_000_000,
                    artifact_sealing_timeout_ns=30_000_000_000, planned_case_handles=profile["case_handles"],
                    max_attempts=2, max_attempts_per_case=1)
        evidence["binding"] = dict(binding, authorization_binding_sha256="2" * 64, local_claim_receipt_sha256="3" * 64)
        for index, segment in enumerate(evidence["segments"]):
            segment["case_handle"] = profile["case_handles"][index]
        for name, document in (("plan", plan), ("evidence", evidence), ("publication", publication)):
            document.update(schema_version=f"provider-completion-first-live-timing-{name}/1.0",
                            document_type=f"completion_first_live_timing_{name}", first_live_profile_sha256=first.PROFILE_SHA256)
        return self.seal(fixture)

    def seal(self, fixture):
        plan, evidence, publication = (fixture[name] for name in ("plan", "evidence", "publication"))
        plan["plan_commitment_sha256"] = first.commitment("plan", plan)
        evidence["plan_commitment_sha256"] = plan["plan_commitment_sha256"]
        evidence["execution_binding_sha256"] = first.commitment("execution_binding", evidence["binding"])
        events = []
        for segment in evidence["segments"]:
            segment.update(plan_commitment_sha256=plan["plan_commitment_sha256"], execution_binding_sha256=evidence["execution_binding_sha256"])
            segment["segment_commitment_sha256"] = first.commitment("segment", segment)
            events.append(dict(schema_version="provider-completion-timing-terminal-projection/1.0",
                event_type="model_response_telemetry_recorded", timing_segment_sha256=segment["segment_commitment_sha256"],
                **{key: segment[key] for key in ("case_handle", "attempt_index", "response_index", "clock_domain_id", "completion_record_sha256", "usage_sha256")}))
        fixture["events"] = {"events": events}
        evidence["evidence_commitment_sha256"] = first.commitment("evidence", evidence)
        publication.update(plan_commitment_sha256=plan["plan_commitment_sha256"], execution_binding_sha256=evidence["execution_binding_sha256"],
                           timing_evidence_file_sha256=core.digest(raw(evidence)))
        publication["publication_commitment_sha256"] = first.commitment("publication", publication)
        return fixture

    def check(self, fixture, function=first.validate_first_live_timing_documents):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            return function(ROOT, plan_bytes=raw(fixture["plan"]), evidence_bytes=raw(fixture["evidence"]),
                terminal_projection_bytes=raw(fixture["events"]), publication_bytes=raw(fixture["publication"]),
                expected_manifest_file_sha256=fixture["publication"]["bundle_manifest_file_sha256"])

    def test_local_claim_binding_is_complete_only_as_authoring_not_first_live_success(self):
        fixture = self.fixture()
        result = self.check(fixture)
        self.assertTrue(result["timing_gate_complete"])
        self.assertFalse(result["first_live_success_verified"])
        self.assertFalse(result["actual_clock_capture_proved"])
        self.assertFalse(result["current_closure_claim_allowed"])
        for name in ("campaign_id", "runtime_plan_core_sha256", "external_consumption_anchor_sha256"):
            self.assertNotIn(name, fixture["evidence"]["binding"])

    def test_campaign_and_first_live_documents_are_not_interchangeable(self):
        with self.assertRaisesRegex(core.TimingContractError, "timing_schema_invalid"):
            self.check(self.fixture(), function=core.validate_documents)
        with self.assertRaisesRegex(core.TimingContractError, "timing_schema_invalid"):
            self.check(authoring.TimingAuthoringTests().fixture())

    def test_domain_separation_preserves_shared_response_segment_domain(self):
        fixture = self.fixture()
        self.assertNotEqual(first.commitment("plan", fixture["plan"]), core.commitment("plan", fixture["plan"]))
        segment = fixture["evidence"]["segments"][0]
        self.assertEqual(first.commitment("segment", segment), core.commitment("segment", segment))

    def test_missing_second_scenario_never_passes_full_timing_gate(self):
        fixture = self.fixture(); fixture["evidence"]["segments"].pop()
        result = self.check(self.seal(fixture))
        self.assertFalse(result["timing_gate_complete"])
        self.assertIn("planned_case_without_accepted_response", result["incomplete_reasons"])

    def test_fixed_caps_case_order_and_local_origin_cannot_be_changed(self):
        for field, value in (("request_timeout_ns", 119_000_000_000), ("max_attempts", 3),
                             ("origin_scope", "after_verified_external_consumption_anchor_before_task_release_and_key_load")):
            fixture = self.fixture(); fixture["plan"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(core.TimingContractError, "timing_schema_invalid"):
                self.check(self.seal(fixture))

    def test_overlap_and_phase_overrun_still_reach_shared_numeric_gates(self):
        fixture = self.fixture(); fixture["evidence"]["segments"][1]["attempt_started_ns"] = 89
        with self.assertRaisesRegex(core.TimingContractError, "timing_concurrency_overlap"):
            self.check(self.seal(fixture))
        fixture = self.fixture(); fixture["evidence"]["phase"]["phase_terminal_ns"] = 300_000_000_001
        fixture["publication"].update(sealing_started_ns=300_000_000_002, sealing_finished_ns=300_000_000_003)
        with self.assertRaisesRegex(core.TimingContractError, "timing_phase_timeout_exceeded"):
            self.check(self.seal(fixture))

    def test_profile_hash_and_derived_run_id_are_checked_after_rehash(self):
        fixture = self.fixture(); fixture["plan"]["first_live_profile_sha256"] = "f" * 64
        with self.assertRaisesRegex(core.TimingContractError, "timing_first_live_profile_mismatch"):
            self.check(self.seal(fixture))
        fixture = self.fixture()
        for binding in (fixture["plan"]["binding"], fixture["evidence"]["binding"]):
            binding["validation_run_id"] = "PCEVALID-" + "F" * 32
        with self.assertRaisesRegex(core.TimingContractError, "timing_first_live_binding_invalid"):
            self.check(self.seal(fixture))


if __name__ == "__main__":
    unittest.main()
