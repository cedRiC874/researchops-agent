"""Timed layer A: raw inputs to an unsigned v2 receipt, never a signer/permit."""
from dataclasses import dataclass, field
from pathlib import Path

from researchops_completion_timing import artifacts as timing_artifacts
from researchops_completion_timing.admission import TimedAdmissionInputs, VerifiedTimedAdmissionLinksV5, verify_timed_admission_links_v5
from researchops_completion_timing.first_live_identity_v5 import IMPLEMENTATION_COMMITMENT_SHA256
from researchops_completion_timing.contract import TimingContractError
from researchops_completion_telemetry.persisted_evidence import create_persisted_live_evidence_validation_context
from researchops_completion_telemetry.surface_mapping import load_and_select_surface_mapping
from . import evaluator as legacy, timed_contract as wire
from .artifact_inputs import _read_database_rows
from .artifacts import _verify_audit_database, _validate_audit_index, _validate_completion_telemetry
from .errors import ExternalClosurePrimitiveError
from .primitives import canonical_json_bytes as raw, decode_strict_json_object
from .timed_artifact_snapshot import inspect_timed_artifact_snapshot
from .timed_history import verify_timed_pre_receipt_inputs
from .timed_semantics import verify_timed_content_and_timing
from .types import PreReceiptRejected, ReceiptProjectionReady


# Stable receipt categories for known lower-level diagnostics. Preserve the
# frozen wire enum; do not mislabel semantic/encoding failures as file-hash drift.
_TIMED_ERROR_CATEGORIES = {
    'timing_audit_heads_mismatch': 'timing_projection_mismatch',
    'timing_artifact_noncanonical_json': 'timing_artifact_invalid',
}


@dataclass(frozen=True, slots=True)
class TimedReceiptProjectionReady(ReceiptProjectionReady):
    timed_closure_contract_sha256: str = field(default=wire.CONTRACT_SHA256, init=False)
    runtime_authority_granted: bool = field(default=False, init=False)
    closure_claim_allowed: bool = field(default=False, init=False)


def _timing_projection(history, facts, *, status):
    proof = history.scope.timing
    return dict(status=status, plan_commitment_sha256=proof.timing_plan_commitment_sha256,
        execution_binding_sha256=proof.timing_execution_binding_sha256,
        source_recipe_sha256='d07be82febc00009b03a63541c3a86d3dea81f9bd00983c157e41f28eeaded08',
        first_live_implementation_commitment_sha256=IMPLEMENTATION_COMMITMENT_SHA256,
        evidence_file_sha256=None, publication_file_sha256=None, clock_domain_id=None,
        phase_duration_ns=None, last_completed_write_stage=facts.timed_last_completed_write_stage,
        gate_complete=False, error_code=None, incomplete_reasons=[])


def _verify_admission(root, documents, envelope, history, inputs):
    candidate = decode_strict_json_object(documents.candidate_freeze_receipt, max_bytes=2_000_000)
    if (type(inputs) is not TimedAdmissionInputs
            or inputs.expected_trust_manifest_sha256 != history.trust_manifest_sha256
            or inputs.expected_candidate_freeze_receipt_sha256 != history.candidate_freeze_receipt_sha256
            or inputs.expected_candidate_frozen_at_utc != candidate['frozen_at_utc']):
        raise ExternalClosurePrimitiveError('closure_denominator_invalid')
    checked = verify_timed_admission_links_v5(root, execution_binding=envelope['execution_binding'], inputs=inputs)
    if (type(checked) is not VerifiedTimedAdmissionLinksV5
            or checked.candidate_commit != envelope['execution_binding']['execution_commit']
            or checked.candidate_tree != envelope['execution_binding']['execution_tree']
            or not all(getattr(checked, name) is True for name in ('artifact_source_binding_verified',
                'review_signature_and_external_bindings_verified', 'source_byte_equality_verified',
                'first_live_snapshot_link_verified', 'registry_links_verified'))
            or checked.runtime_authority_granted is not False or checked.closure_claim_allowed is not False):
        raise ExternalClosurePrimitiveError('closure_denominator_invalid')


def _complete_container(root, directory, snapshot, history, envelope):
    profile, manifest_schema = timing_artifacts.load_artifact_contract(root)
    files = snapshot._payloads
    manifest = timing_artifacts._json_file(files[profile['manifest_file']], 32768, (), manifest_schema)
    proof = history.scope.timing
    if (manifest['artifact_contract_sha256'] != timing_artifacts.PROFILE_SHA256
            or manifest['timing_contract_sha256'] != timing_artifacts.CONTRACT_SHA256
            or manifest['timed_ledger_contract_sha256'] != timing_artifacts.LEDGER_CONTRACT_SHA256
            or manifest['plan_commitment_sha256'] != proof.timing_plan_commitment_sha256
            or manifest['execution_binding_sha256'] != proof.timing_execution_binding_sha256
            or manifest['campaign_id'] != envelope['runtime_plan']['campaign_topology']['campaign_id']):
        raise ExternalClosurePrimitiveError('closure_bundle_hash_mismatch')
    for name in profile['payload_files']:
        if manifest['files'][name] != {'bytes': len(files[name]), 'sha256': wire._sha(files[name])}:
            raise ExternalClosurePrimitiveError('closure_bundle_hash_mismatch')
    # All JSON files, not just the manifest, must use their original canonical bytes.
    decoded = {name: timing_artifacts._json_file(payload, profile['max_file_bytes'], ())
               for name, payload in files.items() if name.endswith('.json')}
    database = directory / 'phase6_audit.sqlite3'
    db = _verify_audit_database(database, expected_payload=files['phase6_audit.sqlite3'])
    if db.schema_commitment_sha256 != profile['expected_database_schema_sha256']:
        raise ExternalClosurePrimitiveError('closure_audit_schema_invalid')
    index, telemetry = decoded['phase6_audit_index.json'], decoded['phase6_completion_telemetry.json']
    _validate_audit_index(index); _validate_completion_telemetry(telemetry)
    rows = _read_database_rows(database, expected_payload=files['phase6_audit.sqlite3'])
    return telemetry, index, rows


def _content_and_timing(root, snapshot, history, facts, envelope, extracted):
    telemetry, index, (runs, events, models, tools, tool_attempts, approvals) = extracted
    runtime, execution, custody = envelope['runtime_plan'], envelope['execution_binding'], envelope['task_custody']
    denominator, interpretation = runtime['denominator_plan'], runtime['telemetry_interpretation']
    expected = {name: denominator[name] for name in ('telemetry_schema_sha256', 'adapter_version',
        'mapping_schema_version', 'mapping_version', 'mapping_sha256', 'provider_id', 'api_surface', 'transport_id')}
    expected.update({name: interpretation[name] for name in ('output_counter_comparability', 'output_counter_path')})
    selection = load_and_select_surface_mapping(root, denominator['provider_id'], denominator['api_surface'],
        denominator['transport_id'], purpose='offline_validation')
    context = create_persisted_live_evidence_validation_context(selection, expected_binding=expected,
        expected_denominator_plan=denominator, expected_denominator_plan_commitment_sha256=runtime['denominator_plan_commitment_sha256'])
    inputs = dict(run_rows=runs, event_rows=events, model_call_rows=models, persisted_evidence_context=context,
        envelope_id=envelope['envelope_id'], campaign_id=facts.campaign_id, expected_runtime_plan=runtime,
        authorization_grant_sha256=history.authorization_grant_sha256,
        execution_input_order_commitment_sha256=custody['execution_input_order_commitment_sha256'],
        api_origin=execution['api_origin'], api_path='/responses', model_id=execution['model_id'],
        postrun_completed_at_utc=facts.completed_at_utc,
        authorization_consumed_at_utc=history.authorization_consumed_observed_at_utc,
        task_released_at_utc=facts.task_released_at_utc, provider_key_loaded_at_utc=facts.provider_key_loaded_at_utc,
        tool_call_count=tools, tool_attempt_count=tool_attempts, approval_decision_count=approvals)
    files = snapshot._payloads
    return verify_timed_content_and_timing(root, telemetry=telemetry, audit_index=index, semantic_inputs=inputs,
        timing_plan_bytes=files['completion_timing_plan.json'], timing_evidence_bytes=files['completion_timing_evidence.json'],
        publication_bytes=files['completion_timing_publication.json'],
        expected_timing_plan_commitment_sha256=history.scope.timing.timing_plan_commitment_sha256,
        expected_timing_execution_binding_sha256=history.scope.timing.timing_execution_binding_sha256,
        expected_manifest_file_sha256=wire._sha(files['completion_closure_manifest.json']))


def _unsigned_result(root, profile, history, facts, projection, planned_cases, semantic, error, timing_value):
    body = legacy._receipt_body(pre=history, facts=facts, projection=projection,
        planned_case_count=planned_cases, semantic=semantic if error is None else None, evidence_error=error)
    body.update(schema_version='provider-completion-closure-receipt/2.0',
        document_type='completion_telemetry_timed_closure_receipt', timed_closure_contract_sha256=wire.CONTRACT_SHA256,
        timing=timing_value)
    if body['evidence_valid']:
        reasons = set(body['closure_reasons'])
        if timing_value['status'] == 'incomplete': reasons.add('timing_constraints_incomplete')
        if 'publication_unverified' in timing_value['incomplete_reasons']: reasons.add('timing_publication_unverified')
        body['closure_reasons'] = [reason for reason in profile['reason_order'] if reason in reasons]
        body['pre_anchor_closure_eligible'] = bool(body['pre_anchor_closure_eligible'] and timing_value['gate_complete'] and not reasons)
    body['closure_reasons_commitment_sha256'] = wire.reasons_sha256(body['closure_reasons'], profile)
    digest = wire.receipt_sha256(body, profile)
    # Schema-only placeholder, never emitted as a signature or verified as one.
    schema_document = dict(body, document_sha256=digest, signatures=[dict(role='task_custodian',
        key_id='PCEKEY-' + '0' * 32, algorithm='ed25519', signed_sha256=digest, signature_b64='A' * 86 + '==')])
    wire.validate_document('receipt', raw(schema_document), root=root)
    return TimedReceiptProjectionReady(raw(body), digest, body['evidence_valid'], body['pre_anchor_closure_eligible'],
        body['evidence_error_code'], tuple(body['closure_reasons']))


def evaluate_timed_closure_bundle(project_root, documents, *, pre_execution_observation,
        postrun_observation, timing_plan_bytes, artifact_directory, postrun_attested_facts, admission_inputs=None):
    """All raw-input A branches; outputs an unsigned projection, not closure.

    Partial/quarantined evidence cannot become eligible. Contradictory/unreadable
    artifact layouts reject without invented commitments or repaired facts.
    """
    try:
        verified = verify_timed_pre_receipt_inputs(project_root, documents,
            pre_execution_observation=pre_execution_observation, postrun_observation=postrun_observation,
            timing_plan_bytes=timing_plan_bytes, postrun_attested_facts=postrun_attested_facts)
        history, facts = verified.history, verified.facts
        profile, _ = wire.load_contract(project_root)
        envelope = decode_strict_json_object(documents.preregistration_envelope, max_bytes=2_000_000)
        observation = decode_strict_json_object(postrun_observation, max_bytes=2_000_000)
        snapshot = inspect_timed_artifact_snapshot(project_root, artifact_directory, postrun_attested_facts=postrun_attested_facts)
        quarantined = snapshot.publication_disposition != 'normal'
        # Under quarantine this is only an attested writer classification, not
        # independently observed file presence. No artifact path is inspected.
        complete_stage = facts.timed_last_completed_write_stage == 'timing_publication_written'
        status = ('complete' if complete_stage else 'manifest_present_invalid') if facts.last_completed_artifact_write_stage == 'manifest_written' else 'manifestless_failure'
        if not quarantined and complete_stage:
            status = 'manifest_present_invalid'  # Presence alone is not a verified container.
        commitments = {} if quarantined else snapshot.file_commitments()
        manifest_sha = None
        if not quarantined and facts.last_completed_artifact_write_stage == 'manifest_written':
            manifest_sha = commitments['completion_closure_manifest.json']['sha256']
        projection = legacy._ArtifactProjection(status, snapshot.publication_disposition, snapshot.public_scan_status,
            manifest_sha if status == 'complete' else None, {} if quarantined else commitments)
        mismatch = legacy._manifest_observation_error(observation, projection)
        if mismatch: return PreReceiptRejected(mismatch)
        timing_value = _timing_projection(history, facts, status='quarantined' if quarantined else 'unavailable')
        semantic = None
        error = snapshot.quarantine_error if quarantined else 'closure_bundle_manifest_invalid'
        if not quarantined and complete_stage:
            admission_error = None
            try: _verify_admission(project_root, documents, envelope, history, admission_inputs)
            except Exception: admission_error = 'closure_denominator_invalid'
            try:
                extracted = _complete_container(project_root, artifact_directory, snapshot, history, envelope)
                projection = legacy._ArtifactProjection('complete', 'normal', 'passed', manifest_sha, {})
                combined = _content_and_timing(project_root, snapshot, history, facts, envelope, extracted)
                semantic = combined.content
                error = legacy._semantic_postrun_error(semantic, facts=facts, planned_case_count=envelope['task_custody']['planned_case_count'])
                evidence = decode_strict_json_object(snapshot._payloads['completion_timing_evidence.json'], max_bytes=2_000_000)
                timing_value.update(status='verified' if combined.timing_gate_complete else 'incomplete',
                    evidence_file_sha256=commitments['completion_timing_evidence.json']['sha256'],
                    publication_file_sha256=commitments['completion_timing_publication.json']['sha256'],
                    clock_domain_id=evidence['clock_domain_id'], phase_duration_ns=combined.phase_duration_ns,
                    gate_complete=combined.timing_gate_complete, incomplete_reasons=list(combined.timing_incomplete_reasons))
            except (ExternalClosurePrimitiveError, TimingContractError) as failure:
                code = _TIMED_ERROR_CATEGORIES.get(failure.code, failure.code)
                if code in {'external_closure_sensitive_content_detected', 'timing_sensitive_content'}:
                    quarantined = True
                    projection = legacy._ArtifactProjection(status, 'sensitive_quarantined', 'sensitive_detected', None, {})
                    timing_value = _timing_projection(history, facts, status='quarantined')
                    error = 'closure_sensitive_content_detected'
                elif code in profile['timing_error_codes']:
                    timing_value.update(status='invalid', error_code=code,
                        evidence_file_sha256=commitments['completion_timing_evidence.json']['sha256'],
                        publication_file_sha256=commitments['completion_timing_publication.json']['sha256'])
                    error = 'closure_timeline_invalid' if any(tag in code for tag in ('timeout', 'overlap', 'clock', 'lifetime')) else 'closure_outer_projection_invalid'
                else:
                    error = code if code in legacy._EVIDENCE_ERROR_PRIORITY else legacy._map_artifact_error(code)
            if admission_error is not None and not quarantined: error = admission_error
        mismatch = legacy._manifest_observation_error(observation, projection)
        if mismatch: return PreReceiptRejected(mismatch)
        if not quarantined:
            again = inspect_timed_artifact_snapshot(project_root, artifact_directory, postrun_attested_facts=postrun_attested_facts)
            if again.publication_disposition != 'normal' or again._payloads != snapshot._payloads:
                return PreReceiptRejected('timed_closure_artifact_snapshot_changed')
        return _unsigned_result(project_root, profile, history, facts, projection,
            envelope['task_custody']['planned_case_count'], semantic, error, timing_value)
    except (ExternalClosurePrimitiveError, TimingContractError) as failure:
        return PreReceiptRejected(failure.code)
    except Exception:
        return PreReceiptRejected('timed_closure_evaluation_invalid')
