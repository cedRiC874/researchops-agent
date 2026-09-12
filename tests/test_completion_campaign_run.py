"""Public API/packet boundaries with explicit offline mocks, never real Key/API."""
import asyncio
import inspect
import io
import json
import os
import unittest
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import campaign_run as run, campaign_start as start
from researchops_completion_timing.admission import TimedAdmissionInputs
from researchops_external_closure.types import PreReceiptDocumentBytes
from researchops_external_closure.primitives import canonical_json_bytes as raw


def valid_shape():
    admission=TimedAdmissionInputs(artifact_directory=run.ROOT/'fixture',bundle_bytes=b'{}',review_bytes=b'{}',
        trust_bytes=b'{}',observation_bytes=b'{}',expected_review_document_sha256='a'*64,expected_trust_manifest_sha256='b'*64,
        expected_authorization_binding_sha256='c'*64,expected_candidate_freeze_receipt_sha256='d'*64,
        expected_candidate_frozen_at_utc='2026-09-08T00:00:00Z',expected_first_live_completed_at_utc='2026-09-08T00:00:00Z',
        verification_time_utc='2026-09-08T00:00:00Z')
    return dict(documents=PreReceiptDocumentBytes(**{field.name:b'{}' for field in fields(PreReceiptDocumentBytes)}),
        external_observation_bundle=b'{}',timing_plan_bytes=b'{}',admission_inputs=admission,task_package_path=run.ROOT/'never-read-private.json')


def packet():
    args=valid_shape(); admission=args['admission_inputs']
    return dict(schema_version='provider-completion-campaign-invocation-input/1.0',
        documents={field.name:getattr(args['documents'],field.name).decode() for field in fields(PreReceiptDocumentBytes)},
        external_observation_bundle='{}',timing_plan='{}',task_package_path=str(args['task_package_path']),
        admission={field.name:(getattr(admission,field.name).decode() if type(getattr(admission,field.name)) is bytes
                              else str(getattr(admission,field.name))) for field in fields(TimedAdmissionInputs)})


class CampaignPublicBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_signature_has_only_frozen_keyword_inputs(self):
        signature=inspect.signature(run.run_timed_campaign)
        self.assertEqual(list(signature.parameters),run.load_invocation_contract()['public_api']['keyword_only_parameters'])
        self.assertTrue(all(value.kind is inspect.Parameter.KEYWORD_ONLY for value in signature.parameters.values()))

    async def test_no_exact_confirmation_reads_nothing_or_prepares_claim(self):
        for confirmation in (False,None,1,'true'):
            with self.subTest(confirmation=confirmation),patch.object(run,'load_invocation_contract') as loader, \
                 patch.object(start,'_prepare_process_bound_campaign_start') as preparation:
                value=json.loads(await run.run_timed_campaign(**{name:object() for name in valid_shape()},confirm_online=confirmation))
            loader.assert_not_called(); preparation.assert_not_called()
            self.assertEqual(value['status'],'not_confirmed')

    async def test_bad_types_reject_before_preparation(self):
        args=valid_shape(); args['documents']=object()
        with patch.object(start,'_prepare_process_bound_campaign_start') as preparation:
            value=json.loads(await run.run_timed_campaign(**args,confirm_online=True))
        preparation.assert_not_called(); self.assertFalse(value['claim_consumed'])
        self.assertEqual(value['error_code'],'campaign_invocation_inputs_invalid')

    async def test_admission_exception_consumption_is_not_invented(self):
        for error,expected in ((start.CampaignStartError('fixture',claim_consumed=True),True),
                               (start.CampaignStartError('fixture',claim_consumed=False),False),
                               (ValueError('Authorization: Bearer FIXTURE-SECRET'),None)):
            with self.subTest(expected=expected),patch.object(start,'_prepare_process_bound_campaign_start',side_effect=error):
                payload=await run.run_timed_campaign(**valid_shape(),confirm_online=True)
            value=run.validate_invocation_summary(payload)
            self.assertIs(value['claim_consumed'],expected); self.assertIsNone(value['native_dispatch_count'])
            self.assertNotIn(b'FIXTURE-SECRET',payload)


class CampaignPacketTests(unittest.TestCase):
    def test_packet_preserves_embedded_document_bytes(self):
        value=packet(); value['documents']['trust_manifest']='{  "test": 1 }\n'
        args=run._decode_packet(raw(value),run.load_invocation_contract())
        self.assertEqual(args['documents'].trust_manifest,b'{  "test": 1 }\n')

    def test_extra_fields_bad_documents_and_path_reject(self):
        for change in ('extra','object','path'):
            value=packet()
            if change=='extra': value['api_key']='FIXTURE-SECRET'
            if change=='object': value['documents']['trust_manifest']={}
            if change=='path': value['task_package_path']='relative'
            with self.subTest(change=change),self.assertRaises(ValueError): run._decode_packet(raw(value),run.load_invocation_contract())

    def test_cli_without_confirmation_does_not_read_even_supplied_packet(self):
        output=io.StringIO()
        with patch.object(run,'read_regular_file_no_follow') as reader,patch.object(run,'load_invocation_contract') as loader,patch.object(run.sys,'stdout',output):
            code=run.main(['--packet','C:/never-read-private.json'])
        reader.assert_not_called(); loader.assert_not_called()
        self.assertEqual(code,2); self.assertEqual(output.getvalue(),run._not_confirmed_summary().decode())

    def test_cli_bad_arguments_do_not_echo_secret_value(self):
        output=io.StringIO()
        with patch.object(run.sys,'stdout',output): code=run.main(['--confirm-online','--api-key','FIXTURE-SECRET'])
        self.assertEqual(code,4); self.assertNotIn('FIXTURE-SECRET',output.getvalue())

    def test_cli_unexpected_execution_error_keeps_consumption_unknown(self):
        output=io.StringIO()
        profile=run.load_invocation_contract()
        async def failure(**kwargs): raise RuntimeError('FIXTURE-SECRET')
        with patch.object(run,'read_regular_file_no_follow',return_value=raw(packet())), \
             patch.object(run,'load_invocation_contract',return_value=profile), \
             patch.object(run,'run_timed_campaign',side_effect=failure),patch.object(run.sys,'stdout',output):
            code=run.main(['--confirm-online','--packet',str(run.ROOT/'fake-packet.json')])
        value=json.loads(output.getvalue()); self.assertEqual(code,5)
        self.assertIsNone(value['claim_consumed']); self.assertNotIn('FIXTURE-SECRET',output.getvalue())


@unittest.skipUnless(os.name=='nt','native Windows work and publication paths')
class CampaignPublicIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests import test_completion_campaign_artifacts as artifacts_fixture
        from tests import test_completion_campaign_start as start_fixture
        from researchops_completion_timing import campaign_tasks as tasks, campaign_publish as publish
        self.helper=artifacts_fixture.CampaignArtifactProducerTests()
        await self.helper.asyncSetUp()
        self.addCleanup(self.helper.helper._cleanup_fixture)
        self.stack=self.helper.helper.fixture.stack
        old=self.helper.factory
        self.prepared=start_fixture.CampaignOwnershipTests().holder(envelope_bytes=old._prepared.envelope_bytes)
        self.prepared.proof=old._prepared.proof
        self.prepared.proof.scope.authorization_id_sha256='9'*64
        self.prepared.plan_bytes=old._prepared.plan_bytes
        self.prepared.clock_domain_id=old._prepared.clock_domain_id
        self.root=self.helper.helper.fixture.root/'public-root'; self.root.mkdir()
        self.stack.enter_context(patch.object(run,'ROOT',self.root))
        self.stack.enter_context(patch.object(start,'_prepare_process_bound_campaign_start',return_value=self.prepared))
        self.stack.enter_context(patch.object(publish,'_check_publication_identity'))
        def opened(owner,path):
            # Only admission/private opening are stubs; the later factory,
            # Runner, Key-phase owner, DB handles, publisher and verifier are real.
            owner._attempted=True; owner._opened=old._opening
            owner._prepared.clock.task_released()
        self.stack.enter_context(patch.object(tasks._CampaignTaskOwner,'open_tasks',opened))
        self.arguments=valid_shape()
        self.arguments['timing_plan_bytes']=self.prepared.plan_bytes
        self.summary_path=self.root/'output'/'campaign-v1'/('9'*64+'.invocation.json')

    async def invoke(self):
        return json.loads(await run.run_timed_campaign(**self.arguments,confirm_online=True))

    async def test_public_api_returns_complete_only_after_disk_snapshot_and_close(self):
        value=await self.invoke()
        self.assertEqual(value['status'],'completed')
        self.assertEqual((value['run_count'],value['native_dispatch_count']),(2,2))
        self.assertTrue(value['final_workspace_close_observed'])
        disk=json.loads(self.summary_path.read_bytes())
        self.assertEqual(disk['status'],'pre_return_archive_verified')
        self.assertFalse(disk['final_workspace_close_observed'])
        self.assertEqual(disk['receipt_write_status'],'excluded_self_write')
        self.assertEqual(disk['manifest_file_sha256'],value['manifest_file_sha256'])
        self.assertFalse(value['closure_claim_allowed'])

    async def test_final_workspace_failure_cannot_rewrite_pre_return_snapshot(self):
        from contextlib import contextmanager
        from researchops_completion_timing import campaign_workspace as workspace
        original=workspace._new_database
        @contextmanager
        def failed_close(path):
            with original(path) as database: yield database
            raise OSError('FIXTURE-SECRET')
        with patch.object(workspace,'_new_database',failed_close): value=await self.invoke()
        self.assertEqual(value['status'],'failed')
        self.assertEqual(value['error_code'],'campaign_invocation_finalization_failed')
        self.assertIsNone(value['manifest_file_sha256']); self.assertIsNone(value['archive_relative_path'])
        self.assertEqual(json.loads(self.summary_path.read_bytes())['status'],'pre_return_archive_verified')
        self.assertNotIn('FIXTURE-SECRET',json.dumps(value))

    async def test_summary_write_failure_retains_archive_without_success(self):
        from researchops_completion_timing import first_live_publish as exclusive
        original=exclusive._write_exclusive
        def broken(path,payload,created):
            if path.name.endswith('.invocation.json'): raise OSError('FIXTURE-SECRET')
            return original(path,payload,created)
        with patch.object(exclusive,'_write_exclusive',side_effect=broken): value=await self.invoke()
        self.assertEqual(value['status'],'failed'); self.assertIsNone(value['manifest_file_sha256'])
        self.assertFalse(self.summary_path.exists())
        self.assertEqual(len(list((self.summary_path.parent/('9'*64)).iterdir())),7)

    async def test_task_open_failure_prevents_key_load_and_work_creation(self):
        from researchops_completion_timing import campaign_tasks as tasks
        loader=self.helper.helper.loader; before=loader.call_count
        with patch.object(tasks._CampaignTaskOwner,'open_tasks',side_effect=ValueError('FIXTURE-SECRET')):
            value=await self.invoke()
        self.assertEqual(value['error_code'],'campaign_invocation_task_opening_failed')
        self.assertEqual(loader.call_count,before); self.assertFalse((self.root/'output').exists())
        self.assertTrue(value['claim_consumed']); self.assertIsNone(value['run_count'])


if __name__=='__main__': unittest.main()
