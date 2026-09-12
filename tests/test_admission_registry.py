from __future__ import annotations

import copy
import json
import unittest

from researchops_external_closure.admission_contract import load_admission_link_contract
from researchops_external_closure.admission_registry import validate_registry_carrier
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from tests.execution_v2_fixture import ROOT, h, registry_documents


class RegistryCarrierTests(unittest.TestCase):
    def setUp(self):
        self.contract=load_admission_link_contract(ROOT)
        self.subject={"provider_id":"deepseek","model_id":"deepseek-v4-flash","api_origin":"https://api.deepseek.com",
                      "api_surface":"responses","transport_id":"openai_compatible_responses","adapter_version":"deepseek-responses-adapter/1.0",
                      "first_live_execution_commit":"1"*40,"first_live_execution_tree":"2"*40,
                      **{name:h(name) for name in ("first_live_source_integrity_commitment_sha256","first_live_implementation_commitment_sha256",
                         "source_byte_inventory_sha256","dependency_lock_sha256","pyproject_sha256","selected_mapping_sha256")}}
        self.registry,self.bundle,self.registry_raw,self.bundle_raw=registry_documents(self.subject,h("evidence"),h("review"))

    def verify(self,**overrides):
        arguments={"registry_bytes":self.registry_raw,"admission_bundle_bytes":self.bundle_raw,
                   "expected_registry_commitment_sha256":self.bundle["commitment_sha256"],"expected_subject":self.subject,
                   "expected_first_live_evidence_commitment_sha256":h("evidence"),"expected_review_document_sha256":h("review"),
                   "expected_reviewed_evidence_commit":"3"*40,"expected_reviewed_evidence_tree":"4"*40}
        return validate_registry_carrier(ROOT,self.contract,**(arguments|overrides))

    def test_valid_carrier_is_not_runtime_authority_or_provider_registration(self):
        registry,bundle=self.verify()
        self.assertFalse(registry["runtime_authority_granted_by_document"])
        self.assertFalse(registry["provider_registration_authorized"])
        self.assertFalse(bundle["online_execution_authorized"])
        self.assertEqual([row["eligibility"] for row in registry["entries"]],
                         ["requires_verified_first_live_review","not_eligible","not_eligible","not_eligible"])

    def test_registry_v2_and_unsupported_provider_promotion_are_rejected(self):
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"document_invalid"):
            self.verify(registry_bytes=(ROOT/"evals/provider_completion_telemetry_v2/provider_completion_surface_registry_v2.json").read_bytes())
        for index in (1,2,3):
            registry=copy.deepcopy(self.registry)
            registry["entries"][index]["eligibility"]="requires_verified_first_live_review"
            with self.subTest(index=index),self.assertRaises(ExternalClosurePrimitiveError):
                self.verify(registry_bytes=json.dumps(registry).encode())

    def test_expected_subject_review_evidence_and_mapping_bindings_are_exact(self):
        for name,value in (("expected_review_document_sha256",h("wrong")),("expected_first_live_evidence_commitment_sha256",h("wrong")),
                           ("expected_reviewed_evidence_commit","5"*40),("expected_registry_commitment_sha256",h("wrong"))):
            with self.subTest(name=name),self.assertRaises(ExternalClosurePrimitiveError):
                self.verify(**{name:value})
        subject=dict(self.subject);subject["selected_mapping_sha256"]=h("new mapping")
        with self.assertRaises(ExternalClosurePrimitiveError):
            self.verify(expected_subject=subject)

    def test_document_cannot_add_authority_or_unknown_fields(self):
        for key,value in (("runtime_authority_granted_by_document",True),("online_execution_authorized",True),("unknown",False)):
            registry=dict(self.registry);registry[key]=value
            with self.subTest(key=key),self.assertRaises(ExternalClosurePrimitiveError):
                self.verify(registry_bytes=json.dumps(registry).encode())


if __name__=="__main__":
    unittest.main()
