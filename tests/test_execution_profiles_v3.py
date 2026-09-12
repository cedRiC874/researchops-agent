"""Timed-source successors on synthetic inventories and real temporary Git."""
from __future__ import annotations

import json
from collections.abc import Mapping
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_external_closure import execution_components as base
from researchops_external_closure import execution_components_v2 as v2
from researchops_external_closure import execution_components_v3 as v3
from researchops_external_closure.execution_binding_v3 import verify_historical_timed_profile
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from tests import execution_v3_fixture as fixtures
from tests.test_external_closure_execution_binding import write_file_tree
from tests.test_external_closure_git_objects import Repository


class TimedSourceProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Legacy algorithm conformance uses the pinned pre-v4 source fixture.
        cls.files, cls.paths = fixtures.archived_first_live_files()

    def select(self, paths, files=None, profile="first_live"):
        files = self.files if files is None else files
        return v3.select_profile_paths(paths, files[v3.RECIPE_PATH], files[v2.RECIPE_PATH], files[base.RECIPE_PATH], profile=profile)

    def build(self, files=None, paths=None, profile="first_live"):
        return v3.build_profile_documents(self.files if files is None else files,
            available_paths=self.paths if paths is None else paths, profile=profile)

    def test_new_required_contracts_are_covered_and_old_recipe_omits_them(self):
        old = set(v2.select_profile_paths(self.paths, self.files[v2.RECIPE_PATH], self.files[base.RECIPE_PATH], profile="first_live"))
        new = set(self.select(self.paths))
        for name in ("evals/provider_completion_timing_v1/timing_contract_v1.json",
                     "evals/provider_completion_first_live_review_v2/review_v2.schema.json",
                     "evals/provider_completion_first_live_artifact_v1/contract_v1.json"):
            self.assertNotIn(name, old)
            self.assertIn(name, new)
        documents = self.build()
        checked = v3.verify_profile_documents(self.files, available_paths=self.paths, profile="first_live",
            manifest=documents.manifest, plan=documents.plan)
        self.assertEqual(checked.selected_file_count, len(new))
        self.assertFalse(checked.runtime_admission_verified)
        self.assertFalse(checked.online_execution_authorized)
        self.assertFalse(checked.historical_result_revalidated)
        self.assertEqual(json.loads(documents.plan)["plan_id"], "phase6-deepseek-depth60-v9")

    def test_future_matching_contract_cannot_be_silently_omitted(self):
        documents = self.build()
        name = "evals/provider_completion_future_v1/new_contract.json"
        paths = tuple(sorted(set(self.paths) | {name}))
        files = self.files | {name: b'{"synthetic":true}'}
        self.assertIn(name, self.select(paths, files))
        self.assertNotEqual(documents.plan, self.build(files, paths).plan)
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_v3_file_set_mismatch"):
            self.build(self.files, paths)

    def test_removing_a_required_directory_is_not_treated_as_smaller_coverage(self):
        prefix = "evals/provider_completion_first_live_review_v2/"
        paths = tuple(path for path in self.paths if not path.startswith(prefix))
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_v3_required_file_missing"):
            self.select(paths)

    def test_unchanged_256_file_limit_still_fails_closed(self):
        additions = {f"evals/provider_completion_future_v1/{index}.json" for index in range(257)}
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_v3_coverage_invalid"):
            self.select(tuple(sorted(set(self.paths) | additions)))

    def test_oversized_file_mapping_is_rejected_before_key_enumeration(self):
        files = self.files
        class OversizedMapping(Mapping):
            def __len__(self):
                return 257
            def __getitem__(self, key):
                return files[key]
            def __iter__(self):
                raise AssertionError("oversized mapping must not be enumerated")
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_v3_file_set_mismatch"):
            self.build(OversizedMapping())

    def test_historical_v7_bytes_cannot_be_rewritten_preserving_literal_commitment(self):
        files = dict(self.files)
        name = v2.PROFILE_PATHS["first_live"][1]
        value = json.loads(files[name]); value["network_calls"] = 1
        files[name] = json.dumps(value).encode()
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_v3_v7_predecessor_drift"):
            self.build(files)

    def test_campaign_revalidates_same_first_live_snapshot_before_registry_data(self):
        first = self.build()
        files, paths = fixtures.campaign_files(self.files, self.paths, first)
        campaign = self.build(files, paths, profile="campaign")
        self.assertEqual(json.loads(campaign.plan)["plan_id"], "phase6-deepseek-depth60-v10")
        self.assertEqual(json.loads(campaign.plan)["predecessor"]["plan_commitment_sha256"], json.loads(first.plan)["plan_commitment_sha256"])
        self.assertFalse(json.loads(campaign.plan)["runtime_admission_verified"])
        # The empty synthetic registry payloads are hashed, not admitted.
        for name in ("src/researchops/__init__.py", "requirements.lock", "pyproject.toml",
                     "evals/provider_completion_first_live_review_v2/review_v2.schema.json"):
            changed = dict(files); changed[name] += b"\n"
            # This new-contract branch must reach the successor snapshot gate,
            # not merely fail at an unrelated legacy parser/schema check.
            error = "execution_v3_first_live_snapshot_drift" if "first_live_review_v2/" in name else ".*"
            with self.subTest(path=name), self.assertRaisesRegex(ExternalClosurePrimitiveError, error):
                self.build(changed, paths, profile="campaign")

    def test_core_has_no_io_or_provider_activity(self):
        with patch("builtins.open", side_effect=AssertionError("no IO")), \
             patch("subprocess.Popen", side_effect=AssertionError("no Git")), \
             patch("socket.socket", side_effect=AssertionError("no Provider")):
            self.build()

    def test_historical_reader_uses_fixed_git_bytes_not_current_worktree(self):
        first = self.build()
        manifest_path, plan_path = v3.PROFILE_PATHS["first_live"]
        with tempfile.TemporaryDirectory() as temporary:
            repository = Repository(Path(temporary) / "repo")
            tree = write_file_tree(repository, self.files | {manifest_path: first.manifest, plan_path: first.plan})
            commit = repository.commit(tree)
            result = verify_historical_timed_profile(repository.root, commit=commit, tree=tree, profile="first_live")
            self.assertEqual(result.components.source_integrity_commitment_sha256, json.loads(first.plan)["plan_commitment_sha256"])
            self.assertFalse(result.execution_observed)
            self.assertFalse(result.runtime_authority_granted)
            # No checkout is needed; adding a conflicting current file cannot
            # repair a historical object or choose its source inventory.
            path = repository.root / "src/researchops/__init__.py"
            path.parent.mkdir(parents=True); path.write_bytes(b"unrelated current source")
            again = verify_historical_timed_profile(repository.root, commit=commit, tree=tree, profile="first_live")
            self.assertEqual(result, again)


if __name__ == "__main__":
    unittest.main()
