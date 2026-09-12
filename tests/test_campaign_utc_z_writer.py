"""New v5 write format, unchanged legacy bytes/parser, actual SQLite and SDK."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx2
from researchops.audit import AuditLedger, AuditError
from researchops_completion_timing import campaign_run as run, campaign_runtime as runtime
from researchops_external_closure.primitives import parse_utc_timestamp
from tests import test_completion_campaign_runner_bridge as bridge


class CampaignTimestampTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stamp = datetime(2026, 9, 6, 1, 1, 14, 123456, tzinfo=timezone.utc)

    def test_legacy_default_remains_offset_and_new_writer_emits_Z(self):
        for mode, suffix in (('iso8601', '+00:00'), ('utc_z', 'Z')):
            kwargs = {} if mode == 'iso8601' else {'timestamp_format': mode}
            ledger = AuditLedger(self.root / (mode + '.sqlite3'), clock=lambda: self.stamp, **kwargs)
            run_id = ledger.start_run(mode='synthetic', request_summary={})
            ledger.append_event(run_id, 'synthetic_event', {})
            ledger.set_run_status(run_id, 'completed')
            with ledger._connect() as connection:
                events = [dict(row) for row in connection.execute('SELECT * FROM audit_events ORDER BY sequence')]
            record = ledger.get_run(run_id)
            expected = '2026-09-06T01:01:14.123456' + suffix
            self.assertEqual(record['created_at_utc'], expected)
            self.assertEqual(record['updated_at_utc'], expected)
            self.assertTrue(all(row['occurred_at_utc'] == expected for row in events))
            self.assertTrue(ledger.verify_chain(run_id).valid)
            if mode == 'utc_z':
                parse_utc_timestamp(expected)
            else:
                with self.assertRaises(ValueError): parse_utc_timestamp(expected)

    def test_format_option_rejects_before_any_filesystem_creation(self):
        for value in (True, None, 'Z', 'utc_z\n', 1):
            with self.subTest(value=value), self.assertRaises(AuditError):
                AuditLedger(self.root / 'must-not-exist' / 'audit.sqlite3', timestamp_format=value)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_canonical_writer_converts_offset_and_preserves_microseconds(self):
        value = self.stamp.astimezone(timezone(timedelta(hours=8)))
        ledger = AuditLedger(self.root / 'audit.sqlite3', clock=lambda: value, timestamp_format='utc_z')
        self.assertEqual(ledger._now(), '2026-09-06T01:01:14.123456Z')

    def test_reopening_does_not_rewrite_legacy_events_or_hashes(self):
        path = self.root / 'audit.sqlite3'
        old = AuditLedger(path, clock=lambda: self.stamp)
        run_id = old.start_run(mode='synthetic', request_summary={})
        old.set_run_status(run_id, 'completed')
        with old._connect() as connection:
            before = [dict(row) for row in connection.execute('SELECT * FROM audit_events ORDER BY sequence')]
        new = AuditLedger(path, timestamp_format='utc_z')
        with new._connect() as connection:
            after = [dict(row) for row in connection.execute('SELECT * FROM audit_events ORDER BY sequence')]
        self.assertEqual(after, before); self.assertTrue(new.verify_chain(run_id).valid)

    def test_only_v5_campaign_factory_selects_canonical_format(self):
        for version, suffix in ((4, '+00:00'), (5, 'Z')):
            ledger = run._campaign_ledger(self.root / f'{version}.sqlite3', implementation_version=version)
            ledger._clock = lambda: self.stamp
            self.assertEqual(ledger._now(), '2026-09-06T01:01:14.123456' + suffix)

    def test_unknown_campaign_version_creates_nothing(self):
        for value in (True, 4.0, '5', 6):
            with self.subTest(value=value), self.assertRaises(run.CampaignInvocationError):
                run._campaign_ledger(self.root / 'absent' / 'audit.sqlite3', implementation_version=value)
        self.assertEqual(list(self.root.iterdir()), [])


class CampaignSdkTimestampTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_sdk_model_call_does_not_append_a_second_Z(self):
        helper = bridge.CampaignRunnerBridgeTests()
        def cleanup():
            while helper._cleanups:
                function, args, kwargs = helper._cleanups.pop(); function(*args, **kwargs)
        self.addCleanup(cleanup)
        with patch.object(bridge, 'AuditLedger', side_effect=lambda path: AuditLedger(path, timestamp_format='utc_z')):
            await helper.asyncSetUp()
        with patch.object(runtime, '_native_transport', side_effect=lambda: httpx2.MockTransport(helper.transport)):
            await helper.factory.run_case(0, api_key='FIXTURE-NOT-A-REAL-KEY')
        with helper.ledger._connect() as connection:
            models = [dict(row) for row in connection.execute('SELECT * FROM model_calls')]
            events = [dict(row) for row in connection.execute('SELECT * FROM audit_events')]
        self.assertEqual(len(models), 1)
        parse_utc_timestamp(models[0]['started_at_utc'])
        self.assertFalse(models[0]['started_at_utc'].endswith('ZZ'))
        for event in events: parse_utc_timestamp(event['occurred_at_utc'])
        self.assertTrue(helper.ledger.verify_chain(helper.factory._runs[0]).valid)


if __name__ == '__main__': unittest.main()
