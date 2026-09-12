from __future__ import annotations

import ast
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_external_closure.admission_first_live import verify_first_live_success_bundle
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.primitives import canonical_json_bytes
from tests.first_live_success_fixture import ROOT, SyntheticFirstLiveArtifacts


class FirstLiveSuccessEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture=SyntheticFirstLiveArtifacts(Path(self.temporary.name)/"artifacts")

    def verify(self,**changes):
        return verify_first_live_success_bundle(ROOT,**(self.fixture.arguments()|changes))

    def test_complete_synthetic_artifacts_validate_without_runtime_factory_or_writes(self):
        from researchops_completion_telemetry.surface_mapping import VerifiedRuntimeCompletionBinding
        before={p.name:p.read_bytes() for p in self.fixture.directory.iterdir()}
        with patch.object(VerifiedRuntimeCompletionBinding,"_create",side_effect=AssertionError("runtime authority forbidden")),patch("researchops.audit.AuditLedger",side_effect=AssertionError("writer forbidden")),patch("socket.socket",side_effect=AssertionError("network forbidden")):
            result=self.verify()
        self.assertTrue(result.artifact_structure_and_semantics_verified)
        self.assertEqual((result.recorded_response_count,result.recorded_network_attempt_count),(2,2))
        self.assertEqual((result.recorded_input_tokens,result.recorded_output_tokens),(20,21))
        self.assertEqual(result.recorded_cost_cny,"0.000062")
        self.assertFalse(result.execution_identity_verified)
        self.assertFalse(result.registry_admission_verified)
        self.assertFalse(result.runtime_authority_granted)
        self.assertFalse(result.closure_claim_allowed)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.fixture.directory.iterdir()})

    def test_independent_bundle_and_authorization_expectations_are_required(self):
        for name in ("expected_evidence_commitment_sha256","expected_authorization_binding_sha256"):
            with self.subTest(name=name),self.assertRaises(ExternalClosurePrimitiveError):
                self.verify(**{name:"f"*64})

    def test_wrong_external_binding_rejects_before_artifact_directory_access(self):
        with patch("researchops_external_closure.admission_first_live.read_exact_artifact_directory",
                   side_effect=AssertionError("artifact path must not be read")) as reader:
            with self.assertRaisesRegex(ExternalClosurePrimitiveError,"external_binding_mismatch"):
                self.verify(expected_authorization_binding_sha256="f"*64)
            reader.assert_not_called()

    def test_file_tamper_and_missing_or_extra_file_are_rejected(self):
        path=self.fixture.directory/"terminal.json"
        raw=path.read_bytes()
        path.write_bytes(raw+b"\n")
        with self.assertRaises(ExternalClosurePrimitiveError):
            self.verify()
        path.write_bytes(raw)
        extra=self.fixture.directory/"extra.json"
        extra.write_bytes(b"{}")
        with self.assertRaises(ExternalClosurePrimitiveError):
            self.verify()
        extra.unlink()
        path.unlink()
        with self.assertRaises(ExternalClosurePrimitiveError):
            self.verify()

    def test_rehashed_success_or_usage_lies_cannot_pass(self):
        terminal=self.fixture.documents["terminal.json"]
        for name,value in (("network_calls",1),("raw_response_cleanup_complete",False),("input_tokens",21),("local_observed_usage_cost_cny","0.000001"),("status","failed")):
            previous=terminal[name]
            terminal[name]=value
            self.fixture.refresh()
            with self.subTest(name=name),self.assertRaises(ExternalClosurePrimitiveError):
                self.verify()
            terminal[name]=previous
        self.fixture.refresh()

    def test_rehashed_missing_sdk_usage_coverage_is_not_success(self):
        self.fixture.denominator["cases"][0]["sdk_request_usage_indices_by_response"][0]["sdk_request_usage_indices"]=[]
        self.fixture.refresh()
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"sdk_usage_coverage_invalid"):
            self.verify()

    def test_rehashed_late_terminal_and_wrong_output_cap_are_rejected(self):
        terminal=self.fixture.documents["terminal.json"]
        terminal["completed_at_utc"]="2026-09-05T01:09:00.000Z"
        self.fixture.bundle["first_live_completed_at_utc"]=terminal["completed_at_utc"]
        self.fixture.refresh()
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"timeline_invalid"):
            self.verify()
        terminal["completed_at_utc"]="2026-09-05T01:00:10.000Z"
        self.fixture.bundle["first_live_completed_at_utc"]=terminal["completed_at_utc"]
        self.fixture.records[0]["output_token_cap"]["value"]=128
        self.fixture.refresh()
        with self.assertRaises(ExternalClosurePrimitiveError):
            self.verify()

    def test_rehashed_and_rechained_transport_semantic_tamper_is_rejected(self):
        event=self.fixture.events[2]
        payload=json.loads(event["safe_payload_json"])
        payload["network_call_index"]=99
        event["safe_payload_json"]=canonical_json_bytes(payload).decode()
        self.fixture.rechain()
        self.fixture.refresh()
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"event_projection_invalid"):
            self.verify()

    def test_rehashed_scope_expansion_and_fake_key_are_rejected(self):
        t=self.fixture.documents["terminal.json"]
        t["closure_claim_allowed"]=True
        self.fixture.refresh()
        with self.assertRaises(ExternalClosurePrimitiveError):
            self.verify()
        t["closure_claim_allowed"]=False
        t["source_integrity_plan_id"]="sk-offlinecanary12345"
        self.fixture.refresh()
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"sensitive_content_detected") as caught:
            self.verify()
        self.assertNotIn("offlinecanary",str(caught.exception))

    def test_no_first_live_runner_or_adapter_is_imported_by_new_modules(self):
        for name in ("admission_first_live.py","first_live_success_semantics.py","admission_bundle_bytes.py"):
            syntax=ast.parse((ROOT/"src/researchops_external_closure"/name).read_text(encoding="utf-8"))
            imports={node.module for node in ast.walk(syntax) if isinstance(node,ast.ImportFrom)}
            self.assertNotIn("researchops.deepseek_completion_first_live_validation",imports)
            self.assertNotIn("researchops.model_providers",imports)

    def test_profile_replays_frozen_git_writer_fields_without_running_it(self):
        from researchops_external_closure.git_objects import read_git_object_snapshot
        profile=self.fixture.profile
        source=profile["source"]
        # Historical interface evidence belongs to this commit, not the
        # mutable current source tree. This permits deliberate successors.
        snapshot=read_git_object_snapshot(ROOT,"5f6f9cde2f5e7092ddfbd20bed63c3baad0ea1ab",(source["path"],),
                                           expected_tree_oid="30ecfd86ac00aecd8b67305a7c6ed2af88eeee10")
        raw=snapshot.blobs[0].payload
        self.assertEqual(len(raw),source["bytes"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(),source["sha256"])
        syntax=ast.parse(raw)
        nodes={node.targets[0].id:node.value for node in syntax.body if isinstance(node,ast.Assign)
               and len(node.targets)==1 and isinstance(node.targets[0],ast.Name)}
        for name,fields in profile["field_sets"].items():
            self.assertEqual(sorted(ast.literal_eval(nodes[name].args[0])),fields)
        for name,value in profile["constants"].items():
            self.assertEqual(ast.literal_eval(nodes[name]),value)
        self.assertEqual(ast.literal_eval(nodes["_EXPECTED_COMPLETION_SHAPES"]),profile["expected_completion_shapes"])
        success=[]
        for node in ast.walk(syntax):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=="seal_case":
                keywords={item.arg:item.value for item in node.keywords}
                count=keywords.get("sdk_raw_response_count")
                if isinstance(count,ast.Constant) and count.value==2:
                    success.append(ast.literal_eval(keywords["sdk_request_usage_indices_by_response"]))
        self.assertEqual(success,[{0:(0,),1:(0,)}])


if __name__=="__main__":
    unittest.main()
