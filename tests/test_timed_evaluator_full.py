"""Actual signed/Git/admission/SQLite timed A composition, all data synthetic."""
import copy
import json
import sqlite3
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from researchops.audit import AuditLedger
from researchops_completion_timing import contract as timing, artifacts as containers
from researchops_completion_timing.ledger_event import LEDGER_CONTRACT_SHA256
from researchops_external_closure import timed_evaluator as evaluator, timed_contract as wire
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests.timed_campaign_fixture import TimedCampaignFixture
from tests import test_timed_content_semantics as content_fixture
from tests import test_timed_semantics_integration as timing_fixture
from tests import test_external_closure_evaluator as old_fixture
from tests import test_external_closure_semantics as old_content
from tests import test_completion_timing_pre_execution as signed
from tests.test_external_closure_documents import SyntheticPreReceipt


class TimedEvaluatorFullFixture(TimedCampaignFixture):
    def __init__(self):
        super().__init__(implementation_version=5)
        try:
            self.build_campaign_bundle()
        except BaseException:
            self.close(); raise

    def build_campaign_bundle(self):
        content_fixture.TimedContentSemanticsTests.setUpClass()
        helper = content_fixture.TimedContentSemanticsTests(); helper.setUp()
        value, _ = helper.fixture()
        envelope = self.signers.documents['preregistration_envelope']
        runtime = copy.deepcopy(value['expected_runtime_plan'])
        original_budget = envelope['runtime_plan']['budget_policy']
        budget = runtime['budget_policy']
        for name in ('pricing_snapshot_date', 'pricing_source_url', 'pricing_source_snapshot_sha256', 'pricing_retrieved_at_utc'):
            budget[name] = original_budget[name]
        body = dict(budget); body.pop('budget_policy_commitment_sha256')
        budget['budget_policy_commitment_sha256'] = old_fixture._domain_hash(old_fixture.RUNTIME_PLAN_DOMAIN, b'budget_policy', raw(body))
        cases = runtime['denominator_plan']['case_ids']
        case_set = old_fixture._domain_hash(old_fixture.CASE_SET_DOMAIN, raw(sorted(cases)))
        case_order = old_fixture._domain_hash(old_fixture.CASE_ORDER_DOMAIN, raw(cases))
        campaign_id = envelope['runtime_plan']['campaign_topology']['campaign_id']
        runtime['campaign_topology'].update(campaign_id=campaign_id, case_handle_set_commitment_sha256=case_set,
            case_handle_order_commitment_sha256=case_order,
            audit_run_set_commitment_sha256=old_fixture._domain_hash(old_fixture.AUDIT_SET_DOMAIN, b'planned',
                raw([old_content._run_id(envelope['envelope_id'], case) for case in cases])))
        envelope['runtime_plan'] = runtime
        envelope['task_custody'].update(planned_case_count=len(cases), case_handle_set_commitment_sha256=case_set,
            case_order_commitment_sha256=case_order, execution_input_order_commitment_sha256=value['execution_input_order_commitment_sha256'])
        grant = self.signers.documents['authorization_grant']
        grant['denominator_plan_commitment_sha256'] = runtime['denominator_plan_commitment_sha256']
        for name in ('budget_policy_commitment_sha256', 'pricing_snapshot_date', 'pricing_source_snapshot_sha256',
                     'pricing_retrieved_at_utc', 'official_pricing_attestation_current'):
            grant[name] = budget[name]
        self.signers.documents['consumption_receipt']['denominator_plan_commitment_sha256'] = runtime['denominator_plan_commitment_sha256']
        plan = json.loads(self.campaign_timing_plan)
        plan['binding'].update(runtime_plan_core_sha256=timing.runtime_plan_core_commitment(runtime))
        plan.update(planned_case_handles=cases, max_attempts=runtime['denominator_plan']['total_model_request_cap'],
            max_attempts_per_case=runtime['denominator_plan']['max_turns_per_case'],
            request_timeout_ns=runtime['transport_limits']['request_timeout_seconds'] * 1_000_000_000,
            phase_timeout_ns=runtime['transport_limits']['total_timeout_seconds'] * 1_000_000_000)
        signed.resign(self.signers, plan, envelope_version=3)
        old_fixture._rebind_semantic_fixture(value, envelope_id=envelope['envelope_id'], campaign_id=campaign_id,
            runtime_plan=runtime, authorization_grant_sha256=grant['document_sha256'], model_id=envelope['execution_binding']['model_id'])
        for rows, keys in ((value['run_rows'], ('created_at_utc', 'updated_at_utc')),
                           (value['event_rows'], ('occurred_at_utc',)),
                           (value['model_call_rows'], ('started_at_utc',))):
            for row in rows:
                for name in keys:
                    instant = datetime.fromisoformat(row[name].replace('Z', '+00:00')) + timedelta(days=1, hours=22, seconds=20)
                    row[name] = instant.isoformat().replace('+00:00', 'Z')
        frame = timing_fixture.time_fixture.TimingAuthoringTests().fixture()
        frame['plan'] = plan
        frame['evidence']['binding'] = dict(plan['binding'], authorization_grant_sha256=grant['document_sha256'],
            external_consumption_anchor_sha256=timing.digest(raw(self.signers.observation['authorization_consumed_anchor'])),
            runtime_plan_commitment_sha256=runtime['external_plan_binding_sha256'])
        terminals = [json.loads(row['safe_payload_json']) for row in value['event_rows'] if row['event_type'] == 'model_response_telemetry_recorded']
        for segment, event in zip(frame['evidence']['segments'], terminals):
            segment.update(case_handle=event['case_id'], completion_record_sha256=timing.digest(raw(event['completion_record'])),
                usage_sha256=timing.digest(raw(event['completion_record']['usage'])))
        joined = timing_fixture.TimedSemanticsIntegrationTests(); joined.setUp()
        joined.reseal(value, frame)
        directory = self.root / 'timed-campaign-result'; directory.mkdir()
        database = directory / 'phase6_audit.sqlite3'
        AuditLedger(database)
        with sqlite3.connect(database) as connection:
            connection.executemany('INSERT INTO runs(run_id,mode,status,request_sha256,dataset_sha256,created_at_utc,updated_at_utc,terminal_error_code) VALUES(:run_id,:mode,:status,:request_sha256,:dataset_sha256,:created_at_utc,:updated_at_utc,:terminal_error_code)', value['run_rows'])
            connection.executemany('INSERT INTO audit_events(event_id,run_id,sequence,event_type,occurred_at_utc,actor_kind,safe_payload_json,prev_hash,event_hash) VALUES(:event_id,:run_id,:sequence,:event_type,:occurred_at_utc,:actor_kind,:safe_payload_json,:prev_hash,:event_hash)', value['event_rows'])
            connection.executemany('INSERT INTO model_calls(model_call_id,run_id,provider,model,started_at_utc,latency_ms,input_tokens,output_tokens,cached_tokens,cost_usd,outcome,error_code) VALUES(:model_call_id,:run_id,:provider,:model,:started_at_utc,:latency_ms,:input_tokens,:output_tokens,:cached_tokens,:cost_usd,:outcome,:error_code)', value['model_call_rows'])
            connection.commit(); connection.execute('PRAGMA wal_checkpoint(TRUNCATE)'); connection.execute('PRAGMA journal_mode=DELETE')
        connection.close()
        for name, data in (('phase6_audit_index.json', value['audit_index']), ('phase6_completion_telemetry.json', value['telemetry']),
                           ('completion_timing_plan.json', plan), ('completion_timing_evidence.json', frame['evidence'])):
            (directory / name).write_bytes(raw(data))
        profile, _ = containers.load_artifact_contract(self.repository.root)
        manifest = dict(schema_version='provider-completion-timed-bundle-manifest/1.0', status='sealed_payloads', campaign_id=campaign_id,
            artifact_contract_sha256=containers.PROFILE_SHA256, timing_contract_sha256=timing.CONTRACT_SHA256,
            timed_ledger_contract_sha256=LEDGER_CONTRACT_SHA256, plan_commitment_sha256=plan['plan_commitment_sha256'],
            execution_binding_sha256=frame['evidence']['execution_binding_sha256'],
            files={name: {'bytes': len((directory / name).read_bytes()), 'sha256': timing.digest((directory / name).read_bytes())} for name in profile['payload_files']},
            manifest_self_hash_included=False, raw_provider_content_persisted=False, task_content_persisted=False,
            api_key_persisted=False, online_execution_authorized=False)
        manifest_bytes = raw(manifest); (directory / profile['manifest_file']).write_bytes(manifest_bytes)
        publication = frame['publication']; publication['bundle_manifest_file_sha256'] = timing.digest(manifest_bytes)
        publication['publication_commitment_sha256'] = timing.commitment('publication', publication)
        (directory / profile['publication_file']).write_bytes(raw(publication))
        after = copy.deepcopy(SyntheticPreReceipt().observation)
        after.update(copy.deepcopy(self.signers.observation))
        after.update(schema_version='provider-completion-timed-closure-observation/2.0', mode='pre_receipt')
        after['manifest_observation'].update(state='present', manifest_sha256=timing.digest(manifest_bytes),
            observed_at_utc='2026-09-06T01:03:01Z', last_completed_artifact_write_stage='manifest_written')
        facts = old_fixture._postrun(self.signers)
        facts.update(schema_version='provider-completion-postrun-attested-facts/2.0', timed_last_completed_write_stage='timing_publication_written',
            task_released_at_utc='2026-09-06T01:01:14Z', provider_key_loaded_at_utc='2026-09-06T01:01:15Z',
            completed_at_utc='2026-09-06T01:03:00Z', receipt_issued_at_utc='2026-09-06T01:03:02Z')
        contract, _ = wire.load_contract(self.repository.root)
        facts['receipt_id'] = wire.receipt_id(dict(campaign_id=campaign_id, authorization_grant_sha256=grant['document_sha256'],
            authorization_consumption_entry_sha256=self.signers.documents['authorization_consumed_ledger_entry']['entry_sha256']), contract)
        self.full_arguments = dict(pre_execution_observation=raw(self.signers.observation), postrun_observation=raw(after),
            timing_plan_bytes=raw(plan), artifact_directory=directory, postrun_attested_facts=raw(facts),
            admission_inputs=self.campaign_admission_inputs)
        self.full_documents = signed.documents(self.signers)


class TimedEvaluatorFullTests(unittest.TestCase):
    def test_real_signed_admission_and_sqlite_pipeline_produces_unsigned_eligible_receipt(self):
        fixture = TimedEvaluatorFullFixture(); self.addCleanup(fixture.close)
        failures = []
        original = evaluator._verify_admission
        def traced(*args, **kwargs):
            try: return original(*args, **kwargs)
            except Exception as error:
                failures.append(getattr(error, 'code', type(error).__name__)); raise
        with patch('socket.socket', side_effect=AssertionError('no network')), patch.object(evaluator, '_verify_admission', side_effect=traced) as admission:
            result = evaluator.evaluate_timed_closure_bundle(fixture.repository.root, fixture.full_documents, **fixture.full_arguments)
        self.assertEqual(failures, [])
        self.assertEqual(admission.call_count, 1)
        self.assertIs(type(result), evaluator.TimedReceiptProjectionReady, getattr(result, 'error_code', None))
        self.assertTrue(result.evidence_valid, result.evidence_error_code)
        self.assertTrue(result.pre_anchor_closure_eligible, result.closure_reasons)
        self.assertFalse(result.closure_claim_allowed)
        body = json.loads(result.unsigned_receipt_canonical_json)
        self.assertEqual(body['attempt_count'], 2)
        self.assertTrue(body['timing']['gate_complete'])
        self.assertNotIn('signatures', body)


if __name__ == '__main__': unittest.main()
