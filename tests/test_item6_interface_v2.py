"""Prospective interface, honest outcome layers and native exit propagation."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from researchops_item6_experiment_v1 import contract as c, interface_v2 as interface, outcome_layers
from researchops_behavior_eval_v1 import core as scorer
from . import item6_experiment_fixture as f


class InterfaceUnitTests(unittest.TestCase):
    def test_instruction_policy_and_embedded_schemas_bind_same_public_interface(self):
        digest=hashlib.sha256(interface.INSTRUCTION.encode()).hexdigest()
        freeze=c.decode(c.read(c.DIRECTORY+'/freeze_v1_3.schema.json'))
        artifact=c.decode(c.read(c.DIRECTORY+'/artifact_v1_6.schema.json'))
        self.assertEqual(digest,c.policy()['instruction_sha256'])
        self.assertEqual(digest,freeze['properties']['policy']['properties']['instruction_sha256']['const'])
        self.assertEqual(artifact['properties']['authority']['properties']['freeze'],freeze)
        self.assertEqual((c.FREEZE_VERSION,c.ARTIFACT_VERSION,c.ARCHIVE_VERSION),
                         ('item6-experiment-freeze/1.3','item6-experiment-artifact/1.6','item6-experiment-archive/1.6'))
        self.assertEqual(interface.REVISION,'item6-task-interface/2.0')

    def test_only_ic15_metadata_changes_old_requests_ids_order_are_preserved(self):
        old=c.decode(c.read(c.SERVICE+'/tasks/frozen/tasks.json'))
        new=c.decode(c.read(c.TASKS))
        self.assertEqual(c.digest(c.read(c.SERVICE+'/tasks/frozen/tasks.json')),new['predecessor']['sha256'])
        self.assertEqual([t['task_id'] for t in old['tasks']],[t['task_id'] for t in new['tasks']])
        changes=[]
        for a,b in zip(old['tasks'],new['tasks']):
            self.assertEqual(a['request'],b['request'])
            for key in a:
                if a[key]!=b[key]:changes.append((a['task_id'],key,a[key],b[key]))
        self.assertEqual(changes,[('IC-15','scope_id','s01','s05'),('IC-15','subject','Spruce','East-West'),
                                 ('IC-15','metric','mass','difference')])
        self.assertEqual(new['schema_version'],'item6-controlled-task-set/2.0')

    def test_tools_budgets_and_transport_gates_are_not_expanded(self):
        old=c.decode(c.read(c.DIRECTORY+'/policy_v1.json'));new=c.policy()
        allowed={'schema_version','instruction_sha256'}
        self.assertEqual({k:v for k,v in old.items() if k not in allowed},{k:v for k,v in new.items() if k not in allowed})
        self.assertFalse(new['online_authorized']);self.assertIsNone(new['live_budget_default'])

    def test_all_successor_files_are_in_source_selection(self):
        names=set(c.source.source_files(f.ROOT))
        self.assertTrue({c.TASKS,'src/researchops_item6_experiment_v1/interface_v2.py',
            'src/researchops_item6_experiment_v1/outcome_layers.py','scripts/invoke_item6_once.ps1',
            'scripts/item6_process_exit.psm1',c.DIRECTORY+'/TASK_INTERFACE_V2.md',
            c.DIRECTORY+'/policy_v2.json',c.DIRECTORY+'/freeze_v1_3.schema.json',
            c.DIRECTORY+'/artifact_v1_6.schema.json','tests/test_item6_interface_v2.py'}<=names)

    def test_public_format_works_without_facts_or_gold_entering_parser(self):
        for text in ('Example的质量为1.25 g [E2]。','A-B的差值为-2.1 mg [E2]。'):
            value=scorer.parse_answer(text)
            self.assertEqual(len(value['claims']),1);self.assertEqual(value['unresolved'],[])
        for text in ('请指定分析设计。','不能伪造数据。'):
            self.assertEqual(len(scorer.parse_answer(text)['actions']),1)

    def test_free_expression_still_unknown_and_null_not_coerced(self):
        self.assertTrue(scorer.parse_answer('这个质量大约符合预期。')['unresolved'])
        self.assertEqual(scorer.parse_answer(None)['text_state'],'unobserved')


class OutcomeLayerTests(unittest.TestCase):
    def sample(self):
        business={'agent':{},'fixed_workflow':{}};scores={p:{'raw_report':{'tasks':[]}} for p in business};plan=[]
        for i in range(16):
            tid='CASE-'+str(i)
            for path in business:
                run_id='RUN-'+path+'-'+tid
                row=dict(task_id=tid,path_kind=path,run_id=run_id,execution_state='observed',status='completed',
                    completion='complete',final_output='synthetic',plan=[],events=[],known_failures=[],case_rejection=None)
                business[path][tid]=row;plan.append(dict(task_id=tid,path_kind=path))
                scores[path]['raw_report']['tasks'].append(dict(task_id=tid,run_id=run_id,task_verdict='unknown',
                    dimensions={'facts':{'status':'unknown'}},manual_review_required=True))
        return dict(run_id='RUN',business=business,scores=scores,authority={'freeze':{'business_plan':plan}})

    def test_projection_preserves_original_unknown_all_rows_and_input_bytes(self):
        doc=self.sample();before=c.raw(doc);result=outcome_layers.project(doc)
        self.assertEqual(result['planned'],32);self.assertEqual(len(result['rows']),32)
        self.assertEqual(result['original_verdict_counts'],{'agent':{'unknown':16},'fixed_workflow':{'unknown':16}})
        self.assertFalse(result['rescored']);self.assertFalse(result['archive_authenticity_verified'])
        self.assertEqual(c.raw(doc),before)
        result['rows'][0]['original_scoring']['task_verdict']='pass'
        self.assertEqual(c.raw(doc),before)

    def test_proposed_rejected_tool_is_not_fabricated_as_execution_or_new_failure_grade(self):
        doc=self.sample();row=doc['business']['agent']['CASE-0']
        row.update(status='failed',completion='unknown',final_output=None,
            plan=[{'execution':'not_executed'}],case_rejection={'state':'isolated','reason_kind':'missing_design','code':'item6_tool_before_design_or_refusal'})
        result=outcome_layers.project(doc)['rows'][0]
        self.assertEqual(result['operation']['proposed_tool_calls'],1)
        self.assertEqual(result['operation']['actual_tool_events'],0)
        self.assertEqual(result['operation']['unexecuted_proposals'],1)
        self.assertEqual(result['original_scoring']['task_verdict'],'unknown')

    def test_incomplete_rejection_and_unexecuted_case_remain_visible(self):
        doc=self.sample();row=doc['business']['agent']['CASE-0']
        row.update(status='failed',completion='unknown',final_output=None,
            case_rejection={'state':'rejected','reason_kind':'missing_design','code':'item6_tool_before_design_or_refusal'})
        doc['business']['agent']['CASE-1']['execution_state']='not_executed'
        result=outcome_layers.project(doc)
        self.assertEqual(result['not_executed'],1);self.assertEqual(result['observed'],31)
        self.assertEqual(result['rows'][0]['rejection']['state'],'rejected')

    def test_cross_run_score_and_duplicate_plan_are_rejected(self):
        for mutation,code in ((lambda d:d['scores']['agent']['raw_report']['tasks'][0].update(run_id='another'),'score_identity'),
            (lambda d:d['authority']['freeze']['business_plan'].append(d['authority']['freeze']['business_plan'][0]),'denominator')):
            doc=self.sample();mutation(doc)
            with self.assertRaisesRegex(c.ExperimentError,'outcome_layers_'+code):outcome_layers.project(doc)

    def test_extra_score_missing_row_and_invalid_verdict_are_rejected(self):
        mutations=(lambda d:d['scores']['agent']['raw_report']['tasks'].append(d['scores']['agent']['raw_report']['tasks'][0]),
                   lambda d:d['business']['agent'].pop('CASE-0'),
                   lambda d:d['scores']['agent']['raw_report']['tasks'][0].update(task_verdict='approved'))
        for mutation in mutations:
            doc=self.sample();mutation(doc)
            with self.assertRaises(c.ExperimentError):outcome_layers.project(doc)


class NativeExitTests(unittest.TestCase):
    def invoke(self,directory,executable,arguments,receipt):
        shell=shutil.which('pwsh') or shutil.which('powershell')
        self.assertIsNotNone(shell,'PowerShell is required, no skip')
        module=f.ROOT/'scripts/item6_process_exit.psm1'
        quote=lambda value:"'"+str(value).replace("'","''")+"'"
        script=directory/'probe.ps1'
        script.write_text("Import-Module "+quote(module)+"\n$r=Invoke-Item6NativeOnce -Executable "+quote(executable)+
            " -Arguments @("+','.join(quote(a) for a in arguments)+") -Receipt "+quote(receipt)+
            "\nif($null -eq $r.actual_exit_code -or -not $r.invocation_completed) { exit 2 }; exit $r.actual_exit_code\n",encoding='utf-8')
        return subprocess.run([shell,'-NoProfile','-File',str(script)],env=f.environment(f.ROOT),
            capture_output=True,timeout=30)

    def test_actual_zero_two_three_are_propagated_without_rewrite(self):
        for code in (0,2,3):
            with tempfile.TemporaryDirectory(prefix='item6-exit-test-') as name:
                directory=Path(name);receipt=directory/'exit.json'
                child=self.invoke(directory,sys.executable,['-I','-S','-B','-c','import sys;sys.exit('+str(code)+')'],receipt)
                self.assertEqual(child.returncode,code)
                value=json.loads(receipt.read_bytes());self.assertEqual(value['actual_exit_code'],code)
                self.assertTrue(value['invocation_completed']);self.assertIsNone(value['invocation_error_type'])

    def test_launch_failure_has_null_actual_exit_and_safe_type(self):
        with tempfile.TemporaryDirectory(prefix='item6-exit-test-') as name:
            directory=Path(name);receipt=directory/'exit.json'
            child=self.invoke(directory,str(directory/'absent.exe'),['synthetic'],receipt)
            self.assertEqual(child.returncode,2)
            value=json.loads(receipt.read_bytes());self.assertIsNone(value['actual_exit_code'])
            self.assertFalse(value['invocation_completed']);self.assertIsNotNone(value['invocation_error_type'])
            self.assertFalse(value['exception_body_recorded'])

    def test_existing_receipt_rejects_before_child_start(self):
        with tempfile.TemporaryDirectory(prefix='item6-exit-test-') as name:
            directory=Path(name);receipt=directory/'exit.json';receipt.write_bytes(b'preserved')
            marker=directory/'child.txt'
            child=self.invoke(directory,sys.executable,['-I','-S','-B','-c',
                'from pathlib import Path;Path('+repr(str(marker))+').write_text("started")'],receipt)
            self.assertNotEqual(child.returncode,0);self.assertEqual(receipt.read_bytes(),b'preserved')
            self.assertFalse(marker.exists())

    def test_launcher_rejects_interpreter_override_before_body(self):
        with tempfile.TemporaryDirectory(prefix='item6-exit-test-') as name:
            directory=Path(name);receipt=directory/'exit.json';probe=directory/'override.ps1'
            quote=lambda value:"'"+str(value).replace("'","''")+"'"
            launcher=f.ROOT/'scripts/invoke_item6_once.ps1'
            probe.write_text("try { & "+quote(launcher)+" -Python 'unapproved.exe' -Freeze 'synthetic-freeze' "
                "-Approval 'synthetic-approval' -ApprovedDigest "+quote('a'*64)+" -Receipt "+quote(receipt)+
                "; exit 9 } catch { if($_.FullyQualifiedErrorId -like 'NamedParameterNotFound*') { exit 0 }; exit 8 }\n",encoding='utf-8')
            shell=shutil.which('pwsh') or shutil.which('powershell')
            self.assertIsNotNone(shell)
            result=subprocess.run([shell,'-NoProfile','-NonInteractive','-File',str(probe)],
                env=f.environment(f.ROOT),capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0)
            self.assertFalse(receipt.exists())


class InterfaceIntegrationTests(unittest.TestCase):
    def test_actual_entry_binds_instruction_task_successor_and_projects_all_32(self):
        result=f.isolation_case('interface_v2');doc=result['artifact']
        self.assertEqual(result['process_exit_code'],0,result.get('error'))
        self.assertEqual(doc['collection_summary']['observed'],32)
        self.assertEqual(result['hits'].count('interface_instruction_checked_IC-15'),1)
        self.assertEqual(sum(h.startswith('interface_instruction_checked_') for h in result['hits']),len(result['calls']))
        projection=outcome_layers.project(doc)
        self.assertEqual(projection['original_verdict_counts'],{'agent':{'pass':16},'fixed_workflow':{'pass':16}})
        self.assertEqual(len(projection['rows']),32);self.assertFalse(projection['rescored'])
        self.assertEqual(doc['business']['agent']['IC-15']['task_input']['subject'],'East-West')
        checked=f.verify_result(result);self.assertEqual(checked['actual_exit_code'],0,checked)
        self.assertTrue(checked['archive_verified']);self.assertEqual(result['provider_calls'],0)

    def test_extra_query_still_fails_and_rejected_proposals_are_separate_from_unknown(self):
        result=f.isolation_case('interface_deviation');doc=result['artifact']
        self.assertEqual(result['process_exit_code'],3,result.get('error'))
        self.assertEqual(result['hits'].count('isolation_response_IC-12'),1)
        projection=outcome_layers.project(doc)
        numeric=next(r for r in projection['rows'] if r['path']=='agent' and r['task_id']=='IC-01')
        self.assertEqual(numeric['original_scoring']['task_verdict'],'fail')
        self.assertEqual(numeric['operation']['actual_tool_events'],2)
        for tid in ('IC-11','IC-12','IC-13'):
            row=next(r for r in projection['rows'] if r['path']=='agent' and r['task_id']==tid)
            self.assertEqual(row['operation']['proposed_tool_calls'],1)
            self.assertEqual(row['operation']['actual_tool_events'],0)
            self.assertFalse(row['operation']['final_output_observed'])
            self.assertEqual(row['original_scoring']['task_verdict'],'unknown')
            self.assertEqual(row['rejection']['state'],'isolated')
        self.assertEqual(len(projection['rows']),32)
        checked=f.verify_result(result);self.assertEqual(checked['actual_exit_code'],0,checked)
        self.assertEqual(result['provider_calls'],0)
