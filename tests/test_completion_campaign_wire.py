"""Exact input formatting checks, not network/whole-request/admission proof."""
import unittest
from pathlib import Path
from tests import test_completion_campaign_opening as fixtures
from researchops_completion_timing.campaign_wire import _render_case_input, _verify_wire_case_input

ROOT = Path(__file__).resolve().parents[1]


class CampaignWireTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.CampaignOpeningTests(); fixture.setUp()
        self.opened = fixture.verify()

    def test_rendering_keeps_task_text_and_appends_fixed_canary(self):
        value = _render_case_input(ROOT, self.opened, 0)
        self.assertEqual(value, self.opened._cases[0]['user_input'] + '\n\n[researchops-completion-canary:' + self.opened._canary_b64 + ']')
        result = _verify_wire_case_input(ROOT, self.opened, 0, [{'role': 'user', 'content': value}])
        self.assertEqual(result['execution_input_commitment_sha256'], self.opened.execution_input_commitments[0])
        self.assertTrue(result['input_and_canary_match'])
        self.assertFalse(result['full_request_validated'])
        self.assertFalse(result['runtime_authority_granted'])

    def test_omitted_canary_extra_message_or_wrong_case_is_rejected(self):
        correct = [{'role': 'user', 'content': _render_case_input(ROOT, self.opened, 0)}]
        for value in ([{'role': 'user', 'content': self.opened._cases[0]['user_input']}], correct * 2,
                      [{'role': 'user', 'content': _render_case_input(ROOT, self.opened, 1)}]):
            with self.subTest(count=len(value)), self.assertRaises(ValueError):
                _verify_wire_case_input(ROOT, self.opened, 0, value)

    def test_index_requires_exact_integer_in_range(self):
        for value in (True, 0.0, -1, 2):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _render_case_input(ROOT, self.opened, value)


if __name__ == '__main__': unittest.main()
