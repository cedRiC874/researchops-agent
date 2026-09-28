"""No raw rejected content: unchanged scanner, safe labels, real offline path."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
import uuid
from unittest.mock import patch

from researchops_item6_experiment_v1 import contract as c, observations as o, privacy_diagnostics as p
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from . import item6_experiment_fixture as f

LEGACY = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|[A-Za-z]:[\\/]|/(?:Users|home)/|Traceback|Authorization|reasoning_text"


class PrivacyDiagnosticUnitTests(unittest.TestCase):
    def test_matcher_union_order_and_flags_are_unchanged(self):
        self.assertEqual(p.PATTERN, LEGACY)
        examples=['plain','', 'AUTHORIZATION','x/home/demo','Traceback','reasoning_text','https://example.invalid',
                  'C:/example','person@example.invalid','Traceback Authorization','Authorization Traceback']
        for value in examples:
            self.assertEqual(p.business_rule(value) is not None, re.search(LEGACY,value,re.I) is not None)

    def test_each_business_rule_has_only_an_opaque_label(self):
        for value,rule in [('person@example.invalid','P01'),('C:/demo','P02'),('x/home/demo','P03'),
                           ('Traceback','P04'),('Authorization','P05'),('reasoning_text','P06')]:
            self.assertEqual(p.business_rule(value),rule)

    def test_legacy_url_rejection_is_not_silently_relaxed(self):
        seen=[]
        with self.assertRaisesRegex(c.ExperimentError,'item6_business_privacy'):
            o.safe_business('https://example.invalid',diagnostic=lambda rule,error:seen.append(rule))
        self.assertEqual(seen,['P02'])

    def test_public_scan_precedence_and_canary_are_preserved(self):
        for value,key in [('Authorization: synthetic',None),('sk-SyntheticFixtureOnly123456',None),
                          ('person@example.invalid',None),('unique-offline-canary','unique-offline-canary')]:
            seen=[]
            with self.assertRaises(ExternalClosurePrimitiveError) as rejected:
                o.safe_business(value,key=key,diagnostic=lambda rule,error:seen.append((rule,error)))
            self.assertEqual(seen[0][0],'P00');self.assertIs(seen[0][1],rejected.exception)
            self.assertEqual(rejected.exception.code,'external_closure_sensitive_content_detected')

    def test_safe_input_and_size_failure_do_not_create_privacy_labels(self):
        seen=[]
        o.safe_business({'result':'ordinary synthetic'},diagnostic=lambda *args:seen.append(args))
        with self.assertRaisesRegex(c.ExperimentError,'item6_business_size'):
            o.safe_business('x'*(c.policy()['business_field_bytes']+1),diagnostic=lambda *args:seen.append(args))
        self.assertEqual(seen,[])

    def test_callback_error_cannot_replace_original_rejection(self):
        originals=[]
        def broken(rule,error):originals.append(error);raise OSError('synthetic diagnostic I/O')
        with self.assertRaisesRegex(c.ExperimentError,'item6_business_privacy') as rejected:
            o.safe_business('Authorization',diagnostic=broken)
        self.assertIs(rejected.exception,originals[0])
        self.assertEqual(rejected.exception.args,('item6_business_privacy',))

    def test_unknown_shared_exception_is_not_relabelled(self):
        seen=[];error=OSError('synthetic scan fault')
        with patch.object(c,'scan_public_artifact_bytes',side_effect=error):
            with self.assertRaises(OSError) as rejected:
                o.safe_business('Authorization',diagnostic=lambda *args:seen.append(args))
        self.assertIs(rejected.exception,error);self.assertEqual(seen,[])

    def test_valid_metadata_itself_passes_original_privacy_scan(self):
        for rule in ('P00','P01','P02','P03','P04','P05','P06'):
            value=self.value();value['rule_id']=rule
            if rule=='P00':value.update(error_code='external_closure_sensitive_content_detected',stop_code='item6_execution_failed')
            p._validate_fields(value);o.safe_business(value,key='offline-diagnostic-canary')

    @staticmethod
    def value():
        return dict(schema_version=p.VERSION,rule_id='P05',scan_location='sdk_text',
            error_code='item6_business_privacy',stop_code='item6_business_privacy',sdk_response_index=0,
            response_index=0,attempt_index=0,response_event_hash='a'*64,completion_record_sha256='b'*64)

    def test_metadata_rejects_extra_content_unbounded_labels_and_bool_indices(self):
        for key,value in [('rule_id','Authorization'),('scan_location','raw body'),('sdk_response_index',True),
                          ('response_index',48),('attempt_index',-1),('completion_record_sha256','not-a-hash'),
                          ('stop_code','unknown'),('content','rejected input')]:
            row=self.value();row[key]=value
            with self.assertRaises(c.ExperimentError):p._validate_fields(row)

    def test_fake_case_or_exception_code_is_not_a_bound_diagnostic(self):
        from types import SimpleNamespace
        with self.assertRaises(c.ExperimentError):
            p.record_model_failure(SimpleNamespace(), 'P05', c.ExperimentError('business_privacy'),location='sdk_text')

    def test_old_artifact_schema_and_freeze_are_preserved(self):
        for name,digest in [('artifact_v1_3.schema.json','6d8c5c0bac3f6e1865c5a5a1dfe90b5d7981b38f696169dfd45ec7f0a9ccac85'),
                            ('freeze_v1_2.schema.json','63d88401282dfbfcb88c5d920069bc7e081cc0cf033e238ee57fbe93771e0b0c')]:
            self.assertEqual(hashlib.sha256((f.ROOT/c.DIRECTORY/name).read_bytes()).hexdigest(),digest)
        self.assertEqual(c.FREEZE_VERSION,'item6-experiment-freeze/1.2')


class PrivacyDiagnosticIntegrationTests(unittest.TestCase):
    def checked(self,mode,location,index,rule='P05'):
        result=f.isolation_case(mode);doc=result['artifact'];row=doc['business']['agent']['IC-01']
        self.assertEqual(result['process_exit_code'],2,result.get('error'))
        self.assertEqual((result['provider_calls'],result['network_attempts']),(0,0))
        self.assertFalse(result['real_store_touched'])
        self.assertEqual(result['hits'].count(mode+'_response_injected'),1)
        self.assertEqual(len(result['calls']),index+1)
        self.assertEqual(doc['collection_summary']['observed'],2)
        self.assertEqual(doc['collection_summary']['not_executed'],30)
        self.assertEqual(doc['collection_status'],'stopped')
        self.assertIsNone(row['final_output']);self.assertIsNone(row['case_rejection'])
        diagnostic=row['privacy_diagnostic'];self.assertIsNotNone(diagnostic)
        self.assertEqual((diagnostic['rule_id'],diagnostic['scan_location'],diagnostic['sdk_response_index']),
                         (rule,location,index))
        self.assertEqual((diagnostic['response_index'],diagnostic['attempt_index']),(index,index))
        self.assertEqual(set(diagnostic),set(p.FIELDS)|{'state','event_hash'})
        self.assertEqual(doc['authority']['stopped'],'item6_execution_failed' if rule=='P00' else 'item6_business_privacy')
        checked=f.verify_result(result)
        self.assertEqual(checked['actual_exit_code'],0,checked)
        self.assertTrue(checked['archive_verified']);self.assertFalse(checked['execution_completed'])
        # Only the synthetic sensitive value is used; real Key/store are absent.
        encoded=c.raw(doc)
        self.assertNotIn(b'Authorization',encoded)
        self.assertNotIn(b'sk-SyntheticFixtureOnly123456',encoded)
        self.assertNotIn(hashlib.sha256(c.raw('Authorization')).hexdigest().encode(),c.raw(diagnostic))
        return result,checked

    def test_text_rejection_is_bound_and_contains_no_rejected_value(self):
        result,checked=self.checked('privacy_diag_text','sdk_text',0)
        self.assertEqual(result['artifact']['business']['agent']['IC-01']['privacy_diagnostic']['state'],'recorded')
        self.assertEqual(checked['unacknowledged_privacy_events'],[])

    def test_tool_arguments_rejection_has_distinct_location_and_zero_tools(self):
        result,_=self.checked('privacy_diag_args','sdk_tool_args',0)
        self.assertEqual(result['artifact']['business']['agent']['IC-01']['events'],[])

    def test_shared_scanner_retains_original_exception_attribution(self):
        self.checked('privacy_diag_public','sdk_text',0,'P00')

    def test_second_response_is_not_mislabelled_as_first(self):
        result,_=self.checked('privacy_diag_second','sdk_text',1)
        self.assertEqual(len(result['artifact']['business']['agent']['IC-01']['events']),1)

    def test_evidence_failure_stops_before_and_after_commit_without_fabricated_ack(self):
        for mode in ('privacy_diag_io_before','privacy_diag_io_after'):
            result,checked=self.checked(mode,'sdk_text',0)
            self.assertEqual(result['hits'].count(mode+'_append_hit'),1)
            self.assertEqual(result['hits'].count('privacy_diagnostic_committed_before_error'),int(mode.endswith('after')))
            diagnostic=result['artifact']['business']['agent']['IC-01']['privacy_diagnostic']
            self.assertEqual(diagnostic['state'],'unacknowledged');self.assertIsNone(diagnostic['event_hash'])
            self.assertEqual(len(checked['unacknowledged_privacy_events']),int(mode.endswith('after')))

    def test_rehashed_wrong_response_index_is_rejected(self):
        from . import test_item6_experiment_artifacts as artifacts
        result=f.isolation_case('privacy_diag_second');root=Path(result['fixture_root'])
        original=root/result['run']['archive_namespace'];before={x.name:c.digest(x.read_bytes()) for x in original.iterdir()}
        clone=original.parent/('isolation-tamper-privacy-'+uuid.uuid4().hex)
        shutil.copytree(original,clone)
        document=c.decode((clone/'experiment.json').read_bytes(),c.policy()['archive_bytes'])
        diagnostic=document['business']['agent']['IC-01']['privacy_diagnostic']
        self.assertEqual(diagnostic['sdk_response_index'],1);diagnostic['sdk_response_index']=0
        event=next(e for e in document['audit']['events'] if e['event_hash']==diagnostic['event_hash'])
        event['safe_payload']['sdk_response_index']=0
        helper=artifacts.CaseIsolationArtifactTamperTests('runTest')
        _,seal,final,_=helper._rebuild_archive(clone,document,root)
        process=subprocess.run([f.PYTHON,'-B','-m','researchops_item6_experiment_v1','verify','--archive',str(clone),
            '--seal-sha256',seal,'--finalization-sha256',final],cwd=root,env=f.environment(root),
            capture_output=True,text=True,encoding='utf-8',timeout=120)
        checked=json.loads(process.stdout.splitlines()[-1])
        self.assertEqual(process.returncode,2);self.assertEqual(checked['error'],'item6_privacy_diagnostic_response')
        self.assertEqual(before,{x.name:c.digest(x.read_bytes()) for x in original.iterdir()})
        f.RESULTS.append(dict(kind='privacy_rehashed_response_link_tamper',actual_exit_code=process.returncode,
            actual_error=checked['error'],mutation_hit=True,original_unchanged=True,provider_calls=0))

    def test_nonprivacy_complete_run_retains_null_diagnostics(self):
        result=f.normal();self.assertEqual(result['process_exit_code'],0,result.get('error'))
        self.assertEqual(result['artifact']['collection_status'],'complete')
        self.assertTrue(all(row['privacy_diagnostic'] is None for rows in result['artifact']['business'].values() for row in rows.values()))
        self.assertFalse(any(e['event_type']==p.EVENT for e in result['artifact']['audit']['events']))
        checked=f.verify_result(result);self.assertEqual(checked['actual_exit_code'],0,checked)
