from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from researchops_external_closure.execution_components import MANIFEST_PATH, V6_PLAN_PATH
from tests.test_external_closure_execution_components import component_fixture_files


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("v6_generator", ROOT / "scripts/build_depth60_successor_plan_v6.py")
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class V6GeneratorTests(unittest.TestCase):
    def test_preview_is_read_only_write_is_exclusive_and_verify_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files, _ = component_fixture_files()
            for name, raw in files.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            before = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}

            def run(*options):
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    code = generator.main(["--project-root", str(root), *options])
                return code, json.loads(stream.getvalue())

            code, preview = run()
            self.assertEqual(code, 0)
            self.assertEqual(preview["created_paths"], [])
            self.assertFalse((root / MANIFEST_PATH).exists())
            code, created = run("--write")
            self.assertEqual(code, 0)
            self.assertEqual(created["created_paths"], [MANIFEST_PATH, V6_PLAN_PATH])
            self.assertEqual(preview["source_integrity_v6"], created["source_integrity_v6"])
            snapshots = {path: (root / path).read_bytes() for path in (MANIFEST_PATH, V6_PLAN_PATH)}
            code, verified = run("--verify")
            self.assertEqual(code, 0)
            self.assertEqual(verified["status"], "valid_source_integrity_only")
            self.assertFalse(verified["runtime_admission_verified"])
            code, rejected = run("--write")
            self.assertEqual(code, 2)
            self.assertEqual(rejected["error_code"], "source_artifact_already_exists")
            self.assertEqual(rejected["created_paths"], [])
            for path, raw in snapshots.items():
                self.assertEqual((root / path).read_bytes(), raw)
            for path, raw in before.items():
                self.assertEqual((root / path).read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
