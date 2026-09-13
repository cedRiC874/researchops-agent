"""Schema/hash/signature-message conformance, not an A/B closure result."""
import base64
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from cryptography.exceptions import InvalidSignature

from researchops_external_closure import timed_contract as contract
from researchops_external_closure.primitives import canonical_json_bytes as raw,build_signature_message,derive_ed25519_key_id
from tests.test_provider_completion_external_preregistration_contract import _representative_documents


class TimedClosureContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile,cls.schemas=contract.load_contract()

    def setUp(self):
        self.network=patch('socket.socket',side_effect=AssertionError('network forbidden'))
        self.network.start(); self.addCleanup(self.network.stop)
        self.value=copy.deepcopy(_representative_documents()['closure_receipt_v1.schema.json'])
        self.value.update(schema_version='provider-completion-closure-receipt/2.0',document_type='completion_telemetry_timed_closure_receipt',
            timed_closure_contract_sha256=contract.CONTRACT_SHA256)
        self.value['timing']=dict(status='verified',plan_commitment_sha256='a'*64,execution_binding_sha256='b'*64,
            source_recipe_sha256='d07be82febc00009b03a63541c3a86d3dea81f9bd00983c157e41f28eeaded08',
            first_live_implementation_commitment_sha256='b07da916da4f444b55bd499ab436d85e35ed28c88dc821bc3817508252e1bc09',
            evidence_file_sha256='c'*64,publication_file_sha256='d'*64,clock_domain_id='PCECLOCK-'+'A'*32,
            phase_duration_ns=100,last_completed_write_stage='timing_publication_written',gate_complete=True,error_code=None,incomplete_reasons=[])

    def seal(self,value):
        value['receipt_id']=contract.receipt_id(value,self.profile)
        value['closure_reasons_commitment_sha256']=contract.reasons_sha256(value['closure_reasons'],self.profile)
        value['document_sha256']=contract.receipt_sha256(value,self.profile)
        for signature in value['signatures']:
            signature['signed_sha256']=value['document_sha256']
            signature['signature_b64']=base64.b64encode(b'\0'*64).decode()
        return raw(value)

    def invalid(self,value,code):
        value.update(evidence_valid=False,evidence_error_code=code,pre_anchor_closure_eligible=False,closure_reasons=[])
        for name in ('observed_audit_run_set_commitment_sha256','ordered_final_chain_heads_commitment_sha256',
                     'attempt_count','accepted_response_count','usage_complete','observed_input_tokens','observed_output_tokens','observed_cost_cny'):
            value[name]=None

    def test_pinned_schemas_and_complete_receipt_shape(self):
        self.assertEqual(len(self.schemas),4)
        value=contract.validate_document('receipt',self.seal(self.value))
        self.assertTrue(value['timing']['gate_complete'])
        self.assertEqual(len(self.schemas[contract.SCHEMAS['receipt']]['required']),62)
        self.assertFalse(self.profile['boundaries']['online_execution_authorized'])
        # The dummy signature is deliberately not called verified by this helper.

    def test_incomplete_timing_cannot_keep_pre_anchor_eligibility(self):
        self.value['timing'].update(status='incomplete',gate_complete=False,incomplete_reasons=['missing_phase_offsets'])
        with self.assertRaises(ValueError): contract.validate_document('receipt',self.seal(self.value))
        self.value['pre_anchor_closure_eligible']=False
        self.value['closure_reasons']=['timing_constraints_incomplete']
        contract.validate_document('receipt',self.seal(self.value))

    def test_nested_timing_cannot_change_without_body_hash_change(self):
        payload=self.seal(self.value); value=json.loads(payload)
        value['timing']['phase_duration_ns']=101
        with self.assertRaisesRegex(ValueError,'receipt_hash_mismatch'): contract.validate_document('receipt',raw(value))

    def test_trailing_newline_ids_and_float_offsets_are_rejected(self):
        for key,new in (('clock_domain_id','PCECLOCK-'+'A'*32+'\n'),('phase_duration_ns',100.0)):
            value=copy.deepcopy(self.value); value['timing'][key]=new
            with self.subTest(key=key),self.assertRaises(ValueError): contract.validate_document('receipt',self.seal(value))

    def test_reason_order_and_unknown_reason_reject(self):
        for reasons in (['timing_publication_unverified','timing_constraints_incomplete'],['unknown_reason'],['timing_constraints_incomplete']*2):
            with self.subTest(reasons=reasons),self.assertRaises(ValueError): contract.reasons_sha256(reasons,self.profile)

    def test_new_receipt_signature_domain_does_not_accept_legacy_message(self):
        payload=self.seal(self.value); value=json.loads(payload)
        key=Ed25519PrivateKey.from_private_bytes(b'\x17'*32)
        public=base64.b64encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()
        key_id=derive_ed25519_key_id(public)
        message=contract.signature_message('receipt',key_id=key_id,signed_sha256=value['document_sha256'],contract=self.profile,schemas=self.schemas)
        signature=key.sign(message); key.public_key().verify(signature,message)
        legacy=build_signature_message(document_type='completion_telemetry_closure_receipt',role='task_custodian',key_id=key_id,signed_sha256=value['document_sha256'])
        with self.assertRaises(InvalidSignature): key.public_key().verify(signature,legacy)

    def test_new_ledger_hashes_bind_contract_and_do_not_reuse_old_domains(self):
        from researchops_external_closure.primitives import compute_ledger_entry_sha256
        value=copy.deepcopy(_representative_documents()['external_ledger_entry_v1.schema.json'])
        value.update(schema_version='provider-completion-timed-closure-ledger-entry/2.0',document_type='completion_telemetry_timed_closure_ledger_entry',
                     entry_type='closure_evidence_anchored',timed_closure_contract_sha256=contract.CONTRACT_SHA256,closure_receipt_sha256='a'*64,
                     preregistration_envelope_sha256='b'*64,authorization_grant_sha256='c'*64,consumption_receipt_sha256='d'*64)
        value['entry_sha256']=contract.entry_sha256(value,self.profile)
        value['resulting_head_sha256']=contract.resulting_head_sha256(value['previous_head_sha256'],value['entry_sha256'],self.profile)
        for signature in value['signatures']:
            signature['role']='ledger_witness'; signature['signed_sha256']=value['entry_sha256']; signature['signature_b64']=base64.b64encode(b'\0'*64).decode()
        with self.assertRaisesRegex(ValueError,'ledger_document_type_invalid'): compute_ledger_entry_sha256(value)
        contract.validate_document('ledger',raw(value))

    def test_all_timed_write_stages_have_exact_core_stage_and_error_projection(self):
        base=_representative_documents()['postrun_attested_facts_v1.schema.json']
        for stage,(core,error) in self.profile['core_stage_and_error_projection'].items():
            value=copy.deepcopy(base)
            value.update(schema_version='provider-completion-postrun-attested-facts/2.0',timed_last_completed_write_stage=stage,
                         last_completed_artifact_write_stage=core,artifact_error_code=error)
            with self.subTest(stage=stage): contract.validate_document('facts',raw(value))

    def test_quarantine_cannot_publish_timing_file_hashes(self):
        value=copy.deepcopy(self.value)
        value.update(artifact_publication_disposition='privacy_unverified_quarantined',public_generic_privacy_scan_status='unavailable',
                     closure_bundle_manifest_sha256=None,partial_artifacts={},evidence_valid=False,
                     evidence_error_code='closure_sensitive_scan_unavailable',pre_anchor_closure_eligible=False,closure_reasons=[])
        self.invalid(value,'closure_sensitive_scan_unavailable')
        value['timing'].update(status='quarantined',gate_complete=False,evidence_file_sha256=None,publication_file_sha256=None,
                               clock_domain_id=None,phase_duration_ns=None)
        contract.validate_document('receipt',self.seal(value))
        value['timing']['evidence_file_sha256']='c'*64
        with self.assertRaises(ValueError): contract.validate_document('receipt',self.seal(value))

    def test_normal_partial_prefix_shapes_are_not_forced_into_four_files(self):
        for index,stage in enumerate(self.profile['timed_write_stages'][:-1]):
            value=copy.deepcopy(self.value); core,error=self.profile['core_stage_and_error_projection'][stage]
            value.update(bundle_status='manifestless_failure' if index<6 else 'manifest_present_invalid',
                last_completed_artifact_write_stage=core,artifact_error_code=error,closure_bundle_manifest_sha256=None,
                evidence_valid=False,evidence_error_code='closure_bundle_manifest_invalid',pre_anchor_closure_eligible=False,
                closure_reasons=[],
                partial_artifacts={name:{'bytes':1,'sha256':'a'*64} for name in self.profile['artifact_write_order'][:index]})
            value['timing'].update(status='unavailable',gate_complete=False,evidence_file_sha256=None,publication_file_sha256=None,
                clock_domain_id=None,phase_duration_ns=None,last_completed_write_stage=stage)
            self.invalid(value,'closure_bundle_manifest_invalid')
            with self.subTest(stage=stage): contract.validate_document('receipt',self.seal(value))

    def test_empty_failed_next_file_can_be_represented(self):
        value=copy.deepcopy(self.value)
        value.update(bundle_status='manifestless_failure',last_completed_artifact_write_stage='not_started',
            artifact_error_code='pce_artifact_not_started_failed',closure_bundle_manifest_sha256=None,
            partial_artifacts={'phase6_audit.sqlite3':{'bytes':0,'sha256':'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'}})
        self.invalid(value,'closure_bundle_manifest_invalid')
        value['timing'].update(status='unavailable',gate_complete=False,evidence_file_sha256=None,publication_file_sha256=None,
            clock_domain_id=None,phase_duration_ns=None,last_completed_write_stage='not_started')
        contract.validate_document('receipt',self.seal(value))

    def test_empty_completed_file_and_nonprefix_partial_map_reject(self):
        value=copy.deepcopy(self.value)
        value.update(bundle_status='manifestless_failure',last_completed_artifact_write_stage='audit_database_written',
            artifact_error_code='pce_artifact_audit_index_failed',closure_bundle_manifest_sha256=None,
            partial_artifacts={'phase6_audit.sqlite3':{'bytes':0,'sha256':'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'}})
        self.invalid(value,'closure_bundle_manifest_invalid')
        value['timing'].update(status='unavailable',gate_complete=False,evidence_file_sha256=None,publication_file_sha256=None,
            clock_domain_id=None,phase_duration_ns=None,last_completed_write_stage='audit_database_written')
        with self.assertRaisesRegex(ValueError,'partial_empty_file_invalid'): contract.validate_document('receipt',self.seal(value))
        value['partial_artifacts']={'phase6_audit_index.json':{'bytes':1,'sha256':'a'*64}}
        with self.assertRaises(ValueError): contract.validate_document('receipt',self.seal(value))

    def test_legacy_receipt_schema_rejects_new_version(self):
        import jsonschema
        parent=json.loads((contract.ROOT/'evals/provider_completion_external_preregistration_v1/schemas/closure_receipt_v1.schema.json').read_bytes())
        value=json.loads(self.seal(self.value))
        self.assertFalse(jsonschema.Draft202012Validator(parent).is_valid(value))

    def test_preserved_draft_empty_reason_hash_conflict_is_machine_visible(self):
        archive=contract.ROOT/'docs/diagnostics/timed-closure-pre-freeze-correction-20260908'
        for folder,expected in (
            ('timed-closure-pre-freeze-correction-20260908','b53bf6de3be627138ea367f51a2d41dc5569d4c139cf063aa6346cf4a8dc415c'),
            ('timed-closure-before-empty-partial-20260908','d1b2217704b67ec6216b7bf43af642b76eaec956eee319d44052cf7b25c16d3d'),
            ('timed-closure-before-prefix-expansion-20260908','19681888a0b7b686f36e9a6503b6c82efd406c63ad5509e7ea519b1984281265')):
            directory=contract.ROOT/'docs/diagnostics'/folder
            payload=(directory/'contract_v1.json').read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(),expected)
            for item in json.loads(payload)['schema_bindings']:
                saved=(directory/item['path'].rsplit('/',1)[1]).read_bytes()
                self.assertEqual((len(saved),hashlib.sha256(saved).hexdigest()),(item['bytes'],item['sha256']))
        old=json.loads((archive/'closure_receipt_v2.schema.json').read_bytes())
        pinned=old['allOf'][27]['else']['properties']['closure_reasons_commitment_sha256']['const']
        self.assertEqual(pinned,'2507147c335dbc6ec940d57f870df0275bc8143fec60f99385ede8d5bc2642cf')
        self.assertNotEqual(pinned,contract.reasons_sha256([],self.profile))
        self.assertEqual(contract.reasons_sha256([],self.profile),self.profile['empty_reason_list_commitment_sha256'])


if __name__=='__main__': unittest.main()
