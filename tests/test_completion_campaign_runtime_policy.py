"""Wire-policy unit tests; admission/current checks are intentionally isolated."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import httpx2

from researchops_completion_timing import campaign_runtime as runtime
from researchops_completion_timing.campaign_wire import _render_case_input
from researchops_external_closure.semantics import _SEND_FIELDS
from tests import test_completion_campaign_opening as opening_fixture


class CampaignRuntimePolicyTests(unittest.TestCase):
    def setUp(self):
        opening = opening_fixture.CampaignOpeningTests(); opening.setUp()
        self.factory = object.__new__(runtime._CampaignModelFactory)
        value = self.factory
        value._opening = opening.verify(); value._case_index = 0
        value._execution = opening.envelope['execution_binding']; value._plan = opening.envelope['runtime_plan']
        value._task_owner = SimpleNamespace(abort=lambda: None)
        value._prepared = SimpleNamespace(proof=SimpleNamespace(scope=SimpleNamespace(timing=SimpleNamespace(authorization_grant_sha256='a' * 64))))
        value._failed = False; value._network_count = 0; value._send_pending = None; value._request_sha = 'b' * 64
        value._reservation = SimpleNamespace(request_index=0, output_token_cap=512)
        value._active = SimpleNamespace(_attempt_handle=SimpleNamespace(case_id=value._opening._cases[0]['case_handle'], attempt_index=0, case_attempt_index=0))
        # This test isolates wire policy; actual capability/SQLite validation is
        # exercised without a stub in test_completion_campaign_runner_bridge.
        value._active._capability = object()
        value._ledger = SimpleNamespace(_create_transport_send_capability=lambda payload, **kwargs: payload)
        self.body = dict(model='deepseek-v4-flash', input=[{'role':'user','content':_render_case_input(runtime.ROOT, value._opening, 0)}],
                         include=[], tools=[], max_output_tokens=512, store=False)

    def check(self, body=None, url='https://api.deepseek.com/responses'):
        with patch.object(self.factory, '_check_current'):
            self.factory._before_send(self.factory._active, httpx2.Request('POST', url, json=self.body if body is None else body))

    def test_send_metadata_has_exact_frozen_shape_and_no_private_input(self):
        self.check()
        payload = self.factory._send_pending
        self.assertEqual(set(payload), set(_SEND_FIELDS))
        self.assertEqual(payload['requested_output_token_cap'], 512)
        self.assertNotIn('input', payload)
        self.assertFalse(payload['request_body_persisted'])

    def test_unplanned_replay_tools_storage_and_cap_are_rejected(self):
        for field, value in (('instructions', 'unplanned'), ('previous_response_id', 'resp_old'), ('tools', [{'type':'function'}]),
                             ('store', True), ('max_output_tokens', 511), ('max_output_tokens', 512.0), ('stream', True)):
            body = copy.deepcopy(self.body); body[field] = value
            self.factory._send_pending = None; self.factory._failed = False
            with self.subTest(field=field, value=value), self.assertRaises(runtime.CampaignRuntimeError): self.check(body)

    def test_wrong_origin_query_or_missing_canary_cannot_be_sent(self):
        for url in ('https://other.invalid/responses', 'https://api.deepseek.com/responses?extra=1'):
            self.factory._failed = False
            with self.subTest(url=url), self.assertRaises(runtime.CampaignRuntimeError): self.check(url=url)
        body = copy.deepcopy(self.body); body['input'][0]['content'] = 'missing canary'
        with self.assertRaises(ValueError): self.check(body)


if __name__ == '__main__': unittest.main()
