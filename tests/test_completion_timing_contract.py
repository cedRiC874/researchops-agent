"""Synthetic authoring conformance only; no clock, Adapter or Provider call."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("timing_contract_authoring", ROOT / "scripts/validate_completion_timing_contract.py")
timing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(timing)


def h(label):
    return hashlib.sha256(label.encode()).hexdigest()


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class TimingAuthoringTests(unittest.TestCase):
    def fixture(self):
        binding = {"campaign_id": "PCECAMP-" + "A" * 32, "execution_commit": "1" * 40,
                   "execution_tree": "2" * 40, "source_integrity_commitment_sha256": h("source"),
                   "runtime_plan_core_sha256": h("runtime core"), "provider_id": "deepseek",
                   "api_surface": "responses", "transport_id": "openai_compatible_responses",
                   "adapter_version": "deepseek-responses-adapter/1.0"}
        cases = ["PCECASE-" + "1" * 32, "PCECASE-" + "2" * 32]
        plan = {"schema_version": "provider-completion-timing-plan/1.0", "document_type": "completion_timing_plan",
                "timing_contract_sha256": timing.CONTRACT_SHA256, "binding": binding,
                "clock_source": "time.monotonic_ns", "clock_unit": "relative_integer_nanoseconds",
                "clock_domain_policy": "single_process_single_epoch_no_resume",
                "origin_scope": "after_verified_external_consumption_anchor_before_task_release_and_key_load",
                "origin_offset_ns": 0, "request_timeout_ns": 50, "phase_timeout_ns": 200,
                "artifact_sealing_timeout_ns": 100, "planned_case_handles": cases, "max_attempts": 2,
                "max_attempts_per_case": 1, "concurrency": 1, "client_retries": 0, "resume": False,
                "fallback": False, "online_execution_authorized": False}
        live_binding = binding | {"authorization_grant_sha256": h("grant"),
                                  "external_consumption_anchor_sha256": h("external consumption"),
                                  "runtime_plan_commitment_sha256": h("full runtime")}
        clock = "PCECLOCK-" + "B" * 32
        segments = []
        for index, offsets in enumerate(((30, 40, 80, 90), (90, 100, 150, 170))):
            start, send, terminal, cleanup = offsets
            segments.append({"schema_version": "provider-completion-timing-segment/1.0", "document_type": "completion_timing_segment",
                "clock_domain_id": clock, "case_handle": cases[index], "attempt_index": index, "case_attempt_index": 0,
                "request_index": index, "response_index": index, "terminal_kind": "response_accepted", "attempt_started_ns": start,
                "send_started_ns": send, "transport_terminal_ns": terminal, "raw_cleanup_terminal_ns": cleanup,
                "raw_cleanup_status": "completed", "lifetime_ended_ns": cleanup,
                "completion_record_sha256": h("record" + str(index)), "usage_sha256": h("usage" + str(index))})
        evidence = {"schema_version": "provider-completion-timing-evidence/1.0", "document_type": "completion_timing_evidence",
            "timing_contract_sha256": timing.CONTRACT_SHA256, "binding": live_binding, "clock_domain_id": clock,
            "clock_source": "time.monotonic_ns", "clock_unit": "relative_integer_nanoseconds", "origin_offset_ns": 0,
            # A simulated label exercises the branch; the result must NOT claim
            # actual clock capture, authoritative ledger, or current closure.
            "record_provenance": "live_monotonic_capture", "segments": segments,
            "phase": {"task_released_ns": 10, "key_loaded_ns": 20, "post_cleanup_terminal_ns": 180,
                      "post_cleanup_status": "completed", "key_reference_released_ns": 190,
                      "key_reference_release_status": "released", "phase_terminal_ns": 200},
            "audit_chain_heads_sha256": h("untrusted authoring head projection")}
        publication = {"schema_version": "provider-completion-timing-publication/1.0", "document_type": "completion_timing_publication",
            "timing_contract_sha256": timing.CONTRACT_SHA256, "clock_domain_id": clock,
            "bundle_manifest_file_sha256": h("manifest"), "sealing_started_ns": 210, "sealing_finished_ns": 310,
            "sealing_status": "sealed", "sealing_scope": "bounded_artifact_set_before_publication_receipt",
            "online_execution_authorized": False}
        return self.seal({"plan": plan, "evidence": evidence, "publication": publication})

    def seal(self, fixture, *, rebuild_events=True):
        plan, evidence, publication = (fixture[key] for key in ("plan", "evidence", "publication"))
        plan["plan_commitment_sha256"] = timing.commitment("plan", plan)
        evidence["plan_commitment_sha256"] = plan["plan_commitment_sha256"]
        evidence["execution_binding_sha256"] = timing.commitment("execution_binding", evidence["binding"])
        events = []
        for segment in evidence["segments"]:
            segment["plan_commitment_sha256"] = plan["plan_commitment_sha256"]
            segment["execution_binding_sha256"] = evidence["execution_binding_sha256"]
            segment["segment_commitment_sha256"] = timing.commitment("segment", segment)
            events.append({"schema_version": "provider-completion-timing-terminal-projection/1.0",
                "event_type": sorted(timing._EVENTS[segment["terminal_kind"]])[0],
                **{key: segment[key] for key in ("case_handle", "attempt_index", "response_index", "clock_domain_id", "completion_record_sha256", "usage_sha256")},
                "timing_segment_sha256": segment["segment_commitment_sha256"]})
        if rebuild_events:
            fixture["events"] = {"events": events}
        evidence["evidence_commitment_sha256"] = timing.commitment("evidence", evidence)
        publication["plan_commitment_sha256"] = plan["plan_commitment_sha256"]
        publication["execution_binding_sha256"] = evidence["execution_binding_sha256"]
        publication["timing_evidence_file_sha256"] = timing.digest(raw(evidence))
        publication["publication_commitment_sha256"] = timing.commitment("publication", publication)
        return fixture

    def check(self, fixture, **overrides):
        values = dict(plan_bytes=raw(fixture["plan"]), evidence_bytes=raw(fixture["evidence"]),
                      terminal_projection_bytes=raw(fixture["events"]), publication_bytes=raw(fixture["publication"]),
                      expected_manifest_file_sha256=h("manifest"))
        values.update(overrides)
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            return timing.validate_documents(ROOT, **values)

    def rejected(self, fixture, code):
        with self.assertRaisesRegex(timing.TimingContractError, "^" + code + "$"):
            self.check(fixture)

    def test_frozen_contract_and_all_five_schema_bytes_verify(self):
        contract, schemas = timing.load_contract(ROOT)
        self.assertEqual(len(schemas), 5)
        self.assertFalse(contract["claim_boundary"]["provider_calls_authorized"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / timing.RELATIVE, root / timing.RELATIVE)
            path = root / timing.RELATIVE / "schemas/completion_timing_plan_v1.schema.json"
            path.write_bytes(path.read_bytes() + b"\n")
            with self.assertRaisesRegex(timing.TimingContractError, "timing_contract_invalid"):
                timing.load_contract(root)

    def test_exact_caps_and_sequential_equality_are_consistent_but_not_authority(self):
        result = self.check(self.fixture())
        self.assertTrue(result["timing_gate_complete"])
        for key in ("authoritative_ledger_verified", "actual_clock_capture_proved", "runtime_authority_granted", "current_closure_claim_allowed"):
            self.assertFalse(result[key])

    def test_over_request_phase_and_sealing_caps_reject_after_rehash(self):
        fixture = self.fixture()
        first = fixture["evidence"]["segments"][0]
        first.update(transport_terminal_ns=91, raw_cleanup_terminal_ns=92, lifetime_ended_ns=92)
        fixture["evidence"]["segments"][1]["attempt_started_ns"] = 92
        self.rejected(self.seal(fixture), "timing_request_timeout_exceeded")
        fixture = self.fixture(); fixture["evidence"]["phase"]["phase_terminal_ns"] = 201
        self.rejected(self.seal(fixture), "timing_phase_timeout_exceeded")
        fixture = self.fixture(); fixture["publication"]["sealing_finished_ns"] = 311
        self.rejected(self.seal(fixture), "timing_sealing_timeout_exceeded")

    def test_overlap_reversed_time_and_mixed_domains_reject(self):
        fixture = self.fixture(); fixture["evidence"]["segments"][1]["attempt_started_ns"] = 89
        self.rejected(self.seal(fixture), "timing_concurrency_overlap")
        fixture = self.fixture(); fixture["evidence"]["segments"][0]["transport_terminal_ns"] = 39
        self.rejected(self.seal(fixture), "timing_clock_order_invalid")
        fixture = self.fixture(); fixture["evidence"]["segments"][1]["clock_domain_id"] = "PCECLOCK-" + "C" * 32
        self.rejected(self.seal(fixture), "timing_clock_domain_mismatch")

    def test_600_661_case_and_legacy_receipt_have_no_timing_fallback(self):
        fixture = self.fixture()
        second = 1_000_000_000
        fixture["plan"].update(request_timeout_ns=100 * second, phase_timeout_ns=600 * second,
                               artifact_sealing_timeout_ns=100 * second)
        fixture["evidence"]["segments"][0].update(attempt_started_ns=3 * second, send_started_ns=4 * second,
            transport_terminal_ns=5 * second, raw_cleanup_terminal_ns=6 * second, lifetime_ended_ns=6 * second)
        fixture["evidence"]["segments"][1].update(attempt_started_ns=650 * second, send_started_ns=660 * second,
            transport_terminal_ns=665 * second, raw_cleanup_terminal_ns=670 * second, lifetime_ended_ns=670 * second)
        fixture["evidence"]["phase"].update(task_released_ns=second, key_loaded_ns=2 * second,
            post_cleanup_terminal_ns=680 * second, key_reference_released_ns=690 * second, phase_terminal_ns=700 * second)
        fixture["publication"].update(sealing_started_ns=710 * second, sealing_finished_ns=720 * second)
        self.assertEqual((665 - 4), 661)
        self.rejected(self.seal(fixture), "timing_phase_timeout_exceeded")
        legacy = (ROOT / "docs/diagnostics/t6c-timeout-admission-review-v1.json").read_bytes()
        with self.assertRaisesRegex(timing.TimingContractError, "timing_schema_invalid"):
            self.check(self.fixture(), evidence_bytes=legacy)

    def test_missing_null_float_boolean_and_negative_are_not_interchangeable(self):
        for value in (1.0, True, -1):
            fixture = self.fixture(); fixture["evidence"]["segments"][0]["attempt_started_ns"] = value
            self.rejected(self.seal(fixture), "timing_schema_invalid")
        fixture = self.fixture(); del fixture["evidence"]["phase"]["phase_terminal_ns"]
        self.rejected(self.seal(fixture), "timing_schema_invalid")
        fixture = self.fixture(); fixture["evidence"]["phase"]["phase_terminal_ns"] = None
        self.assertFalse(self.check(self.seal(fixture))["timing_gate_complete"])
        fixture = self.fixture(); fixture["evidence"]["segments"][1].update(
            transport_terminal_ns=None, raw_cleanup_terminal_ns=None, lifetime_ended_ns=None)
        self.assertFalse(self.check(self.seal(fixture))["timing_gate_complete"])

    def test_missing_duplicate_and_mismatched_response_coverage_reject(self):
        fixture = self.fixture(); fixture["events"]["events"].pop()
        self.rejected(fixture, "timing_projection_mismatch")
        fixture = self.fixture(); fixture["evidence"]["segments"][1] = copy.deepcopy(fixture["evidence"]["segments"][0])
        self.rejected(self.seal(fixture), "timing_index_invalid")
        fixture = self.fixture(); fixture["evidence"]["segments"][1]["response_index"] = 0
        self.rejected(self.seal(fixture), "timing_index_invalid")
        fixture = self.fixture(); fixture["evidence"]["segments"].pop()
        self.assertFalse(self.check(self.seal(fixture))["timing_gate_complete"])

    def test_tamper_with_and_without_recomputed_hashes_rejects(self):
        fixture = self.fixture(); fixture["evidence"]["segments"][0]["usage_sha256"] = h("tamper")
        self.rejected(fixture, "timing_commitment_mismatch")
        self.rejected(self.seal(fixture, rebuild_events=False), "timing_projection_mismatch")

    def test_failure_no_send_and_unknown_publication_stay_incomplete(self):
        fixture = self.fixture()
        fixture["evidence"]["segments"][1].update(terminal_kind="no_response", send_started_ns=None,
            transport_terminal_ns=120, raw_cleanup_terminal_ns=None, raw_cleanup_status="not_applicable",
            lifetime_ended_ns=120, response_index=None, completion_record_sha256=None, usage_sha256=None)
        self.assertFalse(self.check(self.seal(fixture))["timing_gate_complete"])
        fixture = self.fixture(); fixture["publication"]["sealing_finished_ns"] = None
        self.assertFalse(self.check(self.seal(fixture))["timing_gate_complete"])
        self.assertFalse(self.check(self.fixture(), publication_bytes=None)["timing_gate_complete"])

    def test_synthetic_or_historical_labels_and_unmapped_do_not_grant_closure(self):
        for provenance in ("synthetic_fixture", "not_persisted"):
            fixture = self.fixture(); fixture["evidence"]["record_provenance"] = provenance
            self.assertFalse(self.check(self.seal(fixture))["timing_gate_complete"])
        fixture = self.fixture()
        fixture["events"]["events"][0]["event_type"] = "provider_completion_mapping_unmapped"
        result = self.check(fixture)
        self.assertFalse(result["current_closure_claim_allowed"])

    def test_key_authorization_paths_canaries_and_unknown_fields_are_blocked(self):
        for value in ("sk-FAKECANARY12345678", "Authorization: Bearer fake", "C:\\Users\\synthetic\\private"):
            fixture = self.fixture(); fixture["evidence"]["unexpected"] = value
            self.rejected(fixture, "timing_sensitive_content")
        fixture = self.fixture(); fixture["evidence"]["unexpected"] = "ordinary"
        self.rejected(fixture, "timing_schema_invalid")
        fixture = self.fixture(); canary = fixture["evidence"]["binding"]["authorization_grant_sha256"].encode()
        with self.assertRaisesRegex(timing.TimingContractError, "timing_sensitive_content"):
            self.check(fixture, sensitive_canaries=(canary,))

    def test_publication_requires_independent_manifest_expectation(self):
        with self.assertRaisesRegex(timing.TimingContractError, "timing_binding_mismatch"):
            self.check(self.fixture(), expected_manifest_file_sha256=h("wrong"))
        with self.assertRaisesRegex(timing.TimingContractError, "timing_binding_mismatch"):
            self.check(self.fixture(), expected_manifest_file_sha256=None)

    def test_hash_graph_omits_only_explicit_self_references(self):
        core = {"budget": {"limit": 10}, "timing_plan_commitment_sha256": h("time"), "external_plan_binding_sha256": h("self")}
        before = timing.runtime_plan_core_commitment(core)
        core["timing_plan_commitment_sha256"] = h("new time")
        core["external_plan_binding_sha256"] = h("new self")
        self.assertEqual(before, timing.runtime_plan_core_commitment(core))
        core["budget"]["limit"] = 11
        self.assertNotEqual(before, timing.runtime_plan_core_commitment(core))

    def test_post_cleanup_and_key_release_can_finish_in_either_order(self):
        fixture = self.fixture(); fixture["evidence"]["phase"].update(post_cleanup_terminal_ns=190, key_reference_released_ns=180)
        self.assertTrue(self.check(self.seal(fixture))["timing_gate_complete"])


if __name__ == "__main__":
    unittest.main()
