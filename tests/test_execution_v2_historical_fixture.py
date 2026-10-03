"""Historical replay cannot waive current coverage or the production v2 bound."""
from pathlib import Path
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from researchops_external_closure import execution_components as base
from researchops_external_closure import execution_components_v2 as v2
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_internal_telemetry import source_integrity_v3 as current
from tests import execution_v2_fixture as fixture


class HistoricalV2FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files, cls.paths = fixture.first_live_source_files()
        cls.first = v2.build_profile_documents(cls.files, available_paths=cls.paths, profile="first_live")

    def git(self, *args, input=None):
        env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP") if k in os.environ}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
        return subprocess.run(["git", *args], cwd=fixture.ROOT, env=env, input=input,
                              check=True, capture_output=True, timeout=120).stdout

    def test_inventory_and_every_selected_byte_match_fixed_git_snapshot(self):
        self.assertEqual(self.git("rev-parse", fixture.HISTORICAL_COMMIT + "^{tree}").decode().strip(), fixture.HISTORICAL_TREE)
        inventory = self.git("ls-tree", "-r", "-z", fixture.HISTORICAL_COMMIT)
        names = tuple(sorted(entry.split(b"\t", 1)[1].decode() for entry in inventory.split(b"\0") if entry))
        self.assertEqual(self.paths, names)
        selected = v2.select_profile_paths(names, self.files[v2.RECIPE_PATH], self.files[base.RECIPE_PATH], profile="first_live")
        self.assertEqual(set(selected), set(self.files))
        raw = self.git("cat-file", "--batch", input="".join(f"{fixture.HISTORICAL_COMMIT}:{n}\n" for n in selected).encode())
        offset = 0
        for name in selected:
            end = raw.index(b"\n", offset)
            oid, kind, size = raw[offset:end].split()
            self.assertEqual(kind, b"blob")
            size = int(size); offset = end + 1
            self.assertEqual(self.files[name], raw[offset:offset + size], name)
            self.assertEqual(raw[offset + size:offset + size + 1], b"\n")
            offset += size + 1
        self.assertEqual(offset, len(raw))
        self.assertEqual(len(self.files), 245)
        files, paths = fixture.add_campaign_data(self.files, self.paths, self.first)
        self.assertEqual(len(files), 249)
        campaign = v2.build_profile_documents(files, available_paths=paths, profile="campaign")
        checked = v2.verify_profile_documents(files, available_paths=paths, profile="campaign",
                                              manifest=campaign.manifest, plan=campaign.plan)
        self.assertFalse(checked.online_execution_authorized)
        self.assertFalse(checked.runtime_admission_verified)

    def test_cold_replay_never_reads_or_scans_worktree_bytes(self):
        with patch.object(Path, "read_bytes", side_effect=AssertionError("worktree bytes")), \
             patch.object(Path, "rglob", side_effect=AssertionError("worktree inventory")):
            items, paths = fixture._historical_first_live_snapshot.__wrapped__()
        self.assertEqual(dict(items), self.files)
        self.assertEqual(paths, self.paths)

    def test_missing_fixed_object_cannot_fallback_to_worktree(self):
        with patch.object(fixture, "read_git_object_snapshot", side_effect=ExternalClosurePrimitiveError("synthetic_object_missing")) as reader:
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, "synthetic_object_missing"):
                fixture._historical_first_live_snapshot.__wrapped__()
        reader.assert_called_once_with(fixture.ROOT, fixture.HISTORICAL_COMMIT,
            (base.RECIPE_PATH, v2.RECIPE_PATH), expected_tree_oid=fixture.HISTORICAL_TREE)

    def test_caller_mutation_does_not_poison_historical_cache(self):
        files, paths = fixture.first_live_source_files()
        files[base.RECIPE_PATH] = b"changed by one test"
        files["src/researchops_external_closure/extra.py"] = b"synthetic"
        self.assertEqual(fixture.first_live_source_files(), (self.files, self.paths))
        self.assertEqual(paths, self.paths)

    def test_added_source_is_selected_and_cannot_reuse_old_commitment(self):
        name = "src/researchops_external_closure/new_security_boundary.py"
        paths = tuple(sorted((*self.paths, name)))
        self.assertIn(name, v2.select_profile_paths(paths, self.files[v2.RECIPE_PATH], self.files[base.RECIPE_PATH], profile="first_live"))
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_v2_file_set_mismatch"):
            v2.build_profile_documents(self.files, available_paths=paths, profile="first_live")
        files = {**self.files, name: b"# synthetic additional source\n"}
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_v2_component_drift"):
            v2.verify_profile_documents(files, available_paths=paths, profile="first_live", manifest=self.first.manifest, plan=self.first.plan)
        changed = v2.build_profile_documents(files, available_paths=paths, profile="first_live")
        self.assertNotEqual(json.loads(changed.manifest)["component_hashes"]["source_bundle_sha256"],
                            json.loads(self.first.manifest)["component_hashes"]["source_bundle_sha256"])
        campaign_files, campaign_paths = fixture.add_campaign_data(self.files, self.paths, self.first)
        campaign_files[name] = files[name]
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_v2_first_live_snapshot_drift"):
            v2.build_profile_documents(campaign_files, available_paths=tuple(sorted((*campaign_paths, name))), profile="campaign")

    def test_exactly_256_selected_paths_allowed_and_257_rejected(self):
        campaign_files, campaign_paths = fixture.add_campaign_data(self.files, self.paths, self.first)
        for profile, files, paths in (("first_live", self.files, self.paths), ("campaign", campaign_files, campaign_paths)):
            with self.subTest(profile=profile):
                # Extra contract files exercise the v2 bound independently of the base bound.
                additions = tuple(f"evals/provider_completion_admission_link_v1/synthetic-{i}.json" for i in range(257 - len(files)))
                at_limit = tuple(sorted((*paths, *additions[:-1])))
                self.assertEqual(len(v2.select_profile_paths(at_limit, files[v2.RECIPE_PATH], files[base.RECIPE_PATH], profile=profile)), 256)
                with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_v2_coverage_invalid"):
                    v2.select_profile_paths(tuple(sorted((*at_limit, additions[-1]))), files[v2.RECIPE_PATH], files[base.RECIPE_PATH], profile=profile)

    def test_current_v3_commitment_still_verifies_without_fixture_binding(self):
        checked = current.verify_source(fixture.ROOT)
        selected = {row["path"] for row in checked["files"]}
        self.assertNotIn("tests/execution_v2_fixture.py", selected)
        self.assertNotIn("tests/test_execution_v2_historical_fixture.py", selected)
        self.assertIn("src/researchops_item6_experiment_v1/runner.py", selected)
        self.assertEqual(len(selected), 499)
        self.assertFalse(checked["online_execution_authorized"])
