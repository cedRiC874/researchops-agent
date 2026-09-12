"""Deterministically signed synthetic v2 graphs; never runtime authorization."""
from __future__ import annotations

import copy
import inspect
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing.contract import TimingContractError, commitment, runtime_plan_core_commitment
from researchops_completion_timing.pre_execution import VerifiedTimedPreExecutionDocuments, verify_timed_pre_execution
from researchops_external_closure.documents import verify_pre_receipt_documents
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.primitives import canonical_json_bytes as raw
from researchops_external_closure.types import PreReceiptDocumentBytes
from tests import test_completion_timing_contract as authoring
from tests.test_external_closure_documents import SyntheticPreReceipt, RUNTIME_PLAN_DOMAIN, USER_BINDING_DOMAIN, _domain_hash, _schemas


ROOT = Path(__file__).resolve().parents[1]


def signed_fixture():
    fixture = SyntheticPreReceipt()
    plan = authoring.TimingAuthoringTests().fixture()["plan"]
    envelope = fixture.documents["preregistration_envelope"]
    runtime, execution = envelope["runtime_plan"], envelope["execution_binding"]
    denominator, limits = runtime["denominator_plan"], runtime["transport_limits"]
    plan["binding"] = {key: execution[key] for key in ("execution_commit", "execution_tree", "source_integrity_commitment_sha256")}
    plan["binding"].update({key: denominator[key] for key in ("provider_id", "api_surface", "transport_id", "adapter_version")})
    plan["binding"].update(campaign_id=runtime["campaign_topology"]["campaign_id"], runtime_plan_core_sha256=runtime_plan_core_commitment(runtime))
    plan.update(planned_case_handles=list(denominator["case_ids"]), max_attempts=denominator["total_model_request_cap"],
                max_attempts_per_case=denominator["max_turns_per_case"], request_timeout_ns=limits["request_timeout_seconds"] * 1_000_000_000,
                phase_timeout_ns=limits["total_timeout_seconds"] * 1_000_000_000)
    fixture.observation = {key: value for key, value in fixture.observation.items()
                           if key not in {"manifest_observation", "closure_receipt_observation", "closure_evidence_anchor"}}
    fixture.observation.update(schema_version="provider-completion-external-pre-execution-observation/1.0", mode="pre_execution")
    resign(fixture, plan)
    return fixture, plan


def resign(fixture, plan, *, envelope_version=2):
    docs = fixture.documents
    envelope, grant, consumption = (docs[key] for key in ("preregistration_envelope", "authorization_grant", "consumption_receipt"))
    envelope["schema_version"] = f"provider-completion-external-preregistration/{envelope_version}.0"
    plan["plan_commitment_sha256"] = commitment("plan", plan)
    runtime = envelope["runtime_plan"]
    runtime["timing_plan_commitment_sha256"] = plan["plan_commitment_sha256"]
    body = dict(runtime); body.pop("external_plan_binding_sha256")
    runtime["external_plan_binding_sha256"] = _domain_hash(RUNTIME_PLAN_DOMAIN, raw(body))
    fixture._finalize_document(envelope, ("freeze_authority", "task_custodian"))
    previous = docs["preregistration_frozen_ledger_entry"]
    prereg = fixture._ledger_entry(entry_type="preregistration_frozen", sequence=previous["sequence"],
        previous_head=previous["previous_head_sha256"], occurred_at=previous["occurred_at_utc"],
        candidate_hash=docs["candidate_freeze_receipt"]["document_sha256"], envelope_hash=envelope["document_sha256"],
        grant_hash=None, consumption_hash=None)
    docs["preregistration_frozen_ledger_entry"] = prereg
    grant.update(preregistration_envelope_sha256=envelope["document_sha256"], preregistration_freeze_entry_sha256=prereg["entry_sha256"],
                 ledger_head_sha256=prereg["resulting_head_sha256"], external_plan_binding_sha256=runtime["external_plan_binding_sha256"])
    body = {key: value for key, value in grant.items() if key not in {"explicit_user_authorization_binding_sha256", "document_sha256", "signatures"}}
    grant["explicit_user_authorization_binding_sha256"] = _domain_hash(USER_BINDING_DOMAIN, raw(body))
    fixture._finalize_document(grant, ("freeze_authority", "task_custodian"))
    consumption.update(preregistration_envelope_sha256=envelope["document_sha256"], preregistration_freeze_entry_sha256=prereg["entry_sha256"],
                       authorization_grant_sha256=grant["document_sha256"], external_plan_binding_sha256=runtime["external_plan_binding_sha256"])
    fixture._finalize_document(consumption)
    old = docs["authorization_consumed_ledger_entry"]
    consumed = fixture._ledger_entry(entry_type="authorization_consumed", sequence=old["sequence"], previous_head=prereg["resulting_head_sha256"],
        occurred_at=old["occurred_at_utc"], candidate_hash=docs["candidate_freeze_receipt"]["document_sha256"],
        envelope_hash=envelope["document_sha256"], grant_hash=grant["document_sha256"], consumption_hash=consumption["document_sha256"])
    docs["authorization_consumed_ledger_entry"] = consumed
    for key, entry in (("preregistration_freeze_anchor", prereg), ("authorization_consumed_anchor", consumed)):
        for name in ("entry_sha256", "ledger_id", "entry_type", "sequence", "previous_head_sha256", "resulting_head_sha256"):
            fixture.observation[key][name] = entry[name]
    fixture.observation["preregistration_envelope_observation"]["document_sha256"] = envelope["document_sha256"]
    fixture.observation["user_authorization_observation"]["authorization_binding_sha256"] = grant["explicit_user_authorization_binding_sha256"]


def documents(fixture):
    return PreReceiptDocumentBytes(**{key: raw(fixture.documents[key]) for key in PreReceiptDocumentBytes.__dataclass_fields__})


class TimedPreExecutionTests(unittest.TestCase):
    def test_legacy_timeline_source_is_identical_to_preserved_c1(self):
        from researchops_external_closure import documents as module
        baseline = (ROOT / "docs/diagnostics/t6c-source-baseline-c1c25226-v1/files/src/researchops_external_closure/documents.py").read_text(encoding="utf-8")
        original = baseline[baseline.index("def _verify_timeline("):baseline.index("def verify_pre_receipt_documents(")].strip()
        self.assertEqual(inspect.getsource(module._verify_timeline).strip(), original)

    def check(self, fixture, plan, *, as_of="2026-09-04T00:00:17Z"):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
             patch("time.monotonic_ns", side_effect=AssertionError("clock forbidden")):
            return verify_timed_pre_execution(ROOT, documents(fixture), external_observation_bundle=raw(fixture.observation),
                                              timing_plan_bytes=raw(plan), verification_time_utc=as_of)

    def test_signed_v2_graph_verifies_without_postrun_observation_or_runtime_authority(self):
        fixture, plan = signed_fixture()
        before = raw(fixture.documents), raw(fixture.observation), raw(plan)
        checked = self.check(fixture, plan)
        self.assertEqual(checked.timing_plan_commitment_sha256, plan["plan_commitment_sha256"])
        self.assertEqual(checked.runtime_plan_core_sha256, plan["binding"]["runtime_plan_core_sha256"])
        self.assertEqual(checked.runtime_plan_commitment_sha256, fixture.documents["authorization_grant"]["external_plan_binding_sha256"])
        summary = checked.summary()
        for key in ("runtime_authority_granted", "current_closure_claim_allowed", "one_shot_execution_claimed", "independent_current_time_proved"):
            self.assertFalse(summary[key])
        self.assertNotIn("manifest_observation", fixture.observation)
        self.assertEqual(before, (raw(fixture.documents), raw(fixture.observation), raw(plan)))
        with self.assertRaises(FrozenInstanceError):
            checked.verified_as_of_utc = "2099-01-01T00:00:00Z"
        with self.assertRaises(TypeError):
            VerifiedTimedPreExecutionDocuments()

    def test_pre_consumption_and_expired_as_of_times_reject(self):
        fixture, plan = signed_fixture()
        for value in ("2026-09-04T00:00:14Z", "2026-09-04T01:00:00Z", "2026-09-06T00:00:00Z"):
            with self.subTest(as_of=value), self.assertRaisesRegex(TimingContractError, "timing_pre_execution_as_of_invalid"):
                self.check(fixture, plan, as_of=value)

    def test_postrun_fields_are_forbidden_even_if_null(self):
        for name in ("manifest_observation", "closure_receipt_observation", "closure_evidence_anchor"):
            fixture, plan = signed_fixture(); fixture.observation[name] = None
            with self.subTest(field=name), self.assertRaisesRegex(ExternalClosurePrimitiveError, "timing_pre_execution_observation_invalid"):
                self.check(fixture, plan)

    def test_changed_plan_cannot_reuse_the_old_signed_grant(self):
        fixture, plan = signed_fixture()
        plan["artifact_sealing_timeout_ns"] += 1
        plan["plan_commitment_sha256"] = commitment("plan", plan)
        with self.assertRaisesRegex(TimingContractError, "timing_pre_execution_plan_mismatch"):
            self.check(fixture, plan)

    def test_resigning_does_not_hide_core_binding_or_timeout_mismatch(self):
        for mutate, code in ((lambda p: p["binding"].update(runtime_plan_core_sha256="f" * 64), "timing_pre_execution_binding_mismatch"),
                             (lambda p: p.update(request_timeout_ns=p["request_timeout_ns"] - 1), "timing_pre_execution_limits_invalid")):
            fixture, plan = signed_fixture(); mutate(plan); resign(fixture, plan)
            with self.subTest(code=code), self.assertRaisesRegex(TimingContractError, code):
                self.check(fixture, plan)

    def test_invalid_signature_and_external_anchor_mismatch_reject(self):
        fixture, plan = signed_fixture()
        fixture.documents["authorization_grant"]["signatures"][0]["signature_b64"] = "A" * 86 + "=="
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "external_closure_signature_invalid"):
            self.check(fixture, plan)
        fixture, plan = signed_fixture()
        fixture.observation["authorization_consumed_anchor"]["resulting_head_sha256"] = "f" * 64
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "external_closure_observation_mismatch"):
            self.check(fixture, plan)

    def test_legacy_postrun_verifier_still_rejects_v2_and_requires_real_manifest_time(self):
        fixture, plan = signed_fixture()
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "external_closure_document_schema_invalid"):
            verify_pre_receipt_documents(documents(fixture), schemas=_schemas(), external_observation_bundle=raw(fixture.observation))
        legacy = SyntheticPreReceipt()
        legacy.observation["manifest_observation"]["observed_at_utc"] = "2026-09-04T00:00:15Z"
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "external_closure_timeline_invalid"):
            verify_pre_receipt_documents(documents(legacy), schemas=_schemas(), external_observation_bundle=raw(legacy.observation))


if __name__ == "__main__":
    unittest.main()
