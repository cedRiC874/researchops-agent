from pathlib import Path
import fnmatch
import json
import unittest
import yaml
from researchops_internal_telemetry import source_integrity_v3 as v3
from . import item6_experiment_fixture as f


class SourceV3Tests(unittest.TestCase):
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
