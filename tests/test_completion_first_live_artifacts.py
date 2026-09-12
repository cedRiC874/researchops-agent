"""Portable synthetic first-live archives backed by real temporary SQLite."""
from __future__ import annotations

import copy
import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import first_live, first_live_artifacts as artifacts, local_claim
from researchops_completion_timing.contract import TimingContractError, digest
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import test_completion_first_live_audit as audit_helpers
from tests import test_completion_first_live_control as control_helpers
from tests import test_completion_first_live_timing as timing_helpers


ROOT = Path(__file__).resolve().parents[1]


class FirstLiveArtifactTests(unittest.TestCase):
    def setUp(self):
        self.helper = audit_helpers.FirstLiveAuditTests()
        self.helper.setUp(); self.addCleanup(self.helper.doCleanups)
        self.directory = Path(self.helper.temporary.name) / "archive"
        self.directory.mkdir()
        self.helper.database = self.directory / "audit.sqlite3"
        self.helper.data = control_helpers.FirstLiveControlTests().controlled_fixture()
        self.helper.build_database()
        self.data = self.helper.data
        self.profile, _ = artifacts.load_first_live_artifact_contract(ROOT)
        self.reseal()

    def envelope(self):
        def entry(name):
            payload = (self.directory / name).read_bytes()
            return {"bytes": len(payload), "sha256": digest(payload)}
        self.bundle = dict(schema_version="provider-completion-first-live-bundle/1.0", **self.binding(),
            files={name: entry(name) for name in self.profile["artifact_files"]})
        self.bundle["bundle_commitment_sha256"] = artifacts.bundle_commitment(self.bundle)

    def binding(self):
        return dict(artifact_contract_sha256=artifacts.PROFILE_SHA256,
            authorization_binding_sha256=self.data["auth"]["authorization_binding_sha256"],
            plan_commitment_sha256=self.data["plan"]["plan_commitment_sha256"],
            execution_binding_sha256=self.data["evidence"]["execution_binding_sha256"])

    def reseal(self):
        timing_helpers.FirstLiveTimingTests().seal(self.data)
        values = {"authorization.json": self.data["auth"], "claim_intent.json": self.data["intent"],
            "claim_receipt.json": self.data["receipt"], "completion_timing_plan.json": self.data["plan"],
            "completion_timing_evidence.json": self.data["evidence"]}
        for name, value in values.items():
            (self.directory / name).write_bytes(raw(value))  # Test-owned fixture only.
        manifest = dict(schema_version="provider-completion-first-live-manifest/1.0", status="sealed_payloads", **self.binding(),
            files={name: {"bytes": len((self.directory / name).read_bytes()), "sha256": digest((self.directory / name).read_bytes())}
                   for name in self.profile["payload_files"]})
        self.manifest = manifest
        (self.directory / "manifest.json").write_bytes(raw(manifest))
        publication = self.data["publication"]
        publication["bundle_manifest_file_sha256"] = digest(raw(manifest))
        publication["timing_evidence_file_sha256"] = digest(raw(self.data["evidence"]))
        publication["publication_commitment_sha256"] = first_live.commitment("publication", publication)
        (self.directory / "completion_timing_publication.json").write_bytes(raw(publication))
        self.envelope()

    def verify(self, **overrides):
        values = dict(bundle_bytes=raw(self.bundle), expected_bundle_commitment_sha256=self.bundle["bundle_commitment_sha256"],
            expected_authorization_binding_sha256=self.data["auth"]["authorization_binding_sha256"],
            verification_time_utc=self.data["auth"]["authorized_at_utc"])
        values.update(overrides)
        with patch.object(local_claim, "_windows_local_app_data", side_effect=AssertionError("portable reader must not access store")), \
             patch.object(local_claim, "reserve_local_claim", side_effect=AssertionError("must not reserve")):
            return artifacts.verify_first_live_artifact_bundle(ROOT, self.directory, **values)

    def test_complete_eight_file_archive_has_actual_chain_but_no_execution_or_review_claim(self):
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        result = self.verify()
        self.assertEqual(result["file_count"], 8)
        self.assertEqual(result["record_count"], 2)
        self.assertTrue(result["authoritative_ledger_verified"])
        self.assertTrue(result["timing_gate_complete"])
        for name in ("local_store_entry_independently_verified", "source_admission_verified", "actual_clock_capture_proved",
                     "runtime_authority_granted", "first_live_success_verified", "current_closure_claim_allowed",
                     "review_signature_verified", "external_review_digest_verified", "first_live_campaign_source_equivalence_verified"):
            self.assertIs(result[name], False, name)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory.iterdir()})
        self.assertNotIn("terminal_projection_bytes", inspect.signature(artifacts.verify_first_live_artifact_bundle).parameters)

    def test_independent_envelope_and_authorization_digests_are_required(self):
        for name in ("expected_bundle_commitment_sha256", "expected_authorization_binding_sha256"):
            with self.subTest(name=name), self.assertRaisesRegex(TimingContractError, "first_live_bundle_commitment_mismatch"):
                self.verify(**{name: "f" * 64})

    def test_extra_sidecar_and_missing_file_are_rejected(self):
        sidecar = self.directory / "audit.sqlite3-wal"
        sidecar.write_bytes(b"synthetic")
        with self.assertRaisesRegex(TimingContractError, "first_live_artifact_invalid"):
            self.verify()
        sidecar.unlink()  # Test-owned file only.
        (self.directory / "claim_receipt.json").unlink()
        with self.assertRaisesRegex(TimingContractError, "first_live_artifact_invalid"):
            self.verify()

    def test_final_publication_is_bound_by_external_bundle_not_manifest_cycle(self):
        original = copy.deepcopy(self.bundle)
        self.data["publication"]["sealing_finished_ns"] += 1
        self.reseal()
        self.assertEqual(original["files"]["manifest.json"], self.bundle["files"]["manifest.json"])
        self.assertNotEqual(original["files"]["completion_timing_publication.json"], self.bundle["files"]["completion_timing_publication.json"])
        with self.assertRaisesRegex(TimingContractError, "first_live_bundle_commitment_mismatch"):
            self.verify(expected_bundle_commitment_sha256=original["bundle_commitment_sha256"])

    def test_rehashed_envelope_cannot_hide_stale_manifest(self):
        path = self.directory / "claim_receipt.json"
        changed = copy.deepcopy(self.data["receipt"]); changed["clock_domain_id"] = "PCECLOCK-" + "F" * 32
        path.write_bytes(raw(changed)); self.envelope()
        with self.assertRaisesRegex(TimingContractError, "first_live_manifest_payload_mismatch"):
            self.verify()

    def test_rehashed_manifest_cannot_change_execution_binding(self):
        self.manifest["execution_binding_sha256"] = "f" * 64
        (self.directory / "manifest.json").write_bytes(raw(self.manifest)); self.envelope()
        with self.assertRaisesRegex(TimingContractError, "first_live_manifest_binding_mismatch"):
            self.verify()

    def test_rehashed_archive_cannot_hide_timing_mismatch_with_actual_rows(self):
        self.data["evidence"]["segments"][0]["send_started_ns"] += 1
        self.reseal()
        with self.assertRaisesRegex(TimingContractError, "first_live_audit_segment_mismatch"):
            self.verify()

    def test_privacy_scan_runs_before_open_even_if_envelope_is_stale(self):
        path = self.directory / "authorization.json"
        path.write_bytes(path.read_bytes() + b"Authorization: Bearer FAKECANARY")
        with patch.object(artifacts.audit, "_extract", side_effect=AssertionError("must not open SQLite")):
            with self.assertRaisesRegex(TimingContractError, "timing_sensitive_content"):
                self.verify()

    def test_noncanonical_json_is_not_accepted_after_all_hashes_are_updated(self):
        path = self.directory / "claim_intent.json"
        path.write_bytes(path.read_bytes() + b" ")
        self.envelope()
        with self.assertRaisesRegex(TimingContractError, "timing_artifact_noncanonical_json"):
            self.verify()

    def test_late_file_mutation_is_caught_by_final_exact_snapshot(self):
        original = artifacts.read_exact_artifact_directory
        calls = []
        def read(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                path = self.directory / "authorization.json"
                path.write_bytes(path.read_bytes() + b" ")
            return original(*args, **kwargs)
        with patch.object(artifacts, "read_exact_artifact_directory", side_effect=read):
            with self.assertRaisesRegex(TimingContractError, "first_live_artifact_snapshot_changed"):
                self.verify()
        self.assertEqual(len(calls), 2)

    def test_pinned_schema_drift_is_rejected_before_artifact_read(self):
        original = artifacts.read_regular_file_no_follow
        def read(path, **kwargs):
            payload = original(path, **kwargs)
            return payload + b" " if path.name == "bundle_v1.schema.json" else payload
        with patch.object(artifacts, "read_regular_file_no_follow", side_effect=read), \
             patch.object(artifacts, "read_exact_artifact_directory", side_effect=AssertionError("must not read artifact")):
            with self.assertRaisesRegex(TimingContractError, "first_live_artifact_contract_invalid"):
                self.verify()

    def test_rehashed_publication_overrun_still_fails_numeric_gate(self):
        self.data["publication"]["sealing_finished_ns"] = self.data["publication"]["sealing_started_ns"] + 30_000_000_001
        self.reseal()
        with self.assertRaisesRegex(TimingContractError, "timing_sealing_timeout_exceeded"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
