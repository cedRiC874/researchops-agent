"""Actual A/B signed source/artifact flow on synthetic data, not live evidence."""
import base64
import json
import unittest
from unittest.mock import patch

from researchops_external_closure import timed_final as final, timed_evaluator as evaluator, timed_contract as wire
from researchops_external_closure.primitives import canonical_json_bytes as raw
from researchops_external_closure.types import FinalDocumentBytes
from tests.test_timed_evaluator_full import TimedEvaluatorFullFixture
from tests import test_external_closure_evaluator as legacy_fixture


class TimedFinalFullTests(unittest.TestCase):
    def test_real_A_B_with_new_signatures_final_witness_and_exact_A_rerun(self):
        fixture = TimedEvaluatorFullFixture(); self.addCleanup(fixture.close)
        root = fixture.repository.root
        projected = evaluator.evaluate_timed_closure_bundle(root, fixture.full_documents, **fixture.full_arguments)
        self.assertIs(type(projected), evaluator.TimedReceiptProjectionReady, getattr(projected, 'error_code', None))
        self.assertTrue(projected.pre_anchor_closure_eligible, projected.evidence_error_code)
        profile, schemas = wire.load_contract(root)
        def sign(kind, value):
            role = 'task_custodian' if kind == 'receipt' else 'ledger_witness'
            key = fixture.signers.key_ids[role]
            digest = value['document_sha256' if kind == 'receipt' else 'entry_sha256']
            message = wire.signature_message(kind, key_id=key, signed_sha256=digest, contract=profile, schemas=schemas)
            value['signatures'] = [dict(role=role, key_id=key, algorithm='ed25519', signed_sha256=digest,
                signature_b64=base64.b64encode(fixture.signers.keys[role].sign(message)).decode())]
        receipt = json.loads(projected.unsigned_receipt_canonical_json)
        receipt['document_sha256'] = projected.receipt_document_sha256; sign('receipt', receipt)
        docs = fixture.signers.documents; consumed = docs['authorization_consumed_ledger_entry']
        entry = dict(schema_version='provider-completion-timed-closure-ledger-entry/2.0',
            document_type='completion_telemetry_timed_closure_ledger_entry', timed_closure_contract_sha256=wire.CONTRACT_SHA256,
            entry_type='closure_evidence_anchored', ledger_id=consumed['ledger_id'], sequence=consumed['sequence'] + 1,
            previous_head_sha256=consumed['resulting_head_sha256'], occurred_at_utc='2026-09-06T01:03:04Z',
            candidate_freeze_receipt_sha256=docs['candidate_freeze_receipt']['document_sha256'],
            preregistration_envelope_sha256=docs['preregistration_envelope']['document_sha256'],
            authorization_grant_sha256=docs['authorization_grant']['document_sha256'],
            consumption_receipt_sha256=docs['consumption_receipt']['document_sha256'], closure_receipt_sha256=receipt['document_sha256'])
        entry['entry_sha256'] = wire.entry_sha256(entry, profile)
        entry['resulting_head_sha256'] = wire.resulting_head_sha256(entry['previous_head_sha256'], entry['entry_sha256'], profile)
        sign('ledger', entry)
        observation = legacy_fixture._final_observation_bytes(fixture.full_arguments['postrun_observation'],
            receipt_sha256=receipt['document_sha256'], receipt_observed_at_utc='2026-09-06T01:03:03Z',
            entry=entry, entry_observed_at_utc='2026-09-06T01:03:05Z')
        actual = evaluator.evaluate_timed_closure_bundle; reruns = []
        def traced(*args, **kwargs):
            value = actual(*args, **kwargs); reruns.append(value); return value
        with patch.object(evaluator, 'evaluate_timed_closure_bundle', side_effect=traced), \
             patch('socket.socket', side_effect=AssertionError('no Provider/network')):
            result = final.verify_closed_timed_campaign(root,
                FinalDocumentBytes(fixture.full_documents, raw(receipt), raw(entry)),
                final_observation=observation, **fixture.full_arguments)
        self.assertEqual(len(reruns), 1)
        self.assertEqual(result.status, 'closed_evidence_verified',
            (result.error_code, [(type(value).__name__, getattr(value, 'error_code', None), getattr(value, 'evidence_error_code', None)) for value in reruns]))
        self.assertEqual(reruns[0].unsigned_receipt_canonical_json, projected.unsigned_receipt_canonical_json)
        self.assertTrue(result.evidence_valid); self.assertTrue(result.closure_claim_allowed)
        self.assertFalse(result.runtime_authority_granted)
        self.assertEqual(result.verification_provider_calls, 0)


if __name__ == '__main__': unittest.main()
