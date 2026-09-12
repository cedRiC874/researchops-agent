"""Actual Runner/SDK/SQLite bridge with explicit admission stubs, fake Key only."""
import asyncio
import copy
import json
import tempfile
import unittest
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx2

from researchops.audit import AuditLedger, AuditError, sha256_json, verify_audit_chain_rows
from researchops_completion_timing import campaign_runtime as runtime, campaign_tasks as tasks
from researchops_external_closure.primitives import canonical_json_bytes as raw
from researchops_external_closure.semantics import _SEND_FIELDS, _validate_send_payload, _validate_model_usage_payload
from tests import test_completion_campaign_opening as opening_fixture, test_completion_campaign_start as start_fixture
from tests.test_completion_telemetry_ledger import _test_runtime_binding
from tests.test_deepseek_completion_first_live_validation import _response_body


class CampaignRunnerBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.getaddrinfo'):
            self.stack.enter_context(patch(target, side_effect=AssertionError('real network forbidden')))
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        opening = opening_fixture.CampaignOpeningTests(); opening.setUp()
        snapshot = _test_runtime_binding().runtime_snapshot()
        denominator = opening.envelope['runtime_plan']['denominator_plan']
        for name in ('provider_id', 'api_surface', 'transport_id', 'adapter_version', 'telemetry_schema_sha256',
                     'mapping_schema_version', 'mapping_version', 'mapping_sha256'):
            denominator[name] = snapshot[name]
        opening.envelope['runtime_plan']['denominator_plan_commitment_sha256'] = sha256_json(denominator)
        opening.reseal()
        prepared = start_fixture.CampaignOwnershipTests().holder(envelope_bytes=raw(opening.envelope))
        prepared.proof = SimpleNamespace(scope=SimpleNamespace(timing=SimpleNamespace(timing_plan_commitment_sha256='1' * 64,
            timing_execution_binding_sha256='2' * 64, authorization_grant_sha256='3' * 64)))
        prepared.clock_domain_id = 'PCECLOCK-' + 'A' * 32
        self.stack.enter_context(patch.object(tasks._CampaignTaskOwner, '_check_current'))
        self.stack.enter_context(patch.object(runtime._CampaignModelFactory, '_check_current'))
        owner = tasks._CampaignTaskOwner(prepared)
        prepared.clock.task_released()
        owner._attempted = True; owner._opened = opening.verify()
        self.ledger = AuditLedger(self.root / 'audit.sqlite3')
        self.factory = runtime._CampaignModelFactory(owner, self.ledger)
        self.factory.before_key_load(); self.factory.key_loaded()
        self.calls = []

    def transport(self, request):
        body = json.loads(request.content); self.calls.append(body['max_output_tokens'])
        return httpx2.Response(200, json=_response_body('completed', body['max_output_tokens']))

    def persisted_events(self, run):
        # export_run intentionally applies the generic scrubber again. Closure
        # evidence consumes the actual SQLite rows, not that convenience view.
        with self.ledger._connect() as connection:
            rows = connection.execute('SELECT * FROM audit_events WHERE run_id=? ORDER BY sequence', (run,)).fetchall()
        return [dict(row, payload=json.loads(row['safe_payload_json'])) for row in rows]

    def assert_usage_tail(self, run, *, complete):
        events = self.persisted_events(run)
        self.assertEqual([row['event_type'] for row in events[-2:]], ['model_run_usage_recorded', 'run_status_changed'])
        self.assertEqual(sum(row['event_type'] == 'model_run_usage_recorded' for row in events), 1)
        with self.ledger._connect() as connection:
            rows = [dict(row) for row in connection.execute('SELECT * FROM model_calls WHERE run_id=?', (run,))]
        detail_count = sum(row['event_type'] in ('model_response_telemetry_recorded',
            'provider_completion_mapping_unmapped', 'model_response_telemetry_rejected') for row in events)
        _validate_model_usage_payload(events[-2]['payload'], model_rows=rows, response_detail_count=detail_count,
            plan=SimpleNamespace(provider_id='deepseek', transport_id='openai_compatible_responses'))
        self.assertEqual(events[-2]['payload']['usage_complete'], complete)
        self.assertTrue(self.ledger.verify_chain(run).valid)

    async def test_real_runner_joins_case_send_record_usage_and_budget(self):
        with patch.object(runtime, '_native_transport', side_effect=lambda: httpx2.MockTransport(self.transport)):
            for case in range(2):
                result = await self.factory.run_case(case, api_key='FIXTURE-NOT-A-REAL-KEY')
                self.assertFalse(result['task_pass_produced'])
        self.assertEqual(self.calls, [512, 512])
        self.assertEqual(self.factory._budget.summary()['completed_case_count'], 2)
        for run in self.factory._runs:
            events = self.ledger.export_run(run)['events']
            self.assertEqual([item['event_type'] for item in events], ['run_started', 'model_request_started',
                'provider_transport_request_sent', 'model_response_telemetry_recorded', 'model_call_recorded',
                'model_run_usage_recorded', 'run_status_changed'])
            self.assertEqual(events[1]['safe_payload']['case_id'], events[2]['safe_payload']['case_id'])
            self.assertEqual(events[2]['safe_payload']['case_id'], events[3]['safe_payload']['case_id'])
            self.assertEqual(events[3]['safe_payload']['schema_version'], 'provider-completion-ledger-event/1.2')
            self.assertTrue(self.ledger.verify_chain(run).valid)
            self.assert_usage_tail(run, complete=True)
        self.assertTrue(all(item.closure_eligible for item in self.factory._case_reconciliations))

    async def test_send_payload_survives_actual_sqlite_without_generic_redaction(self):
        with patch.object(runtime, '_native_transport', side_effect=lambda: httpx2.MockTransport(self.transport)):
            await self.factory.run_case(0, api_key='FIXTURE-NOT-A-REAL-KEY')
        events = self.persisted_events(self.factory._runs[0])
        event = events[2]['payload']
        self.assertEqual(event['requested_output_token_cap'], 512)
        self.assertEqual(event['authorization_grant_sha256'], '3' * 64)
        self.assertEqual(set(event), set(_SEND_FIELDS))
        plan = SimpleNamespace(**{name: self.factory._execution[name] for name in
            ('provider_id', 'api_surface', 'transport_id', 'adapter_version')},
            external_plan_binding_sha256=self.factory._plan['external_plan_binding_sha256'],
            output_token_limit_per_request=512)
        _validate_send_payload(event, expected_start=events[1]['payload'], plan=plan,
            campaign_id=self.factory._plan['campaign_topology']['campaign_id'], authorization_grant_sha256='3' * 64,
            api_origin='https://api.deepseek.com', api_path='/responses', model_id='deepseek-v4-flash')
        # The export's generic privacy behavior has not been globally weakened.
        exported = self.ledger.export_run(self.factory._runs[0])['events'][2]['safe_payload']
        self.assertEqual(exported['requested_output_token_cap'], '[SECRET_REDACTED]')
        for field, value in (('requested_output_token_cap', 513), ('authorization_grant_sha256', '4' * 64)):
            rows = copy.deepcopy(events)
            rows[2]['payload'][field] = value
            rows[2]['safe_payload_json'] = raw(rows[2]['payload']).decode()
            with self.subTest(tamper=field):
                self.assertFalse(verify_audit_chain_rows(self.factory._runs[0], rows).valid)

    async def test_generic_append_rejects_reserved_transport_send(self):
        run = self.ledger.start_run(mode='synthetic', request_summary={})
        with self.assertRaises(AuditError):
            self.ledger.append_event(run, 'provider_transport_request_sent', {}, actor_kind='provider_adapter')

    async def test_send_accounting_delay_cannot_invent_a_dispatch(self):
        now = [time.monotonic_ns()]
        self.factory._prepared.clock._request_cap = 100_000_000
        original = self.factory._record_send
        def delayed(session):
            result = original(session)
            now[0] += 200_000_000
            return result
        with patch('researchops_completion_timing.clock.time.monotonic_ns', side_effect=lambda: now[0]), \
             patch.object(self.factory, '_record_send', side_effect=delayed), \
             patch.object(runtime, '_native_transport', side_effect=lambda: httpx2.MockTransport(self.transport)):
            with self.assertRaises(Exception):
                await self.factory.run_case(0, api_key='FIXTURE-NOT-A-REAL-KEY')
        events = self.ledger.export_run(self.factory._runs[0])['events']
        sends = [event for event in events if event['event_type'] == 'provider_transport_request_sent']
        self.assertEqual(len(sends), len(self.calls))
        self.assertEqual(len(self.calls), 1)
        self.assert_usage_tail(self.factory._runs[0], complete=False)

    async def test_preparation_deadline_has_no_send_and_no_native_dispatch(self):
        now = [time.monotonic_ns()]
        clock = self.factory._prepared.clock
        original = self.factory._before_send
        def delayed(session, request):
            original(session, request)
            # Request time starts at dispatch; preparation is bounded by the
            # phase deadline, not by a not-yet-started request timeout.
            now[0] = clock._origin + clock._phase_cap + 1
        with patch('researchops_completion_timing.clock.time.monotonic_ns', side_effect=lambda: now[0]), \
             patch.object(self.factory, '_before_send', side_effect=delayed), \
             patch.object(runtime, '_native_transport', side_effect=lambda: httpx2.MockTransport(self.transport)):
            with self.assertRaises(Exception):
                await self.factory.run_case(0, api_key='FIXTURE-NOT-A-REAL-KEY')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.factory._network_count, 0)
        events = self.persisted_events(self.factory._runs[0])
        self.assertNotIn('provider_transport_request_sent', [row['event_type'] for row in events])
        self.assertIn('model_request_no_response', [row['event_type'] for row in events])
        self.assert_usage_tail(self.factory._runs[0], complete=False)

    async def test_send_capability_rejects_tampered_bindings_before_dispatch(self):
        original = self.ledger._create_transport_send_capability
        def checked(payload, **kwargs):
            for field, value in (('authorization_grant_sha256', '4' * 64),
                                 ('external_plan_binding_sha256', '4' * 64),
                                 ('execution_input_commitment_sha256', '4' * 64),
                                 ('case_id', 'wrong-case'), ('requested_output_token_cap', True),
                                 ('headers_persisted', True), ('origin', 'https://other.invalid'),
                                 ('adapter_version', 'Authorization: Bearer synthetic-secret')):
                mutated = dict(payload, **{field: value})
                with self.subTest(field=field), self.assertRaises((AuditError, ValueError)):
                    original(mutated, **kwargs)
            with self.assertRaises(AuditError): original(dict(payload, unexpected='raw content'), **kwargs)
            capability = original(payload, **kwargs)
            with self.assertRaises(AuditError): original(payload, **kwargs)
            with self.assertRaises(AttributeError): capability._payload_bytes = b'{}'
            with self.assertRaises(AttributeError): del capability._used
            with self.assertRaises(TypeError): copy.copy(capability)
            with self.assertRaises(AuditError): self.ledger.append_transport_send_event(capability)
            return capability
        with patch.object(self.ledger, '_create_transport_send_capability', side_effect=checked), \
             patch.object(runtime, '_native_transport', side_effect=lambda: httpx2.MockTransport(self.transport)):
            await self.factory.run_case(0, api_key='FIXTURE-NOT-A-REAL-KEY')
        self.assertEqual(self.calls, [512])

    async def test_send_capability_is_single_use_and_ledger_bound(self):
        original = self.ledger.append_transport_send_event
        foreign = AuditLedger(self.root / 'foreign.sqlite3')
        def checked(capability):
            with self.assertRaises(AuditError): foreign.append_transport_send_event(capability)
            result = original(capability)
            with self.assertRaises(AuditError): original(capability)
            with self.assertRaises(AuditError): capability._mark_dispatch_boundary()
            return result
        with patch.object(self.ledger, 'append_transport_send_event', side_effect=checked), \
             patch.object(runtime, '_native_transport', side_effect=lambda: httpx2.MockTransport(self.transport)):
            await self.factory.run_case(0, api_key='FIXTURE-NOT-A-REAL-KEY')
        self.assertEqual(self.factory._network_count, 1)

    async def test_send_write_failure_closes_unreturned_response_without_retry(self):
        closed = []; capabilities = []
        class Stream(httpx2.AsyncByteStream):
            async def __aiter__(self): yield b'{}'
            async def aclose(self): closed.append(True)
        def response(request):
            self.calls.append(1)
            return httpx2.Response(200, stream=Stream())
        original_append = self.ledger._append_event_tx
        original_send = self.ledger.append_transport_send_event
        def broken(connection, run_id, event_type, *args, **kwargs):
            if event_type == 'provider_transport_request_sent': raise OSError('synthetic DB failure')
            return original_append(connection, run_id, event_type, *args, **kwargs)
        def send(capability):
            capabilities.append(capability)
            return original_send(capability)
        with patch.object(self.ledger, '_append_event_tx', side_effect=broken), \
             patch.object(self.ledger, 'append_transport_send_event', side_effect=send), \
             patch.object(runtime, '_native_transport', side_effect=lambda: httpx2.MockTransport(response)):
            with self.assertRaises(Exception): await self.factory.run_case(0, api_key='FIXTURE-NOT-A-REAL-KEY')
        self.assertEqual(self.calls, [1]); self.assertEqual(closed, [True])
        self.assertEqual(self.factory._network_count, 1); self.assertEqual(len(capabilities), 1)
        with self.assertRaises(AuditError): original_send(capabilities[0])
        self.assertTrue(self.factory._failed)
        self.assert_usage_tail(self.factory._runs[0], complete=False)

    async def test_native_exception_and_post_send_cancellation_keep_the_send_fact(self):
        async def cancelled(request):
            self.calls.append(1)
            raise asyncio.CancelledError()
        with patch.object(runtime, '_native_transport', side_effect=lambda: httpx2.MockTransport(cancelled)):
            with self.assertRaises(BaseException): await self.factory.run_case(0, api_key='FIXTURE-NOT-A-REAL-KEY')
        events = self.persisted_events(self.factory._runs[0])
        self.assertEqual(self.calls, [1]); self.assertEqual(self.factory._network_count, 1)
        self.assertEqual(sum(row['event_type'] == 'provider_transport_request_sent' for row in events), 1)
        self.assertIn('model_request_outcome_unknown', [row['event_type'] for row in events])
        self.assert_usage_tail(self.factory._runs[0], complete=False)

    async def test_runner_failure_retains_available_sdk_usage_before_failed_status(self):
        original = runtime.Runner.run
        async def failed_after_response(*args, **kwargs):
            result = await original(*args, **kwargs)
            error = RuntimeError('synthetic Runner failure')
            error.run_data = result
            raise error
        with patch.object(runtime.Runner, 'run', side_effect=failed_after_response), \
             patch.object(runtime, '_native_transport', side_effect=lambda: httpx2.MockTransport(self.transport)):
            with self.assertRaises(RuntimeError): await self.factory.run_case(0, api_key='FIXTURE-NOT-A-REAL-KEY')
        self.assertEqual(self.calls, [512])
        self.assert_usage_tail(self.factory._runs[0], complete=True)
        self.assertEqual(self.ledger.get_run(self.factory._runs[0])['status'], 'failed')

    async def test_http_failure_stops_actual_runner_without_retry(self):
        def rejected(request):
            self.calls.append(1); return httpx2.Response(403, json={'error': {'message':'synthetic forbidden'}})
        with patch.object(runtime, '_native_transport', side_effect=lambda: httpx2.MockTransport(rejected)):
            with self.assertRaises(Exception): await self.factory.run_case(0, api_key='FIXTURE-NOT-A-REAL-KEY')
            with self.assertRaises(Exception): await self.factory.run_case(1, api_key='FIXTURE-NOT-A-REAL-KEY')
        self.assertEqual(self.calls, [1])
        self.assertTrue(self.factory._failed)
        self.assertTrue(self.factory._budget.summary()['halted'])
        self.assertIsNone(self.factory._budget.summary()['observed_cost_cny'])
        self.assert_usage_tail(self.factory._runs[0], complete=False)


if __name__ == '__main__': unittest.main()
