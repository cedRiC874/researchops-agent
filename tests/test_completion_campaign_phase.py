"""Private lifecycle integration: explicit admission stubs, fake Key, no network."""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx2

from researchops_completion_timing import campaign_phase as phase, campaign_runtime as runtime
from tests import test_completion_campaign_runner_bridge as bridge_fixture


class CampaignPhaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reuse fixture setup, not TestCase inheritance/duplicate test discovery.
        self.fixture = bridge_fixture.CampaignRunnerBridgeTests()
        self.addCleanup(self._cleanup_fixture)
        with patch.object(runtime._CampaignModelFactory, 'key_loaded'):
            await self.fixture.asyncSetUp()
        self.factory = self.fixture.factory
        self.original_loader = phase._load_configured_key
        self.loader = self.fixture.stack.enter_context(patch.object(phase, '_load_configured_key', return_value='FIXTURE-NOT-A-REAL-KEY'))
        self.fixture.stack.enter_context(patch.object(runtime, '_native_transport',
            side_effect=lambda: httpx2.MockTransport(self.fixture.transport)))

    def _cleanup_fixture(self):
        # This helper TestCase owns no asyncio runner. Its registered cleanups
        # are synchronous; run them in our lifecycle instead of its doCleanups.
        while self.fixture._cleanups:
            function, args, kwargs = self.fixture._cleanups.pop()
            function(*args, **kwargs)

    async def test_all_cases_reconciled_before_phase_and_denominator_complete(self):
        result = await phase._run_owned_campaign_phase(self.factory)
        self.assertEqual(result['status'], 'campaign_phase_completed')
        self.assertTrue(result['phase_completed'])
        self.assertFalse(result['artifact_published']); self.assertFalse(result['closure_claim_allowed'])
        self.assertEqual(self.fixture.calls, [512, 512]); self.loader.assert_called_once()
        state = self.factory._prepared.clock.snapshot()
        self.assertTrue(state['closed']); self.assertFalse(state['halted'])
        values = state['phase']
        self.assertLessEqual(values['task_released_ns'], values['key_loaded_ns'])
        self.assertLessEqual(values['post_cleanup_terminal_ns'], values['key_reference_released_ns'])
        self.assertLessEqual(values['key_reference_released_ns'], values['phase_terminal_ns'])
        self.assertEqual(self.factory._sealed_denominator.not_finalized_case_ids, ())
        self.assertEqual(len(self.factory._sealed_denominator.cases), 2)

    async def test_consumed_invocation_cannot_load_key_again(self):
        await phase._run_owned_campaign_phase(self.factory)
        with self.assertRaises(runtime.CampaignRuntimeError):
            await phase._run_owned_campaign_phase(self.factory)
        self.loader.assert_called_once(); self.assertEqual(len(self.fixture.calls), 2)

    async def test_pre_key_check_failure_does_not_load_key(self):
        with patch.object(self.factory, 'before_key_load', side_effect=ValueError('synthetic-secret')):
            result = await phase._run_owned_campaign_phase(self.factory)
        self.loader.assert_not_called(); self.assertEqual(self.fixture.calls, [])
        self.assertFalse(result['phase_completed']); self.assertNotIn('synthetic-secret', json.dumps(result))
        self.assertIsNone(self.factory._prepared.clock.snapshot()['phase']['key_loaded_ns'])

    async def test_key_load_failure_has_no_attempt_and_unknown_cleanup(self):
        self.loader.side_effect = ValueError('synthetic-secret')
        result = await phase._run_owned_campaign_phase(self.factory)
        state = self.factory._prepared.clock.snapshot()
        self.assertFalse(result['phase_completed']); self.assertEqual(state['attempts'], [])
        self.assertEqual(state['phase']['post_cleanup_status'], 'unknown')
        self.assertIsNone(state['phase']['post_cleanup_terminal_ns'])
        self.assertEqual(state['phase']['key_reference_release_status'], 'released')
        self.assertNotIn('synthetic-secret', json.dumps(result))

    async def test_http_failure_stops_before_second_case_and_does_not_seal_success(self):
        calls = []
        def rejected(request):
            calls.append(1); return httpx2.Response(403, json={'error': {'message':'synthetic-secret'}})
        with patch.object(runtime, '_native_transport', side_effect=lambda: httpx2.MockTransport(rejected)):
            result = await phase._run_owned_campaign_phase(self.factory)
        self.assertEqual(calls, [1]); self.assertFalse(result['phase_completed'])
        self.assertIsNone(self.factory._sealed_denominator)
        self.assertFalse(result['retry_authorized']); self.assertNotIn('synthetic-secret', json.dumps(result))
        self.fixture.assert_usage_tail(self.factory._runs[0], complete=False)

    async def test_cancellation_retains_unknown_outcome_without_retry(self):
        async def cancelled(request):
            self.fixture.calls.append(1); raise asyncio.CancelledError()
        with patch.object(runtime, '_native_transport', side_effect=lambda: httpx2.MockTransport(cancelled)):
            result = await phase._run_owned_campaign_phase(self.factory)
        # The session rejects its outcome-unknown terminal before the original
        # cancellation reaches this owner. Do not invent its original exception.
        self.assertEqual(result['error_code'], 'campaign_phase_failed')
        self.assertFalse(result['phase_completed']); self.assertEqual(self.fixture.calls, [1])
        self.assertEqual(self.factory._prepared.clock.snapshot()['attempts'][0]['terminal_kind'], 'outcome_unknown')

    async def test_missing_case_cannot_be_upgraded_by_finalizer(self):
        with patch.object(self.factory, 'run_case', return_value={'status':'completed'}):
            result = await phase._run_owned_campaign_phase(self.factory)
        self.assertEqual(result['error_code'], 'campaign_phase_finalization_failed')
        self.assertFalse(result['phase_completed']); self.assertIsNone(self.factory._sealed_denominator)

    async def test_key_frame_has_unwound_before_finalizer(self):
        import sys
        original = phase._complete_after_owned_key_release
        def checked(factory):
            frame = sys._getframe()
            while frame is not None:
                self.assertNotEqual(frame.f_code.co_name, '_owned_key_cases')
                frame = frame.f_back
            return original(factory)
        with patch.object(phase, '_complete_after_owned_key_release', side_effect=checked):
            result = await phase._run_owned_campaign_phase(self.factory)
        self.assertTrue(result['phase_completed'])

    def test_loader_uses_only_named_fake_environment(self):
        with patch.object(phase.os, 'environ', {'DEEPSEEK_API_KEY':' fixture '}):
            self.assertEqual(self.original_loader(), 'fixture')
        with patch.object(phase.os, 'environ', {}), self.assertRaises(runtime.CampaignRuntimeError):
            self.original_loader()

    async def test_terminal_overrun_cannot_be_a_successful_phase(self):
        clock = self.factory._prepared.clock
        original = clock.phase_terminal
        def late():
            with patch('researchops_completion_timing.clock.time.monotonic_ns',
                       return_value=clock._origin + clock._phase_cap + 1):
                return original()
        with patch.object(clock, 'phase_terminal', side_effect=late):
            result = await phase._run_owned_campaign_phase(self.factory)
        self.assertFalse(result['phase_completed']); self.assertTrue(clock.snapshot()['halted'])
        self.assertIsNone(self.factory._sealed_denominator)

    async def test_known_zero_phase_budget_never_enters_case_runner(self):
        with patch.object(self.factory._prepared.clock, 'remaining_phase_ns', return_value=0), \
             patch.object(self.factory, 'run_case') as runner:
            result = await phase._run_owned_campaign_phase(self.factory)
        runner.assert_not_called(); self.assertFalse(result['phase_completed'])

    async def test_previously_loaded_key_is_not_loaded_again(self):
        self.factory.key_loaded()
        with self.assertRaises(runtime.CampaignRuntimeError):
            await phase._run_owned_campaign_phase(self.factory)
        self.loader.assert_not_called(); self.assertEqual(self.fixture.calls, [])

    async def test_direct_owner_cancellation_does_not_escape_with_traceback(self):
        with patch.object(self.factory, 'run_case', side_effect=asyncio.CancelledError()):
            result = await phase._run_owned_campaign_phase(self.factory)
        self.assertEqual(result['error_code'], 'campaign_phase_cancelled')
        self.assertFalse(result['phase_completed']); self.assertEqual(self.fixture.calls, [])


if __name__ == '__main__': unittest.main()
