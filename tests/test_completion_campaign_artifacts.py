"""Actual Runner payloads to the independent verifier; admission is stubbed."""
import json
import unittest
from unittest.mock import patch

from researchops_completion_timing import campaign_artifacts as producer, campaign_phase as phase
from researchops_completion_timing.artifacts import PROFILE_SHA256, load_artifact_contract, verify_timing_artifact_bundle
from researchops_completion_timing.contract import CONTRACT_SHA256, commitment, digest, runtime_plan_core_commitment
from researchops_completion_timing.ledger_event import LEDGER_CONTRACT_SHA256
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import test_completion_campaign_phase as phase_fixture, test_completion_timing_contract as timing_fixture


class CampaignArtifactProducerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.helper = phase_fixture.CampaignPhaseTests()
        self.addCleanup(self.helper._cleanup_fixture)
        await self.helper.asyncSetUp()
        self.factory = self.helper.factory
        value = self.factory
        runtime = value._envelope['runtime_plan']; execution = value._execution
        plan = timing_fixture.TimingAuthoringTests().fixture()['plan']
        binding = {name: execution[name] for name in ('execution_commit', 'execution_tree', 'source_integrity_commitment_sha256',
                                                     'provider_id', 'api_surface', 'transport_id', 'adapter_version')}
        binding.update(campaign_id=runtime['campaign_topology']['campaign_id'], runtime_plan_core_sha256=runtime_plan_core_commitment(runtime))
        plan.update(binding=binding, planned_case_handles=list(value._denominator.case_ids),
                    max_attempts=value._denominator.total_model_request_cap, max_attempts_per_case=value._denominator.max_turns_per_case,
                    request_timeout_ns=value._prepared.clock._request_cap, phase_timeout_ns=value._prepared.clock._phase_cap,
                    artifact_sealing_timeout_ns=30_000_000_000)
        plan['plan_commitment_sha256'] = commitment('plan', plan)
        proof = value._prepared.proof.scope.timing
        proof.timing_plan_commitment_sha256 = plan['plan_commitment_sha256']
        proof.external_consumption_anchor_sha256 = '4' * 64
        evidence_binding = dict(binding, authorization_grant_sha256=proof.authorization_grant_sha256,
            external_consumption_anchor_sha256=proof.external_consumption_anchor_sha256,
            runtime_plan_commitment_sha256=runtime['external_plan_binding_sha256'])
        proof.timing_execution_binding_sha256 = commitment('execution_binding', evidence_binding)
        value._prepared.plan_bytes = raw(plan)
        value._timing_binding.update(plan_commitment_sha256=proof.timing_plan_commitment_sha256,
                                     execution_binding_sha256=proof.timing_execution_binding_sha256)
        self.plan = plan
        result = await phase._run_owned_campaign_phase(value)
        self.assertTrue(result['phase_completed'])

    def build(self):
        return producer._build_completed_campaign_payloads(self.factory)

    def publish_test_wrapper(self, payloads):
        # Test-only wrapper. The production exclusive publisher remains separate.
        profile, _ = load_artifact_contract(producer.ROOT)
        evidence = json.loads(payloads['completion_timing_evidence.json'])
        directory = self.helper.fixture.root / 'seven-files'; directory.mkdir()
        manifest = dict(schema_version='provider-completion-timed-bundle-manifest/1.0', status='sealed_payloads',
            campaign_id=self.plan['binding']['campaign_id'], artifact_contract_sha256=PROFILE_SHA256,
            timing_contract_sha256=CONTRACT_SHA256, timed_ledger_contract_sha256=LEDGER_CONTRACT_SHA256,
            plan_commitment_sha256=self.plan['plan_commitment_sha256'], execution_binding_sha256=evidence['execution_binding_sha256'],
            files={name:dict(bytes=len(value), sha256=digest(value)) for name,value in payloads.items()},
            manifest_self_hash_included=False, raw_provider_content_persisted=False, task_content_persisted=False,
            api_key_persisted=False, online_execution_authorized=False)
        started = self.factory._prepared.clock._sealing_offset()
        for name, value in payloads.items(): (directory / name).write_bytes(value)
        manifest_bytes = raw(manifest); (directory / profile['manifest_file']).write_bytes(manifest_bytes)
        ended = self.factory._prepared.clock._sealing_offset()
        publication = dict(schema_version='provider-completion-timing-publication/1.0', document_type='completion_timing_publication',
            timing_contract_sha256=CONTRACT_SHA256, plan_commitment_sha256=self.plan['plan_commitment_sha256'],
            execution_binding_sha256=evidence['execution_binding_sha256'], clock_domain_id=self.factory._prepared.clock_domain_id,
            timing_evidence_file_sha256=digest(payloads['completion_timing_evidence.json']), bundle_manifest_file_sha256=digest(manifest_bytes),
            sealing_started_ns=started, sealing_finished_ns=ended, sealing_status='sealed',
            sealing_scope='bounded_artifact_set_before_publication_receipt', online_execution_authorized=False)
        publication['publication_commitment_sha256'] = commitment('publication', publication)
        (directory / profile['publication_file']).write_bytes(raw(publication))
        return verify_timing_artifact_bundle(producer.ROOT, directory, expected_manifest_sha256=digest(manifest_bytes),
            expected_plan_commitment_sha256=self.plan['plan_commitment_sha256'],
            expected_execution_binding_sha256=evidence['execution_binding_sha256'],
            expected_denominator_plan_bytes=raw(self.factory._plan['denominator_plan']),
            expected_case_run_ids=dict(zip(self.plan['planned_case_handles'], self.factory._runs)),
            sensitive_canaries=(self.factory._opening._canary_b64.encode(),))

    def test_actual_producer_payloads_pass_independent_seven_file_verifier(self):
        with patch.object(self.factory._ledger, 'export_run', side_effect=AssertionError('lossy export forbidden')):
            files = self.build()
        self.assertEqual(len(files), 5)
        telemetry = json.loads(files['phase6_completion_telemetry.json'])
        self.assertFalse(telemetry['closure']['claim_allowed'])
        self.assertEqual(len(telemetry['runtime_denominator']['records']), 2)
        for payload in files.values():
            self.assertNotIn(b'FIXTURE-NOT-A-REAL-KEY', payload)
            self.assertNotIn(self.factory._opening._canary_b64.encode(), payload)
        checked = self.publish_test_wrapper(files)
        self.assertTrue(checked['timing_constraints_complete'])
        self.assertTrue(checked['audit_hash_chains_verified'])
        self.assertEqual((checked['run_count'],checked['segment_count']), (2,2))
        self.assertFalse(checked['current_closure_claim_allowed'])

    def test_unfinished_phase_rejected_before_database_access(self):
        self.factory._phase_finished = False
        with patch.object(producer, '_database_snapshot', side_effect=AssertionError('must not read DB')) as reader:
            with self.assertRaisesRegex(ValueError, 'campaign_artifacts_build_failed'): self.build()
        reader.assert_not_called()

    def test_untrusted_bytes_scanned_before_sqlite_open(self):
        actual = producer._database_snapshot(self.factory._ledger.database_path, 100_000_000)
        with patch.object(producer, '_database_snapshot', return_value=actual + b'Authorization: Bearer FIXTURE-LEAK'), \
             patch.object(producer, '_verify_audit_database', side_effect=AssertionError('must not open SQLite')) as opener:
            with self.assertRaisesRegex(ValueError, 'campaign_artifacts_build_failed'): self.build()
        opener.assert_not_called()

    def test_mutated_plan_cannot_replace_consumed_binding(self):
        plan = dict(self.plan); plan['binding'] = dict(plan['binding'], campaign_id='PCECAMP-'+'F'*32)
        plan['plan_commitment_sha256'] = commitment('plan',plan)
        self.factory._prepared.plan_bytes = raw(plan)
        with self.assertRaisesRegex(ValueError, 'campaign_artifacts_build_failed'): self.build()

    def test_clock_snapshot_must_match_terminal_bound_segments(self):
        self.factory._prepared.clock._completed[0]['send_started_ns'] += 1
        with self.assertRaisesRegex(ValueError, 'campaign_artifacts_build_failed'): self.build()

    def test_database_tamper_is_not_hidden_by_fresh_manifest(self):
        # SQL UPDATE is correctly blocked by the append-only trigger. Tamper the
        # test-owned serialized image without repairing its hash-covered rows.
        path = self.factory._ledger.database_path
        before = path.read_bytes(); needle = b'"network_call_index":0'
        self.assertGreater(before.count(needle), 0)
        path.write_bytes(before.replace(needle, b'"network_call_index":1'))
        with self.assertRaisesRegex(ValueError, 'campaign_artifacts_build_failed'): self.build()


if __name__ == '__main__': unittest.main()
