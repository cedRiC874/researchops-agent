from pathlib import Path
import fnmatch
import json
import unittest
import yaml
from researchops_internal_telemetry import source_integrity_v3 as v3
from . import item6_experiment_fixture as f


class SourceV3Tests(unittest.TestCase):
    def _parity_copy(self):
        root = f.allocate("i6-parity-")
        f.command(["git", "clone", "--shared", str(f.seed()), str(root)], f.ROOT)
        self.assertTrue(root.resolve().is_relative_to(root.parent.resolve()))
        return root

    def test_current_fixture_matches_all_selected_bytes_and_workflows(self):
        root = f.seed()
        result = f.verify_fixture_source_parity(f.ROOT, root)
        self.assertEqual(result["selected_count"], len(v3.source_files(f.ROOT)))
        self.assertEqual(result["source_commitment_sha256"], v3.verify_source(root)["commitment_sha256"])
        self.assertEqual(set(result["separately_bound_workflows"]), set(v3.SEPARATE_GIT_BINDINGS))
        reader = "src/researchops_external_closure/git_objects.py"
        self.assertIn(reader, f.OVERLAY)
        self.assertEqual((root / reader).read_bytes(), (f.ROOT / reader).read_bytes())
        recorded = json.loads((root / "output/item6-bridge-validation/seed-source-parity.json").read_bytes())
        self.assertEqual(recorded, result)

    def test_omitted_shared_reader_rejected_before_manifest_acceptance(self):
        root = self._parity_copy()
        name = "src/researchops_external_closure/git_objects.py"
        historical = v3.committed_blobs(f.ROOT, f.BASE, [name])[0]
        self.assertNotEqual(historical, (f.ROOT / name).read_bytes())
        (root / name).write_bytes(historical)
        with self.assertRaises(f.FixtureSourceParityError) as rejected:
            f.verify_fixture_source_parity(f.ROOT, root)
        self.assertEqual(rejected.exception.code, "fixture_selected_bytes_mismatch")
        self.assertEqual(rejected.exception.differing_paths, (name,))
        f.RESULTS.append(dict(kind="fixture_parity_negative", fault="omitted_reader", hit_count=1,
            actual_code=rejected.exception.code, prior_git_blob_restored_in_disposable_copy=True))

    def test_same_length_source_mutation_is_not_hidden_by_file_count(self):
        root = self._parity_copy()
        name = "src/researchops_item6_experiment_v1/budget.py"
        path = root / name; before = path.read_bytes()
        path.write_bytes(bytes([before[0] ^ 1]) + before[1:])
        self.assertEqual(len(path.read_bytes()), len(before))
        with self.assertRaises(f.FixtureSourceParityError) as rejected:
            f.verify_fixture_source_parity(f.ROOT, root)
        self.assertEqual(rejected.exception.code, "fixture_selected_bytes_mismatch")
        self.assertEqual(rejected.exception.differing_paths, (name,))
        f.RESULTS.append(dict(kind="fixture_parity_negative", fault="same_length_bytes", hit_count=1,
            actual_code=rejected.exception.code, byte_count_unchanged=True))

    def test_equal_count_renamed_source_and_separate_workflow_are_rejected(self):
        root = self._parity_copy()
        name = "src/researchops_external_closure/git_objects.py"
        before = v3.source_files(root)
        path = root / name; path.rename(path.with_name("git_objects_parity_renamed.py"))
        self.assertEqual(len(before), len(v3.source_files(root)))
        with self.assertRaises(f.FixtureSourceParityError) as renamed:
            f.verify_fixture_source_parity(f.ROOT, root)
        self.assertEqual(renamed.exception.code, "fixture_selected_paths_mismatch")
        root = self._parity_copy()
        workflow = v3.SEPARATE_GIT_BINDINGS[0]
        path = root / workflow; path.write_bytes(path.read_bytes() + b"\n# isolated parity mutation\n")
        with self.assertRaises(f.FixtureSourceParityError) as changed:
            f.verify_fixture_source_parity(f.ROOT, root)
        self.assertEqual(changed.exception.code, "fixture_separate_workflow_mismatch")
        self.assertEqual(changed.exception.differing_paths, (workflow,))
        f.RESULTS.append(dict(kind="fixture_parity_negative", fault="paths_and_workflow", hit_count=2,
            actual_codes=[renamed.exception.code, changed.exception.code]))

    def test_actual_git_lineage_and_separate_domain(self):
        root = f.seed()
        checked = v3.verify_source(root)
        self.assertEqual(checked["lineage"]["commit"], v3.HISTORICAL_COMMIT)
        self.assertEqual(checked["lineage"]["source_commitment_sha256"], v3.HISTORICAL_COMMITMENT)
        self.assertNotEqual(v3.DOMAIN, v3.v2.DOMAIN)
        self.assertFalse(checked["online_execution_authorized"])
        self.assertFalse(checked["runtime_admission_verified"])

    def test_selected_closure_and_workflow_hashes_are_acyclic(self):
        root = f.seed(); names = set(v3.source_files(root))
        self.assertNotIn(v3.MANIFEST, names)
        self.assertTrue(set(v3.SEPARATE_GIT_BINDINGS).isdisjoint(names))
        self.assertIn(v3.v2.MANIFEST, names)
        self.assertIn("src/researchops_item6_experiment_v1/authority.py", names)
        self.assertIn("services/agent_workflow_comparison_v1/controlled_comparison_v1/scoring/contracts.json", names)
        required = {
            "evals/item6_experiment_bridge_v1/CASE_REJECTION_ISOLATION_V1.md",
            "evals/item6_experiment_bridge_v1/case_rejection_policy_v1.json",
            "evals/item6_experiment_bridge_v1/freeze_v1_1.schema.json",
            "evals/item6_experiment_bridge_v1/artifact_v1_2.schema.json",
            "src/researchops_item6_experiment_v1/case_isolation.py",
            "tests/test_item6_case_isolation.py",
        }
        self.assertTrue(required <= names)
        recipe = json.loads((root / v3.RECIPE).read_text(encoding="utf-8"))
        self.assertEqual(recipe, v3.expected_recipe())
        self.assertTrue({
            "evals/item6_experiment_bridge_v1/CASE_REJECTION_ISOLATION_V1.md",
            "tests/test_item6_case_isolation.py",
        } <= set(recipe["explicit_files"]))
        directory = root / "evals/item6_experiment_bridge_v1"
        freeze = json.loads((directory / "freeze_v1_1.schema.json").read_text(encoding="utf-8"))
        artifact = json.loads((directory / "artifact_v1_2.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(artifact["properties"]["authority"]["properties"]["freeze"], freeze)

    def test_exclusive_manifest_does_not_replace_old_or_new(self):
        root = f.seed()
        before = {n: (root / n).read_bytes() for n in (v3.MANIFEST, v3.v2.MANIFEST, v3.v2.HISTORICAL_MANIFEST)}
        with self.assertRaisesRegex(v3.SourceIntegrityV3Error, "manifest_exists"):
            v3.write_manifest_exclusive(root)
        self.assertEqual(before, {n: (root / n).read_bytes() for n in before})

    def test_real_source_byte_mutation_is_rejected(self):
        root = f.allocate("i6-source-")
        f.command(["git", "clone", "--shared", str(f.seed()), str(root)], f.ROOT)
        path = root / "src/researchops_item6_experiment_v1/budget.py"
        path.write_bytes(path.read_bytes() + b"\n# isolated source mutation\n")
        with self.assertRaisesRegex(v3.SourceIntegrityV3Error, "source_drift"):
            v3.verify_source(root)

    def test_narrow_ci_covers_selected_inputs_and_itself(self):
        root = f.seed()
        workflow = yaml.load((root / ".github/workflows/item6-experiment-bridge-offline.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        for event in ("pull_request", "push"):
            patterns = workflow["on"][event]["paths"]
            for name in [*v3.source_files(root), v3.MANIFEST, *v3.SEPARATE_GIT_BINDINGS]:
                with self.subTest(event=event, name=name): self.assertTrue(any(fnmatch.fnmatchcase(name, p) for p in patterns))
