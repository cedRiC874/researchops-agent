"""Timed protocol/source identity via real temporary Git; no live execution."""
from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import first_live_identity as identity
from researchops_completion_timing import first_live_review as review
from researchops_completion_timing.contract import TimingContractError, _decode
from researchops_external_closure import execution_components_v3 as source
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import execution_v3_fixture as fixtures
from tests.test_external_closure_git_objects import Repository
from tests.test_external_closure_execution_binding import write_file_tree


ROOT = Path(__file__).resolve().parents[1]


class TimedImplementationIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temporary.cleanup)
        cls.repository = Repository(Path(cls.temporary.name) / "repo")
        # V4/v9 historical identity, not the expanded current-tree v5 inventory.
        cls.files, cls.paths = fixtures.archived_first_live_files()
        cls.documents = source.build_profile_documents(cls.files, available_paths=cls.paths, profile="first_live")
        manifest_path, plan_path = source.PROFILE_PATHS["first_live"]
        cls.tree = write_file_tree(cls.repository, cls.files | {manifest_path: cls.documents.manifest, plan_path: cls.documents.plan})
        cls.commit = cls.repository.commit(cls.tree)
        # Deliberately wrong current bytes. The verifier must read the Git blob,
        # not import or execute this checkout's implementation.
        current = cls.repository.root / identity.IMPLEMENTATION_PATH
        current.parent.mkdir(parents=True); current.write_bytes(b"wrong current worktree protocol")
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            cls.checked = identity.verify_historical_timed_implementation(cls.repository.root, commit=cls.commit, tree=cls.tree)
        protocol = json.loads(cls.files[identity.IMPLEMENTATION_PATH])
        cls.protocol_files = {path: cls.files[path] for path in {identity.IMPLEMENTATION_PATH} | {item["path"] for item in protocol["fixed_bindings"]}}

    def test_control_protocol_commitment_is_not_source_manifest_commitment(self):
        checked = self.checked
        self.assertEqual(checked.control_protocol_commitment_sha256, identity.IMPLEMENTATION_COMMITMENT_SHA256)
        self.assertEqual(checked.source_manifest_commitment_sha256, json.loads(self.documents.manifest)["commitment_sha256"])
        self.assertNotEqual(checked.control_protocol_commitment_sha256, checked.source_manifest_commitment_sha256)
        self.assertEqual(checked.subject["first_live_implementation_commitment_sha256"], checked.control_protocol_commitment_sha256)
        self.assertTrue(checked.control_protocol_identity_verified)
        self.assertTrue(checked.source_components_verified)
        for name in ("runnable_implementation_verified", "artifact_binding_verified", "review_verified", "registry_admission_verified",
                     "runtime_authority_granted", "first_live_success_verified"):
            self.assertIs(getattr(checked, name), False, name)

    def test_review_subject_is_derived_from_fixed_git_and_components_not_supplied(self):
        subject = self.checked.subject
        self.assertEqual(subject["first_live_execution_commit"], self.commit)
        self.assertEqual(subject["first_live_execution_tree"], self.tree)
        self.assertEqual(subject["first_live_source_integrity_commitment_sha256"], json.loads(self.documents.plan)["plan_commitment_sha256"])
        hashes = self.checked.historical.components.component_hashes
        for name in ("source_byte_inventory_sha256", "dependency_lock_sha256", "pyproject_sha256"):
            self.assertEqual(subject[name], hashes[name])
        self.assertEqual(subject["selected_mapping_sha256"], hashes["mapping_sha256"])
        self.assertNotIn("expected_subject", inspect.signature(identity.verify_historical_timed_implementation).parameters)
        with self.assertRaises(TypeError):
            subject["dependency_lock_sha256"] = "f" * 64
        _, schema = review.load_first_live_review_contract(ROOT)
        _decode(raw(dict(subject)), schema["properties"]["subject"], 8192, ())

    def test_pinned_protocol_is_not_a_runnable_implementation_or_source_snapshot(self):
        # Do not freeze the temporary absence of v9/v10 into a permanent test:
        # legitimate later source generation must not require weakening it.
        paths = [ROOT / path for pair in source.PROFILE_PATHS.values() for path in pair]
        before = {path: path.read_bytes() if path.exists() else None for path in paths}
        checked = identity.load_timed_implementation_contract(ROOT)
        self.assertFalse(checked["runnable_implementation_verified"])
        self.assertFalse(checked["source_profile_verified"])
        self.assertEqual(before, {path: path.read_bytes() if path.exists() else None for path in paths})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, payload in self.protocol_files.items():
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
            sentinel = root / source.PROFILE_PATHS["first_live"][1]
            sentinel.write_bytes(b"synthetic existing artifact; not a source proof")
            self.assertFalse(identity.load_timed_implementation_contract(root)["source_profile_verified"])
            self.assertEqual(sentinel.read_bytes(), b"synthetic existing artifact; not a source proof")

    def test_each_pinned_dependency_drift_is_rejected(self):
        for name in self.protocol_files:
            changed = dict(self.protocol_files); changed[name] += b"\n"
            code = "contract_invalid" if name == identity.IMPLEMENTATION_PATH else "dependency_drift"
            with self.subTest(path=name), self.assertRaisesRegex(TimingContractError, "timed_first_live_implementation_" + code):
                identity.verify_timed_implementation_files(changed)

    def test_missing_and_extra_protocol_files_are_not_ignored(self):
        missing = dict(self.protocol_files)
        missing.pop(next(name for name in missing if name != identity.IMPLEMENTATION_PATH))
        extra = self.protocol_files | {"extra.json": b"{}"}
        for files in (missing, extra):
            with self.assertRaisesRegex(TimingContractError, "timed_first_live_implementation_files_invalid"):
                identity.verify_timed_implementation_files(files)

    def test_pure_graph_verifier_performs_no_io(self):
        with patch("builtins.open", side_effect=AssertionError("no file IO")), \
             patch("subprocess.Popen", side_effect=AssertionError("no process IO")), \
             patch("socket.socket", side_effect=AssertionError("no Provider")):
            self.assertFalse(identity.verify_timed_implementation_files(self.protocol_files)["runtime_authority_granted"])

    def test_fixture_source_is_not_the_real_worktree_source_anchor(self):
        name = "src/researchops/__init__.py"
        self.assertNotEqual(self.files[name], (ROOT / name).read_bytes())
        self.assertIn(b"Synthetic archived source-profile fixture; not an execution anchor.", self.files[name])


if __name__ == "__main__":
    unittest.main()
