from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Mapping, Sequence


TOOL_NAME: Final = "lookup_synthetic_metric"
SUCCESS_DATASET_ID: Final = "kimi_synth_success_v1"
MISSING_DATASET_ID: Final = "kimi_synth_missing_v1"
METRIC_ID: Final = "effect_size"
MAX_ARGUMENT_BYTES: Final = 4 * 1024
MAX_TOOL_EXECUTIONS: Final = 6
MAX_REQUESTED_TOOL_CALLS: Final = 64

_CALL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CALL_FIELDS = frozenset({"call_id", "name", "arguments"})
_ARGUMENT_FIELDS = frozenset({"dataset_id", "metric_id"})
_SUCCESS_RESULT = MappingProxyType(
    {
        "status": "ok",
        "dataset_id": SUCCESS_DATASET_ID,
        "metric_id": METRIC_ID,
        "value": 0.375,
        "unit": "synthetic_standardized_units",
    }
)


class KimiSyntheticToolError(ValueError):
    """Stable local contract failure with no caller-controlled message text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict[str, str]:
        return {"status": "error", "error_code": self.code, "message": str(self)}


@dataclass(frozen=True, slots=True)
class _ValidatedCall:
    call_id: str
    name: str
    arguments: Mapping[str, str]
    canonical_arguments: str


def synthetic_tool_schema() -> dict[str, Any]:
    """Return a fresh strict tool schema for the one in-memory tool."""

    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Return one fixed aggregate metric from a synthetic-only fixture."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dataset_id": {
                        "type": "string",
                        "enum": [SUCCESS_DATASET_ID, MISSING_DATASET_ID],
                    },
                    "metric_id": {"type": "string", "const": METRIC_ID},
                },
                "required": ["dataset_id", "metric_id"],
            },
        },
    }


class KimiSyntheticToolExecutor:
    """One in-memory run budget shared across sequential provider batches."""

    def __init__(self) -> None:
        self._call_by_id: dict[str, _ValidatedCall] = {}
        self._result_by_id: dict[str, dict[str, Any]] = {}
        self._requested_total = 0
        self._executed_total = 0

    @property
    def requested_total(self) -> int:
        return self._requested_total

    @property
    def executed_total(self) -> int:
        return self._executed_total

    def execute_batch(
        self, raw_calls: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Validate a whole batch, then execute new call IDs sequentially.

        A repeated call ID with the same exact name and canonical arguments is
        an idempotent replay, including across batches. Reusing a call ID for a
        different request fails the whole batch before any lookup. The executor
        performs no file, network, database, logging, environment, clock, or
        random operation.
        """

        if isinstance(raw_calls, (str, bytes, bytearray)) or not isinstance(
            raw_calls, Sequence
        ):
            raise KimiSyntheticToolError(
                "synthetic_tool_batch_invalid",
                "Synthetic tool batch must be a bounded sequence.",
            )
        if len(raw_calls) > MAX_REQUESTED_TOOL_CALLS:
            raise KimiSyntheticToolError(
                "synthetic_tool_batch_invalid",
                "Synthetic tool batch exceeds the request-count limit.",
            )

        # Phase one is validation only. No lookup is dispatched until every
        # item, cross-batch call-ID relationship, and cumulative execution
        # budget is valid.
        validated = tuple(_validate_call(item) for item in raw_calls)
        known_by_id = dict(self._call_by_id)
        new_calls: list[_ValidatedCall] = []
        for call in validated:
            prior = known_by_id.get(call.call_id)
            if prior is None:
                known_by_id[call.call_id] = call
                new_calls.append(call)
                continue
            if (
                prior.name != call.name
                or prior.canonical_arguments != call.canonical_arguments
            ):
                raise KimiSyntheticToolError(
                    "synthetic_tool_call_id_conflict",
                    "A synthetic call ID cannot identify two different requests.",
                )
        if self._executed_total + len(new_calls) > MAX_TOOL_EXECUTIONS:
            raise KimiSyntheticToolError(
                "synthetic_tool_execution_budget_exceeded",
                "Synthetic tool execution budget exceeds six unique call IDs.",
            )

        # Phase two is deliberately sequential in first-occurrence order.
        new_results: dict[str, dict[str, Any]] = {}
        for call in new_calls:
            new_results[call.call_id] = _execute_validated_call(call)

        known_before = set(self._call_by_id)
        emitted_in_batch: set[str] = set()
        all_results = {**self._result_by_id, **new_results}
        results: list[dict[str, Any]] = []
        for call in validated:
            replay = (
                call.call_id in known_before
                or call.call_id in emitted_in_batch
            )
            emitted_in_batch.add(call.call_id)
            results.append(
                {
                    "call_id": call.call_id,
                    "name": call.name,
                    "deduplicated": replay,
                    "executed": not replay,
                    "result": dict(all_results[call.call_id]),
                }
            )

        # Commit in-memory telemetry only after validation and all deterministic
        # lookups complete.
        self._call_by_id.update({call.call_id: call for call in new_calls})
        self._result_by_id.update(new_results)
        self._requested_total += len(validated)
        self._executed_total += len(new_calls)
        return {
            "status": "completed",
            "requested_tool_call_count": len(validated),
            "deduplicated_tool_call_count": len(validated) - len(new_calls),
            "executed_tool_call_count": len(new_calls),
            "requested_tool_call_count_total": self._requested_total,
            "executed_tool_call_count_total": self._executed_total,
            "execution_limit": MAX_TOOL_EXECUTIONS,
            "results": results,
        }


def execute_synthetic_tool_batch(
    raw_calls: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Convenience wrapper for one isolated in-memory batch."""

    return KimiSyntheticToolExecutor().execute_batch(raw_calls)


def _validate_call(raw_call: Mapping[str, Any]) -> _ValidatedCall:
    if not isinstance(raw_call, Mapping) or set(raw_call) != _CALL_FIELDS:
        raise KimiSyntheticToolError(
            "synthetic_tool_call_invalid",
            "Synthetic tool call fields are invalid.",
        )
    call_id = raw_call["call_id"]
    name = raw_call["name"]
    arguments_json = raw_call["arguments"]
    if type(call_id) is not str or _CALL_ID.fullmatch(call_id) is None:
        raise KimiSyntheticToolError(
            "synthetic_tool_call_id_invalid",
            "Synthetic tool call ID is invalid.",
        )
    if type(name) is not str or name != TOOL_NAME:
        raise KimiSyntheticToolError(
            "synthetic_tool_name_not_allowed",
            "Synthetic tool name is not allowlisted.",
        )
    if type(arguments_json) is not str:
        raise KimiSyntheticToolError(
            "synthetic_tool_arguments_invalid",
            "Synthetic tool arguments must be a JSON object string.",
        )
    try:
        encoded_size = len(arguments_json.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise KimiSyntheticToolError(
            "synthetic_tool_arguments_invalid",
            "Synthetic tool arguments are not valid UTF-8 text.",
        ) from exc
    if encoded_size == 0 or encoded_size > MAX_ARGUMENT_BYTES:
        raise KimiSyntheticToolError(
            "synthetic_tool_arguments_too_large",
            "Synthetic tool arguments exceed the 4 KiB limit.",
        )

    arguments = _decode_arguments(arguments_json)
    if set(arguments) != _ARGUMENT_FIELDS:
        raise KimiSyntheticToolError(
            "synthetic_tool_arguments_invalid",
            "Synthetic tool argument fields are invalid.",
        )
    dataset_id = arguments["dataset_id"]
    metric_id = arguments["metric_id"]
    if type(dataset_id) is not str or dataset_id not in {
        SUCCESS_DATASET_ID,
        MISSING_DATASET_ID,
    }:
        raise KimiSyntheticToolError(
            "synthetic_tool_arguments_invalid",
            "Synthetic dataset ID is not allowlisted.",
        )
    if type(metric_id) is not str or metric_id != METRIC_ID:
        raise KimiSyntheticToolError(
            "synthetic_tool_arguments_invalid",
            "Synthetic metric ID is not allowlisted.",
        )
    normalized = {"dataset_id": dataset_id, "metric_id": metric_id}
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _ValidatedCall(
        call_id=call_id,
        name=name,
        arguments=MappingProxyType(normalized),
        canonical_arguments=canonical,
    )


def _decode_arguments(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            value,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except KimiSyntheticToolError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise KimiSyntheticToolError(
            "synthetic_tool_arguments_invalid",
            "Synthetic tool arguments are invalid JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise KimiSyntheticToolError(
            "synthetic_tool_arguments_invalid",
            "Synthetic tool arguments must decode to an object.",
        )
    return payload


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise KimiSyntheticToolError(
                "synthetic_tool_arguments_invalid",
                "Synthetic tool arguments cannot contain duplicate keys.",
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    raise KimiSyntheticToolError(
        "synthetic_tool_arguments_invalid",
        "Synthetic tool arguments cannot contain non-finite numbers.",
    )


def _execute_validated_call(call: _ValidatedCall) -> dict[str, Any]:
    dataset_id = call.arguments["dataset_id"]
    if dataset_id == SUCCESS_DATASET_ID:
        return dict(_SUCCESS_RESULT)
    # The second fixed fixture deliberately has no metric. It exercises a stable
    # tool error without looking up a file, database row, external service, or
    # caller-provided resource.
    return {"status": "error", "error_code": "synthetic_metric_not_found"}


__all__ = [
    "KimiSyntheticToolExecutor",
    "KimiSyntheticToolError",
    "MAX_ARGUMENT_BYTES",
    "MAX_TOOL_EXECUTIONS",
    "METRIC_ID",
    "MISSING_DATASET_ID",
    "SUCCESS_DATASET_ID",
    "TOOL_NAME",
    "execute_synthetic_tool_batch",
    "synthetic_tool_schema",
]
