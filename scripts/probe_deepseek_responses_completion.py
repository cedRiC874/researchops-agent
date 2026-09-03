"""Contract-development probe: DeepSeek /responses completion metadata shape.

Not an evaluation task. Does not enter a run ledger, produce ``task_pass``, or
support any model-quality or Provider-registration claim. Retains only the
allowlisted response projection; the raw response body is never persisted.

Usage: ``python scripts/probe_deepseek_responses_completion.py --confirm-online``
Writes no files. Prints one canonical JSON object to stdout.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import logging
import os
import re
import sys
from typing import Any


BASE_URL = "https://api.deepseek.com"
PATH = "/responses"
MODEL = "deepseek-v4-flash"
EXPECTED_OPENAI_SDK_VERSION = "3.1.0"
PREDECESSOR_RECEIPT_SHA256 = (
    "d124a07f40b1031247a832409bbf13f9c352e42862f3d4c64da6690eb89709ad"
)
TIMEOUT_SECONDS = 120.0
TOTAL_TIMEOUT_SECONDS = 300.0
CLOSE_TIMEOUT_SECONDS = 5.0
NORMAL_MAX_OUTPUT_TOKENS = 256
FORCED_CAP_MAX_OUTPUT_TOKENS = 16
MESSAGE_STAGE_MAX_OUTPUT_TOKENS = 96
MESSAGE_STAGE_PROBE_LABEL = "responses_message_stage_cap_attempt"
MESSAGE_STAGE_INSTRUCTION = (
    "Repeat the word ok exactly 500 times, separated by single spaces. "
    "Do not think or add anything else."
)
DISTINCT_SHAPE_DEFINITION = (
    "(status_key_present,status_value); incomplete_details_reason_presence; "
    "ordered output_item_shapes ((type_key_present,type_value),"
    "(status_key_present,status_value)); sorted top_level_presence key/value pairs"
)
MAX_RAW_RESPONSE_BYTES = 1024 * 1024

RESPONSE_PROJECTION_KEYS = (
    "status",
    "finish_reason",
    "stop_reason",
    "incomplete_details",
    "usage",
)
PRESENCE_KEYS = ("status", "incomplete_details", "stop_sequence")
SHAPE_ONLY_TOP_LEVEL_KEYS = ("content_filters", "error", "truncation")
USAGE_COUNT_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
)
USAGE_DETAIL_KEYS = {
    "input_tokens_details": ("cached_tokens",),
    "output_tokens_details": ("reasoning_tokens",),
}
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_SAFE_SCHEMA_VALUE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_FORBIDDEN_OPENAI_ENVIRONMENT = (
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_CUSTOM_HEADERS",
    "OPENAI_LOG",
)
_NETWORK_LOGGERS = ("openai", "httpx", "httpx2", "httpcore")


class ProbeError(RuntimeError):
    """Stable, redacted probe failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProbeRunError(ProbeError):
    """Redacted failure bound to one dispatched probe request."""

    def __init__(
        self,
        code: str,
        *,
        probe_label: str,
        client_dispatches: int,
        http_status: int | None,
        request_id: object,
        completed_probes: list[dict[str, object]],
        raw_response_cleanup_state: str | None = None,
    ) -> None:
        super().__init__(code)
        self.probe_label = probe_label
        self.client_dispatches = client_dispatches
        self.http_status = http_status
        self.request_id_present = request_id is not None
        self.request_id_sha256 = _sha256_request_id(request_id)
        self.request_id_hash_withheld = (
            request_id is not None and self.request_id_sha256 is None
        )
        self.completed_probes = tuple(completed_probes)
        self.raw_response_cleanup_state = raw_response_cleanup_state


class ProbeRawCleanupError(ProbeError):
    """Raw response cleanup failed after a sanitized record was built."""

    def __init__(
        self,
        completed_probe: dict[str, object],
        *,
        http_status: object,
        request_id: object,
    ) -> None:
        super().__init__("deepseek_responses_raw_close_failed")
        self.completed_probe = completed_probe
        self.status_code = http_status
        self.request_id = request_id


class ProbeResponseProcessingError(ProbeError):
    """Local response-processing failure with observed transport metadata."""

    def __init__(
        self,
        code: str,
        *,
        http_status: object,
        request_id: object,
    ) -> None:
        super().__init__(code)
        self.status_code = http_status
        self.request_id = request_id


class ProbeProgress:
    """In-memory progress needed to make timeout receipts non-vacuous."""

    def __init__(self) -> None:
        self.client_dispatches = 0
        self.active_probe_label: str | None = None
        self.current_sanitized_probe: dict[str, object] | None = None
        self.active_raw_response_cleanup_state: str | None = None

    def begin(self, probe_label: str) -> None:
        if self.active_probe_label is not None:
            raise ProbeError("deepseek_responses_progress_reentry")
        self.client_dispatches += 1
        self.active_probe_label = probe_label
        self.current_sanitized_probe = None
        self.active_raw_response_cleanup_state = "not_started"

    def retain_current(self, record: dict[str, object]) -> None:
        self.current_sanitized_probe = record

    def finish(self) -> None:
        self.active_probe_label = None
        self.current_sanitized_probe = None
        self.active_raw_response_cleanup_state = None

    def retained_records(
        self,
        completed_probes: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        retained = list(completed_probes)
        if self.current_sanitized_probe is not None:
            retained.append(self.current_sanitized_probe)
        return retained


def _sha256_request_id(value: object) -> str | None:
    if not isinstance(value, str) or not _SAFE_REQUEST_ID.fullmatch(value):
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _environment_is_isolated() -> bool:
    return not any(os.environ.get(name) for name in _FORBIDDEN_OPENAI_ENVIRONMENT)


def _network_debug_logging_is_disabled() -> bool:
    return not any(
        logging.getLogger(name).isEnabledFor(logging.DEBUG)
        for name in _NETWORK_LOGGERS
    )


def _openai_sdk_version_is_exact() -> bool:
    try:
        return importlib.metadata.version("openai") == EXPECTED_OPENAI_SDK_VERSION
    except importlib.metadata.PackageNotFoundError:
        return False


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, child in pairs:
        if key in value:
            raise ProbeError("deepseek_responses_duplicate_json_key")
        value[key] = child
    return value


def _reject_nonfinite(value: str) -> object:
    del value
    raise ProbeError("deepseek_responses_nonfinite_json")


def _load_raw_body(payload: bytes) -> dict[str, object]:
    if len(payload) > MAX_RAW_RESPONSE_BYTES:
        raise ProbeError("deepseek_responses_body_too_large")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeError("deepseek_responses_body_not_utf8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except ProbeError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProbeError("deepseek_responses_body_not_json") from exc
    finally:
        text = ""
    if not isinstance(value, dict):
        raise ProbeError("deepseek_responses_body_not_object")
    return value


def _bounded_schema_value(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, str) or not _SAFE_SCHEMA_VALUE.fullmatch(value):
        raise ProbeError("deepseek_responses_schema_value_unsafe")
    return value


def _usage_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProbeError("deepseek_responses_usage_count_invalid")
    return value


def _project_incomplete_details(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProbeError("deepseek_responses_incomplete_details_not_object")
    projection: dict[str, object] = {}
    if "reason" in value:
        projection["reason"] = _bounded_schema_value(value["reason"])
    return projection


def _project_usage(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProbeError("deepseek_responses_usage_not_object")
    projection: dict[str, object] = {}
    for key in USAGE_COUNT_KEYS:
        if key in value:
            projection[key] = _usage_count(value[key])
    for detail_key, count_keys in USAGE_DETAIL_KEYS.items():
        if detail_key not in value:
            continue
        details = value[detail_key]
        if details is None:
            projection[detail_key] = None
            continue
        if not isinstance(details, dict):
            raise ProbeError("deepseek_responses_usage_details_not_object")
        detail_projection: dict[str, int] = {}
        for count_key in count_keys:
            if count_key in details:
                detail_projection[count_key] = _usage_count(details[count_key])
        projection[detail_key] = detail_projection
    return projection


def _project(body: dict[str, object]) -> dict[str, object]:
    """Return a bounded projection while preserving key absence versus null."""

    projection: dict[str, object] = {}
    for key in RESPONSE_PROJECTION_KEYS:
        if key not in body:
            continue
        value = body[key]
        if key in {"status", "finish_reason", "stop_reason"}:
            projection[key] = _bounded_schema_value(value)
        elif key == "incomplete_details":
            projection[key] = _project_incomplete_details(value)
        else:
            projection[key] = _project_usage(value)
    return projection


def _presence(body: dict[str, object], key: str) -> str:
    if key not in body:
        return "missing"
    if body[key] is None:
        return "present_null"
    return "present_non_null"


def _incomplete_reason_presence(body: dict[str, object]) -> str:
    if "incomplete_details" not in body:
        return "parent_missing"
    details = body["incomplete_details"]
    if details is None:
        return "parent_null"
    if not isinstance(details, dict):
        return "parent_non_object"
    if "reason" not in details:
        return "reason_missing"
    if details["reason"] is None:
        return "reason_present_null"
    return "reason_present_non_null"


def _top_level_keys(body: dict[str, object]) -> list[str]:
    keys = sorted(body)
    if len(keys) > 128 or any(not _SAFE_SCHEMA_VALUE.fullmatch(key) for key in keys):
        raise ProbeError("deepseek_responses_top_level_key_unsafe")
    return keys


def _nested_keys(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return []
    return _top_level_keys(value)


def _output_item_shapes(body: dict[str, object]) -> list[dict[str, object]]:
    """Return item index/type/status only; never content, arguments, or IDs."""

    items = body.get("output")
    if not isinstance(items, list):
        return []
    shapes: list[dict[str, object]] = []
    for index, item in enumerate(items[:128]):
        if not isinstance(item, dict):
            shapes.append({"index": index, "non_object": True})
            continue
        shape: dict[str, object] = {
            "index": index,
            "status_key_present": "status" in item,
        }
        if "type" in item:
            shape["type"] = _bounded_schema_value(item["type"])
        if "status" in item:
            shape["status"] = _bounded_schema_value(item["status"])
        shapes.append(shape)
    return shapes


def _json_type_name(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "non_json"


def _output_container_diagnostic(body: dict[str, object]) -> dict[str, object]:
    if "output" not in body:
        return {
            "presence": "missing",
            "json_type": None,
            "item_count": None,
            "shapes_truncated": False,
        }
    value = body["output"]
    if value is None:
        return {
            "presence": "present_null",
            "json_type": "null",
            "item_count": None,
            "shapes_truncated": False,
        }
    if not isinstance(value, list):
        return {
            "presence": "present_non_null",
            "json_type": _json_type_name(value),
            "item_count": None,
            "shapes_truncated": False,
        }
    return {
        "presence": "present_non_null",
        "json_type": "array",
        "item_count": len(value),
        "shapes_truncated": len(value) > 128,
    }


def _shape_only_top_level_diagnostic(
    body: dict[str, object],
    key: str,
) -> dict[str, object]:
    """Retain only presence, JSON type and direct container cardinality."""

    if key not in body:
        return {
            "presence": "missing",
            "json_type": None,
            "direct_child_count": None,
        }
    value = body[key]
    if value is None:
        return {
            "presence": "present_null",
            "json_type": "null",
            "direct_child_count": None,
        }
    direct_child_count = len(value) if isinstance(value, (dict, list)) else None
    return {
        "presence": "present_non_null",
        "json_type": _json_type_name(value),
        "direct_child_count": direct_child_count,
    }


async def _probe(
    client: Any,
    label: str,
    instruction: str,
    max_output_tokens: int,
    progress: ProbeProgress,
) -> dict[str, object]:
    raw = await client.responses.with_raw_response.create(
        model=MODEL,
        input=instruction,
        max_output_tokens=max_output_tokens,
        stream=False,
    )
    progress.active_raw_response_cleanup_state = "pending"
    primary_error: BaseException | None = None
    record: dict[str, object] | None = None
    http_status: object = None
    request_id: object = None
    try:
        http_status = raw.status_code
        request_id = raw.headers.get("x-request-id")
        body = _load_raw_body(raw.content)
        try:
            record = {
                "probe_label": label,
                "requested_max_output_tokens": max_output_tokens,
                "http_status": http_status,
                "provider_request_id_sha256": _sha256_request_id(request_id),
                "provider_request_id_header_present": request_id is not None,
                "provider_request_id_hash_withheld": (
                    request_id is not None and _sha256_request_id(request_id) is None
                ),
                "top_level_keys_observed": _top_level_keys(body),
                "top_level_presence": {
                    key: _presence(body, key) for key in PRESENCE_KEYS
                },
                "shape_only_top_level_observations": {
                    key: _shape_only_top_level_diagnostic(body, key)
                    for key in SHAPE_ONLY_TOP_LEVEL_KEYS
                },
                "incomplete_details_reason_presence": (
                    _incomplete_reason_presence(body)
                ),
                "incomplete_details_keys_observed": _nested_keys(
                    body.get("incomplete_details")
                ),
                "usage_keys_observed": _nested_keys(body.get("usage")),
                "response_projection": _project(body),
                "output_container": _output_container_diagnostic(body),
                "output_item_shapes": _output_item_shapes(body),
                "raw_response_cleanup_state": "pending",
            }
            progress.retain_current(record)
        finally:
            body.clear()
    except ProbeError as exc:
        primary_error = exc
        raise ProbeResponseProcessingError(
            exc.code,
            http_status=http_status,
            request_id=request_id,
        ) from exc
    except Exception as exc:
        primary_error = exc
        raise ProbeResponseProcessingError(
            "deepseek_responses_response_processing_failed",
            http_status=http_status,
            request_id=request_id,
        ) from exc
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                await raw.http_response.aclose()
            if record is not None:
                record["raw_response_cleanup_state"] = "succeeded"
            progress.active_raw_response_cleanup_state = "succeeded"
        except asyncio.CancelledError:
            if record is not None:
                record["raw_response_cleanup_state"] = "cancelled"
            progress.active_raw_response_cleanup_state = "cancelled"
            raise
        except Exception as exc:
            if record is not None:
                record["raw_response_cleanup_state"] = "failed"
            progress.active_raw_response_cleanup_state = "failed"
            if primary_error is None:
                if record is None:
                    raise ProbeError("deepseek_responses_raw_close_failed") from exc
                raise ProbeRawCleanupError(
                    record,
                    http_status=http_status,
                    request_id=request_id,
                ) from exc
    if record is None:
        raise ProbeError("deepseek_responses_projection_missing")
    return record


def _boundary() -> dict[str, object]:
    return {
        "is_evaluation_task": False,
        "enters_run_ledger": False,
        "produces_task_pass": False,
        "authorizes_prompt_or_scorer_change": False,
        "supports_model_quality_claim": False,
        "authorizes_provider_registration": False,
        "response_body_persisted": False,
        "raw_nested_objects_persisted": False,
        "input_content_persisted": False,
        "provider_side_retention_unverified": True,
        "diagnostic_key_names_and_item_shapes_persisted": True,
        "shape_only_top_level_values_persisted": False,
        "shape_only_top_level_presence_type_and_count_persisted": True,
        "raw_response_cleanup_state_persisted_when_record_retained": True,
    }


def _transport() -> dict[str, object]:
    return {
        "base_url": BASE_URL,
        "path": PATH,
        "model": MODEL,
        "openai_sdk_version": EXPECTED_OPENAI_SDK_VERSION,
        "stream": False,
        "max_retries": 0,
        "concurrency": 1,
        "resume": False,
        "fallback": False,
        "network_attempts_max": 3,
        "model_requests_max": 3,
        "request_timeout_seconds": TIMEOUT_SECONDS,
        "request_phase_total_timeout_seconds": TOTAL_TIMEOUT_SECONDS,
        "request_phase_total_timeout_preempts_per_request_timeouts": True,
        "cleanup_timeout_seconds_per_resource": CLOSE_TIMEOUT_SECONDS,
        "maximum_raw_response_cleanup_resources": 3,
        "raw_response_cleanup_included_in_request_phase_total_timeout": True,
        "maximum_post_request_cleanup_resources": 2,
        "request_phase_plus_post_request_cleanup_timeout_upper_bound_seconds": (
            TOTAL_TIMEOUT_SECONDS + (2 * CLOSE_TIMEOUT_SECONDS)
        ),
        "setup_time_bounded": False,
        "whole_process_wall_timeout_claimed": False,
        "trust_environment": False,
        "follow_redirects": False,
        "kwargs_source": "minimal_direct_probe_not_sdk_built",
        "requested_max_output_tokens_sum_max": (
            NORMAL_MAX_OUTPUT_TOKENS
            + FORCED_CAP_MAX_OUTPUT_TOKENS
            + MESSAGE_STAGE_MAX_OUTPUT_TOKENS
        ),
    }


def _forced_cap_retained_status(probes: list[dict[str, object]]) -> str | None:
    """Read back the ``status`` this receipt already publishes for the cap probe.

    This is a verbatim read-back of a value the projection already retained. It does
    not map, normalize, or decide whether truncation occurred; only the mapper may
    do that. ``None`` means the forced-cap attempt produced no retained status.
    """
    for record in probes:
        if not isinstance(record, dict):
            continue
        if record.get("probe_label") != "responses_output_cap_attempt":
            continue
        projection = record.get("response_projection")
        if not isinstance(projection, dict):
            return None
        status = projection.get("status")
        return status if isinstance(status, str) else None
    return None


def _forced_cap_attempt_completed(probes: list[dict[str, object]]) -> bool:
    """Return whether a sanitized forced-cap response record was retained."""
    return any(
        isinstance(record, dict)
        and record.get("probe_label") == "responses_output_cap_attempt"
        for record in probes
    )


def _message_stage_observations(probes: list[dict[str, object]]) -> dict[str, bool]:
    """Read back exact sanitized message-stage signals without mapping them."""

    record = next(
        (
            candidate
            for candidate in probes
            if isinstance(candidate, dict)
            and candidate.get("probe_label") == MESSAGE_STAGE_PROBE_LABEL
        ),
        None,
    )
    if record is None:
        return {
            "message_stage_cap_attempt_completed": False,
            "message_output_item_observed": False,
            "message_output_item_with_incomplete_status_observed": False,
            "top_level_incomplete_max_output_tokens_observed": False,
            "message_stage_cap_target_observed": False,
        }
    shapes = record.get("output_item_shapes")
    shape_list = shapes if isinstance(shapes, list) else []
    message_item_observed = any(
        isinstance(shape, dict) and shape.get("type") == "message"
        for shape in shape_list
    )
    incomplete_message_observed = any(
        isinstance(shape, dict)
        and shape.get("type") == "message"
        and shape.get("status") == "incomplete"
        for shape in shape_list
    )
    projection = record.get("response_projection")
    projected = projection if isinstance(projection, dict) else {}
    incomplete_details = projected.get("incomplete_details")
    details = incomplete_details if isinstance(incomplete_details, dict) else {}
    top_level_cap_observed = (
        projected.get("status") == "incomplete"
        and details.get("reason") == "max_output_tokens"
    )
    return {
        "message_stage_cap_attempt_completed": True,
        "message_output_item_observed": message_item_observed,
        "message_output_item_with_incomplete_status_observed": (
            incomplete_message_observed
        ),
        "top_level_incomplete_max_output_tokens_observed": top_level_cap_observed,
        "message_stage_cap_target_observed": (
            incomplete_message_observed and top_level_cap_observed
        ),
    }


def _distinct_shape_key(record: dict[str, object]) -> tuple[object, ...] | None:
    projection = record.get("response_projection")
    output_shapes = record.get("output_item_shapes")
    top_level_presence = record.get("top_level_presence")
    reason_presence = record.get("incomplete_details_reason_presence")
    if (
        not isinstance(projection, dict)
        or not isinstance(output_shapes, list)
        or not isinstance(top_level_presence, dict)
        or not isinstance(reason_presence, str)
    ):
        return None
    status_value = projection.get("status")
    if status_value is not None and not isinstance(status_value, str):
        return None
    item_sequence: list[tuple[tuple[bool, object], tuple[bool, object]]] = []
    for shape in output_shapes:
        if not isinstance(shape, dict):
            return None
        type_value = shape.get("type")
        item_status_value = shape.get("status")
        if type_value is not None and not isinstance(type_value, str):
            return None
        if item_status_value is not None and not isinstance(
            item_status_value, str
        ):
            return None
        item_sequence.append(
            (
                ("type" in shape, type_value),
                ("status" in shape, item_status_value),
            )
        )
    presence_items: list[tuple[str, object]] = []
    for key, value in sorted(top_level_presence.items()):
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        presence_items.append((key, value))
    return (
        ("status" in projection, status_value),
        reason_presence,
        tuple(item_sequence),
        tuple(presence_items),
    )


def _shape_counts(probes: list[dict[str, object]]) -> dict[str, object]:
    observed_response_count = 0
    shape_keys: set[tuple[object, ...]] = set()
    unavailable_count = 0
    for record in probes:
        if not isinstance(record, dict):
            continue
        observed_response_count += 1
        key = _distinct_shape_key(record)
        if key is None:
            unavailable_count += 1
        else:
            shape_keys.add(key)
    eligible_count = observed_response_count - unavailable_count
    return {
        "observed_response_count": observed_response_count,
        "observed_response_count_definition": (
            "sanitized_response_shape_records_present_in_this_receipt"
        ),
        "distinct_top_level_shape_count": len(shape_keys),
        "distinct_top_level_shape_eligible_response_count": eligible_count,
        "distinct_top_level_shape_unavailable_response_count": unavailable_count,
        "distinct_shape_definition": DISTINCT_SHAPE_DEFINITION,
    }


def _limitations(probes: list[dict[str, object]]) -> dict[str, object]:
    forced_cap_status = _forced_cap_retained_status(probes)
    limitations: dict[str, object] = {
        "forced_cap_minimum_output_tokens": FORCED_CAP_MAX_OUTPUT_TOKENS,
        "forced_cap_attempt_completed": _forced_cap_attempt_completed(probes),
        "forced_cap_attempt_retained_status": forced_cap_status,
        "forced_cap_not_observed_interpretation_scope": (
            "applies_only_when_no_truncation_was_observed"
        ),
        "forced_cap_not_observed_interpretation": (
            "not_triggered_not_evidence_of_nonexistence"
        ),
        "adapter_kwargs_equivalence_claimed": False,
        "sdk_built_kwargs_offline_diff_performed": False,
        "known_but_unobserved_values_remain_unmapped": True,
        "supersedes_probe_receipt_sha256": PREDECESSOR_RECEIPT_SHA256,
        "superseded_field": "observed_shape_count_max",
        "superseded_field_reason": "counted_requests_not_distinct_shapes",
        "message_stage_probe_max_output_tokens": MESSAGE_STAGE_MAX_OUTPUT_TOKENS,
        "message_stage_probe_cap_is_fixed_non_adaptive": True,
        "message_stage_entry_guaranteed": False,
        "message_stage_requested_word_repetitions": 500,
        "message_stage_final_planned_attempt": True,
        "no_further_message_stage_probe_if_target_not_observed": True,
        "input_token_hard_cap_claimed": False,
        "message_stage_cap_not_observed_interpretation_scope": (
            "applies_only_when_message_stage_cap_target_observed_is_false"
        ),
        "message_stage_cap_not_observed_interpretation": (
            "not_triggered_not_evidence_of_nonexistence"
        ),
    }
    limitations.update(_shape_counts(probes))
    limitations.update(_message_stage_observations(probes))
    return limitations


def _receipt(probes: list[dict[str, object]]) -> dict[str, object]:
    return {
        "probe_id": "deepseek_responses_completion_shape_v3",
        "status": "completed",
        "boundary": _boundary(),
        "transport": _transport(),
        "limitations": _limitations(probes),
        "probes": probes,
    }


def _run_error(
    exc: Exception,
    *,
    probe_label: str,
    client_dispatches: int,
    completed_probes: list[dict[str, object]],
    raw_response_cleanup_state: str | None = None,
) -> ProbeRunError:
    status = getattr(exc, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        status = None
    request_id = getattr(exc, "request_id", None)
    sdk_timeout = any(
        cls.__name__ == "APITimeoutError" and cls.__module__.startswith("openai")
        for cls in type(exc).__mro__
    )
    if isinstance(exc, ProbeError):
        code = exc.code
    elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or sdk_timeout:
        code = "deepseek_responses_request_timeout"
    elif status is not None:
        code = "deepseek_responses_http_error"
    else:
        code = "deepseek_responses_request_failed"
    retained_probes = list(completed_probes)
    if isinstance(exc, ProbeRawCleanupError):
        retained_probes.append(exc.completed_probe)
    return ProbeRunError(
        code,
        probe_label=probe_label,
        client_dispatches=client_dispatches,
        http_status=status,
        request_id=request_id,
        completed_probes=retained_probes,
        raw_response_cleanup_state=raw_response_cleanup_state,
    )


def _probe_run_outcome_unknown(exc: ProbeRunError) -> bool:
    return (
        exc.code
        in {
            "deepseek_responses_request_timeout",
            "deepseek_responses_request_failed",
        }
        or exc.http_status == 408
        or (exc.http_status is not None and exc.http_status >= 500)
    )


async def run_probes(
    client: Any,
    completed_probes: list[dict[str, object]] | None = None,
    progress: ProbeProgress | None = None,
) -> dict[str, object]:
    completed = [] if completed_probes is None else completed_probes
    run_progress = ProbeProgress() if progress is None else progress
    run_progress.begin("responses_normal_completion")
    try:
        normal = await _probe(
            client,
            "responses_normal_completion",
            "Reply with the single word: ok",
            NORMAL_MAX_OUTPUT_TOKENS,
            run_progress,
        )
    except Exception as exc:
        raise _run_error(
            exc,
            probe_label="responses_normal_completion",
            client_dispatches=1,
            completed_probes=completed,
            raw_response_cleanup_state=(
                run_progress.active_raw_response_cleanup_state
            ),
        ) from None
    completed.append(normal)
    run_progress.finish()
    run_progress.begin("responses_output_cap_attempt")
    try:
        forced_cap = await _probe(
            client,
            "responses_output_cap_attempt",
            "Count upward from one, one number per line, without stopping.",
            FORCED_CAP_MAX_OUTPUT_TOKENS,
            run_progress,
        )
    except Exception as exc:
        raise _run_error(
            exc,
            probe_label="responses_output_cap_attempt",
            client_dispatches=2,
            completed_probes=completed,
            raw_response_cleanup_state=(
                run_progress.active_raw_response_cleanup_state
            ),
        ) from None
    completed.append(forced_cap)
    run_progress.finish()
    run_progress.begin(MESSAGE_STAGE_PROBE_LABEL)
    try:
        message_stage = await _probe(
            client,
            MESSAGE_STAGE_PROBE_LABEL,
            MESSAGE_STAGE_INSTRUCTION,
            MESSAGE_STAGE_MAX_OUTPUT_TOKENS,
            run_progress,
        )
    except Exception as exc:
        raise _run_error(
            exc,
            probe_label=MESSAGE_STAGE_PROBE_LABEL,
            client_dispatches=3,
            completed_probes=completed,
            raw_response_cleanup_state=(
                run_progress.active_raw_response_cleanup_state
            ),
        ) from None
    completed.append(message_stage)
    run_progress.finish()
    return _receipt(list(completed))


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _failure_receipt(
    error_code: str,
    *,
    outcome_unknown: bool,
    **details: object,
) -> dict[str, object]:
    if {"boundary", "transport", "limitations"} & set(details):
        raise ProbeError("deepseek_responses_failure_guardrail_override")
    retained = details.get("retained_probe_records")
    value: dict[str, object] = {
        "probe_id": "deepseek_responses_completion_shape_v3",
        "status": "failed",
        "error_code": error_code,
        "outcome_unknown": outcome_unknown,
        "error_body_persisted": False,
        "exception_text_persisted": False,
        "retained_probe_record_definition": (
            "sanitized_record_built_not_full_probe_lifecycle_completed"
        ),
        "boundary": _boundary(),
        "transport": _transport(),
        "limitations": _limitations(retained if isinstance(retained, list) else []),
    }
    value.update(details)
    return value


async def _bounded_close(target: object, method_name: str) -> bool:
    method = getattr(target, method_name, None)
    if not callable(method):
        return False
    try:
        async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
            await method()
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
    return True


async def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments != ["--confirm-online"]:
        print("online confirmation is required", file=sys.stderr)
        return 2
    if not _environment_is_isolated():
        print("unsafe OpenAI environment is configured", file=sys.stderr)
        return 2
    if not _network_debug_logging_is_disabled():
        print("network debug logging must be disabled", file=sys.stderr)
        return 2
    if not _openai_sdk_version_is_exact():
        print("OpenAI SDK version must be exactly 3.1.0", file=sys.stderr)
        return 2

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("DEEPSEEK_API_KEY is not set", file=sys.stderr)
        return 2

    try:
        from openai import AsyncOpenAI, DefaultAsyncHttpxClient
    except Exception:
        api_key = ""
        print(
            _canonical_json(
                _failure_receipt(
                    "deepseek_responses_sdk_import_failed",
                    outcome_unknown=False,
                )
            )
        )
        return 1

    try:
        http_client = DefaultAsyncHttpxClient(
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        )
    except Exception:
        api_key = ""
        print(
            _canonical_json(
                _failure_receipt(
                    "deepseek_responses_http_client_construction_failed",
                    outcome_unknown=False,
                )
            )
        )
        return 1

    try:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=BASE_URL,
            timeout=TIMEOUT_SECONDS,
            max_retries=0,
            http_client=http_client,
        )
    except Exception:
        http_client_cleanup_succeeded = await _bounded_close(http_client, "aclose")
        api_key = ""
        print(
            _canonical_json(
                _failure_receipt(
                    "deepseek_responses_client_construction_failed",
                    outcome_unknown=False,
                    http_client_cleanup_succeeded=http_client_cleanup_succeeded,
                )
            )
        )
        return 1

    output: dict[str, object]
    exit_code = 1
    completed_probes: list[dict[str, object]] = []
    progress = ProbeProgress()
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
            output = await run_probes(client, completed_probes, progress)
        exit_code = 0
    except TimeoutError:
        timeout_records = progress.retained_records(completed_probes)
        output = _failure_receipt(
            "deepseek_responses_total_timeout",
            outcome_unknown=True,
            client_dispatches=progress.client_dispatches,
            active_probe_label=progress.active_probe_label,
            active_raw_response_cleanup_state=(
                progress.active_raw_response_cleanup_state
            ),
            retained_probe_record_count=len(timeout_records),
            retained_probe_records=timeout_records,
        )
    except ProbeRunError as exc:
        output = _failure_receipt(
            exc.code,
            outcome_unknown=_probe_run_outcome_unknown(exc),
            failed_probe_label=exc.probe_label,
            client_dispatches=exc.client_dispatches,
            http_status=exc.http_status,
            provider_request_id_header_present=exc.request_id_present,
            provider_request_id_sha256=exc.request_id_sha256,
            provider_request_id_hash_withheld=exc.request_id_hash_withheld,
            active_raw_response_cleanup_state=exc.raw_response_cleanup_state,
            retained_probe_record_count=len(exc.completed_probes),
            retained_probe_records=list(exc.completed_probes),
        )
    except ProbeError as exc:
        output = _failure_receipt(exc.code, outcome_unknown=False)
    except asyncio.CancelledError:
        raise
    except Exception:
        output = _failure_receipt(
            "deepseek_responses_probe_failed",
            outcome_unknown=True,
        )
    finally:
        client_cleanup_succeeded = await _bounded_close(client, "close")
        http_cleanup_succeeded = await _bounded_close(http_client, "aclose")
        api_key = ""

    post_request_cleanup_succeeded = (
        client_cleanup_succeeded and http_cleanup_succeeded
    )
    if not post_request_cleanup_succeeded:
        if exit_code == 0:
            successful_probes = output.get("probes")
            if not isinstance(successful_probes, list):
                successful_probes = []
            output = _failure_receipt(
                "deepseek_responses_client_close_failed",
                outcome_unknown=False,
                post_request_cleanup_succeeded=False,
                retained_probe_record_count=len(successful_probes),
                retained_probe_records=successful_probes,
            )
            exit_code = 1
        else:
            output["post_request_cleanup_succeeded"] = False
    else:
        output["post_request_cleanup_succeeded"] = True

    print(_canonical_json(output))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
