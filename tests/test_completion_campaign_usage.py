"""Isolated numeric SDK/native reconciliation, not the shared Runner integration."""
import unittest
from types import SimpleNamespace as Box
from researchops_completion_timing.campaign_usage import _project_case_usage, _usage_event
from researchops_external_closure.semantics import _MODEL_USAGE_FIELDS, _validate_model_usage_payload


def usage(i=5, o=1, c=0):
    return Box(input_tokens=i, output_tokens=o, total_tokens=i+o, input_tokens_details=Box(cached_tokens=c), requests=1, request_usage_entries=[])


class CampaignUsageTests(unittest.TestCase):
    def fixture(self):
        native = {'usage': {'normalized': {'input_tokens': 5, 'output_tokens': 1, 'total_tokens': 6, 'cached_input_tokens': 0, 'requests': 1}}}
        response = Box(usage=usage()); aggregate = usage(); aggregate.request_usage_entries = [usage()]
        return Box(raw_responses=[response], context_wrapper=Box(usage=aggregate)), [native]

    def project(self, result, records):
        return _project_case_usage(result, records, observed_response_count=1, terminal_attempt_count=1)

    def test_actual_aggregate_entry_can_align_with_one_request_response(self):
        result, records = self.fixture(); value = self.project(result, records)
        self.assertTrue(value['matched'])
        self.assertEqual(value['grouping_sources'], ('aggregate_single_request_alignment',))
        self.assertEqual(value['indices_by_response'], {0: (0,)})
        event = _usage_event(value, response_detail_count=1, provider='deepseek', transport='openai_compatible_responses')
        self.assertEqual(set(event), set(_MODEL_USAGE_FIELDS))
        _validate_model_usage_payload(event, model_rows=value['rows'], response_detail_count=1,
            plan=Box(provider_id='deepseek', transport_id='openai_compatible_responses'))

    def test_explicit_response_entries_are_checked_too(self):
        result, records = self.fixture(); result.raw_responses[0].usage.request_usage_entries = [usage()]
        value = self.project(result, records)
        self.assertTrue(value['matched']); self.assertEqual(value['grouping_sources'], ('response_request_entry',))

    def test_sdk_default_zero_does_not_override_native_missing_cache(self):
        result, records = self.fixture(); records[0]['usage']['normalized']['cached_input_tokens'] = None
        value = self.project(result, records)
        self.assertFalse(value['matched']); self.assertIsNone(value['rows'][0]['cached_tokens'])
        self.assertIsNone(_usage_event(value, response_detail_count=1, provider='deepseek', transport='openai_compatible_responses')['cached_input_unit_count'])

    def test_bad_aggregate_is_not_replaced_by_native_totals(self):
        result, records = self.fixture(); result.context_wrapper.usage.output_tokens = 99; result.context_wrapper.usage.total_tokens = 104
        value = self.project(result, records); self.assertFalse(value['matched'])
        event = _usage_event(value, response_detail_count=1, provider='deepseek', transport='openai_compatible_responses')
        self.assertEqual(event['output_unit_count'], 99)
        with self.assertRaises(ValueError):
            _validate_model_usage_payload(event, model_rows=value['rows'], response_detail_count=1,
                plan=Box(provider_id='deepseek', transport_id='openai_compatible_responses'))

    def test_missing_entries_are_not_synthesized(self):
        result, records = self.fixture(); result.context_wrapper.usage.request_usage_entries = []
        value = self.project(result, records)
        self.assertFalse(value['matched']); self.assertEqual(value['rows'], ())
        self.assertEqual(value['indices_by_response'], {0: ()})

    def test_unavailable_run_data_has_null_counts(self):
        value = self.project(None, [])
        self.assertIsNone(value['sdk_raw_response_count']); self.assertIsNone(value['sdk_usage_request_count'])
        event = _usage_event(value, response_detail_count=1, provider='deepseek', transport='openai_compatible_responses')
        self.assertFalse(event['usage_complete']); self.assertIsNone(event['input_unit_count'])

    def test_output_and_identifiers_are_never_read(self):
        result, records = self.fixture()
        class Response:
            usage = result.raw_responses[0].usage
            @property
            def output(self): raise AssertionError('output forbidden')
            @property
            def response_id(self): raise AssertionError('id forbidden')
        result.raw_responses = [Response()]
        self.assertTrue(self.project(result, records)['matched'])


if __name__ == '__main__': unittest.main()
