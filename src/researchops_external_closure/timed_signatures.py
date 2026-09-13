"""Verify new receipt/witness signatures against supplied independent digests.

No artifact, Git, clock, private Key, signer, runtime factory or network access.
The real-world origin of supplied expectations remains outside this helper.
"""
from dataclasses import dataclass,field

from researchops.provider_completion_external_contract import load_frozen_external_preregistration_contract
from . import timed_contract as wire
from .documents import _decode_schema_document,_role_map,_verify_signatures,_assert_role_time
from .io import scan_public_artifact_bytes
from .primitives import canonical_json_bytes as raw,parse_utc_timestamp,verify_document_sha256,verify_ed25519_signature,_require_sha256
from .errors import ExternalClosurePrimitiveError


def _fail(code): raise ExternalClosurePrimitiveError('timed_signature_'+code) from None


@dataclass(frozen=True,slots=True)
class TimedSignatureResult:
    kind: str
    document_sha256: str
    trust_manifest_sha256: str
    signer_role: str
    signer_key_id: str
    signature_verified: bool=field(default=True,init=False)
    signature_time_valid: bool=field(default=True,init=False)
    oob_origin_independently_proved: bool=field(default=False,init=False)
    artifact_evidence_verified: bool=field(default=False,init=False)
    source_admission_verified: bool=field(default=False,init=False)
    final_witness_chain_verified: bool=field(default=False,init=False)
    runtime_authority_granted: bool=field(default=False,init=False)
    closure_claim_allowed: bool=field(default=False,init=False)


def verify_document_signature(kind,payload,*,trust_manifest_bytes,expected_trust_manifest_sha256,
                              expected_document_sha256,root=wire.ROOT):
    if type(kind) is not str or kind not in {'receipt','ledger'}: _fail('kind_invalid')
    expected_trust_manifest_sha256=_require_sha256(expected_trust_manifest_sha256,nonzero=True)
    expected_document_sha256=_require_sha256(expected_document_sha256,nonzero=True)
    value=wire.validate_document(kind,payload,root=root)
    digest=value['document_sha256' if kind=='receipt' else 'entry_sha256']
    if digest!=expected_document_sha256: _fail('document_binding_mismatch')
    schemas=load_frozen_external_preregistration_contract(root)['schemas']
    trust=_decode_schema_document(trust_manifest_bytes,schemas=schemas,schema_name='external_trust_manifest_v1.schema.json',
                                   schema_error='timed_signature_trust_schema_invalid')
    scan_public_artifact_bytes((trust_manifest_bytes,raw(trust)))
    verify_document_sha256(trust)
    if trust['document_sha256']!=expected_trust_manifest_sha256: _fail('trust_binding_mismatch')
    roles=_role_map(trust)
    created=parse_utc_timestamp(trust['created_at_utc'])
    valid_from=parse_utc_timestamp(trust['valid_from_utc']); expires=parse_utc_timestamp(trust['expires_at_utc'])
    if not valid_from<=created<expires: _fail('trust_time_invalid')
    _verify_signatures(trust,expected_roles=('freeze_authority','task_custodian','ledger_witness'),roles=roles,
        action_time=created,trust_valid_from=valid_from,trust_expires=expires)
    profile,new_schemas=wire.load_contract(root)
    role=profile['signers']['receipt' if kind=='receipt' else 'final_ledger_entry']
    record=roles[role]; signature=value['signatures'][0]
    action=parse_utc_timestamp(value['receipt_issued_at_utc' if kind=='receipt' else 'occurred_at_utc'])
    if action<created: _fail('action_before_trust')
    _assert_role_time(record,action_time=action,trust_valid_from=valid_from,trust_expires=expires)
    if signature['key_id']!=record['key_id'] or signature['role']!=role: _fail('role_key_mismatch')
    message=wire.signature_message(kind,key_id=record['key_id'],signed_sha256=digest,contract=profile,schemas=new_schemas)
    verify_ed25519_signature(public_key_b64=record['public_key_b64'],signature_b64=signature['signature_b64'],message=message)
    return TimedSignatureResult(kind,digest,trust['document_sha256'],role,record['key_id'])
