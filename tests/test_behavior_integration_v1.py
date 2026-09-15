"""Integration-only discovery and publication checks, not Agent scores."""
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "evals/internal_behavior_eval_v1"


def load_utility():
    spec = importlib.util.spec_from_file_location("behavior_publication_builder", ROOT / "scripts/build_behavior_publication_v1.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DiscoveryIntegrationTests(unittest.TestCase):
    def run_wrapper(self, directory, scope, expected):
        with tempfile.TemporaryDirectory(prefix="behavior-wrapper-") as temp:
            report = Path(temp) / "receipt.json"
            child = subprocess.run([sys.executable, "-B", "-X", "utf8", str(BASE / directory / "verify.py"),
                "--scope", scope, "--report", str(report)], cwd=str(ROOT), capture_output=True, text=True, timeout=120)
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            receipt = json.loads(report.read_bytes())
            self.assertEqual((receipt["tests"], receipt["failures"], receipt["errors"], receipt["skips"], receipt["exit_code"]),
                (expected, 0, 0, 0, 0))
            self.assertTrue(receipt["binding"]["unchanged"])
            self.assertEqual(len(receipt["test_ids"]), len(set(receipt["test_ids"])))
            self.assertEqual(receipt["unique_test_id_count"], expected)
            self.assertTrue(all(name.startswith("tests.test_behavior_eval_v1") for name in receipt["test_ids"]))
            self.assertFalse(receipt["root_full_regression"])

    def test_repair_full_package_discovery(self):
        self.run_wrapper("repair_p1", "full", 129)

    def test_text_full_package_discovery(self):
        self.run_wrapper("text_observation_v1_1", "full", 129)

    def test_text_related_five_keep_unique_ids(self):
        self.run_wrapper("text_observation_v1_1", "related", 5)

    def test_package_discovery_in_clean_subprocess_has_129_unique_ids(self):
        code = """import json,unittest
from pathlib import Path
root=Path.cwd()
loader=unittest.TestLoader()
suite=loader.discover(str(root/'tests'), pattern='test_behavior_eval_v1*.py', top_level_dir=str(root))
def ids(node):
 return [i for child in node for i in ids(child)] if isinstance(node,unittest.TestSuite) else [node.id()]
values=ids(suite)
print(json.dumps({'count':len(values),'unique':len(set(values)),'errors':loader.errors}))
"""
        child = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout), {"count": 129, "unique": 129, "errors": []})

    def test_no_imported_testcase_classes(self):
        import tests.test_behavior_eval_v1_tolerance as tolerance
        import tests.test_behavior_eval_v1_text_observation as text
        for module in (tolerance, text):
            for value in vars(module).values():
                if isinstance(value, type) and issubclass(value, unittest.TestCase):
                    self.assertEqual(value.__module__, module.__name__)


class PublicDerivationTests(unittest.TestCase):
    def test_unsafe_original_reports_and_archives_absent(self):
        utility = load_utility()
        for relative in (*utility.REPORTS, *utility.ARCHIVES, *utility.HANDOFFS):
            self.assertFalse((BASE / relative).exists(), relative)

    def test_all_public_text_and_decoded_json_have_no_user_absolute_path(self):
        utility = load_utility()
        for path in BASE.rglob("*"):
            if path.is_file() and path.suffix in (".md", ".json", ".py"):
                value = json.loads(path.read_bytes()) if path.suffix == ".json" else path.read_text(encoding="utf-8")
                self.assertFalse(utility.private_paths(value), str(path.relative_to(ROOT)))

    def test_public_derivative_hashes_and_failed_outcomes(self):
        record = json.loads((BASE / "PUBLIC_DERIVATION_PROVENANCE_v1.json").read_bytes())
        for row in record["reports"]:
            data = (BASE / row["derivative_relative_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), row["derivative_sha256"])
            self.assertNotEqual(row["original_sha256"], row["derivative_sha256"])
            report = json.loads(data)
            self.assertEqual({key: report[key] for key in row["outcome_preserved"]}, row["outcome_preserved"])
            self.assertEqual(report["exit_code"], 1)
        self.assertEqual([r["outcome_preserved"]["failures"] for r in record["reports"]], [5, 7])
        self.assertEqual([r["outcome_preserved"]["errors"] for r in record["reports"]], [0, 17])
        self.assertTrue(any("nested_members" in member for archive in record["archives"] for member in archive["members"]))

    def test_handoff_derivative_hashes(self):
        record = json.loads((BASE / "PUBLIC_HANDOFF_DERIVATION_PROVENANCE_v1.json").read_bytes())
        self.assertEqual(len(record["reports"]), 3)
        for row in record["reports"]:
            data = (BASE / row["derivative_relative_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), row["derivative_sha256"])
            self.assertNotEqual(row["original_sha256"], row["derivative_sha256"])
