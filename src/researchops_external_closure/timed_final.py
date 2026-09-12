"""Timed layer B: v2 signatures/witness and exact normal-path A recomputation."""
from dataclasses import dataclass, field
from pathlib import Path

from . import final as legacy, timed_contract as wire
from .timed_history import verify_timed_history
from .timed_signatures import verify_document_signature
from .types import FinalClosureResult, FinalDocumentBytes
from .primitives import canonical_json_bytes as raw, decode_strict_json_object, parse_utc_timestamp
from .errors import ExternalClosurePrimitiveError
from researchops_completion_timing.contract import TimingContractError


@dataclass(frozen=True, slots=True)
class TimedFinalClosureResult(FinalClosureResult):
    timed_closure_contract_sha256: str = field(default=wire.CONTRACT_SHA256, init=False)
    runtime_authority_granted: bool = field(default=False, init=False)


def _invalid(code):
    return TimedFinalClosureResult('unclosed_invalid', False, False, code, None, None)


def _projection(receipt, history, decoded, observation):
    envelope, grant, consumed = (decoded[name] for name in ('preregistration_envelope', 'authorization_grant', 'authorization_consumed_ledger_entry'))
    custody = envelope['task_custody']
    expected = dict(campaign_id=envelope['runtime_plan']['campaign_topology']['campaign_id'],
        preregistration_envelope_sha256=history.preregistration_envelope_sha256,
        preregistration_freeze_entry_sha256=history.preregistration_frozen_ledger_entry_sha256,
        authorization_grant_sha256=history.authorization_grant_sha256,
        consumption_receipt_sha256=history.consumption_receipt_sha256,
        authorization_consumption_entry_sha256=history.authorization_consumed_ledger_entry_sha256,
        planned_case_count=custody['planned_case_count'])
    if any(receipt[name] != value for name, value in expected.items()):
        legacy._reject(legacy._RECEIPT_PROJECTION_ERROR)
    if receipt['task_released'] and any(receipt[target] != custody[source] for target, source in (
            ('released_task_bundle_commitment_sha256', 'task_bundle_commitment_sha256'),
            ('released_case_handle_order_commitment_sha256', 'case_order_commitment_sha256'),
            ('released_execution_input_order_commitment_sha256', 'execution_input_order_commitment_sha256'))):
        legacy._reject(legacy._RECEIPT_PROJECTION_ERROR)
    proof = history.scope.timing
    if (receipt['timing']['plan_commitment_sha256'] != proof.timing_plan_commitment_sha256
            or receipt['timing']['execution_binding_sha256'] != proof.timing_execution_binding_sha256):
        legacy._reject(legacy._RECEIPT_PROJECTION_ERROR)
    legacy._verify_manifest_observation_projection(receipt, observation)
    completed = parse_utc_timestamp(receipt['completed_at_utc'])
    issued = parse_utc_timestamp(receipt['receipt_issued_at_utc'])
    manifest = parse_utc_timestamp(observation['manifest_observation']['observed_at_utc'])
    if not (parse_utc_timestamp(consumed['occurred_at_utc']) < completed <= manifest < issued
            and completed < parse_utc_timestamp(grant['expires_at_utc'])):
        legacy._reject(legacy._RECEIPT_PROJECTION_ERROR)
    anchor = parse_utc_timestamp(history.authorization_consumed_observed_at_utc)
    for name in ('task_released_at_utc', 'provider_key_loaded_at_utc'):
        if receipt[name] is not None and not anchor < parse_utc_timestamp(receipt[name]) <= completed:
            legacy._reject(legacy._RECEIPT_PROJECTION_ERROR)


def _witness(entry, receipt, history, decoded, observation):
    consumed = decoded['authorization_consumed_ledger_entry']
    if (consumed['sequence'] >= 9007199254740991
            or entry['ledger_id'] != consumed['ledger_id'] or entry['sequence'] != consumed['sequence'] + 1
            or entry['previous_head_sha256'] != consumed['resulting_head_sha256']):
        legacy._reject(legacy._EXTERNAL_ANCHOR_ERROR)
    expected = dict(candidate_freeze_receipt_sha256=history.candidate_freeze_receipt_sha256,
        preregistration_envelope_sha256=history.preregistration_envelope_sha256,
        authorization_grant_sha256=history.authorization_grant_sha256,
        consumption_receipt_sha256=history.consumption_receipt_sha256,
        closure_receipt_sha256=receipt['document_sha256'])
    if any(entry[name] != value for name, value in expected.items()):
        legacy._reject(legacy._EXTERNAL_ANCHOR_ERROR)
    receipt_observed = observation['closure_receipt_observation']
    anchor = observation['closure_evidence_anchor']
    if receipt_observed['document_sha256'] != receipt['document_sha256']:
        legacy._reject(legacy._EXTERNAL_ANCHOR_ERROR)
    projected = {name: entry[name] for name in ('entry_sha256', 'ledger_id', 'entry_type', 'sequence',
                                               'previous_head_sha256', 'resulting_head_sha256')}
    projected['entry_occurred_at_utc'] = entry['occurred_at_utc']
    if any(anchor[name] != value for name, value in projected.items()):
        legacy._reject(legacy._EXTERNAL_ANCHOR_ERROR)
    issued = parse_utc_timestamp(receipt['receipt_issued_at_utc'])
    seen = parse_utc_timestamp(receipt_observed['observed_at_utc'])
    occurred = parse_utc_timestamp(entry['occurred_at_utc'])
    anchored = parse_utc_timestamp(anchor['observed_at_utc'])
    valid_until = parse_utc_timestamp(decoded['preregistration_envelope']['valid_until_utc'])
    if not (issued <= seen <= occurred and issued < occurred <= anchored <= valid_until):
        legacy._reject(legacy._EXTERNAL_ANCHOR_ERROR)


def verify_closed_timed_campaign(project_root, documents, *, pre_execution_observation,
        postrun_observation, final_observation, timing_plan_bytes, artifact_directory=None,
        postrun_attested_facts=None, admission_inputs=None):
    """Verify raw public documents, then rerun timed A exactly once if normal.

    Quarantine deliberately never inspects artifact/facts/admission arguments.
    No signer, caller callback or precomputed evaluator result is accepted.
    """
    error_code = legacy._RECEIPT_SCHEMA_ERROR
    try:
        if not isinstance(project_root, Path) or type(documents) is not FinalDocumentBytes:
            legacy._reject(error_code)
        try:
            receipt = wire.validate_document('receipt', documents.closure_receipt, root=project_root)
        except ExternalClosurePrimitiveError as error:
            if error.code in {'timed_closure_receipt_hash_mismatch', 'timed_closure_signature_subject_mismatch',
                              'timed_closure_signature_encoding_invalid'}:
                legacy._reject(legacy._RECEIPT_SIGNATURE_ERROR)
            raise
        error_code = legacy._EXTERNAL_ANCHOR_ERROR
        after = wire.validate_document('observation', final_observation, root=project_root)
        post = wire.validate_document('observation', postrun_observation, root=project_root)
        if after['mode'] != 'final' or post['mode'] != 'pre_receipt':
            legacy._reject(error_code)
        common = set(post) - {'mode', 'closure_receipt_observation', 'closure_evidence_anchor'}
        if raw({name: after[name] for name in common}) != raw({name: post[name] for name in common}):
            legacy._reject(error_code)
        history = verify_timed_history(project_root, documents.pre_receipt,
            pre_execution_observation=pre_execution_observation, postrun_observation=final_observation,
            timing_plan_bytes=timing_plan_bytes, observation_mode='final')
        decoded = {name: decode_strict_json_object(getattr(documents.pre_receipt, name), max_bytes=2_000_000)
                   for name in ('preregistration_envelope', 'authorization_grant', 'authorization_consumed_ledger_entry')}
        if after['closure_receipt_observation']['document_sha256'] != receipt['document_sha256']:
            legacy._reject(legacy._EXTERNAL_ANCHOR_ERROR)
        error_code = legacy._RECEIPT_SIGNATURE_ERROR
        verify_document_signature('receipt', documents.closure_receipt, trust_manifest_bytes=documents.pre_receipt.trust_manifest,
            expected_trust_manifest_sha256=history.trust_manifest_sha256,
            expected_document_sha256=after['closure_receipt_observation']['document_sha256'], root=project_root)
        error_code = legacy._RECEIPT_PROJECTION_ERROR
        _projection(receipt, history, decoded, after)
        error_code = legacy._EXTERNAL_ANCHOR_ERROR
        entry = wire.validate_document('ledger', documents.closure_evidence_anchored_ledger_entry, root=project_root)
        verify_document_signature('ledger', documents.closure_evidence_anchored_ledger_entry,
            trust_manifest_bytes=documents.pre_receipt.trust_manifest,
            expected_trust_manifest_sha256=history.trust_manifest_sha256,
            expected_document_sha256=after['closure_evidence_anchor']['entry_sha256'], root=project_root)
        _witness(entry, receipt, history, decoded, after)
        if receipt['artifact_publication_disposition'] != 'normal':
            return TimedFinalClosureResult('closed_failure_attested', False, False, None,
                receipt['document_sha256'], entry['entry_sha256'])
        error_code = legacy._RECEIPT_PROJECTION_ERROR
        # Import only on the normal branch; no artifact/private inputs cross this
        # boundary during quarantine. This is the real fixed evaluator, not a hook.
        from .timed_evaluator import evaluate_timed_closure_bundle, TimedReceiptProjectionReady
        rerun = evaluate_timed_closure_bundle(project_root, documents.pre_receipt,
            pre_execution_observation=pre_execution_observation, postrun_observation=postrun_observation,
            timing_plan_bytes=timing_plan_bytes, artifact_directory=artifact_directory,
            postrun_attested_facts=postrun_attested_facts, admission_inputs=admission_inputs)
        if (type(rerun) is not TimedReceiptProjectionReady
                or rerun.unsigned_receipt_canonical_json != legacy._receipt_unsigned_body(receipt)
                or rerun.receipt_document_sha256 != receipt['document_sha256']
                or rerun.evidence_valid != receipt['evidence_valid']
                or rerun.pre_anchor_closure_eligible != receipt['pre_anchor_closure_eligible']
                or rerun.evidence_error_code != receipt['evidence_error_code']
                or rerun.closure_reasons != tuple(receipt['closure_reasons'])):
            legacy._reject(error_code)
        return TimedFinalClosureResult('closed_evidence_verified' if rerun.evidence_valid else 'closed_failure_attested',
            rerun.evidence_valid, rerun.pre_anchor_closure_eligible if rerun.evidence_valid else False, None,
            receipt['document_sha256'], entry['entry_sha256'])
    except legacy._FinalVerificationFailure as failure:
        return _invalid(failure.code)
    except (ExternalClosurePrimitiveError, TimingContractError):
        return _invalid(error_code)
    except Exception:
        return _invalid(error_code)
