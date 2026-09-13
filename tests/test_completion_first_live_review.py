"""Real ephemeral Ed25519 signatures over synthetic timed-review documents."""
from __future__ import annotations

import base64
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import first_live_review as review, first_live_artifacts as archive
from researchops_completion_timing.contract import TimingContractError
from researchops_external_closure import admission_review as legacy_review
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import test_admission_link_prerequisites as legacy_tests


ROOT = Path(__file__).resolve().parents[1]


class TimedFirstLiveReviewTests(unittest.TestCase):
    def setUp(self):
        legacy_tests.ReviewLinkTests.setUpClass()
        self.helper = legacy_tests.ReviewLinkTests()
        self.helper.setUp()
        self.document = copy.deepcopy(self.helper.review)
        self.document.update(schema_version="provider-completion-timed-first-live-review/2.0",
            document_type="completion_telemetry_timed_first_live_review",
            review_scope="two_response_timing_control_archive_and_source_linkage_only",
            first_live_artifact_contract_sha256=archive.PROFILE_SHA256)
        self.observation = copy.deepcopy(self.helper.observation)
        self.sign()

    def sign(self, role="freeze_authority", *, legacy_domain=False):
        digest = review.review_document_sha256(self.document)
        key_id = self.helper.fixture.key_ids[role]
        message = (legacy_review.review_signature_message(self.helper.contract, key_id, digest) if legacy_domain
                   else review.review_signature_message(key_id, digest))
        signature = self.helper.fixture.keys[role].sign(message)
        self.document["document_sha256"] = digest
        self.document["signatures"] = [{"role": "freeze_authority", "key_id": key_id, "algorithm": "ed25519",
            "signed_sha256": digest, "signature_b64": base64.b64encode(signature).decode()}]
        self.expected_review = digest
        self.observation["review_document_sha256"] = digest

    def verify(self, **overrides):
        old = self.helper.review
        values = dict(review_bytes=raw(self.document), trust_bytes=raw(self.helper.trust), observation_bytes=raw(self.observation),
            expected_review_document_sha256=self.expected_review, expected_trust_manifest_sha256=self.helper.trust["document_sha256"],
            expected_candidate_freeze_receipt_sha256=self.observation["candidate_freeze_receipt_sha256"],
            expected_candidate_frozen_at_utc=self.observation["candidate_frozen_at_utc"], expected_subject=self.helper.subject,
            expected_first_live_evidence_commitment_sha256=old["first_live_evidence_commitment_sha256"],
            expected_evidence_commit=old["evidence_commit"], expected_evidence_tree=old["evidence_tree"],
            expected_first_live_completed_at_utc=old["first_live_completed_at_utc"])
        values.update(overrides)
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
             patch("subprocess.Popen", side_effect=AssertionError("no Git/process proof in this component")):
            return review.verify_timed_first_live_review(ROOT, **values)

    def test_valid_signature_and_external_digest_do_not_grant_other_proofs(self):
        result = self.verify()
        self.assertTrue(result["review_signature_verified"])
        self.assertTrue(result["external_review_digest_matched"])
        self.assertEqual(result["decision"], "approved")
        for name in ("artifact_integrity_verified", "historical_source_equality_verified", "registry_admission_verified",
                     "actual_live_execution_verified", "runtime_authority_granted", "first_live_success_verified", "current_closure_claim_allowed"):
            self.assertIs(result[name], False, name)

    def test_legacy_document_and_signature_domain_are_not_interchangeable(self):
        with self.assertRaisesRegex(TimingContractError, "timing_schema_invalid"):
            self.verify(review_bytes=raw(self.helper.review))
        self.sign(legacy_domain=True)
        with self.assertRaisesRegex(ValueError, "signature"):
            self.verify()
        # The original v1 signature still verifies under the untouched v1 path.
        self.assertTrue(self.helper.run_review().review_signature_verified)

    def test_independent_review_trust_and_evidence_anchors_cannot_be_self_replaced(self):
        for name, value in (("expected_review_document_sha256", "f" * 64), ("expected_trust_manifest_sha256", "f" * 64),
                            ("expected_first_live_evidence_commitment_sha256", "f" * 64), ("expected_evidence_commit", "f" * 40)):
            with self.subTest(name=name), self.assertRaises(TimingContractError):
                self.verify(**{name: value})

    def test_resigned_subject_change_is_rejected_against_independent_subject(self):
        self.document["subject"]["dependency_lock_sha256"] = "f" * 64
        self.sign()
        with self.assertRaisesRegex(TimingContractError, "first_live_review_subject_mismatch"):
            self.verify()

    def test_completion_time_and_candidate_freeze_timeline_are_independent(self):
        with self.assertRaisesRegex(TimingContractError, "first_live_review_subject_mismatch"):
            self.verify(expected_first_live_completed_at_utc="2026-09-04T00:00:09Z")
        self.document["reviewed_at_utc"] = "2026-09-04T00:00:13.000000001Z"
        self.sign()
        with self.assertRaisesRegex(TimingContractError, "first_live_review_timeline_invalid"):
            self.verify()

    def test_valid_rejected_review_is_not_rewritten_to_approved(self):
        self.document.update(decision="rejected", reason_code="evidence_invalid")
        self.sign()
        result = self.verify()
        self.assertEqual(result["decision"], "rejected")
        self.assertFalse(result["first_live_success_verified"])

    def test_wrong_role_and_missing_trust_signature_are_rejected(self):
        self.sign("task_custodian")
        with self.assertRaisesRegex(TimingContractError, "first_live_review_signature_binding_invalid"):
            self.verify()
        self.sign()
        self.helper.trust["signatures"].pop()
        with self.assertRaises(ValueError):
            self.verify()

    def test_schema_valid_sensitive_adapter_value_is_rejected_without_echo(self):
        self.document["subject"]["adapter_version"] = "sk-offlinecanary12345"
        self.sign()
        with self.assertRaisesRegex(ValueError, "sensitive_content") as caught:
            self.verify()
        self.assertNotIn("offlinecanary", str(caught.exception))

    def test_fully_resigned_revoked_trust_is_rejected(self):
        self.helper.trust["roles"][0]["revoked"] = True
        self.helper.fixture._finalize_document(self.helper.trust, roles=("freeze_authority", "task_custodian", "ledger_witness"))
        self.observation["trust_manifest_sha256"] = self.helper.trust["document_sha256"]
        with self.assertRaisesRegex(ValueError, "schema_invalid"):
            self.verify()

    def test_signed_decision_reason_contradiction_is_not_eligible(self):
        for decision, reason in (("approved", "evidence_invalid"), ("rejected", "passed")):
            self.document.update(decision=decision, reason_code=reason)
            self.sign()
            # The inherited allOf decision/reason rule rejects at schema decode;
            # do not invent an unreachable second semantic gate/error code.
            with self.subTest(decision=decision), self.assertRaisesRegex(TimingContractError, "timing_schema_invalid"):
                self.verify()


if __name__ == "__main__":
    unittest.main()
