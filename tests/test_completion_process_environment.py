"""Process metadata checks; no distribution install, SDK client or Key read."""
from __future__ import annotations

import importlib.machinery
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import process_environment as environment
from researchops_completion_timing.contract import TimingContractError, digest


ROOT = Path(__file__).resolve().parents[1]


class ProcessEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.lock = (ROOT / "requirements.lock").read_bytes()
        self.project = (ROOT / "pyproject.toml").read_bytes()
        self.versions = environment._locked_versions(self.lock)

    def verify(self, **overrides):
        values = dict(expected_dependency_lock_sha256=digest(self.lock), expected_pyproject_sha256=digest(self.project))
        values.update(overrides)
        with patch("socket.socket", side_effect=AssertionError("no network")), \
             patch("subprocess.Popen", side_effect=AssertionError("no installation or Git")):
            return environment.verify_process_environment(ROOT, **values)

    @unittest.skipUnless(sys.platform == "win32", "the locked runtime includes pywin32")
    def test_actual_local_origins_and_versions_match_but_are_not_binary_attestation(self):
        result = self.verify()
        self.assertEqual(result["locked_distribution_count"], len(self.versions))
        self.assertGreater(result["loaded_project_module_count"], 0)
        self.assertTrue(result["loaded_project_origins_matched"])
        self.assertTrue(result["locked_distribution_versions_matched"])
        for name in ("loaded_bytecode_integrity_verified", "dependency_binary_integrity_verified", "extra_distributions_audited",
                     "trusted_launcher_verified", "runtime_authority_granted"):
            self.assertIs(result[name], False, name)

    def test_independent_hash_mismatch_precedes_version_queries(self):
        with patch.object(environment.importlib.metadata, "version", side_effect=AssertionError("must not query")):
            with self.assertRaisesRegex(TimingContractError, "process_environment_input_hash_mismatch"):
                self.verify(expected_dependency_lock_sha256="f" * 64)

    def test_lock_rejects_duplicate_normalized_names_and_non_exact_requirements(self):
        for payload in (b"PyJWT==2.0\npyjwt==2.0\n", b"thing>=1\n", b"-r other.txt\n", b"thing @ https://example.invalid/\n", b""):
            with self.subTest(payload=payload), self.assertRaisesRegex(TimingContractError, "process_environment_lock_invalid"):
                environment._locked_versions(payload)

    def test_wrong_version_and_metadata_failure_are_safe_errors(self):
        with patch.object(environment.importlib.metadata, "version", return_value="0"):
            with self.assertRaisesRegex(TimingContractError, "process_environment_locked_version_mismatch"):
                self.verify()
        with patch.object(environment.importlib.metadata, "version", side_effect=ValueError("synthetic-private-detail")):
            with self.assertRaisesRegex(TimingContractError, "process_environment_unavailable") as caught:
                self.verify()
        self.assertNotIn("synthetic-private-detail", str(caught.exception))

    def test_foreign_project_module_cannot_hide_behind_matching_versions(self):
        name = "researchops_completion_timing.foreign_fixture"
        module = types.ModuleType(name)
        module.__file__ = str(ROOT.parent / "foreign_fixture.py")
        module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None, origin=module.__file__)
        with patch.dict(sys.modules, {name: module}), patch.object(environment.importlib.metadata, "version", side_effect=self.versions.__getitem__):
            with self.assertRaisesRegex(TimingContractError, "process_environment_foreign_project_module"):
                self.verify()

    def test_metadata_inspection_does_not_invoke_module_getattr(self):
        name = "researchops_completion_timing.incomplete_fixture"
        module = types.ModuleType(name)
        def forbidden(name): raise AssertionError("module hook must not execute")
        module.__getattr__ = forbidden
        with patch.dict(sys.modules, {name: module}), patch.object(environment.importlib.metadata, "version", side_effect=self.versions.__getitem__):
            with self.assertRaisesRegex(TimingContractError, "process_environment_module_invalid"):
                self.verify()

    def test_added_package_search_path_is_rejected(self):
        package = sys.modules["researchops_completion_timing"]
        paths = list(package.__path__) + [str(ROOT.parent / "another-tree")]
        with patch.object(package, "__path__", paths), patch.object(environment.importlib.metadata, "version", side_effect=self.versions.__getitem__):
            with self.assertRaisesRegex(TimingContractError, "process_environment_package_path_mismatch"):
                self.verify()

    def test_version_change_during_check_is_not_returned_as_match(self):
        calls = []
        def version(name):
            calls.append(name)
            return self.versions[name] if len(calls) <= len(self.versions) else "0"
        with patch.object(environment.importlib.metadata, "version", side_effect=version):
            with self.assertRaisesRegex(TimingContractError, "process_environment_changed"):
                self.verify()

    def test_module_inventory_limit_is_checked_before_enumeration(self):
        class Oversized(dict):
            def __len__(self): return 8193
            def items(self): raise AssertionError("must not enumerate")
        with patch.object(environment.sys, "modules", Oversized()):
            with self.assertRaisesRegex(TimingContractError, "process_environment_module_limit"):
                environment._project_origins(ROOT)


if __name__ == "__main__":
    unittest.main()
