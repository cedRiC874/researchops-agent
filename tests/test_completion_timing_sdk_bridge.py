"""Actual SDK + private Adapter function + MockTransport + temporary SQLite.

Runtime bindings and clock origins are test fixtures, not consumed authorization.
The public Provider factory must continue to reject this pending session.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import httpx2
from agents import ModelSettings, OpenAIResponsesModel
from openai import AsyncOpenAI

from researchops.audit import AuditError, AuditLedger
from researchops.model_providers import DeepSeekProvider, ProviderConfigurationError, _isolate_openai_compatible_client, _responses_model
from researchops_completion_telemetry.capture import CompletionTelemetryCollector
from researchops_completion_timing.clock import _TimingClock
from researchops_completion_timing.contract import commitment, digest
from researchops_completion_timing.session import _PendingTimedLedgerSession, _PendingTimedSessionTransport
from researchops_external_closure.primitives import canonical_json_bytes
from tests.test_completion_telemetry_ledger import _test_runtime_binding, _plan_binding
from tests.test_deepseek_completion_first_live_validation import _response_body


class TimingSdkBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Keep socket.socket a type: Windows Proactor uses isinstance on the
        # loop's self-pipe. Block connection/DNS operations, not that class.
        self.network = ExitStack(); self.addCleanup(self.network.close)
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo",
                       "asyncio.BaseEventLoop.create_connection"):
            self.network.enter_context(patch(target, side_effect=AssertionError("real network forbidden")))
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.ledger = AuditLedger(Path(self.temp.name) / "audit.sqlite3")
        self.run = self.ledger.start_run(mode="timed-sdk-mock", request_summary={"purpose": "synthetic integration"})
        runtime = _test_runtime_binding()
        case = "PCECASE-" + "A" * 32
        self.plan = _plan_binding(runtime, case_ids=(case,))
        self.tracker = CompletionTelemetryCollector.for_runtime(self.plan)
        self.clock = _TimingClock(request_timeout_ns=100_000_000, phase_timeout_ns=10_000_000_000)
        self.clock.task_released(); self.clock.key_loaded()
        self.session = _PendingTimedLedgerSession(self.tracker.bind_case(case), ledger=self.ledger, run_id=self.run,
            runtime_plan_binding=self.plan, clock=self.clock,
            timing_binding={"plan_commitment_sha256": "1" * 64, "execution_binding_sha256": "2" * 64,
                            "clock_domain_id": "PCECLOCK-" + "A" * 32})
        self.calls = 0

    def client(self, handler):
        transport = _PendingTimedSessionTransport(httpx2.MockTransport(handler), self.session)
        http = httpx2.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)
        client = AsyncOpenAI(api_key="FIXTURE-NOT-A-REAL-KEY", admin_api_key="", organization="", project="",
            webhook_secret="", base_url="https://api.deepseek.com", max_retries=0, http_client=http)
        _isolate_openai_compatible_client(client)
        return client

    async def invoke(self, client, *, input_text='synthetic'):
        model = _responses_model(OpenAIResponsesModel, model="deepseek-v4-flash", openai_client=client,
                                 session=self.session, api_key="FIXTURE-NOT-A-REAL-KEY")
        return await model._fetch_response(system_instructions=None, input=input_text, model_settings=ModelSettings(max_tokens=16),
                                         tools=[], output_schema=None, handoffs=[])

    def handler(self, _):
        self.calls += 1
        return httpx2.Response(200, json=_response_body("completed", 16), headers={"x-request-id": "fixture-id"})

    def terminal(self):
        return self.ledger.export_run(self.run)["events"][-1]["safe_payload"]

    async def test_sdk_adapter_clock_record_usage_and_sqlite_terminal_are_linked(self):
        async with self.client(self.handler) as client:
            response = await self.invoke(client)
            self.assertEqual(response.status, "completed")
        self.clock.post_cleanup_terminal(status="completed")
        self.clock.key_reference_released(status="released")
        self.clock.phase_terminal()
        self.assertEqual(self.calls, 1)
        event = self.terminal()
        self.assertEqual(event["schema_version"], "provider-completion-ledger-event/1.2")
        self.assertEqual(event["terminal_kind"], "response_accepted")
        segment = json.loads(self.session.segment_bytes()[0])
        self.assertEqual(segment["segment_commitment_sha256"], commitment("segment", segment))
        self.assertEqual(segment["segment_commitment_sha256"], event["timing"]["segment_commitment_sha256"])
        self.assertEqual(segment["completion_record_sha256"], digest(canonical_json_bytes(event["completion_record"])))
        self.assertEqual(segment["usage_sha256"], digest(canonical_json_bytes(event["completion_record"]["usage"])))
        self.assertLessEqual(segment["send_started_ns"], segment["transport_terminal_ns"])
        self.assertLessEqual(segment["transport_terminal_ns"], segment["raw_cleanup_terminal_ns"])
        self.assertTrue(self.ledger.verify_chain(self.run).valid)
        self.assertNotIn("FIXTURE-NOT-A-REAL-KEY", json.dumps(self.ledger.export_run(self.run)))
        self.assertNotIn("output", event)

    async def test_public_provider_still_rejects_pending_timing_without_consumed_gate(self):
        with self.assertRaises(ProviderConfigurationError):
            async with DeepSeekProvider().open_model(model_id="deepseek-v4-flash", api_key="FIXTURE-NOT-A-REAL-KEY",
                                                    completion_telemetry_session=self.session):
                self.fail("public timing path was enabled")
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.session.segment_bytes(), ())

    async def test_custodian_canary_echo_is_rejected_before_sqlite_write(self):
        marker = 'CUSTODIAN-OPAQUE-CANARY-0123456789ABCDEF'
        self.session._capture_canaries = (marker,)
        def handler(_):
            self.calls += 1
            body = _response_body('completed', 16)
            body['incomplete_details'] = {'reason': 'x' * 600 + marker}
            return httpx2.Response(200, json=body)
        async with self.client(handler) as client:
            with self.assertRaises(ProviderConfigurationError): await self.invoke(client)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.terminal()['terminal_kind'], 'response_rejected')
        self.assertNotIn('completion_record', self.terminal())
        self.assertNotIn(marker.encode(), Path(self.ledger.database_path).read_bytes())
        self.assertNotIn(marker, json.dumps(self.ledger.export_run(self.run)))
        self.assertTrue(self.ledger.verify_chain(self.run).valid)

    async def test_canary_policy_is_snapshotted_before_send(self):
        marker = 'CUSTODIAN-SNAPSHOT-CANARY-0123456789'
        self.session._capture_canaries = (marker,)
        def handler(_):
            self.calls += 1
            self.session._capture_canaries = ()  # Must not retroactively weaken this response's scan.
            return httpx2.Response(200, json=_response_body('completed', 16), headers={'x-request-id': marker})
        async with self.client(handler) as client:
            with self.assertRaises(ProviderConfigurationError): await self.invoke(client)
        self.assertEqual(self.terminal()['terminal_kind'], 'response_rejected')
        self.assertNotIn(marker.encode(), Path(self.ledger.database_path).read_bytes())
        self.assertEqual(self.calls, 1)

    async def test_fixed_canary_input_reaches_actual_sdk_wire_shape(self):
        from tests import test_completion_campaign_opening as opening_fixture
        from researchops_completion_timing.campaign_wire import _render_case_input, _verify_wire_case_input
        fixture = opening_fixture.CampaignOpeningTests(); fixture.setUp()
        opened = fixture.verify(); root = Path(__file__).resolve().parents[1]
        self.session._capture_canaries = (opened._canary_b64,)
        observed = []
        def handler(request):
            observed.append(_verify_wire_case_input(root, opened, 0, json.loads(request.content)['input']))
            self.calls += 1
            return httpx2.Response(200, json=_response_body('completed', 16))
        async with self.client(handler) as client:
            await self.invoke(client, input_text=_render_case_input(root, opened, 0))
        self.assertEqual(self.calls, 1)
        self.assertTrue(observed[0]['input_and_canary_match'])
        self.assertFalse(observed[0]['full_request_validated'])
        self.assertNotIn(opened._canary_b64.encode(), Path(self.ledger.database_path).read_bytes())

    async def test_invalid_canary_policy_never_dispatches(self):
        self.session._capture_canaries = ['not-a-tuple']
        async with self.client(self.handler) as client:
            with self.assertRaises(ProviderConfigurationError): await self.invoke(client)
            with self.assertRaises(ProviderConfigurationError): await self.invoke(client)
        self.assertEqual(self.calls, 0)
        self.assertTrue(self.clock.snapshot()['halted'])
        self.assertEqual(self.terminal()['terminal_kind'], 'no_response')

    async def test_real_sdk_timeout_path_writes_outcome_unknown_once_without_retry(self):
        async def blocked(_):
            self.calls += 1
            await asyncio.Event().wait()
        async with self.client(blocked) as client:
            with self.assertRaises(ProviderConfigurationError):
                await asyncio.wait_for(self.invoke(client), timeout=2)
            with self.assertRaises(ProviderConfigurationError):
                await self.invoke(client)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.terminal()["terminal_kind"], "outcome_unknown")
        self.assertEqual(len(self.session.segment_bytes()), 1)
        self.assertTrue(self.ledger.verify_chain(self.run).valid)

    async def test_after_send_cancellation_is_unknown_not_no_send_cancelled(self):
        sent = asyncio.Event()
        async def blocked(_):
            self.calls += 1
            sent.set()
            await asyncio.Event().wait()
        async with self.client(blocked) as client:
            task = asyncio.create_task(self.invoke(client))
            try:
                await asyncio.wait_for(sent.wait(), timeout=2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.terminal()["terminal_kind"], "outcome_unknown")
        self.assertIsNotNone(json.loads(self.session.segment_bytes()[0])["send_started_ns"])

    async def test_complete_body_then_cleanup_timeout_is_rejected_response(self):
        class Body(httpx2.AsyncByteStream):
            async def __aiter__(self):
                yield json.dumps(_response_body("completed", 16)).encode()
            async def aclose(self):
                await asyncio.Event().wait()
        def handler(_):
            self.calls += 1
            return httpx2.Response(200, stream=Body())
        async with self.client(handler) as client:
            # Exercise the phase-budget clamp without waiting the additional
            # five-second cleanup ceiling; this remains a synthetic boundary.
            with patch.object(self.clock, "remaining_phase_ns", return_value=2_000_000):
                with self.assertRaises(ProviderConfigurationError):
                    await asyncio.wait_for(self.invoke(client), timeout=2)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.terminal()["terminal_kind"], "response_rejected")
        segment = json.loads(self.session.segment_bytes()[0])
        self.assertEqual(segment["raw_cleanup_status"], "timed_out")
        self.assertIsNotNone(segment["response_index"])
        self.assertIsNone(segment["completion_record_sha256"])

    async def test_observed_late_complete_body_is_not_returned_to_the_planner(self):
        current = [time.monotonic_ns()]
        class Body(httpx2.AsyncByteStream):
            async def __aiter__(self):
                yield json.dumps(_response_body("completed", 16)).encode()
                current[0] += 100_000_001
            async def aclose(self):
                pass
        def handler(_):
            self.calls += 1
            return httpx2.Response(200, stream=Body())
        with patch("researchops_completion_timing.clock.time.monotonic_ns", side_effect=lambda: current[0]):
            async with self.client(handler) as client:
                with self.assertRaises(ProviderConfigurationError):
                    await self.invoke(client)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.terminal()["terminal_kind"], "response_rejected")
        segment = json.loads(self.session.segment_bytes()[0])
        self.assertGreater(segment["transport_terminal_ns"] - segment["send_started_ns"], 100_000_000)
        self.assertNotIn("completion_record", self.terminal())

    async def test_write_failure_poisoning_preserves_segment_and_prevents_more_requests(self):
        async with self.client(self.handler) as client:
            with patch.object(AuditLedger, "append_timed_completion_telemetry_event", side_effect=AuditError("fixture_write_failed", "fixture")):
                with self.assertRaises(ProviderConfigurationError):
                    await self.invoke(client)
            with self.assertRaises(ProviderConfigurationError):
                await self.invoke(client)
        self.assertEqual(self.calls, 1)
        self.assertTrue(self.session.failed)
        self.assertEqual(len(self.session.segment_bytes()), 1)
        self.assertFalse(self.session.event_commitment()["all_started_attempts_terminal"])
        self.assertTrue(self.clock.snapshot()["halted"])


if __name__ == "__main__":
    unittest.main()
