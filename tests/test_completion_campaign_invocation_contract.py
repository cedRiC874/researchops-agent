"""Offline frozen summary states; no runtime invocation, Provider or Key."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import campaign_run as run
from researchops_completion_timing.admission import TimedAdmissionInputs
from researchops_external_closure.types import PreReceiptDocumentBytes
from researchops_external_closure.primitives import canonical_json_bytes as raw


class CampaignInvocationContractTests(unittest.TestCase):
    def setUp(self):
        self.network=patch('socket.socket',side_effect=AssertionError('network forbidden'))
        self.network.start(); self.addCleanup(self.network.stop)
        self.completed=dict(schema_version='provider-completion-campaign-invocation/1.0',status='completed',
            observation_scope='local_invocation_only',authorization_id_sha256='a'*64,claim_consumed=True,
            phase_completed=True,archive_status='verified',final_workspace_close_observed=True,
            run_count=2,native_dispatch_count=2,archive_relative_path='output/campaign-v1/'+'a'*64,
            manifest_file_sha256='b'*64,receipt_write_status='completed',error_code=None,
            retry_authorized=False,closure_claim_allowed=False,runtime_authority_granted=False,
            provider_bill_cny=None,provider_calls_independently_verified=False)

    def check(self,value): return run.validate_invocation_summary(raw(value))

    def test_contract_pins_existing_document_and_admission_field_sets(self):
        profile=run.load_invocation_contract()
        self.assertEqual(set(profile['cli']['document_fields']),{field.name for field in fields(PreReceiptDocumentBytes)})
        self.assertEqual(set(profile['cli']['admission_fields']),{field.name for field in fields(TimedAdmissionInputs)})
        self.assertFalse(profile['online_execution_authorized'])
        self.assertFalse(profile['runtime_entry_implemented_by_this_contract'])

    def test_completed_summary_is_still_not_closure_or_permission(self):
        result=self.check(self.completed)
        self.assertFalse(result['closure_claim_allowed']); self.assertFalse(result['runtime_authority_granted'])

    def test_disk_snapshot_excludes_its_own_write_and_final_close(self):
        value=dict(self.completed,status='pre_return_archive_verified',final_workspace_close_observed=False,receipt_write_status='excluded_self_write')
        self.check(value)
        for changes in ({'final_workspace_close_observed':True},{'receipt_write_status':'completed'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError): self.check(value|changes)

    def test_success_requires_final_close_and_completed_receipt_write(self):
        for changes in ({'final_workspace_close_observed':False},{'receipt_write_status':'excluded_self_write'},
                        {'claim_consumed':None},{'phase_completed':None},{'run_count':0},{'native_dispatch_count':1}):
            with self.subTest(changes=changes),self.assertRaises(ValueError): self.check(self.completed|changes)

    def test_failed_summary_keeps_unknown_counts_and_no_artifact_hash(self):
        value=json.loads(run._not_confirmed_summary())
        value.update(status='outcome_unknown',claim_consumed=None,error_code='campaign_invocation_unknown',archive_status='unknown')
        result=self.check(value)
        self.assertIsNone(result['claim_consumed']); self.assertIsNone(result['native_dispatch_count'])
        with self.assertRaises(ValueError): self.check(value|{'manifest_file_sha256':'b'*64})
        with self.assertRaises(ValueError): self.check(value|{'error_code':None})

    def test_not_confirmed_constant_does_not_read_contract(self):
        with patch.object(run,'read_regular_file_no_follow',side_effect=AssertionError('read forbidden')) as reader:
            payload=run._not_confirmed_summary()
        reader.assert_not_called()
        result=run.validate_invocation_summary(payload)
        self.assertFalse(result['claim_consumed']); self.assertIsNone(result['run_count'])

    def test_path_binding_and_trailing_digest_characters_reject(self):
        for changes in ({'authorization_id_sha256':'c'*64},{'manifest_file_sha256':'b'*64+'\n'},
                        {'archive_relative_path':self.completed['archive_relative_path']+'\n'},
                        {'archive_relative_path':'C:/Users/private/archive'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError): self.check(self.completed|changes)

    def test_float_bool_duplicate_noncanonical_and_unknown_fields_reject(self):
        for changes in ({'run_count':2.0},{'native_dispatch_count':True},{'unexpected':'value'},
                        {'error_code':'Authorization: Bearer FAKE-SECRET'},{'closure_claim_allowed':True}):
            with self.subTest(changes=changes),self.assertRaises(ValueError): self.check(self.completed|changes)
        for payload in (raw(self.completed)+b'\n',raw(self.completed)[:-1]+b',"run_count":2}'):
            with self.assertRaises(ValueError): run.validate_invocation_summary(payload)

    def test_contract_tamper_cannot_change_summary_semantics(self):
        original=(run.ROOT/run.CONTRACT_PATH).read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); path=root/run.CONTRACT_PATH; path.parent.mkdir(parents=True)
            path.write_bytes(original+b'\n')
            with self.assertRaisesRegex(run.CampaignInvocationError,'contract_invalid'): run.load_invocation_contract(root)

    def test_unconfirmed_module_cannot_return_a_success_exit(self):
        environment={name:os.environ[name] for name in ('SystemRoot','WINDIR','PATH','TEMP','TMP') if name in os.environ}
        environment['PYTHONPATH']=str(run.ROOT/'src')
        result=subprocess.run([sys.executable,'-m','researchops_completion_timing.campaign_run'],
            cwd=run.ROOT,env=environment,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,2)
        self.assertEqual(result.stdout,run._not_confirmed_summary().decode()); self.assertEqual(result.stderr,'')


if __name__=='__main__': unittest.main()
