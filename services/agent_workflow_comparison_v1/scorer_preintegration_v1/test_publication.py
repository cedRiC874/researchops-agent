"""Publication portability and fail-closed identity tests, not scorer changes."""
from copy import deepcopy
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from . import bootstrap, identity, prepare_ci, verify


class PublicationTests(unittest.TestCase):
    def test_each_required_fixed_path_is_mandatory_before_git_or_import(self):
        original = identity.read_upstream()
        for name in identity.REQUIRED_FIXED_FILES:
            data = deepcopy(original)
            del data["fixed_files_sha256"][name]
            with self.subTest(path=name), patch.object(identity, "read_upstream", return_value=data), patch.object(
                identity, "git", side_effect=AssertionError("invalid list must stop before Git")
            ):
                with self.assertRaisesRegex(identity.IdentityError, "required_fixed_file_set_mismatch"):
                    identity.prepare()

    def test_empty_extra_or_bad_hash_manifest_is_rejected(self):
        data = identity.read_upstream()["fixed_files_sha256"]
        for invalid in ({}, {**data, "extra": "0" * 64}, {**data, "requirements.lock": "not-a-hash"}):
            with self.assertRaises(identity.IdentityError):
                identity.validate_fixed_files(invalid)

    def test_receipt_cannot_omit_fixed_paths(self):
        data = deepcopy(bootstrap.IDENTITY)
        data["files_sha256"] = {}
        with self.assertRaises(identity.IdentityError):
            identity.check_files(data)

    def test_fixed_target_head_guard_remains(self):
        with patch.object(identity, "git", return_value=b"055d2f2a875ab50554c7233fad19734e021c0a2a\n"):
            with self.assertRaisesRegex(identity.IdentityError, "fixed_head_or_tree_mismatch"):
                identity.prepare()

    def test_portable_preservation_does_not_read_original_workstation(self):
        original_open = Path.open
        touched = []
        def guarded(path, *args, **kwargs):
            touched.append(path.resolve())
            if not path.resolve().is_relative_to(identity.REPO):
                raise AssertionError("external original tree read")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded):
            result = verify.preserved()
        self.assertEqual(result["historical_origin_check"], "not_performed_in_this_context")
        self.assertNotIn("original_files_unchanged", result)
        self.assertTrue(result["carried_files_unchanged"])
        self.assertTrue(touched)

    def test_public_manifest_has_required_files_and_safe_paths(self):
        data = prepare_ci.read_json(identity.REPO / prepare_ci.MANIFEST)
        prepare_ci.validate_manifest(data)
        for path in ("../escape.py", "services/agent_workflow_comparison_v1/../escape.py", "src/scorer.py"):
            bad = deepcopy(data)
            bad["files"][path] = "0" * 64
            with self.assertRaises(prepare_ci.PreparationError):
                prepare_ci.validate_manifest(bad)
        bad = deepcopy(data)
        del bad["files"][prepare_ci.DIRECTORY + "identity.py"]
        with self.assertRaises(prepare_ci.PreparationError):
            prepare_ci.validate_manifest(bad)

    def test_source_snapshot_verifies_all_allowlisted_bytes(self):
        manifest, buffers = prepare_ci.snapshot(identity.REPO)
        self.assertEqual(set(buffers), set(manifest["files"]) | {prepare_ci.MANIFEST})
        self.assertTrue(all(prepare_ci.sha(buffers[n]) == h for n, h in manifest["files"].items()))

    def test_manifest_duplicate_keys_and_wrong_anchor_rejected(self):
        with self.assertRaises(prepare_ci.PreparationError):
            prepare_ci.decode_json('{"a":1,"a":2}')
        data = prepare_ci.read_json(identity.REPO / prepare_ci.MANIFEST)
        data["scorer_commit"] = "055d2f2a875ab50554c7233fad19734e021c0a2a"
        with self.assertRaises(prepare_ci.PreparationError):
            prepare_ci.validate_manifest(data)

    def test_public_files_exclude_machine_paths_and_credential_patterns(self):
        manifest = prepare_ci.read_json(identity.REPO / prepare_ci.MANIFEST)
        patterns = [r"[A-Za-z]:[/\\]+Users[/\\]+", r"\.codex[/\\]+(?:attachments|sessions|visualizations)",
                    r"sk-[A-Za-z0-9_-]{16,}", r"gh[pousr]_[A-Za-z0-9]{20,}"]
        for name in manifest["files"]:
            text = (identity.REPO / name).read_text(encoding="utf-8")
            with self.subTest(path=name):
                self.assertFalse(any(re.search(pattern, text) for pattern in patterns))

    def test_raw_scoring_reports_and_inputs_are_not_redacted(self):
        provenance = json.loads((identity.ROOT / "public-provenance.json").read_text(encoding="utf-8"))
        for row in provenance["derivations"]:
            if row["path"].split("/")[-1] in {"plan-agent.json", "plan-fixed_workflow.json", "raw-agent.json", "raw-fixed_workflow.json", "fault-validation.json"}:
                self.assertEqual(row["operation"], "byte_copy")
                self.assertEqual(prepare_ci.sha((identity.REPO / row["path"]).read_bytes()), row["source_sha256"])

    def test_history_summary_not_claimed_as_current_repair_validation(self):
        data = json.loads((identity.ROOT / "outputs/validation-history.public.json").read_text(encoding="utf-8"))
        self.assertEqual(data["tests"], 48)
        self.assertIn("not validation of current publication code", data["scope"])
        self.assertNotIn("installed_versions", data)
