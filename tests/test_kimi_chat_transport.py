from __future__ import annotations

import asyncio
import hashlib
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import httpx

from researchops import kimi_chat_transport as transport_module
from researchops.kimi_chat_transport import (
    KIMI_CHAT_API_ORIGIN,
    KIMI_CHAT_MODEL_ID,
    KIMI_CHAT_PATH,
    KIMI_INVALID_REQUEST_PROBE_BODY,
    KIMI_INVALID_REQUEST_PROBE_BODY_SHA256,
    KimiAssistantMessage,
    KimiChatRequest,
    KimiChatTransportError,
    KimiFunctionTool,
    KimiSpecifiedToolChoice,
    KimiTextMessage,
    KimiToolCall,
    KimiToolResultMessage,
    run_kimi_chat_completion,
    run_kimi_invalid_request_probe,
)


_KEY_CANARY = "offline-kimi-chat-key-CANARY-0123456789"
_REQUEST_ID = "4f8ac10b-58cc-4372-a567-0e02b2c3d479"
_COMPLETION_ID = "cmpl-offline-kimi-chat-001"
_RAW_BODY_CANARY = "RAW-PROVIDER-BODY-CANARY"


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _sse_event(value: object, *, crlf: bool = False) -> bytes:
    separator = b"\r\n\r\n" if crlf else b"\n\n"
    return b"data: " + _json_bytes(value) + separator


def _sse_done(*, crlf: bool = False) -> bytes:
    separator = b"\r\n\r\n" if crlf else b"\n\n"
    return b"data: [DONE]" + separator


def _chunk(
    *,
    delta: dict[str, object],
    finish_reason: str | None = None,
    completion_id: str = _COMPLETION_ID,
    model: str = KIMI_CHAT_MODEL_ID,
    index: int = 0,
) -> dict[str, object]:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": 1_787_702_400,
        "model": model,
        "choices": [
            {
                "index": index,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def _usage_chunk(
    *,
    prompt_tokens: object = 120,
    completion_tokens: object = 30,
    total_tokens: object = 150,
    cached_tokens: object = 20,
    completion_id: str = _COMPLETION_ID,
) -> dict[str, object]:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": 1_787_702_400,
        "model": KIMI_CHAT_MODEL_ID,
        "choices": [],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": cached_tokens,
        },
    }


def _stop_stream(*, content: str = "synthetic answer", crlf: bool = False) -> bytes:
    return b"".join(
        (
            _sse_event(
                _chunk(delta={"role": "assistant", "reasoning_content": "r"}),
                crlf=crlf,
            ),
            _sse_event(
                _chunk(delta={"content": content}, finish_reason="stop"),
                crlf=crlf,
            ),
            _sse_event(_usage_chunk(), crlf=crlf),
            _sse_done(crlf=crlf),
        )
    )


def _tool_stream(*, two_tools: bool = False) -> bytes:
    first_calls: list[dict[str, object]] = [
        {
            "index": 0,
            "id": "call_metric_0",
            "type": "function",
            "function": {
                "name": "lookup_synthetic_metric",
                "arguments": '{"metric_',
            },
        }
    ]
    second_calls: list[dict[str, object]] = [
        {"index": 0, "function": {"arguments": 'id":"rows"}'}},
    ]
    if two_tools:
        first_calls.append(
            {
                "index": 1,
                "id": "call_metric_1",
                "type": "function",
                "function": {
                    "name": "lookup_synthetic_metric",
                    "arguments": '{"metric_id":"missing"}',
                },
            }
        )
    return b"".join(
        (
            _sse_event(
                _chunk(
                    delta={
                        "role": "assistant",
                        "reasoning_content": "reason first",
                        "content": "I will use the tool.",
                        "tool_calls": first_calls,
                    }
                )
            ),
            _sse_event(
                _chunk(
                    delta={"tool_calls": second_calls},
                    finish_reason="tool_calls",
                )
            ),
            _sse_event(_usage_chunk()),
            _sse_done(),
        )
    )


def _tool() -> KimiFunctionTool:
    return KimiFunctionTool.from_schema(
        name="lookup_synthetic_metric",
        description="Return one pre-registered synthetic aggregate metric.",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "metric_id": {
                    "type": "string",
                    "enum": ["rows", "missing"],
                }
            },
            "required": ["metric_id"],
        },
    )


def _request(*, content: str = "Use the synthetic metric tool.") -> KimiChatRequest:
    return KimiChatRequest(
        messages=(KimiTextMessage("user", content),),
        tools=(_tool(),),
        tool_choice="required",
        max_completion_tokens=1536,
        reasoning_effort="low",
    )


class _TrackingMockTransport(httpx.MockTransport):
    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


class _CloseFailingTransport(_TrackingMockTransport):
    async def aclose(self) -> None:
        self.closed = True
        await httpx.MockTransport.aclose(self)
        raise RuntimeError(f"close failure {_KEY_CANARY}")


class _HangingCloseTransport(_TrackingMockTransport):
    async def aclose(self) -> None:
        self.closed = True
        await asyncio.Event().wait()


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _success_response(body: bytes | None = None, **kwargs) -> httpx.Response:
    return httpx.Response(
        200,
        headers={
            "content-type": "text/event-stream; charset=utf-8",
            "msh-request-id": _REQUEST_ID,
            **kwargs.pop("headers", {}),
        },
        content=body if body is not None else _stop_stream(),
        **kwargs,
    )


def _run(handler, *, request: KimiChatRequest | None = None, **overrides):
    transport = _TrackingMockTransport(handler)
    kwargs = {
        "api_key": _KEY_CANARY,
        "confirm_online": True,
        "_transport_factory": lambda: transport,
    }
    kwargs.update(overrides)
    result = asyncio.run(
        run_kimi_chat_completion(request or _request(), **kwargs)
    )
    return result, transport


class KimiChatTransportTests(unittest.TestCase):
    def test_invalid_request_probe_is_exact_promptless_and_single_attempt(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                str(request.url), "https://api.moonshot.cn/v1/chat/completions"
            )
            self.assertEqual(request.content, KIMI_INVALID_REQUEST_PROBE_BODY)
            self.assertEqual(len(request.content), 124)
            self.assertEqual(
                hashlib.sha256(request.content).hexdigest(),
                KIMI_INVALID_REQUEST_PROBE_BODY_SHA256,
            )
            payload = json.loads(request.content)
            self.assertNotIn("messages", payload)
            self.assertNotIn("tools", payload)
            self.assertEqual(payload["max_completion_tokens"], 1)
            self.assertEqual(payload["model"], "kimi-k3")
            self.assertEqual(payload["reasoning_effort"], "low")
            return httpx.Response(
                400,
                headers={"content-type": "application/json"},
                content=_json_bytes(
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "message": f"{_RAW_BODY_CANARY} {_KEY_CANARY}",
                        }
                    }
                ),
                request=request,
            )

        result = asyncio.run(
            run_kimi_invalid_request_probe(
                api_key=_KEY_CANARY,
                confirm_online=True,
                _transport_factory=lambda: httpx.MockTransport(handler),
            )
        )
        self.assertEqual(calls, 1)
        self.assertEqual(result.http_status, 400)
        self.assertEqual(result.provider_error_type, "invalid_request_error")
        self.assertNotIn(_KEY_CANARY, repr(result))
        self.assertNotIn(_RAW_BODY_CANARY, repr(result))

    def test_invalid_request_probe_local_gates_precede_key_and_transport(self) -> None:
        def forbidden_loader() -> str:
            self.fail("unconfirmed probe must not load Key")

        with self.assertRaises(KimiChatTransportError) as caught:
            asyncio.run(
                run_kimi_invalid_request_probe(
                    _key_loader=forbidden_loader,
                    _transport_factory=lambda: self.fail(
                        "unconfirmed probe must not create transport"
                    ),
                )
            )
        self.assertEqual(caught.exception.code, "kimi_chat_confirmation_required")
        self.assertEqual(caught.exception.network_calls, 0)

    def test_invalid_request_probe_rejects_mismatch_and_any_other_status_once(self) -> None:
        cases = (
            (
                400,
                _json_bytes(
                    {"error": {"type": "content_filter", "message": "blocked"}}
                ),
                "kimi_chat_invalid_request_probe_mismatch",
                False,
            ),
            (200, b"must-not-be-read", "kimi_chat_invalid_request_probe_unexpected_status", True),
            (401, b"must-not-be-read", "kimi_chat_invalid_request_probe_unexpected_status", False),
            (500, b"must-not-be-read", "kimi_chat_invalid_request_probe_unexpected_status", True),
        )
        for status, body, expected, outcome_unknown in cases:
            with self.subTest(status=status):
                calls = 0
                stream = _ChunkStream([body])

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal calls
                    calls += 1
                    headers = {
                        "content-type": (
                            "application/json" if status == 400 else "text/plain"
                        )
                    }
                    return httpx.Response(
                        status, headers=headers, stream=stream, request=request
                    )

                with self.assertRaises(KimiChatTransportError) as caught:
                    asyncio.run(
                        run_kimi_invalid_request_probe(
                            api_key=_KEY_CANARY,
                            confirm_online=True,
                            _transport_factory=lambda: httpx.MockTransport(handler),
                        )
                    )
                self.assertEqual(calls, 1)
                self.assertEqual(caught.exception.code, expected)
                self.assertIs(caught.exception.outcome_unknown, outcome_unknown)
                if status != 400:
                    self.assertEqual(stream.yielded, 0)
                self.assertTrue(stream.closed)

    def test_confirmation_and_local_request_gates_precede_key_and_transport(self) -> None:
        def forbidden_key_loader() -> str:
            self.fail("unconfirmed request must not load the Key")

        with self.assertRaises(KimiChatTransportError) as caught:
            asyncio.run(
                run_kimi_chat_completion(
                    _request(),
                    _key_loader=forbidden_key_loader,
                    _transport_factory=lambda: self.fail(
                        "unconfirmed request must not create transport"
                    ),
                )
            )
        self.assertEqual(caught.exception.code, "kimi_chat_confirmation_required")
        self.assertEqual(caught.exception.network_calls, 0)

        with self.assertRaises(KimiChatTransportError) as wrong_type:
            asyncio.run(
                run_kimi_chat_completion(
                    object(),
                    api_key=_KEY_CANARY,
                    confirm_online=True,
                    _transport_factory=lambda: self.fail("invalid request must stop"),
                )
            )
        self.assertEqual(wrong_type.exception.code, "kimi_chat_request_invalid")

    def test_key_loader_runs_after_gates_and_key_is_never_returned(self) -> None:
        loaded = 0

        def loader() -> str:
            nonlocal loaded
            loaded += 1
            return _KEY_CANARY

        result, transport = _run(
            lambda request: _success_response(), api_key=None, _key_loader=loader
        )
        self.assertEqual(loaded, 1)
        self.assertTrue(transport.closed)
        self.assertNotIn(_KEY_CANARY, repr(result))

    def test_ambiguous_missing_and_unsafe_key_sources_fail_without_network(self) -> None:
        cases = (
            (
                {"api_key": _KEY_CANARY, "_key_loader": lambda: _KEY_CANARY},
                "kimi_chat_configuration_invalid",
            ),
            ({"api_key": None}, "kimi_chat_key_missing"),
            ({"api_key": ""}, "kimi_chat_key_missing"),
            ({"api_key": " key"}, "kimi_chat_key_invalid"),
            ({"api_key": "key\nvalue"}, "kimi_chat_key_invalid"),
            ({"api_key": "x" * 513}, "kimi_chat_key_invalid"),
        )
        for values, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(KimiChatTransportError) as caught:
                    asyncio.run(
                        run_kimi_chat_completion(
                            _request(),
                            confirm_online=True,
                            _transport_factory=lambda: self.fail(
                                "invalid key must not create transport"
                            ),
                            **values,
                        )
                    )
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.network_calls, 0)

    def test_exact_request_identity_and_fixed_payload(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                str(request.url),
                "https://api.moonshot.cn/v1/chat/completions",
            )
            self.assertEqual(request.headers["authorization"], f"Bearer {_KEY_CANARY}")
            self.assertEqual(request.headers["accept"], "text/event-stream")
            self.assertEqual(request.headers["accept-encoding"], "identity")
            self.assertEqual(request.headers["content-type"], "application/json")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "kimi-k3")
            self.assertEqual(payload["reasoning_effort"], "low")
            self.assertEqual(payload["max_completion_tokens"], 1536)
            self.assertIs(payload["stream"], True)
            self.assertEqual(payload["stream_options"], {"include_usage": True})
            self.assertEqual(payload["tool_choice"], "required")
            self.assertNotIn("temperature", payload)
            self.assertNotIn("thinking", payload)
            self.assertNotIn("prompt_cache_key", payload)
            self.assertLessEqual(len(request.content), 6 * 1024)
            return _success_response()

        result, transport = _run(handler)
        self.assertEqual(calls, 1)
        self.assertTrue(transport.closed)
        self.assertEqual(result.network_calls, 1)

    def test_request_body_accepts_exact_6k_and_rejects_6145(self) -> None:
        base = _request(content="x")
        extra = 6 * 1024 - len(base.to_body_bytes())
        exact = _request(content="x" + ("a" * extra))
        self.assertEqual(len(exact.to_body_bytes()), 6 * 1024)
        with self.assertRaises(ValueError):
            _request(content="x" + ("a" * (extra + 1)))

    def test_request_schema_and_message_sequence_are_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            KimiChatRequest(messages=(), tools=(), tool_choice="none")
        with self.assertRaises(ValueError):
            KimiChatRequest(
                messages=(KimiTextMessage("user", "x"),),
                tools=(),
                tool_choice="required",
            )
        with self.assertRaises(ValueError):
            KimiChatRequest(
                messages=(KimiTextMessage("user", "x"),),
                tools=(_tool(),),
                tool_choice="auto",
            )
        with self.assertRaises(ValueError):
            KimiChatRequest(
                messages=(KimiTextMessage("user", "x"),),
                tools=(_tool(),),
                tool_choice="none",
                reasoning_effort="max",
            )
        with self.assertRaises(ValueError):
            KimiFunctionTool.from_schema(
                name="tool",
                description="bad",
                parameters={"type": "object"},
            )
        unresolved = KimiAssistantMessage(
            content="",
            tool_calls=(KimiToolCall("call_1", "lookup", "{}"),),
        )
        with self.assertRaises(ValueError):
            KimiChatRequest(messages=(unresolved,), tool_choice="none")

    def test_specified_tool_choice_is_typed_and_bound_to_declared_tool(self) -> None:
        request = KimiChatRequest(
            messages=(KimiTextMessage("user", "error probe"),),
            tools=(_tool(),),
            tool_choice=KimiSpecifiedToolChoice("lookup_synthetic_metric"),
        )
        self.assertEqual(
            request.to_payload()["tool_choice"],
            {
                "type": "function",
                "function": {"name": "lookup_synthetic_metric"},
            },
        )
        with self.assertRaises(ValueError):
            KimiChatRequest(
                messages=(KimiTextMessage("user", "x"),),
                tools=(_tool(),),
                tool_choice=KimiSpecifiedToolChoice("undeclared_tool"),
            )

    def test_success_stop_response_requires_usage_and_done_and_is_redacted(self) -> None:
        result, transport = _run(lambda request: _success_response())
        self.assertTrue(transport.closed)
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.assistant_message.content, "synthetic answer")
        self.assertEqual(result.assistant_message.reasoning_content, "r")
        self.assertEqual(result.assistant_message.tool_calls, ())
        self.assertEqual(result.usage.to_dict(), {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "cached_tokens": 20,
        })
        self.assertEqual(
            result.request_id_sha256,
            hashlib.sha256(_REQUEST_ID.encode()).hexdigest(),
        )
        self.assertEqual(
            result.completion_id_sha256,
            hashlib.sha256(_COMPLETION_ID.encode()).hexdigest(),
        )
        serialized = repr(result)
        self.assertNotIn("synthetic answer", serialized)
        self.assertNotIn("reasoning_content='r'", serialized)
        self.assertNotIn(_KEY_CANARY, serialized)

    def test_key_related_completion_or_tool_identifiers_are_never_exposed(self) -> None:
        key_id_body = _stop_stream().replace(
            _COMPLETION_ID.encode("utf-8"), _KEY_CANARY.encode("utf-8")
        )
        result, _ = _run(lambda request: _success_response(key_id_body))
        self.assertIsNone(result.completion_id_sha256)
        self.assertNotIn(_KEY_CANARY, repr(result))

        tool_body = _tool_stream().replace(
            b"call_metric_0", _KEY_CANARY.encode("utf-8")
        )
        with self.assertRaises(KimiChatTransportError) as caught:
            _run(lambda request: _success_response(tool_body))
        self.assertEqual(
            caught.exception.code, "kimi_chat_tool_protocol_invalid"
        )
        self.assertNotIn(_KEY_CANARY, repr(caught.exception))

    def test_crlf_sse_is_supported(self) -> None:
        result, _ = _run(lambda request: _success_response(_stop_stream(crlf=True)))
        self.assertEqual(result.finish_reason, "stop")

    def test_tool_fragments_are_assembled_and_complete_message_round_trips(self) -> None:
        result, _ = _run(lambda request: _success_response(_tool_stream()))
        self.assertEqual(result.finish_reason, "tool_calls")
        message = result.assistant_message
        self.assertEqual(message.content, "I will use the tool.")
        self.assertEqual(message.reasoning_content, "reason first")
        self.assertEqual(len(message.tool_calls), 1)
        call = message.tool_calls[0]
        self.assertEqual(call.call_id, "call_metric_0")
        self.assertEqual(call.name, "lookup_synthetic_metric")
        self.assertEqual(json.loads(call.arguments_json), {"metric_id": "rows"})
        api_message = message.to_api_dict()
        self.assertEqual(api_message["reasoning_content"], "reason first")
        self.assertEqual(api_message["tool_calls"][0]["id"], "call_metric_0")

        followup = KimiChatRequest(
            messages=(
                KimiTextMessage("user", "Use the tool."),
                message,
                KimiToolResultMessage(
                    "call_metric_0",
                    "lookup_synthetic_metric",
                    '{"status":"ok","value":303}',
                ),
            ),
            tools=(_tool(),),
            tool_choice="none",
        )
        self.assertEqual(followup.to_payload()["tool_choice"], "none")
        self.assertIn("reasoning_content", followup.to_payload()["messages"][1])

    def test_two_tool_calls_are_indexed_and_arguments_are_not_in_repr(self) -> None:
        result, _ = _run(
            lambda request: _success_response(_tool_stream(two_tools=True))
        )
        self.assertEqual(len(result.assistant_message.tool_calls), 2)
        self.assertNotIn("metric_id", repr(result.assistant_message.tool_calls))

    def test_wrong_media_encoding_identity_and_model_fields_fail_before_trust(self) -> None:
        cases = (
            (
                lambda: httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    content=_stop_stream(),
                ),
                "kimi_chat_response_invalid",
            ),
            (
                lambda: httpx.Response(
                    200,
                    headers={
                        "content-type": "text/event-stream",
                        "content-encoding": "gzip",
                    },
                    content=b"not-gzip",
                ),
                "kimi_chat_response_invalid",
            ),
            (
                lambda: _success_response(
                    b"".join(
                        (
                            _sse_event(
                                _chunk(
                                    delta={"role": "assistant"},
                                    model="kimi-k3-alias",
                                    finish_reason="stop",
                                )
                            ),
                            _sse_event(_usage_chunk()),
                            _sse_done(),
                        )
                    )
                ),
                "kimi_chat_response_invalid",
            ),
        )
        for factory, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(lambda request, factory=factory: factory())
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.http_status, 200)

    def test_duplicate_nonfinite_and_malformed_event_json_are_rejected(self) -> None:
        bodies = (
            b'data: {"id":"x","id":"x"}\n\n' + _sse_done(),
            b'data: {"unknown":NaN}\n\n' + _sse_done(),
            b"data: {not-json\n\n" + _sse_done(),
            b"event: message\ndata: {}\n\n" + _sse_done(),
        )
        for body in bodies:
            with self.subTest(body=body[:30]):
                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(lambda request, body=body: _success_response(body))
                self.assertEqual(caught.exception.code, "kimi_chat_response_invalid")

    def test_done_is_required_unique_and_terminal(self) -> None:
        complete_without_done = _stop_stream().replace(_sse_done(), b"")
        duplicate_done = _stop_stream() + _sse_done()
        event_after_done = _stop_stream() + _sse_event(_usage_chunk())
        for body in (complete_without_done, duplicate_done, event_after_done):
            with self.subTest(length=len(body)):
                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(lambda request, body=body: _success_response(body))
                self.assertEqual(caught.exception.code, "kimi_chat_stream_incomplete")

    def test_choice_after_finish_and_empty_stop_are_rejected(self) -> None:
        after_finish = _stop_stream().replace(
            _sse_event(_usage_chunk()),
            _sse_event(_chunk(delta={"content": "late"}))
            + _sse_event(_usage_chunk()),
        )
        empty_stop = b"".join(
            (
                _sse_event(
                    _chunk(delta={"role": "assistant"}, finish_reason="stop")
                ),
                _sse_event(_usage_chunk()),
                _sse_done(),
            )
        )
        for body in (after_finish, empty_stop):
            with self.subTest(length=len(body)):
                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(lambda request, body=body: _success_response(body))
                self.assertEqual(caught.exception.code, "kimi_chat_response_invalid")

    def test_usage_is_required_unique_and_reconciled(self) -> None:
        no_usage = b"".join(
            (
                _sse_event(
                    _chunk(delta={"role": "assistant"}, finish_reason="stop")
                ),
                _sse_done(),
            )
        )
        duplicate_usage = _stop_stream().replace(
            _sse_done(), _sse_event(_usage_chunk()) + _sse_done()
        )
        invalid_usages = (
            _usage_chunk(total_tokens=151),
            _usage_chunk(cached_tokens=121),
            _usage_chunk(prompt_tokens=True),
        )
        with self.assertRaises(KimiChatTransportError) as missing:
            _run(lambda request: _success_response(no_usage))
        self.assertEqual(missing.exception.code, "kimi_chat_usage_missing")
        with self.assertRaises(KimiChatTransportError) as duplicate:
            _run(lambda request: _success_response(duplicate_usage))
        self.assertEqual(duplicate.exception.code, "kimi_chat_usage_invalid")
        for usage in invalid_usages:
            body = b"".join(
                (
                    _sse_event(
                        _chunk(delta={"role": "assistant"}, finish_reason="stop")
                    ),
                    _sse_event(usage),
                    _sse_done(),
                )
            )
            with self.subTest(usage=usage["usage"]):
                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(lambda request, body=body: _success_response(body))
                self.assertEqual(caught.exception.code, "kimi_chat_usage_invalid")

    def test_length_is_controlled_failure_with_usage(self) -> None:
        body = b"".join(
            (
                _sse_event(
                    _chunk(delta={"role": "assistant"}, finish_reason="length")
                ),
                _sse_event(_usage_chunk()),
                _sse_done(),
            )
        )
        with self.assertRaises(KimiChatTransportError) as caught:
            _run(lambda request: _success_response(body))
        self.assertEqual(caught.exception.code, "kimi_chat_output_limit_reached")
        self.assertEqual(caught.exception.usage.total_tokens, 150)

    def test_reasoning_after_content_is_protocol_invalid(self) -> None:
        body = b"".join(
            (
                _sse_event(
                    _chunk(delta={"role": "assistant", "content": "answer"})
                ),
                _sse_event(
                    _chunk(
                        delta={"reasoning_content": "late"},
                        finish_reason="stop",
                    )
                ),
                _sse_event(_usage_chunk()),
                _sse_done(),
            )
        )
        with self.assertRaises(KimiChatTransportError) as caught:
            _run(lambda request: _success_response(body))
        self.assertEqual(caught.exception.code, "kimi_chat_response_invalid")

    def test_tool_protocol_rejects_gap_incomplete_invalid_json_and_wrong_finish(self) -> None:
        fragments = (
            [{"index": 1, "id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
            [{"index": 0, "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
            [{"index": 0, "id": "call_0", "type": "function", "function": {"name": "lookup", "arguments": "{bad"}}],
        )
        for tool_calls in fragments:
            body = b"".join(
                (
                    _sse_event(
                        _chunk(
                            delta={"role": "assistant", "tool_calls": tool_calls},
                            finish_reason="tool_calls",
                        )
                    ),
                    _sse_event(_usage_chunk()),
                    _sse_done(),
                )
            )
            with self.subTest(tool_calls=tool_calls):
                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(lambda request, body=body: _success_response(body))
                self.assertEqual(
                    caught.exception.code, "kimi_chat_tool_protocol_invalid"
                )

        wrong_finish = _tool_stream().replace(b'"finish_reason":"tool_calls"', b'"finish_reason":"stop"')
        with self.assertRaises(KimiChatTransportError) as caught:
            _run(lambda request: _success_response(wrong_finish))
        self.assertEqual(caught.exception.code, "kimi_chat_tool_protocol_invalid")

        unknown_tool = _tool_stream().replace(
            b"lookup_synthetic_metric", b"unknown_synthetic_tool"
        )
        with self.assertRaises(KimiChatTransportError) as unknown:
            _run(lambda request: _success_response(unknown_tool))
        self.assertEqual(
            unknown.exception.code, "kimi_chat_tool_protocol_invalid"
        )

    def test_tool_argument_4k_limit_is_exact(self) -> None:
        base = '{"value":""}'
        exact = '{"value":"' + ("x" * (4 * 1024 - len(base.encode("utf-8")))) + '"}'
        self.assertEqual(len(exact.encode("utf-8")), 4 * 1024)
        call = KimiToolCall("call_exact", "lookup", exact)
        self.assertEqual(json.loads(call.arguments_json)["value"].count("x"), 4084)
        with self.assertRaises(ValueError):
            KimiToolCall("call_large", "lookup", exact + " ")

    def test_event_and_response_size_caps_fail_closed(self) -> None:
        oversized_event = b"data: " + (b"x" * (64 * 1024)) + b"\n\n"
        with self.assertRaises(KimiChatTransportError) as event_error:
            _run(lambda request: _success_response(oversized_event))
        self.assertEqual(event_error.exception.code, "kimi_chat_response_invalid")

        comment = b":" + (b"x" * 60_000) + b"\n\n"
        oversized_response = comment * 9
        with self.assertRaises(KimiChatTransportError) as response_error:
            _run(lambda request: _success_response(oversized_response))
        self.assertEqual(response_error.exception.code, "kimi_chat_response_invalid")

    def test_http_error_taxonomy_is_stable_and_redacted(self) -> None:
        cases = (
            (400, "content_filter", "kimi_chat_content_filtered"),
            (400, "invalid_request_error", "kimi_chat_invalid_request"),
            (401, "incorrect_api_key_error", "kimi_chat_auth_failed"),
            (403, "permission_denied_error", "kimi_chat_permission_denied"),
            (404, "resource_not_found_error", "kimi_chat_model_unavailable"),
            (499, "client_closed_request", "kimi_chat_client_closed_request"),
            (500, "server_error", "kimi_chat_provider_error"),
            (503, "server_unavailable", "kimi_chat_provider_unavailable"),
        )
        for status, provider_type, expected in cases:
            with self.subTest(status=status, provider_type=provider_type):
                body = _json_bytes(
                    {
                        "error": {
                            "type": provider_type,
                            "message": f"{_RAW_BODY_CANARY} {_KEY_CANARY}",
                        }
                    }
                )
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(KimiChatTransportError) as caught:
                        _run(
                            lambda request, status=status, body=body: httpx.Response(
                                status,
                                headers={
                                    "content-type": "application/json",
                                    "msh-request-id": _REQUEST_ID,
                                },
                                content=body,
                            )
                        )
                error = caught.exception
                self.assertEqual(error.code, expected)
                self.assertEqual(error.http_status, status)
                self.assertEqual(error.network_calls, 1)
                self.assertEqual(error.provider_error_type, provider_type)
                serialized = repr(error.to_receipt_fields())
                self.assertNotIn(_KEY_CANARY, serialized)
                self.assertNotIn(_RAW_BODY_CANARY, serialized)
                self.assertNotIn(_KEY_CANARY, stdout.getvalue())
                self.assertNotIn(_KEY_CANARY, stderr.getvalue())

    def test_three_429_error_types_are_distinct_and_never_retried(self) -> None:
        cases = {
            "engine_overloaded_error": "kimi_chat_engine_overloaded",
            "exceeded_current_quota_error": "kimi_chat_quota_exceeded",
            "rate_limit_reached_error": "kimi_chat_rate_limited",
        }
        for provider_type, expected in cases.items():
            with self.subTest(provider_type=provider_type):
                calls = 0

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal calls
                    calls += 1
                    return httpx.Response(
                        429,
                        headers={
                            "content-type": "application/json",
                            "retry-after": "1",
                        },
                        content=_json_bytes(
                            {"error": {"type": provider_type, "message": "retry"}}
                        ),
                    )

                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(handler)
                self.assertEqual(calls, 1)
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.network_calls, 1)

    def test_redirect_and_html_504_never_read_body(self) -> None:
        cases = (
            (307, "kimi_chat_redirect_denied"),
            (504, "kimi_chat_provider_timeout"),
        )
        for status, expected in cases:
            with self.subTest(status=status):
                stream = _ChunkStream([f"{_RAW_BODY_CANARY}{_KEY_CANARY}".encode()])
                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(
                        lambda request, status=status, stream=stream: httpx.Response(
                            status,
                            headers={
                                "content-type": "text/html",
                                "location": "https://evil.invalid/steal",
                            },
                            stream=stream,
                        )
                    )
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(stream.yielded, 0)
                self.assertTrue(stream.closed)
                self.assertNotIn(_KEY_CANARY, repr(caught.exception))

    def test_timeout_network_and_unknown_errors_are_single_attempt_and_redacted(self) -> None:
        cases = (
            (
                lambda request: httpx.ReadTimeout(
                    f"timeout {_KEY_CANARY}", request=request
                ),
                "kimi_chat_timeout",
                True,
            ),
            (
                lambda request: httpx.ConnectError(
                    f"network {_KEY_CANARY}", request=request
                ),
                "kimi_chat_network_failed",
                True,
            ),
            (
                lambda request: RuntimeError(f"unknown {_KEY_CANARY}"),
                "kimi_chat_failed",
                False,
            ),
        )
        for exception_factory, expected, ambiguous in cases:
            with self.subTest(expected=expected):
                calls = 0

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal calls
                    calls += 1
                    raise exception_factory(request)

                with self.assertRaises(KimiChatTransportError) as caught:
                    _run(handler)
                self.assertEqual(calls, 1)
                self.assertEqual(caught.exception.code, expected)
                self.assertIs(caught.exception.outcome_unknown, ambiguous)
                self.assertNotIn(_KEY_CANARY, repr(caught.exception))

    def test_close_failure_fails_success_but_preserves_primary_error(self) -> None:
        success_transport = _CloseFailingTransport(
            lambda request: _success_response()
        )
        with self.assertRaises(KimiChatTransportError) as success_error:
            asyncio.run(
                run_kimi_chat_completion(
                    _request(),
                    api_key=_KEY_CANARY,
                    confirm_online=True,
                    _transport_factory=lambda: success_transport,
                )
            )
        self.assertTrue(success_transport.closed)
        self.assertEqual(
            success_error.exception.code, "kimi_chat_client_close_failed"
        )

        failure_transport = _CloseFailingTransport(
            lambda request: httpx.Response(401, content=b"not read")
        )
        with self.assertRaises(KimiChatTransportError) as primary:
            asyncio.run(
                run_kimi_chat_completion(
                    _request(),
                    api_key=_KEY_CANARY,
                    confirm_online=True,
                    _transport_factory=lambda: failure_transport,
                )
            )
        self.assertEqual(primary.exception.code, "kimi_chat_auth_failed")

    def test_hanging_close_is_bounded(self) -> None:
        transport = _HangingCloseTransport(lambda request: _success_response())
        with patch("researchops.kimi_chat_transport._CLOSE_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(KimiChatTransportError) as caught:
                asyncio.run(
                    run_kimi_chat_completion(
                        _request(),
                        api_key=_KEY_CANARY,
                        confirm_online=True,
                        _transport_factory=lambda: transport,
                    )
                )
        self.assertTrue(transport.closed)
        self.assertEqual(caught.exception.code, "kimi_chat_client_close_failed")

    def test_cancellation_closes_transport_and_propagates(self) -> None:
        entered = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        transport = _TrackingMockTransport(handler)

        async def exercise() -> None:
            task = asyncio.create_task(
                run_kimi_chat_completion(
                    _request(),
                    api_key=_KEY_CANARY,
                    confirm_online=True,
                    _transport_factory=lambda: transport,
                )
            )
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(exercise())
        self.assertTrue(transport.closed)

    def test_client_construction_failure_closes_transport_without_attempt(self) -> None:
        transport = _TrackingMockTransport(
            lambda request: self.fail("request must not be attempted")
        )
        with patch(
            "researchops.kimi_chat_transport._build_client",
            side_effect=RuntimeError(f"constructor {_KEY_CANARY}"),
        ):
            with self.assertRaises(KimiChatTransportError) as caught:
                asyncio.run(
                    run_kimi_chat_completion(
                        _request(),
                        api_key=_KEY_CANARY,
                        confirm_online=True,
                        _transport_factory=lambda: transport,
                    )
                )
        self.assertTrue(transport.closed)
        self.assertEqual(caught.exception.code, "kimi_chat_failed")
        self.assertEqual(caught.exception.network_calls, 0)

    def test_default_transport_is_tls_verified_environment_isolated_and_zero_retry(self) -> None:
        mock_transport = _TrackingMockTransport(
            lambda request: self.fail("configuration test must not request")
        )
        with patch.object(
            transport_module.httpx,
            "AsyncHTTPTransport",
            return_value=mock_transport,
        ) as constructor:
            result = transport_module._default_transport_factory()
        self.assertIs(result, mock_transport)
        kwargs = constructor.call_args.kwargs
        self.assertFalse(kwargs["trust_env"])
        self.assertEqual(kwargs["retries"], 0)
        self.assertFalse(kwargs["http2"])
        tls_context = kwargs["verify"]
        self.assertEqual(tls_context.verify_mode, 2)
        self.assertTrue(tls_context.check_hostname)
        self.assertIsNone(tls_context.keylog_filename)
        self.assertGreater(len(tls_context.get_ca_certs()), 0)
        asyncio.run(mock_transport.aclose())

    def test_dependency_debug_and_timeout_drift_stop_before_key_loader(self) -> None:
        def forbidden_loader() -> str:
            self.fail("configuration drift must precede Key lookup")

        with patch(
            "researchops.kimi_chat_transport._REQUEST_DEADLINE_SECONDS", 0
        ):
            with self.assertRaises(KimiChatTransportError) as caught:
                asyncio.run(
                    run_kimi_chat_completion(
                        _request(),
                        confirm_online=True,
                        _key_loader=forbidden_loader,
                    )
                )
        self.assertEqual(caught.exception.code, "kimi_chat_configuration_invalid")

        logger = transport_module.logging.getLogger("httpx")
        old_level = logger.level
        try:
            logger.setLevel(transport_module.logging.DEBUG)
            with self.assertRaises(KimiChatTransportError) as debug:
                asyncio.run(
                    run_kimi_chat_completion(
                        _request(),
                        confirm_online=True,
                        _key_loader=forbidden_loader,
                    )
                )
            self.assertEqual(debug.exception.code, "kimi_chat_configuration_invalid")
        finally:
            logger.setLevel(old_level)

    def test_receipt_fields_never_include_raw_key_body_or_assistant_content(self) -> None:
        body = _json_bytes(
            {
                "error": {
                    "type": "invalid_request_error",
                    "message": f"{_RAW_BODY_CANARY} {_KEY_CANARY}",
                }
            }
        )
        with self.assertRaises(KimiChatTransportError) as caught:
            _run(
                lambda request: httpx.Response(
                    400,
                    headers={"content-type": "application/json"},
                    content=body,
                )
            )
        receipt = caught.exception.to_receipt_fields()
        serialized = json.dumps(receipt, sort_keys=True)
        for forbidden in (_KEY_CANARY, _RAW_BODY_CANARY, "authorization"):
            self.assertNotIn(forbidden, serialized.lower())


if __name__ == "__main__":
    unittest.main()
