"""Named rules v2; unchanged shared scanner, safe labels, real offline path."""
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
    def test_legacy_rejections_are_preserved_as_history_not_current_semantics(self):
        self.assertEqual(p.VERSION, 'item6-business-privacy-diagnostic/1.1')
        self.assertEqual(p.RULE_REVISION, 'item6-business-privacy-rules/2.0')
        for value in ('Authorization', 'Traceback', 'reasoning_text', 'https://example.invalid/home/help'):
            self.assertIsNotNone(re.search(LEGACY, value, re.I))
            self.assertIsNone(p.business_rule(value))

    def test_each_business_rule_has_only_an_opaque_label(self):
        for value,rule in [('person@example.invalid','P01'),('C:/demo','P02'),('/home/demo','P03'),
                           ('Traceback: synthetic frame','P04'),('Authorization=synthetic','P05'),('reasoning_text=synthetic','P06')]:
            self.assertEqual(p.business_rule(value),rule)

    def test_new_url_acceptance_does_not_exempt_adjacent_sensitive_content(self):
        seen=[]
        for value in ('https://example.invalid', 'https://example.invalid/home/help', 'https://example.invalid/Users/help'):
            o.safe_business(value,diagnostic=lambda rule,error:seen.append(rule))
            for suffix in (' C:/private/data', ' /home/private/data', ' Authorization: synthetic',
                           ' sk-SyntheticFixtureOnly123456', ' person@example.invalid', ' unique-offline-canary'):
                with self.assertRaises(ExternalClosurePrimitiveError):
                    o.safe_business(value+suffix,key='unique-offline-canary')
        self.assertEqual(seen,[])

    def test_mentions_pass_but_structured_fields_and_tracebacks_fail(self):
        for text in ('Discuss Authorization and reasoning_text; mention Traceback.', ''):
            o.safe_business(text)
        for key in ('Authorization', 'proxy-Authorization', 'reasoning_text'):
            for value in ('synthetic', '', None):
                for level,payload in enumerate(({key:value}, json.dumps({key:value}), json.dumps(json.dumps({key:value})))):
                    with self.subTest(key=key,payload=payload):
                        seen=[]
                        expected=ExternalClosurePrimitiveError if level==2 else c.ExperimentError
                        with self.assertRaises(expected) as rejected:
                            o.safe_business(payload,diagnostic=lambda rule,error:seen.append(rule))
                        self.assertEqual(seen,['P00' if level==2 else 'P06' if key=='reasoning_text' else 'P05'])
                        self.assertEqual(rejected.exception.code,'external_closure_sensitive_content_detected' if level==2 else 'item6_business_privacy')
        for text in ('Traceback: synthetic frame', 'Traceback (most recent call last):'):
            with self.assertRaises((c.ExperimentError,ExternalClosurePrimitiveError)):o.safe_business(text)

    def test_boundary_matching_returns_label_without_losing_lookbehind_context(self):
        self.assertEqual(p.business_rule('https://example.invalid then C:/private'), 'P02')
        self.assertEqual(p.business_rule('https://example.invalid then /Users/private'), 'P03')
        self.assertEqual(p.business_rule('reasoning_text=synthetic Authorization=synthetic'), 'P06')

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
            o.safe_business({'Authorization':'synthetic'},diagnostic=broken)
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
                            ('artifact_v1_4.schema.json','5cb3286941248a1c2d73821a3692fb37ddb661c13fd28634dca2fa1c449f8232'),
                            ('freeze_v1_2.schema.json','63d88401282dfbfcb88c5d920069bc7e081cc0cf033e238ee57fbe93771e0b0c')]:
            self.assertEqual(hashlib.sha256((f.ROOT/c.DIRECTORY/name).read_bytes()).hexdigest(),digest)
        self.assertEqual(json.loads((f.ROOT/c.DIRECTORY/'freeze_v1_2.schema.json').read_bytes())['properties']['schema_version']['const'],
                         'item6-experiment-freeze/1.2')
        self.assertEqual(c.FREEZE_VERSION,'item6-experiment-freeze/1.3')


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


class BatchCollectionV2Tests(unittest.TestCase):
    def check_common(self, mode):
        result=f.isolation_case(mode);doc=result['artifact']
        self.assertEqual((result['provider_calls'],result['network_attempts']),(0,0))
        self.assertFalse(result['real_store_touched'])
        self.assertEqual(result['claim_files'],1)
        self.assertEqual(result['hits'].count(mode),1)  # IC-01 actual read result mutation.
        self.assertNotIn('declared_stop_after_boundary_case',result['hits'])
        for tid in ('IC-02','IC-03','IC-04'):
            self.assertEqual(result['hits'].count('batch_final_'+tid),1)
        self.assertEqual(doc['business']['agent']['IC-01']['status'],'failed')
        self.assertEqual(doc['business']['agent']['IC-02']['final_output'],'')
        self.assertEqual(doc['business']['agent']['IC-03']['final_output'],' \t\n　')
        self.assertIn('https://example.invalid/home/help',doc['business']['agent']['IC-04']['final_output'])
        self.assertFalse(doc['all_business_passed'])
        for tid in ('IC-12','IC-14','IC-15'):
            row=doc['business']['agent'][tid]
            self.assertEqual(result['hits'].count('isolation_response_'+tid),1)
            self.assertEqual(row['status'],'failed')
            self.assertIsNotNone(row['case_rejection'])
            self.assertEqual(row['events'],[])
            self.assertIsNone(row['final_output'])
        self.assertEqual(len(doc['budget']['requests']),len(result['calls']))
        self.assertEqual(len(doc['segments']),len(result['calls']))
        self.assertEqual(doc['cleanup_errors'],[])
        self.assertEqual(sum(len(rows) for rows in doc['business'].values()),32)
        checked=f.verify_result(result)
        self.assertEqual(checked['actual_exit_code'],0,checked)
        self.assertTrue(checked['archive_verified'])
        return result,doc

    def test_complete_32_after_missing_facts_empty_text_mentions_and_four_rejections(self):
        result,doc=self.check_common('batch_complete_v2')
        self.assertEqual(result['process_exit_code'],3,result.get('error'))
        self.assertEqual(doc['collection_status'],'complete_with_case_rejections')
        self.assertEqual(doc['collection_summary']['observed'],32)
        self.assertEqual(doc['collection_summary']['not_executed'],0)
        self.assertEqual(result['hits'].count('isolation_response_IC-16'),1)
        self.assertTrue(all(row['execution_state']=='observed' for rows in doc['business'].values() for row in rows.values()))
        self.assertTrue(all(row['privacy_diagnostic'] is None for rows in doc['business'].values() for row in rows.values()))

    def test_late_privacy_stops_suffix_despite_prior_isolated_rejections(self):
        result,doc=self.check_common('batch_late_privacy_v2')
        self.assertEqual(result['process_exit_code'],2,result.get('error'))
        self.assertEqual(doc['collection_status'],'stopped')
        self.assertEqual(doc['authority']['stopped'],'item6_business_privacy')
        self.assertEqual(result['hits'].count('batch_late_privacy_injected'),1)
        self.assertEqual(result['hits'].count('isolation_response_IC-16'),1)
        row=doc['business']['agent']['IC-06']
        self.assertEqual(row['privacy_diagnostic']['rule_id'],'P06')
        self.assertEqual(row['privacy_diagnostic']['state'],'recorded')
        self.assertIsNone(row['case_rejection'])
        position=next(i for i,entry in enumerate(doc['authority']['freeze']['business_plan'])
                      if entry=={'path_kind':'agent','task_id':'IC-06'})
        self.assertEqual(doc['collection_summary']['observed'],position+1)
        self.assertEqual(doc['collection_summary']['not_executed'],32-position-1)
        self.assertEqual(result['calls'][-1]['task_id'],'IC-06')
