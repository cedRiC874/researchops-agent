"""Shared strict timing-document conformance checks.

These checks do not prove clock capture, ledger authenticity or admission.
The authoring CLI and future runtime verifier must use the same semantics.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

import jsonschema
from researchops_external_closure.io import read_regular_file_no_follow, scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes, decode_strict_json_object
from researchops_external_closure.schema_guard import LOCAL_ONLY_SCHEMA_REGISTRY, only_local_schema_references

RELATIVE = Path("evals/provider_completion_timing_v1")
CONTRACT_SHA256 = "7490576433693a45520e82bb161ee371d5a3087fa65975f5a1ac7418f6b13c6f"
CONTRACT_BYTES = 10171
DOMAINS = {kind: "researchops-provider-completion-timing-" + name + "-v1" for kind, name in (
    ("plan", "plan"), ("segment", "segment"), ("evidence", "evidence"),
    ("publication", "publication"), ("runtime_plan_core", "runtime-plan-core"),
    ("execution_binding", "execution-binding"))}
SELF_FIELDS = {kind: kind + "_commitment_sha256" for kind in ("plan", "segment", "evidence", "publication")}
_EVENTS = {
    "response_accepted": {"model_response_telemetry_recorded", "provider_completion_mapping_unmapped"},
    "response_rejected": {"model_response_telemetry_rejected"}, "http_error": {"model_request_http_error"},
    "no_response": {"model_request_no_response"}, "cancelled": {"model_request_cancelled"},
    "outcome_unknown": {"model_request_outcome_unknown"},
}


class TimingContractError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fail(code):
    raise TimingContractError(code) from None


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def commitment(kind, document):
    if kind not in DOMAINS or type(document) is not dict:
        fail("timing_schema_invalid")
    body = dict(document)
    if kind in SELF_FIELDS:
        body.pop(SELF_FIELDS[kind], None)
    return digest(DOMAINS[kind].encode() + b"\0" + canonical_json_bytes(body))


def runtime_plan_core_commitment(document):
    body = dict(document)
    body.pop("external_plan_binding_sha256", None)
    body.pop("timing_plan_commitment_sha256", None)
    return commitment("runtime_plan_core", body)


def load_contract(root=ROOT):
    try:
        raw = read_regular_file_no_follow(root / RELATIVE / "timing_contract_v1.json", max_bytes=32768)
        if len(raw) != CONTRACT_BYTES or digest(raw) != CONTRACT_SHA256:
            fail("timing_contract_invalid")
        contract = decode_strict_json_object(raw, max_bytes=32768)
        if contract["byte_protocol"]["domains"] != DOMAINS or contract["byte_protocol"]["self_fields"] != SELF_FIELDS:
            fail("timing_contract_invalid")
        schema_root = root / RELATIVE / "schemas"
        expected = {item["name"] for item in contract["schemas"]}
        # The five exact public files are checked, not a caller-selected schema.
        names = set()
        import os
        with os.scandir(schema_root) as entries:
            for entry in entries:
                if entry.name not in expected or entry.name in names:
                    fail("timing_contract_invalid")
                names.add(entry.name)
        if names != expected:
            fail("timing_contract_invalid")
        schemas = {}
        for item in contract["schemas"]:
            payload = read_regular_file_no_follow(schema_root / item["name"], max_bytes=65536)
            if len(payload) != item["bytes"] or digest(payload) != item["sha256"]:
                fail("timing_contract_invalid")
            schema = decode_strict_json_object(payload, max_bytes=65536)
            if not only_local_schema_references(schema):
                fail("timing_contract_invalid")
            jsonschema.Draft202012Validator.check_schema(schema)
            schemas[item["name"]] = schema
        segment = dict(schemas["completion_timing_segment_v1.schema.json"])
        segment.pop("$id"); segment.pop("$schema")
        if segment != schemas["completion_timing_evidence_v1.schema.json"]["properties"]["segments"]["items"]:
            fail("timing_contract_invalid")
        return contract, schemas
    except TimingContractError:
        raise
    except Exception:
        fail("timing_contract_invalid")


def _no_float(value):
    if type(value) is float:
        fail("timing_schema_invalid")
    if type(value) is dict:
        for child in value.values():
            _no_float(child)
    elif type(value) is list:
        for child in value:
            _no_float(child)


def _decode(payload, schema, maximum, canaries):
    try:
        value = decode_strict_json_object(payload, max_bytes=maximum)
        scan_public_artifact_bytes((payload, canonical_json_bytes(value)), sensitive_canaries=canaries)
        _no_float(value)
        if schema is not None:
            jsonschema.Draft202012Validator(schema, registry=LOCAL_ONLY_SCHEMA_REGISTRY).validate(value)
        return value
    except TimingContractError:
        raise
    except Exception as error:
        if getattr(error, "code", None) == "external_closure_sensitive_content_detected":
            fail("timing_sensitive_content")
        fail("timing_schema_invalid")


def _validate_documents(root, *, plan_bytes, evidence_bytes, terminal_projection_bytes,
                        publication_bytes=None, expected_manifest_file_sha256=None, sensitive_canaries=(), _scope):
    profile_hash = None
    compute = commitment
    if _scope == "campaign":
        contract, schemas = load_contract(root)
    elif _scope == "first_live":
        from .first_live import load_first_live_timing_contract, commitment as first_live_commitment, PROFILE_SHA256
        _profile, contract, schemas = load_first_live_timing_contract(root)
        profile_hash, compute = PROFILE_SHA256, first_live_commitment
    else:
        fail("timing_validation_scope_invalid")
    limits = contract["limits"]
    plan = _decode(plan_bytes, schemas["completion_timing_plan_v1.schema.json"], limits["max_plan_bytes"], sensitive_canaries)
    evidence = _decode(evidence_bytes, schemas["completion_timing_evidence_v1.schema.json"], limits["max_evidence_bytes"], sensitive_canaries)
    projection = _decode(terminal_projection_bytes, None, limits["max_evidence_bytes"], sensitive_canaries)
    if set(projection) != {"events"} or type(projection["events"]) is not list or len(projection["events"]) > 800:
        fail("timing_schema_invalid")
    events = [_decode(canonical_json_bytes(row), schemas["completion_timing_terminal_projection_v1.schema.json"],
                      limits["max_terminal_projection_bytes"], sensitive_canaries) for row in projection["events"]]
    publication = None if publication_bytes is None else _decode(publication_bytes,
        schemas["completion_timing_publication_v1.schema.json"], limits["max_publication_bytes"], sensitive_canaries)
    if profile_hash is not None:
        if any(value["first_live_profile_sha256"] != profile_hash for value in (plan, evidence) + (() if publication is None else (publication,))):
            fail("timing_first_live_profile_mismatch")
    segments = evidence["segments"]
    for kind, value in (("plan", plan), ("evidence", evidence), *(("segment", value) for value in segments)):
        if value[SELF_FIELDS[kind]] != compute(kind, value):
            fail("timing_commitment_mismatch")
    if publication is not None and publication["publication_commitment_sha256"] != compute("publication", publication):
        fail("timing_commitment_mismatch")
    if (plan["timing_contract_sha256"] != CONTRACT_SHA256 or evidence["timing_contract_sha256"] != CONTRACT_SHA256
        or evidence["plan_commitment_sha256"] != plan["plan_commitment_sha256"]
        or {key: evidence["binding"][key] for key in plan["binding"]} != plan["binding"]
        or evidence["execution_binding_sha256"] != compute("execution_binding", evidence["binding"])):
        fail("timing_binding_mismatch")
    if len(events) != len(segments):
        fail("timing_projection_mismatch")
    errors = set()
    incomplete = set()
    case_ids = plan["planned_case_handles"]
    if not len(case_ids) <= plan["max_attempts"] <= len(case_ids) * plan["max_attempts_per_case"] or len(segments) > plan["max_attempts"]:
        errors.add("timing_index_invalid")
    counts = {case: 0 for case in case_ids}
    accepted_cases = set()
    prior_case = 0
    response_index = 0
    previous = None
    for index, (segment, event) in enumerate(zip(segments, events)):
        if (segment["plan_commitment_sha256"] != plan["plan_commitment_sha256"]
            or segment["execution_binding_sha256"] != evidence["execution_binding_sha256"]):
            errors.add("timing_binding_mismatch")
        if segment["clock_domain_id"] != evidence["clock_domain_id"] or event["clock_domain_id"] != evidence["clock_domain_id"]:
            errors.add("timing_clock_domain_mismatch")
        kind = segment["terminal_kind"]
        for name in ("case_handle", "attempt_index", "response_index", "completion_record_sha256", "usage_sha256"):
            if event[name] != segment[name]:
                errors.add("timing_projection_mismatch")
        if event["event_type"] not in _EVENTS[kind] or event["timing_segment_sha256"] != segment["segment_commitment_sha256"]:
            errors.add("timing_projection_mismatch")
        accepted = kind == "response_accepted"
        if any((segment[name] is not None) != accepted for name in ("completion_record_sha256", "usage_sha256")):
            errors.add("timing_projection_mismatch")
        case = segment["case_handle"]
        if case not in counts:
            errors.add("timing_index_invalid")
        else:
            ordinal = case_ids.index(case)
            if (ordinal < prior_case or ordinal > prior_case + 1 or (index == 0 and ordinal != 0)
                or segment["case_attempt_index"] != counts[case] or counts[case] >= plan["max_attempts_per_case"]):
                errors.add("timing_index_invalid")
            counts[case] += 1
            prior_case = ordinal
        if segment["attempt_index"] != index or segment["request_index"] != index:
            errors.add("timing_index_invalid")
        has_response = kind in {"response_accepted", "response_rejected"}
        if segment["response_index"] != (response_index if has_response else None):
            errors.add("timing_index_invalid")
        if has_response:
            response_index += 1
        sent = segment["send_started_ns"]
        terminal = segment["transport_terminal_ns"]
        raw_end = segment["raw_cleanup_terminal_ns"]
        end = segment["lifetime_ended_ns"]
        if (sent is not None) != (kind in {"response_accepted", "response_rejected", "http_error", "outcome_unknown"}):
            errors.add("timing_projection_mismatch")
        known = [segment[name] for name in ("attempt_started_ns", "send_started_ns", "transport_terminal_ns", "raw_cleanup_terminal_ns", "lifetime_ended_ns") if segment[name] is not None]
        if any(right < left for left, right in zip(known, known[1:])):
            errors.add("timing_clock_order_invalid")
        expected_end = terminal if segment["raw_cleanup_status"] == "not_applicable" else raw_end
        if end != expected_end or (segment["raw_cleanup_status"] == "not_applicable" and raw_end is not None):
            errors.add("timing_lifetime_invalid")
        if sent is not None and terminal is not None and terminal - sent > plan["request_timeout_ns"]:
            errors.add("timing_request_timeout_exceeded")
        if sent is not None and sent >= plan["phase_timeout_ns"]:
            errors.add("timing_phase_timeout_exceeded")
        if previous is not None:
            if (previous["lifetime_ended_ns"] is None or previous["raw_cleanup_status"] != "completed"
                or previous["terminal_kind"] != "response_accepted"):
                errors.add("timing_lifetime_invalid")
            elif segment["attempt_started_ns"] < previous["lifetime_ended_ns"]:
                errors.add("timing_concurrency_overlap")
        if not accepted or segment["raw_cleanup_status"] != "completed":
            incomplete.add("attempt_not_successful")
        if terminal is None or end is None or (accepted and raw_end is None):
            incomplete.add("missing_attempt_offsets")
        if accepted:
            accepted_cases.add(case)
        previous = segment
    phase = evidence["phase"]
    released = phase["task_released_ns"]
    loaded = phase["key_loaded_ns"]
    phase_end = phase["phase_terminal_ns"]
    post_end = phase["post_cleanup_terminal_ns"]
    key_end = phase["key_reference_released_ns"]
    if released is not None and loaded is not None and loaded < released:
        errors.add("timing_clock_order_invalid")
    # Post cleanup and reference release can finish in either order; the
    # confirmed boundary requires phase end after BOTH, not an invented order.
    if phase_end is not None and any(value is not None and value > phase_end for value in (released, loaded, post_end, key_end)):
        errors.add("timing_clock_order_invalid")
    for segment in segments:
        if released is not None and segment["attempt_started_ns"] < released:
            errors.add("timing_clock_order_invalid")
        if segment["send_started_ns"] is not None and (released is None or loaded is None):
            errors.add("timing_binding_mismatch")
        if loaded is not None and segment["send_started_ns"] is not None and segment["send_started_ns"] < loaded:
            errors.add("timing_clock_order_invalid")
        if segment["lifetime_ended_ns"] is not None:
            if any(value is not None and value < segment["lifetime_ended_ns"] for value in (post_end, key_end, phase_end)):
                errors.add("timing_clock_order_invalid")
    if phase_end is not None and phase_end > plan["phase_timeout_ns"]:
        errors.add("timing_phase_timeout_exceeded")
    if any(value is None for value in (released, loaded, phase_end, post_end, key_end)):
        incomplete.add("missing_phase_offsets")
    if phase["post_cleanup_status"] != "completed" or phase["key_reference_release_status"] != "released":
        incomplete.add("phase_cleanup_incomplete")
    if not segments or accepted_cases != set(case_ids):
        incomplete.add("planned_case_without_accepted_response")
    if evidence["record_provenance"] != "live_monotonic_capture":
        incomplete.add("not_live_provenance")
    if publication is None:
        incomplete.add("publication_unverified")
    else:
        if (publication["timing_contract_sha256"] != CONTRACT_SHA256
            or publication["plan_commitment_sha256"] != plan["plan_commitment_sha256"]
            or publication["execution_binding_sha256"] != evidence["execution_binding_sha256"]
            or publication["timing_evidence_file_sha256"] != digest(evidence_bytes)
            or type(expected_manifest_file_sha256) is not str
            or publication["bundle_manifest_file_sha256"] != expected_manifest_file_sha256):
            errors.add("timing_binding_mismatch")
        if publication["clock_domain_id"] != evidence["clock_domain_id"]:
            errors.add("timing_clock_domain_mismatch")
        start, end = publication["sealing_started_ns"], publication["sealing_finished_ns"]
        if phase_end is not None and start < phase_end or end is not None and end < start:
            errors.add("timing_clock_order_invalid")
        if end is not None and end - start > plan["artifact_sealing_timeout_ns"]:
            errors.add("timing_sealing_timeout_exceeded")
        if publication["sealing_status"] != "sealed" or end is None:
            incomplete.add("publication_unverified")
    for code in contract["error_priority"]:
        if code in errors:
            fail(code)
    return {"status": "authoring_consistent", "scope": "untrusted_projection_conformance_only",
            "segment_count": len(segments), "timing_gate_complete": not incomplete,
            "incomplete_reasons": sorted(incomplete), "phase_duration_ns": phase_end,
            "authoritative_ledger_verified": False, "actual_clock_capture_proved": False,
            "runtime_authority_granted": False, "current_closure_claim_allowed": False,
            "provider_calls": 0, "real_key_loads": 0}


def validate_documents(root, *, plan_bytes, evidence_bytes, terminal_projection_bytes,
                       publication_bytes=None, expected_manifest_file_sha256=None, sensitive_canaries=()):
    """Original campaign authoring entrypoint; no first-live schema fallback."""
    return _validate_documents(root, plan_bytes=plan_bytes, evidence_bytes=evidence_bytes,
        terminal_projection_bytes=terminal_projection_bytes, publication_bytes=publication_bytes,
        expected_manifest_file_sha256=expected_manifest_file_sha256, sensitive_canaries=sensitive_canaries, _scope="campaign")
