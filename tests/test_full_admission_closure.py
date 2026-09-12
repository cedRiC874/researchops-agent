"""Full A/B synthetic conformance, with real verifier calls and no verdict stubs."""

from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from researchops_completion_telemetry.surface_mapping import VerifiedRuntimeCompletionBinding
from researchops_external_closure.evaluator import evaluate_closure_bundle
from researchops_external_closure.final import verify_closed_campaign
from researchops_external_closure.types import FinalDocumentBytes,ReceiptProjectionReady
from researchops_external_closure.primitives import canonical_json_bytes
from tests import test_external_closure_evaluator as helpers
from tests.full_closure_fixture import full_campaign_fixture


class FullAdmissionClosureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import test_admission_evidence as admission_setup_module
        setup=admission_setup_module.AdmissionEvidenceChainTests
        with patch.object(VerifiedRuntimeCompletionBinding,"_create",side_effect=AssertionError("runtime factory forbidden in fixture")):
            setup.setUpClass()
            cls.addClassCleanup(setup.doClassCleanups)
            cls.root=setup.repo.root
            cls.fixture,cls.semantic,cls.postrun,cls.admission=full_campaign_fixture(setup)
        cls.directory=cls.root/"synthetic-campaign-artifacts"
        cls.directory.mkdir()
        manifest=helpers._write_complete_bundle(cls.directory,cls.fixture,cls.semantic)
        cls.fixture.observation["manifest_observation"]["manifest_sha256"]=hashlib.sha256(manifest).hexdigest()
        cls.documents,cls.observation=cls.fixture.inputs()

    def layer_a(self,admission=None):
        return evaluate_closure_bundle(self.root,self.documents,external_observation_bundle=self.observation,
            artifact_directory=self.directory,postrun_attested_facts=canonical_json_bytes(self.postrun),
            admission_evidence=self.admission if admission is None else admission)

    def test_actual_public_a_and_b_close_only_the_complete_synthetic_graph(self):
        with patch.object(VerifiedRuntimeCompletionBinding,"_create",side_effect=AssertionError("runtime factory forbidden")):
            ready=self.layer_a()
            self.assertIsInstance(ready,ReceiptProjectionReady,repr(ready))
            self.assertTrue(ready.evidence_valid,ready.evidence_error_code)
            self.assertTrue(ready.pre_anchor_closure_eligible,ready.closure_reasons)
            entry=helpers._final_entry(self.fixture,receipt_sha256=ready.receipt_document_sha256,occurred_at_utc="2026-09-05T02:05:00Z")
            final_observation=helpers._final_observation_bytes(self.observation,receipt_sha256=ready.receipt_document_sha256,
                receipt_observed_at_utc="2026-09-05T02:04:30Z",entry=entry,entry_observed_at_utc="2026-09-05T02:05:30Z")
            closed=verify_closed_campaign(self.root,FinalDocumentBytes(self.documents,helpers._signed_receipt_bytes(self.fixture,ready),canonical_json_bytes(entry)),
                external_observation_bundle=final_observation,artifact_directory=self.directory,
                postrun_attested_facts=canonical_json_bytes(self.postrun),admission_evidence=self.admission)
            changed=verify_closed_campaign(self.root,FinalDocumentBytes(self.documents,helpers._signed_receipt_bytes(self.fixture,ready),canonical_json_bytes(entry)),
                external_observation_bundle=final_observation,artifact_directory=self.directory,
                postrun_attested_facts=canonical_json_bytes(self.postrun),
                admission_evidence=replace(self.admission,expected_review_document_sha256="f"*64))
        self.assertEqual(closed.status,"closed_evidence_verified")
        self.assertTrue(closed.evidence_valid)
        self.assertTrue(closed.closure_claim_allowed)
        self.assertEqual(changed.status,"unclosed_invalid")
        self.assertFalse(changed.closure_claim_allowed)
        self.assertEqual(changed.error_code,"closure_receipt_projection_mismatch")

    def test_correct_campaign_without_valid_admission_evidence_stays_unclosed(self):
        wrong=replace(self.admission,expected_review_document_sha256="f"*64)
        result=self.layer_a(wrong)
        self.assertIsInstance(result,ReceiptProjectionReady,repr(result))
        self.assertFalse(result.evidence_valid)
        self.assertFalse(result.pre_anchor_closure_eligible)
        self.assertEqual(result.evidence_error_code,"closure_denominator_invalid")


if __name__=="__main__":
    unittest.main()
