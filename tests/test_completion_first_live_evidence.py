"""Composed actual Git/archive linkage on synthetic captures and authorization."""
from __future__ import annotations

import copy
import inspect
import unittest
from unittest.mock import patch

from researchops_completion_timing import first_live_evidence as evidence
from researchops_completion_timing import first_live_identity as identity
from researchops_completion_timing.contract import TimingContractError
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests.timed_first_live_evidence_fixture import TimedEvidenceFixture
from tests.test_external_closure_execution_binding import write_file_tree


class TimedEvidenceIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = TimedEvidenceFixture(); cls.addClassCleanup(cls.fixture.close)
        cls.checked = evidence.verify_timed_first_live_evidence(cls.fixture.repository.root, **cls.fixture.arguments())

    def test_actual_archive_execution_source_and_reviewed_git_blob_are_bound(self):
        result = self.checked
        self.assertTrue(result.artifact_linkage_verified)
        self.assertTrue(result.reviewed_bundle_git_verified)
        self.assertTrue(result.execution_source_mapping_binding_verified)
        self.assertEqual(result.subject["first_live_execution_commit"], self.fixture.source_commit)
        self.assertEqual(result.subject["first_live_implementation_commitment_sha256"], identity.IMPLEMENTATION_COMMITMENT_SHA256)
        self.assertEqual(result.reviewed_evidence_commit, self.fixture.reviewed_commit)
        for name in ("source_equivalence_verified", "review_signature_verified", "registry_admission_verified",
                     "actual_live_execution_verified", "runtime_authority_granted", "first_live_success_verified"):
            self.assertIs(getattr(result, name), False, name)
        self.assertNotIn("expected_subject", inspect.signature(evidence.verify_timed_first_live_evidence).parameters)

    def test_wrong_independent_bundle_digest_rejects_before_git(self):
        values = self.fixture.arguments(); values["expected_bundle_commitment_sha256"] = "f" * 64
        with patch.object(evidence, "read_git_object_snapshot", side_effect=AssertionError("must not read Git")):
            with self.assertRaisesRegex(TimingContractError, "first_live_bundle_commitment_mismatch"):
                evidence.verify_timed_first_live_evidence(self.fixture.repository.root, **values)

    def test_reviewed_anchor_requires_exact_blob_bytes_not_current_file(self):
        changed = raw(self.fixture.archive.bundle) + b"\n"
        tree = write_file_tree(self.fixture.repository, self.fixture.files | {self.fixture.path: changed})
        commit = self.fixture.repository.commit(tree, self.fixture.source_commit)
        values = self.fixture.arguments(); values.update(reviewed_evidence_commit=commit, reviewed_evidence_tree=tree)
        with self.assertRaisesRegex(TimingContractError, "timed_first_live_reviewed_bundle_mismatch"):
            evidence.verify_timed_first_live_evidence(self.fixture.repository.root, **values)

    def test_plan_binding_uses_independently_derived_source_subject(self):
        binding = self.fixture.archive.data["plan"]["binding"]
        for name, value in (("source_integrity_commitment_sha256", "f" * 64), ("execution_tree", "f" * 40),
                            ("adapter_version", "different-adapter")):
            changed = dict(binding); changed[name] = value
            with self.subTest(field=name), self.assertRaisesRegex(TimingContractError, "timed_first_live_evidence_source_mismatch"):
                evidence._assert_plan_subject(changed, self.checked.subject)

    def test_incomplete_timing_is_rejected_before_historical_identity(self):
        helper = self.fixture.archive
        before = copy.deepcopy(helper.data)
        try:
            helper.data["evidence"]["record_provenance"] = "synthetic_fixture"
            helper.reseal()
            with patch.object(evidence, "read_git_object_snapshot", side_effect=AssertionError("must not read Git")):
                with self.assertRaisesRegex(TimingContractError, "timed_first_live_evidence_timing_incomplete"):
                    evidence.verify_timed_first_live_evidence(self.fixture.repository.root, **self.fixture.arguments())
        finally:
            helper.data = helper.helper.data = before; helper.reseal()

    def test_fully_rehashed_archive_cannot_forge_actual_source_commitment(self):
        fixture = TimedEvidenceFixture(); self.addCleanup(fixture.close)
        fixture.bind_archive(source_commitment="f" * 64)
        # This is a fresh synthetic DB/control/manifest/envelope and a real Git
        # evidence commit, not a stale-hash or malformed-schema shortcut.
        with self.assertRaisesRegex(TimingContractError, "timed_first_live_evidence_source_mismatch"):
            evidence.verify_timed_first_live_evidence(fixture.repository.root, **fixture.arguments())

    def test_archive_change_during_real_historical_verification_is_rejected(self):
        path = self.fixture.archive.directory / "authorization.json"
        before = path.read_bytes()
        original = evidence.verify_historical_timed_implementation
        def verify(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_bytes(before + b" ")
            return result
        try:
            with patch.object(evidence, "verify_historical_timed_implementation", side_effect=verify):
                with self.assertRaisesRegex(TimingContractError, "timed_first_live_evidence_snapshot_changed"):
                    evidence.verify_timed_first_live_evidence(self.fixture.repository.root, **self.fixture.arguments())
        finally:
            path.write_bytes(before)  # Restore only this test-owned artifact.


if __name__ == "__main__":
    unittest.main()
