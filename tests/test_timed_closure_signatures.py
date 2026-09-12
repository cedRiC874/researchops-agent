"""Synthetic real cryptography, not Provider credentials or external evidence."""
import base64
import copy
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from researchops_external_closure import timed_contract as wire,timed_signatures as signatures
from researchops_external_closure.primitives import canonical_json_bytes as raw,build_signature_message
from tests.test_external_closure_documents import SyntheticPreReceipt
from tests import test_timed_closure_contract as shapes
from tests.test_provider_completion_external_preregistration_contract import _representative_documents


class TimedSignatureTests(unittest.TestCase):
    def setUp(self):
        self.network=patch('socket.socket',side_effect=AssertionError('network forbidden'))
        self.network.start(); self.addCleanup(self.network.stop)
        self.fixture=SyntheticPreReceipt()
        self.trust=self.fixture.documents['trust_manifest']
        self.profile,self.schemas=wire.load_contract()
        helper=shapes.TimedClosureContractTests()
        helper.profile,helper.schemas=self.profile,self.schemas
        helper.setUp(); self.addCleanup(helper.doCleanups)
        self.receipt=helper.value
        self.receipt['receipt_issued_at_utc']='2026-09-04T00:00:31Z'
        self.receipt['completed_at_utc']='2026-09-04T00:00:30Z'
        helper.seal(self.receipt)
        self.sign('receipt',self.receipt)

    def sign(self,kind,value,role=None):
        expected='task_custodian' if kind=='receipt' else 'ledger_witness'
        role=expected if role is None else role
        digest=value['document_sha256' if kind=='receipt' else 'entry_sha256']
        key_id=self.fixture.key_ids[role]
        message=wire.signature_message(kind,key_id=key_id,signed_sha256=digest,contract=self.profile,schemas=self.schemas)
        value['signatures']=[dict(role=expected,key_id=key_id,algorithm='ed25519',signed_sha256=digest,
            signature_b64=base64.b64encode(self.fixture.keys[role].sign(message)).decode())]

    def check(self,kind='receipt',value=None,**changes):
        value=self.receipt if value is None else value
        expected=dict(trust_manifest_bytes=raw(self.trust),expected_trust_manifest_sha256=self.trust['document_sha256'],
                      expected_document_sha256=value['document_sha256' if kind=='receipt' else 'entry_sha256'])
        expected.update(changes)
        return signatures.verify_document_signature(kind,raw(value),**expected)

    def reseal_trust(self):
        self.fixture._finalize_document(self.trust,('freeze_authority','task_custodian','ledger_witness'))

    def test_valid_custodian_signature_is_not_closure_or_admission(self):
        result=self.check()
        self.assertTrue(result.signature_verified); self.assertTrue(result.signature_time_valid)
        self.assertEqual(result.signer_role,'task_custodian')
        for name in ('oob_origin_independently_proved','artifact_evidence_verified','source_admission_verified',
                     'final_witness_chain_verified','runtime_authority_granted','closure_claim_allowed'):
            self.assertFalse(getattr(result,name))
        with self.assertRaises(FrozenInstanceError): result.closure_claim_allowed=True

    def test_wrong_independent_document_or_trust_digest_rejects(self):
        for name in ('expected_document_sha256','expected_trust_manifest_sha256'):
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'binding_mismatch'): self.check(**{name:'f'*64})

    def test_cryptographically_valid_wrong_role_key_is_rejected(self):
        self.sign('receipt',self.receipt,role='freeze_authority')
        with self.assertRaisesRegex(ValueError,'role_key_mismatch'): self.check()

    def test_invalid_signature_cannot_pass_schema_and_body_hash(self):
        self.receipt['signatures'][0]['signature_b64']=base64.b64encode(b'\0'*64).decode()
        wire.validate_document('receipt',raw(self.receipt))
        with self.assertRaisesRegex(ValueError,'signature_invalid'): self.check()

    def test_legacy_signature_domain_is_not_accepted(self):
        role='task_custodian'; key=self.fixture.key_ids[role]
        message=build_signature_message(document_type='completion_telemetry_closure_receipt',role=role,key_id=key,
                                         signed_sha256=self.receipt['document_sha256'])
        self.receipt['signatures'][0]['signature_b64']=base64.b64encode(self.fixture.keys[role].sign(message)).decode()
        with self.assertRaisesRegex(ValueError,'signature_invalid'): self.check()

    def test_role_expiry_at_action_time_rejects(self):
        self.trust['roles'][1]['expires_at_utc']=self.receipt['receipt_issued_at_utc']; self.reseal_trust()
        with self.assertRaisesRegex(ValueError,'signature_time_invalid'): self.check()

    def test_revoked_role_is_not_repaired_by_resigning_trust(self):
        self.trust['roles'][1]['revoked']=True; self.reseal_trust()
        with self.assertRaisesRegex(ValueError,'trust_schema_invalid'): self.check()

    def test_missing_trust_cosignature_rejects(self):
        self.trust['signatures'].pop()
        with self.assertRaises(ValueError): self.check()

    def test_rehashed_receipt_with_old_signature_is_rejected(self):
        self.receipt['receipt_issued_at_utc']='2026-09-04T00:00:32Z'
        self.receipt['document_sha256']=wire.receipt_sha256(self.receipt,self.profile)
        self.receipt['signatures'][0]['signed_sha256']=self.receipt['document_sha256']
        wire.validate_document('receipt',raw(self.receipt))
        with self.assertRaisesRegex(ValueError,'signature_invalid'): self.check()

    def test_invalid_trust_cosignature_cannot_authorize_valid_receipt(self):
        self.trust['signatures'][0]['signature_b64']=base64.b64encode(b'\0'*64).decode()
        with self.assertRaisesRegex(ValueError,'signature_invalid'): self.check()

    def test_role_expiry_preserves_submicrosecond_precision(self):
        self.trust['roles'][1]['expires_at_utc']='2026-09-04T00:00:31.000000001Z'
        self.reseal_trust()
        self.assertTrue(self.check().signature_time_valid)
        self.receipt['receipt_issued_at_utc']='2026-09-04T00:00:31.000000002Z'
        self.receipt['document_sha256']=wire.receipt_sha256(self.receipt,self.profile)
        self.sign('receipt',self.receipt)
        with self.assertRaisesRegex(ValueError,'signature_time_invalid'): self.check()

    def test_signed_action_before_trust_creation_is_rejected(self):
        self.receipt['receipt_issued_at_utc']='2026-09-03T23:59:59Z'
        self.receipt['completed_at_utc']='2026-09-03T23:59:58Z'
        self.receipt['document_sha256']=wire.receipt_sha256(self.receipt,self.profile)
        self.sign('receipt',self.receipt)
        with self.assertRaisesRegex(ValueError,'action_before_trust'): self.check()

    def test_witness_signature_is_distinct_from_full_witness_chain_verification(self):
        value=copy.deepcopy(_representative_documents()['external_ledger_entry_v1.schema.json'])
        value.update(schema_version='provider-completion-timed-closure-ledger-entry/2.0',document_type='completion_telemetry_timed_closure_ledger_entry',
            entry_type='closure_evidence_anchored',timed_closure_contract_sha256=wire.CONTRACT_SHA256,
            occurred_at_utc='2026-09-04T00:00:32Z',closure_receipt_sha256=self.receipt['document_sha256'],
            preregistration_envelope_sha256='b'*64,authorization_grant_sha256='c'*64,consumption_receipt_sha256='d'*64)
        value['entry_sha256']=wire.entry_sha256(value,self.profile)
        value['resulting_head_sha256']=wire.resulting_head_sha256(value['previous_head_sha256'],value['entry_sha256'],self.profile)
        self.sign('ledger',value)
        result=self.check('ledger',value)
        self.assertEqual(result.signer_role,'ledger_witness'); self.assertTrue(result.signature_verified)
        self.assertFalse(result.final_witness_chain_verified)


if __name__=='__main__': unittest.main()
