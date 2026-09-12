from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from researchops_external_closure import execution_binding as binding
from researchops_external_closure import execution_components as components
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from tests.test_external_closure_execution_components import component_fixture_files
from tests.test_external_closure_git_objects import Repository


def write_file_tree(repo: Repository, files: dict[str, bytes]) -> str:
    tree: dict = {}
    blobs: dict[bytes, str] = {}
    for path, raw in sorted(files.items()):
        node = tree
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if raw not in blobs:
            blobs[raw] = repo.object("blob", raw)
        node[parts[-1]] = blobs[raw]

    def build(node: dict) -> str:
        return repo.tree(*(
            ("40000", name, build(child)) if isinstance(child, dict)
            else ("100644", name, child)
            for name, child in node.items()
        ))

    return build(tree)


class HistoricalExecutionBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.repo = Repository(Path(cls.temporary.name) / "repo")
        files, paths = component_fixture_files()
        # Source bytes are an explicit synthetic identity fixture. They are
        # committed and hashed, never executed or claimed to be a working Agent.
        cls.files = {
            path: (b"# synthetic component-identity fixture; never executed\n" if path.endswith(".py") else raw)
            for path, raw in files.items()
        }
        cls.paths = paths
        built = components.build_execution_component_documents(cls.files, available_paths=paths)
        cls.documents = built
        cls.committed_files = {
            **cls.files,
            components.MANIFEST_PATH: built.implementation_manifest,
            components.V6_PLAN_PATH: built.source_integrity_v6_plan,
        }
        cls.tree = write_file_tree(cls.repo, cls.committed_files)
        cls.commit = cls.repo.commit(cls.tree)
        manifest = json.loads(built.implementation_manifest)
        plan = json.loads(built.source_integrity_v6_plan)
        recipe = json.loads(cls.files[components.RECIPE_PATH])
        config = recipe["runner_config_values"]
        cls.execution = {
            "execution_commit": cls.commit,
            "execution_tree": cls.tree,
            "implementation_commitment_sha256": manifest["commitment_sha256"],
            "source_integrity_commitment_sha256": plan["plan_commitment_sha256"],
            **{name: manifest["component_hashes"][name] for name in binding._COMPONENT_FIELDS},
            **{name: config[name] for name in binding._PROVIDER_FIELDS},
            "closure_evidence_contract_commitment_sha256": recipe["predecessors"]["closure_evidence"]["semantic_commitment"],
            # These self-reports are deliberately NOT attested by this verifier.
            "first_live_evidence_commitment_sha256": "a" * 64,
            "runtime_registry_commitment_sha256": "b" * 64,
            "first_live_validation_status": "reviewed_success",
            "runtime_registry_binding_allowed": True,
        }

    def verify(self, execution=None):
        return binding.verify_execution_component_binding(
            self.repo.root, self.execution if execution is None else execution
        )

    def test_real_git_component_proof_never_becomes_execution_admission(self) -> None:
        result = self.verify()
        self.assertEqual(result.execution_commit, self.commit)
        self.assertEqual(result.execution_tree, self.tree)
        self.assertEqual(result.verified_file_count, len(self.files))
        self.assertFalse(result.admission_verified)
        self.assertFalse(result.first_live_review_verified)
        self.assertFalse(result.registry_admission_verified)
        self.assertFalse(result.runtime_authority_granted)
        with self.assertRaises(TypeError):
            binding.VerifiedHistoricalExecutionComponents()
        with self.assertRaises(FrozenInstanceError):
            result.admission_verified = True
        with self.assertRaises(TypeError):
            result.component_hashes["source_bundle_sha256"] = "f" * 64

    def test_current_head_and_worktree_cannot_replace_historical_files(self) -> None:
        before = self.verify()
        new_tree = self.repo.tree(("100644", "unrelated.txt", self.repo.object("blob", b"new head")))
        self.repo.git("update-ref", "refs/heads/main", self.repo.commit(new_tree, self.commit))
        (self.repo.root / "requirements.lock").write_bytes(b"dirty worktree is not the historical lock")
        self.assertEqual(self.verify(), before)

    def test_wrong_tree_missing_commit_and_self_reported_hashes_reject(self) -> None:
        for field, value, expected in (
            ("execution_tree", "f" * 40, "git_tree_mismatch"),
            ("execution_commit", "1" * 40, "git_object_unavailable"),
            ("implementation_commitment_sha256", "f" * 64, "execution_component_binding_mismatch"),
            ("source_integrity_commitment_sha256", "f" * 64, "execution_component_binding_mismatch"),
            ("mapping_sha256", "f" * 64, "execution_component_binding_mismatch"),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ExternalClosurePrimitiveError, expected):
                    self.verify({**self.execution, field: value})

    def test_adding_or_mutating_source_cannot_reuse_old_manifest(self) -> None:
        for path in (
            "src/researchops_external_closure/execution_binding.py",
            "src/unlisted_package/new_module.py",
        ):
            with self.subTest(path=path):
                tree = write_file_tree(self.repo, {**self.committed_files, path: b"# changed source\n"})
                commit = self.repo.commit(tree, self.commit)
                with self.assertRaisesRegex(ExternalClosurePrimitiveError, "components_implementation_drift"):
                    self.verify({**self.execution, "execution_commit": commit, "execution_tree": tree})

    def test_bound_blob_absent_cannot_be_repaired_from_current_worktree(self) -> None:
        # A separate real tree omits the required manifest. Adding it as an
        # untracked worktree file must not fill the committed-tree gap.
        files = dict(self.committed_files)
        files.pop(components.MANIFEST_PATH)
        tree = write_file_tree(self.repo, files)
        commit = self.repo.commit(tree, self.commit)
        path = self.repo.root / components.MANIFEST_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.documents.implementation_manifest)
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "git_required_path_missing"):
            self.verify({**self.execution, "execution_commit": commit, "execution_tree": tree})


if __name__ == "__main__":
    unittest.main()
