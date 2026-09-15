"""Report-path hardening only; no scorer, source manifest, or full suite execution."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("behavior_preparation_driver", ROOT / "scripts/verify_behavior_integration_preparation_v1.py")
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


class ReportPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="behavior-report-boundary-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "evals").mkdir()

    def test_parent_traversal_rejected_before_any_report_created(self):
        target = self.root / "evals/not-created.json"
        exploit = self.root / "output/../evals/not-created.json"
        self.assertTrue(exploit.absolute().is_relative_to(self.root / "output"))
        with self.assertRaisesRegex(ValueError, "report_parent_traversal"):
            DRIVER.reserve_report(self.root, exploit)
        self.assertFalse(target.exists())
        self.assertFalse((self.root / "output").exists())

    def test_relative_traversal_rejected(self):
        with self.assertRaisesRegex(ValueError, "report_parent_traversal"):
            DRIVER.reserve_report(self.root, Path("output/../evals/no.json"))
        self.assertFalse((self.root / "evals/no.json").exists())

    def test_direct_source_output_rejected(self):
        with self.assertRaisesRegex(ValueError, "under_output"):
            DRIVER.reserve_report(self.root, self.root / "evals/no.json")
        self.assertFalse((self.root / "evals/no.json").exists())

    def test_prefix_collision_directory_rejected(self):
        with self.assertRaisesRegex(ValueError, "under_output"):
            DRIVER.reserve_report(self.root, self.root / "output-other/no.json")
        self.assertFalse((self.root / "output-other").exists())

    def test_create_once_preserves_existing_receipt(self):
        path = self.root / "output/safe/receipt.json"
        with DRIVER.reserve_report(self.root, path) as stream:
            stream.write('{"first":true}')
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            DRIVER.reserve_report(self.root, path)
        self.assertEqual(path.read_bytes(), original)

    def test_output_directory_is_not_a_receipt(self):
        with self.assertRaisesRegex(ValueError, "report_file_required"):
            DRIVER.reserve_report(self.root, self.root / "output")

    def test_real_directory_link_cannot_escape_output(self):
        output = self.root / "output"
        output.mkdir()
        link = output / "escape"
        outside = self.root / "evals"
        if os.name == "nt":
            created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, timeout=30)
            self.assertEqual(created.returncode, 0, created.stderr.decode(errors="replace"))
        else:
            link.symlink_to(outside, target_is_directory=True)
        self.assertEqual(link.resolve(), outside.resolve())
        with self.assertRaisesRegex(ValueError, "report_directory_link_or_kind"):
            DRIVER.reserve_report(self.root, link / "no.json")
        self.assertFalse((outside / "no.json").exists())

    def test_file_cannot_stand_in_for_report_parent(self):
        (self.root / "output").mkdir()
        parent = self.root / "output/not-directory"
        parent.write_text("preserved", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "report_directory_link_or_kind"):
            DRIVER.reserve_report(self.root, parent / "no.json")
        self.assertEqual(parent.read_text(encoding="utf-8"), "preserved")

    def test_main_rejects_before_loading_any_test_suite(self):
        invalid = self.root / "output/../evals/not-created.json"
        with patch.object(DRIVER, "ROOT", self.root), patch.object(DRIVER.sys, "argv", ["driver", "--report", str(invalid)]):
            with patch.object(DRIVER.unittest, "TestLoader") as loader:
                with self.assertRaisesRegex(ValueError, "report_parent_traversal"):
                    DRIVER.main()
                loader.assert_not_called()
        self.assertFalse((self.root / "evals/not-created.json").exists())
