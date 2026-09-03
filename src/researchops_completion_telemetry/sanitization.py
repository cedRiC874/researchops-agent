from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .surface_mapping import VerifiedRuntimeCompletionBinding


JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
CompletionMappingResult = tuple[str, str, JsonValue, str]
MappingResolver = Callable[
    [Mapping[str, Any], str, str, str], CompletionMappingResult
]


TELEMETRY_SCHEMA_VERSION = "provider-completion-record/1.0"
ARTIFACT_SCHEMA_VERSION = "provider-completion-telemetry-artifact/1.0"
TRUNCATION_MARKER = "[TRUNCATED]"
TOKEN_CAP_FALLBACK_RULE_ID = "runtime-token-cap-fallback-v1"

_RAW_CAPTURE_FIELDS = frozenset(
    {
        "status",
        "finish_reason",
        "stop_reason",
        "stop_sequence",
        "incomplete_details",
        "usage",
        "provider_request_id",
        "provider_request_id_sha256",
        "http_status",
        "requested_output_token_cap",
    }
)
_MAPPING_PROJECTION_FIELDS = frozenset(
    {"status", "finish_reason", "stop_reason", "stop_sequence", "incomplete_details"}
)
_CAPTURE_COMPONENT_FIELDS = frozenset(
    {
        "native_status",
        "native_finish_reason",
        "native_stop_reason",
        "native_stop_sequence",
        "native_incomplete_details",
        "provider_request_id_sha256",
        "http_status",
        "usage",
        "output_token_cap",
    }
)
_RECORD_FIELDS = frozenset(
    {
        "telemetry_schema_version",
        "telemetry_schema_sha256",
        "record_provenance",
        "adapter_version",
        "mapping_schema_version",
        "mapping_version",
        "mapping_sha256",
        "provider_id",
        "api_surface",
        "transport_id",
        "response_index",
        "request_index",
        "native_status",
        "native_finish_reason",
        "native_stop_reason",
        "native_stop_sequence",
        "native_incomplete_details",
        "normalized_completion_state",
        "truncation_signal_source",
        "matched_rule_id",
        "provider_request_id_sha256",
        "http_status",
        "usage",
        "output_token_cap",
        "output_counter_comparability",
        "output_counter_path",
    }
)
_SCALAR_CAPTURE_FIELDS = frozenset(
    {"availability", "value", "redaction_applied", "truncated"}
)
_DETAIL_CAPTURE_FIELDS = frozenset(
    {
        "availability",
        "value",
        "redaction_applied",
        "truncated",
        "omitted_child_field_count",
    }
)
_AVAILABILITY_VALUE_FIELDS = frozenset({"availability", "value"})
_USAGE_FIELDS = frozenset(
    {
        "availability",
        "native_value_state",
        "complete",
        "normalized",
        "native_numeric_counters",
        "omitted_non_numeric_field_count",
        "truncated",
    }
)
_NORMALIZED_USAGE_FIELDS = (
    "requests",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
_NORMALIZED_USAGE_FIELD_SET = frozenset(_NORMALIZED_USAGE_FIELDS)
_COUNTER_FIELDS = frozenset({"path", "value"})
_AVAILABILITIES = frozenset({"provided", "not_provided", "not_persisted"})
_STATES = frozenset(
    {
        "completed",
        "incomplete_length",
        "incomplete_content_filter",
        "incomplete_other",
        "error",
        "unmapped",
        "not_provided",
        "not_persisted",
    }
)
_RECOGNIZED_TERMINAL_STATES = frozenset(
    {
        "completed",
        "incomplete_length",
        "incomplete_content_filter",
        "incomplete_other",
        "error",
    }
)
_SOURCES = frozenset({"native_status", "token_cap_fallback", "none"})
_COMPARABILITIES = frozenset(
    {"comparable", "not_comparable", "not_provided", "not_persisted"}
)

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
_SAFE_RULE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_COUNTER_SEGMENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_COUNTER_PATH = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){0,3}$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_PREFIX = re.compile(
    r"(?:\bsk[-_][A-Za-z0-9_-]{8,}\b|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
_AUTH_OR_COOKIE = re.compile(
    r"\b(?:proxy-)?authorization\s*:\s*(?:bearer|basic)\s+\S+|"
    r"\b(?:set-)?cookie\s*:\s*\S+",
    re.IGNORECASE,
)
_SENSITIVE_KEY = re.compile(
    r"^(?:api[_-]?key|authorization|proxy[_-]?authorization|cookie|set[_-]?cookie|"
    r"secret|password|credential|access[_-]?token|refresh[_-]?token|traceback)$",
    re.IGNORECASE,
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:[A-Z]:[\\/]|\\\\)[^\r\n\t\"']+")
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![\w/])/(?:[A-Za-z0-9._~-]+/)*[A-Za-z0-9._~-]+"
)
_TRACEBACK = re.compile(
    r"Traceback\s*\(most recent call last\)|"
    r"\bFile\s+[\"'][^\"']+[\"']\s*,\s*line\s+\d+|"
    r"\b__traceback__\b",
    re.IGNORECASE,
)
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)

_MAX_NATIVE_SCALAR_BYTES = 64
_MAX_STOP_SEQUENCE_BYTES = 64
_MAX_DETAILS_BYTES = 512
_MAX_DETAILS_REASON_BYTES = 64
_MAX_USAGE_BYTES = 4096
_MAX_USAGE_LEAVES = 64
_MAX_USAGE_DEPTH = 4
_MAX_COUNTER_PATH_BYTES = 255
_MAX_RECORD_BYTES = 16384
_MAX_RESPONSE_INDEX = 2_147_483_647
_MAX_SCAN_DEPTH = 16

MISSING = object()


class CompletionTelemetryError(ValueError):
    """Fail-closed error carrying only a stable, non-sensitive code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code}


@dataclass(frozen=True, slots=True)
class OfflineCompletionRecordBinding:
    telemetry_schema_sha256: str
    adapter_version: str
    mapping_schema_version: str
    mapping_version: str
    mapping_sha256: str
    provider_id: str
    api_surface: str
    transport_id: str


_CAPTURE_CONSTRUCTION_TOKEN = object()


class SanitizedCompletionCapture:
    """Opaque, defensively copied output of the write-time sanitizer."""

    __slots__ = (
        "_canaries",
        "_historical",
        "_locked",
        "_mapping_projection_bytes",
        "_record_components_bytes",
    )

    def __init__(
        self,
        *,
        _token: object,
        mapping_projection: Mapping[str, Any],
        record_components: Mapping[str, Any],
        historical: bool,
        canaries: tuple[str, ...],
    ) -> None:
        if _token is not _CAPTURE_CONSTRUCTION_TOKEN:
            raise _error("completion_telemetry_capture_construction_forbidden")
        object.__setattr__(
            self,
            "_mapping_projection_bytes",
            _canonical_json_bytes(dict(mapping_projection)),
        )
        object.__setattr__(
            self,
            "_record_components_bytes",
            _canonical_json_bytes(dict(record_components)),
        )
        object.__setattr__(self, "_historical", historical)
        object.__setattr__(self, "_canaries", tuple(canaries))
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("SanitizedCompletionCapture is immutable")
        object.__setattr__(self, name, value)

    @property
    def historical(self) -> bool:
        return self._historical

    def mapping_projection(self) -> dict[str, Any]:
        value = json.loads(self._mapping_projection_bytes)
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise _error("completion_telemetry_capture_projection_invalid")
        return value

    def record_components(self) -> dict[str, Any]:
        value = json.loads(self._record_components_bytes)
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise _error("completion_telemetry_capture_components_invalid")
        return value

    def _sensitive_canaries(self) -> tuple[str, ...]:
        return self._canaries

    def _collector_snapshot(self) -> SanitizedCompletionCapture:
        """Return an independent immutable capture for collector ownership."""

        return SanitizedCompletionCapture(
            _token=_CAPTURE_CONSTRUCTION_TOKEN,
            mapping_projection=self.mapping_projection(),
            record_components=self.record_components(),
            historical=self._historical,
            canaries=self._canaries,
        )


def _error(code: str) -> CompletionTelemetryError:
    return CompletionTelemetryError(code)


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _error("completion_telemetry_json_invalid") from exc


def _normalize_string(value: str) -> str:
    try:
        normalized = unicodedata.normalize("NFC", value)
        normalized.encode("utf-8")
    except (TypeError, UnicodeEncodeError) as exc:
        raise _error("completion_telemetry_string_invalid") from exc
    if any(
        unicodedata.category(character) == "Cc" and character not in "\t\n\r"
        for character in normalized
    ):
        raise _error("completion_telemetry_string_invalid")
    return normalized


def _normalized_canaries(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray, Mapping)):
        raise _error("completion_telemetry_canary_configuration_invalid")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise _error("completion_telemetry_canary_configuration_invalid")
        normalized = _normalize_string(value)
        if len(normalized.encode("utf-8")) > 512:
            raise _error("completion_telemetry_canary_configuration_invalid")
        result.append(normalized)
    return tuple(result)


def _scan_string(value: str, canaries: tuple[str, ...]) -> str:
    normalized = _normalize_string(value)
    if (
        _SECRET_PREFIX.search(normalized)
        or _AUTH_OR_COOKIE.search(normalized)
        or _WINDOWS_ABSOLUTE_PATH.search(normalized)
        or _POSIX_ABSOLUTE_PATH.search(normalized)
        or _TRACEBACK.search(normalized)
        or _EMAIL.search(normalized)
        or any(canary in normalized for canary in canaries)
    ):
        raise _error("completion_telemetry_sensitive_value")
    return normalized


def _scan_json_value(
    value: object,
    canaries: tuple[str, ...],
    *,
    depth: int = 0,
) -> None:
    if depth > _MAX_SCAN_DEPTH:
        raise _error("completion_telemetry_scan_depth_exceeded")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _error("completion_telemetry_nonfinite_number")
        return
    if isinstance(value, str):
        _scan_string(value, canaries)
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise _error("completion_telemetry_object_key_invalid")
            normalized_key = _scan_string(key, canaries)
            if _SENSITIVE_KEY.fullmatch(normalized_key):
                raise _error("completion_telemetry_sensitive_field")
            _scan_json_value(child, canaries, depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _scan_json_value(child, canaries, depth=depth + 1)
        return
    raise _error("completion_telemetry_value_type_invalid")


def _validate_exact_fields(
    value: object,
    expected: frozenset[str],
    code: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise _error(code)
    if any(not isinstance(key, str) for key in value):
        raise _error(code)
    return value


def _safe_identifier(value: object, *, rule_id: bool = False) -> str:
    pattern = _SAFE_RULE_ID if rule_id else _SAFE_IDENTIFIER
    byte_limit = 128 if rule_id else 64
    if not isinstance(value, str):
        raise _error("completion_telemetry_identifier_invalid")
    normalized = _normalize_string(value)
    if (
        not pattern.fullmatch(normalized)
        or len(normalized.encode("utf-8")) > byte_limit
    ):
        raise _error("completion_telemetry_identifier_invalid")
    return normalized


def _nonnegative_integer(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _error(code)
    return value


def _index(value: object, *, nullable: bool) -> int | None:
    if value is None and nullable:
        return None
    result = _nonnegative_integer(value, "completion_telemetry_index_invalid")
    if result > _MAX_RESPONSE_INDEX:
        raise _error("completion_telemetry_index_invalid")
    return result


def _availability_value(
    raw_value: object,
    *,
    historical: bool,
    kind: str,
) -> dict[str, Any]:
    if historical:
        if raw_value is not MISSING:
            raise _error("completion_telemetry_historical_raw_value_forbidden")
        return {"availability": "not_persisted", "value": None}
    if raw_value is MISSING:
        return {"availability": "not_provided", "value": None}
    if raw_value is None:
        return {"availability": "provided", "value": None}
    if kind == "http_status":
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise _error("completion_telemetry_http_status_invalid")
        if not 100 <= raw_value <= 599:
            raise _error("completion_telemetry_http_status_invalid")
        return {"availability": "provided", "value": raw_value}
    if kind == "output_token_cap":
        return {
            "availability": "provided",
            "value": _nonnegative_integer(
                raw_value, "completion_telemetry_output_token_cap_invalid"
            ),
        }
    raise _error("completion_telemetry_internal_contract_error")


def _truncate_utf8(value: str, byte_limit: int) -> str:
    encoded = value.encode("utf-8")
    marker = TRUNCATION_MARKER.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value
    prefix = encoded[: byte_limit - len(marker)]
    while prefix:
        try:
            return prefix.decode("utf-8") + TRUNCATION_MARKER
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return TRUNCATION_MARKER


def _capture_scalar(
    raw_value: object,
    *,
    historical: bool,
    canaries: tuple[str, ...],
    truncatable: bool,
) -> tuple[dict[str, Any], object]:
    if historical:
        if raw_value is not MISSING:
            raise _error("completion_telemetry_historical_raw_value_forbidden")
        return (
            {
                "availability": "not_persisted",
                "value": None,
                "redaction_applied": False,
                "truncated": False,
            },
            MISSING,
        )
    if raw_value is MISSING:
        return (
            {
                "availability": "not_provided",
                "value": None,
                "redaction_applied": False,
                "truncated": False,
            },
            MISSING,
        )
    if raw_value is None:
        return (
            {
                "availability": "provided",
                "value": None,
                "redaction_applied": False,
                "truncated": False,
            },
            None,
        )
    if not isinstance(raw_value, str):
        raise _error("completion_telemetry_native_scalar_invalid")
    normalized = _scan_string(raw_value, canaries)
    if TRUNCATION_MARKER in normalized:
        raise _error("completion_telemetry_reserved_marker_spoofed")
    byte_limit = _MAX_STOP_SEQUENCE_BYTES if truncatable else _MAX_NATIVE_SCALAR_BYTES
    over_limit = len(normalized.encode("utf-8")) > byte_limit
    if over_limit and not truncatable:
        raise _error("completion_telemetry_mapping_critical_value_over_limit")
    persisted = _truncate_utf8(normalized, byte_limit) if over_limit else normalized
    capture = {
        "availability": "provided",
        "value": persisted,
        "redaction_applied": False,
        "truncated": over_limit,
    }
    return capture, persisted


def _capture_incomplete_details(
    raw_value: object,
    *,
    historical: bool,
    canaries: tuple[str, ...],
) -> tuple[dict[str, Any], object]:
    if historical:
        if raw_value is not MISSING:
            raise _error("completion_telemetry_historical_raw_value_forbidden")
        return (
            {
                "availability": "not_persisted",
                "value": None,
                "redaction_applied": False,
                "truncated": False,
                "omitted_child_field_count": 0,
            },
            MISSING,
        )
    if raw_value is MISSING:
        return (
            {
                "availability": "not_provided",
                "value": None,
                "redaction_applied": False,
                "truncated": False,
                "omitted_child_field_count": 0,
            },
            MISSING,
        )
    if raw_value is None:
        return (
            {
                "availability": "provided",
                "value": None,
                "redaction_applied": False,
                "truncated": False,
                "omitted_child_field_count": 0,
            },
            None,
        )
    if not isinstance(raw_value, Mapping):
        raise _error("completion_telemetry_incomplete_details_invalid")
    if any(not isinstance(key, str) for key in raw_value):
        raise _error("completion_telemetry_incomplete_details_invalid")
    if "_telemetry_truncated" in raw_value:
        raise _error("completion_telemetry_reserved_marker_spoofed")
    _scan_json_value(raw_value, canaries)
    raw_bytes = len(_canonical_json_bytes(raw_value))
    projected: dict[str, Any] = {}
    if "reason" in raw_value:
        reason = raw_value["reason"]
        if reason is None:
            projected["reason"] = None
        elif isinstance(reason, str):
            normalized_reason = _scan_string(reason, canaries)
            if len(normalized_reason.encode("utf-8")) > _MAX_DETAILS_REASON_BYTES:
                raise _error("completion_telemetry_mapping_critical_value_over_limit")
            projected["reason"] = normalized_reason
        else:
            raise _error("completion_telemetry_incomplete_reason_invalid")
    omitted = len(set(raw_value) - {"reason"})
    truncated = raw_bytes > _MAX_DETAILS_BYTES
    if truncated:
        projected["_telemetry_truncated"] = True
    if len(_canonical_json_bytes(projected)) > _MAX_DETAILS_BYTES:
        raise _error("completion_telemetry_incomplete_details_over_limit")
    return (
        {
            "availability": "provided",
            "value": projected,
            "redaction_applied": False,
            "truncated": truncated,
            "omitted_child_field_count": omitted,
        },
        projected,
    )


def _validate_normalized_usage(
    value: Mapping[str, Any] | None,
) -> dict[str, int | None]:
    if value is None:
        return {field: None for field in _NORMALIZED_USAGE_FIELDS}
    if not isinstance(value, Mapping) or set(value) != _NORMALIZED_USAGE_FIELD_SET:
        raise _error("completion_telemetry_normalized_usage_invalid")
    result: dict[str, int | None] = {}
    for field in _NORMALIZED_USAGE_FIELDS:
        item = value[field]
        if item is None:
            result[field] = None
        else:
            result[field] = _nonnegative_integer(
                item, "completion_telemetry_normalized_usage_invalid"
            )
    if (
        result["input_tokens"] is not None
        and result["output_tokens"] is not None
        and result["total_tokens"] is not None
        and result["total_tokens"]
        != result["input_tokens"] + result["output_tokens"]
    ):
        raise _error("completion_telemetry_usage_total_inconsistent")
    return result


def _flatten_usage(
    raw_usage: Mapping[str, Any],
    canaries: tuple[str, ...],
) -> tuple[list[dict[str, Any]], int, bool]:
    _scan_json_value(raw_usage, canaries)
    counters: list[dict[str, Any]] = []
    omitted_non_numeric = 0
    truncated = False

    def visit(value: Mapping[str, Any], path: tuple[str, ...]) -> None:
        nonlocal omitted_non_numeric, truncated
        for raw_key in sorted(value):
            if not isinstance(raw_key, str):
                raise _error("completion_telemetry_usage_key_invalid")
            key = _scan_string(raw_key, canaries)
            if not _COUNTER_SEGMENT.fullmatch(key):
                raise _error("completion_telemetry_usage_key_invalid")
            child_path = (*path, key)
            dotted = ".".join(child_path)
            if len(dotted.encode("utf-8")) > _MAX_COUNTER_PATH_BYTES:
                raise _error("completion_telemetry_usage_counter_path_invalid")
            child = value[raw_key]
            if isinstance(child, Mapping):
                if len(child_path) >= _MAX_USAGE_DEPTH:
                    _scan_json_value(child, canaries)
                    truncated = True
                else:
                    visit(child, child_path)
                continue
            if child is None:
                counters.append({"path": dotted, "value": None})
                continue
            if isinstance(child, bool):
                raise _error("completion_telemetry_usage_boolean_invalid")
            if isinstance(child, int):
                if child < 0:
                    raise _error("completion_telemetry_usage_counter_invalid")
                counters.append({"path": dotted, "value": child})
                continue
            if isinstance(child, float):
                if not math.isfinite(child):
                    raise _error("completion_telemetry_nonfinite_number")
                raise _error("completion_telemetry_usage_counter_invalid")
            omitted_non_numeric += 1

    visit(raw_usage, ())
    counters.sort(key=lambda item: item["path"])
    if len(counters) > _MAX_USAGE_LEAVES:
        counters = counters[:_MAX_USAGE_LEAVES]
        truncated = True
    return counters, omitted_non_numeric, truncated


def _capture_usage(
    raw_value: object,
    *,
    historical: bool,
    normalized_usage: Mapping[str, Any] | None,
    canaries: tuple[str, ...],
) -> tuple[dict[str, Any], object]:
    null_normalized = {field: None for field in _NORMALIZED_USAGE_FIELDS}
    if historical:
        if raw_value is not MISSING or normalized_usage is not None:
            raise _error("completion_telemetry_historical_raw_value_forbidden")
        return (
            {
                "availability": "not_persisted",
                "native_value_state": "not_persisted",
                "complete": False,
                "normalized": null_normalized,
                "native_numeric_counters": [],
                "omitted_non_numeric_field_count": 0,
                "truncated": False,
            },
            MISSING,
        )
    if raw_value is MISSING:
        if normalized_usage is not None:
            raise _error("completion_telemetry_usage_missing_with_normalized_values")
        return (
            {
                "availability": "not_provided",
                "native_value_state": "not_provided",
                "complete": False,
                "normalized": null_normalized,
                "native_numeric_counters": [],
                "omitted_non_numeric_field_count": 0,
                "truncated": False,
            },
            MISSING,
        )
    if raw_value is None:
        if normalized_usage is not None:
            raise _error("completion_telemetry_usage_null_with_normalized_values")
        return (
            {
                "availability": "provided",
                "native_value_state": "null",
                "complete": False,
                "normalized": null_normalized,
                "native_numeric_counters": [],
                "omitted_non_numeric_field_count": 0,
                "truncated": False,
            },
            None,
        )
    if not isinstance(raw_value, Mapping):
        raise _error("completion_telemetry_usage_invalid")
    normalized = _validate_normalized_usage(normalized_usage)
    counters, omitted_non_numeric, truncated = _flatten_usage(raw_value, canaries)
    core = (
        normalized["requests"],
        normalized["input_tokens"],
        normalized["output_tokens"],
        normalized["total_tokens"],
    )
    complete = (
        all(item is not None for item in core)
        and normalized["requests"] is not None
        and normalized["requests"] >= 1
        and omitted_non_numeric == 0
        and not truncated
    )
    capture: dict[str, Any] = {
        "availability": "provided",
        "native_value_state": "object",
        "complete": complete,
        "normalized": normalized,
        "native_numeric_counters": counters,
        "omitted_non_numeric_field_count": omitted_non_numeric,
        "truncated": truncated,
    }
    while len(_canonical_json_bytes(capture)) > _MAX_USAGE_BYTES and counters:
        counters.pop()
        capture["truncated"] = True
        capture["complete"] = False
    if len(_canonical_json_bytes(capture)) > _MAX_USAGE_BYTES:
        raise _error("completion_telemetry_usage_over_limit")
    projection: dict[str, Any] = {}
    for item in counters:
        target = projection
        parts = item["path"].split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = item["value"]
    return capture, projection


def _capture_request_id(
    raw_value: object,
    *,
    historical: bool,
    canaries: tuple[str, ...],
) -> dict[str, Any]:
    if historical:
        if raw_value is not MISSING:
            raise _error("completion_telemetry_historical_raw_value_forbidden")
        return {"availability": "not_persisted", "value": None}
    if raw_value is MISSING:
        return {"availability": "not_provided", "value": None}
    if raw_value is None:
        return {"availability": "provided", "value": None}
    if not isinstance(raw_value, str):
        raise _error("completion_telemetry_provider_request_id_invalid")
    normalized = _scan_string(raw_value, canaries)
    if not _SAFE_REQUEST_ID.fullmatch(normalized):
        raise _error("completion_telemetry_provider_request_id_invalid")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return {"availability": "provided", "value": digest}


def _capture_hashed_request_id(
    raw_value: object,
    *,
    historical: bool,
) -> dict[str, Any]:
    if historical:
        if raw_value is not MISSING:
            raise _error("completion_telemetry_historical_raw_value_forbidden")
        return {"availability": "not_persisted", "value": None}
    if raw_value is MISSING:
        return {"availability": "not_provided", "value": None}
    if raw_value is None:
        return {"availability": "provided", "value": None}
    if not isinstance(raw_value, str) or not _SHA256.fullmatch(raw_value):
        raise _error("completion_telemetry_provider_request_id_hash_invalid")
    return {"availability": "provided", "value": raw_value}


def hash_provider_request_id(
    value: str,
    *,
    sensitive_canaries: Iterable[str] = (),
) -> str:
    """Validate and hash a raw Provider request ID without retaining it."""

    canaries = _normalized_canaries(sensitive_canaries)
    capture = _capture_request_id(
        value,
        historical=False,
        canaries=canaries,
    )
    digest = capture["value"]
    if not isinstance(digest, str):  # pragma: no cover - value is required above
        raise _error("completion_telemetry_provider_request_id_invalid")
    return digest


def _projection_value(capture: Mapping[str, Any]) -> object:
    if capture["availability"] == "provided":
        return capture["value"]
    return MISSING


def _mapping_projection_from_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct only completion-native discriminators for the mapper."""

    projection: dict[str, Any] = {}
    for native_name, projection_name in (
        ("native_status", "status"),
        ("native_finish_reason", "finish_reason"),
        ("native_stop_reason", "stop_reason"),
        ("native_stop_sequence", "stop_sequence"),
        ("native_incomplete_details", "incomplete_details"),
    ):
        capture = record[native_name]
        value = _projection_value(capture)
        if value is not MISSING:
            projection[projection_name] = value
    return projection


def _validated_mapping_result(value: object) -> CompletionMappingResult:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) != 4
    ):
        raise _error("completion_telemetry_mapping_result_invalid")
    state, source, preserved, rule_id = value
    if (
        not isinstance(state, str)
        or not isinstance(source, str)
        or state not in _STATES
        or source not in _SOURCES
    ):
        raise _error("completion_telemetry_mapping_result_invalid")
    safe_rule_id = _safe_identifier(rule_id, rule_id=True)
    if preserved is not None:
        if not isinstance(preserved, str):
            raise _error("completion_telemetry_mapping_result_invalid")
        preserved = _scan_string(preserved, ())
        if len(preserved.encode("utf-8")) > _MAX_NATIVE_SCALAR_BYTES:
            raise _error("completion_telemetry_mapping_result_invalid")
    return str(state), str(source), preserved, safe_rule_id


def _resolve_offline_mapping(
    resolver: MappingResolver,
    projection: Mapping[str, Any],
    binding: OfflineCompletionRecordBinding,
) -> CompletionMappingResult:
    try:
        value = resolver(
            projection,
            binding.provider_id,
            binding.api_surface,
            binding.transport_id,
        )
    except CompletionTelemetryError:
        raise
    except Exception:
        raise _error("completion_telemetry_mapping_resolver_failed") from None
    result = _validated_mapping_result(value)
    if result[1] == "token_cap_fallback":
        raise _error("completion_telemetry_provider_mapping_fallback_forbidden")
    return result


def _resolve_runtime_mapping(
    binding: VerifiedRuntimeCompletionBinding,
    projection: Mapping[str, Any],
) -> CompletionMappingResult:
    try:
        binding.assert_runtime_authority()
        value = binding.resolve_mapping(projection)
    except CompletionTelemetryError:
        raise
    except Exception:
        raise _error("completion_telemetry_runtime_mapping_failed") from None
    result = _validated_mapping_result(value)
    if result[1] == "token_cap_fallback":
        raise _error("completion_telemetry_provider_mapping_fallback_forbidden")
    return result


def _effective_mapping_result(
    native_result: CompletionMappingResult,
    *,
    usage: Mapping[str, Any],
    output_cap: Mapping[str, Any],
    output_counter_comparability: str,
    output_counter_path: str | None,
) -> CompletionMappingResult:
    """Apply the only allowed token-cap fallback outside Provider mapping."""

    if native_result[0] != "not_provided" or native_result[1] != "none":
        return native_result
    if output_counter_comparability != "comparable" or output_counter_path is None:
        return native_result
    counters = {
        item["path"]: item["value"] for item in usage["native_numeric_counters"]
    }
    if (
        output_cap["availability"] == "provided"
        and output_cap["value"] is not None
        and counters.get(output_counter_path) == output_cap["value"]
    ):
        return (
            "incomplete_length",
            "token_cap_fallback",
            None,
            TOKEN_CAP_FALLBACK_RULE_ID,
        )
    return native_result


_BINDING_FIELDS = frozenset(
    {
        "telemetry_schema_sha256",
        "adapter_version",
        "mapping_schema_version",
        "mapping_version",
        "mapping_sha256",
        "provider_id",
        "api_surface",
        "transport_id",
        "output_counter_comparability",
        "output_counter_path",
    }
)


def _validate_binding_snapshot(value: object) -> dict[str, str]:
    snapshot = _validate_exact_fields(
        value, _BINDING_FIELDS, "completion_telemetry_binding_invalid"
    )
    for field in (
        "adapter_version",
        "mapping_schema_version",
        "mapping_version",
        "provider_id",
        "api_surface",
        "transport_id",
    ):
        _safe_identifier(snapshot[field])
    if (
        not isinstance(snapshot["telemetry_schema_sha256"], str)
        or not _SHA256.fullmatch(snapshot["telemetry_schema_sha256"])
        or not isinstance(snapshot["mapping_sha256"], str)
        or not _SHA256.fullmatch(snapshot["mapping_sha256"])
        or not isinstance(snapshot["output_counter_comparability"], str)
        or snapshot["output_counter_comparability"] not in _COMPARABILITIES
        or not isinstance(snapshot["output_counter_path"], str)
        or not _COUNTER_PATH.fullmatch(snapshot["output_counter_path"])
    ):
        raise _error("completion_telemetry_binding_invalid")
    return dict(snapshot)


def _validate_offline_binding(
    binding: OfflineCompletionRecordBinding,
) -> dict[str, str]:
    if type(binding) is not OfflineCompletionRecordBinding:
        raise _error("completion_telemetry_binding_invalid")
    snapshot = {
        "telemetry_schema_sha256": binding.telemetry_schema_sha256,
        "adapter_version": binding.adapter_version,
        "mapping_schema_version": binding.mapping_schema_version,
        "mapping_version": binding.mapping_version,
        "mapping_sha256": binding.mapping_sha256,
        "provider_id": binding.provider_id,
        "api_surface": binding.api_surface,
        "transport_id": binding.transport_id,
        "output_counter_comparability": "not_provided",
        "output_counter_path": "output_tokens",
    }
    return _validate_binding_snapshot(snapshot)


def _validate_runtime_binding(
    binding: VerifiedRuntimeCompletionBinding,
) -> dict[str, str]:
    if type(binding) is not VerifiedRuntimeCompletionBinding:
        raise _error("completion_telemetry_runtime_binding_required")
    try:
        binding.assert_runtime_authority()
        snapshot = binding.runtime_snapshot()
    except Exception:
        raise _error("completion_telemetry_runtime_binding_required") from None
    return _validate_binding_snapshot(snapshot)


def sanitize_completion_capture(
    raw_capture: Mapping[str, Any],
    *,
    normalized_usage: Mapping[str, Any] | None = None,
    historical: bool = False,
    sensitive_canaries: Iterable[str] = (),
) -> SanitizedCompletionCapture:
    """Create the only capture type accepted by the record builder.

    The returned mapping projection contains completion-native discriminators
    only. Usage, request identifiers, HTTP status, and request caps cannot
    influence ordinary Provider mapping.
    """

    if not isinstance(raw_capture, Mapping) or any(
        not isinstance(key, str) for key in raw_capture
    ):
        raise _error("completion_telemetry_raw_capture_invalid")
    if set(raw_capture) - _RAW_CAPTURE_FIELDS:
        raise _error("completion_telemetry_raw_capture_extra_field")
    if historical and raw_capture:
        raise _error("completion_telemetry_historical_raw_value_forbidden")
    canaries = _normalized_canaries(sensitive_canaries)

    native_status, projected_status = _capture_scalar(
        raw_capture.get("status", MISSING),
        historical=historical,
        canaries=canaries,
        truncatable=False,
    )
    native_finish_reason, projected_finish_reason = _capture_scalar(
        raw_capture.get("finish_reason", MISSING),
        historical=historical,
        canaries=canaries,
        truncatable=False,
    )
    native_stop_reason, projected_stop_reason = _capture_scalar(
        raw_capture.get("stop_reason", MISSING),
        historical=historical,
        canaries=canaries,
        truncatable=False,
    )
    native_stop_sequence, projected_stop_sequence = _capture_scalar(
        raw_capture.get("stop_sequence", MISSING),
        historical=historical,
        canaries=canaries,
        truncatable=True,
    )
    native_details, projected_details = _capture_incomplete_details(
        raw_capture.get("incomplete_details", MISSING),
        historical=historical,
        canaries=canaries,
    )
    usage, _ = _capture_usage(
        raw_capture.get("usage", MISSING),
        historical=historical,
        normalized_usage=normalized_usage,
        canaries=canaries,
    )
    if (
        "provider_request_id" in raw_capture
        and "provider_request_id_sha256" in raw_capture
    ):
        raise _error("completion_telemetry_provider_request_id_ambiguous")
    if "provider_request_id_sha256" in raw_capture:
        request_id = _capture_hashed_request_id(
            raw_capture["provider_request_id_sha256"], historical=historical
        )
    else:
        request_id = _capture_request_id(
            raw_capture.get("provider_request_id", MISSING),
            historical=historical,
            canaries=canaries,
        )
    http_status = _availability_value(
        raw_capture.get("http_status", MISSING),
        historical=historical,
        kind="http_status",
    )
    output_cap = _availability_value(
        raw_capture.get("requested_output_token_cap", MISSING),
        historical=historical,
        kind="output_token_cap",
    )

    projection: dict[str, Any] = {}
    for key, value in (
        ("status", projected_status),
        ("finish_reason", projected_finish_reason),
        ("stop_reason", projected_stop_reason),
        ("stop_sequence", projected_stop_sequence),
        ("incomplete_details", projected_details),
    ):
        if value is not MISSING:
            projection[key] = value
    components = {
        "native_status": native_status,
        "native_finish_reason": native_finish_reason,
        "native_stop_reason": native_stop_reason,
        "native_stop_sequence": native_stop_sequence,
        "native_incomplete_details": native_details,
        "provider_request_id_sha256": request_id,
        "http_status": http_status,
        "usage": usage,
        "output_token_cap": output_cap,
    }
    return SanitizedCompletionCapture(
        _token=_CAPTURE_CONSTRUCTION_TOKEN,
        mapping_projection=projection,
        record_components=components,
        historical=historical,
        canaries=canaries,
    )


def _validated_capture(
    capture: SanitizedCompletionCapture,
) -> tuple[dict[str, Any], Mapping[str, Any], tuple[str, ...]]:
    if type(capture) is not SanitizedCompletionCapture:
        raise _error("completion_telemetry_sanitized_capture_required")
    projection = capture.mapping_projection()
    if not isinstance(projection, Mapping) or set(projection) - _MAPPING_PROJECTION_FIELDS:
        raise _error("completion_telemetry_capture_projection_invalid")
    components = _validate_exact_fields(
        capture.record_components(),
        _CAPTURE_COMPONENT_FIELDS,
        "completion_telemetry_capture_components_invalid",
    )
    return dict(projection), components, capture._sensitive_canaries()


def _assemble_record(
    *,
    binding: Mapping[str, str],
    capture: SanitizedCompletionCapture,
    components: Mapping[str, Any],
    response_index: int,
    request_index: int | None,
    mapping_result: CompletionMappingResult,
    record_provenance: str,
    output_counter_comparability: str,
    output_counter_path: str | None,
) -> dict[str, Any]:
    state, source, _, rule_id = mapping_result
    return {
        "telemetry_schema_version": TELEMETRY_SCHEMA_VERSION,
        "telemetry_schema_sha256": binding["telemetry_schema_sha256"],
        "record_provenance": record_provenance,
        "adapter_version": binding["adapter_version"],
        "mapping_schema_version": binding["mapping_schema_version"],
        "mapping_version": binding["mapping_version"],
        "mapping_sha256": binding["mapping_sha256"],
        "provider_id": binding["provider_id"],
        "api_surface": binding["api_surface"],
        "transport_id": binding["transport_id"],
        "response_index": _index(response_index, nullable=False),
        "request_index": _index(request_index, nullable=True),
        "native_status": components["native_status"],
        "native_finish_reason": components["native_finish_reason"],
        "native_stop_reason": components["native_stop_reason"],
        "native_stop_sequence": components["native_stop_sequence"],
        "native_incomplete_details": components["native_incomplete_details"],
        "normalized_completion_state": state,
        "truncation_signal_source": source,
        "matched_rule_id": rule_id,
        "provider_request_id_sha256": components["provider_request_id_sha256"],
        "http_status": components["http_status"],
        "usage": components["usage"],
        "output_token_cap": components["output_token_cap"],
        "output_counter_comparability": output_counter_comparability,
        "output_counter_path": output_counter_path,
    }


def build_completion_record(
    capture: SanitizedCompletionCapture,
    *,
    binding: VerifiedRuntimeCompletionBinding,
    response_index: int,
    request_index: int,
) -> dict[str, Any]:
    """Build a live record only from strict-loader runtime authority."""

    binding_snapshot = _validate_runtime_binding(binding)
    projection, components, canaries = _validated_capture(capture)
    if capture.historical:
        raise _error("completion_telemetry_live_historical_capture_forbidden")
    request_index = _index(request_index, nullable=False)
    native_result = _resolve_runtime_mapping(binding, projection)
    comparability = binding_snapshot["output_counter_comparability"]
    output_path = binding_snapshot["output_counter_path"]
    mapping_result = _effective_mapping_result(
        native_result,
        usage=components["usage"],
        output_cap=components["output_token_cap"],
        output_counter_comparability=comparability,
        output_counter_path=output_path,
    )
    record = _assemble_record(
        binding=binding_snapshot,
        capture=capture,
        components=components,
        response_index=response_index,
        request_index=request_index,
        mapping_result=mapping_result,
        record_provenance="live_adapter_write",
        output_counter_comparability=comparability,
        output_counter_path=output_path,
    )
    validate_completion_record(record, binding=binding, sensitive_canaries=canaries)
    return record


def build_offline_completion_record(
    capture: SanitizedCompletionCapture,
    *,
    binding: OfflineCompletionRecordBinding,
    response_index: int,
    request_index: int | None,
    mapping_resolver: MappingResolver | None,
    output_counter_comparability: str = "not_provided",
    output_counter_path: str | None = None,
) -> dict[str, Any]:
    """Build a non-closure-eligible record for schema and fixture validation."""

    binding_snapshot = _validate_offline_binding(binding)
    projection, components, canaries = _validated_capture(capture)
    historical = capture.historical

    if historical:
        if mapping_resolver is not None:
            raise _error("completion_telemetry_historical_mapping_forbidden")
        mapping_result: CompletionMappingResult = (
            "not_persisted",
            "none",
            None,
            "legacy-not-persisted-v1",
        )
        output_counter_comparability = "not_persisted"
        output_counter_path = None
    else:
        if not callable(mapping_resolver):
            raise _error("completion_telemetry_mapping_resolver_required")
        native_result = _resolve_offline_mapping(
            mapping_resolver, projection, binding
        )
        mapping_result = _effective_mapping_result(
            native_result,
            usage=components["usage"],
            output_cap=components["output_token_cap"],
            output_counter_comparability=output_counter_comparability,
            output_counter_path=output_counter_path,
        )

    record = _assemble_record(
        binding=binding_snapshot,
        capture=capture,
        components=components,
        response_index=response_index,
        request_index=request_index,
        mapping_result=mapping_result,
        record_provenance=(
            "historical_projection" if historical else "offline_validation"
        ),
        output_counter_comparability=output_counter_comparability,
        output_counter_path=output_counter_path,
    )
    validate_offline_completion_record(
        record,
        binding=binding,
        mapping_resolver=mapping_resolver,
        sensitive_canaries=canaries,
    )
    return record


def _validate_scalar_capture(
    value: object,
    *,
    truncatable: bool,
    historical: bool,
    canaries: tuple[str, ...],
) -> None:
    capture = _validate_exact_fields(
        value,
        _SCALAR_CAPTURE_FIELDS,
        "completion_telemetry_scalar_capture_invalid",
    )
    availability = capture["availability"]
    if not isinstance(availability, str) or availability not in _AVAILABILITIES:
        raise _error("completion_telemetry_scalar_capture_invalid")
    if not isinstance(capture["redaction_applied"], bool) or not isinstance(
        capture["truncated"], bool
    ):
        raise _error("completion_telemetry_scalar_capture_invalid")
    if capture["redaction_applied"]:
        raise _error("completion_telemetry_redacted_record_forbidden")
    item = capture["value"]
    if availability in {"not_provided", "not_persisted"}:
        if item is not None or capture["truncated"]:
            raise _error("completion_telemetry_scalar_capture_invalid")
    elif item is not None:
        if not isinstance(item, str):
            raise _error("completion_telemetry_scalar_capture_invalid")
        normalized = _scan_string(item, canaries)
        byte_limit = _MAX_STOP_SEQUENCE_BYTES if truncatable else _MAX_NATIVE_SCALAR_BYTES
        if len(normalized.encode("utf-8")) > byte_limit:
            raise _error("completion_telemetry_scalar_capture_invalid")
        if capture["truncated"]:
            if not truncatable or not normalized.endswith(TRUNCATION_MARKER):
                raise _error("completion_telemetry_scalar_capture_invalid")
        elif TRUNCATION_MARKER in normalized:
            raise _error("completion_telemetry_reserved_marker_spoofed")
    elif capture["truncated"]:
        raise _error("completion_telemetry_scalar_capture_invalid")
    if historical and availability != "not_persisted":
        raise _error("completion_telemetry_historical_record_invalid")
    if not historical and availability == "not_persisted":
        raise _error("completion_telemetry_current_record_not_persisted")


def _validate_details_capture(
    value: object,
    *,
    historical: bool,
    canaries: tuple[str, ...],
) -> None:
    capture = _validate_exact_fields(
        value,
        _DETAIL_CAPTURE_FIELDS,
        "completion_telemetry_details_capture_invalid",
    )
    availability = capture["availability"]
    if not isinstance(availability, str) or availability not in _AVAILABILITIES:
        raise _error("completion_telemetry_details_capture_invalid")
    if capture["redaction_applied"] is not False:
        raise _error("completion_telemetry_redacted_record_forbidden")
    if not isinstance(capture["truncated"], bool):
        raise _error("completion_telemetry_details_capture_invalid")
    omitted = capture["omitted_child_field_count"]
    _nonnegative_integer(omitted, "completion_telemetry_details_capture_invalid")
    details = capture["value"]
    if availability in {"not_provided", "not_persisted"}:
        if details is not None or capture["truncated"] or omitted != 0:
            raise _error("completion_telemetry_details_capture_invalid")
    elif details is not None:
        if not isinstance(details, Mapping) or set(details) - {
            "reason",
            "_telemetry_truncated",
        }:
            raise _error("completion_telemetry_details_capture_invalid")
        if "reason" in details:
            reason = details["reason"]
            if reason is not None:
                if not isinstance(reason, str):
                    raise _error("completion_telemetry_details_capture_invalid")
                reason = _scan_string(reason, canaries)
                if len(reason.encode("utf-8")) > _MAX_DETAILS_REASON_BYTES:
                    raise _error("completion_telemetry_details_capture_invalid")
        marker = details.get("_telemetry_truncated", MISSING)
        if capture["truncated"]:
            if marker is not True:
                raise _error("completion_telemetry_details_capture_invalid")
        elif marker is not MISSING:
            raise _error("completion_telemetry_details_capture_invalid")
        if len(_canonical_json_bytes(details)) > _MAX_DETAILS_BYTES:
            raise _error("completion_telemetry_details_capture_invalid")
    elif capture["truncated"] or omitted != 0:
        raise _error("completion_telemetry_details_capture_invalid")
    if historical and availability != "not_persisted":
        raise _error("completion_telemetry_historical_record_invalid")
    if not historical and availability == "not_persisted":
        raise _error("completion_telemetry_current_record_not_persisted")


def _validate_availability_value(
    value: object,
    *,
    kind: str,
    historical: bool,
) -> None:
    capture = _validate_exact_fields(
        value,
        _AVAILABILITY_VALUE_FIELDS,
        "completion_telemetry_availability_value_invalid",
    )
    availability = capture["availability"]
    if not isinstance(availability, str) or availability not in _AVAILABILITIES:
        raise _error("completion_telemetry_availability_value_invalid")
    item = capture["value"]
    if availability in {"not_provided", "not_persisted"} and item is not None:
        raise _error("completion_telemetry_availability_value_invalid")
    if availability == "provided" and item is not None:
        if kind == "sha256":
            if not isinstance(item, str) or not _SHA256.fullmatch(item):
                raise _error("completion_telemetry_availability_value_invalid")
        elif kind == "http_status":
            if (
                isinstance(item, bool)
                or not isinstance(item, int)
                or not 100 <= item <= 599
            ):
                raise _error("completion_telemetry_availability_value_invalid")
        elif kind == "output_token_cap":
            _nonnegative_integer(
                item, "completion_telemetry_availability_value_invalid"
            )
        else:
            raise _error("completion_telemetry_internal_contract_error")
    if historical and availability != "not_persisted":
        raise _error("completion_telemetry_historical_record_invalid")
    if not historical and availability == "not_persisted":
        raise _error("completion_telemetry_current_record_not_persisted")


def _validate_usage_capture(
    value: object,
    *,
    historical: bool,
    canaries: tuple[str, ...],
) -> None:
    usage = _validate_exact_fields(
        value, _USAGE_FIELDS, "completion_telemetry_usage_capture_invalid"
    )
    availability = usage["availability"]
    if not isinstance(availability, str) or availability not in _AVAILABILITIES:
        raise _error("completion_telemetry_usage_capture_invalid")
    if not isinstance(usage["complete"], bool) or not isinstance(
        usage["truncated"], bool
    ):
        raise _error("completion_telemetry_usage_capture_invalid")
    native_value_state = usage["native_value_state"]
    if not isinstance(native_value_state, str) or native_value_state not in {
        "object",
        "null",
        "not_provided",
        "not_persisted",
    }:
        raise _error("completion_telemetry_usage_capture_invalid")
    omitted = _nonnegative_integer(
        usage["omitted_non_numeric_field_count"],
        "completion_telemetry_usage_capture_invalid",
    )
    normalized = _validate_normalized_usage(usage["normalized"])
    counters = usage["native_numeric_counters"]
    if (
        not isinstance(counters, Sequence)
        or isinstance(counters, (str, bytes, bytearray))
        or len(counters) > _MAX_USAGE_LEAVES
    ):
        raise _error("completion_telemetry_usage_capture_invalid")
    paths: list[str] = []
    for counter in counters:
        item = _validate_exact_fields(
            counter, _COUNTER_FIELDS, "completion_telemetry_usage_counter_invalid"
        )
        path = item["path"]
        if (
            not isinstance(path, str)
            or not _COUNTER_PATH.fullmatch(path)
            or len(path.encode("utf-8")) > _MAX_COUNTER_PATH_BYTES
        ):
            raise _error("completion_telemetry_usage_counter_invalid")
        _scan_string(path, canaries)
        counter_value = item["value"]
        if counter_value is not None:
            _nonnegative_integer(
                counter_value, "completion_telemetry_usage_counter_invalid"
            )
        paths.append(path)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise _error("completion_telemetry_usage_counter_order_invalid")
    if availability in {"not_provided", "not_persisted"}:
        if (
            native_value_state != availability
            or usage["complete"]
            or usage["truncated"]
            or omitted != 0
            or counters
            or any(normalized[field] is not None for field in _NORMALIZED_USAGE_FIELDS)
        ):
            raise _error("completion_telemetry_usage_capture_invalid")
    elif native_value_state == "null":
        if (
            usage["complete"]
            or usage["truncated"]
            or omitted != 0
            or counters
            or any(normalized[field] is not None for field in _NORMALIZED_USAGE_FIELDS)
        ):
            raise _error("completion_telemetry_usage_capture_invalid")
    elif native_value_state != "object":
        raise _error("completion_telemetry_usage_capture_invalid")
    elif usage["complete"]:
        required = (
            normalized["requests"],
            normalized["input_tokens"],
            normalized["output_tokens"],
            normalized["total_tokens"],
        )
        if (
            any(item is None for item in required)
            or normalized["requests"] == 0
            or usage["truncated"]
            or omitted != 0
        ):
            raise _error("completion_telemetry_usage_capture_invalid")
    if len(_canonical_json_bytes(usage)) > _MAX_USAGE_BYTES:
        raise _error("completion_telemetry_usage_capture_invalid")
    if historical and availability != "not_persisted":
        raise _error("completion_telemetry_historical_record_invalid")
    if not historical and availability == "not_persisted":
        raise _error("completion_telemetry_current_record_not_persisted")


def _validate_completion_record(
    record: Mapping[str, Any],
    *,
    binding: Mapping[str, str],
    mapping_resolver: Callable[[Mapping[str, Any]], CompletionMappingResult] | None,
    allowed_provenances: frozenset[str],
    sensitive_canaries: Iterable[str] = (),
) -> None:
    """Validate one persisted record without repairing or redacting it."""

    record = _validate_exact_fields(
        record, _RECORD_FIELDS, "completion_telemetry_record_shape_invalid"
    )
    canaries = _normalized_canaries(sensitive_canaries)
    _scan_json_value(record, canaries)
    if len(_canonical_json_bytes(record)) > _MAX_RECORD_BYTES:
        raise _error("completion_telemetry_record_over_limit")
    if record["telemetry_schema_version"] != TELEMETRY_SCHEMA_VERSION:
        raise _error("completion_telemetry_schema_version_invalid")
    for field, expected in (
        ("telemetry_schema_sha256", binding["telemetry_schema_sha256"]),
        ("adapter_version", binding["adapter_version"]),
        ("mapping_schema_version", binding["mapping_schema_version"]),
        ("mapping_version", binding["mapping_version"]),
        ("mapping_sha256", binding["mapping_sha256"]),
        ("provider_id", binding["provider_id"]),
        ("api_surface", binding["api_surface"]),
        ("transport_id", binding["transport_id"]),
    ):
        if record[field] != expected:
            raise _error("completion_telemetry_binding_mismatch")
    for field in (
        "adapter_version",
        "mapping_schema_version",
        "mapping_version",
        "provider_id",
        "api_surface",
        "transport_id",
    ):
        _safe_identifier(record[field])
    if not isinstance(record["mapping_sha256"], str) or not _SHA256.fullmatch(
        record["mapping_sha256"]
    ):
        raise _error("completion_telemetry_binding_mismatch")
    if not isinstance(record["telemetry_schema_sha256"], str) or not _SHA256.fullmatch(
        record["telemetry_schema_sha256"]
    ):
        raise _error("completion_telemetry_binding_mismatch")
    _index(record["response_index"], nullable=False)
    _index(record["request_index"], nullable=True)

    provenance = record["record_provenance"]
    if not isinstance(provenance, str) or provenance not in allowed_provenances:
        raise _error("completion_telemetry_record_provenance_invalid")
    historical = provenance == "historical_projection"
    _validate_scalar_capture(
        record["native_status"],
        truncatable=False,
        historical=historical,
        canaries=canaries,
    )
    _validate_scalar_capture(
        record["native_finish_reason"],
        truncatable=False,
        historical=historical,
        canaries=canaries,
    )
    _validate_scalar_capture(
        record["native_stop_reason"],
        truncatable=False,
        historical=historical,
        canaries=canaries,
    )
    _validate_scalar_capture(
        record["native_stop_sequence"],
        truncatable=True,
        historical=historical,
        canaries=canaries,
    )
    _validate_details_capture(
        record["native_incomplete_details"],
        historical=historical,
        canaries=canaries,
    )
    _validate_availability_value(
        record["provider_request_id_sha256"], kind="sha256", historical=historical
    )
    _validate_availability_value(
        record["http_status"], kind="http_status", historical=historical
    )
    _validate_usage_capture(record["usage"], historical=historical, canaries=canaries)
    _validate_availability_value(
        record["output_token_cap"],
        kind="output_token_cap",
        historical=historical,
    )

    state = record["normalized_completion_state"]
    source = record["truncation_signal_source"]
    if (
        not isinstance(state, str)
        or not isinstance(source, str)
        or state not in _STATES
        or source not in _SOURCES
    ):
        raise _error("completion_telemetry_completion_state_invalid")
    _safe_identifier(record["matched_rule_id"], rule_id=True)
    comparability = record["output_counter_comparability"]
    if not isinstance(comparability, str) or comparability not in _COMPARABILITIES:
        raise _error("completion_telemetry_output_comparability_invalid")
    output_path = record["output_counter_path"]
    if output_path is not None and (
        not isinstance(output_path, str)
        or not _COUNTER_PATH.fullmatch(output_path)
        or len(output_path.encode("utf-8")) > _MAX_COUNTER_PATH_BYTES
    ):
        raise _error("completion_telemetry_output_counter_path_invalid")
    if allowed_provenances == frozenset({"live_adapter_write"}) and (
        comparability != binding["output_counter_comparability"]
        or output_path != binding["output_counter_path"]
    ):
        raise _error("completion_telemetry_output_contract_mismatch")
    counters = {
        item["path"]: item["value"]
        for item in record["usage"]["native_numeric_counters"]
    }
    if comparability == "comparable":
        if (
            output_path is None
            or output_path not in counters
            or counters[output_path] is None
            or record["output_token_cap"]["availability"] != "provided"
            or record["output_token_cap"]["value"] is None
            or record["usage"]["availability"] != "provided"
        ):
            raise _error("completion_telemetry_comparable_counter_invalid")
    elif output_path is not None:
        raise _error("completion_telemetry_output_counter_path_invalid")

    if source == "token_cap_fallback":
        if (
            state != "incomplete_length"
            or comparability != "comparable"
            or output_path is None
            or counters.get(output_path) != record["output_token_cap"]["value"]
            or record["matched_rule_id"] != TOKEN_CAP_FALLBACK_RULE_ID
        ):
            raise _error("completion_telemetry_token_cap_fallback_invalid")
        for field in (
            "native_status",
            "native_finish_reason",
            "native_stop_reason",
            "native_stop_sequence",
            "native_incomplete_details",
        ):
            if record[field]["availability"] != "not_provided":
                raise _error("completion_telemetry_token_cap_fallback_invalid")

    if historical:
        if (
            state != "not_persisted"
            or source != "none"
            or comparability != "not_persisted"
            or mapping_resolver is not None
        ):
            raise _error("completion_telemetry_historical_record_invalid")
        return
    if state == "not_persisted":
        raise _error("completion_telemetry_current_record_not_persisted")
    if not callable(mapping_resolver):
        raise _error("completion_telemetry_mapping_resolver_required")
    native_expected = _validated_mapping_result(
        mapping_resolver(_mapping_projection_from_record(record))
    )
    if native_expected[1] == "token_cap_fallback":
        raise _error("completion_telemetry_provider_mapping_fallback_forbidden")
    expected = _effective_mapping_result(
        native_expected,
        usage=record["usage"],
        output_cap=record["output_token_cap"],
        output_counter_comparability=comparability,
        output_counter_path=output_path,
    )
    if (state, source, record["matched_rule_id"]) != (
        expected[0],
        expected[1],
        expected[3],
    ):
        raise _error("completion_telemetry_mapping_result_mismatch")
    if state == "not_provided":
        if source != "none":
            raise _error("completion_telemetry_completion_state_invalid")
        for field in (
            "native_status",
            "native_finish_reason",
            "native_stop_reason",
            "native_stop_sequence",
            "native_incomplete_details",
        ):
            capture = record[field]
            if capture["availability"] not in {"provided", "not_provided"} or capture[
                "value"
            ] is not None:
                raise _error("completion_telemetry_not_provided_state_invalid")
    if state == "unmapped" and source != "native_status":
        raise _error("completion_telemetry_completion_state_invalid")
    if state == "unmapped":
        scalar_evidence = any(
            record[field]["availability"] == "provided"
            and isinstance(record[field]["value"], str)
            and bool(record[field]["value"])
            for field in (
                "native_status",
                "native_finish_reason",
                "native_stop_reason",
                "native_stop_sequence",
            )
        )
        details = record["native_incomplete_details"]
        details_value = details["value"]
        details_reason = (
            details_value.get("reason")
            if isinstance(details_value, Mapping)
            else None
        )
        details_evidence = (
            details["availability"] == "provided"
            and (
                (isinstance(details_reason, str) and bool(details_reason))
                or details["omitted_child_field_count"] > 0
            )
        )
        if not scalar_evidence and not details_evidence:
            raise _error("completion_telemetry_unmapped_without_native_evidence")
    if source == "none" and state not in {"not_provided", "not_persisted"}:
        raise _error("completion_telemetry_completion_state_invalid")
    if source == "native_status" and state not in _RECOGNIZED_TERMINAL_STATES | {
        "unmapped"
    }:
        raise _error("completion_telemetry_completion_state_invalid")
    if state in _RECOGNIZED_TERMINAL_STATES and source == "native_status":
        if not any(
            record[field]["availability"] == "provided"
            and isinstance(record[field]["value"], str)
            and bool(record[field]["value"])
            for field in (
                "native_status",
                "native_finish_reason",
                "native_stop_reason",
            )
        ):
            raise _error("completion_telemetry_native_discriminator_missing")


def validate_completion_record(
    record: Mapping[str, Any],
    *,
    binding: VerifiedRuntimeCompletionBinding,
    sensitive_canaries: Iterable[str] = (),
) -> None:
    """Validate a live record using only strict-loader runtime authority."""

    snapshot = _validate_runtime_binding(binding)

    def resolve(projection: Mapping[str, Any]) -> CompletionMappingResult:
        return _resolve_runtime_mapping(binding, projection)

    _validate_completion_record(
        record,
        binding=snapshot,
        mapping_resolver=resolve,
        allowed_provenances=frozenset({"live_adapter_write"}),
        sensitive_canaries=sensitive_canaries,
    )


def validate_offline_completion_record(
    record: Mapping[str, Any],
    *,
    binding: OfflineCompletionRecordBinding,
    mapping_resolver: MappingResolver | None,
    sensitive_canaries: Iterable[str] = (),
) -> None:
    """Validate fixture/schema records that can never satisfy live closure."""

    snapshot = _validate_offline_binding(binding)
    provenance = record.get("record_provenance") if isinstance(record, Mapping) else None
    if provenance == "historical_projection":
        if mapping_resolver is not None:
            raise _error("completion_telemetry_historical_mapping_forbidden")
        resolver = None
    else:
        if not callable(mapping_resolver):
            raise _error("completion_telemetry_mapping_resolver_required")

        def resolver(projection: Mapping[str, Any]) -> CompletionMappingResult:
            return _resolve_offline_mapping(mapping_resolver, projection, binding)

    _validate_completion_record(
        record,
        binding=snapshot,
        mapping_resolver=resolver,
        allowed_provenances=frozenset(
            {"offline_validation", "historical_projection"}
        ),
        sensitive_canaries=sensitive_canaries,
    )


def _validate_completion_artifact(
    artifact: Mapping[str, Any],
    *,
    record_validator: Callable[[Mapping[str, Any], tuple[str, ...]], None],
    sensitive_canaries: Iterable[str] = (),
) -> None:
    """Validate a complete in-memory artifact; never repairs persisted values."""

    artifact = _validate_exact_fields(
        artifact,
        frozenset(
            {
                "schema_version",
                "expected_response_count",
                "preregistration_commitment",
                "records",
            }
        ),
        "completion_telemetry_artifact_shape_invalid",
    )
    canaries = _normalized_canaries(sensitive_canaries)
    _scan_json_value(artifact, canaries)
    if artifact["schema_version"] != ARTIFACT_SCHEMA_VERSION:
        raise _error("completion_telemetry_artifact_schema_invalid")
    expected_response_count = _nonnegative_integer(
        artifact["expected_response_count"],
        "completion_telemetry_artifact_expected_count_invalid",
    )
    if expected_response_count < 1:
        raise _error("completion_telemetry_artifact_expected_count_invalid")
    preregistration_commitment = artifact["preregistration_commitment"]
    if (
        not isinstance(preregistration_commitment, str)
        or not _SHA256.fullmatch(preregistration_commitment)
    ):
        raise _error("completion_telemetry_artifact_preregistration_invalid")
    records = artifact["records"]
    if not isinstance(records, list):
        raise _error("completion_telemetry_artifact_records_invalid")
    if len(records) != expected_response_count:
        raise _error("completion_telemetry_artifact_denominator_mismatch")
    response_indices: set[int] = set()
    request_indices: set[int] = set()
    for record in records:
        record_validator(record, canaries)
        response_index = record["response_index"]
        request_index = record["request_index"]
        if response_index in response_indices:
            raise _error("completion_telemetry_artifact_duplicate_response_index")
        if request_index is None or request_index in request_indices:
            raise _error("completion_telemetry_artifact_duplicate_request_index")
        response_indices.add(response_index)
        request_indices.add(request_index)
    expected_indices = set(range(expected_response_count))
    if response_indices != expected_indices:
        raise _error("completion_telemetry_artifact_response_index_gap")
    if request_indices != expected_indices:
        raise _error("completion_telemetry_artifact_request_index_gap")


def validate_completion_artifact(
    artifact: Mapping[str, Any],
    *,
    plan_binding: object,
    sensitive_canaries: Iterable[str] = (),
) -> None:
    """Validate a live artifact against one verified run denominator."""

    # Local import avoids a module-import cycle: capture depends on this
    # sanitizer, while the live artifact gate depends on capture-plan authority.
    from .capture import VerifiedCapturePlanBinding

    if type(plan_binding) is not VerifiedCapturePlanBinding:
        raise _error("completion_telemetry_capture_plan_binding_required")
    try:
        plan_binding.assert_plan_authority()
        binding = plan_binding.runtime_binding()
    except Exception:
        raise _error("completion_telemetry_capture_plan_binding_required") from None
    _validate_runtime_binding(binding)
    if not isinstance(artifact, Mapping) or (
        artifact.get("expected_response_count") != plan_binding.expected_response_count
        or artifact.get("preregistration_commitment")
        != plan_binding.preregistration_commitment
    ):
        raise _error("completion_telemetry_artifact_plan_binding_mismatch")

    def validate_record(record: Mapping[str, Any], canaries: tuple[str, ...]) -> None:
        validate_completion_record(
            record,
            binding=binding,
            sensitive_canaries=canaries,
        )

    _validate_completion_artifact(
        artifact,
        record_validator=validate_record,
        sensitive_canaries=sensitive_canaries,
    )


def validate_offline_completion_artifact(
    artifact: Mapping[str, Any],
    *,
    binding: OfflineCompletionRecordBinding,
    mapping_resolver: MappingResolver | None,
    sensitive_canaries: Iterable[str] = (),
) -> None:
    """Validate an artifact whose records are never closure-eligible."""

    _validate_offline_binding(binding)

    def validate_record(record: Mapping[str, Any], canaries: tuple[str, ...]) -> None:
        record_resolver = (
            None
            if record.get("record_provenance") == "historical_projection"
            else mapping_resolver
        )
        validate_offline_completion_record(
            record,
            binding=binding,
            mapping_resolver=record_resolver,
            sensitive_canaries=canaries,
        )

    _validate_completion_artifact(
        artifact,
        record_validator=validate_record,
        sensitive_canaries=sensitive_canaries,
    )


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "CompletionTelemetryError",
    "MISSING",
    "MappingResolver",
    "OfflineCompletionRecordBinding",
    "SanitizedCompletionCapture",
    "TELEMETRY_SCHEMA_VERSION",
    "TOKEN_CAP_FALLBACK_RULE_ID",
    "build_completion_record",
    "build_offline_completion_record",
    "hash_provider_request_id",
    "sanitize_completion_capture",
    "validate_completion_artifact",
    "validate_completion_record",
    "validate_offline_completion_artifact",
    "validate_offline_completion_record",
]
