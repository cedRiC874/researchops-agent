"""Join original v1.2 content and complete timing, not signed A/B admission.

Inputs are decoded rows/JSON and raw timing documents. No artifact container,
signature, source, external observation, claim store or runtime is verified here.
The outer evaluator must extract the actual rows and supply independent digests.
"""
from dataclasses import dataclass, field
from pathlib import Path

from researchops_completion_timing import contract as timing
from researchops_completion_timing.ledger_event import validate_segment_link
from . import semantics as content
from .io import scan_public_artifact_bytes
from .primitives import canonical_json_bytes as raw, decode_strict_json_object, _require_sha256


_FIELDS = frozenset({'run_rows', 'event_rows', 'model_call_rows', 'persisted_evidence_context',
    'envelope_id', 'campaign_id', 'expected_runtime_plan', 'authorization_grant_sha256',
    'execution_input_order_commitment_sha256', 'api_origin', 'api_path', 'model_id',
    'postrun_completed_at_utc', 'authorization_consumed_at_utc', 'task_released_at_utc',
    'provider_key_loaded_at_utc', 'tool_call_count', 'tool_attempt_count', 'approval_decision_count'})


@dataclass(frozen=True, slots=True)
class TimedContentResult:
    content: content.VerifiedArtifactContentSemantics
    timing_gate_complete: bool
    timing_incomplete_reasons: tuple[str, ...]
    phase_duration_ns: int | None
    content_and_timing_complete: bool
    container_origin_verified: bool = field(default=False, init=False)
    signed_expectations_verified: bool = field(default=False, init=False)
    source_admission_verified: bool = field(default=False, init=False)
    actual_clock_capture_independently_proved: bool = field(default=False, init=False)
    runtime_authority_granted: bool = field(default=False, init=False)
    closure_claim_allowed: bool = field(default=False, init=False)


def verify_timed_content_and_timing(project_root, *, telemetry, audit_index, semantic_inputs,
        timing_plan_bytes, timing_evidence_bytes, expected_timing_plan_commitment_sha256,
        expected_timing_execution_binding_sha256, expected_manifest_file_sha256,
        publication_bytes=None, sensitive_canaries=()):
    """Recompute both projections from the same original rows; never downgrade.

    semantic_inputs has an exact raw-input field set, including explicit tool
    counts. It cannot carry callbacks, precomputed verdicts or caller references.
    A complete result is still not source/admission, signature or witness proof.
    """
    if not isinstance(project_root, Path) or type(semantic_inputs) is not dict or set(semantic_inputs) != _FIELDS:
        timing.fail('timed_content_input_invalid')
    expected_plan = _require_sha256(expected_timing_plan_commitment_sha256, nonzero=True)
    expected_binding = _require_sha256(expected_timing_execution_binding_sha256, nonzero=True)
    expected_manifest = _require_sha256(expected_manifest_file_sha256, nonzero=True)
    inputs = dict(semantic_inputs)
    for name, maximum, code in (('event_rows', content._MAX_EVENTS, 'closure_audit_chain_invalid'),
                                ('run_rows', content._MAX_RUNS, 'closure_audit_run_set_invalid'),
                                ('model_call_rows', content._MAX_ATTEMPTS, 'closure_event_projection_invalid')):
        inputs[name] = content._snapshot_rows(inputs[name], maximum=maximum, code=code)
    inputs['expected_runtime_plan'] = content._snapshot_mapping(inputs['expected_runtime_plan'], 'closure_denominator_invalid')
    telemetry = content._snapshot_mapping(telemetry, 'closure_outer_projection_invalid')
    audit_index = content._snapshot_mapping(audit_index, 'closure_audit_run_set_invalid')
    canaries = tuple(sensitive_canaries)
    scan_public_artifact_bytes((raw(telemetry), raw(audit_index),
        *(raw(list(inputs[name])) for name in ('run_rows', 'event_rows', 'model_call_rows')),
        timing_plan_bytes, timing_evidence_bytes, *(() if publication_bytes is None else (publication_bytes,))),
        sensitive_canaries=canaries)

    # First verify original hashes. References extracted here are not treated
    # as timing proof: the independent full segment comparison follows below.
    content._validate_raw_chains(inputs['event_rows'])
    terminals = []
    references = {}
    for row in inputs['event_rows']:
        if row['event_type'] in content._TERMINAL_EVENT_TYPES:
            payload = decode_strict_json_object(row['safe_payload_json'].encode(), max_bytes=content._MAX_TIMED_TERMINAL_PAYLOAD_BYTES)
            terminals.append((row['event_type'], payload))
            references[str(payload.get('attempt_index'))] = payload.get('timing')
    checked_content = content._verify_artifact_content_semantics(telemetry, audit_index,
        terminal_timing_references=references, **inputs)
    terminals.sort(key=lambda item: item[1]['attempt_index'])

    frozen, schemas = timing.load_contract(project_root)
    plan = timing._decode(timing_plan_bytes, schemas['completion_timing_plan_v1.schema.json'], frozen['limits']['max_plan_bytes'], canaries)
    evidence = timing._decode(timing_evidence_bytes, schemas['completion_timing_evidence_v1.schema.json'], frozen['limits']['max_evidence_bytes'], canaries)
    runtime = inputs['expected_runtime_plan']
    denominator, limits = runtime['denominator_plan'], runtime['transport_limits']
    if (plan['plan_commitment_sha256'] != expected_plan
            or runtime['timing_plan_commitment_sha256'] != expected_plan
            or evidence['execution_binding_sha256'] != expected_binding
            or evidence['binding']['authorization_grant_sha256'] != inputs['authorization_grant_sha256']
            or evidence['binding']['runtime_plan_commitment_sha256'] != runtime['external_plan_binding_sha256']
            or plan['binding']['campaign_id'] != inputs['campaign_id']
            or plan['binding']['runtime_plan_core_sha256'] != timing.runtime_plan_core_commitment(runtime)
            or any(plan['binding'][name] != denominator[name] for name in ('provider_id', 'api_surface', 'transport_id', 'adapter_version'))
            or plan['planned_case_handles'] != denominator['case_ids']
            or plan['max_attempts'] != denominator['total_model_request_cap']
            or plan['max_attempts_per_case'] != denominator['max_turns_per_case']
            or plan['request_timeout_ns'] != limits['request_timeout_seconds'] * 1_000_000_000
            or plan['phase_timeout_ns'] != limits['total_timeout_seconds'] * 1_000_000_000):
        timing.fail('timing_binding_mismatch')
    if evidence['audit_chain_heads_sha256'] != checked_content.ordered_final_chain_heads_commitment_sha256:
        timing.fail('timing_audit_heads_mismatch')
    if len(evidence['segments']) != len(terminals):
        timing.fail('timing_projection_mismatch')
    expected_reference_binding = dict(plan_commitment_sha256=expected_plan,
        execution_binding_sha256=expected_binding, clock_domain_id=evidence['clock_domain_id'])
    projections = []
    for segment, (event_type, payload) in zip(evidence['segments'], terminals):
        reference = validate_segment_link(payload, event_type=event_type, segment_bytes=raw(segment),
            expected_timing_binding=expected_reference_binding, root=project_root, sensitive_canaries=canaries)
        if raw(reference) != raw(payload['timing']):
            timing.fail('timing_projection_mismatch')
        accepted = 'completion_record' in payload
        projections.append(dict(schema_version='provider-completion-timing-terminal-projection/1.0',
            event_type=event_type, case_handle=payload['case_id'], attempt_index=payload['attempt_index'],
            response_index=payload['response_index'], clock_domain_id=reference['clock_domain_id'],
            timing_segment_sha256=reference['segment_commitment_sha256'],
            completion_record_sha256=timing.digest(raw(payload['completion_record'])) if accepted else None,
            usage_sha256=timing.digest(raw(payload['completion_record']['usage'])) if accepted else None))
    checked_timing = timing.validate_documents(project_root, plan_bytes=timing_plan_bytes,
        evidence_bytes=timing_evidence_bytes, terminal_projection_bytes=raw({'events': projections}),
        publication_bytes=publication_bytes, expected_manifest_file_sha256=expected_manifest, sensitive_canaries=canaries)
    complete = (checked_content.full_plan_observed and checked_content.all_chains_valid
        and checked_content.semantic_closure_claim_allowed and checked_timing['timing_gate_complete'])
    return TimedContentResult(checked_content, checked_timing['timing_gate_complete'],
        tuple(checked_timing['incomplete_reasons']), checked_timing['phase_duration_ns'], bool(complete))
