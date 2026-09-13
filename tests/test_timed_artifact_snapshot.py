"""Exact-prefix/privacy boundaries on test-owned files, not valid model evidence."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops_external_closure import timed_artifact_snapshot as snapshot, timed_contract as wire
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import test_external_closure_evaluator as facts_fixture
from tests.test_external_closure_documents import SyntheticPreReceipt


class TimedArtifactSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile, _ = wire.load_contract()
        cls.template = facts_fixture._postrun(SyntheticPreReceipt())
        cls.template['schema_version'] = 'provider-completion-postrun-attested-facts/2.0'

    def facts(self, stage):
        value = copy.deepcopy(self.template)
        value['timed_last_completed_write_stage'] = stage
        value['last_completed_artifact_write_stage'], value['artifact_error_code'] = self.profile['core_stage_and_error_projection'][stage]
        return value

    def write(self, root, count, *, empty_last=False):
        for index, name in enumerate(self.profile['artifact_write_order'][:count]):
            (root / name).write_bytes(b'' if empty_last and index == count - 1 else b'{}')

    def inspect(self, root, facts):
        return snapshot.inspect_timed_artifact_snapshot(wire.ROOT, root, postrun_attested_facts=raw(facts))

    def test_all_completed_prefixes_and_empty_failed_next_files(self):
        for completed, stage in enumerate(self.profile['timed_write_stages']):
            for count in sorted({completed, min(completed + 1, 7)}):
                with self.subTest(stage=stage, count=count), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    self.write(root, count, empty_last=count > completed)
                    result = self.inspect(root, self.facts(stage))
                    self.assertEqual(result.artifact_count, count)
                    self.assertEqual(result.publication_disposition, 'normal')
                    self.assertFalse(result.bundle_semantics_verified)
                    self.assertFalse(result.closure_claim_allowed)
                    self.assertEqual(result.complete_write_attested, completed == 7)
                    if count > completed:
                        row = result.file_commitments()[self.profile['artifact_write_order'][count - 1]]
                        self.assertEqual(row, {'bytes': 0, 'sha256': wire._sha(b'')})

    def test_quarantine_never_touches_a_hostile_directory(self):
        class Hostile:
            def __fspath__(self): raise AssertionError('private path touched')
            def __str__(self): raise AssertionError('private path rendered')
        for change in ({'custodian_private_leak_canary_scan_status': 'leak_detected'},
                       {'public_generic_privacy_scan_status': 'unavailable'},
                       {'database_origin_status': 'unavailable'}):
            facts = self.facts('timing_publication_written') | change
            with self.subTest(change=change), patch.object(snapshot, '_read_prefix', side_effect=AssertionError('no artifact read')) as called:
                result = self.inspect(Hostile(), facts)
                self.assertEqual(called.call_count, 0)
                self.assertEqual(result.file_commitments(), {})
                self.assertIsNone(result.artifact_count)
                self.assertIsNone(result.total_byte_count)

    def test_invalid_fact_enum_rejects_before_artifact_access(self):
        facts = self.facts('timing_publication_written') | {'database_origin_status': 'unverified'}
        with patch.object(snapshot, '_read_prefix', side_effect=AssertionError('no artifact read')) as called:
            with self.assertRaises(ValueError): self.inspect(None, facts)
            self.assertEqual(called.call_count, 0)

    def test_sensitive_bytes_never_publish_hashes_or_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write(root, 1)
            path = root / self.profile['artifact_write_order'][0]
            payload = b'sk-synthetic123456789'
            path.write_bytes(payload)
            result = self.inspect(root, self.facts('not_started'))
            self.assertEqual(result.publication_disposition, 'sensitive_quarantined')
            self.assertEqual(result.file_commitments(), {})
            self.assertEqual(len(result._payloads), 0)
            self.assertEqual(path.read_bytes(), payload)

    def test_empty_completed_file_is_not_reinterpreted_as_failed_next(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.write(root, 1, empty_last=True)
            with self.assertRaisesRegex(ValueError, 'completed_file_empty'):
                self.inspect(root, self.facts('audit_database_written'))

    def test_gap_or_sidecar_is_rejected_before_file_reads(self):
        for name in (self.profile['artifact_write_order'][1], 'extra.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); (root / name).write_bytes(b'{}')
                with patch.object(snapshot, 'read_regular_file_no_follow', side_effect=AssertionError('no payload read')) as called:
                    with self.assertRaisesRegex(ValueError, 'prefix_invalid'):
                        self.inspect(root, self.facts('not_started'))
                    self.assertEqual(called.call_count, 0)

    def test_directory_enumeration_stops_at_first_excess_entry(self):
        order = self.profile['artifact_write_order']
        class Entries:
            count = 0
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def __iter__(self): return self
            def __next__(self):
                self.count += 1
                if self.count > 8: raise AssertionError('unbounded enumeration')
                return type('Entry', (), {'name': order[self.count - 1] if self.count < 8 else 'extra'})()
        with tempfile.TemporaryDirectory() as temporary:
            entries = Entries()
            with patch.object(snapshot.os, 'scandir', return_value=entries):
                with self.assertRaisesRegex(ValueError, 'prefix_invalid'):
                    snapshot._read_prefix(Path(temporary), tuple(order), 7, self.profile['limits'])
            self.assertEqual(entries.count, 8)

    def test_byte_change_after_scan_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.write(root, 1)
            actual = snapshot.scan_public_artifact_bytes
            def changed(*args, **kwargs):
                result = actual(*args, **kwargs)
                (root / self.profile['artifact_write_order'][0]).write_bytes(b'{ }')
                return result
            with patch.object(snapshot, 'scan_public_artifact_bytes', side_effect=changed):
                with self.assertRaisesRegex(ValueError, 'snapshot_changed'):
                    self.inspect(root, self.facts('audit_database_written'))

    def test_hardlink_is_not_an_ordinary_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'archive'; root.mkdir(); self.write(root, 1)
            os.link(root / self.profile['artifact_write_order'][0], Path(temporary) / 'alias')
            with self.assertRaises(ValueError):
                self.inspect(root, self.facts('audit_database_written'))


if __name__ == '__main__': unittest.main()
