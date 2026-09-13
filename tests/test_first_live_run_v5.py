"""Explicit v5 invocation boundaries and isolated synthetic public-path checks."""
import contextlib
import inspect
import io
import json
import os
import unittest
from unittest.mock import patch

from researchops_completion_timing import first_live_run as runner
from tests import test_completion_first_live_runtime as process_fixture


class FirstLiveV5InvocationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_without_exact_confirmation_reads_no_inputs_or_key(self):
        for confirmation in (False, None, 1, 'true'):
            with self.subTest(confirmation=confirmation), \
                 patch.object(runner, '_prepare_process_bound_first_live_start_impl') as prepare, \
                 patch.object(runner, '_load_configured_key') as key, \
                 patch.object(runner, '_StartBudget') as budget:
                result = json.loads(await runner.run_timed_first_live_v5(plan_bytes=object(), authorization_bytes=object(),
                    expected_authorization_binding_sha256=object(), confirm_online=confirmation))
                self.assertEqual((prepare.call_count, key.call_count, budget.call_count), (0, 0, 0))
                self.assertFalse(result['claim_consumed'])

    async def test_invocation_exposes_no_key_transport_root_or_version_override(self):
        self.assertEqual(tuple(inspect.signature(runner.run_timed_first_live_v5).parameters),
            ('plan_bytes', 'authorization_bytes', 'expected_authorization_binding_sha256', 'confirm_online'))

    async def test_invalid_internal_version_rejects_before_preparation(self):
        with patch.object(runner, '_prepare_process_bound_first_live_start_impl') as prepare, \
             patch.object(runner, 'load_summary_schema') as schema:
            result = json.loads(await runner._run_with_budget(plan_bytes=b'', authorization_bytes=b'',
                expected_authorization_binding_sha256='f' * 64, budget=None, _implementation_version=True))
        self.assertEqual((prepare.call_count, schema.call_count), (0, 0))
        self.assertFalse(result['claim_consumed'])


class FirstLiveV5CliBoundaryTests(unittest.TestCase):
    def test_no_confirmation_or_abbreviated_flag_reads_no_input_files(self):
        base = ['--plan', 'not-read', '--authorization', 'not-read', '--expected-authorization-binding', 'f' * 64]
        for suffix in ([], ['--confirm-on']):
            with self.subTest(suffix=suffix), patch.object(runner, 'read_regular_file_no_follow') as read, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main_v5(base + suffix), 4)
                self.assertEqual(read.call_count, 0)


@unittest.skipUnless(os.name == 'nt', 'designated Windows environment')
class FirstLiveV5PublicProcessTests(unittest.TestCase):
    def run_process(self, mode):
        helper = process_fixture.FirstLiveRuntimeBoundaryTests(); self.addCleanup(helper.doCleanups)
        return helper.run_factory_process(mode, implementation_version=5)

    def test_public_v5_success_preserves_wire_format_and_publishes_eight_files(self):
        value = self.run_process('public_success')
        self.assertEqual(value['calls'], [256, 16])
        self.assertEqual(value['key_load_count'], 1)
        self.assertEqual(value['result']['status'], 'completed')
        self.assertEqual(value['result']['schema_version'], 'provider-completion-first-live-run/4.0')
        self.assertEqual(value['archive_count'], 8)
        self.assertFalse(value['result']['closure_claim_allowed'])

    def test_public_v5_http_error_stops_after_one_dispatch(self):
        value = self.run_process('public_http_error')
        self.assertEqual(value['calls'], [256])
        self.assertEqual(value['result']['status'], 'failed')
        self.assertEqual(value['result']['dispatch_attempt_count'], 1)
        self.assertTrue(value['failure_artifact']['audit_chain_bound'])
        self.assertIsNone(value['result']['token_usage'])

    def test_public_v5_key_loader_failure_never_dispatches(self):
        value = self.run_process('public_missing_key')
        self.assertEqual(value['calls'], [])
        self.assertEqual(value['key_load_count'], 1)
        self.assertEqual(value['result']['status'], 'failed_before_dispatch')
        self.assertEqual(value['result']['dispatch_attempt_count'], 0)
        self.assertTrue(value['failure_artifact']['audit_chain_bound'])


if __name__ == '__main__': unittest.main()
