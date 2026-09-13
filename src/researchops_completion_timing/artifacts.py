"""Readonly seven-file timing bundle verification, not current A/B admission.

Independent expectations must come from the outer signed/admission workflow.
This module verifies their local byte/chain/record linkage; it does not verify
their signatures, source authority, consumption, budget or tool semantics.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path

from researchops.audit import (
    COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION, COMPLETION_TELEMETRY_STARTED_EVENT,
    COMPLETION_TELEMETRY_TERMINAL_EVENT_TYPES, COMPLETION_TELEMETRY_UNMAPPED_EVENT,
    _COMPLETION_STARTED_FIELDS, _COMPLETION_ACCEPTED_FIELDS, _COMPLETION_TERMINAL_FIELDS,
    _COMPLETION_TERMINAL_KIND_BY_EVENT, _COMPLETION_SAFE_ERROR, _completion_index,
    verify_audit_chain_rows,
)
from researchops_completion_telemetry.persisted_evidence import (
    create_persisted_live_evidence_validation_context,
    validate_persisted_live_completion_record, validate_persisted_live_runtime_denominator_artifact,
)
from researchops_completion_telemetry.surface_mapping import load_and_select_surface_mapping
from researchops_external_closure.artifact_inputs import _read_database_rows
from researchops_external_closure.artifacts import _verify_audit_database, _validate_audit_index, _validate_completion_telemetry
from researchops_external_closure.io import read_exact_artifact_directory, read_regular_file_no_follow, scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes, decode_strict_json_object
from .contract import CONTRACT_SHA256, TimingContractError, _decode, commitment, digest, fail, load_contract, validate_documents
from .ledger_event import EVENT_VERSION, LEDGER_CONTRACT_SHA256, load_ledger_contract, validate_segment_link


ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = Path("evals/provider_completion_timing_artifacts_v1")
PROFILE_SHA256 = "4d5102ef7fffac81f07d62584e2b86463e5f0bc4cd637a994d1e491a9a8afc72"


def load_artifact_contract(root=ROOT):
    raw = read_regular_file_no_follow(root / PROFILE_PATH / "contract_v1.json", max_bytes=8192)
    if len(raw) != 3611 or digest(raw) != PROFILE_SHA256:
        fail("timing_artifact_contract_invalid")
    profile = decode_strict_json_object(raw, max_bytes=8192)
    spec = profile["manifest_schema"]
    schema_raw = read_regular_file_no_follow(root / PROFILE_PATH / spec["path"], max_bytes=8192)
    if len(schema_raw) != spec["bytes"] or digest(schema_raw) != spec["sha256"]:
        fail("timing_artifact_contract_invalid")
    return profile, decode_strict_json_object(schema_raw, max_bytes=8192)


def _sha(value):
    if type(value) is not str or re.fullmatch(r"(?!0{64}$)[0-9a-f]{64}", value) is None:
        fail("timing_artifact_expectation_invalid")
    return value


def _json_file(raw, maximum, canaries, schema=None):
    if schema is None:
        # Legacy record/usage fields keep their own validators. Do not apply
        # the timing-offset integer restriction to unrelated native metadata.
        value = decode_strict_json_object(raw, max_bytes=maximum)
        scan_public_artifact_bytes((raw, canonical_json_bytes(value)), sensitive_canaries=canaries)
    else:
        value = _decode(raw, schema, maximum, canaries)
    if canonical_json_bytes(value) != raw:
        fail("timing_artifact_noncanonical_json")
    return value


def _event(payload, event_type, *, case_id, binding, context):
    started = event_type == COMPLETION_TELEMETRY_STARTED_EVENT
    expected = _COMPLETION_STARTED_FIELDS if started else (
        _COMPLETION_ACCEPTED_FIELDS if event_type in {"model_response_telemetry_recorded", COMPLETION_TELEMETRY_UNMAPPED_EVENT}
        else _COMPLETION_TERMINAL_FIELDS)
    if set(payload) != set(expected) | (set() if started else {"timing"}):
        fail("timing_audit_event_invalid")
    if payload["schema_version"] != (COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION if started else EVENT_VERSION):
        fail("timing_audit_event_version_invalid")
    if payload["case_id"] != case_id or payload["binding"] != binding:
        fail("timing_audit_binding_mismatch")
    for name in ("attempt_index", "case_attempt_index"):
        _completion_index(payload[name], "timing_audit_index_invalid")
    if started:
        return
    kind = _COMPLETION_TERMINAL_KIND_BY_EVENT[event_type]
    if payload["terminal_kind"] != kind:
        fail("timing_audit_terminal_invalid")
    if kind in {"response_accepted", "response_rejected"}:
        _completion_index(payload["response_index"], "timing_audit_index_invalid")
    elif payload["response_index"] is not None:
        fail("timing_audit_terminal_invalid")
    error = payload["error_code"]
    if kind in {"response_accepted", "cancelled"}:
        if error is not None:
            fail("timing_audit_terminal_invalid")
    elif type(error) is not str or _COMPLETION_SAFE_ERROR.fullmatch(error) is None:
        fail("timing_audit_terminal_invalid")
    if kind == "response_accepted":
        record = payload["completion_record"]
        validate_persisted_live_completion_record(record, context=context)
        if record["request_index"] != payload["attempt_index"] or record["response_index"] != payload["response_index"]:
            fail("timing_audit_index_invalid")
        if (record["normalized_completion_state"] == "unmapped") != (event_type == COMPLETION_TELEMETRY_UNMAPPED_EVENT):
            fail("timing_audit_terminal_invalid")


def _verify_bundle(root, directory, *, expected_manifest_sha256, expected_plan_commitment_sha256,
                   expected_execution_binding_sha256, expected_denominator_plan_bytes,
                   expected_case_run_ids, sensitive_canaries):
    profile, manifest_schema = load_artifact_contract(root)
    frozen, schemas = load_contract(root)
    load_ledger_contract(root)
    for value in (expected_manifest_sha256, expected_plan_commitment_sha256, expected_execution_binding_sha256):
        _sha(value)
    names = tuple(profile["payload_files"] + [profile["manifest_file"], profile["publication_file"]])
    def snapshot():
        return read_exact_artifact_directory(directory, expected_names=names,
            max_file_bytes=profile["max_file_bytes"], max_total_bytes=profile["max_total_bytes"])
    files = snapshot()
    scan_public_artifact_bytes(tuple(files[name] for name in names), sensitive_canaries=sensitive_canaries)
    manifest_bytes = files[profile["manifest_file"]]
    if digest(manifest_bytes) != expected_manifest_sha256:
        fail("timing_bundle_manifest_hash_mismatch")
    manifest = _json_file(manifest_bytes, 32768, sensitive_canaries, manifest_schema)
    if (manifest["artifact_contract_sha256"] != PROFILE_SHA256 or manifest["timing_contract_sha256"] != CONTRACT_SHA256
        or manifest["timed_ledger_contract_sha256"] != LEDGER_CONTRACT_SHA256
        or manifest["plan_commitment_sha256"] != expected_plan_commitment_sha256
        or manifest["execution_binding_sha256"] != expected_execution_binding_sha256):
        fail("timing_bundle_binding_mismatch")
    for name in profile["payload_files"]:
        if manifest["files"][name] != {"bytes": len(files[name]), "sha256": digest(files[name])}:
            fail("timing_bundle_file_hash_mismatch")
    plan = _json_file(files["completion_timing_plan.json"], frozen["limits"]["max_plan_bytes"], sensitive_canaries,
                      schemas["completion_timing_plan_v1.schema.json"])
    evidence = _json_file(files["completion_timing_evidence.json"], frozen["limits"]["max_evidence_bytes"], sensitive_canaries,
                          schemas["completion_timing_evidence_v1.schema.json"])
    if (plan["plan_commitment_sha256"] != expected_plan_commitment_sha256
        or evidence["execution_binding_sha256"] != expected_execution_binding_sha256
        or manifest["campaign_id"] != plan["binding"]["campaign_id"]):
        fail("timing_bundle_binding_mismatch")
    cases = plan["planned_case_handles"]
    if (type(expected_case_run_ids) is not dict or set(expected_case_run_ids) != set(cases)
        or any(type(value) is not str or re.fullmatch(r"PCERUN-[A-F0-9]{32}", value) is None for value in expected_case_run_ids.values())
        or len(set(expected_case_run_ids.values())) != len(cases)):
        fail("timing_artifact_expectation_invalid")
    expected_case_run_ids = dict(expected_case_run_ids)
    denominator_plan = _json_file(expected_denominator_plan_bytes, 131072, sensitive_canaries)
    if (denominator_plan["case_ids"] != cases or denominator_plan["max_turns_per_case"] != plan["max_attempts_per_case"]
        or denominator_plan["total_model_request_cap"] != plan["max_attempts"]):
        fail("timing_bundle_denominator_mismatch")
    selection = load_and_select_surface_mapping(root, "deepseek", "responses", "openai_compatible_responses", purpose="offline_validation")
    binding = {key: denominator_plan[key] for key in ("provider_id", "api_surface", "transport_id", "adapter_version",
        "telemetry_schema_sha256", "mapping_schema_version", "mapping_version", "mapping_sha256")}
    binding.update(output_counter_comparability=selection.output_counter_comparability, output_counter_path=selection.output_counter_path)
    context = create_persisted_live_evidence_validation_context(selection, expected_binding=binding,
        expected_denominator_plan=denominator_plan, expected_denominator_plan_commitment_sha256=digest(expected_denominator_plan_bytes))
    if any(plan["binding"][key] != binding[key] for key in ("provider_id", "api_surface", "transport_id", "adapter_version")):
        fail("timing_bundle_binding_mismatch")
    index = _json_file(files["phase6_audit_index.json"], profile["max_file_bytes"], sensitive_canaries)
    telemetry = _json_file(files["phase6_completion_telemetry.json"], profile["max_file_bytes"], sensitive_canaries)
    _validate_audit_index(index); _validate_completion_telemetry(telemetry)
    if canonical_json_bytes(telemetry["runtime_plan"]) != expected_denominator_plan_bytes:
        fail("timing_bundle_denominator_mismatch")
    denominator = telemetry["runtime_denominator"]
    validate_persisted_live_runtime_denominator_artifact(denominator, context=context)
    database = directory / "phase6_audit.sqlite3"
    checked_db = _verify_audit_database(database, expected_payload=files["phase6_audit.sqlite3"])
    if checked_db.schema_commitment_sha256 != profile["expected_database_schema_sha256"]:
        fail("timing_audit_schema_invalid")
    runs, rows, _model_calls, _tools, _tool_attempts, _approvals = _read_database_rows(database, expected_payload=files["phase6_audit.sqlite3"])
    indexed = index["runs"]
    if [run["task_id"] for run in indexed] != cases[:len(indexed)]:
        fail("timing_audit_run_set_mismatch")
    run_ids = [expected_case_run_ids[run["task_id"]] for run in indexed]
    if [run["run_id"] for run in indexed] != run_ids or set(run_ids) != {run["run_id"] for run in runs}:
        fail("timing_audit_run_set_mismatch")
    by_run = {run_id: [] for run_id in run_ids}
    for row in rows:
        if row["run_id"] not in by_run:
            fail("timing_audit_run_set_mismatch")
        by_run[row["run_id"]].append(row)
    terminals, records, attempts, heads = [], [], [], []
    for item in indexed:
        run_id, case = item["run_id"], item["task_id"]
        chain = verify_audit_chain_rows(run_id, by_run[run_id])
        if not chain.valid or asdict(chain) != item["chain_verification"]:
            fail("timing_audit_chain_mismatch")
        heads.append({"run_id": run_id, "final_chain_head_sha256": chain.chain_head})
        started, ended = {}, {}
        for row in by_run[run_id]:
            event_type = row["event_type"]
            if event_type != COMPLETION_TELEMETRY_STARTED_EVENT and event_type not in COMPLETION_TELEMETRY_TERMINAL_EVENT_TYPES:
                continue
            payload = _json_file(row["safe_payload_json"].encode("utf-8"), 262144, sensitive_canaries)
            if row["actor_kind"] != "provider_adapter":
                fail("timing_audit_actor_invalid")
            _event(payload, event_type, case_id=case, binding=binding, context=context)
            ordinal = payload["attempt_index"]
            if event_type == COMPLETION_TELEMETRY_STARTED_EVENT:
                if ordinal in started or payload["case_attempt_index"] != len(started):
                    fail("timing_audit_projection_mismatch")
                started[ordinal] = (payload, row["event_hash"])
            else:
                if ordinal not in started or ordinal in ended or payload["case_attempt_index"] != started[ordinal][0]["case_attempt_index"]:
                    fail("timing_audit_projection_mismatch")
                ended[ordinal] = row["event_hash"]
                terminals.append((event_type, payload))
                attempts.append({key: payload[key] for key in ("case_id", "attempt_index", "case_attempt_index", "terminal_kind", "response_index", "error_code")})
                if "completion_record" in payload:
                    records.append(payload["completion_record"])
        if set(started) != set(ended):
            fail("timing_audit_terminal_coverage_missing")
        bridge = {"schema_version": "provider-completion-ledger-bridge-commitment/1.0", "case_id": case,
                  "binding_sha256": digest(canonical_json_bytes(binding)),
                  "started": [{"attempt_index": key, "event_hash": started[key][1]} for key in sorted(started)],
                  "terminals": [{"attempt_index": key, "event_hash": ended[key]} for key in sorted(ended)],
                  "all_started_attempts_terminal": True, "write_failed": False}
        bridge["commitment_sha256"] = digest(canonical_json_bytes(bridge))
        if bridge != item["completion_telemetry_event_commitment"]:
            fail("timing_audit_bridge_mismatch")
    if records != denominator["records"] or attempts != denominator["attempts"]:
        fail("timing_bundle_denominator_mismatch")
    heads_hash = digest(profile["ordered_audit_heads_domain"].encode() + b"\0" + profile["ordered_audit_heads_tag"].encode() + b"\0" + canonical_json_bytes(heads))
    if evidence["audit_chain_heads_sha256"] != heads_hash:
        fail("timing_audit_heads_mismatch")
    segments = evidence["segments"]
    if len(segments) != len(terminals):
        fail("timing_audit_terminal_coverage_missing")
    expected_binding = {"plan_commitment_sha256": expected_plan_commitment_sha256,
                        "execution_binding_sha256": expected_execution_binding_sha256, "clock_domain_id": evidence["clock_domain_id"]}
    events = []
    for segment, (event_type, payload) in zip(segments, terminals):
        reference = validate_segment_link(payload, event_type=event_type, segment_bytes=canonical_json_bytes(segment),
            expected_timing_binding=expected_binding, root=root, sensitive_canaries=sensitive_canaries)
        if reference != payload["timing"]:
            fail("timing_audit_projection_mismatch")
        events.append({"schema_version": "provider-completion-timing-terminal-projection/1.0", "event_type": event_type,
            "case_handle": payload["case_id"], "attempt_index": payload["attempt_index"], "response_index": payload["response_index"],
            "clock_domain_id": reference["clock_domain_id"], "timing_segment_sha256": reference["segment_commitment_sha256"],
            "completion_record_sha256": digest(canonical_json_bytes(payload["completion_record"])) if "completion_record" in payload else None,
            "usage_sha256": digest(canonical_json_bytes(payload["completion_record"]["usage"])) if "completion_record" in payload else None})
    _json_file(files[profile["publication_file"]], frozen["limits"]["max_publication_bytes"], sensitive_canaries,
               schemas["completion_timing_publication_v1.schema.json"])
    checked = validate_documents(root, plan_bytes=files["completion_timing_plan.json"], evidence_bytes=files["completion_timing_evidence.json"],
        terminal_projection_bytes=canonical_json_bytes({"events": events}), publication_bytes=files[profile["publication_file"]],
        expected_manifest_file_sha256=expected_manifest_sha256, sensitive_canaries=sensitive_canaries)
    if snapshot() != files:
        fail("timing_bundle_bytes_changed")
    return {"status": "timing_bundle_verified_against_supplied_expectations", "scope": "timing_bundle_and_audit_linkage_only",
            "file_count": len(files), "run_count": len(indexed), "segment_count": len(segments),
            "audit_hash_chains_verified": bool(indexed), "timing_constraints_complete": checked["timing_gate_complete"],
            "incomplete_reasons": checked["incomplete_reasons"], "caller_closure_flags_used": False,
            "signed_expectations_verified": False, "consumption_verified": False,
            "source_budget_tool_semantics_verified": False, "actual_clock_capture_independently_proved": False,
            "runtime_authority_granted": False, "current_closure_claim_allowed": False,
            "provider_calls": 0, "real_key_loads": 0}


def verify_timing_artifact_bundle(root, directory, *, expected_manifest_sha256, expected_plan_commitment_sha256,
                                 expected_execution_binding_sha256, expected_denominator_plan_bytes,
                                 expected_case_run_ids, sensitive_canaries=()):
    """Verify immutable SQLite bytes/rows; no caller-supplied projection argument."""
    try:
        if type(root) is not type(Path()) or type(directory) is not type(Path()):
            fail("timing_artifact_path_invalid")
        return _verify_bundle(root, directory, expected_manifest_sha256=expected_manifest_sha256,
            expected_plan_commitment_sha256=expected_plan_commitment_sha256,
            expected_execution_binding_sha256=expected_execution_binding_sha256,
            expected_denominator_plan_bytes=expected_denominator_plan_bytes, expected_case_run_ids=expected_case_run_ids,
            sensitive_canaries=sensitive_canaries)
    except TimingContractError:
        raise
    except Exception as error:
        if getattr(error, "code", None) == "external_closure_sensitive_content_detected":
            fail("timing_sensitive_content")
        fail("timing_artifact_invalid")
