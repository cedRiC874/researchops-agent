"""New-domain synthetic signatures and actual A rerun; not real campaign proof."""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops_external_closure import timed_final as final, timed_evaluator as evaluator, timed_contract as wire
from researchops_external_closure.primitives import canonical_json_bytes as raw, build_signature_message
from researchops_external_closure.types import FinalDocumentBytes
from tests import test_timed_evaluator as a_fixture
from tests import test_completion_timing_pre_execution as pre_fixture
from tests import test_external_closure_evaluator as legacy_fixture


class TimedFinalFixture:
    def __init__(self, *, quarantine=True):
        self.case = a_fixture.TimedEvaluatorTests(); self.case.setUp()
        self.signers = self.case.helper.fixture
        self.profile, self.schemas = wire.load_contract()
        self.temporary = None
        if quarantine:
            self.case.helper.facts['custodian_private_leak_canary_scan_status'] = 'leak_detected'
            self.case.helper.after['manifest_observation'].update(state='withheld_quarantined', manifest_sha256=None,
                quarantine_reason='sensitive_detected')
            self.directory = None
        else:
            self.temporary = tempfile.TemporaryDirectory()
            self.directory = Path(self.temporary.name)
            self.case.set_stage('not_started')
            self.case.helper.after['manifest_observation'].update(state='absent', manifest_sha256=None)
        projected = self.case.check(self.directory)
        if type(projected) is not evaluator.TimedReceiptProjectionReady:
            self.close(); raise AssertionError(getattr(projected, 'error_code', 'A projection failed'))
        self.receipt = json.loads(projected.unsigned_receipt_canonical_json)
        consumed = self.signers.documents['authorization_consumed_ledger_entry']
        self.entry = dict(schema_version='provider-completion-timed-closure-ledger-entry/2.0',
            document_type='completion_telemetry_timed_closure_ledger_entry', entry_type='closure_evidence_anchored',
            timed_closure_contract_sha256=wire.CONTRACT_SHA256, ledger_id=consumed['ledger_id'], sequence=consumed['sequence'] + 1,
            previous_head_sha256=consumed['resulting_head_sha256'], occurred_at_utc='2026-09-04T00:00:23Z',
            candidate_freeze_receipt_sha256=self.signers.documents['candidate_freeze_receipt']['document_sha256'],
            preregistration_envelope_sha256=self.signers.documents['preregistration_envelope']['document_sha256'],
            authorization_grant_sha256=self.signers.documents['authorization_grant']['document_sha256'],
            consumption_receipt_sha256=self.signers.documents['consumption_receipt']['document_sha256'])
        self.seal()

    def close(self):
        if self.temporary is not None: self.temporary.cleanup()

    def sign(self, kind, value):
        role = 'task_custodian' if kind == 'receipt' else 'ledger_witness'
        digest = value['document_sha256' if kind == 'receipt' else 'entry_sha256']
        key = self.signers.key_ids[role]
        message = wire.signature_message(kind, key_id=key, signed_sha256=digest, contract=self.profile, schemas=self.schemas)
        value['signatures'] = [dict(role=role, key_id=key, algorithm='ed25519', signed_sha256=digest,
            signature_b64=base64.b64encode(self.signers.keys[role].sign(message)).decode())]

    def seal(self):
        self.receipt['document_sha256'] = wire.receipt_sha256(self.receipt, self.profile)
        self.sign('receipt', self.receipt)
        self.entry['closure_receipt_sha256'] = self.receipt['document_sha256']
        self.entry['entry_sha256'] = wire.entry_sha256(self.entry, self.profile)
        self.entry['resulting_head_sha256'] = wire.resulting_head_sha256(self.entry['previous_head_sha256'], self.entry['entry_sha256'], self.profile)
        self.sign('ledger', self.entry)
        self.observation = json.loads(legacy_fixture._final_observation_bytes(raw(self.case.helper.after),
            receipt_sha256=self.receipt['document_sha256'], receipt_observed_at_utc='2026-09-04T00:00:22Z',
            entry=self.entry, entry_observed_at_utc='2026-09-04T00:00:24Z'))

    def arguments(self):
        return dict(pre_execution_observation=raw(self.signers.observation), postrun_observation=raw(self.case.helper.after),
            final_observation=raw(self.observation), timing_plan_bytes=raw(self.case.helper.plan), artifact_directory=self.directory,
            postrun_attested_facts=raw(self.case.helper.facts))

    def verify(self, **changes):
        documents = FinalDocumentBytes(pre_fixture.documents(self.signers), raw(self.receipt), raw(self.entry))
        return final.verify_closed_timed_campaign(wire.ROOT, documents, **(self.arguments() | changes))


class TimedFinalTests(unittest.TestCase):
    def fixture(self, **options):
        value = TimedFinalFixture(**options); self.addCleanup(value.close)
        return value

    def test_quarantine_attests_failure_without_A_or_private_input_access(self):
        value = self.fixture()
        class Hostile:
            def __fspath__(self): raise AssertionError('private path accessed')
            def __bytes__(self): raise AssertionError('private facts accessed')
        with patch.object(evaluator, 'evaluate_timed_closure_bundle', side_effect=AssertionError('no A rerun')) as called, \
             patch('socket.socket', side_effect=AssertionError('no network')):
            result = value.verify(artifact_directory=Hostile(), postrun_attested_facts=Hostile(), admission_inputs=Hostile())
        self.assertEqual(called.call_count, 0)
        self.assertEqual(result.status, 'closed_failure_attested', result.error_code)
        self.assertFalse(result.evidence_valid); self.assertFalse(result.closure_claim_allowed)
        self.assertFalse(result.runtime_authority_granted)

    def test_normal_partial_failure_requires_one_actual_A_rerun(self):
        value = self.fixture(quarantine=False)
        with patch.object(evaluator, 'evaluate_timed_closure_bundle', wraps=evaluator.evaluate_timed_closure_bundle) as called:
            result = value.verify()
        self.assertEqual(called.call_count, 1)
        self.assertEqual(result.status, 'closed_failure_attested', result.error_code)
        self.assertFalse(result.closure_claim_allowed)

    def test_validly_resigned_alternate_final_head_is_not_the_consumed_chain(self):
        value = self.fixture(); value.entry['previous_head_sha256'] = 'f' * 64; value.seal()
        result = value.verify()
        self.assertEqual(result.error_code, 'closure_external_anchor_mismatch')

    def test_validly_resigned_receipt_cannot_change_the_verified_timing_binding(self):
        value = self.fixture(); value.receipt['timing']['plan_commitment_sha256'] = 'f' * 64; value.seal()
        result = value.verify()
        self.assertEqual(result.error_code, 'closure_receipt_projection_mismatch')

    def test_old_signature_domain_cannot_authorize_new_receipt(self):
        value = self.fixture(); role = 'task_custodian'; key = value.signers.key_ids[role]
        message = build_signature_message(document_type='completion_telemetry_closure_receipt', role=role,
            key_id=key, signed_sha256=value.receipt['document_sha256'])
        value.receipt['signatures'][0]['signature_b64'] = base64.b64encode(value.signers.keys[role].sign(message)).decode()
        self.assertEqual(value.verify().error_code, 'closure_receipt_signature_invalid')

    def test_postrun_observation_cannot_silently_change_at_finalization(self):
        value = self.fixture()
        value.observation['manifest_observation']['observation_source_commitment_sha256'] = 'f' * 64
        self.assertEqual(value.verify().error_code, 'closure_external_anchor_mismatch')

    def test_normal_facts_change_cannot_be_replaced_by_the_signed_receipt(self):
        value = self.fixture(quarantine=False)
        facts = copy.deepcopy(value.case.helper.facts); facts['network_attempt_count_observed'] = 3
        with patch.object(evaluator, 'evaluate_timed_closure_bundle', wraps=evaluator.evaluate_timed_closure_bundle) as called:
            result = value.verify(postrun_attested_facts=raw(facts))
        self.assertEqual(called.call_count, 1)
        self.assertEqual(result.error_code, 'closure_receipt_projection_mismatch')

    def test_independent_receipt_digest_is_not_a_signature_substitute(self):
        value = self.fixture(); value.observation['closure_receipt_observation']['document_sha256'] = 'f' * 64
        self.assertEqual(value.verify().error_code, 'closure_external_anchor_mismatch')


if __name__ == '__main__': unittest.main()
