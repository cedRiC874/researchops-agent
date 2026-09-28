"""v1.1 only: preserve the refusal gate while sealing a failed zero-tool case."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import unittest
import uuid

from researchops_item6_experiment_v1 import case_isolation as isolation, contract as c
from . import item6_experiment_fixture as f


class RefusalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tasks = {t['task_id']: t for t in json.loads((f.ROOT/c.TASKS).read_bytes())['tasks']}
        self.evidence = json.loads((f.ROOT/c.EVIDENCE).read_bytes())

    def test_all_three_refusal_tasks_use_predicate_not_identity(self):
        for tid in ('IC-14', 'IC-15', 'IC-16'):
            task = deepcopy(self.tasks[tid])
            task['task_id'] = 'synthetic-different-identity'
            self.assertEqual(isolation.reason(task, 'inspect_sources', {'scope_id': task['scope_id']}, self.evidence), 'policy_refusal')

    def test_negated_refusal_does_not_become_eligible(self):
        task = deepcopy(self.tasks['IC-15'])
        for request in ('不要伪造结果。', '禁止编造数据。', '不能捏造结论。', '请分析已有结果。'):
            task['request'] = request
            self.assertIsNone(isolation.reason(task, 'inspect_sources', {'scope_id': task['scope_id']}, self.evidence))

    def test_refusal_has_priority_but_never_relaxes_tool_scope(self):
        for tid in ('IC-14', 'IC-15', 'IC-16'):
            task = deepcopy(self.tasks[tid])
            for designs in ([], ['one', 'two']):
                task['design_requests'] = designs
                self.assertEqual(isolation.reason(task, 'inspect_sources', {'scope_id': task['scope_id']}, self.evidence), 'policy_refusal')
            for tool, args in (('unknown', {}), ('inspect_sources', {'scope_id': 'outside'}),
                               ('inspect_sources', {'scope_id': task['scope_id'], 'extra': True}),
                               ('read_aggregate', {'bundle_id': 'outside'})):
                self.assertIsNone(isolation.reason(task, tool, args, self.evidence))

    def test_aggregate_still_requires_provided_catalog_bundle(self):
        task = deepcopy(self.tasks['IC-15'])
        bundle = self.evidence['catalog'][task['scope_id']][0]['bundle_id']
        self.assertIsNone(isolation.reason(task, 'read_aggregate', {'bundle_id': bundle}, self.evidence))
        task['bundle_id'] = bundle
        self.assertEqual(isolation.reason(task, 'read_aggregate', {'bundle_id': bundle}, self.evidence), 'policy_refusal')
        self.assertIsNone(isolation.reason(task, 'read_aggregate', {'bundle_id': bundle}, {'catalog': {}}))

    def test_prior_tools_evidence_or_final_answer_exclude_isolation(self):
        record=dict(task_id='IC-15',path_kind='agent',events=[],allowed_evidence=[],side_effects=[],
                    delivered_artifacts=[],side_effects_complete=True,final_output=None)
        self.assertTrue(isolation._zero_tools(record, {'tools': {}}))
        for key, value in (('events',[{}]),('allowed_evidence',[{}]),('side_effects',[{}]),
                           ('delivered_artifacts',[{}]),('side_effects_complete',False),('final_output','refused')):
            changed=deepcopy(record); changed[key]=value
            self.assertFalse(isolation._zero_tools(changed, {'tools': {}}))
        self.assertFalse(isolation._zero_tools(record, {'tools': {'IC-15:agent': 1}}))

    def test_historical_contract_and_schemas_remain_exact_bytes(self):
        expected={
            'CASE_REJECTION_ISOLATION_V1.md':'a1ecd73386521fee09f49353fa93c16ef2994422112bcc31c3c372f509637485',
            'case_rejection_policy_v1.json':'e4f2af19debe5c69ce1976c1e828f5feb4a6f5c161985b687ab197baeb83826d',
            'freeze_v1_1.schema.json':'b01c3a978cbebbbced778db1b7d05fcb33d08a184cd70db175c3fbeb93356290',
            'artifact_v1_2.schema.json':'8acfd57f60f74b4aa15a333d604be4ddf38b10da5784e258b299d081e68cdc10'}
        for name, digest in expected.items():
            self.assertEqual(hashlib.sha256((f.ROOT/c.DIRECTORY/name).read_bytes()).hexdigest(), digest)
        old=json.loads((f.ROOT/c.DIRECTORY/'case_rejection_policy_v1.json').read_bytes())
        self.assertTrue(old['forbid_refusal_requests'])
        self.assertNotIn('policy_refusal', old['eligible_reasons'])

    def test_new_revision_explicitly_binds_refusal_without_tool_permission(self):
        policy=c.case_rejection_policy()
        self.assertEqual(c.REJECTION_REVISION, 'item6-case-rejection-isolation/1.1')
        self.assertEqual((c.FREEZE_VERSION,c.ARTIFACT_VERSION,c.ARCHIVE_VERSION),
            ('item6-experiment-freeze/1.2','item6-experiment-artifact/1.5','item6-experiment-archive/1.5'))
        self.assertEqual(policy['eligible_reasons'], ['missing_design','conflicting_design','policy_refusal'])
        self.assertTrue(policy['forbid_refusal_tool_execution'] and policy['require_zero_prior_tools'])
        self.assertFalse(policy['same_case_retry'])

    def test_new_normative_text_and_tests_are_source_inputs(self):
        from researchops_internal_telemetry import source_integrity_v3 as source
        names=source.source_files(f.ROOT)
        for name in (c.DIRECTORY+'/CASE_REJECTION_ISOLATION_V1_1.md',c.REJECTION_POLICY_PATH,
                     c.DIRECTORY+'/freeze_v1_2.schema.json',c.DIRECTORY+'/artifact_v1_3.schema.json',
                     'tests/test_item6_refusal_isolation.py'):
            self.assertIn(name,names)
        self.assertNotIn(source.MANIFEST,names)


class RefusalIntegrationTests(unittest.TestCase):
    @staticmethod
    def complete():
        return f.isolation_case('refusal_isolation_all')

    def test_real_sdk_three_refusals_seal_then_complete_all_observations(self):
        result=self.complete(); doc=result['artifact']
        self.assertEqual(result['process_exit_code'],3,result.get('error'))
        self.assertEqual((result['claim_files'],result['provider_calls'],result['network_attempts']),(1,0,0))
        self.assertFalse(result['real_store_touched'])
        self.assertEqual(doc['collection_summary'],dict(planned=32,observed=32,not_executed=0,
            operational_complete=29,failed_after_start=3,isolated_case_rejections=3))
        self.assertFalse(doc['all_business_passed'])
        self.assertIsNone(doc['authority']['stopped'])
        self.assertEqual(doc['denominator']['not_finalized_case_ids'],[])
        self.assertEqual(len(doc['denominator']['records']),len(result['calls']))
        self.assertIs(doc['budget']['pending_reservation'], False)
        events=doc['audit']['events']
        for kind,count in ((isolation.START,32),(isolation.CLOSE,32),(isolation.REJECT,3),(isolation.SEALED,3)):
            self.assertEqual(sum(e['event_type']==kind for e in events),count)
        for tid in ('IC-14','IC-15','IC-16'):
            self.assertEqual(result['hits'].count('isolation_response_'+tid),1)
            self.assertNotIn('target_tool_read_'+tid,result['hits'])
            self.assertEqual(sum(r['task_id']==tid for r in result['calls']),1)
            row=doc['business']['agent'][tid]
            self.assertEqual((row['status'],row['execution_state'],row['completion']),('failed','observed','unknown'))
            self.assertIsNone(row['final_output'])
            self.assertEqual(row['events'],[]); self.assertEqual(row['allowed_evidence'],[])
            self.assertEqual(row['plan'][0]['execution'],'not_executed')
            reject=row['case_rejection']
            self.assertEqual((reject['schema_version'],reject['reason_kind'],reject['state']),
                             ('item6-case-rejection/1.1','policy_refusal','isolated'))
            self.assertTrue(reject['provider_closed'] and reject['reconciliation']['closure_eligible'])
            sealed=next(e for e in events if e['event_hash']==reject['sealed_event_hash'])
            plan=doc['authority']['freeze']['business_plan']; index=plan.index({'path_kind':'agent','task_id':tid})
            following=plan[index+1]
            next_row=doc['business'][following['path_kind']][following['task_id']]
            next_start=next(e for e in events if e['event_hash']==next_row['case_lifecycle']['started_event_hash'])
            self.assertLess(sealed['sequence'],next_start['sequence'])
        checked=f.verify_result(result)
        self.assertEqual(checked['actual_exit_code'],0,checked)
        self.assertTrue(checked['archive_verified'] and checked['execution_completed'])
        self.assertEqual(checked['collection_status'],'complete_with_case_rejections')
        self.assertFalse(checked['all_business_passed'] or checked['runtime_authority_granted'])

    def test_invalid_scope_still_stops_without_refusal_isolation(self):
        result=f.isolation_case('refusal_isolation_scope')
        self.assertEqual(result['process_exit_code'],2)
        self.assertEqual(result['calls'][-1]['task_id'],'IC-14')
        self.assertEqual(result['hits'].count('isolation_response_IC-14'),1)
        self.assertNotIn('target_tool_read_IC-14',result['hits'])
        self.assertIsNone(result['artifact']['business']['agent']['IC-14']['case_rejection'])
        self.assertEqual(result['artifact']['collection_status'],'stopped')
        self.assertEqual((result['provider_calls'],result['network_attempts']),(0,0))

    def test_seal_failure_is_hit_and_prevents_next_fixed_case(self):
        result=f.isolation_case('refusal_isolation_seal_failure'); doc=result['artifact']
        self.assertEqual(result['process_exit_code'],2)
        self.assertEqual(result['hits'].count('refusal_isolation_seal_failure'),1)
        self.assertEqual(result['calls'][-1]['task_id'],'IC-14')
        self.assertEqual(doc['business']['agent']['IC-14']['case_rejection']['state'],'rejected')
        self.assertIsNone(doc['business']['agent']['IC-14']['case_rejection']['sealed_event_hash'])
        following=doc['business']['fixed_workflow']['IC-14']
        self.assertEqual(following['execution_state'],'not_executed')
        self.assertIsNone(following['case_lifecycle']['started_event_hash'])
        self.assertNotIn('target_tool_read_IC-14',result['hits'])
        self.assertEqual((result['provider_calls'],result['network_attempts']),(0,0))
        checked=f.verify_result(result)
        self.assertEqual(checked['actual_exit_code'],0,checked)
        self.assertFalse(checked['execution_completed'])

    def test_old_isolation_freeze_cannot_claim_under_new_entrypoint(self):
        result=f.isolation_case('previous_isolation_freeze')
        self.assertEqual(result['process_exit_code'],2)
        self.assertEqual(result['claim_files'],0)
        self.assertEqual(result['calls'],[])
        self.assertEqual(result['error'],'item6_freeze_schema')
        self.assertEqual(result['hits'].count('previous_isolation_freeze'),1)

    def test_fully_rehashed_wrong_reason_is_rejected(self):
        from . import test_item6_experiment_artifacts as artifacts
        result=self.complete(); root=Path(result['fixture_root'])
        original=root/result['run']['archive_namespace']
        before={p.name:c.digest(p.read_bytes()) for p in original.iterdir()}
        target=original.parent/('isolation-tamper-refusal-'+uuid.uuid4().hex)
        shutil.copytree(original,target)
        doc=c.decode((target/'experiment.json').read_bytes(),c.policy()['archive_bytes'])
        rejection=doc['business']['agent']['IC-15']['case_rejection']
        self.assertEqual(rejection['reason_kind'],'policy_refusal')
        rejection['reason_kind']='missing_design'
        event=next(e for e in doc['audit']['events'] if e['event_hash']==rejection['rejected_event_hash'])
        event['safe_payload']['reason_kind']='missing_design'
        helper=artifacts.CaseIsolationArtifactTamperTests('runTest')
        _,seal,final,_=helper._rebuild_archive(target,doc,root)
        proc=subprocess.run([f.PYTHON,'-B','-m','researchops_item6_experiment_v1','verify','--archive',str(target),
            '--seal-sha256',seal,'--finalization-sha256',final],cwd=root,env=f.environment(root),
            capture_output=True,text=True,encoding='utf-8',timeout=120)
        checked=json.loads(proc.stdout.splitlines()[-1])
        self.assertEqual(proc.returncode,2)
        self.assertEqual(checked['error'],'item6_isolation_reason')
        self.assertEqual(before,{p.name:c.digest(p.read_bytes()) for p in original.iterdir()})
        f.RESULTS.append(dict(kind='refusal_reason_full_rehash_rejected',actual_exit_code=proc.returncode,
            actual_code=checked['error'],mutation_hit=True,original_unchanged=True,provider_calls=0))
