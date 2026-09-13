"""Private-read branch tests with real files; admission stubs are explicit."""
import base64
import copy
import os
import pickle
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import campaign_tasks as tasks
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import test_completion_campaign_opening as opening_fixture
from tests import test_completion_campaign_start as start_fixture


@unittest.skipUnless(os.name == 'nt', 'local Windows private package')
class CampaignPrivateReadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'custodian-opening.json'
        opening = opening_fixture.CampaignOpeningTests(); opening.setUp()
        self.opening = opening
        self.package = dict(schema_version='provider-completion-custodian-opening/1.0', bundle=opening.bundle,
            secret_salt_b64=base64.b64encode(opening.salt).decode(), seen_task_digests=list(opening.seen))
        self.path.write_bytes(raw(self.package))
        prepared = start_fixture.CampaignOwnershipTests().holder(envelope_bytes=raw(opening.envelope))
        self.prepared = prepared
        # Only IO ordering, parsing, retention and local path behavior here.
        # This stub is not proof of source/environment/store admission.
        self.guard = patch.object(tasks._CampaignTaskOwner, '_check_current')
        self.guard.start(); self.addCleanup(self.guard.stop)
        self.owner = tasks._CampaignTaskOwner(prepared)

    def test_claim_handoff_and_release_marker_precede_private_bytes(self):
        original = tasks.read_regular_file_no_follow; observations = []
        def read(path, **kwargs):
            if path == self.path:
                observations.append((self.prepared._taken, self.prepared.clock.snapshot()['phase']['task_released_ns']))
            return original(path, **kwargs)
        with patch.object(tasks, 'read_regular_file_no_follow', side_effect=read):
            summary = self.owner.open_tasks(self.path)
        self.assertEqual(len(observations), 2)
        self.assertTrue(all(taken and stamp is not None for taken, stamp in observations))
        self.assertTrue(summary['opening_verified']); self.assertEqual(summary['case_count'], 2)
        self.assertFalse(summary['key_loaded']); self.assertEqual(summary['provider_calls'], 0)
        self.assertFalse(summary['runtime_authority_granted'])

    def test_failed_recheck_never_reads_private_file(self):
        with patch.object(self.owner, '_check_current', side_effect=ValueError('synthetic source drift')), \
             patch.object(tasks, 'read_regular_file_no_follow') as reader:
            with self.assertRaises(tasks.CampaignTaskReleaseError): self.owner.open_tasks(self.path)
        reader.assert_not_called()
        self.assertFalse(self.owner.summary()['opening_verified'])
        self.assertTrue(self.prepared.clock.snapshot()['halted'])

    def test_second_open_is_rejected_without_another_read(self):
        self.owner.open_tasks(self.path)
        with patch.object(tasks, 'read_regular_file_no_follow') as reader:
            with self.assertRaisesRegex(tasks.CampaignTaskReleaseError, 'already_attempted'): self.owner.open_tasks(self.path)
        reader.assert_not_called()
        self.assertFalse(self.owner.summary()['opening_verified'])

    def test_bad_package_fails_without_echo_or_key(self):
        self.package['extra'] = 'FAKE-PRIVATE-MARKER'
        self.path.write_bytes(raw(self.package))
        with self.assertRaises(tasks.CampaignTaskReleaseError) as caught: self.owner.open_tasks(self.path)
        self.assertNotIn('FAKE-PRIVATE-MARKER', str(caught.exception))
        self.assertTrue(self.owner.summary()['private_read_attempted'])
        self.assertFalse(self.owner.summary()['opening_verified'])
        self.assertIsNone(self.prepared.clock.snapshot()['phase']['key_loaded_ns'])

    def test_changed_second_snapshot_invalidates_opening(self):
        original = tasks.read_regular_file_no_follow; count = [0]
        def read(path, **kwargs):
            value = original(path, **kwargs)
            if path == self.path:
                count[0] += 1
                if count[0] == 2: return b'{}'
            return value
        with patch.object(tasks, 'read_regular_file_no_follow', side_effect=read):
            with self.assertRaisesRegex(tasks.CampaignTaskReleaseError, 'package_changed'): self.owner.open_tasks(self.path)
        self.assertFalse(self.owner.summary()['opening_verified'])

    def test_named_stream_path_rejects_before_private_read(self):
        with patch.object(tasks, 'read_regular_file_no_follow') as reader:
            with self.assertRaisesRegex(tasks.CampaignTaskReleaseError, 'path_invalid'):
                self.owner.open_tasks(Path(str(self.path) + ':stream'))
        reader.assert_not_called()

    def test_profile_file_limit_is_applied(self):
        self.owner._profile = dict(self.owner._profile, maximum_package_bytes=1)
        with self.assertRaises(tasks.CampaignTaskReleaseError): self.owner.open_tasks(self.path)
        self.assertFalse(self.owner.summary()['opening_verified'])

    def test_salt_limit_and_canonical_encoding_are_enforced(self):
        self.package['secret_salt_b64'] = base64.b64encode(b'x' * 4097).decode()
        self.path.write_bytes(raw(self.package))
        with self.assertRaises(tasks.CampaignTaskReleaseError): self.owner.open_tasks(self.path)

    def test_private_owner_cannot_be_copied_pickled_or_print_contents(self):
        self.owner.open_tasks(self.path)
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with self.assertRaises(TypeError): operation(self.owner)
        self.assertNotIn(self.opening.bundle['cases'][0]['user_input'], repr(self.owner))
        self.assertNotIn(self.opening.bundle['leak_canary_b64'], repr(self.owner))

    def test_budget_handoff_requires_opening_and_cannot_reset_owner_budget(self):
        from researchops_completion_timing.campaign_budget import _budget_for_opened_owner
        self.owner.open_tasks(self.path)
        budget = _budget_for_opened_owner(self.owner)
        budget.begin_case(0)
        self.assertEqual(budget.reserve_request().output_token_cap, 512)
        with self.assertRaises(tasks.CampaignTaskReleaseError): _budget_for_opened_owner(self.owner)
        self.assertTrue(self.prepared.clock.snapshot()['halted'])

    def test_post_opening_budget_swap_is_rejected_and_owner_halted(self):
        from researchops_completion_timing.campaign_budget import _budget_for_opened_owner
        self.owner.open_tasks(self.path)
        replacement = copy.deepcopy(self.opening.envelope)
        replacement['runtime_plan']['budget_policy']['input_token_limit_total'] = 999999
        self.prepared.envelope_bytes = raw(replacement)
        with self.assertRaisesRegex(ValueError, 'envelope_changed'): _budget_for_opened_owner(self.owner)
        self.assertTrue(self.prepared.clock.snapshot()['halted'])
        self.assertTrue(self.owner.summary()['failed'])


if __name__ == '__main__': unittest.main()
