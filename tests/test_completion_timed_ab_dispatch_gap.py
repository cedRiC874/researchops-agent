"""Document the legacy A dispatch boundary; this is not a timed A/B fix."""
import unittest
from unittest.mock import patch

from researchops_completion_timing.execution_scope import verify_single_host_pre_execution
from researchops_external_closure import evaluator
from researchops_external_closure.types import PreReceiptRejected
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests.test_completion_execution_scope import ROOT, scoped_fixture
from tests.test_completion_timing_pre_execution import documents


class TimedABDispatchGapTests(unittest.TestCase):
    def test_valid_v3_pre_execution_graph_is_rejected_by_legacy_a_before_postrun(self):
        fixture,plan=scoped_fixture()
        data=documents(fixture)
        with patch('socket.socket',side_effect=AssertionError('network forbidden')):
            proof=verify_single_host_pre_execution(ROOT,data,external_observation_bundle=raw(fixture.observation),
                timing_plan_bytes=raw(plan),verification_time_utc='2026-09-04T00:00:17Z')
            self.assertEqual(proof.timing.preregistration_envelope_sha256,fixture.documents['preregistration_envelope']['document_sha256'])
            with patch.object(evaluator,'validate_postrun_attested_facts',side_effect=AssertionError('postrun not reached')) as postrun:
                result=evaluator.evaluate_closure_bundle(ROOT,data,external_observation_bundle=raw(fixture.observation),
                    artifact_directory=None,postrun_attested_facts=b'{}')
            postrun.assert_not_called()
        self.assertIs(type(result),PreReceiptRejected)
        self.assertEqual(result.error_code,'external_closure_document_schema_invalid')


if __name__=='__main__': unittest.main()
