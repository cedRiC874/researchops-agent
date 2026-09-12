from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_external_closure import execution_components as base
from researchops_external_closure import execution_components_v2 as v2
from researchops_external_closure.execution_binding_v2 import verify_historical_profile
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.primitives import canonical_json_bytes
from tests.execution_v2_fixture import first_live_source_files,add_campaign_data
from tests.test_external_closure_git_objects import Repository
from tests.test_external_closure_execution_binding import write_file_tree


class ExecutionProfileV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files,cls.paths=first_live_source_files()
        cls.first=v2.build_profile_documents(cls.files,available_paths=cls.paths,profile="first_live")

    def test_both_profiles_are_deterministic_distinct_and_non_authorizing(self):
        self.assertEqual(self.first,v2.build_profile_documents(self.files,available_paths=self.paths,profile="first_live"))
        files,paths=add_campaign_data(self.files,self.paths,self.first)
        campaign=v2.build_profile_documents(files,available_paths=paths,profile="campaign")
        result=v2.verify_profile_documents(files,available_paths=paths,profile="campaign",manifest=campaign.manifest,plan=campaign.plan)
        self.assertFalse(result.runtime_admission_verified)
        self.assertFalse(result.online_execution_authorized)
        first=json.loads(self.first.plan); second=json.loads(campaign.plan)
        self.assertEqual(first["plan_id"],"phase6-deepseek-depth60-v7")
        self.assertEqual(second["plan_id"],"phase6-deepseek-depth60-v8")
        self.assertNotEqual(first["plan_commitment_sha256"],second["plan_commitment_sha256"])
        self.assertEqual(first["component_hashes"]["source_bundle_sha256"],second["component_hashes"]["source_bundle_sha256"])
        self.assertEqual(second["predecessor"]["plan_commitment_sha256"],first["plan_commitment_sha256"])
        self.assertNotIn(v2.PROFILE_PATHS["campaign"][0],files)

    def test_only_the_two_registry_activation_paths_can_follow_first_live(self):
        files,paths=add_campaign_data(self.files,self.paths,self.first)
        first_paths=v2.select_profile_paths(paths,files[v2.RECIPE_PATH],files[base.RECIPE_PATH],profile="first_live")
        self.assertEqual(self.first,v2.build_profile_documents({name:files[name] for name in first_paths},available_paths=paths,profile="first_live"))
        extra="evals/provider_completion_runtime_registry_v3/undeclared_extra.json"
        files[extra]=b"{}"; paths=tuple(sorted((*paths,extra)))
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"first_live_snapshot_drift"):
            v2.build_profile_documents(files,available_paths=paths,profile="campaign")

    def test_source_or_static_contract_change_cannot_reuse_first_live_snapshot(self):
        for name in ("src/researchops_external_closure/admission_review.py","requirements.lock","pyproject.toml"):
            files,paths=add_campaign_data(self.files,self.paths,self.first)
            files[name]+=b"\n"
            with self.subTest(name=name),self.assertRaisesRegex(ExternalClosurePrimitiveError,"first_live_snapshot_drift"):
                v2.build_profile_documents(files,available_paths=paths,profile="campaign")
        files=dict(self.files)
        files["evals/provider_completion_admission_link_v1/schemas/first_live_review_record_v1.schema.json"]+=b"\n"
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"admission_schema_drift"):
            v2.build_profile_documents(files,available_paths=self.paths,profile="first_live")

    def test_old_v6_metadata_is_preserved_and_recomputed(self):
        recipe=json.loads(self.files[v2.RECIPE_PATH])
        for key in ("plan_path","manifest_path"):
            name=recipe["frozen_predecessor_v6"][key]
            files=dict(self.files); files[name]+=b"\n"
            with self.subTest(name=name),self.assertRaisesRegex(ExternalClosurePrimitiveError,"v6_predecessor_drift"):
                v2.build_profile_documents(files,available_paths=self.paths,profile="first_live")

    def test_unknown_profile_missing_registry_and_self_consistent_tamper_reject(self):
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"profile_invalid"):
            v2.build_profile_documents(self.files,available_paths=self.paths,profile="nearest")
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"required_file_missing"):
            v2.build_profile_documents(self.files,available_paths=self.paths,profile="campaign")
        manifest=json.loads(self.first.manifest)
        manifest["file_inventory"].pop()
        domain=json.loads(self.files[v2.RECIPE_PATH])["profiles"]["first_live"]["manifest_domain"]
        manifest.pop("commitment_sha256")
        manifest["commitment_sha256"]=hashlib.sha256(domain.encode()+b"\0"+canonical_json_bytes(manifest)).hexdigest()
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"component_drift"):
            v2.verify_profile_documents(self.files,available_paths=self.paths,profile="first_live",manifest=canonical_json_bytes(manifest)+b"\n",plan=self.first.plan)

    def test_pure_derivation_has_no_filesystem_or_network(self):
        with patch("builtins.open",side_effect=AssertionError("file IO")),patch("pathlib.Path.read_bytes",side_effect=AssertionError("file IO")),patch("socket.socket",side_effect=AssertionError("network IO")):
            self.assertEqual(self.first,v2.build_profile_documents(self.files,available_paths=self.paths,profile="first_live"))

    def test_actual_git_profile_is_independent_of_worktree_and_current_head(self):
        files=dict(self.files)
        manifest_path,plan_path=v2.PROFILE_PATHS["first_live"]
        files.update({manifest_path:self.first.manifest,plan_path:self.first.plan})
        with tempfile.TemporaryDirectory() as directory:
            repo=Repository(Path(directory)/"repo")
            tree=write_file_tree(repo,files); commit=repo.commit(tree)
            repo.git("update-ref","refs/heads/main",commit)
            (repo.root/"dirty.txt").write_bytes(b"unrelated")
            result=verify_historical_profile(repo.root,commit=commit,tree=tree,profile="first_live")
            self.assertEqual(result.components.source_integrity_commitment_sha256,json.loads(self.first.plan)["plan_commitment_sha256"])
            self.assertFalse(result.runtime_authority_granted)
            self.assertFalse(result.execution_observed)
