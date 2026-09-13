"""Current-source checks on test-owned marked trees, not real source generation."""
from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from researchops_external_closure import execution_current_v3 as current
from researchops_external_closure import execution_components_v3 as source
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from tests import execution_v3_fixture as fixtures


class CurrentTimedSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "tree"; self.root.mkdir()
        # Current-reader behavior on an explicit historical v3-sized test tree.
        self.files, self.paths = fixtures.archived_first_live_files()
        self.first = source.build_profile_documents(self.files, available_paths=self.paths, profile="first_live")
        self.write_files(self.files | dict(zip(source.PROFILE_PATHS["first_live"], (self.first.manifest, self.first.plan))))

    def write_files(self, files):
        for name, payload in files.items():
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)

    def test_existing_marked_profile_verifies_without_git_provider_or_mutation(self):
        before = {name: (self.root / name).read_bytes() for name in self.files}
        with patch("subprocess.Popen", side_effect=AssertionError("no historical fallback")), \
             patch("socket.socket", side_effect=AssertionError("no Provider")):
            checked = current.verify_current_timed_profile(self.root)
        self.assertEqual(checked.profile, "first_live")
        self.assertFalse(checked.online_execution_authorized)
        self.assertFalse(checked.runtime_admission_verified)
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})

    def test_missing_profile_is_not_generated_or_recovered(self):
        path = self.root / source.PROFILE_PATHS["first_live"][1]
        path.unlink()  # Test-owned temporary profile only.
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "current_file_unavailable"):
            current.verify_current_timed_profile(self.root)
        self.assertFalse(path.exists())

    def test_changed_python_and_new_contract_directory_both_move_coverage(self):
        path = self.root / "src/researchops/__init__.py"
        original = path.read_bytes(); path.write_bytes(original + b"\n# current drift\n")
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_v3_component_drift"):
            current.verify_current_timed_profile(self.root)
        path.write_bytes(original)
        extra = self.root / "evals/provider_completion_future_v1/added.json"
        extra.parent.mkdir(); extra.write_bytes(b"{}")
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_v3_component_drift"):
            current.verify_current_timed_profile(self.root)

    def test_missing_required_new_schema_does_not_shrink_the_inventory(self):
        path = self.root / "evals/provider_completion_first_live_review_v2/review_v2.schema.json"
        path.unlink()
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "current_file_unavailable"):
            current.verify_current_timed_profile(self.root)

    def test_campaign_reads_the_same_first_live_snapshot_and_late_registry_bytes(self):
        files, paths = fixtures.campaign_files(self.files, self.paths, self.first)
        campaign = source.build_profile_documents(files, available_paths=paths, profile="campaign")
        self.write_files(files | dict(zip(source.PROFILE_PATHS["campaign"], (campaign.manifest, campaign.plan))))
        checked = current.verify_current_timed_profile(self.root, profile="campaign")
        self.assertEqual(checked.profile, "campaign")
        self.assertFalse(checked.runtime_admission_verified)

    def test_selected_file_change_after_pure_validation_is_detected(self):
        original = source.verify_profile_documents
        path = self.root / "src/researchops/__init__.py"
        def verify(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_bytes(path.read_bytes() + b"\n# late change\n")
            return result
        with patch.object(source, "verify_profile_documents", side_effect=verify):
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_current_v3_tree_changed"):
                current.verify_current_timed_profile(self.root)

    def test_own_manifest_change_after_validation_is_detected(self):
        original = source.verify_profile_documents
        path = self.root / source.PROFILE_PATHS["first_live"][0]
        def verify(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_bytes(path.read_bytes() + b" ")
            return result
        with patch.object(source, "verify_profile_documents", side_effect=verify):
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_current_v3_tree_changed"):
                current.verify_current_timed_profile(self.root)

    def test_provider_directory_added_after_enumeration_is_detected(self):
        original = source.verify_profile_documents
        def verify(*args, **kwargs):
            result = original(*args, **kwargs)
            path = self.root / "evals/provider_completion_late_v1/new.json"
            path.parent.mkdir(); path.write_bytes(b"{}")
            return result
        with patch.object(source, "verify_profile_documents", side_effect=verify):
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_current_v3_tree_changed"):
                current.verify_current_timed_profile(self.root)

    def test_directory_enumeration_is_bounded_before_materialization(self):
        original = current.os.scandir
        class Entry:
            name = "unrelated"
        class Entries:
            consumed = 0
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def __iter__(self): return self
            def __next__(self):
                self.consumed += 1
                if self.consumed > 16385:
                    raise AssertionError("enumeration exceeded bound")
                return Entry()
        entries = Entries()
        def scan(path):
            return entries if Path(path) == self.root / "evals" else original(path)
        with patch.object(current.os, "scandir", side_effect=scan):
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_current_v3_inventory_limit"):
                current.verify_current_timed_profile(self.root)
        self.assertEqual(entries.consumed, 16385)

    def test_added_contract_is_detected_even_if_parent_mtime_is_restored(self):
        original = source.verify_profile_documents
        evals = self.root / "evals"
        def verify(*args, **kwargs):
            result = original(*args, **kwargs)
            info = evals.stat()
            path = evals / "provider_completion_hidden_change_v1/new.json"
            path.parent.mkdir(); path.write_bytes(b"{}")
            os.utime(evals, ns=(info.st_atime_ns, info.st_mtime_ns))
            return result
        with patch.object(source, "verify_profile_documents", side_effect=verify):
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_current_v3_tree_changed"):
                current.verify_current_timed_profile(self.root)

    def test_hardlinked_selected_file_is_rejected(self):
        path = self.root / "src/researchops/__init__.py"
        os.link(path, Path(self.temporary.name) / "alias.py")
        with self.assertRaises(ExternalClosurePrimitiveError):
            current.verify_current_timed_profile(self.root)

    def test_root_symlink_is_not_resolved_into_an_implicitly_trusted_root(self):
        link = Path(self.temporary.name) / "root-alias"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege unavailable")
            raise
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_current_v3_path_invalid"):
            current.verify_current_timed_profile(link)

    def test_reparse_metadata_is_rejected_without_needing_symlink_privilege(self):
        original = Path.lstat
        def metadata(path):
            info = original(path)
            if path == self.root:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, "lstat", metadata):
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, "execution_current_v3_path_invalid"):
                current.verify_current_timed_profile(self.root)


if __name__ == "__main__":
    unittest.main()
