"""Public composed-verifier conformance: real local Git, real test signatures,
hand-built synthetic artifacts, and no runtime/Provider factory bypass.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from researchops_external_closure import execution_components_v2 as v2
from researchops_external_closure.admission_contract import load_admission_link_contract
from researchops_external_closure.admission_evidence import AdmissionEvidenceInputs,verify_admission_evidence_links
from researchops_external_closure.admission_review import review_document_sha256,review_signature_message
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.primitives import canonical_json_bytes
from tests.execution_v2_fixture import ROOT,first_live_source_files,add_campaign_data,registry_documents,h
from tests.first_live_success_fixture import raw
from tests.test_external_closure_documents import SyntheticPreReceipt
from tests.test_external_closure_execution_binding import write_file_tree


class AdmissionEvidenceChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Local import avoids registering the imported TestCase as another
        # suite. Reuse only its synthetic Git/artifact setup, not its verdicts.
        from tests import test_admission_identity as identity_fixture_module
        setup=identity_fixture_module.FirstLiveHistoricalIdentityTests
        setup.setUpClass()
        cls.addClassCleanup(setup.doClassCleanups)
        cls.repo=setup.repo;cls.fixture=setup.fixture
        cls.contract=load_admission_link_contract(ROOT)
        cls.signers=SyntheticPreReceipt()
        cls.trust=cls.signers.documents["trust_manifest"]
        cls.review={"schema_version":"provider-completion-first-live-review/1.0","document_type":"completion_telemetry_first_live_review",
            "review_id":"PCEREVIEW-"+"A"*32,"reviewed_at_utc":"2026-09-05T01:00:30Z",
            "first_live_completed_at_utc":cls.fixture.bundle["first_live_completed_at_utc"],"evidence_observed_at_utc":"2026-09-05T01:00:20Z",
            "evidence_commit":setup.reviewed_commit,"evidence_tree":setup.reviewed_tree,
            "first_live_evidence_commitment_sha256":cls.fixture.bundle["commitment_sha256"],"subject":cls.fixture.subject,
            "decision":"approved","reason_code":"passed","review_scope":"two_response_adapter_completion_shapes_only",
            "reviewer_independence_claimed":False,"online_execution_authorized":False,"provider_registration_authorized":False,
            "document_sha256":"0"*64,"signatures":[]}
        cls.sign_review(cls.review)
        cls.observation={"schema_version":"provider-completion-first-live-review-observation/1.0",
            "document_type":"completion_telemetry_first_live_review_observation","review_document_sha256":cls.review["document_sha256"],
            "review_observed_at_utc":"2026-09-05T01:00:40Z","review_observation_source_commitment_sha256":h("review observer"),
            "trust_manifest_sha256":cls.trust["document_sha256"],"trust_observed_at_utc":"2026-09-04T00:00:02Z",
            "trust_observation_source_commitment_sha256":h("trust observer"),"candidate_freeze_receipt_sha256":h("candidate freeze"),
            "candidate_frozen_at_utc":"2026-09-05T01:01:00Z","expectations_supplied_out_of_band":True}
        _,registry_bundle,registry_raw,bundle_raw=registry_documents(cls.fixture.subject,cls.fixture.bundle["commitment_sha256"],
            cls.review["document_sha256"],setup.reviewed_commit,setup.reviewed_tree)
        files,paths=first_live_source_files()
        first=v2.build_profile_documents(files,available_paths=paths,profile="first_live")
        cls.assert_first_matches=first
        files,paths=add_campaign_data(files,paths,first,registry_raw,bundle_raw)
        campaign=v2.build_profile_documents(files,available_paths=paths,profile="campaign")
        m,p=v2.PROFILE_PATHS["campaign"]
        tree=write_file_tree(cls.repo,files|{m:campaign.manifest,p:campaign.plan})
        commit=cls.repo.commit(tree,setup.reviewed_commit)
        manifest=json.loads(campaign.manifest);plan=json.loads(campaign.plan)
        cls.binding={"repository":"cedRiC874/researchops-agent","candidate_freeze_receipt_sha256":h("candidate freeze"),
            "candidate_freeze_ledger_entry_sha256":h("candidate witness"),"execution_commit":commit,"execution_tree":tree,
            "source_integrity_commitment_sha256":plan["plan_commitment_sha256"],"implementation_commitment_sha256":manifest["commitment_sha256"],
            "first_live_evidence_commitment_sha256":cls.fixture.bundle["commitment_sha256"],"first_live_validation_status":"reviewed_success",
            "runtime_registry_commitment_sha256":registry_bundle["commitment_sha256"],"runtime_registry_binding_allowed":True,
            "closure_evidence_contract_commitment_sha256":"a96b39a1f4bafa3bcf512fd88a5e7b0ca3987ca2bc9804642a1d1259cbf8cad0",
            **{name:manifest["component_hashes"][name] for name in ("runner_config_commitment_sha256","dependency_lock_sha256","sanitizer_sha256","closure_verifier_sha256","telemetry_schema_sha256","mapping_sha256")},
            **{name:cls.fixture.subject[name] for name in ("provider_id","model_id","api_origin","api_surface","transport_id","adapter_version")}}
        cls.inputs=AdmissionEvidenceInputs(cls.fixture.directory,raw(cls.fixture.bundle),raw(cls.review),raw(cls.observation),raw(cls.trust),
            cls.review["document_sha256"],cls.trust["document_sha256"],cls.fixture.auth,h("candidate freeze"),"2026-09-05T01:01:00Z")

    @classmethod
    def sign_review(cls,review):
        digest=review_document_sha256(cls.contract,review)
        key_id=cls.signers.key_ids["freeze_authority"]
        signature=cls.signers.keys["freeze_authority"].sign(review_signature_message(cls.contract,key_id,digest))
        review["document_sha256"]=digest
        review["signatures"]=[{"role":"freeze_authority","key_id":key_id,"algorithm":"ed25519","signed_sha256":digest,
                               "signature_b64":base64.b64encode(signature).decode()}]

    def test_public_chain_verifies_links_but_not_outer_authorization_or_live_execution(self):
        from researchops_completion_telemetry.surface_mapping import VerifiedRuntimeCompletionBinding
        with patch.object(VerifiedRuntimeCompletionBinding,"_create",side_effect=AssertionError("runtime factory forbidden")):
            result=verify_admission_evidence_links(self.repo.root,execution_binding=self.binding,evidence=self.inputs)
        self.assertTrue(result.component_bindings_verified)
        self.assertTrue(result.first_live_artifact_semantics_verified)
        self.assertTrue(result.source_equivalence_verified)
        self.assertTrue(result.review_signature_and_observation_verified)
        self.assertTrue(result.registry_links_verified)
        self.assertFalse(result.outer_preregistration_and_campaign_authorization_verified)
        self.assertFalse(result.actual_network_execution_independently_proved)
        self.assertFalse(result.runtime_authority_granted)
        self.assertFalse(result.closure_claim_allowed)

    def test_wrong_external_review_digest_rejects_before_git(self):
        wrong=replace(self.inputs,expected_review_document_sha256=h("wrong"))
        with patch("researchops_external_closure.admission_evidence.verify_historical_profile",side_effect=AssertionError("Git must not be read")) as reader:
            with self.assertRaises(ExternalClosurePrimitiveError):
                verify_admission_evidence_links(self.repo.root,execution_binding=self.binding,evidence=wrong)
            reader.assert_not_called()

    def test_rejected_real_signature_cannot_be_overridden_by_registry_declaration(self):
        review=copy.deepcopy(self.review)
        review.update(decision="rejected",reason_code="evidence_invalid")
        self.sign_review(review)
        observation=dict(self.observation);observation["review_document_sha256"]=review["document_sha256"]
        inputs=replace(self.inputs,first_live_review=raw(review),review_observation=raw(observation),expected_review_document_sha256=review["document_sha256"])
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"review_not_approved"):
            verify_admission_evidence_links(self.repo.root,execution_binding=self.binding,evidence=inputs)

    def test_candidate_freeze_projection_and_unknown_fields_reject(self):
        wrong=dict(self.binding);wrong["candidate_freeze_receipt_sha256"]=h("wrong")
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"candidate_freeze_mismatch"):
            verify_admission_evidence_links(self.repo.root,execution_binding=wrong,evidence=self.inputs)
        wrong=dict(self.binding);wrong["injected_success_verdict"]=True
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"binding_invalid"):
            verify_admission_evidence_links(self.repo.root,execution_binding=wrong,evidence=self.inputs)


if __name__=="__main__":
    unittest.main()
