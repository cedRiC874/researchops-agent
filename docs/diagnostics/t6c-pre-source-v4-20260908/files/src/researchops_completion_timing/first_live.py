"""First-live timing authoring profile, not a successful live run or authority."""
from __future__ import annotations

import os

import jsonschema

from researchops_external_closure.io import read_regular_file_no_follow, scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes, decode_strict_json_object
from researchops_external_closure.schema_guard import only_local_schema_references
from . import contract as core


PROFILE_SHA256 = "f104f5c4cc483a708313e6cdd984145ee8b231f8e4489d9ff52426ed4b9fdd2a"
_DIRECTORY = "evals/provider_completion_first_live_timing_v1"
DOMAINS = {kind: "researchops-provider-completion-first-live-timing-" + kind + "-v1" for kind in ("plan", "evidence", "publication")}


def commitment(kind, document):
    if kind in {"segment", "execution_binding"}:
        return core.commitment(kind, document)
    if kind not in DOMAINS or type(document) is not dict:
        core.fail("timing_first_live_commitment_kind_invalid")
    body = dict(document); body.pop(core.SELF_FIELDS[kind], None)
    return core.digest(DOMAINS[kind].encode() + b"\0" + canonical_json_bytes(body))


def validation_run_id(binding):
    try:
        raw = (b"researchops-first-live-timing-run-v1\0" + bytes.fromhex(binding["authorization_id_sha256"])
               + b"\0" + binding["execution_environment_id"].encode("ascii") + b"\0" + binding["execution_commit"].encode("ascii"))
        return "PCEVALID-" + core.digest(raw)[:32].upper()
    except Exception:
        core.fail("timing_first_live_binding_invalid")


def load_first_live_timing_contract(root):
    parent, schemas = core.load_contract(root)
    raw = read_regular_file_no_follow(root / _DIRECTORY / "contract_v1.json", max_bytes=8192)
    if len(raw) != 4482 or core.digest(raw) != PROFILE_SHA256:
        core.fail("timing_first_live_contract_invalid")
    profile = decode_strict_json_object(raw, max_bytes=8192)
    if profile["core_timing_contract_sha256"] != core.CONTRACT_SHA256 or profile["domains"] != DOMAINS:
        core.fail("timing_first_live_contract_invalid")
    for item in (profile["frozen_design"], profile["predecessor_implementation"]):
        payload = read_regular_file_no_follow(root / item["path"], max_bytes=32768)
        if len(payload) != item["bytes"] or core.digest(payload) != item["sha256"]:
            core.fail("timing_first_live_predecessor_drift")
    claim = read_regular_file_no_follow(root / "evals/provider_completion_local_claim_v1/contract_v1.json", max_bytes=8192)
    if core.digest(claim) != profile["local_claim_contract_sha256"]:
        core.fail("timing_first_live_contract_invalid")
    expected = {item["name"] for item in profile["schemas"]}
    found = set()
    with os.scandir(root / _DIRECTORY / "schemas") as entries:
        for entry in entries:
            if entry.name not in expected or entry.name in found:
                core.fail("timing_first_live_contract_invalid")
            found.add(entry.name)
    if found != expected:
        core.fail("timing_first_live_contract_invalid")
    for item in profile["schemas"]:
        raw = read_regular_file_no_follow(root / _DIRECTORY / "schemas" / item["name"], max_bytes=65536)
        if len(raw) != item["bytes"] or core.digest(raw) != item["sha256"]:
            core.fail("timing_first_live_contract_invalid")
        schema = decode_strict_json_object(raw, max_bytes=65536)
        if not only_local_schema_references(schema):
            core.fail("timing_first_live_contract_invalid")
        jsonschema.Draft202012Validator.check_schema(schema)
        schemas[item["name"]] = schema
    segment = dict(schemas["completion_timing_segment_v1.schema.json"])
    segment.pop("$id"); segment.pop("$schema")
    if schemas["completion_timing_evidence_v1.schema.json"]["properties"]["segments"]["items"] != segment:
        core.fail("timing_first_live_contract_invalid")
    return profile, parent, schemas


def validate_first_live_timing_documents(root, *, plan_bytes, evidence_bytes, terminal_projection_bytes,
                                       publication_bytes=None, expected_manifest_file_sha256=None, sensitive_canaries=()):
    """Same numeric engine, distinct local-claim binding; supplied data is not live proof."""
    result = core._validate_documents(root, plan_bytes=plan_bytes, evidence_bytes=evidence_bytes,
        terminal_projection_bytes=terminal_projection_bytes, publication_bytes=publication_bytes,
        expected_manifest_file_sha256=expected_manifest_file_sha256, sensitive_canaries=sensitive_canaries, _scope="first_live")
    plan = decode_strict_json_object(plan_bytes, max_bytes=131072)
    if plan["binding"]["validation_run_id"] != validation_run_id(plan["binding"]):
        core.fail("timing_first_live_binding_invalid")
    return dict(result, scope="first_live_timing_authoring_only", first_live_success_verified=False)


def verify_first_live_record_shapes(root, record_bytes, *, sensitive_canaries=()):
    """Strict persisted-record pair, still not ledger/control/live provenance proof."""
    from researchops_completion_telemetry.surface_mapping import load_and_select_surface_mapping
    from researchops_completion_telemetry.persisted_evidence import (
        create_persisted_live_evidence_validation_context, validate_persisted_live_completion_records,
    )
    profile, _, _ = load_first_live_timing_contract(root)
    document = decode_strict_json_object(record_bytes, max_bytes=262144)
    scan_public_artifact_bytes((record_bytes, canonical_json_bytes(document)), sensitive_canaries=sensitive_canaries)
    if set(document) != {"records"} or type(document["records"]) is not list or len(document["records"]) != 2:
        core.fail("timing_first_live_record_coverage_invalid")
    records = document["records"]
    selection = load_and_select_surface_mapping(root, "deepseek", "responses", "openai_compatible_responses", purpose="offline_validation")
    names = ("telemetry_schema_sha256", "adapter_version", "mapping_schema_version", "mapping_version", "mapping_sha256",
             "provider_id", "api_surface", "transport_id", "output_counter_comparability", "output_counter_path")
    context = create_persisted_live_evidence_validation_context(selection, expected_binding={name: getattr(selection, name) for name in names})
    validate_persisted_live_completion_records(records, context=context)
    total_input = total_output = 0
    for index, (record, scenario) in enumerate(zip(records, profile["scenario_mapping"])):
        cap = scenario["max_output_tokens"]
        if (record["request_index"] != index or record["response_index"] != index
            or record["normalized_completion_state"] != scenario["required_completion_state"]
            or record["truncation_signal_source"] != "native_status"
            or record["output_token_cap"] != {"availability": "provided", "value": cap}
            or record["http_status"] != {"availability": "provided", "value": 200}
            or record["output_counter_comparability"] != "comparable" or not record["usage"]["complete"]):
            core.fail("timing_first_live_record_shape_invalid")
        usage = record["usage"]["normalized"]
        if (usage["requests"] != 1 or type(usage["input_tokens"]) is not int or not 0 <= usage["input_tokens"] <= 512
            or type(usage["output_tokens"]) is not int or not 0 <= usage["output_tokens"] <= cap
            or (index == 1 and usage["output_tokens"] != cap)):
            core.fail("timing_first_live_record_usage_invalid")
        total_input += usage["input_tokens"]
        total_output += usage["output_tokens"]
    if total_input > profile["limits"]["input_tokens_total"] or total_output > profile["limits"]["output_tokens_total"]:
        core.fail("timing_first_live_record_usage_invalid")
    return {"status": "record_shapes_verified", "record_count": 2, "input_tokens": total_input, "output_tokens": total_output,
            "audit_linkage_verified": False, "control_artifacts_verified": False, "first_live_success_verified": False,
            "runtime_authority_granted": False, "provider_calls": 0, "real_key_loads": 0}
