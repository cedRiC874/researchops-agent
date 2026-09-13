"""Reserved send compatibility must not restore generic event spoofing."""
import asyncio
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from researchops.audit import AuditError, AuditLedger
from researchops import deepseek_completion_first_live_validation as first_live

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def legacy_session():
    authority = first_live._ConsumedValidationAuthorization._create(
        first_live._AUTHORITY_TOKEN, authorization_id_sha256='3' * 64,
        authorization_binding_sha256='4' * 64,
        authorization_expires_at_utc=datetime.now(timezone.utc) + timedelta(hours=1),
        contract_commitment_sha256=first_live.CONTRACT_COMMITMENT_SHA256,
        implementation_commitment_sha256=first_live.IMPLEMENTATION_COMMITMENT_SHA256,
        execution_commit='a' * 40, source_integrity_commitment_sha256='b' * 64,
        input_price_per_million_cny=first_live.Decimal('3'),
        output_price_per_million_cny=first_live.Decimal('9'))
    tracker, plan = first_live._mint_validation_tracker(ROOT, authority)
    with tempfile.TemporaryDirectory() as directory:
        ledger = AuditLedger(Path(directory) / 'audit.sqlite3')
        run = ledger.start_run(mode='deepseek_completion_first_live_validation',
            request_summary={'offline': 'legacy-send-regression'})
        session = first_live._DeepSeekFirstLiveValidationLedgerSession(
            tracker.bind_case(first_live.VALIDATION_CASE_ID), ledger=ledger, run_id=run,
            runtime_plan_binding=plan, authorization=authority)
        session.arm_deepseek_transport_observation()
        handle = session.begin_attempt()
        yield ledger, session, handle


class LegacySendWriteTests(unittest.TestCase):
    def test_concurrent_compatibility_appends_have_exactly_one_winner(self):
        with legacy_session() as (ledger, session, handle):
            def append():
                try:
                    ledger.append_first_live_transport_send_event(session=session, attempt_handle=handle)
                    return 'written'
                except AuditError:
                    return 'rejected'
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(lambda _: append(), range(2)))
            self.assertCountEqual(results, ['written', 'rejected'])
            events = ledger.export_run(session._validation_run_id)['events']
            self.assertEqual(sum(event['event_type'] == 'provider_transport_request_sent' for event in events), 1)

    def test_observer_writes_exact_legacy_wire_and_generic_spoof_stays_rejected(self):
        with legacy_session() as (ledger, session, handle):
            request = httpx.Request('POST', 'https://api.deepseek.com/responses')
            asyncio.run(session.observe_deepseek_transport_send(request))
            events = ledger.export_run(session._validation_run_id)['events']
            send = events[-1]
            self.assertEqual(send['event_type'], 'provider_transport_request_sent')
            payload = send['safe_payload']
            self.assertEqual(payload, dict(schema_version='deepseek-first-live-transport-send/1.0',
                case_id=handle.case_id, attempt_index=handle.attempt_index,
                case_attempt_index=handle.case_attempt_index, network_call_index=0,
                method='POST', origin='https://api.deepseek.com', path='/responses'))
            with self.assertRaises(AuditError):
                ledger.append_event(session._validation_run_id, 'provider_transport_request_sent',
                    payload, actor_kind='provider_adapter')
            self.assertTrue(ledger.verify_chain(session._validation_run_id).valid)

    def test_wrong_session_or_ledger_cannot_use_compatibility_writer(self):
        with legacy_session() as (ledger, session, handle), tempfile.TemporaryDirectory() as directory:
            other = AuditLedger(Path(directory) / 'other.sqlite3')
            with self.assertRaises(AuditError):
                ledger.append_first_live_transport_send_event(session=object(), attempt_handle=handle)
            with self.assertRaises(AuditError):
                other.append_first_live_transport_send_event(session=session, attempt_handle=handle)

    def test_unarmed_or_nonpending_handle_is_rejected(self):
        with legacy_session() as (ledger, session, handle):
            session._transport_observation_armed = False
            with self.assertRaises(AuditError):
                ledger.append_first_live_transport_send_event(session=session, attempt_handle=handle)
            session._transport_observation_armed = True
            with self.assertRaises(AuditError):
                ledger.append_first_live_transport_send_event(session=session, attempt_handle=object())

    def test_failed_write_consumes_send_attempt_and_cannot_be_retried(self):
        with legacy_session() as (ledger, session, handle):
            with patch.object(AuditLedger, '_append_event_tx', side_effect=OSError('synthetic failure')):
                with self.assertRaises(OSError):
                    ledger.append_first_live_transport_send_event(session=session, attempt_handle=handle)
            with self.assertRaises(AuditError):
                ledger.append_first_live_transport_send_event(session=session, attempt_handle=handle)


if __name__ == '__main__':
    unittest.main()
