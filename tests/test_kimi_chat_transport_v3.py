from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import httpx

from researchops import kimi_chat_transport as v1
from researchops import kimi_chat_transport_v2 as predecessor_v2
from researchops import kimi_chat_transport_v3 as transport_v3
from researchops.kimi_chat_transport_v3 import (
    KIMI_CHAT_API_ORIGIN,
    KIMI_CHAT_MODEL_ID,
    KIMI_CHAT_PARSER_VERSION,
    KIMI_CHAT_PATH,
    KIMI_CHAT_TRANSPORT_V3_ID,
    KIMI_CHAT_V3_OFFICIAL_SOURCE_COMMITMENTS,
    KIMI_CHAT_V3_SOURCE_CAPTURE_METHOD,
    RESPONSE_VALIDATION_DIAGNOSTIC_CODES,
    RESPONSE_VALIDATION_DIAGNOSTIC_SCHEMA_VERSION,
    KimiChatRequest,
    KimiChatTransportError,
    KimiFunctionTool,
    KimiTextMessage,
    KimiToolResultMessage,
    response_validation_diagnostic,
    run_kimi_chat_completion_v3,
)


_KEY = "offline-kimi-v3-key-CANARY-0123456789"
_REQUEST_ID = "request-offline-v3-001"
_COMPLETION_ID = "cmpl-offline-v3-001"
_RAW_CANARY = "RAW-PROVIDER-V3-CANARY"
_OMIT = object()


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _usage(*, cached: object = 20, extra: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
    }
    if cached is not _OMIT:
        result["cached_tokens"] = cached
    if extra:
        result["provider_extension"] = 1
    return result


def _chunk(
    *,
    delta: dict[str, object],
    finish_reason: str | None = None,
    top_usage: object = _OMIT,
    choice_usage: object = _OMIT,
    completion_id: str = _COMPLETION_ID,
    model: str = KIMI_CHAT_MODEL_ID,
    created: object = 1_787_702_400,
    index: object = 0,
) -> dict[str, object]:
    choice: dict[str, object] = {
        "index": index,
        "delta": delta,
        "finish_reason": finish_reason,
    }
    if choice_usage is not _OMIT:
        choice["usage"] = choice_usage
    result: dict[str, object] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [choice],
    }
    if top_usage is not _OMIT:
        result["usage"] = top_usage
    return result


def _usage_only_chunk() -> dict[str, object]:
    return {
        "id": _COMPLETION_ID,
        "object": "chat.completion.chunk",
        "created": 1_787_702_400,
        "model": KIMI_CHAT_MODEL_ID,
        "choices": [],
        "usage": _usage(),
    }


def _event(value: object) -> bytes:
    return b"data: " + _json_bytes(value) + b"\n\n"


def _done() -> bytes:
    return b"data: [DONE]\n\n"


def _stop_stream(
    *,
    projection: str = "top",
    cached: object = 20,
    include_done: bool = True,
) -> bytes:
    usage = _usage(cached=cached)
    kwargs: dict[str, object]
    if projection == "top":
        kwargs = {"top_usage": usage}
    elif projection == "choice":
        kwargs = {"choice_usage": usage}
    elif projection == "both":
        kwargs = {"top_usage": usage, "choice_usage": dict(usage)}
    else:
        raise AssertionError("unknown projection")
    result = b"".join(
        (
            _event(
                _chunk(
                    delta={"role": "assistant", "reasoning_content": "private-r"}
                )
            ),
            _event(
                _chunk(
                    delta={"content": "synthetic answer"},
                    finish_reason="stop",
                    **kwargs,
                )
            ),
        )
    )
    return result + (_done() if include_done else b"")


def _tool_stream(*, include_done: bool = True) -> bytes:
    first = _chunk(
        delta={
            "role": "assistant",
            "reasoning_content": "private-r",
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_metric_0",
                    "type": "function",
                    "function": {
                        "name": "lookup_synthetic_metric",
                        "arguments": '{"metric_',
                    },
                }
            ],
        }
    )
    terminal = _chunk(
        delta={
            "tool_calls": [
                {
                    "index": 0,
                    "function": {"arguments": 'id":"rows"}'},
                }
            ]
        },
        finish_reason="tool_calls",
        choice_usage=_usage(cached=_OMIT),
    )
    body = _event(first) + _event(terminal)
    return body + (_done() if include_done else b"")


def _tool() -> KimiFunctionTool:
    return KimiFunctionTool.from_schema(
        name="lookup_synthetic_metric",
        description="Return one synthetic metric.",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "metric_id": {"type": "string", "enum": ["rows"]}
            },
            "required": ["metric_id"],
        },
    )


def _request() -> KimiChatRequest:
    return KimiChatRequest(
        messages=(KimiTextMessage("user", "Use the synthetic metric."),),
        tools=(_tool(),),
        tool_choice="required",
        max_completion_tokens=1536,
        reasoning_effort="low",
    )


class _TrackingTransport(httpx.MockTransport):
    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


class _CloseFailingTransport(_TrackingTransport):
    async def aclose(self) -> None:
        self.closed = True
        await httpx.MockTransport.aclose(self)
        raise RuntimeError(f"close {_KEY}")


def _response(body: bytes | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        headers={
            "content-type": "text/event-stream; charset=utf-8",
            "msh-request-id": _REQUEST_ID,
        },
        content=_stop_stream() if body is None else body,
    )


def _run(handler, *, request_override: KimiChatRequest | None = None):
    transport = _TrackingTransport(handler)
    result = asyncio.run(
        run_kimi_chat_completion_v3(
            _request() if request_override is None else request_override,
            api_key=_KEY,
            confirm_online=True,
            _transport_factory=lambda: transport,
        )
    )
    return result, transport


class KimiChatTransportV3Tests(unittest.TestCase):
    def test_versioned_api_is_distinct_and_reuses_only_frozen_controls(self) -> None:
        self.assertEqual(KIMI_CHAT_PARSER_VERSION, "3.0")
        self.assertEqual(
            KIMI_CHAT_TRANSPORT_V3_ID,
            "moonshot_direct_chat_completions_sse_v3",
        )
        self.assertNotEqual(KIMI_CHAT_TRANSPORT_V3_ID, v1.KIMI_CHAT_TRANSPORT_ID)
        self.assertIsNot(
            transport_v3._parse_success_response_v3,
            v1._parse_success_response,
        )
        self.assertIn(
            "terminal usage projection allowlist", transport_v3.__doc__ or ""
        )
        self.assertEqual(KIMI_CHAT_API_ORIGIN, "https://api.moonshot.cn")
        self.assertEqual(KIMI_CHAT_MODEL_ID, "kimi-k3")
        self.assertEqual(KIMI_CHAT_PATH, "/v1/chat/completions")

    def test_all_broad_response_failures_emit_one_closed_diagnostic(self) -> None:
        observed: set[str] = set()

        def capture(
            handler,
            expected: str,
            *,
            request_override: KimiChatRequest | None = None,
        ) -> None:
            with self.assertRaises(KimiChatTransportError) as caught:
                _run(handler, request_override=request_override)
            self.assertEqual(caught.exception.code, "kimi_chat_response_invalid")
            diagnostic = response_validation_diagnostic(caught.exception)
            self.assertIsNotNone(diagnostic)
            assert diagnostic is not None
            self.assertEqual(
                diagnostic.to_dict(),
                {
                    "schema_version": RESPONSE_VALIDATION_DIAGNOSTIC_SCHEMA_VERSION,
                    "code": expected,
                },
            )
            self.assertEqual(set(diagnostic.to_dict()), {"schema_version", "code"})
            self.assertNotIn(_KEY, repr(caught.exception))
            self.assertNotIn(_RAW_CANARY, repr(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            formatted = "".join(traceback.format_exception(caught.exception))
            self.assertNotIn(_KEY, formatted)
            self.assertNotIn(_RAW_CANARY, formatted)
            self.assertNotIn("fixed-local-decoder-error", formatted)
            self.assertNotIn("fixed-transport-decoder-error", formatted)
            observed.add(diagnostic.code)

        capture(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b"{}",
            ),
            "media_type_not_event_stream",
        )
        capture(
            lambda request: httpx.Response(
                200,
                headers={
                    "content-type": "text/event-stream",
                    "content-encoding": "gzip",
                },
                content=gzip.compress(_stop_stream()),
            ),
            "content_encoding_not_identity",
        )

        empty_choices = _usage_only_chunk()
        empty_choices.pop("usage")
        choices_not_array = _chunk(delta={"role": "assistant"})
        choices_not_array["choices"] = {}
        choices_count_not_one = _chunk(delta={"role": "assistant"})
        existing_choice = choices_count_not_one["choices"][0]  # type: ignore[index]
        choices_count_not_one["choices"] = [existing_choice, dict(existing_choice)]
        choice_not_object = _chunk(delta={"role": "assistant"})
        choice_not_object["choices"] = [0]
        choice_fields = _chunk(delta={"role": "assistant"})
        choice_fields["choices"][0]["extension"] = _RAW_CANARY  # type: ignore[index]
        top_fields = _chunk(delta={"role": "assistant"})
        top_fields["extension"] = _RAW_CANARY
        object_invalid = _chunk(delta={"role": "assistant"})
        object_invalid["object"] = _RAW_CANARY
        delta_not_object = _chunk(delta={"role": "assistant"})
        delta_not_object["choices"][0]["delta"] = []  # type: ignore[index]

        body_cases = (
            (
                _stop_stream(include_done=False)
                + _event(_chunk(delta={"content": "late"})),
                "data_after_terminal",
            ),
            (_event(empty_choices), "empty_choices_without_usage"),
            (_event(choices_not_array), "choices_not_array"),
            (_event(choices_count_not_one), "choices_count_not_one"),
            (_event(choice_not_object), "choice_not_object"),
            (_event(choice_fields), "choice_field_set_invalid"),
            (_event(top_fields), "top_level_field_set_invalid"),
            (
                _event(_chunk(delta={"role": "assistant"}, completion_id="bad id")),
                "completion_id_invalid",
            ),
            (
                _event(_chunk(delta={"role": "assistant"}))
                + _event(
                    _chunk(
                        delta={"content": "x"},
                        completion_id="cmpl-offline-v3-002",
                    )
                ),
                "completion_id_changed",
            ),
            (_event(object_invalid), "object_invalid"),
            (
                _event(_chunk(delta={"role": "assistant"}, model="alias")),
                "model_mismatch",
            ),
            (
                _event(_chunk(delta={"role": "assistant"}, created=True)),
                "created_invalid",
            ),
            (
                _event(_chunk(delta={"role": "assistant"}, created=1))
                + _event(_chunk(delta={"content": "x"}, created=2)),
                "created_changed",
            ),
            (
                _event(_chunk(delta={"role": "assistant"}, index=1)),
                "choice_index_invalid",
            ),
            (_event(delta_not_object), "delta_not_object"),
            (
                _event(_chunk(delta={"role": "assistant", "extension": 1})),
                "delta_field_set_invalid",
            ),
            (
                _event(_chunk(delta={"role": "user"})),
                "assistant_role_invalid",
            ),
            (
                _event(_chunk(delta={"role": "assistant"}))
                + _event(_chunk(delta={"role": "assistant"})),
                "assistant_role_repeated",
            ),
            (
                _event(
                    _chunk(
                        delta={"role": "assistant", "reasoning_content": 1}
                    )
                ),
                "reasoning_content_not_string",
            ),
            (
                _event(_chunk(delta={"role": "assistant", "content": "x"}))
                + _event(_chunk(delta={"reasoning_content": "late"})),
                "reasoning_after_content",
            ),
            (
                _event(_chunk(delta={"role": "assistant", "content": 1})),
                "content_not_string",
            ),
            (
                _event(
                    _chunk(
                        delta={"role": "assistant"},
                        finish_reason="unknown",
                        top_usage=_usage(),
                    )
                ),
                "finish_reason_invalid",
            ),
            ((b":" + (b"x" * 60_000) + b"\n\n") * 9, "sse_response_too_large"),
            (b"data: " + (b"x" * (64 * 1024)) + b"\n\n", "sse_event_too_large"),
            (b"data: \xff\n\n", "sse_event_utf8_invalid"),
            (b"event: message\n\n", "sse_event_line_invalid"),
            (b"data: {}", "sse_trailing_bytes"),
            (b"data: {bad-json\n\n", "json_syntax_invalid"),
            (b'data: {"id":"x","id":"x"}\n\n', "json_duplicate_key"),
            (b'data: {"value":NaN}\n\n', "json_nonfinite_number"),
            (b"data: []\n\n", "json_top_level_not_object"),
            (
                _event(
                    _chunk(
                        delta={"content": "x"},
                        finish_reason="stop",
                        top_usage=_usage(),
                    )
                )
                + _done(),
                "assistant_role_missing_after_terminal",
            ),
            (
                _event(_chunk(delta={"role": "assistant"}))
                + _event(
                    _chunk(
                        delta={"content": ""},
                        finish_reason="stop",
                        top_usage=_usage(),
                    )
                )
                + _done(),
                "stop_content_blank",
            ),
        )
        for body, expected in body_cases:
            with self.subTest(expected=expected):
                capture(
                    lambda request, body=body: _response(body),
                    expected,
                )

        recursion_request = _request()
        with patch.object(
            transport_v3, "_JSON_LOADS", side_effect=RecursionError
        ):
            capture(
                lambda request: _response(),
                "json_recursion_limit",
                request_override=recursion_request,
            )

        class _DecodingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise httpx.DecodingError("fixed-local-decoder-error")
                yield b""  # pragma: no cover

        capture(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_DecodingStream(),
            ),
            "success_stream_decoding_error",
        )

        with patch.object(
            transport_v3,
            "_parse_success_response_v3",
            side_effect=httpx.DecodingError("fixed-transport-decoder-error"),
        ):
            capture(lambda request: _response(), "transport_decoding_error")

        defensive = transport_v3._response_invalid(
            "completion_id_missing_after_terminal"
        )
        defensive_diagnostic = response_validation_diagnostic(defensive)
        self.assertIsNotNone(defensive_diagnostic)
        assert defensive_diagnostic is not None
        observed.add(defensive_diagnostic.code)

        self.assertEqual(observed, set(RESPONSE_VALIDATION_DIAGNOSTIC_CODES))

    def test_diagnostic_is_absent_for_specific_errors_and_rejects_unknown_codes(self) -> None:
        body = _event(_chunk(delta={"role": "assistant"})) + _done()
        with self.assertRaises(KimiChatTransportError) as caught:
            _run(lambda request: _response(body))
        self.assertEqual(caught.exception.code, "kimi_chat_stream_incomplete")
        self.assertIsNone(response_validation_diagnostic(caught.exception))
        with self.assertRaises(ValueError):
            transport_v3.KimiResponseValidationDiagnostic(code="provider said bad")

    def test_v3_is_acceptance_equivalent_to_v2_on_shared_wire_corpus(self) -> None:
        def outcome(entrypoint, body: bytes) -> tuple[object, ...]:
            request_body_hashes: list[str] = []

            def handler(request: httpx.Request) -> httpx.Response:
                request_body_hashes.append(hashlib.sha256(request.content).hexdigest())
                return _response(body)

            transport = _TrackingTransport(handler)
            try:
                result = asyncio.run(
                    entrypoint(
                        _request(),
                        api_key=_KEY,
                        confirm_online=True,
                        _transport_factory=lambda: transport,
                    )
                )
            except KimiChatTransportError as exc:
                usage = exc.usage.to_dict() if exc.usage is not None else None
                return (
                    "error",
                    exc.code,
                    exc.outcome_unknown,
                    usage,
                    tuple(request_body_hashes),
                )
            return (
                "success",
                result.finish_reason,
                result.usage.to_dict(),
                result.assistant_message.to_api_dict(),
                tuple(request_body_hashes),
            )

        changed_model = _chunk(delta={"role": "assistant"}, model="alias")
        corpus = (
            _stop_stream(projection="top"),
            _stop_stream(projection="choice", cached=_OMIT),
            _stop_stream(projection="both"),
            _tool_stream(),
            _stop_stream(include_done=False),
            _event(_usage_only_chunk()),
            _event(changed_model),
            b"data: {bad-json\n\n",
            _stop_stream(include_done=False)
            + _event(_chunk(delta={"content": "late"})),
        )
        for body in corpus:
            with self.subTest(length=len(body)):
                self.assertEqual(
                    outcome(predecessor_v2.run_kimi_chat_completion_v2, body),
                    outcome(run_kimi_chat_completion_v3, body),
                )

    def test_official_source_commitments_are_exact_and_immutable(self) -> None:
        self.assertEqual(
            KIMI_CHAT_V3_SOURCE_CAPTURE_METHOD,
            "fixed first-party HTTPS GET with identity content encoding; "
            "SHA-256 over decoded response bytes",
        )
        self.assertEqual(
            tuple(
                (
                    source.source_id,
                    source.url,
                    source.captured_at_utc,
                    source.decoded_bytes,
                    source.sha256,
                )
                for source in KIMI_CHAT_V3_OFFICIAL_SOURCE_COMMITMENTS
            ),
            (
                (
                    "chat_completions_documentation",
                    "https://platform.kimi.com/docs/api/chat",
                    "2026-08-26T16:15:03.000Z",
                    826_200,
                    "8aceb197e56b47dc73c1f06377462ed33e36c126f4e7d5459294b0746b94d43a",
                ),
                (
                    "openai_migration_guide",
                    "https://platform.kimi.com/docs/guide/migrating-from-openai-to-kimi",
                    "2026-08-26T16:28:17.000Z",
                    477_544,
                    "e03668deb99a35666293518e33cb80a15e68bc368a03168c0041f0f7f5b8a476",
                ),
                (
                    "streaming_guide",
                    "https://platform.kimi.com/docs/guide/"
                    "utilize-the-streaming-output-feature-of-kimi-api",
                    "2026-08-26T16:28:25.000Z",
                    547_349,
                    "d2ec3b080c02d98889334e29c9965044f8ef941b85171bfd7dfd910f3ed7b3c2",
                ),
            ),
        )
        with self.assertRaises(AttributeError):
            KIMI_CHAT_V3_OFFICIAL_SOURCE_COMMITMENTS[0].sha256 = "0" * 64  # type: ignore[misc]

    def test_top_choice_and_matching_dual_usage_projections_succeed(self) -> None:
        for projection in ("top", "choice", "both"):
            with self.subTest(projection=projection):
                result, transport = _run(
                    lambda request, projection=projection: _response(
                        _stop_stream(projection=projection)
                    )
                )
                self.assertTrue(transport.closed)
                self.assertEqual(result.finish_reason, "stop")
                self.assertEqual(result.assistant_message.content, "synthetic answer")
                self.assertEqual(result.usage.prompt_tokens, 120)
                self.assertEqual(result.usage.cached_tokens, 20)
                self.assertTrue(result.usage.cached_tokens_reported)
                self.assertNotIn("synthetic answer", repr(result))
                self.assertNotIn("private-r", repr(result))

    def test_optional_cached_tokens_is_explicitly_unknown_and_conservative_zero(self) -> None:
        result, _ = _run(
            lambda request: _response(
                _stop_stream(projection="choice", cached=_OMIT)
            )
        )
        self.assertEqual(result.usage.cached_tokens, 0)
        self.assertFalse(result.usage.cached_tokens_reported)
        self.assertIsNone(result.usage.to_dict()["cached_tokens"])
        self.assertEqual(result.usage.prompt_tokens - result.usage.cached_tokens, 120)

    def test_dual_usage_must_be_strictly_identical(self) -> None:
        top = _usage()
        choice = _usage()
        choice["completion_tokens"] = 29
        choice["total_tokens"] = 149
        body = b"".join(
            (
                _event(_chunk(delta={"role": "assistant"})),
                _event(
                    _chunk(
                        delta={"content": "x"},
                        finish_reason="stop",
                        top_usage=top,
                        choice_usage=choice,
                    )
                ),
                _done(),
            )
        )
        with self.assertRaises(KimiChatTransportError) as caught:
            _run(lambda request: _response(body))
        self.assertEqual(caught.exception.code, "kimi_chat_usage_invalid")

    def test_dual_usage_reconciles_optional_cache_without_discount_ambiguity(self) -> None:
        for top_cache, choice_cache, expected_cache, reported in (
            (20, _OMIT, 20, True),
            (_OMIT, 20, 20, True),
            (_OMIT, _OMIT, 0, False),
            (20, 20, 20, True),
        ):
            with self.subTest(top_cache=top_cache, choice_cache=choice_cache):
                body = b"".join(
                    (
                        _event(_chunk(delta={"role": "assistant"})),
                        _event(
                            _chunk(
                                delta={"content": "x"},
                                finish_reason="stop",
                                top_usage=_usage(cached=top_cache),
                                choice_usage=_usage(cached=choice_cache),
                            )
                        ),
                        _done(),
                    )
                )
                result, _ = _run(lambda request, body=body: _response(body))
                self.assertEqual(result.usage.cached_tokens, expected_cache)
                self.assertIs(result.usage.cached_tokens_reported, reported)

        conflict = b"".join(
            (
                _event(_chunk(delta={"role": "assistant"})),
                _event(
                    _chunk(
                        delta={"content": "x"},
                        finish_reason="stop",
                        top_usage=_usage(cached=20),
                        choice_usage=_usage(cached=19),
                    )
                ),
                _done(),
            )
        )
        with self.assertRaises(KimiChatTransportError) as caught:
            _run(lambda request: _response(conflict))
        self.assertEqual(caught.exception.code, "kimi_chat_usage_invalid")

    def test_openai_style_empty_choices_usage_trailer_is_rejected(self) -> None:
        body = b"".join(
            (
                _event(_chunk(delta={"role": "assistant"})),
                _event(
                    _chunk(delta={"content": "x"}, finish_reason="stop")
                ),
                _event(_usage_only_chunk()),
                _done(),
            )
        )
        with self.assertRaises(KimiChatTransportError) as caught:
            _run(lambda request: _response(body))
        self.assertEqual(caught.exception.code, "kimi_chat_usage_missing")
        self.assertTrue(caught.exception.outcome_unknown)

        body = _event(_usage_only_chunk()) + _done()
        with self.assertRaises(KimiChatTransportError) as direct:
            _run(lambda request: _response(body))
        self.assertEqual(direct.exception.code, "kimi_chat_usage_invalid")

    def test_terminal_requires_at_least_one_usage_projection(self) -> None:
        body = b"".join(
            (
                _event(_chunk(delta={"role": "assistant"})),
                _event(
                    _chunk(delta={"content": "x"}, finish_reason="stop")
                ),
                _done(),
            )
        )
        with self.assertRaises(KimiChatTransportError) as caught:
            _run(lambda request: _response(body))
        self.assertEqual(caught.exception.code, "kimi_chat_usage_missing")

    def test_usage_fields_are_exact_typed_and_reconciled(self) -> None:
        cases: list[dict[str, object]] = []
        missing = _usage()
        del missing["total_tokens"]
        cases.append(missing)
        cases.append(_usage(extra=True))
        boolean = _usage()
        boolean["prompt_tokens"] = True
        cases.append(boolean)
        negative = _usage()
        negative["completion_tokens"] = -1
        cases.append(negative)
        bad_total = _usage()
        bad_total["total_tokens"] = 151
        cases.append(bad_total)
        bad_cache = _usage()
        bad_cache["cached_tokens"] = 121
        cases.append(bad_cache)
        for usage in cases:
            with self.subTest(usage=usage):
                body = b"".join(
                    (
                        _event(_chunk(delta={"role": "assistant"})),
                        _event(
                            _chunk(
                                delta={"content": "x"},
                                finish_reason="stop",
                                top_usage=usage,
                            )
                        ),
                        _done(),
                    )
                )
                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(lambda request, body=body: _response(body))
                self.assertEqual(caught.exception.code, "kimi_chat_usage_invalid")

    def test_stop_tool_calls_and_length_share_terminal_chunk_with_usage(self) -> None:
        stop, _ = _run(lambda request: _response(_stop_stream()))
        self.assertEqual(stop.finish_reason, "stop")

        tool, _ = _run(lambda request: _response(_tool_stream()))
        self.assertEqual(tool.finish_reason, "tool_calls")
        self.assertEqual(len(tool.assistant_message.tool_calls), 1)
        self.assertEqual(
            json.loads(tool.assistant_message.tool_calls[0].arguments_json),
            {"metric_id": "rows"},
        )
        self.assertFalse(tool.usage.cached_tokens_reported)

        length_body = b"".join(
            (
                _event(_chunk(delta={"role": "assistant"})),
                _event(
                    _chunk(
                        delta={"content": "partial"},
                        finish_reason="length",
                        top_usage=_usage(),
                    )
                ),
                _done(),
            )
        )
        with self.assertRaises(KimiChatTransportError) as length:
            _run(lambda request: _response(length_body))
        self.assertEqual(length.exception.code, "kimi_chat_output_limit_reached")
        self.assertEqual(length.exception.usage.total_tokens, 150)

    def test_tool_calls_never_escape_before_terminal_usage_and_done(self) -> None:
        for body in (
            _tool_stream(include_done=False),
            _event(
                _chunk(
                    delta={
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_metric_0",
                                "type": "function",
                                "function": {
                                    "name": "lookup_synthetic_metric",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    }
                )
            ),
        ):
            with self.subTest(length=len(body)):
                executed = 0
                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(lambda request, body=body: _response(body))
                # There is no partial-result callback/API through which a
                # caller could execute the parsed tool before validation.
                self.assertEqual(executed, 0)
                self.assertEqual(
                    caught.exception.code, "kimi_chat_stream_incomplete"
                )
                self.assertTrue(caught.exception.outcome_unknown)
                self.assertTrue(caught.exception.outcome_unknown)

    def test_duplicate_terminal_done_and_any_data_after_terminal_are_rejected(self) -> None:
        prefix = _stop_stream(include_done=False)
        terminal = _event(
            _chunk(
                delta={"content": "late"},
                finish_reason="stop",
                top_usage=_usage(),
            )
        )
        bodies = (
            prefix + terminal + _done(),
            prefix + _done() + _done(),
            prefix + _done() + _event(_chunk(delta={"content": "late"})),
        )
        expected = (
            "kimi_chat_response_invalid",
            "kimi_chat_stream_incomplete",
            "kimi_chat_stream_incomplete",
        )
        for body, code in zip(bodies, expected, strict=True):
            with self.subTest(code=code):
                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(lambda request, body=body: _response(body))
                self.assertEqual(caught.exception.code, code)

    def test_done_before_terminal_and_missing_done_fail_closed(self) -> None:
        cases = (
            _event(_chunk(delta={"role": "assistant"})) + _done(),
            _stop_stream(include_done=False),
        )
        for body in cases:
            with self.subTest(length=len(body)):
                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(lambda request, body=body: _response(body))
                self.assertEqual(
                    caught.exception.code, "kimi_chat_stream_incomplete"
                )

    def test_unknown_fields_are_rejected_at_every_protocol_level(self) -> None:
        top = _chunk(delta={"role": "assistant"})
        top["extension"] = 1
        choice = _chunk(delta={"role": "assistant"})
        choice["choices"][0]["extension"] = 1  # type: ignore[index]
        delta = _chunk(delta={"role": "assistant", "extension": 1})
        tool_fragment = _chunk(
            delta={
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_metric_0",
                        "type": "function",
                        "function": {
                            "name": "lookup_synthetic_metric",
                            "arguments": "{}",
                        },
                        "extension": 1,
                    }
                ],
            }
        )
        function_fragment = _chunk(
            delta={
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_metric_0",
                        "type": "function",
                        "function": {
                            "name": "lookup_synthetic_metric",
                            "arguments": "{}",
                            "extension": 1,
                        },
                    }
                ],
            }
        )
        for payload in (top, choice, delta, tool_fragment, function_fragment):
            with self.subTest(keys=tuple(payload)):
                with self.assertRaises(KimiChatTransportError):
                    _run(lambda request, payload=payload: _response(_event(payload)))

    def test_id_model_index_created_and_role_are_exact_and_stable(self) -> None:
        cases = (
            _chunk(delta={"role": "assistant"}, completion_id="bad id"),
            _chunk(delta={"role": "assistant"}, model="kimi-k3-alias"),
            _chunk(delta={"role": "assistant"}, index=1),
            _chunk(delta={"role": "assistant"}, created=True),
            _chunk(delta={"role": "user"}),
        )
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(lambda request, payload=payload: _response(_event(payload)))
                self.assertEqual(caught.exception.code, "kimi_chat_response_invalid")

        changed_id = _stop_stream().replace(
            _COMPLETION_ID.encode(), b"cmpl-offline-v3-002", 1
        )
        with self.assertRaises(KimiChatTransportError):
            _run(lambda request: _response(changed_id))

    def test_tool_fragments_are_strict_complete_bounded_and_allowlisted(self) -> None:
        cases = (
            _tool_stream().replace(
                b'"tool_calls":[{"index":0',
                b'"tool_calls":[{"index":1',
                1,
            ),
            _tool_stream().replace(b'"id":"call_metric_0",', b"", 1),
            _tool_stream().replace(b'lookup_synthetic_metric', b"unknown_tool"),
            _tool_stream().replace(b'id\\\":\\\"rows\\\"}', b"bad-json"),
        )
        for body in cases:
            with self.subTest(length=len(body)):
                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(lambda request, body=body: _response(body))
                self.assertEqual(
                    caught.exception.code, "kimi_chat_tool_protocol_invalid"
                )
                self.assertEqual(caught.exception.http_status, 200)

    def test_malformed_duplicate_nonfinite_truncated_and_caps_are_rejected(self) -> None:
        cases = (
            b'data: {"id":"x","id":"x"}\n\n' + _done(),
            b'data: {"unknown":NaN}\n\n' + _done(),
            b"data: {bad-json\n\n" + _done(),
            b"data: {}",
            b"data: " + (b"x" * (64 * 1024)) + b"\n\n",
            (b":" + (b"x" * 60_000) + b"\n\n") * 9,
        )
        for body in cases:
            with self.subTest(length=len(body)):
                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(lambda request, body=body: _response(body))
                self.assertIn(
                    caught.exception.code,
                    {"kimi_chat_response_invalid", "kimi_chat_stream_incomplete"},
                )

    def test_http_error_taxonomy_is_single_attempt_and_redacted(self) -> None:
        cases = (
            (400, "invalid_request_error", "kimi_chat_invalid_request"),
            (401, "incorrect_api_key_error", "kimi_chat_auth_failed"),
            (403, "permission_denied_error", "kimi_chat_permission_denied"),
            (429, "rate_limit_reached_error", "kimi_chat_rate_limited"),
            (500, "server_error", "kimi_chat_provider_error"),
            (503, "server_unavailable", "kimi_chat_provider_unavailable"),
        )
        for status, provider_type, code in cases:
            calls = 0

            def handler(request, status=status, provider_type=provider_type):
                nonlocal calls
                calls += 1
                return httpx.Response(
                    status,
                    headers={"content-type": "application/json"},
                    content=_json_bytes(
                        {
                            "error": {
                                "type": provider_type,
                                "message": f"{_RAW_CANARY} {_KEY}",
                            }
                        }
                    ),
                )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.subTest(status=status, provider_type=provider_type):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(KimiChatTransportError) as caught:
                        _run(handler)
                self.assertEqual(calls, 1)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(_KEY, repr(caught.exception))
                self.assertNotIn(_RAW_CANARY, repr(caught.exception.to_receipt_fields()))
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")

    def test_redirect_and_timeout_are_never_retried_and_outcome_unknown(self) -> None:
        calls = 0

        def redirect(request):
            nonlocal calls
            calls += 1
            return httpx.Response(
                307,
                headers={"location": "https://evil.invalid/steal"},
                content=f"{_RAW_CANARY}{_KEY}".encode(),
            )

        with self.assertRaises(KimiChatTransportError) as redirected:
            _run(redirect)
        self.assertEqual(calls, 1)
        self.assertEqual(redirected.exception.code, "kimi_chat_redirect_denied")

        calls = 0

        def timeout(request):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout(f"timeout {_KEY}", request=request)

        with self.assertRaises(KimiChatTransportError) as timed_out:
            _run(timeout)
        self.assertEqual(calls, 1)
        self.assertEqual(timed_out.exception.code, "kimi_chat_timeout")
        self.assertTrue(timed_out.exception.outcome_unknown)

    def test_exact_fixed_request_identity_and_no_environment_transport(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                str(request.url), "https://api.moonshot.cn/v1/chat/completions"
            )
            self.assertEqual(request.headers["authorization"], f"Bearer {_KEY}")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "kimi-k3")
            self.assertEqual(payload["reasoning_effort"], "low")
            self.assertEqual(payload["stream_options"], {"include_usage": True})
            self.assertNotIn("temperature", payload)
            return _response()

        result, _ = _run(handler)
        self.assertEqual(result.network_calls, 1)

        mock = _TrackingTransport(lambda request: self.fail("must not request"))
        with patch.object(
            v1.httpx, "AsyncHTTPTransport", return_value=mock
        ) as constructor:
            built = v1._default_transport_factory()
        self.assertIs(built, mock)
        self.assertFalse(constructor.call_args.kwargs["trust_env"])
        self.assertFalse(constructor.call_args.kwargs["http2"])
        self.assertEqual(constructor.call_args.kwargs["retries"], 0)
        asyncio.run(mock.aclose())

    def test_confirmation_and_configuration_fail_before_key_loader(self) -> None:
        loaded = 0

        def loader() -> str:
            nonlocal loaded
            loaded += 1
            return _KEY

        with self.assertRaises(KimiChatTransportError) as confirmation:
            asyncio.run(
                run_kimi_chat_completion_v3(_request(), _key_loader=loader)
            )
        self.assertEqual(confirmation.exception.code, "kimi_chat_confirmation_required")
        self.assertEqual(loaded, 0)

        with patch.object(transport_v3, "KIMI_CHAT_MODEL_ID", "drift"):
            with self.assertRaises(KimiChatTransportError) as config:
                asyncio.run(
                    run_kimi_chat_completion_v3(
                        _request(),
                        confirm_online=True,
                        _key_loader=loader,
                    )
                )
        self.assertEqual(config.exception.code, "kimi_chat_configuration_invalid")
        self.assertEqual(loaded, 0)

    def test_owned_client_close_failure_does_not_hide_primary_error(self) -> None:
        success_transport = _CloseFailingTransport(lambda request: _response())
        with self.assertRaises(KimiChatTransportError) as close:
            asyncio.run(
                run_kimi_chat_completion_v3(
                    _request(),
                    api_key=_KEY,
                    confirm_online=True,
                    _transport_factory=lambda: success_transport,
                )
            )
        self.assertTrue(success_transport.closed)
        self.assertEqual(close.exception.code, "kimi_chat_client_close_failed")

        failure_transport = _CloseFailingTransport(
            lambda request: httpx.Response(401, content=b"never persisted")
        )
        with self.assertRaises(KimiChatTransportError) as primary:
            asyncio.run(
                run_kimi_chat_completion_v3(
                    _request(),
                    api_key=_KEY,
                    confirm_online=True,
                    _transport_factory=lambda: failure_transport,
                )
            )
        self.assertEqual(primary.exception.code, "kimi_chat_auth_failed")

    def test_followup_tool_message_replay_is_in_memory_only(self) -> None:
        result, _ = _run(lambda request: _response(_tool_stream()))
        call = result.assistant_message.tool_calls[0]
        followup = KimiChatRequest(
            messages=(
                KimiTextMessage("user", "Use the synthetic metric."),
                result.assistant_message,
                KimiToolResultMessage(
                    call.call_id,
                    call.name,
                    '{"status":"ok","value":303}',
                ),
            ),
            tools=(_tool(),),
            tool_choice="none",
        )
        self.assertEqual(followup.to_payload()["tool_choice"], "none")
        self.assertNotIn("metric_id", repr(result))
        self.assertNotIn(_KEY, repr(result))
        self.assertFalse(hasattr(result, "request_id_sha256"))
        self.assertFalse(hasattr(result, "completion_id_sha256"))
        self.assertNotIn(hashlib.sha256(_REQUEST_ID.encode()).hexdigest(), repr(result))


if __name__ == "__main__":
    unittest.main()
