"""Public timed A rejection/partial/quarantine paths; no full normal positive claim."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops_external_closure import timed_evaluator as evaluator, timed_contract as wire
from researchops_external_closure import timed_artifact_snapshot as snapshots
from researchops_external_closure.primitives import canonical_json_bytes as raw
from researchops_external_closure.types import PreReceiptRejected
from tests import test_timed_history as history_fixture
from tests import test_completion_timing_pre_execution as pre_fixture


class TimedEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.helper = history_fixture.TimedHistoryTests(); self.helper.setUp()
        self.profile, _ = wire.load_contract()

    def check(self, directory):
        return evaluator.evaluate_timed_closure_bundle(wire.ROOT, pre_fixture.documents(self.helper.fixture),
            pre_execution_observation=raw(self.helper.fixture.observation), postrun_observation=raw(self.helper.after),
            timing_plan_bytes=raw(self.helper.plan), artifact_directory=directory,
            postrun_attested_facts=raw(self.helper.facts))

    def set_stage(self, stage):
        self.helper.facts['timed_last_completed_write_stage'] = stage
        core, error = self.profile['core_stage_and_error_projection'][stage]
        self.helper.facts.update(last_completed_artifact_write_stage=core, artifact_error_code=error)
        self.helper.after['manifest_observation']['last_completed_artifact_write_stage'] = core

    def test_quarantine_is_unsigned_hash_free_and_performs_no_artifact_or_admission_reads(self):
        class Hostile:
            def __fspath__(self): raise AssertionError('private path accessed')
        self.helper.facts['custodian_private_leak_canary_scan_status'] = 'leak_detected'
        self.helper.after['manifest_observation'].update(state='withheld_quarantined', manifest_sha256=None,
            quarantine_reason='sensitive_detected')
        with patch.object(snapshots, '_read_prefix', side_effect=AssertionError('no artifact read')) as files, \
             patch.object(evaluator, '_verify_admission', side_effect=AssertionError('no admission read')) as admission:
            result = self.check(Hostile())
        self.assertIs(type(result), evaluator.TimedReceiptProjectionReady, getattr(result, 'error_code', None))
        self.assertEqual((files.call_count, admission.call_count), (0, 0))
        body = json.loads(result.unsigned_receipt_canonical_json)
        self.assertFalse(body['evidence_valid']); self.assertFalse(body['pre_anchor_closure_eligible'])
        self.assertEqual(body['timing']['status'], 'quarantined')
        self.assertEqual(body['partial_artifacts'], {})
        self.assertIsNone(body['closure_bundle_manifest_sha256'])
        self.assertIsNone(body['timing']['evidence_file_sha256'])
        self.assertNotIn('signatures', body); self.assertNotIn('document_sha256', body)
        self.assertEqual(result.receipt_document_sha256, wire.receipt_sha256(body, self.profile))
        self.assertFalse(result.closure_claim_allowed)

    def test_all_partial_stages_are_signable_failures_with_exact_safe_prefixes(self):
        for completed, stage in enumerate(self.profile['timed_write_stages'][:-1]):
            for count in (completed, completed + 1):
                with self.subTest(stage=stage, count=count), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary); self.set_stage(stage)
                    for index, name in enumerate(self.profile['artifact_write_order'][:count]):
                        (root / name).write_bytes(b'' if index == completed else b'{}')
                    observation = self.helper.after['manifest_observation']
                    observation.update(state='present' if completed >= 6 else 'absent',
                        manifest_sha256=wire._sha(b'{}') if completed >= 6 else None)
                    result = self.check(root)
                    self.assertIs(type(result), evaluator.TimedReceiptProjectionReady, getattr(result, 'error_code', None))
                    body = json.loads(result.unsigned_receipt_canonical_json)
                    self.assertFalse(body['evidence_valid']); self.assertFalse(body['pre_anchor_closure_eligible'])
                    self.assertEqual(len(body['partial_artifacts']), count)
                    self.assertEqual(body['timing']['status'], 'unavailable')
                    if count > completed:
                        self.assertEqual(body['partial_artifacts'][self.profile['artifact_write_order'][completed]]['sha256'], wire._sha(b''))

    def test_unavailable_privacy_and_untrusted_origin_are_hash_free_failures(self):
        for field, expected in (('custodian_private_leak_canary_scan_status', 'closure_sensitive_scan_unavailable'),
                                ('database_origin_status', 'closure_database_origin_untrusted')):
            previous = self.helper.facts[field]
            self.helper.facts[field] = 'unavailable'
            self.helper.after['manifest_observation'].update(state='withheld_quarantined', manifest_sha256=None,
                quarantine_reason='privacy_unverified')
            with self.subTest(field=field), patch.object(snapshots, '_read_prefix', side_effect=AssertionError('no artifact read')) as files:
                result = self.check(None)
                self.assertIs(type(result), evaluator.TimedReceiptProjectionReady, getattr(result, 'error_code', None))
                body = json.loads(result.unsigned_receipt_canonical_json)
                self.assertEqual(result.evidence_error_code, expected)
                self.assertEqual(files.call_count, 0)
                self.assertEqual(body['partial_artifacts'], {})
                self.assertFalse(body['evidence_valid'])
            self.helper.facts[field] = previous

    def test_seven_named_files_are_not_a_verified_complete_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in self.profile['artifact_write_order']: (root / name).write_bytes(b'{}')
            self.helper.after['manifest_observation']['manifest_sha256'] = wire._sha(b'{}')
            result = self.check(root)
            self.assertIs(type(result), evaluator.TimedReceiptProjectionReady, getattr(result, 'error_code', None))
            body = json.loads(result.unsigned_receipt_canonical_json)
            self.assertEqual(body['bundle_status'], 'manifest_present_invalid')
            self.assertFalse(body['evidence_valid']); self.assertFalse(body['pre_anchor_closure_eligible'])

    def test_wrong_receipt_id_rejects_before_artifact_access(self):
        self.helper.facts['receipt_id'] = 'PCECLOSE-' + 'F' * 32
        with patch.object(evaluator, 'inspect_timed_artifact_snapshot', side_effect=AssertionError('no artifact access')) as called:
            result = self.check(None)
        self.assertIs(type(result), PreReceiptRejected)
        self.assertEqual(result.error_code, 'timed_history_receipt_id_mismatch')
        self.assertEqual(called.call_count, 0)

    def test_mismatched_independent_manifest_does_not_become_a_failure_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in self.profile['artifact_write_order']: (root / name).write_bytes(b'{}')
            self.helper.after['manifest_observation']['manifest_sha256'] = 'f' * 64
            result = self.check(root)
            self.assertIs(type(result), PreReceiptRejected)
            self.assertEqual(result.error_code, 'external_closure_manifest_observation_mismatch')


if __name__ == '__main__': unittest.main()
