"""Offline P2 regressions; temporary artifacts, no Provider or frozen rewrites."""
import copy
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from researchops.audit import AuditLedger, _event_hash
from researchops_completion_timing.artifacts import load_artifact_contract
from researchops_completion_timing.contract import TimingContractError, digest
from researchops_external_closure import artifacts
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import test_completion_first_live_audit as audit_fixture


class FirstLiveEventUtcTests(unittest.TestCase):
    def setUp(self):
        self.helper = audit_fixture.FirstLiveAuditTests()
        self.addCleanup(self.helper.doCleanups)
        self.tick = 0

        def clock():
            self.tick += 1
            return datetime(2026, 9, 6, tzinfo=timezone.utc) + timedelta(seconds=self.tick)

        with patch.object(audit_fixture, 'AuditLedger', side_effect=lambda path: AuditLedger(path, clock=clock)):
            self.helper.setUp()
        self.original = self.helper.database.read_bytes()
        self.original_data = copy.deepcopy(self.helper.data)
        connection = sqlite3.connect(self.helper.database.as_uri() + '?mode=ro&immutable=1', uri=True)
        try:
            connection.row_factory = sqlite3.Row
            self.rows = [dict(row) for row in connection.execute('SELECT * FROM audit_events ORDER BY sequence')]
        finally:
            connection.close()
        self.assertEqual(len({row['occurred_at_utc'] for row in self.rows}), 6)

    def rewrite_temporary_events(self, replacements_by_index):
        """Keep schema/triggers and byte lengths; reseal only synthetic evidence.

        An ordinary SQL UPDATE must remain forbidden. This models a supplied
        internally rehashed artifact, not a bypass of independent signed hashes.
        """
        previous = '0' * 64
        replacements = {}
        for index, row in enumerate(self.rows):
            stamp = replacements_by_index.get(index, row['occurred_at_utc'])
            if stamp != row['occurred_at_utc']:
                self.assertEqual(len(stamp.encode()), len(row['occurred_at_utc'].encode()))
                replacements[row['occurred_at_utc'].encode()] = stamp.encode()
            computed = _event_hash(run_id=row['run_id'], sequence=row['sequence'], event_type=row['event_type'],
                occurred_at_utc=stamp, actor_kind=row['actor_kind'], safe_payload_json=row['safe_payload_json'],
                prev_hash=previous)
            replacements[row['event_hash'].encode()] = computed.encode()
            previous = computed
        changed = re.sub(b'|'.join(re.escape(key) for key in replacements),
            lambda match: replacements[match.group()], self.original)
        self.assertEqual(len(changed), len(self.original))
        self.helper.database.write_bytes(changed)
        self.helper.data = copy.deepcopy(self.original_data)
        profile, _ = load_artifact_contract(audit_fixture.ROOT)
        heads = [{'run_id': self.helper.run_id, 'final_chain_head_sha256': previous}]
        self.helper.data['evidence']['audit_chain_heads_sha256'] = digest(
            profile['ordered_audit_heads_domain'].encode() + b'\0' + profile['ordered_audit_heads_tag'].encode() + b'\0' + raw(heads))
        audit_fixture.TimingHelper().seal(self.helper.data)
        self.helper.database_hash = digest(changed)
        self.assertTrue(self.helper.ledger.verify_chain(self.helper.run_id).valid)
        return changed

    def test_each_intermediate_event_rejects_invalid_utc_even_after_rehash(self):
        for index in (1, 2, 3, 4):
            original = self.rows[index]['occurred_at_utc']
            for kind, stamp in (
                ('not_a_timestamp', '!' * len(original)),
                ('invalid_calendar', original[:5] + '99' + original[7:]),
                ('non_utc_offset', original[:-6] + '+01:00'),
            ):
                with self.subTest(event_index=index, kind=kind):
                    changed = self.rewrite_temporary_events({index: stamp})
                    with self.assertRaisesRegex(TimingContractError, 'first_live_audit_lifecycle_invalid'):
                        self.helper.verify()
                    self.assertEqual(self.helper.database.read_bytes(), changed)

    def test_legacy_zero_offset_and_Z_both_validate_without_rewriting_bytes(self):
        self.assertTrue(self.helper.verify()['timing_gate_complete'])
        changed = self.rewrite_temporary_events({index: self.rows[index]['occurred_at_utc'][:-6] + '.0000Z'
                                                 for index in (1, 2, 3, 4)})
        checked = self.helper.verify()
        self.assertTrue(checked['timing_gate_complete'])
        self.assertFalse(checked['runtime_authority_granted'])
        self.assertFalse(checked['current_closure_claim_allowed'])
        self.assertEqual(self.helper.database.read_bytes(), changed)


class EmptyDirectoryEnumerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_empty_directory_uses_no_eager_listdir_or_iterdir(self):
        with patch.object(Path, 'iterdir', side_effect=AssertionError('eager enumeration forbidden')), \
             patch.object(artifacts.os, 'listdir', side_effect=AssertionError('eager enumeration forbidden')):
            self.assertEqual(artifacts._read_expected_directory(self.root, ()), {})

    def test_nonempty_directory_stops_after_first_entry_and_closes_iterator(self):
        class OneEntry:
            def __init__(self):
                self.count = 0
                self.closed = False
            def __enter__(self): return self
            def __exit__(self, *args): self.closed = True
            def __iter__(self): return self
            def __next__(self):
                self.count += 1
                if self.count > 1: raise AssertionError('must not enumerate a second entry')
                return SimpleNamespace(name='unexpected')
        entries = OneEntry()
        with patch.object(artifacts.os, 'scandir', return_value=entries), \
             patch.object(Path, 'iterdir', side_effect=AssertionError('eager enumeration forbidden')):
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, 'directory_file_set_invalid'):
                artifacts._read_expected_directory(self.root, ())
        self.assertEqual(entries.count, 1)
        self.assertTrue(entries.closed)

    def test_real_unexpected_entry_is_rejected_without_reading_its_contents(self):
        (self.root / 'unexpected').write_bytes(b'synthetic')
        with patch.object(artifacts, 'read_exact_artifact_directory', side_effect=AssertionError('no content reads')):
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, 'directory_file_set_invalid'):
                artifacts._read_expected_directory(self.root, ())

    def test_enumeration_error_is_fail_closed(self):
        with patch.object(artifacts.os, 'scandir', side_effect=OSError('synthetic failure')):
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, 'directory_read_failed'):
                artifacts._read_expected_directory(self.root, ())

    def test_empty_directory_identity_recheck_remains_required(self):
        with patch.object(artifacts, '_directory_identity', side_effect=[(1, 2, 3, 4, 5), (1, 9, 3, 4, 5)]):
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, 'directory_identity_changed'):
                artifacts._read_expected_directory(self.root, ())


if __name__ == '__main__':
    unittest.main()
