"""Small offline regressions for the auxiliary readback, not new live evidence."""
from __future__ import annotations

import ast
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from researchops.audit import AuditLedger

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/internal30_readonly_evidence.py"
spec = importlib.util.spec_from_file_location("internal30_readonly_evidence", SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class ImmutableAuxiliaryReadbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="internal-readback-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        donor = self.base / "donor.sqlite3"
        ledger = AuditLedger(donor)
        ledger.start_run(mode="test", request_summary={"scope": "synthetic"})
        self.archive = self.base / "archive"
        self.archive.mkdir()
        self.db = self.archive / "audit.sqlite3"
        # Freeze a checkpointed WAL-mode database with no required WAL content.
        # The historical bug is specifically opening this shape without immutable.
        with contextlib.closing(sqlite3.connect(donor)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            shutil.copyfile(donor, self.db)
        self.assertEqual(self.db.read_bytes()[18:20], b"\x02\x02")

    def test_real_immutable_reader_adds_no_wal_or_shm(self):
        before = helper.snapshot(self.archive, ("audit.sqlite3",))
        result = helper.immutable_audit_summary(self.archive, expected_names=("audit.sqlite3",))
        self.assertEqual(result["event_count"], 1)
        self.assertEqual(result["event_counts"], {"run_started": 1})
        self.assertEqual(helper.snapshot(self.archive, ("audit.sqlite3",)), before)
        self.assertEqual(sorted(p.name for p in self.archive.iterdir()), ["audit.sqlite3"])

    def test_legacy_read_only_without_immutable_reproduces_sidecars_in_temp_only(self):
        connection = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 1)
            names = {p.name for p in self.archive.iterdir()}
            self.assertIn("audit.sqlite3-shm", names)
            self.assertIn("audit.sqlite3-wal", names)
        finally:
            connection.close()

    def test_file_injection_after_actual_reader_is_detected_and_preserved(self):
        actual = helper._read_database_rows
        hits = []
        def injected(path, *, expected_payload):
            result = actual(path, expected_payload=expected_payload)
            hits.append(path)
            (self.archive / "audit.sqlite3-wal").write_bytes(b"")
            return result
        with patch.object(helper, "_read_database_rows", side_effect=injected):
            with self.assertRaisesRegex(Exception, "directory_file_set_invalid"):
                helper.immutable_audit_summary(self.archive, expected_names=("audit.sqlite3",))
        self.assertEqual(hits, [self.db])
        self.assertTrue((self.archive / "audit.sqlite3-wal").exists())

    def test_existing_sidecar_rejected_before_sqlite_reader(self):
        (self.archive / "audit.sqlite3-wal").write_bytes(b"")
        with patch.object(helper, "_read_database_rows", side_effect=AssertionError("must not enter")) as reader:
            with self.assertRaisesRegex(Exception, "directory_file_set_invalid"):
                helper.immutable_audit_summary(self.archive, expected_names=("audit.sqlite3",))
        self.assertEqual(reader.call_count, 0)

    def test_same_bytes_but_mtime_mutation_is_detected(self):
        import os
        actual = helper._read_database_rows
        hits = []
        def injected(path, *, expected_payload):
            result = actual(path, expected_payload=expected_payload)
            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
            hits.append(path)
            return result
        with patch.object(helper, "_read_database_rows", side_effect=injected):
            with self.assertRaisesRegex(Exception, "auxiliary_read_changed_identity"):
                helper.immutable_audit_summary(self.archive, expected_names=("audit.sqlite3",))
        self.assertEqual(hits, [self.db])

    def test_output_must_be_new_and_outside_both_input_archives(self):
        second = self.base / "second"; second.mkdir()
        for target in (self.archive, self.archive / "nested", second / "nested", self.db):
            with self.subTest(target=target.name), self.assertRaises(Exception):
                helper.validate_output_location(target, self.archive, second)
        existing = self.base / "existing"; existing.mkdir()
        with self.assertRaisesRegex(Exception, "output_exists"):
            helper.validate_output_location(existing, self.archive, second)
        result = helper.validate_output_location(self.base / "new", self.archive, second)
        self.assertEqual(result, self.base / "new")
        self.assertFalse(result.exists())

    def test_wrong_expected_database_bytes_are_rejected(self):
        with self.assertRaisesRegex(Exception, "bundle_hash_mismatch"):
            helper.immutable_audit_counts(self.db, b"not the database")

    def test_audit_chain_tamper_is_rejected(self):
        with contextlib.closing(sqlite3.connect(self.db)) as connection:
            connection.execute("DROP TRIGGER audit_events_no_update")
            connection.execute("UPDATE audit_events SET safe_payload_json='{}'")
            connection.commit()
        with self.assertRaisesRegex(Exception, "audit_chain"):
            helper.immutable_audit_summary(self.archive, expected_names=("audit.sqlite3",))

    def test_helper_has_no_direct_sqlite_or_runtime_call(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        forbidden = {"connect", "run_internal_v1", "prepare", "_reserve_with_ownership", "_load_key", "provision_local_claim_store"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                self.assertNotIn(name, forbidden)

    def test_cli_rejection_does_not_echo_exception_secret_or_path(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(helper, "verify_public_pack",
                side_effect=RuntimeError("synthetic private text must not appear")):
            code = helper.main(["--verify", str(self.base)])
        self.assertEqual(code, 2)
        self.assertNotIn("synthetic private", output.getvalue())
        self.assertNotIn(str(self.base), output.getvalue())


class PublicProjectionTests(unittest.TestCase):
    def value(self):
        rows = [dict(request_index=i, response_index=i, native_status="completed", normalized_state="completed",
            signal_source="native_status", http_status=200, input_tokens=1, output_tokens=2, reasoning_tokens=1,
            cached_input_tokens=0, cache_write_tokens=None, usage_complete=True) for i in range(30)]
        return dict(schema_version="internal30-public-observation/1.0", scope="developer_known_synthetic",
            provider="deepseek", requested_model="deepseek-v4-flash", live_transport="native_http_transport",
            planned_cases=30, executed_cases=30, runtime_accepted_cases=30, model_requests=30, network_attempts=30,
            failed_or_unknown_cases=0, unexecuted_cases=0, input_tokens=30, output_tokens=60,
            local_observed_cost_cny="0.000540", provider_bill_cny=None,
            cost_rates_cny_per_million=dict(input="2.000000", output="8.000000", cache_discount_assumed=False),
            phase_terminal_ns=1000000, publication_sealing_elapsed_ns=1000,
            sealing_scope="before_publication_write_not_process_exit",
            audit=dict(event_count=122, event_counts=dict(run_started=1, model_request_started=30, internal_send_intent=30,
                model_response_telemetry_recorded=30, internal_case_closed=30, run_status_changed=1),
                model_body_rows=0, tool_calls=0, tool_attempts=0, approval_decisions=0),
            responses=rows, original_directory_unmodified_claim_allowed=False, reconstructed_copy_accepted=True,
            external_validation_completed=False, status_t7_closed=False, model_quality_claim_allowed=False, depth60_result="20/60")

    def test_valid_synthetic_projection_not_live_evidence(self):
        helper.validate_public_observation(self.value())

    def test_numeric_state_identity_and_scope_tampering_rejected(self):
        mutations = [lambda v: v.update(input_tokens=0), lambda v: v.update(status_t7_closed=True),
            lambda v: v["responses"][0].update(normalized_state="unmapped"),
            lambda v: v["responses"][0].update(request_index=False),
            lambda v: v["responses"][0].update(input_tokens=True),
            lambda v: v["responses"].pop(), lambda v: v["audit"].update(event_count=0),
            lambda v: v.update(authorization_id="must-not-be-published")]
        for i, mutate in enumerate(mutations):
            with self.subTest(mutation=i):
                value = self.value(); mutate(value)
                with self.assertRaises(Exception): helper.validate_public_observation(value)

    def test_privacy_canaries_rejected(self):
        for marker in ("sk-FAKE-PRIVACY-12345678", "Authorization: Bearer fake", "C:/private/example.txt",
                       "Traceback (most recent call last):", "synthetic@example.invalid"):
            with self.subTest(marker_type=marker.split()[0]):
                value = self.value(); value["scope"] = marker
                with self.assertRaisesRegex(Exception, "sensitive_content_detected"):
                    helper.validate_public_observation(value)

    def test_public_hash_tamper_and_output_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary); value = self.value()
            original = helper.c.raw(value)
            commitments = dict(public_observation_sha256=helper.c.digest(original), helper_script_sha256=helper.c.digest(SCRIPT.read_bytes()),
                incident=dict(original_directory_still_rejected=True, incident_retracted=False), reconstruction=dict(copy_files=12))
            (folder / "public_observation.json").write_bytes(original)
            (folder / "artifact_commitments.json").write_bytes(helper.c.raw(commitments))
            self.assertFalse(helper.verify_public_pack(folder)["full_archive_reverified"])
            value["input_tokens"] = 99
            (folder / "public_observation.json").write_bytes(helper.c.raw(value))
            with self.assertRaisesRegex(Exception, "public_hash"):
                helper.verify_public_pack(folder)


if __name__ == "__main__":
    unittest.main()
