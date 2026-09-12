"""Versioned terminal-event preparation; never a consumed-authority issuer.

The writer still requires its original runtime/case/terminal capability. A valid
reference proves only the segment-to-record linkage, not that the timing origin
followed independent consumption or that a complete run may close T7.
"""
from __future__ import annotations

from pathlib import Path

from researchops_external_closure.io import read_regular_file_no_follow, scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes, decode_strict_json_object
from .contract import CONTRACT_SHA256, _decode, commitment, digest, fail, load_contract


ROOT = Path(__file__).resolve().parents[2]
LEDGER_CONTRACT_PATH = Path("evals/provider_completion_timed_ledger_v1/contract_v1.json")
LEDGER_CONTRACT_SHA256 = "5acc5b2d7d018202a5af42a736d178fa4e0cdc74b25a60a055d2883bc93b7002"
EVENT_VERSION = "provider-completion-ledger-event/1.2"


def load_ledger_contract(root=ROOT):
    raw = read_regular_file_no_follow(root / LEDGER_CONTRACT_PATH, max_bytes=4096)
    if len(raw) != 2717 or digest(raw) != LEDGER_CONTRACT_SHA256:
        fail("timing_ledger_contract_invalid")
    return decode_strict_json_object(raw, max_bytes=4096)


def validate_segment_link(payload, *, event_type, segment_bytes, expected_timing_binding,
                          root=ROOT, sensitive_canaries=()):
    """Check an already validated terminal payload against its segment bytes.

    Independent ledger/record validation is a caller obligation on read paths.
    The write entry below enforces it via the unchanged predecessor validator.
    """
    protocol = load_ledger_contract(root)
    frozen, schemas = load_contract(root)
    if type(event_type) is not str or event_type not in protocol["allowed_terminal_events"]:
        fail("timing_terminal_event_invalid")
    if (type(expected_timing_binding) is not dict or set(expected_timing_binding) !=
        {"plan_commitment_sha256", "execution_binding_sha256", "clock_domain_id"}
        or any(type(value) is not str for value in expected_timing_binding.values())):
        fail("timing_binding_mismatch")
    expected_timing_binding = dict(expected_timing_binding)
    segment = _decode(segment_bytes, schemas["completion_timing_segment_v1.schema.json"],
                      frozen["limits"]["max_segment_bytes"], sensitive_canaries)
    if segment["segment_commitment_sha256"] != commitment("segment", segment):
        fail("timing_commitment_mismatch")
    if any(segment[key] != value for key, value in expected_timing_binding.items()):
        fail("timing_binding_mismatch")
    pairs = (("case_handle", "case_id"), ("attempt_index", "attempt_index"),
             ("case_attempt_index", "case_attempt_index"), ("request_index", "attempt_index"),
             ("response_index", "response_index"), ("terminal_kind", "terminal_kind"))
    if any(segment[left] != payload.get(right) for left, right in pairs):
        fail("timing_projection_mismatch")
    from .contract import _EVENTS
    if event_type not in _EVENTS[segment["terminal_kind"]]:
        fail("timing_projection_mismatch")
    accepted = segment["terminal_kind"] == "response_accepted"
    record = payload.get("completion_record")
    if accepted:
        if type(record) is not dict or type(record.get("usage")) is not dict:
            fail("timing_projection_mismatch")
        record_hash, usage_hash = digest(canonical_json_bytes(record)), digest(canonical_json_bytes(record["usage"]))
    else:
        if "completion_record" in payload:
            fail("timing_projection_mismatch")
        record_hash = usage_hash = None
    if (segment["completion_record_sha256"] != record_hash or segment["usage_sha256"] != usage_hash):
        fail("timing_projection_mismatch")
    # Semantic duration/order/completeness checks belong to the full evidence
    # verifier, which has the independent plan and the complete denominator.
    return {"timing_contract_sha256": CONTRACT_SHA256,
            **{key: segment[key] for key in protocol["extension_fields"] if key != "timing_contract_sha256"}}


def prepare_timed_terminal(event_type, legacy_payload, *, runtime_binding, binding,
                           segment_bytes, expected_timing_binding, root=ROOT, sensitive_canaries=()):
    """Validate/scan before any write; legacy extras and new-version input fail."""
    from researchops.audit import _validated_completion_event_payload
    protocol = load_ledger_contract(root)
    # Freeze caller-owned nested data BEFORE comparing record/usage hashes.
    # Copying only after those comparisons leaves a mutation window.
    try:
        incoming = canonical_json_bytes(dict(legacy_payload))
        if len(incoming) > protocol["max_terminal_payload_bytes"]:
            fail("timing_terminal_payload_too_large")
        scan_public_artifact_bytes((incoming,), sensitive_canaries=sensitive_canaries)
        snapshot = decode_strict_json_object(incoming, max_bytes=protocol["max_terminal_payload_bytes"])
    except Exception as error:
        if getattr(error, "code", None) == "external_closure_sensitive_content_detected":
            fail("timing_sensitive_content")
        if getattr(error, "code", None) == "timing_terminal_payload_too_large":
            raise
        fail("timing_schema_invalid")
    validated = _validated_completion_event_payload(event_type, snapshot,
        runtime_binding=runtime_binding, binding=binding)
    reference = validate_segment_link(validated, event_type=event_type, segment_bytes=segment_bytes,
        expected_timing_binding=expected_timing_binding, root=root, sensitive_canaries=sensitive_canaries)
    result = dict(validated, schema_version=EVENT_VERSION, timing=reference)
    raw = canonical_json_bytes(result)
    if len(raw) > protocol["max_terminal_payload_bytes"]:
        fail("timing_terminal_payload_too_large")
    scan_public_artifact_bytes((raw,), sensitive_canaries=sensitive_canaries)
    # Detached from mutable caller-owned record/usage dictionaries before DB IO.
    return decode_strict_json_object(raw, max_bytes=protocol["max_terminal_payload_bytes"])
