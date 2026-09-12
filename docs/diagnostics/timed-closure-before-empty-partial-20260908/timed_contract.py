"""Timed A/B document conformance only; not signature/admission/closure proof."""
from __future__ import annotations

import hashlib
from pathlib import Path

import jsonschema

from .io import read_regular_file_no_follow
from .primitives import canonical_json_bytes as raw, decode_strict_json_object, _require_sha256, _decode_canonical_base64, _KEY_ID_RE, _LEDGER_ID_RE
from .schema_guard import only_local_schema_references
from .errors import ExternalClosurePrimitiveError
from researchops_completion_timing.contract import _decode

ROOT=Path(__file__).resolve().parents[2]
CONTRACT_PATH='evals/provider_completion_timed_closure_v1/contract_v1.json'
CONTRACT_SHA256='d1b2217704b67ec6216b7bf43af642b76eaec956eee319d44052cf7b25c16d3d'
CONTRACT_BYTES=18704
SCHEMAS={'receipt':'closure_receipt_v2.schema.json','ledger':'closure_ledger_entry_v2.schema.json',
         'observation':'closure_observation_v2.schema.json','facts':'postrun_facts_v2.schema.json'}


def _fail(code): raise ExternalClosurePrimitiveError('timed_closure_'+code) from None
def _sha(payload): return hashlib.sha256(payload).hexdigest()
def _hash(domain,value): return _sha(domain.encode()+b'\0'+raw(value))


def load_contract(root=ROOT):
    payload=read_regular_file_no_follow(root/CONTRACT_PATH,max_bytes=32768)
    if len(payload)!=CONTRACT_BYTES or _sha(payload)!=CONTRACT_SHA256: _fail('contract_invalid')
    contract=decode_strict_json_object(payload,max_bytes=32768)
    schemas={}
    for item in contract['fixed_bindings']+contract['schema_bindings']:
        value=read_regular_file_no_follow(root/item['path'],max_bytes=max(131072,item['bytes']))
        if len(value)!=item['bytes'] or _sha(value)!=item['sha256']: _fail('dependency_drift')
        if item in contract['schema_bindings']:
            schema=decode_strict_json_object(value,max_bytes=contract['limits']['schema_bytes'])
            if not only_local_schema_references(schema): _fail('schema_reference_invalid')
            jsonschema.Draft202012Validator.check_schema(schema)
            schemas[Path(item['path']).name]=schema
    return contract,schemas


def receipt_id(value,contract):
    body={name:value[name] for name in ('campaign_id','authorization_grant_sha256','authorization_consumption_entry_sha256')}
    return 'PCECLOSE-'+_hash(contract['domains']['receipt_id'],body)[:32].upper()


def receipt_sha256(value,contract):
    body=dict(value)
    for name in contract['byte_protocol']['receipt_body_omissions']: body.pop(name,None)
    return _hash(contract['domains']['receipt'],body)


def reasons_sha256(reasons,contract):
    if type(reasons) is not list or reasons!=[reason for reason in contract['reason_order'] if reason in reasons]:
        _fail('reason_order_invalid')
    return _hash(contract['domains']['reasons'],reasons)


def entry_sha256(value,contract):
    if type(value) is not dict or value.get('document_type')!='completion_telemetry_timed_closure_ledger_entry':
        _fail('ledger_type_invalid')
    ledger_id=value.get('ledger_id'); sequence=value.get('sequence')
    if type(ledger_id) is not str or _LEDGER_ID_RE.fullmatch(ledger_id) is None: _fail('ledger_id_invalid')
    if type(sequence) is not int or not 1<=sequence<=9007199254740991: _fail('ledger_sequence_invalid')
    previous=_require_sha256(value.get('previous_head_sha256'))
    body=dict(value)
    for name in contract['byte_protocol']['ledger_entry_omissions']: body.pop(name,None)
    return _sha(contract['domains']['ledger_entry'].encode()+b'\0'+ledger_id.encode('ascii')+b'\0'
                +sequence.to_bytes(8,'big')+bytes.fromhex(previous)+raw(body))


def resulting_head_sha256(previous,entry,contract):
    previous=_require_sha256(previous); entry=_require_sha256(entry,nonzero=True)
    return _sha(contract['domains']['ledger_head'].encode()+b'\0'+bytes.fromhex(previous)+bytes.fromhex(entry))


def signature_message(kind,*,key_id,signed_sha256,contract,schemas):
    if kind not in {'receipt','ledger'}: _fail('signature_kind_invalid')
    if type(key_id) is not str or _KEY_ID_RE.fullmatch(key_id) is None: _fail('key_id_invalid')
    signed_sha256=_require_sha256(signed_sha256,nonzero=True)
    role=contract['signers']['receipt' if kind=='receipt' else 'final_ledger_entry']
    document_type=schemas[SCHEMAS[kind]]['properties']['document_type']['const']
    return b'\0'.join((contract['domains']['signature'].encode(),document_type.encode('ascii'),role.encode('ascii'),
                       key_id.encode('ascii'),bytes.fromhex(signed_sha256)))


def validate_document(kind,payload,*,root=ROOT):
    """Check syntax and byte commitments, not whether input claims are true."""
    if type(kind) is not str or kind not in SCHEMAS: _fail('document_kind_invalid')
    contract,schemas=load_contract(root)
    value=_decode(payload,schemas[SCHEMAS[kind]],contract['limits']['document_bytes'],())
    if raw(value)!=payload: _fail('noncanonical_document')
    if kind in {'receipt','ledger'}:
        if value['timed_closure_contract_sha256']!=CONTRACT_SHA256: _fail('contract_binding_mismatch')
        if kind=='receipt':
            if value['receipt_id']!=receipt_id(value,contract): _fail('receipt_id_mismatch')
            if value['closure_reasons_commitment_sha256']!=reasons_sha256(value['closure_reasons'],contract): _fail('reasons_hash_mismatch')
            expected=receipt_sha256(value,contract)
            if value['document_sha256']!=expected: _fail('receipt_hash_mismatch')
            if value['evidence_valid']!=(value['evidence_error_code'] is None): _fail('evidence_error_mismatch')
        else:
            expected=entry_sha256(value,contract)
            if value['entry_sha256']!=expected or value['resulting_head_sha256']!=resulting_head_sha256(value['previous_head_sha256'],expected,contract):
                _fail('ledger_hash_mismatch')
        for item in value['signatures']:
            if item['signed_sha256']!=expected: _fail('signature_subject_mismatch')
            _decode_canonical_base64(item['signature_b64'],length=64,code='timed_closure_signature_encoding_invalid')
    return value
