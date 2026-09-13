"""Failure package bytes/chain/privacy tests; no Provider or actual claim store."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from researchops.audit import AuditLedger
from researchops_completion_timing import first_live_failure_artifacts as artifact
from researchops_completion_timing.first_live_failure import build_first_live_failure_receipt
from researchops_completion_timing.first_live_start import FirstLiveStartError, _StartBudget
from researchops_external_closure.primitives import canonical_json_bytes as raw

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows exclusive output locking')
class FailureArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.binding = dict(authorization_id_sha256='a' * 64, validation_run_id='PCEVALID-' + 'A' * 32,
                            plan_commitment_sha256='b' * 64, execution_binding_sha256='c' * 64)
        self.receipt = build_first_live_failure_receipt(ROOT,
            FirstLiveStartError('synthetic_preparation_failure', claim_consumed=True, origin='preparation'))
        self.expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat().replace('+00:00', 'Z')

    def ledger(self):
        ledger = AuditLedger(self.directory / 'audit.sqlite3')
        ledger.start_run(run_id=self.binding['validation_run_id'], mode='deepseek_first_live_timed_validation',
                         request_summary={name: self.binding[name] for name in ('plan_commitment_sha256', 'execution_binding_sha256')})
        ledger.set_run_status(self.binding['validation_run_id'], 'failed', terminal_error_code='first_live_failed')
        return ledger

    def write(self):
        return artifact._write_failure_package(ROOT, self.directory, self.receipt, binding=self.binding,
                                               budget=_StartBudget(), expires=self.expires)

    def verify(self, result):
        return artifact.verify_failure_artifacts(ROOT, self.directory,
            expected_manifest_commitment_sha256=result['manifest_commitment_sha256'])

    def test_actual_database_and_receipt_are_bound_without_inferred_counts(self):
        self.ledger()
        result = self.write()
        self.assertEqual(self.verify(result), result)
        self.assertTrue(result['audit_chain_bound'])
        self.assertFalse(result['receipt_counts_independently_verified'])
        self.assertFalse(result['runtime_authority_granted'])
        manifest = json.loads((self.directory / 'failure_manifest.json').read_bytes())
        self.assertEqual(manifest['audit_snapshot']['event_count'], 2)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()),
                         ['audit.sqlite3', 'failure_manifest.json', 'failure_receipt.json'])

    def test_missing_database_is_unknown_not_zero_or_absence_proof(self):
        result = self.write()
        self.assertFalse(self.verify(result)['audit_chain_bound'])
        snapshot = json.loads((self.directory / 'failure_manifest.json').read_bytes())['audit_snapshot']
        self.assertEqual(snapshot['status'], 'unavailable')
        self.assertIsNone(snapshot['event_count']); self.assertIsNone(snapshot['sha256'])

    def test_sensitive_database_is_not_parsed_hashed_or_copied(self):
        fixtures = (b'Authorization: Bearer FIXTURE-NOT-A-REAL-KEY', b'sk-FAKESECRET12345678',
                    b'C:\\fixture\\private.csv', b'Traceback (most recent call last)')
        parent = self.directory
        for index, secret_fixture in enumerate(fixtures):
            with self.subTest(index=index):
                self.directory = parent / str(index); self.directory.mkdir()
                (self.directory / 'audit.sqlite3').write_bytes(secret_fixture)
                with patch.object(artifact, '_verify_audit_database') as parser:
                    result = self.write()
                parser.assert_not_called()
                self.assertFalse(result['audit_chain_bound'])
                for name in ('failure_manifest.json', 'failure_receipt.json'):
                    self.assertNotIn(secret_fixture, (self.directory / name).read_bytes())
                self.assertEqual((self.directory / 'audit.sqlite3').read_bytes(), secret_fixture)

    def test_receipt_tamper_fails(self):
        result = self.write()
        path = self.directory / 'failure_receipt.json'
        value = json.loads(path.read_bytes()); value['claim_consumed'] = False; path.write_bytes(raw(value))
        with self.assertRaises(ValueError): self.verify(result)

    def test_database_tamper_fails_without_rewriting_manifest(self):
        ledger = self.ledger(); result = self.write()
        with ledger._connect() as connection:
            connection.execute("UPDATE runs SET request_sha256 = ?", ('d' * 64,))
        with self.assertRaises(ValueError): self.verify(result)

    def test_event_payload_tamper_breaks_hash_chain(self):
        ledger = self.ledger(); result = self.write()
        with ledger._connect() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                connection.execute('UPDATE audit_events SET safe_payload_json = ? WHERE sequence = 1', ('{"fake":true}',))
            # Offline attacker simulation ONLY in this test's temporary database.
            # Restore the identical trigger so the second check reaches the chain
            # rather than merely detecting a changed schema. Production unchanged.
            trigger_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'audit_events_no_update'").fetchone()[0]
            connection.execute('DROP TRIGGER audit_events_no_update')
            connection.execute('UPDATE audit_events SET safe_payload_json = ? WHERE sequence = 1', ('{"fake":true}',))
            connection.execute(trigger_sql)
        with self.assertRaisesRegex(ValueError, 'database_changed'):
            self.verify(result)

    def test_manifest_tamper_and_new_self_hash_do_not_replace_external_expectation(self):
        result = self.write()
        path = self.directory / 'failure_manifest.json'; value = json.loads(path.read_bytes())
        value['authorization_id_sha256'] = 'd' * 64
        value['manifest_commitment_sha256'] = artifact.manifest_commitment(value)
        path.write_bytes(raw(value))
        with self.assertRaisesRegex(ValueError, 'expected_commitment'): self.verify(result)

    def test_partial_write_is_retained_and_cannot_be_overwritten(self):
        original = artifact._write_exclusive
        def write(path, payload, created):
            if path.name == 'failure_manifest.json': raise OSError('synthetic disk failure')
            return original(path, payload, created)
        with patch.object(artifact, '_write_exclusive', side_effect=write), self.assertRaises(OSError):
            self.write()
        self.assertEqual((self.directory / 'failure_receipt.json').read_bytes(), self.receipt)
        self.assertFalse((self.directory / 'failure_manifest.json').exists())
        with self.assertRaises(FileExistsError): self.write()

    def test_expired_budget_writes_nothing(self):
        self.expires = '2000-01-01T00:00:00Z'
        with self.assertRaises(ValueError): self.write()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_unavailable_cannot_carry_zero_or_derived_metadata(self):
        self.write()
        value = json.loads((self.directory / 'failure_manifest.json').read_bytes())
        value['audit_snapshot']['event_count'] = 1
        value['manifest_commitment_sha256'] = artifact.manifest_commitment(value)
        with self.assertRaisesRegex(ValueError, 'snapshot_invalid'):
            artifact._validate_manifest(ROOT, raw(value))


if __name__ == '__main__': unittest.main()
