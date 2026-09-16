"""New offline v2 selector/lineage tests; manifests exist only in temporary fixtures."""
import copy
import json
from pathlib import Path
import shutil
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from researchops_internal_telemetry import source_integrity_v2 as v2

ROOT = Path(__file__).resolve().parents[1]


class OfflineSourceV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Read-only fixed Git verification, not a current-tree manifest.
        cls.lineage = v2.verify_lineage(ROOT)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="offline-source-v2-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in (*v2.EXPLICIT, v2.RECIPE, v2.HISTORICAL_MANIFEST):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        sample = self.root / "src/sample.py"
        sample.write_text("VALUE = 1\n", encoding="utf-8")
        # Use actual fixed repository objects with the temporary current file set.
        original = v2._git
        self.git_calls = []
        def git_for_fixture(root, *args, **kwargs):
            self.assertEqual(Path(root), self.root)
            self.git_calls.append(args)
            return original(ROOT, *args, **kwargs)
        git_patcher = patch.object(v2, "_git", side_effect=git_for_fixture)
        self.addCleanup(git_patcher.stop)
        git_patcher.start()

    def build(self):
        return v2.build_manifest(self.root)

    def save(self):
        return v2.write_manifest_exclusive(self.root)

    def rewrite(self, name, value):
        (self.root / name).write_bytes(v2.canonical(value) + b"\n")

    def test_fixed_lineage_recomputed_from_361_real_git_blobs(self):
        self.assertEqual(self.lineage["verified_git_blob_count"], 361)
        self.assertEqual(self.lineage["source_commitment_sha256"], "02ef0f6b776e896f09704608867bdf1d91a7bd6fc09444c57bee6cad0d4cb17c")

    def test_fixture_cleanup_does_not_stop_externally_owned_patch(self):
        sentinel = b"unrelated-test-owned-domain"
        external = patch.object(v2, "DOMAIN", sentinel)
        external.start()
        try:
            nested = OfflineSourceV2Tests("test_distinct_v2_domain")
            try:
                nested.setUp()
            finally:
                nested.doCleanups()
            self.assertIs(v2.DOMAIN, sentinel)
        finally:
            external.stop()

    def test_temporary_fixture_valid_and_no_runtime_authority(self):
        self.save()
        manifest = v2.verify_source(self.root)
        self.assertEqual(manifest["algorithm"], v2.ALGORITHM)
        self.assertTrue(manifest["source_integrity_only"])
        for flag in ("online_execution_authorized", "runtime_admission_verified", "historical_result_revalidated"):
            self.assertIs(manifest[flag], False)
        self.assertTrue(any(args[:2] == ("cat-file", "--batch") for args in self.git_calls))

    def test_original_manifest_and_normative_markdown_are_selected_self_is_not(self):
        names = v2.source_files(self.root)
        for name in (v2.HISTORICAL_MANIFEST, v2.SCHEMA, v2.RECIPE, *v2.EXPLICIT):
            self.assertIn(name, names)
        self.assertNotIn(v2.MANIFEST, names)

    def test_manifest_create_once_does_not_overwrite(self):
        self.save()
        previous = (self.root / v2.MANIFEST).read_bytes()
        with self.assertRaisesRegex(v2.SourceIntegrityV2Error, "manifest_exists"):
            self.save()
        self.assertEqual((self.root / v2.MANIFEST).read_bytes(), previous)

    def test_normative_markdown_drift(self):
        self.save()
        path = self.root / "evals/internal_behavior_eval_v1/CONTRACT.md"
        path.write_bytes(path.read_bytes() + b"\nchanged\n")
        with self.assertRaisesRegex(v2.SourceIntegrityV2Error, "source_drift"):
            v2.verify_source(self.root)

    def test_selected_file_deletion_detected(self):
        self.save()
        (self.root / "src/sample.py").unlink()
        with self.assertRaisesRegex(v2.SourceIntegrityV2Error, "source_drift"):
            v2.verify_source(self.root)

    def test_schema_missing_is_not_silently_omitted(self):
        (self.root / v2.SCHEMA).unlink()
        with self.assertRaises(FileNotFoundError):
            self.build()

    def test_validator_missing_is_not_silently_omitted(self):
        (self.root / "src/researchops_internal_telemetry/source_integrity_v2.py").unlink()
        with self.assertRaises(FileNotFoundError):
            self.build()

    def test_historical_non_hash_field_tamper_rejected(self):
        doc = json.loads((self.root / v2.HISTORICAL_MANIFEST).read_bytes())
        doc["historical_commit"] = "0" * 40
        self.rewrite(v2.HISTORICAL_MANIFEST, doc)
        with self.assertRaisesRegex(v2.SourceIntegrityV2Error, "historical_manifest_local_drift"):
            self.build()

    def test_missing_historical_object_fail_closed_with_injection_hit(self):
        original = v2._git
        hits = []
        def missing(root, *args, **kwargs):
            if args[:2] == ("cat-file", "--batch"):
                hits.append(args)
                return b"fixed-object missing\n"
            return original(root, *args, **kwargs)
        with patch.object(v2, "_git", side_effect=missing):
            with self.assertRaisesRegex(v2.SourceIntegrityV2Error, "historical_blob_missing"):
                self.build()
        self.assertEqual(len(hits), 1)

    def test_historical_blob_changed_not_only_self_reported_digest(self):
        original = v2._blobs
        hits = []
        def tamper(root, names):
            data = original(root, names)
            if len(names) > 1:
                hits.append(len(names))
                data[0] += b"changed"
            return data
        with patch.object(v2, "_blobs", side_effect=tamper):
            with self.assertRaisesRegex(v2.SourceIntegrityV2Error, "historical_source_bytes"):
                self.build()
        self.assertEqual(hits, [361])

    def test_unknown_algorithm_rejected(self):
        recipe = v2.expected_recipe()
        recipe["algorithm"] = "unknown"
        self.rewrite(v2.RECIPE, recipe)
        with self.assertRaisesRegex(v2.SourceIntegrityV2Error, "recipe"):
            self.build()

    def test_recipe_false_does_not_accept_integer_zero(self):
        recipe = v2.expected_recipe()
        recipe["online_execution_authorized"] = 0
        self.rewrite(v2.RECIPE, recipe)
        with self.assertRaisesRegex(v2.SourceIntegrityV2Error, "recipe"):
            self.build()

    def test_saved_false_does_not_accept_integer_zero(self):
        self.save()
        saved = json.loads((self.root / v2.MANIFEST).read_bytes())
        saved["online_execution_authorized"] = 0
        self.rewrite(v2.MANIFEST, saved)
        with self.assertRaisesRegex(v2.SourceIntegrityV2Error, "manifest_schema"):
            v2.verify_source(self.root)

    def test_schema_references_rejected_before_interpretation(self):
        schema = json.loads((self.root / v2.SCHEMA).read_bytes())
        schema["$ref"] = "https://invalid.example/schema"
        self.rewrite(v2.SCHEMA, schema)
        with self.assertRaisesRegex(v2.SourceIntegrityV2Error, "schema_reference"):
            self.build()

    def test_explicit_path_traversal_and_noncanonical_paths_rejected(self):
        for name in ("../secret", "/secret", "C:/secret", "src/../secret", "src//sample.py", "src\\sample.py", "src/./sample.py"):
            with self.subTest(name=name), self.assertRaises(v2.SourceIntegrityV2Error):
                v2._relative(name)

    def test_symlink_gate_injection_hits_real_selected_path(self):
        original = Path.lstat
        hits = []
        target = self.root / "requirements.lock"
        def lstat(path):
            if path == target:
                hits.append(path.name)
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
            return original(path)
        with patch.object(Path, "lstat", lstat):
            with self.assertRaisesRegex(v2.SourceIntegrityV2Error, "link"):
                self.build()
        self.assertEqual(hits, ["requirements.lock"])

    def test_distinct_v2_domain(self):
        manifest = self.build()
        body = {key: value for key, value in manifest.items() if key != "commitment_sha256"}
        self.assertEqual(manifest["commitment_sha256"], v2.digest(v2.DOMAIN + v2.canonical(body)))
        self.assertNotEqual(manifest["commitment_sha256"], v2.digest(b"researchops.internal.v1\0source\0" + v2.canonical(body)))

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaisesRegex(v2.SourceIntegrityV2Error, "duplicate_key"):
            v2.decode(b'{"x":1,"x":2}')

    def test_original_online_source_admission_run_bytes_unchanged(self):
        historical = json.loads((ROOT / v2.HISTORICAL_MANIFEST).read_bytes())
        hashes = {row["path"]: row["sha256"] for row in historical["files"]}
        for relative in ("src/researchops_internal_telemetry/source.py", "src/researchops_internal_telemetry/admission.py", "src/researchops_internal_telemetry/run.py"):
            self.assertEqual(v2.digest((ROOT / relative).read_bytes()), hashes[relative])

    def test_directory_enumeration_limit(self):
        reduced = dict(v2.LIMITS, directories=1)
        with patch.object(v2, "LIMITS", reduced):
            self.rewrite(v2.RECIPE, v2.expected_recipe())
            with self.assertRaisesRegex(v2.SourceIntegrityV2Error, "enumeration_limit"):
                v2.source_files(self.root)

    def test_entry_enumeration_limit(self):
        reduced = dict(v2.LIMITS, entries=1)
        with patch.object(v2, "LIMITS", reduced):
            self.rewrite(v2.RECIPE, v2.expected_recipe())
            with self.assertRaisesRegex(v2.SourceIntegrityV2Error, "enumeration_limit"):
                v2.source_files(self.root)

    def test_total_source_byte_limit(self):
        reduced = dict(v2.LIMITS, total_bytes=100)
        with patch.object(v2, "LIMITS", reduced):
            self.rewrite(v2.RECIPE, v2.expected_recipe())
            # Isolate the current-source budget branch; real lineage is verified separately.
            with patch.object(v2, "verify_lineage", return_value=self.lineage) as lineage:
                with self.assertRaisesRegex(v2.SourceIntegrityV2Error, "source_byte_limit"):
                    self.build()
                lineage.assert_called_once_with(self.root)


class CurrentOfflineSourceV2Tests(unittest.TestCase):
    def test_current_manifest_matches_generated_anchor_without_runtime_authority(self):
        current = v2.verify_source(ROOT, expected="0157e921793e0afe5cd897456535c324ccede9b203eb6792375f1d9b7d67e66d")
        self.assertEqual(v2.digest((ROOT / v2.MANIFEST).read_bytes()), "072c1a542427e3282bb3072b7db662dfda43fd0a1e8f062a6a1dd9e545f7750e")
        self.assertEqual(current["algorithm"], v2.ALGORITHM)
        self.assertIs(current["source_integrity_only"], True)
        for name in ("online_execution_authorized", "runtime_admission_verified", "historical_result_revalidated"):
            self.assertIs(current[name], False)
        self.assertEqual(current["lineage"]["verified_git_blob_count"], 361)

    def test_online_v1_still_rejects_current_tree_and_historical_bytes_are_preserved(self):
        from researchops_internal_telemetry import source
        from researchops_internal_telemetry.contract import InternalError
        with self.assertRaisesRegex(InternalError, "^internal_source_drift$"):
            source.verify_source(ROOT)
        self.assertEqual(v2.digest((ROOT / v2.HISTORICAL_MANIFEST).read_bytes()), v2.HISTORICAL_MANIFEST_SHA256)
