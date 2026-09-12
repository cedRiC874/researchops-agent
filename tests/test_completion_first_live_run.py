"""Public invocation unit boundaries; no real environment Key or Provider use."""
import asyncio
import contextlib
import inspect
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from researchops_completion_timing import first_live_run as run
from researchops_completion_timing import first_live_start as start
from researchops_completion_timing.first_live_failure import validate_failure_receipt


class FirstLiveInvocationTests(unittest.TestCase):
    def test_public_signature_has_no_execution_overrides(self):
        self.assertEqual(list(inspect.signature(run.run_timed_first_live).parameters),
            ['plan_bytes', 'authorization_bytes', 'expected_authorization_binding_sha256', 'confirm_online'])

    def test_unconfirmed_api_never_prepares_or_loads_key(self):
        with patch.object(run, '_prepare_process_bound_first_live_start_impl') as prepare, \
                patch.object(run, '_load_configured_key') as key:
            result = asyncio.run(run.run_timed_first_live(plan_bytes=b'invalid', authorization_bytes=b'invalid',
                expected_authorization_binding_sha256='invalid'))
        prepare.assert_not_called(); key.assert_not_called()
        value = validate_failure_receipt(run.ROOT, result)
        self.assertEqual(value['dispatch_attempt_count'], 0)
        self.assertFalse(value['claim_consumed'])

    def test_unconfirmed_cli_never_reads_files(self):
        with patch.object(run, 'read_regular_file_no_follow') as reader, contextlib.redirect_stdout(io.StringIO()) as output:
            code = run.main(['--plan', 'missing', '--authorization', 'missing', '--expected-authorization-binding', 'invalid'])
        reader.assert_not_called()
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(output.getvalue())['status'], 'failed_before_dispatch')

    def test_cli_unknown_argument_is_not_echoed(self):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = run.main(['--FAKE-SECRET-MUST-NOT-ECHO'])
        self.assertEqual(code, 4)
        self.assertNotIn('FAKE-SECRET', output.getvalue() + stderr.getvalue())

    def test_help_returns_zero_without_failure_json(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run.main(['--help']), 0)
        self.assertNotIn('outcome_unknown', output.getvalue())

    def test_cli_budget_precedes_reads_and_is_not_replaced(self):
        events = []; budget = object()
        def read(*args, **kwargs): events.append('read'); return b'{}'
        async def invoke(**kwargs):
            self.assertIs(kwargs['budget'], budget)
            events.append('invoke')
            return b'{"status":"completed"}'
        with patch.object(run, '_StartBudget', side_effect=lambda: events.append('budget') or budget), \
                patch.object(run, 'read_regular_file_no_follow', side_effect=read), \
                patch.object(run, '_run_with_budget', side_effect=invoke), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run.main(['--plan', 'p', '--authorization', 'a', '--expected-authorization-binding', 'h', '--confirm-online']), 0)
        self.assertEqual(events, ['budget', 'read', 'read', 'invoke'])

    def test_schema_failure_fallback_is_canonical_and_sanitized(self):
        with patch.object(run, 'build_first_live_failure_receipt', side_effect=RuntimeError('FAKE-SECRET')):
            result = run._failure(RuntimeError('FAKE-SECRET'))
        value = validate_failure_receipt(run.ROOT, result)
        self.assertEqual(value['status'], 'outcome_unknown')
        self.assertNotIn(b'FAKE-SECRET', result)

    def test_process_preparation_reuses_existing_budget(self):
        budget = start._StartBudget()
        with patch.object(start._StartBudget, '__init__', side_effect=AssertionError('budget reset')) as constructor:
            with self.assertRaisesRegex(start.FirstLiveStartError, 'process_root_mismatch'):
                start._prepare_process_bound_first_live_start_impl(run.ROOT.parent, plan_bytes=b'{}', authorization_bytes=b'{}',
                    expected_authorization_binding_sha256='invalid', _budget=budget)
        constructor.assert_not_called()

    def test_key_loader_uses_only_configured_environment_name(self):
        with patch.object(run.os, 'environ', {'DEEPSEEK_API_KEY': ' FIXTURE-NOT-A-REAL-KEY '}):
            self.assertEqual(run._load_configured_key(), 'FIXTURE-NOT-A-REAL-KEY')
        with patch.object(run.os, 'environ', {'OTHER_KEY': 'FIXTURE-NOT-A-REAL-KEY'}):
            with self.assertRaisesRegex(start.FirstLiveStartError, 'key_unavailable'):
                run._load_configured_key()

    @unittest.skipUnless(os.name == 'nt', 'Windows fixed-directory locking')
    def test_work_paths_never_overwrite_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(run, 'ROOT', Path(temporary)):
            identifier = 'a' * 64
            with run._owned_work_paths(run.ROOT, identifier) as (database, archive, envelope):
                marker = database.parent / 'retained.txt'; marker.write_bytes(b'keep')
            with self.assertRaises(FileExistsError):
                with run._owned_work_paths(run.ROOT, identifier): self.fail('existing work accepted')
            self.assertEqual(marker.read_bytes(), b'keep')
            other = 'b' * 64
            existing = envelope.parent / (other + '.bundle.json'); existing.write_bytes(b'keep')
            with self.assertRaises(FileExistsError):
                with run._owned_work_paths(run.ROOT, other): self.fail('existing envelope accepted')
            self.assertEqual(existing.read_bytes(), b'keep')
            self.assertFalse((envelope.parent / (other + '.work')).exists())


class OwnedKeyPhaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_reference_released_before_phase_returns(self):
        events = []
        class Key:
            def __del__(self): events.append('released')
        async def next_request(*, api_key):
            self.assertIsInstance(api_key, Key); events.append('request')
        factory = SimpleNamespace(before_key_load=lambda: events.append('before'), key_loaded=lambda: events.append('loaded'), run_next=next_request)
        def load(): events.append('load'); return Key()
        with patch.object(run, '_load_configured_key', side_effect=load):
            self.assertIsNone(await run._owned_key_phase(factory))
        self.assertEqual(events, ['before', 'load', 'loaded', 'request', 'request', 'released'])

    async def test_first_failure_never_loads_or_retries_and_hides_text(self):
        events = []
        def before(): raise RuntimeError('FAKE-SECRET')
        factory = SimpleNamespace(before_key_load=before, _prepared=SimpleNamespace(abort=lambda: events.append('abort')))
        with patch.object(run, '_load_configured_key') as loader:
            result = await run._owned_key_phase(factory)
        loader.assert_not_called()
        self.assertTrue(factory._failed)
        self.assertEqual(events, ['abort'])
        self.assertNotIn(b'FAKE-SECRET', result)

    async def test_failed_request_releases_owned_key_and_does_not_start_second(self):
        events = []
        class Key:
            def __del__(self): events.append('released')
        async def next_request(*, api_key):
            self.assertIsInstance(api_key, Key)
            events.append('request')
            raise RuntimeError('FAKE-SECRET')
        factory = SimpleNamespace(before_key_load=lambda: None, key_loaded=lambda: None, run_next=next_request,
            _prepared=SimpleNamespace(abort=lambda: events.append('abort')))
        with patch.object(run, '_load_configured_key', side_effect=Key):
            result = await run._owned_key_phase(factory)
        self.assertEqual(events, ['request', 'abort', 'released'])
        self.assertNotIn(b'FAKE-SECRET', result)


if __name__ == '__main__':
    unittest.main()
