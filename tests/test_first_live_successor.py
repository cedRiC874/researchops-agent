"""New public first-live control plane: real local gates, synthetic key and MockTransport only."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import io
import json
import os
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import AsyncMock,patch

import openai
from agents import OpenAIResponsesModel

from researchops import deepseek_completion_first_live_successor as successor
from researchops import deepseek_completion_first_live_validation as engine
from researchops.first_live_execution_profiles import execution_profile
from researchops_external_closure.execution_current_v2 import build_current_profile_documents,verify_current_profile
from researchops_external_closure.execution_components_v2 import PROFILE_PATHS
from researchops_external_closure.admission_bundle_bytes import admission_bundle_commitment
from researchops_external_closure.admission_contract import load_admission_link_contract
from researchops_external_closure.admission_first_live import verify_first_live_success_bundle
from researchops_external_closure.admission_source import compare_source_bytes, select_source_paths
from researchops_external_closure.primitives import canonical_json_bytes
from researchops_completion_telemetry.surface_mapping import load_and_select_surface_mapping
from tests.execution_v2_fixture import ROOT,first_live_source_files
from tests.test_deepseek_completion_first_live_validation import _MockResponsesTransport,_response_body
from tests.test_external_closure_execution_binding import write_file_tree
from tests.test_external_closure_git_objects import Repository


FAKE_KEY="SYNTHETIC-FIRST-LIVE-SUCCESSOR-KEY"


class FakeEnvironment(dict):
    def __init__(self,values):
        super().__init__(values)
        self.key_reads=0

    def get(self,name,default=None):
        if name=="DEEPSEEK_API_KEY":
            self.key_reads+=1
        return super().get(name,default)


class FirstLiveSuccessorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.repo=Repository(Path(cls.temporary.name)/"repo")
        files,_paths=first_live_source_files()
        for name,payload in files.items():
            target=cls.repo.root/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(payload)
        docs=build_current_profile_documents(cls.repo.root,profile="first_live")
        manifest,plan=PROFILE_PATHS["first_live"]
        files.update({manifest:docs.manifest,plan:docs.plan,".gitignore":(ROOT/".gitignore").read_bytes()})
        for name in (manifest,plan,".gitignore"):
            target=cls.repo.root/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(files[name])
        cls.tree=write_file_tree(cls.repo,files)
        cls.commit=cls.repo.commit(cls.tree)
        cls.repo.git("update-ref","refs/heads/main",cls.commit)
        cls.repo.git("symbolic-ref","HEAD","refs/heads/main")
        cls.repo.git("update-ref","refs/remotes/origin/main",cls.commit)
        cls.repo.git("read-tree",cls.commit)
        cls.source_commitment=json.loads(docs.plan)["plan_commitment_sha256"]
        # Read only noncredential OS paths before replacing the environment
        # object. Existing Provider values are never copied or inspected.
        cls.os_paths={name:value for name in ("PATH","SystemRoot","WINDIR","COMSPEC","TEMP","TMP","TMPDIR")
                      if (value:=os.environ.get(name)) is not None}

    def arguments(self,identifier):
        now=datetime.now(timezone.utc).replace(microsecond=0)
        values={"project_root":self.repo.root,"authorization_id":identifier,
                "authorization_expires_at_utc":(now+timedelta(hours=1)).isoformat().replace("+00:00","Z"),
                "expected_contract_commitment_sha256":engine.CONTRACT_COMMITMENT_SHA256,
                "expected_source_integrity_commitment_sha256":self.source_commitment,"expected_execution_commit":self.commit,
                "pricing_snapshot_date":now.date().isoformat(),"pricing_source_url":engine._PRICING_SOURCE_URL,
                "input_price_per_million_cny":"3.000000","output_price_per_million_cny":"9.000000"}
        bound=successor.calculate_successor_authorization_binding(**values)
        values.update(expected_authorization_binding_sha256=bound["authorization_binding_sha256"],
                      confirm_online=True,accept_locked_caps=True,attest_pricing_current=True)
        return values,bound

    def invoke(self,arguments,transport):
        environment=FakeEnvironment(self.os_paths|{"DEEPSEEK_API_KEY":FAKE_KEY,"OPENAI_AGENTS_DISABLE_TRACING":"1"})
        with patch.object(engine.os,"environ",environment),patch("researchops.model_providers._load_responses_transport",
             return_value=(openai.AsyncOpenAI,OpenAIResponsesModel,transport.client_factory)):
            result=asyncio.run(successor.run_successor_validation(**arguments))
        return result,environment

    def test_current_source_profile_and_public_contract_validate_without_authority(self):
        checked=verify_current_profile(self.repo.root,profile="first_live")
        self.assertEqual(checked.source_integrity_commitment_sha256,self.source_commitment)
        status=successor.validate_successor_contract(self.repo.root)
        self.assertTrue(status["source_profile_valid"])
        self.assertFalse(status["online_execution_authorized"])
        self.assertEqual(status["source_integrity_plan_id"],"phase6-deepseek-depth60-v7")

    def test_public_mock_run_consumes_once_and_persists_v3_without_key(self):
        arguments,bound=self.arguments("successor-mock-once")
        transport=_MockResponsesTransport([_response_body("completed",256),_response_body("incomplete",16)])
        result,environment=self.invoke(arguments,transport)
        self.assertEqual(result["status"],"success",result)
        self.assertEqual(result["implementation_commitment_sha256"],execution_profile(3).implementation_commitment_sha256)
        self.assertEqual(result["source_integrity_plan_id"],"phase6-deepseek-depth60-v7")
        self.assertEqual((result["network_calls"],result["model_requests"]),(2,2))
        self.assertEqual(environment.key_reads,1)
        self.assertEqual([item["max_output_tokens"] for item in transport.requests],[256,16])
        directory=self.repo.root/"artifacts/provider_completion_first_live_validation/deepseek-responses-v1"/bound["authorization_id_sha256"]
        self.assertEqual({path.name for path in directory.iterdir()},set(engine._ARTIFACT_FILENAMES))
        for path in directory.iterdir():
            self.assertNotIn(FAKE_KEY.encode(),path.read_bytes())
        # Feed the real writer's untouched seven files into the independent
        # success-only verifier. This is MockTransport conformance, not live
        # evidence, a review signature, or permission to register a Provider.
        contract=load_admission_link_contract(self.repo.root)
        source_files,tree_paths=first_live_source_files()
        selected={name:source_files[name] for name in select_source_paths(contract,tree_paths)}
        identity=compare_source_bytes(contract,first_live_files=selected,first_live_tree_paths=tree_paths,
                                      campaign_files=selected,campaign_tree_paths=tree_paths)
        mapping=load_and_select_surface_mapping(self.repo.root,"deepseek","responses",
                                                "openai_compatible_responses",purpose="offline_validation")
        subject={"provider_id":"deepseek","model_id":"deepseek-v4-flash","api_origin":"https://api.deepseek.com",
                 "api_surface":"responses","transport_id":"openai_compatible_responses","adapter_version":mapping.adapter_version,
                 "first_live_execution_commit":self.commit,"first_live_execution_tree":self.tree,
                 "first_live_source_integrity_commitment_sha256":self.source_commitment,
                 "first_live_implementation_commitment_sha256":execution_profile(3).implementation_commitment_sha256,
                 "source_byte_inventory_sha256":identity.source_byte_inventory_sha256,
                 "dependency_lock_sha256":identity.dependency_lock_sha256,"pyproject_sha256":identity.pyproject_sha256,
                 "selected_mapping_sha256":mapping.mapping_sha256}
        before={name:(directory/name).read_bytes() for name in contract.document()["evidence_file_order"]}
        terminal=json.loads(before["terminal.json"])
        bundle={"schema_version":"provider-completion-first-live-evidence-bundle/1.0",
                "document_type":"completion_telemetry_first_live_evidence_bundle","subject":subject,
                "authorization_id_sha256":bound["authorization_id_sha256"],
                "external_authorization_binding_sha256":bound["authorization_binding_sha256"],
                "first_live_completed_at_utc":terminal["completed_at_utc"],
                "artifact_files":[{"name":name,"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}
                                  for name,raw in before.items()],
                "first_live_design_commitment_sha256":engine.CONTRACT_COMMITMENT_SHA256,
                "first_live_implementation_contract_sha256":execution_profile(3).implementation_sha256,
                "complete_artifact_set_required":True,"raw_body_included":False,"online_authority_granted":False}
        bundle["commitment_sha256"]=admission_bundle_commitment(contract,bundle)
        with patch("socket.socket",side_effect=AssertionError("verification network forbidden")):
            evidence=verify_first_live_success_bundle(self.repo.root,artifact_directory=directory,
                bundle_bytes=canonical_json_bytes(bundle)+b"\n",expected_evidence_commitment_sha256=bundle["commitment_sha256"],
                expected_authorization_binding_sha256=bound["authorization_binding_sha256"],expected_subject=subject)
        self.assertTrue(evidence.artifact_structure_and_semantics_verified)
        self.assertEqual(evidence.recorded_response_count,2)
        self.assertEqual(evidence.source_plan_id,"phase6-deepseek-depth60-v7")
        self.assertFalse(evidence.execution_identity_verified)
        self.assertFalse(evidence.registry_admission_verified)
        self.assertFalse(evidence.runtime_authority_granted)
        self.assertFalse(evidence.closure_claim_allowed)
        self.assertEqual(before,{name:(directory/name).read_bytes() for name in before})
        again,second_environment=self.invoke(arguments,transport)
        self.assertEqual(again["status"],"not_run")
        self.assertEqual(again["error_code"],"deepseek_first_live_authorization_already_consumed")
        self.assertEqual(second_environment.key_reads,0)
        self.assertEqual(len(transport.requests),2)

    def test_v2_binding_is_not_accepted_by_v3(self):
        arguments,bound=self.arguments("successor-wrong-version")
        old=engine._authorization_binding(authorization_id_sha256=bound["authorization_id_sha256"],expires_at_utc=bound["authorization_expires_at_utc"],
            execution_commit=self.commit,source_integrity_commitment=self.source_commitment,pricing_snapshot_date=arguments["pricing_snapshot_date"],
            pricing_source_url=arguments["pricing_source_url"],input_price=engine.Decimal("3"),output_price=engine.Decimal("9"))
        self.assertNotEqual(old,bound["authorization_binding_sha256"])
        arguments["expected_authorization_binding_sha256"]=old
        transport=_MockResponsesTransport([])
        result,environment=self.invoke(arguments,transport)
        self.assertEqual(result["error_code"],"deepseek_first_live_authorization_binding_mismatch")
        self.assertEqual(environment.key_reads,0)
        self.assertEqual(transport.requests,[])

    def test_wrong_first_shape_stops_before_second_request(self):
        arguments,_=self.arguments("successor-bad-shape")
        transport=_MockResponsesTransport([_response_body("incomplete",256)])
        result,_=self.invoke(arguments,transport)
        self.assertEqual(result["status"],"failed")
        self.assertEqual(result["error_code"],"deepseek_first_live_scenario_shape_mismatch")
        self.assertEqual(result["network_calls"],1)
        self.assertEqual(len(transport.requests),1)

    def test_unconfirmed_public_run_does_not_read_key_or_create_output(self):
        arguments,_=self.arguments("successor-unconfirmed")
        arguments["confirm_online"]=False
        result,environment=self.invoke(arguments,_MockResponsesTransport([]))
        self.assertEqual(result["status"],"not_run")
        self.assertEqual(environment.key_reads,0)
        self.assertEqual(result["network_calls"],0)

    def test_git_environment_never_enumerates_or_reads_provider_credentials(self):
        class OnlyPaths:
            def __iter__(self):
                raise AssertionError("environment must not be enumerated")
            def get(self,name,default=None):
                if name not in ("PATH","SystemRoot","WINDIR","COMSPEC","TEMP","TMP","TMPDIR"):
                    raise AssertionError("non-path environment read")
                return self_allowed.get(name,default)
        self_allowed=self.os_paths
        with patch.object(engine.os,"environ",OnlyPaths()):
            child=engine._git_offline_environment()
        self.assertNotIn("DEEPSEEK_API_KEY",child)
        self.assertEqual(child["GIT_NO_LAZY_FETCH"],"1")
        self.assertEqual(child["GIT_CONFIG_VALUE_0"],"never")

    def test_isolation_checks_names_without_reading_unrelated_secret_values(self):
        class NamesOnly:
            def __iter__(self):
                return iter(("OPENAI_ADMIN_KEY",))
            def get(self,*args):
                raise AssertionError("secret value must not be read")
        self.assertFalse(engine._environment_isolated(NamesOnly()))
        self.assertFalse(engine._environment_isolated({"OPENAI_ADMIN_KEY":""}))
        self.assertTrue(engine._environment_isolated({"DEEPSEEK_API_KEY":"synthetic"}))

    def test_public_api_has_no_injection_seams_and_unknown_versions_reject(self):
        parameters=inspect.signature(successor.run_successor_validation).parameters
        self.assertFalse(any(name.startswith("_") for name in parameters))
        self.assertFalse({"api_key","key_loader","transport","clock","git_state_loader","execution_version"}.intersection(parameters))
        for value in (None,True,1,4,"3"):
            with self.subTest(value=value),self.assertRaises(ValueError):
                execution_profile(value)

    def test_cli_does_not_turn_uncertain_run_exception_into_zero_calls(self):
        output=io.StringIO()
        with patch.object(successor,"run_successor_validation",new=AsyncMock(side_effect=RuntimeError("not printed"))),contextlib.redirect_stdout(output):
            code=successor.main(["run"])
        result=json.loads(output.getvalue())
        self.assertEqual(code,4)
        self.assertEqual(result["status"],"failed")
        self.assertTrue(result["outcome_unknown"])
        self.assertIsNone(result["network_calls"])
        self.assertIsNone(result["provider_key_loaded"])
        self.assertNotIn("not printed",output.getvalue())


if __name__=="__main__":
    unittest.main()
