"""Build completed campaign payload bytes; no writes or closure/admission grant."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict

from researchops.audit import COMPLETION_TELEMETRY_TERMINAL_EVENT_TYPES, verify_audit_chain_rows
from researchops_external_closure.artifact_inputs import _read_database_rows
from researchops_external_closure.artifacts import _verify_audit_database, _validate_audit_index, _validate_completion_telemetry
from researchops_external_closure.io import scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object
from .artifacts import load_artifact_contract
from .campaign_runtime import ROOT, _CampaignModelFactory, _CampaignModelFactoryV5, CampaignRuntimeError
from .contract import CONTRACT_SHA256, commitment, digest, runtime_plan_core_commitment, validate_documents
from .first_live_audit import _database_snapshot
from .ledger_event import validate_segment_link


def _build_completed_campaign_payloads(factory):
    """Private bytes for the later exclusive publisher, never caller projections.

    The independent seven-file verifier must still validate these bytes after
    publication. This builder grants neither publication nor runtime authority.
    """
    if type(factory) not in (_CampaignModelFactory, _CampaignModelFactoryV5):
        raise CampaignRuntimeError('campaign_artifacts_factory_required')
    try:
        prepared = factory._prepared
        prepared._assert_process()
        envelope = prepared._verified_envelope()
        state = prepared.clock.snapshot()
        denominator = factory._sealed_denominator
        if (not factory._phase_finished or factory._failed or factory._busy
            or not state['closed'] or state['halted'] or state['active_attempt'] is not None
            or denominator is None):
            raise ValueError('incomplete phase')
        profile, _ = load_artifact_contract(ROOT)
        plan = decode_strict_json_object(prepared.plan_bytes, max_bytes=131072)
        proof = prepared.proof.scope.timing
        runtime = envelope['runtime_plan']; execution = envelope['execution_binding']
        binding = {name: execution[name] for name in ('execution_commit', 'execution_tree', 'source_integrity_commitment_sha256',
                                                     'provider_id', 'api_surface', 'transport_id', 'adapter_version')}
        binding.update(campaign_id=runtime['campaign_topology']['campaign_id'], runtime_plan_core_sha256=runtime_plan_core_commitment(runtime))
        if (plan['binding'] != binding or plan['plan_commitment_sha256'] != commitment('plan', plan)
            or plan['plan_commitment_sha256'] != proof.timing_plan_commitment_sha256
            or plan['planned_case_handles'] != list(denominator.planned_case_ids)
            # plan_snapshot is an internal nested projection, not the wire plan.
            # Compare the commitment to the full canonical signed denominator.
            or factory._tracker.plan_binding().preregistration_commitment != digest(raw(runtime['denominator_plan']))):
            raise ValueError('plan mismatch')
        binding.update(authorization_grant_sha256=proof.authorization_grant_sha256,
                       external_consumption_anchor_sha256=proof.external_consumption_anchor_sha256,
                       runtime_plan_commitment_sha256=runtime['external_plan_binding_sha256'])
        if commitment('execution_binding', binding) != proof.timing_execution_binding_sha256:
            raise ValueError('execution binding mismatch')
        canaries = (factory._opening._canary_b64.encode(),)
        database = factory._ledger.database_path
        database_bytes = _database_snapshot(database, profile['max_file_bytes'])
        # Scan raw DB bytes BEFORE SQLite schema or row access, never export_run.
        scan_public_artifact_bytes((database_bytes,), sensitive_canaries=canaries)
        if _verify_audit_database(database, expected_payload=database_bytes).schema_commitment_sha256 != profile['expected_database_schema_sha256']:
            raise ValueError('schema mismatch')
        runs, rows, _models, tools, tool_attempts, approvals = _read_database_rows(database, expected_payload=database_bytes)
        if (tools or tool_attempts or approvals or len(runs) != len(factory._runs)
            or {row['run_id'] for row in runs} != set(factory._runs)
            or any(row['status'] != 'completed' for row in runs)
            or len(factory._sessions) != len(plan['planned_case_handles'])):
            raise ValueError('run set mismatch')
        index = dict(schema_version='1.1', runs=[])
        heads = []; terminal_events = []; counts = Counter()
        for case, run_id, session in zip(plan['planned_case_handles'], factory._runs, factory._sessions):
            if session.case_id != case or session.failed:
                raise ValueError('session mismatch')
            events = [row for row in rows if row['run_id'] == run_id]
            chain = verify_audit_chain_rows(run_id, events)
            if not chain.valid: raise ValueError('audit chain mismatch')
            index['runs'].append(dict(task_id=case, run_id=run_id, provider=execution['provider_id'],
                transport=execution['transport_id'], chain_verification=asdict(chain),
                completion_telemetry_event_commitment=session.event_commitment()))
            heads.append(dict(run_id=run_id, final_chain_head_sha256=chain.chain_head))
            for row in events:
                counts[row['event_type']] += 1
                if row['event_type'] in COMPLETION_TELEMETRY_TERMINAL_EVENT_TYPES:
                    payload = decode_strict_json_object(row['safe_payload_json'].encode(), max_bytes=262144)
                    terminal_events.append((row['event_type'], payload))
        segments = [decode_strict_json_object(value, max_bytes=65536)
                    for session in factory._sessions for value in session.segment_bytes()]
        if len(segments) != len(state['attempts']) or len(segments) != len(terminal_events):
            raise ValueError('segment count mismatch')
        projections = []
        for segment, offsets, (kind, event) in zip(segments, state['attempts'], terminal_events):
            if any(segment.get(name) != value for name, value in offsets.items()):
                raise ValueError('clock snapshot mismatch')
            validate_segment_link(event, event_type=kind, segment_bytes=raw(segment),
                expected_timing_binding=factory._timing_binding, root=ROOT, sensitive_canaries=canaries)
            projections.append(dict(schema_version='provider-completion-timing-terminal-projection/1.0', event_type=kind,
                **{name: segment[name] for name in ('case_handle', 'attempt_index', 'response_index', 'clock_domain_id',
                                                   'completion_record_sha256', 'usage_sha256')},
                timing_segment_sha256=segment['segment_commitment_sha256']))
        telemetry = dict(schema_version='phase6-completion-telemetry/1.0', status='recorded',
            runtime_plan=runtime['denominator_plan'], runtime_denominator=denominator.to_dict(),
            ledger_reconciliation=dict(all_chains_valid=True, ledger_export_failed=False, ledger_failure_observed=False,
                event_counts=dict(sorted(counts.items())), reasons=[]),
            closure=dict(claim_allowed=False, claim_scope='producer_payloads_only', reasons=['independent_outer_verification_required']),
            historical_backfill_performed=False)
        _validate_audit_index(index); _validate_completion_telemetry(telemetry)
        heads_hash = digest(profile['ordered_audit_heads_domain'].encode() + b'\0' + profile['ordered_audit_heads_tag'].encode() + b'\0' + raw(heads))
        evidence = dict(schema_version='provider-completion-timing-evidence/1.0', document_type='completion_timing_evidence',
            timing_contract_sha256=CONTRACT_SHA256, plan_commitment_sha256=plan['plan_commitment_sha256'],
            binding=binding, execution_binding_sha256=proof.timing_execution_binding_sha256, clock_domain_id=prepared.clock_domain_id,
            clock_source='time.monotonic_ns', clock_unit='relative_integer_nanoseconds', origin_offset_ns=0,
            record_provenance='live_monotonic_capture', segments=segments, phase=state['phase'], audit_chain_heads_sha256=heads_hash)
        evidence['evidence_commitment_sha256'] = commitment('evidence', evidence)
        checked = validate_documents(ROOT, plan_bytes=raw(plan), evidence_bytes=raw(evidence),
            terminal_projection_bytes=raw(dict(events=projections)), sensitive_canaries=canaries)
        if checked['incomplete_reasons'] != ['publication_unverified']:
            raise ValueError('incomplete timing')
        payloads = {'phase6_audit.sqlite3': database_bytes, 'phase6_audit_index.json': raw(index),
                    'phase6_completion_telemetry.json': raw(telemetry), 'completion_timing_plan.json': raw(plan),
                    'completion_timing_evidence.json': raw(evidence)}
        if (set(payloads) != set(profile['payload_files'])
            or any(len(value) > profile['max_file_bytes'] for value in payloads.values())
            or sum(map(len, payloads.values())) > profile['max_total_bytes']):
            raise ValueError('payload limit')
        scan_public_artifact_bytes(tuple(payloads.values()), sensitive_canaries=canaries)
        if (_database_snapshot(database, profile['max_file_bytes']) != database_bytes
            or prepared.clock.snapshot() != state):
            raise ValueError('snapshot changed')
        return payloads
    except Exception:
        factory._abort()
        raise CampaignRuntimeError('campaign_artifacts_build_failed') from None
