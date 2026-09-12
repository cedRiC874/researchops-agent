"""Internal v1.2 content path; no claim of timing/A/B or real execution proof."""
import copy
import hashlib
import inspect
import json
import unittest
from unittest.mock import patch

from researchops_external_closure import semantics
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import test_external_closure_semantics as legacy_tests

_h = legacy_tests._h


class TimedContentSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        legacy_tests.ExternalClosureSemanticTests.setUpClass()

    def setUp(self):
        self.helper = legacy_tests.ExternalClosureSemanticTests()

    def fixture(self, *, legacy_plan=False, timing_plan_commitment=None, **options):
        plan_commitment = timing_plan_commitment or _h('timed-plan')
        original = legacy_tests._external_plan
        def scoped_plan(*args, **kwargs):
            plan = original(*args, **kwargs)
            plan.update(timing_plan_commitment_sha256=plan_commitment,
                execution_scope=dict(mode='single_host_fixed_store', execution_environment_id='PCEENV-' + 'A' * 32,
                    local_claim_contract_sha256='10e6a6f50712b9f6b7bdf1bcf628f1745d2bfcf5723fd9f7d230ea682ee90695',
                    cross_host_execution_allowed=False, temporary_store_override_allowed=False))
            body = dict(plan); body.pop('external_plan_binding_sha256')
            plan['external_plan_binding_sha256'] = legacy_tests._domain_hash(legacy_tests.RUNTIME_PLAN_DOMAIN, raw(body))
            return plan
        if legacy_plan:
            value = self.helper._fixture(**options)
        else:
            with patch.object(legacy_tests, '_external_plan', side_effect=scoped_plan):
                value = self.helper._fixture(**options)
            value['telemetry']['closure'] = dict(claim_allowed=False, claim_scope='producer_payloads_only',
                reasons=['independent_outer_verification_required'])
        references = {}
        # Create new synthetic v1.2 rows and chains. No saved execution artifact
        # is read, rewritten, rescored or reclassified by this fixture builder.
        for row in value['event_rows']:
            if row['event_type'] not in semantics._TERMINAL_EVENT_TYPES:
                continue
            payload = json.loads(row['safe_payload_json'])
            index = str(payload['attempt_index'])
            reference = dict(timing_contract_sha256=semantics._TIMING_CONTRACT_SHA256,
                plan_commitment_sha256=plan_commitment, execution_binding_sha256=_h('timed-binding'),
                clock_domain_id='PCECLOCK-' + 'A' * 32, segment_commitment_sha256=_h('segment-' + index))
            references[index] = reference
            payload.update(schema_version='provider-completion-ledger-event/1.2', timing=copy.deepcopy(reference))
            row['safe_payload_json'] = raw(payload).decode()
        for ordinal in range(len(value['run_rows'])):
            self.helper._rehash_run(value, ordinal)
        return value, references

    def arguments(self, value):
        return dict(run_rows=value['run_rows'], event_rows=value['event_rows'], model_call_rows=value['model_call_rows'],
            persisted_evidence_context=value['context'], envelope_id=value['envelope_id'], campaign_id=value['campaign_id'],
            expected_runtime_plan=value['expected_runtime_plan'], authorization_grant_sha256=value['authorization_grant_sha256'],
            execution_input_order_commitment_sha256=value['execution_input_order_commitment_sha256'],
            api_origin='https://api.deepseek.com', api_path='/responses', model_id='deepseek-v4-flash',
            postrun_completed_at_utc='2026-09-04T03:30:00Z', authorization_consumed_at_utc='2026-09-04T02:50:00Z',
            task_released_at_utc='2026-09-04T02:55:00Z', provider_key_loaded_at_utc='2026-09-04T02:56:00Z')

    def verify(self, value, references, **changes):
        arguments = self.arguments(value) | changes
        return semantics._verify_artifact_content_semantics(value['telemetry'], value['audit_index'],
            terminal_timing_references=references, **arguments)

    def test_original_v12_rows_and_hashes_are_preserved_without_io(self):
        value, references = self.fixture()
        before = copy.deepcopy((value['event_rows'], value['audit_index'], references))
        with patch('builtins.open', side_effect=AssertionError('no file IO')), \
             patch('subprocess.Popen', side_effect=AssertionError('no subprocess')), \
             patch('socket.socket', side_effect=AssertionError('no network')):
            summary = self.verify(value, references)
        self.assertEqual(before, (value['event_rows'], value['audit_index'], references))
        self.assertTrue(summary.all_chains_valid)
        self.assertEqual(summary.accepted_response_count, 2)
        # Valid non-timing semantics are not full segment/duration validation.
        self.assertFalse(hasattr(summary, 'timing_gate_complete'))
        self.assertFalse(hasattr(summary, 'runtime_authority_granted'))

    def test_legacy_public_entry_still_rejects_v12_and_exposes_no_opt_in(self):
        value, _ = self.fixture(legacy_plan=True)
        self.assertNotIn('terminal_timing_references', inspect.signature(semantics.verify_artifact_content_semantics).parameters)
        with self.assertRaisesRegex(ValueError, 'closure_event_projection_invalid'):
            semantics.verify_artifact_content_semantics(value['telemetry'], value['audit_index'], **self.arguments(value))

    def test_reference_set_requires_exact_terminal_coverage(self):
        value, references = self.fixture()
        for changed in ({}, {'0': references['0']}, references | {'2': references['0']}):
            with self.subTest(keys=tuple(changed)), self.assertRaisesRegex(ValueError, 'closure_event_projection_invalid'):
                self.verify(value, changed)

    def test_reference_shape_and_binding_mismatch_reject(self):
        value, references = self.fixture()
        for change in ({'extra': None}, {'clock_domain_id': 'invalid'}, {'segment_commitment_sha256': 'f' * 64},
                       {'plan_commitment_sha256': '0' * 64}, {'timing_contract_sha256': 'f' * 64}):
            changed = copy.deepcopy(references)
            changed['0'].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'closure_event_projection_invalid'):
                self.verify(value, changed)

    def test_raw_timing_tamper_fails_original_hash_chain(self):
        value, references = self.fixture()
        row = next(row for row in value['event_rows'] if row['event_type'] in semantics._TERMINAL_EVENT_TYPES)
        payload = json.loads(row['safe_payload_json'])
        payload['timing']['segment_commitment_sha256'] = 'f' * 64
        row['safe_payload_json'] = raw(payload).decode()
        with self.assertRaisesRegex(ValueError, 'closure_audit_chain_invalid'):
            self.verify(value, references)

    def test_rehashed_wrong_terminal_versions_do_not_pass(self):
        for version in ('provider-completion-ledger-event/1.1', 'provider-completion-ledger-event/1.3'):
            value, references = self.fixture()
            row = next(row for row in value['event_rows'] if row['event_type'] in semantics._TERMINAL_EVENT_TYPES)
            payload = json.loads(row['safe_payload_json']); payload['schema_version'] = version
            row['safe_payload_json'] = raw(payload).decode()
            self.helper._rehash_run(value, 0)
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'closure_event_projection_invalid'):
                self.verify(value, references)

    def test_budget_and_tool_gates_are_not_bypassed_by_timing_references(self):
        value, references = self.fixture(input_tokens=60)
        summary = self.verify(value, references)
        self.assertEqual(summary.observed_input_tokens, 120)
        self.assertIn('observed_input_token_cap_exceeded', summary.semantic_closure_reasons)
        self.assertFalse(summary.semantic_closure_claim_allowed)
        with self.assertRaisesRegex(ValueError, 'closure_event_projection_invalid'):
            self.verify(value, references, tool_call_count=1)

    def test_rejected_responses_keep_unknown_usage_and_noneligible_semantics(self):
        value, references = self.fixture(rejected_responses=True)
        summary = self.verify(value, references)
        self.assertEqual(summary.accepted_response_count, 0)
        self.assertFalse(summary.usage_complete)
        self.assertFalse(summary.semantic_closure_claim_allowed)

    def test_rehashed_different_terminal_index_rejects_with_safe_semantic_error(self):
        value, references = self.fixture()
        row = next(row for row in value['event_rows'] if row['event_type'] in semantics._TERMINAL_EVENT_TYPES)
        payload = json.loads(row['safe_payload_json']); payload['attempt_index'] = 99
        row['safe_payload_json'] = raw(payload).decode()
        references['99'] = references.pop('0')
        value['audit_index']['runs'][0]['completion_telemetry_event_commitment']['all_started_attempts_terminal'] = False
        self.helper._rehash_run(value, 0)
        with self.assertRaisesRegex(ValueError, 'closure_event_projection_invalid'):
            self.verify(value, references)

    def test_v12_terminal_payload_limit_is_not_the_legacy_two_mb_limit(self):
        value, references = self.fixture()
        row = next(row for row in value['event_rows'] if row['event_type'] in semantics._TERMINAL_EVENT_TYPES)
        payload = json.loads(row['safe_payload_json'])
        payload['completion_record']['native_incomplete_details'] = {'reason': 'x' * 262144}
        row['safe_payload_json'] = raw(payload).decode()
        self.assertGreater(len(row['safe_payload_json'].encode()), 262144)
        self.assertLess(len(row['safe_payload_json'].encode()), 2_000_000)
        self.helper._rehash_run(value, 0)
        # Canonical, rehashed, structurally complete terminal: prove the byte
        # boundary runs BEFORE the (separate) nested-record semantic validator.
        with patch.object(semantics, 'validate_persisted_live_completion_record',
                          side_effect=AssertionError('oversized payload reached record validation')) as record_validator:
            with self.assertRaisesRegex(ValueError, 'closure_event_projection_invalid'):
                self.verify(value, references)
            self.assertEqual(record_validator.call_count, 0)

    def test_internal_reference_constants_match_the_pinned_ledger_contract(self):
        payload = (legacy_tests.ROOT / 'evals/provider_completion_timed_ledger_v1/contract_v1.json').read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), '5acc5b2d7d018202a5af42a736d178fa4e0cdc74b25a60a055d2883bc93b7002')
        contract = json.loads(payload)
        self.assertEqual(semantics._TIMED_TERMINAL_VERSION, contract['event_schema_version'])
        self.assertEqual(semantics._TIMING_CONTRACT_SHA256, contract['timing_contract_sha256'])
        self.assertEqual(semantics._MAX_TIMED_TERMINAL_PAYLOAD_BYTES, contract['max_terminal_payload_bytes'])
        self.assertEqual(semantics._TIMED_REFERENCE_FIELDS, set(contract['extension_fields']))
        self.assertEqual(semantics._TERMINAL_EVENT_TYPES, set(contract['allowed_terminal_events']))

    def test_rehashed_scoped_runtime_cannot_enable_other_hosts_or_store_overrides(self):
        value, _ = self.fixture()
        for change in ({'cross_host_execution_allowed': True}, {'temporary_store_override_allowed': True},
                       {'cross_host_execution_allowed': 0}, {'local_claim_contract_sha256': 'f' * 64},
                       {'execution_environment_id': 'wrong'}, {'extra': False}):
            plan = copy.deepcopy(value['expected_runtime_plan'])
            plan['execution_scope'].update(change)
            body = dict(plan); body.pop('external_plan_binding_sha256')
            plan['external_plan_binding_sha256'] = legacy_tests._domain_hash(legacy_tests.RUNTIME_PLAN_DOMAIN, raw(body))
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'closure_denominator_invalid'):
                semantics._validate_plan(plan, envelope_id=value['envelope_id'], campaign_id=value['campaign_id'], timed=True)

    def test_producer_marker_is_exact_and_cannot_be_a_success_or_incomplete_assertion(self):
        for marker in ({'claim_allowed': 0, 'claim_scope': 'producer_payloads_only', 'reasons': ['independent_outer_verification_required']},
                       {'claim_allowed': True, 'claim_scope': 'producer_payloads_only', 'reasons': []},
                       {'claim_allowed': False, 'claim_scope': 'producer_payloads_only', 'reasons': []}):
            value, references = self.fixture()
            value['telemetry']['closure'] = marker
            with self.subTest(marker=marker), self.assertRaisesRegex(ValueError, 'closure_outer_projection_invalid'):
                self.verify(value, references)


if __name__ == '__main__':
    unittest.main()
