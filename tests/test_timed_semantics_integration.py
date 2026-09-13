"""Actual content/timing composition on newly constructed synthetic raw rows."""
import copy
import json
import unittest
from unittest.mock import patch

from researchops_completion_timing import contract as timing
from researchops_external_closure import semantics, timed_semantics
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import test_timed_content_semantics as content_fixture
from tests import test_completion_timing_contract as time_fixture


class TimedSemanticsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        content_fixture.TimedContentSemanticsTests.setUpClass()

    def setUp(self):
        self.helper = content_fixture.TimedContentSemanticsTests(); self.helper.setUp()
        self.authoring = time_fixture.TimingAuthoringTests()

    def fixture(self, **options):
        seed, _ = self.helper.fixture(**options)
        frame = self.authoring.fixture()
        plan = frame['plan']; runtime = seed['expected_runtime_plan']
        plan['binding'].update(campaign_id=seed['campaign_id'], runtime_plan_core_sha256=timing.runtime_plan_core_commitment(runtime))
        plan.update(planned_case_handles=runtime['denominator_plan']['case_ids'],
            max_attempts=runtime['denominator_plan']['total_model_request_cap'],
            max_attempts_per_case=runtime['denominator_plan']['max_turns_per_case'],
            request_timeout_ns=runtime['transport_limits']['request_timeout_seconds'] * 1_000_000_000,
            phase_timeout_ns=runtime['transport_limits']['total_timeout_seconds'] * 1_000_000_000)
        plan['plan_commitment_sha256'] = timing.commitment('plan', plan)
        value, _ = self.helper.fixture(timing_plan_commitment=plan['plan_commitment_sha256'], **options)
        frame['evidence']['binding'] = dict(plan['binding'], authorization_grant_sha256=value['authorization_grant_sha256'],
            external_consumption_anchor_sha256=time_fixture.h('synthetic independent anchor'),
            runtime_plan_commitment_sha256=value['expected_runtime_plan']['external_plan_binding_sha256'])
        terminals = [json.loads(row['safe_payload_json']) for row in value['event_rows'] if row['event_type'] in semantics._TERMINAL_EVENT_TYPES]
        frame['evidence']['segments'] = frame['evidence']['segments'][:len(terminals)]
        for segment, event in zip(frame['evidence']['segments'], terminals):
            segment.update(case_handle=event['case_id'], completion_record_sha256=timing.digest(raw(event['completion_record'])),
                           usage_sha256=timing.digest(raw(event['completion_record']['usage'])))
        self.reseal(value, frame)
        return value, frame

    def reseal(self, value, frame):
        self.authoring.seal(frame)
        ordinal = 0
        for row in value['event_rows']:
            if row['event_type'] not in semantics._TERMINAL_EVENT_TYPES:
                continue
            segment = frame['evidence']['segments'][ordinal]; ordinal += 1
            payload = json.loads(row['safe_payload_json'])
            payload['timing'] = dict(timing_contract_sha256=timing.CONTRACT_SHA256,
                **{key: segment[key] for key in ('plan_commitment_sha256', 'execution_binding_sha256',
                                               'clock_domain_id', 'segment_commitment_sha256')})
            row['safe_payload_json'] = raw(payload).decode()
        for index in range(len(value['run_rows'])):
            self.helper.helper._rehash_run(value, index)
        heads = [dict(run_id=item['run_id'], final_chain_head_sha256=item['chain_verification']['chain_head'])
                 for item in value['audit_index']['runs']]
        frame['evidence']['audit_chain_heads_sha256'] = semantics._domain_hash(
            semantics._AUDIT_RUN_SET_DOMAIN, b'final_chain_heads', raw(heads))
        self.authoring.seal(frame)

    def arguments(self, value, frame):
        return dict(telemetry=value['telemetry'], audit_index=value['audit_index'],
            semantic_inputs=self.helper.arguments(value) | dict(tool_call_count=0, tool_attempt_count=0, approval_decision_count=0),
            timing_plan_bytes=raw(frame['plan']), timing_evidence_bytes=raw(frame['evidence']),
            publication_bytes=raw(frame['publication']), expected_manifest_file_sha256=time_fixture.h('manifest'),
            expected_timing_plan_commitment_sha256=frame['plan']['plan_commitment_sha256'],
            expected_timing_execution_binding_sha256=frame['evidence']['execution_binding_sha256'])

    def verify(self, value, frame, **changes):
        with patch('socket.socket', side_effect=AssertionError('no network')):
            return timed_semantics.verify_timed_content_and_timing(time_fixture.ROOT, **(self.arguments(value, frame) | changes))

    def test_actual_content_and_timing_verifiers_preserve_rows_without_claiming_admission(self):
        value, frame = self.fixture()
        before = copy.deepcopy((value['event_rows'], value['audit_index'], frame))
        result = self.verify(value, frame)
        self.assertTrue(result.content_and_timing_complete)
        self.assertTrue(result.timing_gate_complete)
        self.assertEqual(result.content.accepted_response_count, 2)
        self.assertEqual(before, (value['event_rows'], value['audit_index'], frame))
        for name in ('container_origin_verified', 'signed_expectations_verified', 'source_admission_verified',
                     'actual_clock_capture_independently_proved', 'runtime_authority_granted', 'closure_claim_allowed'):
            self.assertFalse(getattr(result, name))

    def test_fully_rehashed_overlap_is_rejected_by_actual_timing(self):
        value, frame = self.fixture()
        frame['evidence']['segments'][1]['attempt_started_ns'] = 89
        self.reseal(value, frame)
        with self.assertRaisesRegex(ValueError, 'timing_concurrency_overlap'):
            self.verify(value, frame)

    def test_fully_rehashed_request_overrun_is_rejected(self):
        value, frame = self.fixture()
        segment = frame['evidence']['segments'][1]
        end = segment['send_started_ns'] + frame['plan']['request_timeout_ns'] + 1
        segment.update(transport_terminal_ns=end, raw_cleanup_terminal_ns=end + 1, lifetime_ended_ns=end + 1)
        frame['evidence']['phase'].update(post_cleanup_terminal_ns=end + 2, key_reference_released_ns=end + 3, phase_terminal_ns=end + 4)
        frame['publication'].update(sealing_started_ns=end + 5, sealing_finished_ns=end + 10)
        self.reseal(value, frame)
        with self.assertRaisesRegex(ValueError, 'timing_request_timeout_exceeded'):
            self.verify(value, frame)

    def test_rehashed_segment_cannot_claim_a_different_record(self):
        value, frame = self.fixture()
        frame['evidence']['segments'][0]['completion_record_sha256'] = 'f' * 64
        self.reseal(value, frame)
        with self.assertRaisesRegex(ValueError, 'timing_projection_mismatch'):
            self.verify(value, frame)

    def test_rehashed_evidence_cannot_claim_different_chain_heads(self):
        value, frame = self.fixture()
        frame['evidence']['audit_chain_heads_sha256'] = 'f' * 64
        self.authoring.seal(frame)
        with self.assertRaisesRegex(ValueError, 'timing_audit_heads_mismatch'):
            self.verify(value, frame)

    def test_missing_publication_remains_incomplete(self):
        value, frame = self.fixture()
        result = self.verify(value, frame, publication_bytes=None)
        self.assertFalse(result.content_and_timing_complete)
        self.assertIn('publication_unverified', result.timing_incomplete_reasons)

    def test_empty_observed_denominator_cannot_pass_vacuously(self):
        value, frame = self.fixture(observed_cases=0)
        result = self.verify(value, frame)
        self.assertEqual(result.content.observed_run_count, 0)
        self.assertFalse(result.content.full_plan_observed)
        self.assertFalse(result.timing_gate_complete)
        self.assertFalse(result.content_and_timing_complete)
        self.assertIn('planned_case_without_accepted_response', result.timing_incomplete_reasons)

    def test_valid_timing_does_not_erase_content_budget_failure(self):
        value, frame = self.fixture(input_tokens=60)
        result = self.verify(value, frame)
        self.assertTrue(result.timing_gate_complete)
        self.assertFalse(result.content_and_timing_complete)
        self.assertIn('observed_input_token_cap_exceeded', result.content.semantic_closure_reasons)

    def test_arbitrary_producer_success_does_not_replace_recomputation(self):
        value, frame = self.fixture()
        value['telemetry']['closure']['claim_allowed'] = True
        with patch.object(timing, 'validate_documents', side_effect=AssertionError('content gate comes first')) as called:
            with self.assertRaisesRegex(ValueError, 'closure_outer_projection_invalid'):
                self.verify(value, frame)
            self.assertEqual(called.call_count, 0)

    def test_verdict_or_reference_injection_is_not_an_input(self):
        value, frame = self.fixture()
        arguments = self.arguments(value, frame)
        for name in ('verified', 'verifier', 'terminal_timing_references'):
            arguments['semantic_inputs'][name] = True
            with self.subTest(field=name), self.assertRaisesRegex(ValueError, 'timed_content_input_invalid'):
                timed_semantics.verify_timed_content_and_timing(time_fixture.ROOT, **arguments)
            del arguments['semantic_inputs'][name]


if __name__ == '__main__':
    unittest.main()
