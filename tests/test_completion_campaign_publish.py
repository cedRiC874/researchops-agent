"""Native temporary Windows publication with fake Provider/admission fixtures."""
import json
import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace as Box
from unittest.mock import patch

from researchops_completion_timing import campaign_publish as publish, first_live_publish as exclusive
from tests import test_completion_campaign_artifacts as artifact_fixture


@unittest.skipUnless(os.name == 'nt', 'designated Windows filesystem')
class CampaignPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.helper = artifact_fixture.CampaignArtifactProducerTests()
        # Transfer fixture cleanup to this actual test's loop, not a second runner.
        self.addCleanup(lambda: self.helper.helper._cleanup_fixture())
        await self.helper.asyncSetUp()
        self.factory = self.helper.factory
        self.directory = self.helper.helper.fixture.root / 'published'
        self.stack = self.helper.helper.fixture.stack
        self.identity = self.stack.enter_context(patch.object(publish, '_check_publication_identity'))

    def run_publish(self):
        return publish._publish_completed_campaign(self.factory,self.directory)

    def test_actual_exclusive_writer_and_independent_readback(self):
        result = self.run_publish()
        self.assertEqual(result['status'],'campaign_archive_written_and_verified')
        self.assertEqual(len(result['created_files']),7)
        self.assertEqual(set(result['created_files']),{path.name for path in self.directory.iterdir()})
        self.assertTrue(self.factory._publication_finished)
        self.assertFalse(result['closure_claim_allowed']); self.assertFalse(result['publication_self_write_measured'])
        self.assertEqual(self.identity.call_count,2)
        publication = json.loads((self.directory/'completion_timing_publication.json').read_bytes())
        self.assertEqual(publication['sealing_started_ns'],self.factory._sealing_started_ns)
        self.assertLessEqual(publication['sealing_finished_ns']-publication['sealing_started_ns'],self.factory._sealing_timeout_ns)
        with self.assertRaisesRegex(publish.CampaignPublishError,'already_started'):
            publish._publish_completed_campaign(self.factory,self.directory.parent/'alternate')
        self.assertFalse((self.directory.parent/'alternate').exists())

    def test_existing_directory_never_overwritten(self):
        self.directory.mkdir(); (self.directory/'keep.txt').write_bytes(b'keep')
        with self.assertRaises(publish.CampaignPublishError) as caught: self.run_publish()
        self.assertEqual(caught.exception.code,'campaign_publish_destination_exists')
        self.assertFalse(caught.exception.directory_created); self.assertEqual(caught.exception.created_files,())
        self.assertEqual((self.directory/'keep.txt').read_bytes(),b'keep')
        self.assertEqual(len(list(self.directory.iterdir())),1)

    def test_partial_first_payload_is_retained_and_consumes_invocation(self):
        original = os.write; writes = []
        def broken(fd,data):
            writes.append(1)
            if len(writes)==1: return original(fd,data[:3])
            raise OSError('Authorization: Bearer FIXTURE-SECRET')
        with patch.object(exclusive.os,'write',side_effect=broken):
            with self.assertRaises(publish.CampaignPublishError) as caught: self.run_publish()
        error = caught.exception
        self.assertTrue(error.directory_created); self.assertEqual(error.created_files,('phase6_audit.sqlite3',))
        self.assertEqual((self.directory/'phase6_audit.sqlite3').stat().st_size,3)
        self.assertNotIn('FIXTURE-SECRET',str(error)); self.assertFalse(error.retry_authorized)
        with self.assertRaisesRegex(publish.CampaignPublishError,'already_started'): self.run_publish()

    def test_initial_identity_failure_precedes_any_output_write(self):
        self.identity.side_effect = publish.CampaignPublishError('campaign_publish_source_changed')
        with self.assertRaisesRegex(publish.CampaignPublishError,'source_changed'): self.run_publish()
        self.assertFalse(self.directory.exists())

    def test_final_identity_failure_keeps_files_without_success(self):
        self.identity.side_effect = [None,publish.CampaignPublishError('campaign_publish_source_changed')]
        with self.assertRaises(publish.CampaignPublishError) as caught: self.run_publish()
        self.assertEqual(len(caught.exception.created_files),7)
        self.assertFalse(self.factory._publication_finished)
        self.assertEqual(len(list(self.directory.iterdir())),7)

    def test_expired_authorization_cannot_start_sealing_writes(self):
        self.factory._prepared._validity.expires = '2000-01-01T00:00:00Z'
        with self.assertRaises(publish.CampaignPublishError): self.run_publish()
        self.assertFalse(self.directory.exists())

    def test_publication_receipt_write_cannot_start_a_second_budget(self):
        original = publish._write_exclusive
        def delayed(path,payload,created):
            original(path,payload,created)
            if path.name=='completion_timing_publication.json':
                terminal = self.factory._prepared.clock._origin+self.factory._sealing_started_ns+self.factory._sealing_timeout_ns+1
                self.stack.enter_context(patch('researchops_completion_timing.clock.time.monotonic_ns',return_value=terminal))
        with patch.object(publish,'_write_exclusive',side_effect=delayed):
            with self.assertRaisesRegex(publish.CampaignPublishError,'timeout') as caught: self.run_publish()
        self.assertEqual(len(caught.exception.created_files),7)
        self.assertFalse(self.factory._publication_finished)

    def test_independent_verifier_rejection_is_not_ignored(self):
        with patch.object(publish,'verify_timing_artifact_bundle',side_effect=ValueError('fixture mismatch')) as verifier:
            with self.assertRaises(publish.CampaignPublishError) as caught: self.run_publish()
        verifier.assert_called_once(); self.assertEqual(len(caught.exception.created_files),7)
        self.assertFalse(self.factory._publication_finished)


class PublicationIdentityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        expected=Box(source_integrity_commitment_sha256='a'*64,source_manifest_commitment_sha256='b'*64,execution_commit='c'*40)
        self.factory=Box(_prepared=Box(source_identity=expected,request_bytes=b'{}',receipt_bytes=b'fixture receipt',
                                      _verified_envelope=lambda:{}))
        self.checkpoint=self.stack.enter_context(patch.object(publish,'_publication_checkpoint',return_value=('fixture',1)))
        self.current=self.stack.enter_context(patch.object(publish,'verify_current_timed_profile',return_value=Box(
            source_integrity_commitment_sha256='a'*64,implementation_commitment_sha256='b'*64,
            component_hashes={'dependency_lock_sha256':'d'*64,'pyproject_sha256':'e'*64})))
        self.head=self.stack.enter_context(patch.object(publish,'_head',return_value='c'*40))
        self.environment=self.stack.enter_context(patch.object(publish,'verify_process_environment'))
        self.claim=self.stack.enter_context(patch.object(publish,'read_reserved_local_claim',return_value=b'fixture receipt'))

    def test_closed_phase_identity_still_checks_source_lock_and_actual_claim(self):
        publish._check_publication_identity(self.factory)
        self.current.assert_called_once_with(publish.ROOT,profile='campaign')
        self.environment.assert_called_once_with(publish.ROOT,expected_dependency_lock_sha256='d'*64,expected_pyproject_sha256='e'*64)
        self.claim.assert_called_once_with(b'{}',expected_receipt_sha256=publish.digest(b'fixture receipt'))
        self.assertEqual(self.checkpoint.call_count,2)

    def test_changed_head_rejected_before_environment_or_claim(self):
        self.head.return_value='f'*40
        with self.assertRaisesRegex(publish.CampaignPublishError,'source_changed'):
            publish._check_publication_identity(self.factory)
        self.environment.assert_not_called(); self.claim.assert_not_called()

    def test_changed_claim_is_not_replaced_by_supplied_receipt(self):
        self.claim.return_value=b'different fixture'
        with self.assertRaisesRegex(publish.CampaignPublishError,'claim_changed'):
            publish._check_publication_identity(self.factory)
        self.assertEqual(self.checkpoint.call_count,1)


if __name__ == '__main__': unittest.main()
