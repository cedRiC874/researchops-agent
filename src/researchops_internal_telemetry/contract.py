"""Internal v1 strict documents; no authority, Key, store or network actions."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from researchops_external_closure.io import read_regular_file_no_follow, scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = "evals/provider_completion_internal_v1"
SCOPE = "internal_known_synthetic"
RUNTIME_SCOPE = "internal_telemetry_validation"
ENVIRONMENT_ID = "PCEENV-48947297F99FE2C71099529BACF70106"
SHA = re.compile(r"(?!0{64}$)[0-9a-f]{64}")


class InternalError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def safe_error_code(error):
    """Never persist a Provider-supplied error code, even if it looks harmless."""
    modules = {__name__, "researchops_completion_timing.clock", "researchops_completion_timing.contract",
        "researchops_completion_timing.campaign_budget", "researchops_completion_timing.local_claim",
        "researchops.model_providers", "researchops.audit", "researchops_completion_telemetry.sanitization",
        "researchops_completion_telemetry.capture", "researchops_completion_telemetry.surface_mapping",
        "researchops_external_closure.errors"}
    if type(error).__module__ not in modules:
        return "internal_execution_failed"
    code = getattr(error, "code", None)
    return code if type(code) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code) else "internal_execution_failed"


def require(condition, code):
    if not condition:
        raise InternalError("internal_" + code)


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def commitment(kind, value):
    return digest(b"researchops.internal.v1\0" + kind.encode("ascii") + b"\0" + raw(value))


def exact(value, fields, code="document_fields"):
    require(type(value) is dict and set(value) == set(fields.split()), code)


def sha(value):
    require(type(value) is str and SHA.fullmatch(value) is not None, "digest_invalid")
    return value


def decode(payload, maximum=262144):
    require(type(payload) is bytes and len(payload) <= maximum, "document_size")
    value = decode_strict_json_object(payload, max_bytes=maximum)
    scan_json(value)
    return value


def scan_json(value, *, sensitive_canaries=()):
    """Scan decoded strings: a JSON-escaped single slash is not a UNC path.

    Exact schemas/allowlists remain caller obligations. Keys are scanned too;
    decoding Unicode escapes cannot conceal a secret or absolute path.
    Binary artifacts still use the unchanged raw-byte scanner.
    """
    pending, strings, count = [(value, 0)], [], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        require(depth <= 64 and count <= 32768, "privacy_shape_limit")
        if type(item) is str:
            strings.append(item.encode("utf-8"))
        elif type(item) is dict:
            pending.extend((key, depth + 1) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
    scan_public_artifact_bytes(tuple(strings), sensitive_canaries=sensitive_canaries)


def read(path, maximum=262144):
    return read_regular_file_no_follow(Path(path), max_bytes=maximum)


def utc(value):
    require(type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value), "utc_invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InternalError("internal_utc_invalid") from None


def now():
    return datetime.now(timezone.utc)


def load_policy(root=ROOT):
    policy = decode(read(Path(root) / DIRECTORY / "protocol_v1.json"))
    from jsonschema import Draft202012Validator
    schema = decode(read(Path(root) / DIRECTORY / "protocol_v1.schema.json"))
    require(set(schema) == {"$schema", "title", "const"}, "protocol_schema")
    # Only const is interpreted. A modified schema cannot introduce a remote
    # $ref and cause network IO before source verification or claim consumption.
    require(not list(Draft202012Validator({"const": schema["const"]}).iter_errors(policy)), "protocol_schema")
    require(policy["protocol_id"] == "t6-b-internal-v1" and policy["scope"] == SCOPE, "protocol_invalid")
    return policy


def validate_tasks(document, policy):
    exact(document, "schema_version scope developer_known external_unseen case_count cases")
    require(document["schema_version"] == "provider-completion-internal-tasks/1.0" and document["scope"] == SCOPE
            and document["developer_known"] is True and document["external_unseen"] is False, "tasks_provenance")
    cases = document["cases"]
    require(type(document["case_count"]) is int and document["case_count"] == policy["case_count"]
            and type(cases) is list and len(cases) == policy["case_count"], "case_count")
    ids, texts = set(), set()
    for index, case in enumerate(cases):
        exact(case, "case_id case_index input input_sha256 expected_state max_output_tokens")
        require(type(case["case_id"]) is str and re.fullmatch(r"PCECASE-[A-F0-9]{32}", case["case_id"]), "case_id")
        require(type(case["case_index"]) is int and case["case_index"] == index, "case_order")
        require(type(case["input"]) is str and 0 < len(case["input"].encode("utf-8")) <= policy["limits"]["case_input_bytes"], "case_input")
        require("\x00" not in case["input"], "case_input")
        require(case["input_sha256"] == digest(case["input"].encode("utf-8")), "case_input_digest")
        scan_public_artifact_bytes((case["input"].encode("utf-8"),))
        require(case["expected_state"] == policy["expected_state"] and type(case["max_output_tokens"]) is int
                and case["max_output_tokens"] == policy["max_output_tokens_per_request"], "case_expectation")
        require(case["case_id"] not in ids and case["input"] not in texts, "case_duplicate")
        ids.add(case["case_id"]); texts.add(case["input"])
    return document


def validate_pricing(document):
    exact(document, "schema_version status provider_id requested_model input_price_per_million_cny output_price_per_million_cny cache_discount_assumed provider_invoice_hard_cap evidence_date_utc official_evidence_sha256")
    require(document["schema_version"] == "provider-completion-internal-pricing/1.0"
            and document["status"] == "user_reviewed_official_snapshot"
            and document["provider_id"] == "deepseek" and document["requested_model"] == "deepseek-v4-flash"
            and document["cache_discount_assumed"] is False and document["provider_invoice_hard_cap"] is False, "pricing_not_ready")
    for name in ("input_price_per_million_cny", "output_price_per_million_cny"):
        require(type(document[name]) is str and re.fullmatch(r"(?!0\.000000$)(0|[1-9][0-9]{0,6})\.[0-9]{6}", document[name]), "pricing_invalid")
    utc(document["evidence_date_utc"])
    sha(document["official_evidence_sha256"])
    return document


def budget_plan(policy, tasks, pricing):
    """Adapt numeric policy only, not an external admission or runtime envelope."""
    validate_pricing(pricing)
    return dict(denominator_plan=dict(case_ids=[case["case_id"] for case in tasks["cases"]], max_turns_per_case=1,
        total_model_request_cap=30, agents_sdk_retries=0, http_client_retries=0),
        transport_limits=dict(network_attempt_cap=30, concurrency=1, tools=0, resume=False, fallback=False),
        budget_policy=dict(input_token_limit_total=policy["input_tokens_observed_stop"], output_token_limit_per_request=512,
            output_token_limit_total=15360, input_price_per_million_cny=pricing["input_price_per_million_cny"],
            output_price_per_million_cny=pricing["output_price_per_million_cny"], local_observed_cost_stop_cny="1.000000",
            currency="CNY", token_price_unit="per_million_tokens", cache_discount_assumed=False, provider_invoice_hard_cap=False,
            request_count_and_output_caps_enforced_pre_send=True, single_in_flight_request_may_overshoot_observed_limits=True,
            input_token_limit_enforcement="post_response_observed_stop", cost_stop_enforcement="post_response_observed_stop"))
