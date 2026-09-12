"""Summary conformance and outer-write timing; neither is Provider evidence."""
import copy
import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from researchops_completion_timing import first_live_run as run
from researchops_completion_timing.first_live_summary import validate_invocation_summary
from researchops_completion_timing.first_live_publish import FirstLivePublishError
from researchops_external_closure.primitives import canonical_json_bytes as raw


class FirstLiveSummaryTests(unittest.TestCase):
    def setUp(self):
        self.value = dict(schema_version='provider-completion-first-live-run/4.0', status='completed', summary_only=True,
            authorization_id_sha256='a' * 64, artifact_directory='output/first-live-v4/' + 'a' * 64,
            bundle_path='output/first-live-v4/' + 'a' * 64 + '.bundle.json', bundle_commitment_sha256='b' * 64,
            dispatch_attempt_count=2, model_quality_claim_allowed=False, provider_registration_authorized=False,
            runtime_authority_granted=False, closure_claim_allowed=False, retry_authorized=False, resume_authorized=False)

    def test_canonical_summary_is_not_admission(self):
        value = validate_invocation_summary(run.ROOT, raw(self.value))
        self.assertTrue(value['summary_only'])
        self.assertFalse(value['runtime_authority_granted'])

    def test_extra_sensitive_field_authority_or_path_mismatch_is_rejected(self):
        for changes in ({'message_content': 'not allowed'}, {'closure_claim_allowed': True},
                        {'artifact_directory': 'C:/absolute/path'}, {'authorization_id_sha256': 'c' * 64},
                        {'dispatch_attempt_count': 1}, {'dispatch_attempt_count': True}, {'summary_only': False}):
            value = copy.deepcopy(self.value); value.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_invocation_summary(run.ROOT, raw(value))

    def test_digest_with_trailing_newline_is_not_a_sha256(self):
        value = copy.deepcopy(self.value)
        value['bundle_commitment_sha256'] += '\n'
        with self.assertRaises(ValueError):
            validate_invocation_summary(run.ROOT, raw(value))


@unittest.skipUnless(os.name == 'nt', 'Windows path locking')
class OuterEnvelopeTimingTests(unittest.TestCase):
    def test_expired_before_write_creates_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'bundle.json'
            prepared = SimpleNamespace(_budget=Mock(), _expires='fixture', clock=Mock())
            prepared.clock._sealing_offset.return_value = 30_000_000_001
            with self.assertRaises(FirstLivePublishError):
                run._publish_envelope(prepared, path, {'sealing_started_ns': 0, 'bundle_bytes': b'{}'})
            self.assertFalse(path.exists())

    def test_outer_write_overrun_preserves_file_but_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'bundle.json'
            prepared = SimpleNamespace(_budget=Mock(), _expires='fixture', clock=Mock())
            prepared.clock._sealing_offset.side_effect = [0, 30_000_000_001]
            with self.assertRaises(FirstLivePublishError):
                run._publish_envelope(prepared, path, {'sealing_started_ns': 0, 'bundle_bytes': b'{}'})
            self.assertEqual(path.read_bytes(), b'{}')


if __name__ == '__main__': unittest.main()
