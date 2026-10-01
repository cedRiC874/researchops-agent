"""Publication boundary checks; temporary copies never mint runtime authority."""
from . import binding as b
from .public_artifacts import project, derive, write_json, added_files_diff
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
import fnmatch
import yaml
from unittest.mock import patch


class PublicationTests(unittest.TestCase):
    def materialize(self, directory):
        root = Path(directory)
        manifest = b.read_manifest()
        for name in [*manifest["files"], b.MANIFEST]:
            dest = root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(b.REPO / name, dest)
        return root

    def test_clean_materialization_needs_no_historical_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.materialize(directory)
            self.assertFalse(any(root.rglob("outputs")))
            self.assertEqual(b.verify_files(root), b.read_manifest())

    def test_missing_changed_or_extra_source_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.materialize(directory)
            target = root / b.PREFIX / "deepseek_flash_v1/wire.py"
            original = target.read_bytes()
            target.write_bytes(original + b"\n# changed\n")
            with self.assertRaisesRegex(ValueError, "source_bytes_mismatch"): b.verify_files(root)
            target.write_bytes(original)
            target.unlink()
            with self.assertRaisesRegex(ValueError, "source_inventory_mismatch"): b.verify_files(root)
            target.write_bytes(original)
            (target.parent / "extra.py").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source_inventory_mismatch"): b.verify_files(root)

    def test_manifest_traversal_and_duplicate_fields_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.materialize(directory)
            manifest = b.read_manifest(root)
            manifest["files"]["../outside.py"] = "0" * 64
            (root / b.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest_path"): b.read_manifest(root)
            (root / b.MANIFEST).write_text('{"files":{},"files":{}}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate_manifest_key"): b.read_manifest(root)

    def test_named_task_successor_is_frozen_with_exact_bytes(self):
        name = b.PREFIX + "controlled_comparison_v1/tasks/frozen/tasks_v2.json"
        manifest = b.read_manifest()
        self.assertEqual(len(manifest["files"]), 49)
        self.assertIn(name, b.source_names())
        self.assertIn(name, manifest["files"])
        self.assertEqual(manifest["files"][name], b.sha(b.REPO / name))
        self.assertEqual(set(manifest["files"]), set(b.source_names()))

    def test_named_task_successor_missing_or_mutated_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.materialize(directory)
            name = b.PREFIX + "controlled_comparison_v1/tasks/frozen/tasks_v2.json"
            target = root / name
            original = target.read_bytes()
            with self.subTest(fault="missing_successor"):
                target.unlink()
                with self.assertRaisesRegex(ValueError, "source_inventory_mismatch"):
                    b.verify_files(root)
            target.write_bytes(original)
            with self.subTest(fault="same_json_different_bytes"):
                target.write_bytes(original + b"\n")
                self.assertEqual(json.loads(target.read_bytes()), json.loads(original))
                with self.assertRaisesRegex(ValueError, "source_bytes_mismatch"):
                    b.verify_files(root)

    def fake_git(self, root, tracked=True, ancestor=True):
        # Simulated Git identity unit fixture, not a committed execution receipt.
        names = [*b.read_manifest(root)["files"], b.MANIFEST]
        def call(repo, *args):
            if args[0] == "rev-parse": return b"1111111111111111111111111111111111111111\n"
            if args[0] == "merge-base":
                if not ancestor: raise subprocess.CalledProcessError(1, ["git", "merge-base"])
                return b""
            if args[0] == "ls-tree": return "\n".join(names if tracked else []).encode()
            if args[0] == "show": return (root / args[1].split(":", 1)[1]).read_bytes()
            raise AssertionError(args)
        return call

    def test_new_committed_head_can_be_distinct_from_historical_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.materialize(directory)
            with patch.object(b, "_git", self.fake_git(root)):
                result = b.verify_identity(root, require_committed=True)
            self.assertEqual(result["execution_commit"], "1" * 40)
            self.assertEqual(result["execution_base_commit"], b.BASE)
            self.assertFalse(result["online_authorized"])

    def test_uncommitted_and_unrelated_head_rejected_for_ci(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.materialize(directory)
            with patch.object(b, "_git", self.fake_git(root, tracked=False)):
                self.assertIsNone(b.verify_identity(root)["execution_commit"])
                with self.assertRaisesRegex(ValueError, "committed_source_required"):
                    b.verify_identity(root, require_committed=True)
            with patch.object(b, "_git", self.fake_git(root, ancestor=False)):
                with self.assertRaises(subprocess.CalledProcessError): b.verify_identity(root)

    def test_public_projection_preserves_null_empty_whitespace_and_counts(self):
        value = {"log": str(b.REPO / "local.py"), "outputs": [None, "", " \t"],
                 "counts": {"planned": 16, "unknown": 1}, "cost": None}
        result = project(value)
        self.assertEqual(result["outputs"], value["outputs"])
        self.assertEqual(result["counts"], value["counts"])
        self.assertIsNone(result["cost"])
        self.assertNotIn(str(b.REPO), result["log"])

    def test_public_derivation_binds_original_and_refuses_answer_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); src = root / "original.json"
            write_json(src, {"final_output": "质量为1 mg [E1]。", "log": "C:/Users/example/private.py"})
            mapping = derive(src, root / "public.json")
            self.assertEqual(mapping["original_sha256"], hashlib.sha256(src.read_bytes()).hexdigest())
            self.assertNotIn("C:/Users", (root / "public.json").read_text(encoding="utf-8"))
            src.write_text(json.dumps({"final_output": "C:/Users/example/private.py"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "public_projection_would_change_answer"):
                derive(src, root / "rejected.json")
            self.assertFalse((root / "rejected.json").exists())

    def test_original_unavailability_not_presented_as_preservation_pass(self):
        from ..controlled_comparison_v1 import runtime
        self.assertEqual(runtime.IDENTITY["historical_origin_check"], "not_performed_in_this_context")
        self.assertFalse(runtime.IDENTITY["online_authorized"])

    def test_ci_paths_cover_every_bound_input_for_pr_and_main(self):
        workflow = yaml.load((b.REPO / ".github/workflows/item6-controlled-comparison.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        names = [*b.read_manifest()["files"], b.MANIFEST]
        self.assertEqual(workflow["on"]["push"]["branches"], ["main"])
        for event in ("pull_request", "push"):
            paths = workflow["on"][event]["paths"]
            for name in names:
                with self.subTest(event=event, name=name):
                    self.assertTrue(any(fnmatch.fnmatchcase(name, pattern) for pattern in paths))

    def test_added_diff_separates_unterminated_file_from_next_header(self):
        patch_bytes = added_files_diff({"a.json": b'{"value":1}', "b.json": b'{}\n'})
        self.assertIn(b'+{"value":1}\n\\ No newline at end of file\ndiff --git a/b.json b/b.json\n', patch_bytes)
        self.assertEqual(patch_bytes.count(b"\\ No newline at end of file\n"), 1)
        self.assertEqual(patch_bytes.count(b"\n@@ -0,0 +1,1 @@\n"), 2)
        self.assertTrue(patch_bytes.endswith(b"+{}\n"))

    def test_added_diff_preserves_empty_and_newline_only_files(self):
        empty = added_files_diff({"empty.txt": b""})
        self.assertIn(b"..e69de29bb2d1d6434b8b29ae775ad8c2e48c5391\n", empty)
        self.assertNotIn(b"@@", empty)
        blank = added_files_diff({"blank.txt": b"\n"})
        self.assertTrue(blank.endswith(b"@@ -0,0 +1,1 @@\n+\n"))
        self.assertNotIn(b"No newline", blank)

    def test_added_diff_rejects_unsafe_paths_and_binary_input(self):
        for name in ("../escape", "/absolute", "a//b", "a\nheader", "C:/absolute"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "diff_relative_path_required"):
                added_files_diff({name: b"text"})
        with self.assertRaisesRegex(ValueError, "diff_utf8_text_required"):
            added_files_diff({"a.bin": b"\0"})
