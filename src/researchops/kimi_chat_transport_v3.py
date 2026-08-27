"""Fail-closed Kimi Chat Completions SSE transport diagnostic successor.

Version 3 preserves version 2's exact request bytes and response acceptance
semantics.  Its only protocol change is a fixed, payload-independent diagnostic
enum attached to the broad ``kimi_chat_response_invalid`` error.  No raw body,
header, identifier, field name, value, offset, size or free-text exception is
retained by that diagnostic.

The documented terminal usage projection allowlist is top-level only,
choice-level only, or both with reconciled core values.  The v1 request/value
objects and low-level HTTP controls are reused so their already-frozen key,
TLS, body-size, redaction, timeout, retry and ownership boundaries remain
identical.  The success-stream state machine below is new and independent from
the v1 parser.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from researchops import kimi_chat_transport as _v1


_JSON_LOADS: Final = json.loads


KIMI_CHAT_PARSER_VERSION: Final = "3.0"
KIMI_CHAT_TRANSPORT_V3_ID: Final = "moonshot_direct_chat_completions_sse_v3"
KIMI_CHAT_API_ORIGIN: Final = _v1.KIMI_CHAT_API_ORIGIN
KIMI_CHAT_MODEL_ID: Final = _v1.KIMI_CHAT_MODEL_ID
KIMI_CHAT_PATH: Final = _v1.KIMI_CHAT_PATH
KIMI_CHAT_V3_SOURCE_CAPTURE_METHOD: Final = (
    "fixed first-party HTTPS GET with identity content encoding; "
    "SHA-256 over decoded response bytes"
)

# Public value/API types intentionally remain wire-compatible with v1.  The
# successor boundary is the parser/runner, not a second set of message classes.
KimiAssistantMessage = _v1.KimiAssistantMessage
KimiChatRequest = _v1.KimiChatRequest
KimiChatTransportError = _v1.KimiChatTransportError
KimiFunctionTool = _v1.KimiFunctionTool
KimiSpecifiedToolChoice = _v1.KimiSpecifiedToolChoice
KimiTextMessage = _v1.KimiTextMessage
KimiToolCall = _v1.KimiToolCall
KimiToolResultMessage = _v1.KimiToolResultMessage

_TOP_LEVEL_FIELDS: Final = frozenset(
    {"id", "object", "created", "model", "choices"}
)
_TERMINAL_TOP_LEVEL_FIELDS: Final = _TOP_LEVEL_FIELDS | {"usage"}
_CHOICE_FIELDS: Final = frozenset({"index", "delta", "finish_reason"})
_TERMINAL_CHOICE_FIELDS_WITH_USAGE: Final = _CHOICE_FIELDS | {"usage"}
_DELTA_FIELDS: Final = frozenset(
    {"role", "reasoning_content", "content", "tool_calls"}
)
_TOOL_FRAGMENT_FIELDS: Final = frozenset({"index", "id", "type", "function"})
_TOOL_FUNCTION_FRAGMENT_FIELDS: Final = frozenset(
    {"name", "arguments"}
)
_USAGE_REQUIRED_FIELDS: Final = frozenset(
    {"prompt_tokens", "completion_tokens", "total_tokens"}
)
_USAGE_FIELDS_WITH_CACHE: Final = _USAGE_REQUIRED_FIELDS | {"cached_tokens"}
_COMPLETE_FINISH_REASONS: Final = frozenset({"stop", "tool_calls", "length"})


@dataclass(frozen=True, slots=True)
class KimiChatOfficialSourceCommitment:
    source_id: str
    url: str
    captured_at_utc: str
    decoded_bytes: int
    sha256: str


KIMI_CHAT_V3_OFFICIAL_SOURCE_COMMITMENTS: Final = (
    KimiChatOfficialSourceCommitment(
        source_id="chat_completions_documentation",
        url="https://platform.kimi.com/docs/api/chat",
        captured_at_utc="2026-08-26T16:15:03.000Z",
        decoded_bytes=826_200,
        sha256="8aceb197e56b47dc73c1f06377462ed33e36c126f4e7d5459294b0746b94d43a",
    ),
    KimiChatOfficialSourceCommitment(
        source_id="openai_migration_guide",
        url="https://platform.kimi.com/docs/guide/migrating-from-openai-to-kimi",
        captured_at_utc="2026-08-26T16:28:17.000Z",
        decoded_bytes=477_544,
        sha256="e03668deb99a35666293518e33cb80a15e68bc368a03168c0041f0f7f5b8a476",
    ),
    KimiChatOfficialSourceCommitment(
        source_id="streaming_guide",
        url=(
            "https://platform.kimi.com/docs/guide/"
            "utilize-the-streaming-output-feature-of-kimi-api"
        ),
        captured_at_utc="2026-08-26T16:28:25.000Z",
        decoded_bytes=547_349,
        sha256="d2ec3b080c02d98889334e29c9965044f8ef941b85171bfd7dfd910f3ed7b3c2",
    ),
)


@dataclass(frozen=True, slots=True)
class KimiChatUsageV3:
    """Exact documented usage with conservative optional-cache handling.

    ``cached_tokens`` is zero when the Provider omitted the optional field, but
    ``cached_tokens_reported`` remains false so zero is never misrepresented as
    an observed cache count.  Budgeting must use the zero value as all-uncached
    input and may apply a cache discount only when ``cached_tokens_reported`` is
    true.
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    cached_tokens_reported: bool

    def __post_init__(self) -> None:
        values = (
            self.prompt_tokens,
            self.completion_tokens,
            self.total_tokens,
            self.cached_tokens,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("Kimi usage values must be non-negative integers")
        if type(self.cached_tokens_reported) is not bool:
            raise ValueError("Kimi cache-reporting state must be boolean")
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("Kimi total_tokens does not reconcile")
        if self.cached_tokens > self.prompt_tokens:
            raise ValueError("Kimi cached_tokens exceeds prompt_tokens")
        if not self.cached_tokens_reported and self.cached_tokens != 0:
            raise ValueError("Unreported Kimi cached_tokens must be conservative zero")

    def to_dict(self) -> dict[str, int | bool | None]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": (
                self.cached_tokens if self.cached_tokens_reported else None
            ),
            "cached_tokens_reported": self.cached_tokens_reported,
        }


@dataclass(frozen=True, slots=True)
class KimiChatResponseV3:
    assistant_message: KimiAssistantMessage = field(repr=False)
    finish_reason: str
    usage: KimiChatUsageV3
    latency_ms: int
    http_status: int
    http_attempts: int
    network_calls: int

    def __post_init__(self) -> None:
        if self.finish_reason not in {"stop", "tool_calls"}:
            raise ValueError("Kimi response finish reason is not complete")
        if (
            self.http_status != 200
            or self.http_attempts != 1
            or self.network_calls != 1
        ):
            raise ValueError("Kimi response transport counters are invalid")


# Concise successor aliases for callers that import only this module.
KimiChatUsage = KimiChatUsageV3
KimiChatResponse = KimiChatResponseV3


RESPONSE_VALIDATION_DIAGNOSTIC_SCHEMA_VERSION: Final = (
    "kimi-response-validation-diagnostic/1.0"
)
RESPONSE_VALIDATION_DIAGNOSTIC_CODES: Final = frozenset(
    {
        "media_type_not_event_stream",
        "content_encoding_not_identity",
        "data_after_terminal",
        "empty_choices_without_usage",
        "choices_not_array",
        "choices_count_not_one",
        "choice_not_object",
        "choice_field_set_invalid",
        "top_level_field_set_invalid",
        "completion_id_invalid",
        "completion_id_changed",
        "object_invalid",
        "model_mismatch",
        "created_invalid",
        "created_changed",
        "choice_index_invalid",
        "delta_not_object",
        "delta_field_set_invalid",
        "assistant_role_invalid",
        "assistant_role_repeated",
        "reasoning_content_not_string",
        "reasoning_after_content",
        "content_not_string",
        "finish_reason_invalid",
        "sse_response_too_large",
        "sse_event_too_large",
        "sse_event_utf8_invalid",
        "sse_event_line_invalid",
        "sse_trailing_bytes",
        "json_syntax_invalid",
        "json_duplicate_key",
        "json_nonfinite_number",
        "json_recursion_limit",
        "json_top_level_not_object",
        "success_stream_decoding_error",
        "transport_decoding_error",
        "completion_id_missing_after_terminal",
        "assistant_role_missing_after_terminal",
        "stop_content_blank",
    }
)


@dataclass(frozen=True, slots=True)
class KimiResponseValidationDiagnostic:
    """A fixed local branch label; it is not a Provider-cause assertion."""

    code: str
    schema_version: str = RESPONSE_VALIDATION_DIAGNOSTIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            self.schema_version != RESPONSE_VALIDATION_DIAGNOSTIC_SCHEMA_VERSION
            or self.code not in RESPONSE_VALIDATION_DIAGNOSTIC_CODES
        ):
            raise ValueError("Kimi response validation diagnostic is invalid")

    def to_dict(self) -> dict[str, str]:
        return {"schema_version": self.schema_version, "code": self.code}


class _SseDiagnosticError(Exception):
    def __init__(self, code: str) -> None:
        if code not in {
            "sse_response_too_large",
            "sse_event_too_large",
            "sse_event_utf8_invalid",
            "sse_event_line_invalid",
            "sse_trailing_bytes",
        }:
            raise ValueError("invalid SSE diagnostic code")
        super().__init__(code)
        self.code = code


class _JsonDiagnosticError(Exception):
    def __init__(self, code: str) -> None:
        if code not in {
            "json_syntax_invalid",
            "json_duplicate_key",
            "json_nonfinite_number",
            "json_recursion_limit",
            "json_top_level_not_object",
        }:
            raise ValueError("invalid JSON diagnostic code")
        super().__init__(code)
        self.code = code


def _diagnostic(code: str) -> KimiResponseValidationDiagnostic:
    return KimiResponseValidationDiagnostic(code=code)


def _attach_diagnostic(
    error: KimiChatTransportError, code: str
) -> KimiChatTransportError:
    error.response_validation_diagnostic = _diagnostic(code)
    return error


def response_validation_diagnostic(
    error: BaseException,
) -> KimiResponseValidationDiagnostic | None:
    value = getattr(error, "response_validation_diagnostic", None)
    return value if isinstance(value, KimiResponseValidationDiagnostic) else None


def _configuration_is_valid_v3() -> bool:
    return (
        KIMI_CHAT_PARSER_VERSION == "3.0"
        and KIMI_CHAT_TRANSPORT_V3_ID
        == "moonshot_direct_chat_completions_sse_v3"
        and KIMI_CHAT_API_ORIGIN == "https://api.moonshot.cn"
        and KIMI_CHAT_MODEL_ID == "kimi-k3"
        and KIMI_CHAT_PATH == "/v1/chat/completions"
        and _v1._configuration_is_valid()
    )


def _response_error(
    code: str,
    *,
    usage: KimiChatUsageV3 | None = None,
    outcome_unknown: bool = False,
) -> KimiChatTransportError:
    return KimiChatTransportError(
        code,
        http_status=200,
        network_calls=1,
        outcome_unknown=outcome_unknown,
        usage=usage,
    )


def _response_invalid(
    diagnostic_code: str,
    *,
    usage: KimiChatUsageV3 | None = None,
) -> KimiChatTransportError:
    return _attach_diagnostic(
        _response_error("kimi_chat_response_invalid", usage=usage),
        diagnostic_code,
    )


def _validate_exact_fields(
    value: object,
    expected: frozenset[str],
    *,
    diagnostic_code: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise _response_invalid(diagnostic_code)
    return value


def _parse_usage_v3(value: object) -> KimiChatUsageV3:
    if not isinstance(value, dict) or frozenset(value) not in {
        _USAGE_REQUIRED_FIELDS,
        _USAGE_FIELDS_WITH_CACHE,
    }:
        raise _response_error("kimi_chat_usage_invalid")
    try:
        cache_reported = "cached_tokens" in value
        return KimiChatUsageV3(
            prompt_tokens=value["prompt_tokens"],
            completion_tokens=value["completion_tokens"],
            total_tokens=value["total_tokens"],
            cached_tokens=value.get("cached_tokens", 0),
            cached_tokens_reported=cache_reported,
        )
    except ValueError as exc:
        raise _response_error("kimi_chat_usage_invalid") from exc


def _reconcile_usage_projections(
    top_usage: KimiChatUsageV3 | None,
    choice_usage: KimiChatUsageV3 | None,
) -> KimiChatUsageV3:
    if top_usage is None and choice_usage is None:
        raise _response_error("kimi_chat_usage_missing", outcome_unknown=True)
    if top_usage is None:
        if choice_usage is None:  # pragma: no cover - defensive type narrowing
            raise _response_error("kimi_chat_usage_missing", outcome_unknown=True)
        return choice_usage
    if choice_usage is None:
        return top_usage
    top_core = (
        top_usage.prompt_tokens,
        top_usage.completion_tokens,
        top_usage.total_tokens,
    )
    choice_core = (
        choice_usage.prompt_tokens,
        choice_usage.completion_tokens,
        choice_usage.total_tokens,
    )
    if top_core != choice_core:
        raise _response_error("kimi_chat_usage_invalid")
    if (
        top_usage.cached_tokens_reported
        and choice_usage.cached_tokens_reported
        and top_usage.cached_tokens != choice_usage.cached_tokens
    ):
        raise _response_error("kimi_chat_usage_invalid")
    if top_usage.cached_tokens_reported:
        return top_usage
    if choice_usage.cached_tokens_reported:
        return choice_usage
    return top_usage


def _apply_tool_fragments_v3(
    builders: dict[int, _v1._ToolCallBuilder], value: object
) -> None:
    if not isinstance(value, list) or not value:
        raise _response_error("kimi_chat_tool_protocol_invalid")
    for fragment_value in value:
        if not isinstance(fragment_value, dict) or not set(fragment_value).issubset(
            _TOOL_FRAGMENT_FIELDS
        ):
            raise _response_error("kimi_chat_tool_protocol_invalid")
        if not fragment_value or set(fragment_value) == {"index"}:
            raise _response_error("kimi_chat_tool_protocol_invalid")
        index = fragment_value.get("index")
        if (
            type(index) is not int
            or not 0 <= index < _v1._MAX_TOOL_CALLS_PER_RESPONSE
        ):
            raise _response_error("kimi_chat_tool_protocol_invalid")
        builder = builders.setdefault(index, _v1._ToolCallBuilder())

        if "id" in fragment_value:
            call_id = fragment_value["id"]
            if not isinstance(call_id, str) or (
                builder.call_id is not None and builder.call_id != call_id
            ):
                raise _response_error("kimi_chat_tool_protocol_invalid")
            builder.call_id = call_id

        if "type" in fragment_value:
            call_type = fragment_value["type"]
            if call_type != "function" or (
                builder.call_type is not None and builder.call_type != call_type
            ):
                raise _response_error("kimi_chat_tool_protocol_invalid")
            builder.call_type = call_type

        if "function" in fragment_value:
            function = fragment_value["function"]
            if (
                not isinstance(function, dict)
                or not function
                or not set(function).issubset(_TOOL_FUNCTION_FRAGMENT_FIELDS)
            ):
                raise _response_error("kimi_chat_tool_protocol_invalid")
            if "name" in function:
                name = function["name"]
                if not isinstance(name, str) or (
                    builder.name is not None and builder.name != name
                ):
                    raise _response_error("kimi_chat_tool_protocol_invalid")
                builder.name = name
            if "arguments" in function:
                arguments = function["arguments"]
                if not isinstance(arguments, str):
                    raise _response_error("kimi_chat_tool_protocol_invalid")
                builder.argument_parts.append(arguments)
                if (
                    len("".join(builder.argument_parts).encode("utf-8"))
                    > _v1._MAX_TOOL_ARGUMENT_BYTES
                ):
                    raise _response_error("kimi_chat_tool_protocol_invalid")


def _reject_json_constant_v3(value: str) -> None:
    del value
    raise _JsonDiagnosticError("json_nonfinite_number")


def _reject_duplicate_keys_v3(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _JsonDiagnosticError("json_duplicate_key")
        result[key] = value
    return result


def _decode_json_object_v3(body: bytes) -> dict[str, Any]:
    try:
        value = _JSON_LOADS(
            body,
            object_pairs_hook=_reject_duplicate_keys_v3,
            parse_constant=_reject_json_constant_v3,
        )
    except _JsonDiagnosticError:
        raise
    except RecursionError as exc:
        raise _JsonDiagnosticError("json_recursion_limit") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise _JsonDiagnosticError("json_syntax_invalid") from exc
    if not isinstance(value, dict):
        raise _JsonDiagnosticError("json_top_level_not_object")
    return value


async def _iter_sse_data_v3(response: httpx.Response):
    buffer = bytearray()
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > _v1._MAX_SSE_RESPONSE_BYTES:
            raise _SseDiagnosticError("sse_response_too_large")
        buffer.extend(chunk)
        while True:
            lf = buffer.find(b"\n\n")
            crlf = buffer.find(b"\r\n\r\n")
            candidates = [value for value in (lf, crlf) if value >= 0]
            if not candidates:
                if len(buffer) > _v1._MAX_SSE_EVENT_BYTES:
                    raise _SseDiagnosticError("sse_event_too_large")
                break
            boundary = min(candidates)
            separator_length = 4 if crlf == boundary else 2
            event = bytes(buffer[:boundary])
            del buffer[: boundary + separator_length]
            if len(event) > _v1._MAX_SSE_EVENT_BYTES:
                raise _SseDiagnosticError("sse_event_too_large")
            try:
                lines = event.decode("utf-8").splitlines()
            except UnicodeDecodeError as exc:
                raise _SseDiagnosticError("sse_event_utf8_invalid") from exc
            data_lines: list[str] = []
            for line in lines:
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    raise _SseDiagnosticError("sse_event_line_invalid")
                value = line[5:]
                if value.startswith(" "):
                    value = value[1:]
                data_lines.append(value)
            if data_lines:
                yield "\n".join(data_lines)
    if bytes(buffer).strip():
        raise _SseDiagnosticError("sse_trailing_bytes")


async def _parse_success_response_v3(
    response: httpx.Response,
    *,
    api_key: str,
    latency_start_ns: int,
    allowed_tool_names: frozenset[str],
) -> KimiChatResponseV3:
    """Parse only the documented Kimi same-chunk terminal/usage layout."""

    if _v1._media_type(response) != "text/event-stream":
        raise _response_invalid("media_type_not_event_stream")
    if not _v1._content_encoding_is_identity(response):
        raise _response_invalid("content_encoding_not_identity")

    completion_id: str | None = None
    created: int | None = None
    role: str | None = None
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    content_started = False
    tool_builders: dict[int, _v1._ToolCallBuilder] = {}
    finish_reason: str | None = None
    usage: KimiChatUsageV3 | None = None
    terminal_seen = False
    done_seen = False
    decode_diagnostic_code: str | None = None

    try:
        async for data in _iter_sse_data_v3(response):
            if data == "[DONE]":
                if done_seen:
                    raise _response_error("kimi_chat_stream_incomplete")
                if not terminal_seen:
                    raise _response_error(
                        "kimi_chat_stream_incomplete", outcome_unknown=True
                    )
                done_seen = True
                continue
            if done_seen:
                raise _response_error("kimi_chat_stream_incomplete")
            if terminal_seen:
                raise _response_invalid("data_after_terminal")

            payload = _decode_json_object_v3(data.encode("utf-8"))
            raw_usage = payload.get("usage")
            raw_choices = payload.get("choices")

            # The OpenAI-style empty-choices usage trailer is intentionally
            # incompatible with this successor contract.
            if isinstance(raw_choices, list) and not raw_choices:
                if raw_usage is not None:
                    raise _response_error("kimi_chat_usage_invalid")
                raise _response_invalid("empty_choices_without_usage")
            if not isinstance(raw_choices, list):
                raise _response_invalid("choices_not_array")
            if len(raw_choices) != 1:
                raise _response_invalid("choices_count_not_one")

            if not isinstance(raw_choices[0], dict):
                raise _response_invalid("choice_not_object")
            choice_value = raw_choices[0]
            raw_finish = choice_value.get("finish_reason")
            is_terminal = raw_finish is not None
            expected_choice_fields = (
                _TERMINAL_CHOICE_FIELDS_WITH_USAGE
                if is_terminal and "usage" in choice_value
                else _CHOICE_FIELDS
            )
            choice = _validate_exact_fields(
                choice_value,
                expected_choice_fields,
                diagnostic_code="choice_field_set_invalid",
            )
            expected_top_level = (
                _TERMINAL_TOP_LEVEL_FIELDS
                if is_terminal and raw_usage is not None
                else _TOP_LEVEL_FIELDS
            )
            _validate_exact_fields(
                payload,
                expected_top_level,
                diagnostic_code="top_level_field_set_invalid",
            )

            raw_id = payload["id"]
            if (
                not isinstance(raw_id, str)
                or not _v1._SAFE_PROVIDER_ID.fullmatch(raw_id)
            ):
                raise _response_invalid("completion_id_invalid")
            if completion_id is None:
                completion_id = raw_id
            elif raw_id != completion_id:
                raise _response_invalid("completion_id_changed")
            if payload["object"] != "chat.completion.chunk":
                raise _response_invalid("object_invalid")
            if payload["model"] != KIMI_CHAT_MODEL_ID:
                raise _response_invalid("model_mismatch")
            raw_created = payload["created"]
            if type(raw_created) is not int or raw_created < 0:
                raise _response_invalid("created_invalid")
            if created is None:
                created = raw_created
            elif raw_created != created:
                raise _response_invalid("created_changed")

            if choice["index"] != 0:
                raise _response_invalid("choice_index_invalid")
            delta = choice["delta"]
            if not isinstance(delta, dict):
                raise _response_invalid("delta_not_object")
            if not set(delta).issubset(_DELTA_FIELDS):
                raise _response_invalid("delta_field_set_invalid")

            raw_role = delta.get("role")
            if raw_role is not None:
                if raw_role != "assistant":
                    raise _response_invalid("assistant_role_invalid")
                if role is not None:
                    raise _response_invalid("assistant_role_repeated")
                role = raw_role
            reasoning = delta.get("reasoning_content")
            if reasoning is not None:
                if not isinstance(reasoning, str):
                    raise _response_invalid("reasoning_content_not_string")
                if content_started:
                    raise _response_invalid("reasoning_after_content")
                reasoning_parts.append(reasoning)
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise _response_invalid("content_not_string")
                if content:
                    content_started = True
                content_parts.append(content)
            if "tool_calls" in delta:
                _apply_tool_fragments_v3(tool_builders, delta["tool_calls"])

            if is_terminal:
                if raw_finish not in _COMPLETE_FINISH_REASONS:
                    raise _response_invalid("finish_reason_invalid")
                top_usage = (
                    _parse_usage_v3(raw_usage) if raw_usage is not None else None
                )
                choice_usage = (
                    _parse_usage_v3(choice["usage"])
                    if "usage" in choice
                    else None
                )
                # Finish and exact usage become trusted atomically.  No tool
                # call can escape this parser until the later DONE check.
                usage = _reconcile_usage_projections(top_usage, choice_usage)
                finish_reason = raw_finish
                terminal_seen = True
            elif raw_usage is not None or "usage" in choice:
                raise _response_error("kimi_chat_usage_invalid")
    except KimiChatTransportError:
        raise
    except (_SseDiagnosticError, _JsonDiagnosticError) as exc:
        decode_diagnostic_code = exc.code
    except httpx.DecodingError:
        decode_diagnostic_code = "success_stream_decoding_error"

    # Raise only after leaving the decoder exception handler.  This keeps both
    # ``__cause__`` and ``__context__`` empty so traceback formatting cannot
    # reveal decoder messages, offsets or raw fragments.
    if decode_diagnostic_code is not None:
        raise _response_invalid(decode_diagnostic_code)

    if not done_seen:
        raise _response_error(
            "kimi_chat_stream_incomplete",
            usage=usage,
            outcome_unknown=True,
        )
    if not terminal_seen or finish_reason is None:
        raise _response_error("kimi_chat_stream_incomplete", outcome_unknown=True)
    if usage is None:
        # Defensive: same-chunk parsing makes this unreachable unless the
        # implementation drifts.
        raise _response_error("kimi_chat_usage_missing", outcome_unknown=True)
    if completion_id is None:
        raise _response_invalid(
            "completion_id_missing_after_terminal", usage=usage
        )
    if role != "assistant":
        raise _response_invalid(
            "assistant_role_missing_after_terminal", usage=usage
        )

    tool_calls = _v1._finalize_tool_calls(
        tool_builders,
        api_key=api_key,
        allowed_names=allowed_tool_names,
    )
    if finish_reason == "tool_calls" and not tool_calls:
        raise _response_error("kimi_chat_tool_protocol_invalid", usage=usage)
    if finish_reason != "tool_calls" and tool_calls:
        raise _response_error("kimi_chat_tool_protocol_invalid", usage=usage)
    if finish_reason == "length":
        raise _response_error("kimi_chat_output_limit_reached", usage=usage)
    if finish_reason == "stop" and not any(
        part.strip() for part in content_parts
    ):
        raise _response_invalid("stop_content_blank", usage=usage)

    assistant = KimiAssistantMessage(
        content="".join(content_parts) if content_parts else None,
        reasoning_content="".join(reasoning_parts) if reasoning_parts else None,
        tool_calls=tool_calls,
    )
    return KimiChatResponseV3(
        assistant_message=assistant,
        finish_reason=finish_reason,
        usage=usage,
        latency_ms=max(
            0, (time.perf_counter_ns() - latency_start_ns) // 1_000_000
        ),
        http_status=200,
        http_attempts=1,
        network_calls=1,
    )


async def run_kimi_chat_completion_v3(
    request: KimiChatRequest,
    *,
    api_key: str | None = None,
    confirm_online: bool = False,
    _key_loader: Callable[[], str | None] | None = None,
    _transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> KimiChatResponseV3:
    """Perform one owned fixed-origin request using the v3 diagnostic parser.

    There are no environment reads, redirects, retries, fallbacks, callbacks or
    raw persistence.  Underscore arguments are offline test seams only.
    """

    if not isinstance(request, KimiChatRequest):
        raise KimiChatTransportError("kimi_chat_request_invalid")
    if api_key is not None and _key_loader is not None:
        raise KimiChatTransportError("kimi_chat_configuration_invalid")
    if confirm_online is not True:
        raise KimiChatTransportError("kimi_chat_confirmation_required")
    try:
        body = request.to_body_bytes()
    except Exception as exc:
        raise KimiChatTransportError("kimi_chat_request_invalid") from exc
    if len(body) > _v1._MAX_REQUEST_BODY_BYTES:
        raise KimiChatTransportError("kimi_chat_request_too_large")
    if not _configuration_is_valid_v3():
        raise KimiChatTransportError("kimi_chat_configuration_invalid")
    if _key_loader is not None:
        try:
            api_key = _key_loader()
        except Exception as exc:
            raise KimiChatTransportError("kimi_chat_configuration_invalid") from exc
    if not isinstance(api_key, str) or not api_key or api_key.isspace():
        raise KimiChatTransportError("kimi_chat_key_missing")
    if not _v1._is_safe_api_key(api_key):
        raise KimiChatTransportError("kimi_chat_key_invalid")

    transport: httpx.AsyncBaseTransport | None = None
    client: httpx.AsyncClient | None = None
    result: KimiChatResponseV3 | None = None
    primary_error: KimiChatTransportError | None = None
    cancelled: asyncio.CancelledError | None = None
    attempts = 0
    started_ns = time.perf_counter_ns()
    try:
        try:
            transport = (_transport_factory or _v1._default_transport_factory)()
            client = _v1._build_client(transport)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:
            primary_error = KimiChatTransportError("kimi_chat_failed")
        if client is not None and cancelled is None and primary_error is None:
            attempts = 1
            try:
                async with asyncio.timeout(_v1._REQUEST_DEADLINE_SECONDS):
                    async with client.stream(
                        "POST",
                        KIMI_CHAT_PATH,
                        headers=_v1._request_headers(api_key),
                        content=body,
                    ) as response:
                        if response.status_code == 200:
                            try:
                                result = await _parse_success_response_v3(
                                    response,
                                    api_key=api_key,
                                    latency_start_ns=started_ns,
                                    allowed_tool_names=frozenset(
                                        tool.name for tool in request.tools
                                    ),
                                )
                            except KimiChatTransportError as exc:
                                if exc.http_status is None:
                                    exc.http_status = 200
                                if exc.request_id_sha256 is None:
                                    exc.request_id_sha256 = _v1._request_id_hash(
                                        response, api_key
                                    )
                                raise
                        else:
                            primary_error = await _v1._classify_http_error(
                                response, api_key=api_key
                            )
            except asyncio.CancelledError as exc:
                cancelled = exc
            except KimiChatTransportError as exc:
                if exc.network_calls == 0:
                    exc.network_calls = attempts
                primary_error = exc
            except (TimeoutError, httpx.TimeoutException):
                primary_error = KimiChatTransportError(
                    "kimi_chat_timeout", network_calls=1, outcome_unknown=True
                )
            except httpx.DecodingError as exc:
                primary_error = _attach_diagnostic(
                    KimiChatTransportError(
                        "kimi_chat_response_invalid",
                        http_status=200,
                        network_calls=1,
                    ),
                    "transport_decoding_error",
                )
            except httpx.RequestError:
                primary_error = KimiChatTransportError(
                    "kimi_chat_network_failed",
                    network_calls=1,
                    outcome_unknown=True,
                )
            except Exception:
                primary_error = KimiChatTransportError(
                    "kimi_chat_failed", network_calls=attempts
                )
    finally:
        close_failed = False
        try:
            if client is not None:
                async with asyncio.timeout(_v1._CLOSE_TIMEOUT_SECONDS):
                    await client.aclose()
            elif transport is not None:
                async with asyncio.timeout(_v1._CLOSE_TIMEOUT_SECONDS):
                    await transport.aclose()
        except asyncio.CancelledError as exc:
            if cancelled is None:
                cancelled = exc
        except Exception:
            close_failed = True
        finally:
            api_key = None
        if close_failed and primary_error is None and cancelled is None:
            primary_error = KimiChatTransportError(
                "kimi_chat_client_close_failed", network_calls=attempts
            )

    if cancelled is not None:
        raise cancelled
    if primary_error is not None:
        raise primary_error
    if result is None:
        raise KimiChatTransportError("kimi_chat_failed", network_calls=attempts)
    return result


def kimi_chat_completions_v3_contract() -> dict[str, Any]:
    """Return the exact machine-readable offline v3 transport contract."""

    return {
        "schema_version": "3.0",
        "contract_id": "eval-v2-kimi-chat-completions-v3",
        "status": "implemented_offline_tested_not_run",
        "predecessor": {
            "contract_id": "eval-v2-kimi-chat-completions-v2",
            "contract_path": "evals/v2/kimi_chat_completions_contract_v2.json",
            "contract_sha256": (
                "eb226578df2555813fbef005e366bb014d05bbb0dfde039b170689fc5a00916c"
            ),
            "transport_source_path": "src/researchops/kimi_chat_transport_v2.py",
            "transport_source_sha256": (
                "0e62e6b696a43804ef36bd1b7c1422cb0b9d7a974544d2afe50f5b7c6e2af8ae"
            ),
            "results_inherited": False,
            "authorization_inherited": False,
        },
        "implementation": {
            "module": "researchops.kimi_chat_transport_v3",
            "source_path": "src/researchops/kimi_chat_transport_v3.py",
            "parser_version": KIMI_CHAT_PARSER_VERSION,
            "transport_id": KIMI_CHAT_TRANSPORT_V3_ID,
            "entrypoint": "run_kimi_chat_completion_v3",
            "success_parser": "_parse_success_response_v3",
            "v1_success_parser_called": False,
            "v2_success_parser_called": False,
            "v2_acceptance_semantics_preserved": True,
            "shared_v1_boundaries": [
                "request_and_message_value_objects",
                "fixed_tls_and_http_client_controls",
                "stable_http_error_classifier",
                "identifier_hashing_and_redaction",
            ],
            "successor_owned_boundaries": [
                "bounded_sse_decoder_with_fixed_diagnostic_exceptions",
                "strict_json_decoder_with_fixed_diagnostic_exceptions",
                "v2_equivalent_success_stream_state_machine",
            ],
        },
        "provider": {
            "provider_id": "moonshot_kimi",
            "model_id": "kimi-k3",
            "transport_id": KIMI_CHAT_TRANSPORT_V3_ID,
            "api_origin": "https://api.moonshot.cn",
            "path": "/v1/chat/completions",
            "api_key_environment_variable": "MOONSHOT_API_KEY",
            "generic_provider_registry_enabled": False,
        },
        "official_source_commitments": {
            "capture_method": KIMI_CHAT_V3_SOURCE_CAPTURE_METHOD,
            "sources": [
                {
                    "source_id": source.source_id,
                    "url": source.url,
                    "captured_at_utc": source.captured_at_utc,
                    "decoded_bytes": source.decoded_bytes,
                    "sha256": source.sha256,
                }
                for source in KIMI_CHAT_V3_OFFICIAL_SOURCE_COMMITMENTS
            ],
            "authorizes_provider_call": False,
            "is_effective_terms_evidence": False,
        },
        "request_contract": {
            "method": "POST",
            "content_type": "application/json",
            "accept": "text/event-stream",
            "accept_encoding": "identity",
            "canonical_json_required": True,
            "max_canonical_body_bytes": 6144,
            "model": "kimi-k3",
            "reasoning_effort": "low",
            "stream": True,
            "stream_options_include_usage": True,
            "max_completion_tokens_max": 1536,
            "tool_choice_allowlist": [
                "required",
                "none",
                "specified_allowlisted_function",
            ],
            "explicit_fixed_sampling_parameters_sent": False,
            "files_images_video_allowed": False,
            "hosted_tools_search_or_memory_allowed": False,
            "arbitrary_extra_body_allowed": False,
        },
        "message_contract": {
            "allowed_roles": ["system", "user", "assistant", "tool"],
            "complete_assistant_message_replay_required": True,
            "reasoning_content_replayed_in_memory": True,
            "reasoning_content_persisted": False,
            "tool_calls_replayed_in_memory": True,
            "raw_prompt_or_output_persisted": False,
            "unmatched_or_duplicate_tool_call_ids_allowed": False,
        },
        "tool_contract": {
            "tools_per_request_max": 6,
            "tool_calls_per_response_max": 6,
            "argument_utf8_bytes_max": 4096,
            "strict_schema_required": True,
            "additional_properties_allowed": False,
            "function_call_legacy_fields_allowed": False,
            "whole_batch_validated_before_execution": True,
            "sequential_execution_required": True,
            "parallel_execution_allowed": False,
            "execution_before_terminal_usage_and_done_validation_allowed": False,
        },
        "stream_contract": {
            "media_type": "text/event-stream",
            "content_encoding": "identity",
            "event_decoded_bytes_max": 65536,
            "response_decoded_bytes_max": 524288,
            "strict_json_duplicate_keys_rejected": True,
            "strict_json_nonfinite_numbers_rejected": True,
            "unknown_protocol_fields_allowed": False,
            "stable_completion_id_required": True,
            "exact_response_model_required": True,
            "stable_created_integer_required": True,
            "choice_count": 1,
            "choice_index": 0,
            "terminal_choice_nonempty_required": True,
            "terminal_finish_and_usage_same_event_required": True,
            "terminal_usage_projection_allowlist": [
                "top_level_only",
                "choice_level_only",
                "both_reconciled",
            ],
            "openai_empty_choices_usage_only_allowed": False,
            "allowed_complete_finish_reasons": ["stop", "tool_calls"],
            "length_finish_reason_is_failure": True,
            "finish_reason_exact_count": 1,
            "logical_usage_exact_count": 1,
            "done_marker_exact_count": 1,
            "done_must_immediately_follow_terminal_data": True,
            "data_after_terminal_allowed": False,
            "partial_or_missing_done_is_failure": True,
            "partial_tool_result_exposed": False,
        },
        "usage_contract": {
            "required_fields": [
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
            ],
            "optional_fields": ["cached_tokens"],
            "unknown_fields_allowed": False,
            "nonnegative_integer_fields_required": True,
            "total_equals_prompt_plus_completion": True,
            "cached_not_greater_than_prompt": True,
            "both_projection_reconciliation": {
                "core_three_fields_must_match": True,
                "cached_must_match_when_reported_by_both": True,
                "cached_reported_by_one_projection_is_adopted": True,
                "cached_omitted_by_one_projection_is_not_a_conflict": True,
            },
            "cached_missing_internal_conservative_value": 0,
            "cached_missing_receipt_projection": None,
            "cached_missing_budget_assumption": "all_input_uncached",
            "cache_discount_requires_provider_reported_cached_tokens": True,
            "cache_discount_may_relax_local_reservation": False,
            "usage_missing_stops_run": True,
            "usage_conflict_stops_run": True,
            "reasoning_tokens_included_in_provider_completion_usage": True,
        },
        "transport_controls": {
            "owned_http_client_per_attempt": True,
            "http2_enabled": False,
            "trust_environment": False,
            "tls_verification_required": True,
            "follow_redirects": False,
            "client_retries": 0,
            "fallbacks_allowed": False,
            "connect_timeout_seconds": 5,
            "read_timeout_seconds": 90,
            "write_timeout_seconds": 5,
            "pool_timeout_seconds": 5,
            "request_deadline_seconds": 90,
            "close_timeout_seconds": 5,
            "request_body_bytes_max": 6144,
            "error_body_bytes_max": 65536,
            "tool_argument_bytes_max": 4096,
            "external_callbacks_or_tracing_allowed": False,
            "http_debug_logging_allowed": False,
        },
        "error_contract": {
            "raw_error_body_persisted": False,
            "raw_provider_message_persisted": False,
            "html_redirect_or_504_body_read": False,
            "retry_after_header_persisted": False,
            "retry_after_followed": False,
            "timeout_or_network_outcome_unknown": True,
            "missing_done_outcome_unknown": True,
            "premature_done_without_terminal_outcome_unknown": True,
            "missing_terminal_usage_outcome_unknown": True,
            "primary_error_preserved_over_close_error": True,
            "stable_codes": list(_v1.KIMI_CHAT_STABLE_ERROR_CODES),
        },
        "response_validation_diagnostic_contract": {
            "schema_version": RESPONSE_VALIDATION_DIAGNOSTIC_SCHEMA_VERSION,
            "field_name": "response_validation_diagnostic",
            "object_exact_fields": ["schema_version", "code"],
            "required_only_for_error_code": "kimi_chat_response_invalid",
            "must_be_null_for_other_errors": True,
            "codes": sorted(RESPONSE_VALIDATION_DIAGNOSTIC_CODES),
            "source": "fixed_local_validation_branch_only",
            "v2_acceptance_or_precedence_changed": False,
            "causal_provider_fault_claim_allowed": False,
            "raw_header_body_or_identifier_persisted": False,
            "actual_field_name_value_offset_or_size_persisted": False,
            "free_text_exception_persisted": False,
        },
        "security_and_receipt_boundary": {
            "api_key_loaded_after_local_gates": True,
            "api_key_persisted_logged_or_hashed": False,
            "authorization_header_persisted": False,
            "raw_request_id_persisted": False,
            "request_id_hash_persisted": False,
            "request_id_hash_exposed_on_success_response": False,
            "request_id_hash_scope": "local_correlation_only",
            "raw_completion_id_persisted": False,
            "completion_id_hash_persisted": False,
            "completion_id_hash_exposed_on_success_response": False,
            "raw_assistant_content_persisted": False,
            "raw_reasoning_content_persisted": False,
            "raw_tool_arguments_or_results_persisted": False,
            "response_validation_diagnostic_is_fixed_enum_only": True,
            "model_quality_claim_allowed": False,
            "authorizes_provider_registration": False,
            "authorizes_public_regression": False,
            "authorizes_private_evaluation": False,
            "non_synthetic_release_supported": False,
        },
        "audit_activity": {
            "live_chat_requests_performed": 0,
            "model_token_calls": 0,
            "api_key_read_or_used": False,
            "usage": None,
            "cost": None,
        },
    }


__all__ = (
    "KIMI_CHAT_API_ORIGIN",
    "KIMI_CHAT_MODEL_ID",
    "KIMI_CHAT_PARSER_VERSION",
    "KIMI_CHAT_PATH",
    "KIMI_CHAT_TRANSPORT_V3_ID",
    "KIMI_CHAT_V3_OFFICIAL_SOURCE_COMMITMENTS",
    "KIMI_CHAT_V3_SOURCE_CAPTURE_METHOD",
    "RESPONSE_VALIDATION_DIAGNOSTIC_CODES",
    "RESPONSE_VALIDATION_DIAGNOSTIC_SCHEMA_VERSION",
    "KimiAssistantMessage",
    "KimiChatRequest",
    "KimiChatOfficialSourceCommitment",
    "KimiChatResponse",
    "KimiChatResponseV3",
    "KimiChatTransportError",
    "KimiChatUsage",
    "KimiChatUsageV3",
    "KimiResponseValidationDiagnostic",
    "KimiFunctionTool",
    "KimiSpecifiedToolChoice",
    "KimiTextMessage",
    "KimiToolCall",
    "KimiToolResultMessage",
    "kimi_chat_completions_v3_contract",
    "response_validation_diagnostic",
    "run_kimi_chat_completion_v3",
)
