"""Metadata-only failure receipts. Missing observation never becomes zero use."""
from __future__ import annotations

from researchops_external_closure.io import read_regular_file_no_follow, scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object
from researchops.model_providers import ProviderConfigurationError
from .first_live_start import FirstLiveStartError, _ClaimedFirstLiveStart
from .first_live_runtime import _FirstLiveModelFactory
from .first_live_publish import FirstLivePublishError
from .contract import _decode, digest, fail


SCHEMA_PATH = "evals/provider_completion_first_live_failure_v1/receipt_v1.schema.json"
SCHEMA_SHA256 = "b4b90d7e1ff99b3afa8341e47703a5128c1f96bbe6cb3115c3bf759b043f7eb6"
_SAFE_CODES = {
    "first_live_start_authorization_expired": "first_live_authorization_expired",
    "first_live_request_source_mismatch": "first_live_source_changed",
    "first_live_start_post_claim_source_mismatch": "first_live_source_changed",
    "provider_completion_http_error": "first_live_http_error",
    "first_live_response_contract_mismatch": "first_live_output_rejected",
    "first_live_publish_timeout": "first_live_timeout",
}


def _schema(root):
    payload = read_regular_file_no_follow(root / SCHEMA_PATH, max_bytes=4096)
    if len(payload) != 2845 or digest(payload) != SCHEMA_SHA256:
        fail("first_live_failure_schema_invalid")
    return decode_strict_json_object(payload, max_bytes=4096)


def validate_failure_receipt(root, payload):
    value = _decode(payload, _schema(root), 4096, ())
    if raw(value) != payload:
        fail("first_live_failure_noncanonical")
    scope = value["observation_scope"]
    if scope == "preparation_branch":
        if (value["status"] != "failed_before_dispatch" or value["observed_send_markers"] != 0
            or value["dispatch_attempt_count"] != 0 or value["validated_response_count"] != 0 or value["inflight"] is not False):
            fail("first_live_failure_observation_mismatch")
    elif scope == "insufficient_observation":
        if value["status"] != "outcome_unknown" or any(value[name] is not None for name in
                ("observed_send_markers", "dispatch_attempt_count", "validated_response_count", "inflight")):
            fail("first_live_failure_observation_mismatch")
    else:
        observed, exact, validated = (value[name] for name in ("observed_send_markers", "dispatch_attempt_count", "validated_response_count"))
        if observed is None or validated is None or validated > observed or value["claim_consumed"] is not True:
            fail("first_live_failure_observation_mismatch")
        if exact is not None and (exact != observed or value["inflight"] is not False):
            fail("first_live_failure_observation_mismatch")
        if value["status"] == "failed_before_dispatch" and exact != 0:
            fail("first_live_failure_observation_mismatch")
        if value["status"] == "failed" and (exact is None or exact == 0):
            fail("first_live_failure_observation_mismatch")
    scan_public_artifact_bytes((payload,))
    return value


def build_first_live_failure_receipt(root, error, *, factory=None):
    """No exception text/class name/body is copied, hashed or stringified.

    Preparation origin is assigned by that known no-Provider branch. A generic
    exception with no owned factory has unknown counts. Receipt data is never
    authorization, reconstruction of a Key lifetime, or a failure-archive proof.
    """
    if not isinstance(error, BaseException):
        fail("first_live_failure_exception_required")
    known = type(error) in (FirstLiveStartError, ProviderConfigurationError, FirstLivePublishError)
    fields = vars(error) if known else {}
    code = fields.get("code")
    safe = _SAFE_CODES.get(code) if type(code) is str else None
    origin = fields.get("origin")
    preparation = type(error) is FirstLiveStartError and type(origin) is str and origin == "preparation" and factory is None
    if safe is None:
        safe = ("first_live_publication_failed" if type(error) is FirstLivePublishError else
                "first_live_preparation_failed" if preparation else
                "first_live_runtime_failed" if type(factory) is _FirstLiveModelFactory else "first_live_failure_unknown")
    claim = fields.get("claim_consumed")
    claim = claim if type(claim) is bool else None
    value = dict(schema_version="provider-completion-first-live-failure/1.0", status="outcome_unknown", error_code=safe,
        observation_scope="insufficient_observation", claim_consumed=claim, observed_send_markers=None,
        dispatch_attempt_count=None, validated_response_count=None, inflight=None, token_usage=None, provider_bill=None,
        exception_text_recorded=False, model_content_recorded=False, api_key_recorded=False, retry_authorized=False,
        resume_authorized=False, fallback_authorized=False, model_quality_claim_allowed=False,
        runtime_authority_granted=False, closure_claim_allowed=False)
    if preparation:
        value.update(status="failed_before_dispatch", observation_scope="preparation_branch", observed_send_markers=0,
            dispatch_attempt_count=0, validated_response_count=0, inflight=False)
    elif type(factory) is _FirstLiveModelFactory:
        try:
            prepared = vars(factory)["_prepared"]
            if type(prepared) is not _ClaimedFirstLiveStart:
                raise ValueError("unobserved")
            value["claim_consumed"] = True
            snapshot = prepared.clock.snapshot()
            records = vars(factory)["_records"]
            inflight = vars(factory)["_inflight"]
            attempts = snapshot["attempts"]
            active = snapshot["active_attempt"]
            if type(records) is not list or len(records) > 2 or type(inflight) is not bool or type(attempts) is not list or len(attempts) > 2:
                raise ValueError("unobserved")
            all_attempts = attempts + ([] if active is None else [active])
            if len(all_attempts) > 2 or any(type(item) is not dict for item in all_attempts):
                raise ValueError("unobserved")
            if type(snapshot["clock_failed"]) is not bool or any(item.get("terminal_kind") not in
                    {"response_accepted", "response_rejected", "http_error", "no_response", "cancelled", "outcome_unknown"} for item in attempts):
                raise ValueError("unobserved")
            markers = [item["send_started_ns"] for item in all_attempts]
            if any(marker is not None and (type(marker) is not int or not 0 <= marker <= 9007199254740991) for marker in markers):
                raise ValueError("unobserved")
            observed = sum(marker is not None for marker in markers)
            if len(records) > observed:
                raise ValueError("unobserved")
            settled = not inflight and active is None and snapshot["clock_failed"] is False
            unknown = type(error) is FirstLivePublishError or not settled or any(item.get("terminal_kind") == "outcome_unknown" for item in attempts)
            value.update(status="outcome_unknown" if unknown else "failed_before_dispatch" if observed == 0 else "failed",
                observation_scope="owned_clock_and_factory", observed_send_markers=observed,
                dispatch_attempt_count=observed if settled else None, validated_response_count=len(records), inflight=inflight)
        except Exception:
            pass  # Keep explicit unknowns; do not infer zero from a lost handle.
    payload = raw(value)
    validate_failure_receipt(root, payload)
    return payload
