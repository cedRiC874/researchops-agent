"""Real Git + synthetic artifact conformance, not evidence of a live model run."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_external_closure import execution_components_v2 as v2
from researchops_external_closure.admission_bundle_bytes import admission_bundle_commitment
from researchops_external_closure.admission_identity import CONTROL_COMMITMENT,CONTROL_SHA256,verify_first_live_historical_identity
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.first_live_success_semantics import recompute_authorization_binding
from researchops_external_closure.primitives import canonical_json_bytes
from tests.execution_v2_fixture import first_live_source_files
from tests.first_live_success_fixture import SyntheticFirstLiveArtifacts,raw
from tests.test_external_closure_execution_binding import write_file_tree
from tests.test_external_closure_git_objects import Repository


class FirstLiveHistoricalIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        root=Path(cls.temporary.name)
        cls.repo=Repository(root/"repo")
        files,paths=first_live_source_files()
        docs=v2.build_profile_documents(files,available_paths=paths,profile="first_live")
        manifest=json.loads(docs.manifest);plan=json.loads(docs.plan)
        m,p=v2.PROFILE_PATHS["first_live"]
        files=files|{m:docs.manifest,p:docs.plan}
        tree=write_file_tree(cls.repo,files);commit=cls.repo.commit(tree)
        # Only the current verifier's public schema/fixture reads use these
        # filesystem copies. Historical source and reviewed bundle use Git.
        for name,payload in files.items():
            path=cls.repo.root/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(payload)
        fixture=SyntheticFirstLiveArtifacts(root/"artifacts")
        subject=fixture.subject
        subject.update(first_live_execution_commit=commit,first_live_execution_tree=tree,
                       first_live_source_integrity_commitment_sha256=plan["plan_commitment_sha256"],
                       first_live_implementation_commitment_sha256=CONTROL_COMMITMENT)
        for name,field in (("source_byte_inventory_sha256","source_byte_inventory_sha256"),("dependency_lock_sha256","dependency_lock_sha256"),
                           ("pyproject_sha256","pyproject_sha256"),("selected_mapping_sha256","mapping_sha256")):
            subject[name]=manifest["component_hashes"][field]
        design="ddff10f30031faf77d6417dd695dd61dae4c6a45334efae7388ab4f2adc4a5bc"
        for name in ("consumption.json","terminal.json","completion_telemetry.json","manifest.json"):
            fixture.documents[name]["contract_commitment_sha256"]=design
            fixture.documents[name]["implementation_commitment_sha256"]=CONTROL_COMMITMENT
        for name in ("consumption.json","terminal.json"):
            fixture.documents[name].update(execution_commit=commit,source_integrity_commitment_sha256=plan["plan_commitment_sha256"],source_integrity_plan_id="phase6-deepseek-depth60-v7")
        fixture.auth=recompute_authorization_binding(fixture.documents["consumption.json"])
        for name in ("consumption.json","terminal.json"):
            fixture.documents[name]["authorization_binding_sha256"]=fixture.auth
        fixture.bundle.update(external_authorization_binding_sha256=fixture.auth,first_live_design_commitment_sha256=design,
                              first_live_implementation_contract_sha256=CONTROL_SHA256)
        request={"validation_id":fixture.profile["constants"]["VALIDATION_ID"],"contract_commitment_sha256":design,
                 "implementation_commitment_sha256":CONTROL_COMMITMENT,"authorization_id_sha256":fixture.bundle["authorization_id_sha256"],
                 "scenario_count":2,"provider":"deepseek","model":"deepseek-v4-flash"}
        fixture.request_hash=hashlib.sha256(canonical_json_bytes(request)).hexdigest()
        payload=json.loads(fixture.events[0]["safe_payload_json"]);payload["request_sha256"]=fixture.request_hash
        fixture.events[0]["safe_payload_json"]=canonical_json_bytes(payload).decode()
        fixture.rechain();fixture.refresh()
        cls.fixture=fixture
        cls.source_manifest_commitment=manifest["commitment_sha256"]
        evidence_path="docs/evidence/provider-completion-first-live-v1/"+fixture.bundle["authorization_id_sha256"]+"/bundle.json"
        cls.reviewed_tree=write_file_tree(cls.repo,files|{evidence_path:raw(fixture.bundle)})
        cls.reviewed_commit=cls.repo.commit(cls.reviewed_tree,commit)
        # An unrelated current-file value cannot repair or replace the Git blob.
        target=cls.repo.root/evidence_path
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(b"wrong current worktree bundle")

    def verify(self,**changes):
        arguments=self.fixture.arguments()
        arguments.update(reviewed_evidence_commit=self.reviewed_commit,reviewed_evidence_tree=self.reviewed_tree)
        return verify_first_live_historical_identity(self.repo.root,**(arguments|changes))

    def test_real_component_and_reviewed_blob_bindings_do_not_prove_behavior_or_admission(self):
        from researchops_completion_telemetry.surface_mapping import VerifiedRuntimeCompletionBinding
        with patch.object(VerifiedRuntimeCompletionBinding,"_create",side_effect=AssertionError("no authority")):
            result=self.verify()
        self.assertTrue(result.component_identity_verified)
        self.assertTrue(result.reviewed_bundle_git_verified)
        self.assertEqual(result.control_contract_commitment_sha256,CONTROL_COMMITMENT)
        self.assertEqual(result.source_manifest_commitment_sha256,self.source_manifest_commitment)
        self.assertNotEqual(result.control_contract_commitment_sha256,result.source_manifest_commitment_sha256)
        self.assertFalse(result.implementation_behavior_proved_by_source_hash)
        self.assertFalse(result.review_signature_verified)
        self.assertFalse(result.registry_admission_verified)
        self.assertFalse(result.runtime_authority_granted)

    def test_control_commitment_cannot_be_replaced_by_source_manifest_commitment(self):
        bundle=copy.deepcopy(self.fixture.bundle)
        bundle["subject"]["first_live_implementation_commitment_sha256"]=self.source_manifest_commitment
        bundle["commitment_sha256"]=admission_bundle_commitment(self.fixture.contract,bundle)
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"control_contract_mismatch"):
            self.verify(bundle_bytes=raw(bundle),expected_subject=bundle["subject"],expected_evidence_commitment_sha256=bundle["commitment_sha256"])

    def test_reviewed_evidence_blob_is_exact_bytes_not_current_worktree(self):
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"reviewed_bundle_mismatch"):
            self.verify(bundle_bytes=raw(self.fixture.bundle)+b"\n")


if __name__=="__main__":
    unittest.main()
