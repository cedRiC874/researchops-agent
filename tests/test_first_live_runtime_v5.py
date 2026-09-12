"""V5 exact-type boundaries and real isolated SDK/MockTransport/archive flow."""
import os
import inspect
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from researchops_completion_timing import first_live_runtime as runtime, first_live_start as start
from researchops.audit import AuditLedger
from tests import test_completion_first_live_runtime as fixture_module


class FirstLiveV5FactoryBoundaryTests(unittest.TestCase):
    def test_unchecked_preparation_and_unrecognized_subclasses_are_rejected(self):
        with self.assertRaisesRegex(start.FirstLiveStartError, 'preparation_required'):
            runtime._FirstLiveModelFactoryV5(object(), object())
        class Unrecognized(runtime._FirstLiveModelFactoryV5): pass
        with self.assertRaisesRegex(start.FirstLiveStartError, 'preparation_required'):
            Unrecognized(object(), object())

    def test_factory_signature_has_no_caller_source_verifier_parameter(self):
        self.assertEqual(tuple(inspect.signature(runtime._FirstLiveModelFactoryV5).parameters), ('prepared', 'ledger'))
        self.assertIsNot(runtime._FirstLiveModelFactoryV5, runtime._FirstLiveModelFactory)

    def test_v5_factory_does_not_take_a_legacy_prepared_owner(self):
        prepared = object.__new__(start._ClaimedFirstLiveStart)
        prepared._environment = {'synthetic': True}
        with tempfile.TemporaryDirectory() as temporary:
            ledger = AuditLedger(Path(temporary) / 'test.sqlite3')
            with patch.object(start._ClaimedFirstLiveStart, '_take_for_runtime', side_effect=AssertionError('must not take legacy owner')) as take:
                with self.assertRaisesRegex(start.FirstLiveStartError, 'preparation_required'):
                    runtime._FirstLiveModelFactoryV5(prepared, ledger)
                self.assertEqual(take.call_count, 0)


@unittest.skipUnless(os.name == 'nt', 'designated Windows environment')
class FirstLiveV5ProcessRuntimeTests(unittest.TestCase):
    def run_process(self, mode):
        helper = fixture_module.FirstLiveRuntimeBoundaryTests()
        self.addCleanup(helper.doCleanups)
        return helper.run_factory_process(mode, implementation_version=5)

    def test_v5_mock_responses_publish_verified_eight_file_archive(self):
        observed = self.run_process('publish')
        self.assertEqual(observed['calls'], [256, 16])
        self.assertEqual(observed['states'], ['completed', 'incomplete_length'])
        self.assertEqual(observed['timed_versions'], ['provider-completion-ledger-event/1.2'] * 2)
        self.assertTrue(observed['chain_valid'])
        self.assertEqual(observed['publication']['file_count'], 8)
        self.assertEqual(observed['publication']['status'], 'first_live_archive_written_and_verified')
        self.assertFalse(observed['publication']['authority'])

    def test_v5_http_failure_is_observed_once_and_never_retried(self):
        observed = self.run_process('http_error')
        self.assertEqual(observed['calls'], [256])
        self.assertEqual(observed['terminal_kinds'], ['http_error'])
        self.assertTrue(observed['factory_halted'])
        self.assertTrue(observed['chain_valid'])
        self.assertEqual(observed['failure_receipts'][0]['dispatch_attempt_count'], 1)
        self.assertIsNone(observed['failure_receipts'][0]['token_usage'])

    def test_v5_source_drift_stops_further_dispatch(self):
        observed = self.run_process('source_change')
        self.assertEqual(observed['calls'], [256])
        self.assertTrue(observed['factory_halted'])
        self.assertTrue(observed['chain_valid'])


if __name__ == '__main__': unittest.main()
