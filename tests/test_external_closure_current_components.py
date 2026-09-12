from __future__ import annotations

import tempfile
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_external_closure import execution_binding as binding
from researchops_external_closure.execution_components import MANIFEST_PATH, V6_PLAN_PATH
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from tests.test_external_closure_execution_components import component_fixture_files


class CurrentExecutionComponentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        files, _ = component_fixture_files()
        for name, raw in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)

    def test_generation_is_read_only_and_written_candidate_detects_source_drift(self) -> None:
        before = {path.relative_to(self.root).as_posix() for path in self.root.rglob("*") if path.is_file()}
        documents = binding.build_current_execution_component_documents(self.root)
        after = {path.relative_to(self.root).as_posix() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        (self.root / MANIFEST_PATH).write_bytes(documents.implementation_manifest)
        (self.root / V6_PLAN_PATH).write_bytes(documents.source_integrity_v6_plan)
        verified = binding.verify_current_execution_components(self.root)
        self.assertFalse(verified.runtime_authority_granted)
        self.assertFalse(verified.admission_verified)
        target = self.root / "src/researchops_external_closure/final.py"
        target.write_bytes(target.read_bytes() + b"\n# modified\n")
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "implementation_drift"):
            binding.verify_current_execution_components(self.root)

    def test_added_module_is_covered_by_current_source_verification(self) -> None:
        documents = binding.build_current_execution_component_documents(self.root)
        (self.root / MANIFEST_PATH).write_bytes(documents.implementation_manifest)
        (self.root / V6_PLAN_PATH).write_bytes(documents.source_integrity_v6_plan)
        (self.root / "src/researchops_external_closure/additional.py").write_bytes(b"# added module\n")
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "implementation_drift"):
            binding.verify_current_execution_components(self.root)

    def test_file_addition_during_snapshot_is_detected(self) -> None:
        original = binding._current_file
        injected = False

        def read_and_add(root, relative):
            nonlocal injected
            raw = original(root, relative)
            if not injected and relative.startswith("src/"):
                injected = True
                (self.root / "src/researchops_external_closure/during_snapshot.py").write_bytes(b"# race\n")
            return raw

        with patch.object(binding, "_current_file", side_effect=read_and_add):
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, "current_tree_changed"):
                binding.build_current_execution_component_documents(self.root)
        self.assertTrue(injected)

    def test_missing_current_file_cannot_be_filled_from_another_worktree(self) -> None:
        (self.root / "requirements.lock").unlink()
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "current_file_unavailable"):
            binding.build_current_execution_component_documents(self.root)

    def test_v6_dispatcher_is_valid_but_online_runner_rejects_before_environment(self) -> None:
        from researchops.phase6_depth60 import validate_phase6_depth60_plan, run_phase6_depth60_online
        from researchops.phase6_runner import Phase6RunError

        documents = binding.build_current_execution_component_documents(self.root)
        (self.root / MANIFEST_PATH).write_bytes(documents.implementation_manifest)
        (self.root / V6_PLAN_PATH).write_bytes(documents.source_integrity_v6_plan)
        result = validate_phase6_depth60_plan(self.root, V6_PLAN_PATH)
        self.assertEqual(result["plan_id"], "phase6-deepseek-depth60-v6")
        self.assertFalse(result["online_execution_authorized"])
        self.assertFalse(result["runtime_admission_verified"])

        class PoisonEnvironment(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("environment must not be inspected")

        with self.assertRaises(Phase6RunError) as caught:
            asyncio.run(run_phase6_depth60_online(
                project_root=self.root, plan_path=V6_PLAN_PATH,
                output_directory=self.root / "must-not-exist",
                authorization_id="not-an-online-grant",
                authorization_expires_at_utc="2000-01-01T00:00:00Z",
                expected_plan_commitment=result["plan_commitment_sha256"],
                confirm_online=True, environment=PoisonEnvironment(),
            ))
        self.assertEqual(caught.exception.code, "phase6_depth60_successor_plan_not_executable")
        self.assertFalse((self.root / "must-not-exist").exists())


if __name__ == "__main__":
    unittest.main()
