"""One synthetic signed campaign tied to actual temporary timed admission Git."""
from __future__ import annotations

import copy
import json
import base64
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta

from researchops_completion_timing.contract import digest, runtime_plan_core_commitment
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests.timed_admission_fixture import TimedAdmissionFixture
from tests.test_completion_execution_scope import scope_value
from tests.test_completion_timing_contract import TimingAuthoringTests
from tests.test_completion_timing_pre_execution import resign, documents
from tests.test_external_closure_documents import _domain_hash, RUNTIME_PLAN_DOMAIN


def _shift(value):
    if type(value) is dict: return {key: _shift(item) for key, item in value.items()}
    if type(value) is list: return [_shift(item) for item in value]
    if type(value) is str:
        if value.startswith(('2026-09-04T', '2026-09-05T')):
            stamp = datetime.strptime(value[:19], '%Y-%m-%dT%H:%M:%S') + timedelta(days=2, hours=1, seconds=57)
            return stamp.strftime('%Y-%m-%dT%H:%M:%S') + value[19:]
        if value == '2026-09-04': return '2026-09-06'
    return value


class TimedCampaignFixture(TimedAdmissionFixture):
    def __init__(self, *, implementation_version=4, current_campaign_runtime=False):
        super().__init__(implementation_version=implementation_version, current_campaign_runtime=current_campaign_runtime)
        try:
            self._join()
        except BaseException:
            self.close(); raise

    def set_execution_environment(self, environment_id):
        runtime = self.signers.documents['preregistration_envelope']['runtime_plan']
        runtime['execution_scope'] = scope_value(environment_id)
        plan = json.loads(self.campaign_timing_plan)
        plan['binding']['runtime_plan_core_sha256'] = runtime_plan_core_commitment(runtime)
        resign(self.signers, plan, envelope_version=3)
        self.campaign_documents = documents(self.signers)
        self.campaign_observation = raw(self.signers.observation)
        self.campaign_timing_plan = raw(plan)

    def custodian_opening_package(self):
        """Create/resign ONLY synthetic test data after its candidate freeze."""
        from tests import test_completion_campaign_opening as opening_fixture
        opening = opening_fixture.CampaignOpeningTests(); opening.setUp()
        envelope = self.signers.documents['preregistration_envelope']
        opening.envelope = envelope
        opening.seen = tuple(hashlib.sha256(('synthetic seen ' + str(index)).encode()).hexdigest() for index in range(21))
        seen = self.signers.documents['seen_case_exclusion_manifest']
        seen['exclusion_digest_set_count'] = len(opening.seen)
        seen['exclusion_digest_set_commitment_sha256'] = opening.hash('researchops-provider-completion-seen-case-exclusion-set-v1', sorted(opening.seen))
        self.signers._finalize_document(seen, ('freeze_authority', 'task_custodian'))
        envelope['task_custody']['seen_task_exclusion_manifest_sha256'] = seen['document_sha256']
        opening.bundle.update(candidate_freeze_receipt_sha256=envelope['execution_binding']['candidate_freeze_receipt_sha256'],
            seen_case_exclusion_manifest_sha256=seen['document_sha256'])
        opening.reseal()
        runtime = envelope['runtime_plan']
        campaign_hash = hashlib.sha256(b'researchops-provider-completion-campaign-id-v1\0' + envelope['envelope_id'].encode() + b'\0'
            + bytes.fromhex(envelope['task_custody']['task_bundle_commitment_sha256'])).hexdigest()
        runtime['campaign_topology']['campaign_id'] = 'PCECAMP-' + campaign_hash[:32].upper()
        plan = json.loads(self.campaign_timing_plan)
        plan['binding'].update(campaign_id=runtime['campaign_topology']['campaign_id'], runtime_plan_core_sha256=runtime_plan_core_commitment(runtime))
        resign(self.signers, plan, envelope_version=3)
        self.campaign_documents = documents(self.signers)
        self.campaign_observation = raw(self.signers.observation)
        self.campaign_timing_plan = raw(plan)
        return raw(dict(schema_version='provider-completion-custodian-opening/1.0', bundle=opening.bundle,
            secret_salt_b64=base64.b64encode(opening.salt).decode(), seen_task_digests=list(opening.seen)))

    def _join(self):
        fixture = self.signers
        old_observation = fixture.observation
        fixture.documents = {name: _shift(value) for name, value in fixture.documents.items()}
        fixture.documents['trust_manifest'] = copy.deepcopy(self.trust)
        fixture.observation = _shift(old_observation)
        fixture.observation['trust_manifest_observation'] = copy.deepcopy(old_observation['trust_manifest_observation'])
        fixture.observation['trust_manifest_observation']['document_sha256'] = self.trust['document_sha256']
        for name in ('manifest_observation', 'closure_receipt_observation', 'closure_evidence_anchor'):
            fixture.observation.pop(name)
        fixture.observation.update(schema_version='provider-completion-external-pre-execution-observation/1.0', mode='pre_execution')
        docs = fixture.documents
        candidate = docs['candidate_freeze_receipt']
        candidate.update(candidate_commit=self.binding['execution_commit'], candidate_tree=self.binding['execution_tree'])
        for name in ('source_integrity_commitment_sha256', 'runtime_registry_commitment_sha256', 'implementation_commitment_sha256',
                     'runner_config_commitment_sha256', 'closure_evidence_contract_commitment_sha256', 'dependency_lock_sha256'):
            candidate[name] = self.binding[name]
        fixture._finalize_document(candidate, ('freeze_authority',))
        old = docs['candidate_frozen_ledger_entry']
        entry = fixture._ledger_entry(entry_type='candidate_frozen', sequence=old['sequence'], previous_head=old['previous_head_sha256'],
            occurred_at=old['occurred_at_utc'], candidate_hash=candidate['document_sha256'], envelope_hash=None, grant_hash=None, consumption_hash=None)
        docs['candidate_frozen_ledger_entry'] = entry
        seen = docs['seen_case_exclusion_manifest']; seen['candidate_freeze_receipt_sha256'] = candidate['document_sha256']
        fixture._finalize_document(seen, ('freeze_authority', 'task_custodian'))
        binding = dict(self.binding, candidate_freeze_receipt_sha256=candidate['document_sha256'],
                       candidate_freeze_ledger_entry_sha256=entry['entry_sha256'])
        envelope = docs['preregistration_envelope']; envelope['execution_binding'] = binding
        envelope['candidate_frozen_at_utc'] = candidate['frozen_at_utc']
        envelope['task_custody']['seen_task_exclusion_manifest_sha256'] = seen['document_sha256']
        envelope['external_trust'].update(trust_manifest_sha256=self.trust['document_sha256'], ledger_base_sequence=entry['sequence'],
                                           ledger_base_head_sha256=entry['resulting_head_sha256'])
        docs['preregistration_frozen_ledger_entry']['previous_head_sha256'] = entry['resulting_head_sha256']
        runtime = envelope['runtime_plan']; denominator = runtime['denominator_plan']; budget = runtime['budget_policy']
        runtime['execution_scope'] = scope_value()
        for name in ('provider_id', 'api_surface', 'transport_id', 'adapter_version', 'telemetry_schema_sha256', 'mapping_sha256'):
            denominator[name] = binding[name]
        runtime['denominator_plan_commitment_sha256'] = digest(raw(denominator))
        budget_body = dict(budget); budget_body.pop('budget_policy_commitment_sha256')
        budget['budget_policy_commitment_sha256'] = _domain_hash(RUNTIME_PLAN_DOMAIN, b'budget_policy', raw(budget_body))
        grant = docs['authorization_grant']
        grant.update(candidate_commit=binding['execution_commit'], candidate_tree=binding['execution_tree'],
            source_integrity_commitment_sha256=binding['source_integrity_commitment_sha256'],
            runtime_registry_commitment_sha256=binding['runtime_registry_commitment_sha256'],
            denominator_plan_commitment_sha256=runtime['denominator_plan_commitment_sha256'],
            budget_policy_commitment_sha256=budget['budget_policy_commitment_sha256'])
        consumption = docs['consumption_receipt']
        consumption.update(candidate_commit=binding['execution_commit'], denominator_plan_commitment_sha256=runtime['denominator_plan_commitment_sha256'])
        plan = TimingAuthoringTests().fixture()['plan']
        plan['binding'] = {name: binding[name] for name in ('execution_commit', 'execution_tree', 'source_integrity_commitment_sha256')}
        plan['binding'].update({name: denominator[name] for name in ('provider_id', 'api_surface', 'transport_id', 'adapter_version')})
        plan['binding'].update(campaign_id=runtime['campaign_topology']['campaign_id'], runtime_plan_core_sha256=runtime_plan_core_commitment(runtime))
        limits = runtime['transport_limits']
        plan.update(planned_case_handles=list(denominator['case_ids']), max_attempts=denominator['total_model_request_cap'],
            max_attempts_per_case=denominator['max_turns_per_case'], request_timeout_ns=limits['request_timeout_seconds'] * 1_000_000_000,
            phase_timeout_ns=limits['total_timeout_seconds'] * 1_000_000_000)
        resign(fixture, plan, envelope_version=3)
        for name in ('entry_sha256', 'ledger_id', 'entry_type', 'sequence', 'previous_head_sha256', 'resulting_head_sha256'):
            fixture.observation['candidate_freeze_anchor'][name] = entry[name]
        observation = dict(self.observation, candidate_freeze_receipt_sha256=candidate['document_sha256'],
                           candidate_frozen_at_utc=candidate['frozen_at_utc'])
        self.campaign_admission_inputs = replace(self.inputs(), observation_bytes=raw(observation),
            expected_candidate_freeze_receipt_sha256=candidate['document_sha256'], expected_candidate_frozen_at_utc=candidate['frozen_at_utc'])
        self.campaign_documents = documents(fixture)
        self.campaign_observation = raw(fixture.observation)
        self.campaign_timing_plan = raw(plan)
        self.campaign_as_of = '2026-09-06T01:01:14Z'
