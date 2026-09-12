"""Actual HEAD/current/historical checks in an isolated marked test repository."""
from __future__ import annotations

import json
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_external_closure import execution_local_v3 as local
from researchops_external_closure import execution_components_v3 as source
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from tests import execution_v3_fixture as fixtures
from tests.test_external_closure_git_objects import Repository
from tests.test_external_closure_execution_binding import write_file_tree


class LocalTimedExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temporary.cleanup)
        cls.repository = Repository(Path(cls.temporary.name) / "repo")
        # Preserve the v3 bound; v4 has separate current-tree coverage tests.
        cls.files, cls.paths = fixtures.archived_first_live_files()
        cls.documents = source.build_profile_documents(cls.files, available_paths=cls.paths, profile="first_live")
        manifest, plan = source.PROFILE_PATHS["first_live"]
        cls.all_files = cls.files | {manifest: cls.documents.manifest, plan: cls.documents.plan}
        for name, payload in cls.all_files.items():
            path = cls.repository.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
        cls.tree = write_file_tree(cls.repository, cls.all_files)
        cls.commit = cls.repository.commit(cls.tree)
        cls.repository.git("update-ref", "HEAD", cls.commit)  # Test-owned repo only.
        (cls.repository.root / "unrelated-user-note.txt").write_bytes(b"preserved test note")
        cls.expected = dict(profile="first_live", expected_commit=cls.commit, expected_tree=cls.tree,
            expected_source_integrity_commitment_sha256=json.loads(cls.documents.plan)["plan_commitment_sha256"],
            expected_source_manifest_commitment_sha256=json.loads(cls.documents.manifest)["commitment_sha256"])
        cls.checked = local.verify_local_timed_execution_identity(cls.repository.root, **cls.expected)

    def test_current_source_and_fixed_git_match_without_global_clean_or_runtime_claims(self):
        self.assertTrue(self.checked.current_and_historical_source_matched)
        self.assertTrue(self.checked.head_matched_at_both_observations)
        self.assertEqual(self.checked.execution_tree, self.tree)
        for name in ("repository_globally_clean_verified", "installed_environment_verified", "fresh_authorization_verified",
                     "winning_claim_verified", "runtime_authority_granted", "closure_claim_allowed"):
            self.assertIs(getattr(self.checked, name), False, name)
        self.assertEqual((self.repository.root / "unrelated-user-note.txt").read_bytes(), b"preserved test note")

    def test_invalid_expected_values_reject_before_head_lookup(self):
        with patch.object(local, "_head", side_effect=AssertionError("must not read Git")):
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_local_v3_expectation_invalid"):
                local.verify_local_timed_execution_identity(self.repository.root, **(self.expected | {"expected_commit": "HEAD"}))

    def test_wrong_head_rejects_before_historical_source_work(self):
        with patch.object(local, "verify_historical_timed_profile", side_effect=AssertionError("must not read history")):
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_local_v3_head_mismatch"):
                local.verify_local_timed_execution_identity(self.repository.root, **(self.expected | {"expected_commit": "f" * 40}))

    def test_head_command_is_fixed_bounded_and_credential_free(self):
        original = subprocess.Popen
        calls = []
        def popen(command, **kwargs):
            calls.append((command, kwargs))
            return original(command, **kwargs)
        with patch.object(local.subprocess, "Popen", side_effect=popen):
            self.assertEqual(local._head(self.repository.root), self.commit)
        self.assertEqual(len(calls), 1)
        command, kwargs = calls[0]
        self.assertEqual(command[-3:], ["rev-parse", "--verify", "HEAD"])
        self.assertIn("--no-replace-objects", command)
        self.assertIn("protocol.allow=never", command)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["env"]["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertNotIn("DEEPSEEK_API_KEY", kwargs["env"])
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertFalse(any("safe.directory" in item for item in command))

    def test_same_tree_head_change_after_current_check_is_rejected(self):
        other = self.repository.commit(self.tree, self.commit)
        original = local.verify_current_timed_profile
        def verify(*args, **kwargs):
            result = original(*args, **kwargs)
            self.repository.git("update-ref", "HEAD", other)
            return result
        try:
            with patch.object(local, "verify_current_timed_profile", side_effect=verify):
                with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_local_v3_head_changed"):
                    local.verify_local_timed_execution_identity(self.repository.root, **self.expected)
        finally:
            self.repository.git("update-ref", "HEAD", self.commit)

    def test_another_self_consistent_local_profile_cannot_replace_authorized_git(self):
        changed = dict(self.files)
        name = "src/researchops/__init__.py"
        changed[name] += b"\n# different current source, same HEAD\n"
        documents = source.build_profile_documents(changed, available_paths=self.paths, profile="first_live")
        manifest, plan = source.PROFILE_PATHS["first_live"]
        updates = {name: changed[name], manifest: documents.manifest, plan: documents.plan}
        before = {path: (self.repository.root / path).read_bytes() for path in updates}
        try:
            for path, payload in updates.items():
                (self.repository.root / path).write_bytes(payload)
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_local_v3_commitment_mismatch"):
                local.verify_local_timed_execution_identity(self.repository.root, **self.expected)
        finally:
            for path, payload in before.items():
                (self.repository.root / path).write_bytes(payload)


class HeadProcessBoundaryTests(unittest.TestCase):
    class Process:
        def __init__(self, payload):
            self.stdout = io.BytesIO(payload)
            self.returncode = 0
            self.killed = False
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def wait(self, **kwargs): return self.returncode
        def poll(self): return self.returncode
        def kill(self):
            self.killed = True
            self.returncode = -9

    def test_oversized_head_output_is_bounded_and_killed(self):
        process = self.Process(b"a" * 10000)
        with tempfile.TemporaryDirectory() as temporary, patch.object(local.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_local_v3_head_unavailable"):
                local._head(Path(temporary))
        self.assertTrue(process.killed)
        self.assertEqual(process.stdout.tell(), 42)

    def test_expired_head_process_cannot_return_a_valid_looking_oid(self):
        process = self.Process(b"a" * 40 + b"\n")
        class ImmediateTimer:
            def __init__(self, seconds, callback): self.callback = callback
            def start(self): self.callback()
            def cancel(self): pass
        with tempfile.TemporaryDirectory() as temporary, patch.object(local.subprocess, "Popen", return_value=process), \
             patch.object(local.threading, "Timer", ImmediateTimer):
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_local_v3_head_timeout"):
                local._head(Path(temporary))
        self.assertTrue(process.killed)


if __name__ == "__main__":
    unittest.main()
