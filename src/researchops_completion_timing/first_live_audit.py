"""Actual first-live SQLite/record/timing linkage, never runtime admission."""
from __future__ import annotations

import os
from pathlib import Path

from researchops.audit import verify_audit_chain_rows, sha256_json
from researchops_completion_telemetry.persisted_evidence import create_persisted_live_evidence_validation_context
from researchops_completion_telemetry.surface_mapping import load_and_select_surface_mapping
from researchops_external_closure.artifact_inputs import _read_database_rows
from researchops_external_closure.artifacts import _verify_audit_database
from researchops_external_closure.io import read_regular_file_no_follow, scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object, parse_utc_timestamp
from . import contract as core
from . import first_live
from . import first_live_control as control
from . import local_claim
from .artifacts import PROFILE_SHA256 as ARTIFACT_PROFILE_SHA256, load_artifact_contract, _event, _json_file, _sha
from .ledger_event import validate_segment_link


PROFILE_SHA256 = "b0e8c8470cff76388285cf4e2bcb8eb7d680f66eda3869b3586bfdb5dcd1ff74"


def load_first_live_audit_contract(root):
    payload = read_regular_file_no_follow(root / "evals/provider_completion_first_live_audit_v1/contract_v1.json", max_bytes=4096)
    if len(payload) != 2190 or core.digest(payload) != PROFILE_SHA256:
        core.fail("first_live_audit_contract_invalid")
    profile = decode_strict_json_object(payload, max_bytes=4096)
    if (profile["first_live_timing_profile_sha256"] != first_live.PROFILE_SHA256
        or profile["timed_artifact_profile_sha256"] != ARTIFACT_PROFILE_SHA256):
        core.fail("first_live_audit_contract_invalid")
    return profile


def _database_snapshot(database, maximum):
    if any(os.path.lexists(str(database) + suffix) for suffix in ("-wal", "-shm", "-journal")):
        core.fail("first_live_audit_sidecar_present")
    return read_regular_file_no_follow(database, max_bytes=maximum)


def _audit_utc(value):
    # AuditLedger persists datetime.isoformat() UTC as +00:00, whereas external
    # control documents require Z. Interpret that exact UTC suffix without
    # rewriting the stored/hash-covered timestamp or accepting other offsets.
    if type(value) is not str:
        core.fail("first_live_audit_lifecycle_invalid")
    return parse_utc_timestamp(value[:-6] + "Z" if value.endswith("+00:00") else value)


def _extract(root, database, *, expected_database_sha256, plan_bytes, evidence_bytes, sensitive_canaries):
    if type(root) is not type(Path()) or type(database) is not type(Path()):
        core.fail("first_live_audit_path_invalid")
    _sha(expected_database_sha256)
    profile = load_first_live_audit_contract(root)
    artifact, _ = load_artifact_contract(root)
    _, parent, schemas = first_live.load_first_live_timing_contract(root)
    plan = core._decode(plan_bytes, schemas["completion_timing_plan_v1.schema.json"], parent["limits"]["max_plan_bytes"], sensitive_canaries)
    evidence = core._decode(evidence_bytes, schemas["completion_timing_evidence_v1.schema.json"], parent["limits"]["max_evidence_bytes"], sensitive_canaries)

    def snapshot():
        return _database_snapshot(database, artifact["max_file_bytes"])

    payload = snapshot()
    scan_public_artifact_bytes((payload,), sensitive_canaries=sensitive_canaries)
    if core.digest(payload) != expected_database_sha256:
        core.fail("first_live_audit_database_hash_mismatch")
    checked = _verify_audit_database(database, expected_payload=payload)
    if checked.schema_commitment_sha256 != artifact["expected_database_schema_sha256"]:
        core.fail("first_live_audit_schema_invalid")
    runs, rows, model_calls, tools, attempts, approvals = _read_database_rows(database, expected_payload=payload)
    run_id = plan["binding"]["validation_run_id"]
    request_hash = sha256_json({"plan_commitment_sha256": plan["plan_commitment_sha256"],
                               "execution_binding_sha256": evidence["execution_binding_sha256"]})
    if (len(runs) != profile["run_count"] or model_calls or tools or attempts or approvals
        or len(rows) != profile["event_count"]):
        core.fail("first_live_audit_inventory_invalid")
    run = runs[0]
    if (run["run_id"] != run_id or run["mode"] != profile["run_mode"] or run["status"] != profile["run_status"]
        or run["request_sha256"] != request_hash or run["dataset_sha256"] is not None or run["terminal_error_code"] is not None
        or any(row["run_id"] != run_id for row in rows)):
        core.fail("first_live_audit_run_binding_mismatch")
    if _audit_utc(run["updated_at_utc"]) < _audit_utc(run["created_at_utc"]):
        core.fail("first_live_audit_lifecycle_invalid")
    chain = verify_audit_chain_rows(run_id, rows)
    if not chain.valid:
        core.fail("first_live_audit_chain_mismatch")
    # A valid hash chain can still contain malformed timestamp strings. Check
    # every event, not only the run's first/last lifecycle rows. Interpret the
    # legacy +00:00 suffix without rewriting hash-covered bytes; elapsed-time
    # and concurrency evidence remains the separate monotonic-clock protocol.
    try:
        for row in rows:
            _audit_utc(row["occurred_at_utc"])
    except ValueError:
        core.fail("first_live_audit_lifecycle_invalid")
    expected_events = ["run_started", "model_request_started", "model_response_telemetry_recorded",
                       "model_request_started", "model_response_telemetry_recorded", "run_status_changed"]
    if [row["event_type"] for row in rows] != expected_events:
        core.fail("first_live_audit_event_order_invalid")
    start = {"mode": profile["run_mode"], "request_sha256": request_hash, "dataset_sha256": None}
    end = {"from": "running", "to": profile["run_status"], "error_code": None}
    for row, expected, stamp in ((rows[0], start, run["created_at_utc"]), (rows[-1], end, run["updated_at_utc"])):
        if row["actor_kind"] != "system" or row["safe_payload_json"].encode() != raw(expected) or row["occurred_at_utc"] != stamp:
            core.fail("first_live_audit_lifecycle_invalid")
    heads = [{"run_id": run_id, "final_chain_head_sha256": chain.chain_head}]
    heads_hash = core.digest(artifact["ordered_audit_heads_domain"].encode() + b"\0" + artifact["ordered_audit_heads_tag"].encode() + b"\0" + raw(heads))
    if evidence["audit_chain_heads_sha256"] != heads_hash:
        core.fail("first_live_audit_heads_mismatch")
    if len(evidence["segments"]) != profile["completion_attempt_count"]:
        core.fail("first_live_audit_segment_coverage_invalid")
    selection = load_and_select_surface_mapping(root, "deepseek", "responses", "openai_compatible_responses", purpose="offline_validation")
    names = ("telemetry_schema_sha256", "adapter_version", "mapping_schema_version", "mapping_version", "mapping_sha256",
             "provider_id", "api_surface", "transport_id", "output_counter_comparability", "output_counter_path")
    binding = {name: getattr(selection, name) for name in names}
    context = create_persisted_live_evidence_validation_context(selection, expected_binding=binding)
    records, events = [], []
    expected_timing = {"plan_commitment_sha256": plan["plan_commitment_sha256"],
        "execution_binding_sha256": evidence["execution_binding_sha256"], "clock_domain_id": evidence["clock_domain_id"]}
    for index, (case, segment) in enumerate(zip(plan["planned_case_handles"], evidence["segments"])):
        started, terminal = rows[1 + index * 2:3 + index * 2]
        for row in (started, terminal):
            value = _json_file(row["safe_payload_json"].encode(), 262144, sensitive_canaries)
            if row["actor_kind"] != "provider_adapter":
                core.fail("first_live_audit_actor_invalid")
            _event(value, row["event_type"], case_id=case, binding=binding, context=context)
            if value["attempt_index"] != index or value["case_attempt_index"] != 0:
                core.fail("first_live_audit_attempt_mismatch")
        reference = validate_segment_link(value, event_type=terminal["event_type"], segment_bytes=raw(segment),
            expected_timing_binding=expected_timing, root=root, sensitive_canaries=sensitive_canaries)
        if raw(reference) != raw(value["timing"]):
            core.fail("first_live_audit_segment_mismatch")
        record = value["completion_record"]
        records.append(record)
        events.append(dict(schema_version="provider-completion-timing-terminal-projection/1.0",
            event_type=terminal["event_type"], case_handle=case, attempt_index=index, response_index=value["response_index"],
            clock_domain_id=reference["clock_domain_id"], timing_segment_sha256=reference["segment_commitment_sha256"],
            completion_record_sha256=core.digest(raw(record)), usage_sha256=core.digest(raw(record["usage"]))))
    if snapshot() != payload:
        core.fail("first_live_audit_database_changed")
    return raw({"records": records}), raw({"events": events}), chain.chain_head


def verify_first_live_audit(root, database, *, expected_database_sha256, plan_bytes, evidence_bytes,
                           publication_bytes=None, expected_manifest_file_sha256=None, sensitive_canaries=()):
    """Verify actual SQLite rows; accepts no caller record or terminal projection."""
    try:
        canaries = tuple(sensitive_canaries)
        records, events, head = _extract(root, database, expected_database_sha256=expected_database_sha256,
            plan_bytes=plan_bytes, evidence_bytes=evidence_bytes, sensitive_canaries=canaries)
        timing = first_live.validate_first_live_timing_documents(root, plan_bytes=plan_bytes, evidence_bytes=evidence_bytes,
            terminal_projection_bytes=events, publication_bytes=publication_bytes,
            expected_manifest_file_sha256=expected_manifest_file_sha256, sensitive_canaries=canaries)
        shapes = first_live.verify_first_live_record_shapes(root, records, sensitive_canaries=canaries)
        artifact, _ = load_artifact_contract(root)
        if core.digest(_database_snapshot(database, artifact["max_file_bytes"])) != expected_database_sha256:
            core.fail("first_live_audit_database_changed")
        return {"status": "first_live_audit_timing_verified", "audit_hash_chain_verified": True,
            "audit_chain_head_sha256": head, "database_sha256": expected_database_sha256,
            "record_count": shapes["record_count"], "records_sha256": core.digest(records),
            "timing_gate_complete": timing["timing_gate_complete"], "incomplete_reasons": timing["incomplete_reasons"],
            "control_artifacts_verified": False, "local_store_entry_independently_verified": False,
            "source_admission_verified": False, "actual_clock_capture_proved": False,
            "runtime_authority_granted": False, "first_live_success_verified": False,
            "current_closure_claim_allowed": False, "provider_calls": 0, "real_key_loads": 0}
    except core.TimingContractError:
        raise
    except Exception as error:
        if getattr(error, "code", None) == "external_closure_sensitive_content_detected":
            core.fail("timing_sensitive_content")
        core.fail("first_live_audit_invalid")


def verify_first_live_controlled_audit(root, database, *, expected_database_sha256,
        plan_bytes, authorization_bytes, expected_authorization_binding_sha256,
        verification_time_utc, intent_bytes, expected_claim_receipt_sha256,
        evidence_bytes, publication_bytes=None, expected_manifest_file_sha256=None, sensitive_canaries=()):
    """Join actual local store and SQLite bytes; no caller receipt/projection.

    This remains readonly, as-of-time evidence verification, not winning-claim
    ownership, live provenance, source admission or permission to execute.
    """
    try:
        canaries = tuple(sensitive_canaries)
        proof = control.verify_first_live_authorization(root, plan_bytes=plan_bytes, authorization_bytes=authorization_bytes,
            expected_authorization_binding_sha256=expected_authorization_binding_sha256, verification_time_utc=verification_time_utc)
        _, schemas = control.load_control_contract(root)
        intent = core._decode(intent_bytes, schemas["claim_intent_v1.schema.json"], 4096, canaries)
        expected_intent, request = control.build_first_live_claim_intent(root, proof,
            clock_domain_id=intent["clock_domain_id"], intended_at_utc=intent["intended_at_utc"])
        if intent_bytes != expected_intent:
            core.fail("first_live_claim_link_mismatch")
        receipt = local_claim.read_reserved_local_claim(request, expected_receipt_sha256=expected_claim_receipt_sha256)
        records, events, head = _extract(root, database, expected_database_sha256=expected_database_sha256,
            plan_bytes=plan_bytes, evidence_bytes=evidence_bytes, sensitive_canaries=canaries)
        checked = control.verify_first_live_controlled_timing(root, plan_bytes=plan_bytes, authorization_bytes=authorization_bytes,
            expected_authorization_binding_sha256=expected_authorization_binding_sha256, verification_time_utc=verification_time_utc,
            intent_bytes=intent_bytes, claim_receipt_bytes=receipt, expected_claim_receipt_sha256=expected_claim_receipt_sha256,
            evidence_bytes=evidence_bytes, record_bytes=records, terminal_projection_bytes=events, publication_bytes=publication_bytes,
            expected_manifest_file_sha256=expected_manifest_file_sha256, sensitive_canaries=canaries)
        artifact, _ = load_artifact_contract(root)
        if core.digest(_database_snapshot(database, artifact["max_file_bytes"])) != expected_database_sha256:
            core.fail("first_live_audit_database_changed")
        if local_claim.read_reserved_local_claim(request, expected_receipt_sha256=expected_claim_receipt_sha256) != receipt:
            core.fail("first_live_audit_store_changed")
        return dict(checked, status="first_live_controlled_audit_verified", scope="actual_local_store_and_audit_linkage_only",
            authoritative_ledger_verified=True, local_store_entry_independently_verified=True,
            audit_chain_head_sha256=head, database_sha256=expected_database_sha256,
            store_verification_scope="readonly_snapshot_only", successful_reservation_return_proved=False,
            retry_or_resume_authorized=False)
    except (core.TimingContractError, local_claim.LocalClaimError):
        raise
    except Exception as error:
        if getattr(error, "code", None) == "external_closure_sensitive_content_detected":
            core.fail("timing_sensitive_content")
        core.fail("first_live_audit_invalid")
