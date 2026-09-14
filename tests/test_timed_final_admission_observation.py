"""Fast tests of test-only observation; no A/B fixture or production run is made."""
import ast
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from researchops_external_closure.errors import ExternalClosurePrimitiveError
from tests import test_timed_final_full as target

ROOT = Path(__file__).resolve().parents[1]
BASE = 'e96e7647827293a3da3ef235a4e2ea46d367d648'
TARGET_FILE = 'tests/test_timed_final_full.py'
METHOD = 'test_real_A_B_with_new_signatures_final_witness_and_exact_A_rerun'


class AdmissionObservationTests(unittest.TestCase):
    def test_real_arguments_and_return_identity_are_preserved(self):
        argument, keyword, answer = object(), object(), object()
        received, observations = [], []
        def actual(*args, **kwargs):
            received.append((args, kwargs))
            return answer
        observed = target._observe_admission(actual, observations)
        self.assertIs(observed(argument, key=keyword), answer)
        self.assertIs(received[0][0][0], argument)
        self.assertIs(received[0][1]['key'], keyword)
        self.assertEqual(observations, [dict(call=1, phase='initial_A', outcome='returned')])

    def test_original_exception_identity_and_safe_code_are_preserved(self):
        error = ExternalClosurePrimitiveError('external_closure_git_timeout')
        observations = []
        def actual():
            raise error
        with self.assertRaises(ExternalClosurePrimitiveError) as caught:
            target._observe_admission(actual, observations)()
        self.assertIs(caught.exception, error)
        self.assertEqual(observations[0]['exception_type'], 'ExternalClosurePrimitiveError')
        self.assertEqual(observations[0]['exception_code'], 'external_closure_git_timeout')
        self.assertEqual(observations[0]['code_status'], 'retained')

    def test_second_admission_is_identified_without_replacing_results(self):
        observations, values = [], iter([None, RuntimeError('message must not be emitted')])
        def actual():
            value = next(values)
            if isinstance(value, Exception):
                raise value
            return value
        observed = target._observe_admission(actual, observations)
        self.assertIsNone(observed())
        with self.assertRaises(RuntimeError):
            observed()
        self.assertEqual([row['phase'] for row in observations], ['initial_A', 'B_rerun_A'])
        self.assertNotIn('message must not be emitted', json.dumps(observations))

    def test_unsafe_codes_are_redacted_not_copied_to_failure_message(self):
        canaries = ['sk-fake-secret-canary', 'Bearer fake-authorization-canary',
            r'C:\Users\synthetic-user\private.txt', 'synthetic@example.test',
            'external_closure_' + 'a' * 64, 'external_closure_sk_fake_key',
            'external_closure_bearer_fake_token', 'traceback\nprivate-text']
        for canary in canaries:
            with self.subTest(kind=canaries.index(canary)):
                error = ExternalClosurePrimitiveError('external_closure_git_timeout')
                error.code = canary
                fields = target._safe_admission_error(error)
                self.assertIsNone(fields['exception_code'])
                self.assertEqual(fields['code_status'], 'redacted')
                self.assertNotIn(canary, json.dumps(fields))

    def test_exception_message_args_and_code_property_are_not_read(self):
        class UnsafeError(Exception):
            @property
            def code(self):
                raise AssertionError('code property must not run')
            def __str__(self):
                raise AssertionError('message must not be formatted')
            def __repr__(self):
                raise AssertionError('args must not be formatted')
        error = UnsafeError('sk-fake-secret-canary')
        fields = target._safe_admission_error(error)
        self.assertEqual(fields['exception_type'], 'unmapped')
        self.assertEqual(fields['code_status'], 'not_stored')
        self.assertNotIn('sk-fake-secret-canary', json.dumps(fields))

    def test_unknown_type_name_is_not_emitted(self):
        canary = 'sk-fake-secret-type-name'
        error = type(canary, (Exception,), {})()
        fields = target._safe_admission_error(error)
        self.assertEqual(fields['exception_type'], 'unmapped')
        self.assertNotIn(canary, json.dumps(fields))

    def test_metadata_failure_does_not_replace_original_exception(self):
        class UnsafeMetadataError(Exception):
            def __getattribute__(self, name):
                if name == '__dict__':
                    raise RuntimeError('metadata access must not mask original')
                return super().__getattribute__(name)
        error, observations = UnsafeMetadataError(), []
        def actual():
            raise error
        with self.assertRaises(UnsafeMetadataError) as caught:
            target._observe_admission(actual, observations)()
        self.assertIs(caught.exception, error)
        self.assertEqual(observations[0]['code_status'], 'metadata_unavailable')

    def test_missing_null_and_non_string_code_remain_distinct(self):
        uninitialized = ExternalClosurePrimitiveError.__new__(ExternalClosurePrimitiveError)
        self.assertEqual(target._safe_admission_error(uninitialized)['code_status'], 'not_stored')
        error = RuntimeError()
        self.assertEqual(target._safe_admission_error(error)['code_status'], 'not_stored')
        error.code = None
        self.assertEqual(target._safe_admission_error(error)['code_status'], 'null')
        error.code = {'sensitive': 'not printed'}
        self.assertEqual(target._safe_admission_error(error)['code_status'], 'redacted')

    def test_observation_collection_has_an_explicit_cap(self):
        observations = []
        observed = target._observe_admission(lambda: None, observations)
        for _ in range(25):
            self.assertIsNone(observed())
        self.assertEqual(len(observations), 5)
        self.assertEqual(observations[-1], dict(observation_truncated=True, max_recorded_calls=4))

    def test_only_owned_patch_is_restored(self):
        original = target.evaluator._verify_admission
        with patch.object(target.evaluator, '_verify_admission',
                          new=target._observe_admission(original, [])):
            self.assertIsNot(target.evaluator._verify_admission, original)
        self.assertIs(target.evaluator._verify_admission, original)

    def test_original_assertion_operands_and_count_are_unchanged(self):
        old = subprocess.check_output(['git', '--no-replace-objects', '-C', str(ROOT),
            'show', BASE + ':' + TARGET_FILE], timeout=30)
        new = (ROOT / TARGET_FILE).read_bytes()
        def assertions(source):
            tree = ast.parse(source)
            method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == METHOD)
            found = []
            for node in ast.walk(method):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    if node.func.value.id == 'self' and node.func.attr.startswith('assert'):
                        arity = 1 if node.func.attr in ('assertTrue', 'assertFalse') else 2
                        found.append((node.lineno, node.col_offset, node.func.attr,
                            tuple(ast.dump(value, include_attributes=False) for value in node.args[:arity])))
            return [(kind, args) for _, _, kind, args in sorted(found)]
        self.assertEqual(assertions(old), assertions(new))
        self.assertEqual(len(assertions(new)), 9)
