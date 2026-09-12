from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from researchops_external_closure.execution_components_v2 import PROFILE_PATHS,verify_profile_documents
from researchops_external_closure.execution_current_v2 import build_current_profile_documents,verify_current_profile,_snapshot
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from tests.execution_v2_fixture import ROOT,first_live_source_files

spec=importlib.util.spec_from_file_location("profile_snapshot_generator",ROOT/"scripts/build_execution_profile_snapshot.py")
generator=importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class CurrentProfileV2Tests(unittest.TestCase):
    def test_archived_v7_checkpoints_replay_only_with_preserved_source_overlays(self):
        original=ROOT/"docs/diagnostics/t6c-review-pre-hardening-v7-20260906"
        budget=ROOT/"docs/diagnostics/t6c-review-before-budget-fix-v7-20260906"
        original_io=(original/"io.py.before.txt").read_bytes()
        original_documents=(budget/"documents.py.before.txt").read_bytes()
        self.assertEqual(hashlib.sha256(original_io).hexdigest(),"0fdf30655957f4dd7a1ecaaeab0265a898b9a11fb8bcf6a480f601ff76e925e4")
        self.assertEqual(hashlib.sha256(original_documents).hexdigest(),"16c70bb34a5b3b365c7f1b595fb21a62cd4ab8e1a34503f2f1551581b1cfea50")
        # Historical replay uses the complete preserved c1 source inventory,
        # not the mutable current tree plus two old source overlays.
        baseline=ROOT/"docs/diagnostics/t6c-source-baseline-c1c25226-v1"
        if os.name=="nt":
            baseline=Path("\\\\?\\"+str(baseline.resolve()))
        manifest=(baseline/"implementation_manifest_first_live_v1.json").read_bytes()
        plan=(baseline/"phase6_deepseek_depth60_plan_v7.json").read_bytes()
        self.assertEqual(hashlib.sha256(manifest).hexdigest(),"13457502cf2fe55ff9bbf071caf86b473812212f42acad4081001b2036b4f6a1")
        self.assertEqual(hashlib.sha256(plan).hexdigest(),"396b2103399af1a68854f1f61abd5547f841a68edde78702fbba4df2910806c1")
        inventory=json.loads(manifest)["file_inventory"]
        self.assertEqual(len(inventory),169)
        files={}
        for row in inventory:
            payload=(baseline/"files"/row["path"]).read_bytes()
            self.assertEqual(len(payload),row["bytes"])
            self.assertEqual(hashlib.sha256(payload).hexdigest(),row["sha256"])
            files[row["path"]]=payload
        paths=tuple(files)
        current_checkpoint=verify_profile_documents(files,available_paths=paths,profile="first_live",manifest=manifest,plan=plan)
        self.assertEqual(current_checkpoint.source_integrity_commitment_sha256,"c1c25226ef9186432d64b297def584da2d875afb0f923014975f9c9b481fb362")
        self.assertFalse(current_checkpoint.online_execution_authorized)
        checkpoints=(
            (original,"c8f03179b950430b17bfabd6be4662189e440e7f1bec4b26b5b43270e436850c",
             "67713d2818e02b414a2d13720255afd3dd0a7850253123b53af5984e869744bc",
             "be89b5d1be6224080e4352d8fbbbcbb78e5373e3e18072c01f5e9d9eac4ca910"),
            (budget,"26b5fe86aa76bed44b7f958d2fb7fb78c5cc16a0b9cbee9db66fe223639d662d",
             "aa7a8cf8939555d633b49fe3ba08118eb97b890c763ae598bfb11caeaa4f8d73",
             "af137873e7425f6c043265f1bfc996eb8160ee4cee0e2a90fbf33ca90a209a07"),
        )
        for archive,manifest_hash,plan_hash,commitment in checkpoints:
            with self.subTest(checkpoint=archive.name):
                manifest=(archive/"implementation_manifest_first_live_v1.json").read_bytes()
                plan=(archive/"phase6_deepseek_depth60_plan_v7.json").read_bytes()
                self.assertEqual(hashlib.sha256(manifest).hexdigest(),manifest_hash)
                self.assertEqual(hashlib.sha256(plan).hexdigest(),plan_hash)
                with self.assertRaisesRegex(ExternalClosurePrimitiveError,"component_drift"):
                    verify_profile_documents(files,available_paths=paths,profile="first_live",manifest=manifest,plan=plan)
                replay=dict(files)
                replay["src/researchops_external_closure/documents.py"]=original_documents
                if archive==original:
                    replay["src/researchops_external_closure/io.py"]=original_io
                checked=verify_profile_documents(replay,available_paths=paths,profile="first_live",manifest=manifest,plan=plan)
                self.assertEqual(checked.source_integrity_commitment_sha256,commitment)
                self.assertFalse(checked.online_execution_authorized)

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        files,_paths=first_live_source_files()
        for name,raw in files.items():
            path=self.root/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(raw)

    def run_generator(self,*arguments):
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream):
            code=generator.main(["--project-root",str(self.root),*arguments])
        return code,json.loads(stream.getvalue())

    def test_preview_exclusive_write_and_verification_preserve_history(self):
        historical=self.root/"evals/phase6_deepseek_depth60_plan_v6.json"
        before=historical.read_bytes()
        code,preview=self.run_generator()
        self.assertEqual(code,0)
        self.assertEqual(preview["created_paths"],[])
        self.assertFalse((self.root/PROFILE_PATHS["first_live"][0]).exists())
        code,written=self.run_generator("--write")
        self.assertEqual(code,0)
        self.assertEqual(written["plan_commitment_sha256"],preview["plan_commitment_sha256"])
        self.assertEqual(written["created_paths"],list(PROFILE_PATHS["first_live"]))
        frozen={name:(self.root/name).read_bytes() for name in PROFILE_PATHS["first_live"]}
        self.assertEqual(self.run_generator("--verify")[0],0)
        code,rejected=self.run_generator("--write")
        self.assertEqual(code,2)
        self.assertEqual(rejected["error_code"],"source_artifact_already_exists")
        self.assertEqual(frozen,{name:(self.root/name).read_bytes() for name in frozen})
        self.assertEqual(historical.read_bytes(),before)

    def test_added_source_and_modified_contract_cannot_reuse_snapshot(self):
        self.assertEqual(self.run_generator("--write")[0],0)
        extra=self.root/"src/researchops/new_module.py"
        extra.write_bytes(b"value = 1\n")
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"component_drift"):
            verify_current_profile(self.root)
        extra.unlink()
        contract=self.root/"evals/provider_completion_admission_link_v1/admission_link_contract_v1.json"
        contract.write_bytes(contract.read_bytes()+b"\n")
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"fixed_binding_drift"):
            build_current_profile_documents(self.root)

    def test_campaign_cannot_be_generated_without_registry_evidence(self):
        code,result=self.run_generator("--profile","campaign","--write")
        self.assertEqual(code,2)
        self.assertEqual(result["created_paths"],[])
        self.assertFalse((self.root/PROFILE_PATHS["campaign"][1]).exists())

    def test_v7_dispatch_is_source_only_and_depth60_runner_rejects_it(self):
        from researchops.phase6_depth60 import validate_phase6_depth60_plan,run_phase6_depth60_online
        from researchops.phase6_runner import Phase6RunError
        import asyncio
        self.assertEqual(self.run_generator("--write")[0],0)
        value=validate_phase6_depth60_plan(self.root,PROFILE_PATHS["first_live"][1])
        self.assertEqual(value["plan_id"],"phase6-deepseek-depth60-v7")
        self.assertFalse(value["online_execution_authorized"])
        class Poison(dict):
            def get(self,*args,**kwargs):
                raise AssertionError("environment must not be read")
        with self.assertRaises(Phase6RunError) as caught:
            asyncio.run(run_phase6_depth60_online(project_root=self.root,plan_path=PROFILE_PATHS["first_live"][1],
                output_directory=self.root/"no-run",authorization_id="not-authorized",authorization_expires_at_utc="2099-01-01T00:00:00Z",
                expected_plan_commitment=value["plan_commitment_sha256"],confirm_online=True,environment=Poison()))
        self.assertEqual(caught.exception.code,"phase6_depth60_successor_plan_not_executable")
        self.assertFalse((self.root/"no-run").exists())


if __name__=="__main__":
    unittest.main()
