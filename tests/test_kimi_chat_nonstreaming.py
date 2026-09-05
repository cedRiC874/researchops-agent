from __future__ import annotations

import asyncio
import hashlib
import json
import unittest
from pathlib import Path

import httpx

from researchops.kimi_chat_nonstreaming import (
    KIMI_NONSTREAMING_TRANSPORT_ID,
    kimi_nonstreaming_contract,
    request_body_bytes,
    run_kimi_nonstreaming_completion,
)
from researchops.kimi_chat_transport import (
    KimiAssistantMessage,
    KimiChatRequest,
    KimiChatTransportError,
    KimiFunctionTool,
    KimiSpecifiedToolChoice,
    KimiTextMessage,
    KimiToolCall,
    KimiToolResultMessage,
)


SAFE_KEY = "sk-kimi-safe-test-value-1234567890"
ROOT = Path(__file__).resolve().parents[1]


def _transport_traceback_locals(error: BaseException) -> str:
    """Render only transport-frame locals for redaction assertions."""

    values: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        if Path(traceback.tb_frame.f_code.co_filename).name == (
            "kimi_chat_nonstreaming.py"
        ):
            values.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    return "\n".join(values)


def _tool() -> KimiFunctionTool:
    return KimiFunctionTool.from_schema(
        name="lookup_synthetic_metric",
        description="Return one fixed synthetic metric.",
        parameters={
            "type": "object",
            "properties": {
                "dataset_id": {"type": "string"},
                "metric_id": {"type": "string"},
            },
            "required": ["dataset_id", "metric_id"],
            "additionalProperties": False,
        },
    )


def _first_request() -> KimiChatRequest:
    return KimiChatRequest(
        messages=(
            KimiTextMessage("system", "Use exactly the declared synthetic tool."),
            KimiTextMessage(
                "user",
                "Look up effect_size for kimi_synth_success_v1 and use the tool.",
            ),
        ),
        tools=(_tool(),),
        tool_choice="required",
        max_completion_tokens=1536,
        reasoning_effort="low",
    )


def _payload(
    *,
    finish_reason: str,
    message: dict,
    usage: dict | None = None,
) -> dict:
    return {
        "id": "cmpl_test_001",
        "object": "chat.completion",
        "created": 1788180000,
        "model": "kimi-k3",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage
        or {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cached_tokens": 0,
        },
    }


class KimiNonstreamingTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_required_tool_response_is_parsed_from_one_json_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertFalse(payload["stream"])
            self.assertNotIn("stream_options", payload)
            self.assertEqual(payload["model"], "kimi-k3")
            self.assertEqual(payload["reasoning_effort"], "low")
            self.assertEqual(payload["tool_choice"], "required")
            self.assertEqual(len(payload["tools"]), 1)
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json=_payload(
                    finish_reason="tool_calls",
                    message={
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "I should call the synthetic tool.",
                        "tool_calls": [
                            {
                                "id": "call_synth_success_01",
                                "type": "function",
                                "function": {
                                    "name": "lookup_synthetic_metric",
                                    "arguments": (
                                        '{"dataset_id":"kimi_synth_success_v1",'
                                        '"metric_id":"effect_size"}'
                                    ),
                                },
                            }
                        ],
                    },
                ),
            )

        result = await run_kimi_nonstreaming_completion(
            _first_request(),
            api_key=SAFE_KEY,
            confirm_online=True,
            _transport_factory=lambda: httpx.MockTransport(handler),
        )

        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(len(result.assistant_message.tool_calls), 1)
        self.assertEqual(
            result.assistant_message.tool_calls[0].name,
            "lookup_synthetic_metric",
        )
        self.assertEqual(result.usage.total_tokens, 120)
        self.assertTrue(result.usage.cached_tokens_reported)
        self.assertEqual(result.network_calls, 1)

    async def test_current_k3_nested_usage_projection_is_strictly_parsed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            payload = _payload(
                finish_reason="stop",
                message={
                    "role": "assistant",
                    "content": "Synthetic answer.",
                    "reasoning_content": "Synthetic reasoning.",
                    "refusal": None,
                },
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_tokens_details": {
                        "audio_tokens": 0,
                        "cached_tokens": 80,
                    },
                    "completion_tokens_details": {
                        "accepted_prediction_tokens": 0,
                        "audio_tokens": 0,
                        "reasoning_tokens": 12,
                        "rejected_prediction_tokens": 0,
                    },
                },
            )
            payload["service_tier"] = "standard"
            payload["system_fingerprint"] = "fp_kimi_test"
            payload["choices"][0]["logprobs"] = None
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json=payload,
            )

        result = await run_kimi_nonstreaming_completion(
            _first_request(),
            api_key=SAFE_KEY,
            confirm_online=True,
            _transport_factory=lambda: httpx.MockTransport(handler),
        )
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.usage.cached_tokens, 80)
        self.assertTrue(result.usage.cached_tokens_reported)

    async def test_usage_cache_projections_reconcile_and_reject_unknowns(self) -> None:
        invalid_usages = (
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "cached_tokens": 40,
                "prompt_tokens_details": {"cached_tokens": 41},
            },
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {
                    "cached_tokens": 40,
                    "undocumented": 1,
                },
            },
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "completion_tokens_details": {"reasoning_tokens": 21},
            },
        )
        for usage in invalid_usages:
            with self.subTest(usage=usage):
                def handler(
                    request: httpx.Request, current: dict = usage
                ) -> httpx.Response:
                    del request
                    return httpx.Response(
                        200,
                        headers={"content-type": "application/json"},
                        json=_payload(
                            finish_reason="stop",
                            message={"role": "assistant", "content": "done"},
                            usage=current,
                        ),
                    )

                with self.assertRaises(KimiChatTransportError) as caught:
                    await run_kimi_nonstreaming_completion(
                        _first_request(),
                        api_key=SAFE_KEY,
                        confirm_online=True,
                        _transport_factory=lambda: httpx.MockTransport(handler),
                    )
                self.assertEqual(caught.exception.code, "kimi_chat_usage_invalid")
                self.assertEqual(caught.exception.http_status, 200)

        def reconciled_handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json=_payload(
                    finish_reason="stop",
                    message={"role": "assistant", "content": "done"},
                    usage={
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                        "cached_tokens": 40,
                        "prompt_tokens_details": {"cached_tokens": 40},
                    },
                ),
            )

        result = await run_kimi_nonstreaming_completion(
            _first_request(),
            api_key=SAFE_KEY,
            confirm_online=True,
            _transport_factory=lambda: httpx.MockTransport(reconciled_handler),
        )
        self.assertEqual(result.usage.cached_tokens, 40)

    async def test_tool_call_content_and_parsed_assistant_are_replayed(self) -> None:
        def first_handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json=_payload(
                    finish_reason="tool_calls",
                    message={
                        "role": "assistant",
                        "content": "I will use the declared synthetic tool.",
                        "reasoning_content": "Preserved reasoning from K3.",
                        "tool_calls": [
                            {
                                "id": "call_synth_success_01",
                                "type": "function",
                                "function": {
                                    "name": "lookup_synthetic_metric",
                                    "arguments": (
                                        '{"dataset_id":"kimi_synth_success_v1",'
                                        '"metric_id":"effect_size"}'
                                    ),
                                },
                            }
                        ],
                    },
                ),
            )

        first = await run_kimi_nonstreaming_completion(
            _first_request(),
            api_key=SAFE_KEY,
            confirm_online=True,
            _transport_factory=lambda: httpx.MockTransport(first_handler),
        )
        self.assertIn("declared synthetic tool", first.assistant_message.content or "")

        original_request = _first_request()
        replay_request = KimiChatRequest(
            messages=(
                *original_request.messages,
                first.assistant_message,
                KimiToolResultMessage(
                    "call_synth_success_01",
                    "lookup_synthetic_metric",
                    '{"status":"ok","value":0.375}',
                ),
            ),
            tools=original_request.tools,
            tool_choice="none",
            max_completion_tokens=1536,
            reasoning_effort="low",
        )
        replay = json.loads(request_body_bytes(replay_request))["messages"][2]
        self.assertEqual(
            replay["content"], "I will use the declared synthetic tool."
        )
        self.assertEqual(
            replay["reasoning_content"], "Preserved reasoning from K3."
        )
        self.assertEqual(replay["tool_calls"][0]["id"], "call_synth_success_01")

    async def test_complete_assistant_message_and_tool_result_are_replayed(self) -> None:
        assistant = KimiAssistantMessage(
            content="",
            reasoning_content="Preserved synthetic reasoning.",
            tool_calls=(
                KimiToolCall(
                    "call_synth_success_01",
                    "lookup_synthetic_metric",
                    '{"dataset_id":"kimi_synth_success_v1","metric_id":"effect_size"}',
                ),
            ),
        )
        request = KimiChatRequest(
            messages=(
                KimiTextMessage("system", "Use only synthetic data."),
                KimiTextMessage("user", "Return the synthetic metric."),
                assistant,
                KimiToolResultMessage(
                    "call_synth_success_01",
                    "lookup_synthetic_metric",
                    '{"status":"ok","value":0.375}',
                ),
            ),
            tools=(_tool(),),
            tool_choice="none",
            max_completion_tokens=1536,
            reasoning_effort="low",
        )

        def handler(http_request: httpx.Request) -> httpx.Response:
            payload = json.loads(http_request.content)
            replay = payload["messages"][2]
            self.assertEqual(replay["reasoning_content"], "Preserved synthetic reasoning.")
            self.assertEqual(replay["tool_calls"][0]["id"], "call_synth_success_01")
            self.assertEqual(
                payload["messages"][3]["tool_call_id"], "call_synth_success_01"
            )
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json=_payload(
                    finish_reason="stop",
                    message={
                        "role": "assistant",
                        "content": "The synthetic effect size is 0.375.",
                        "reasoning_content": "The tool result contains the value.",
                    },
                    usage={
                        "prompt_tokens": 140,
                        "completion_tokens": 30,
                        "total_tokens": 170,
                    },
                ),
            )

        result = await run_kimi_nonstreaming_completion(
            request,
            api_key=SAFE_KEY,
            confirm_online=True,
            _transport_factory=lambda: httpx.MockTransport(handler),
        )
        self.assertEqual(result.finish_reason, "stop")
        self.assertIn("0.375", result.assistant_message.content or "")
        self.assertFalse(result.usage.cached_tokens_reported)
        self.assertIsNone(result.usage_dict()["cached_tokens"])

    async def test_duplicate_json_and_unknown_fields_fail_closed(self) -> None:
        bodies = (
            b'{"id":"a","id":"b","object":"chat.completion","created":1,'
            b'"model":"kimi-k3","choices":[],"usage":{}}',
            json.dumps(
                {
                    **_payload(
                        finish_reason="stop",
                        message={"role": "assistant", "content": "done"},
                    ),
                    "undocumented": True,
                }
            ).encode(),
        )
        for body in bodies:
            with self.subTest(body=body[:40]):
                def handler(request: httpx.Request, raw=body) -> httpx.Response:
                    del request
                    return httpx.Response(
                        200,
                        headers={"content-type": "application/json"},
                        content=raw,
                    )

                with self.assertRaises(KimiChatTransportError) as caught:
                    await run_kimi_nonstreaming_completion(
                        _first_request(),
                        api_key=SAFE_KEY,
                        confirm_online=True,
                        _transport_factory=lambda: httpx.MockTransport(handler),
                    )
                self.assertEqual(caught.exception.code, "kimi_chat_response_invalid")
                if b"undocumented" in body:
                    self.assertIsNotNone(caught.exception.usage)
                    self.assertEqual(caught.exception.usage.total_tokens, 120)

    async def test_malformed_tool_arguments_keep_usage_and_stable_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "msh-request-id": "req_kimi_malformed_tool_01",
                },
                json=_payload(
                    finish_reason="tool_calls",
                    message={
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "Synthetic reasoning.",
                        "tool_calls": [
                            {
                                "id": "call_synth_success_01",
                                "type": "function",
                                "function": {
                                    "name": "lookup_synthetic_metric",
                                    "arguments": "{",
                                },
                            }
                        ],
                    },
                ),
            )

        with self.assertRaises(KimiChatTransportError) as caught:
            await run_kimi_nonstreaming_completion(
                _first_request(),
                api_key=SAFE_KEY,
                confirm_online=True,
                _transport_factory=lambda: httpx.MockTransport(handler),
            )
        error = caught.exception
        self.assertEqual(error.code, "kimi_chat_tool_protocol_invalid")
        self.assertEqual(error.http_status, 200)
        self.assertIsNotNone(error.usage)
        self.assertEqual(error.usage.total_tokens, 120)
        self.assertEqual(
            error.request_id_sha256,
            hashlib.sha256(b"req_kimi_malformed_tool_01").hexdigest(),
        )
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    async def test_valid_usage_is_attached_to_later_response_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json=_payload(
                    finish_reason="stop",
                    message={
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "No final answer was emitted.",
                    },
                ),
            )

        with self.assertRaises(KimiChatTransportError) as caught:
            await run_kimi_nonstreaming_completion(
                _first_request(),
                api_key=SAFE_KEY,
                confirm_online=True,
                _transport_factory=lambda: httpx.MockTransport(handler),
            )
        self.assertEqual(caught.exception.code, "kimi_chat_response_invalid")
        self.assertIsNotNone(caught.exception.usage)
        self.assertEqual(caught.exception.usage.prompt_tokens, 100)

    async def test_missing_usage_has_distinct_unknown_outcome(self) -> None:
        payload = _payload(
            finish_reason="stop",
            message={"role": "assistant", "content": "done"},
        )
        del payload["usage"]

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json=payload,
            )

        with self.assertRaises(KimiChatTransportError) as caught:
            await run_kimi_nonstreaming_completion(
                _first_request(),
                api_key=SAFE_KEY,
                confirm_online=True,
                _transport_factory=lambda: httpx.MockTransport(handler),
            )
        self.assertEqual(caught.exception.code, "kimi_chat_usage_missing")
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertIsNone(caught.exception.usage)

    async def test_malformed_json_and_key_loader_errors_retain_no_raw_marker(
        self,
    ) -> None:
        raw_marker = "PRIVATE-RESPONSE-MARKER-DO-NOT-RETAIN"

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=(f'{{"private":"{raw_marker}",').encode(),
            )

        with self.assertRaises(KimiChatTransportError) as malformed:
            await run_kimi_nonstreaming_completion(
                _first_request(),
                api_key=SAFE_KEY,
                confirm_online=True,
                _transport_factory=lambda: httpx.MockTransport(handler),
            )
        error = malformed.exception
        self.assertEqual(error.code, "kimi_chat_response_invalid")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(raw_marker, repr(error.__dict__))
        self.assertNotIn(raw_marker, _transport_traceback_locals(error))

        loader_marker = "PRIVATE-KEY-LOADER-MARKER-DO-NOT-RETAIN"

        def bad_loader() -> str:
            raise RuntimeError(loader_marker)

        def forbidden_transport() -> httpx.AsyncBaseTransport:
            raise AssertionError("transport must not be created")

        with self.assertRaises(KimiChatTransportError) as loader_failure:
            await run_kimi_nonstreaming_completion(
                _first_request(),
                confirm_online=True,
                _key_loader=bad_loader,
                _transport_factory=forbidden_transport,
            )
        error = loader_failure.exception
        self.assertEqual(error.code, "kimi_chat_configuration_invalid")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(loader_marker, repr(error.__dict__))
        self.assertNotIn(loader_marker, _transport_traceback_locals(error))

    async def test_specified_tool_choice_is_rejected_before_key_or_transport(
        self,
    ) -> None:
        request = KimiChatRequest(
            messages=(KimiTextMessage("user", "Use the declared tool."),),
            tools=(_tool(),),
            tool_choice=KimiSpecifiedToolChoice("lookup_synthetic_metric"),
            max_completion_tokens=1536,
            reasoning_effort="low",
        )

        def forbidden_key_loader() -> str:
            raise AssertionError("key loader must not run")

        def forbidden_transport() -> httpx.AsyncBaseTransport:
            raise AssertionError("transport must not be created")

        with self.assertRaises(KimiChatTransportError) as caught:
            await run_kimi_nonstreaming_completion(
                request,
                confirm_online=True,
                _key_loader=forbidden_key_loader,
                _transport_factory=forbidden_transport,
            )
        self.assertEqual(caught.exception.code, "kimi_chat_request_invalid")
        self.assertEqual(caught.exception.network_calls, 0)

    async def test_cancellation_during_close_clears_sensitive_transport_locals(
        self,
    ) -> None:
        private_key = "sk-kimi-private-close-test-1234567890"

        class CancelOnCloseTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                return httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    json=_payload(
                        finish_reason="stop",
                        message={"role": "assistant", "content": "done"},
                    ),
                    request=request,
                )

            async def aclose(self) -> None:
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError) as caught:
            await run_kimi_nonstreaming_completion(
                _first_request(),
                api_key=private_key,
                confirm_online=True,
                _transport_factory=CancelOnCloseTransport,
            )
        self.assertNotIn(private_key, _transport_traceback_locals(caught.exception))

    async def test_primary_http_error_survives_client_close_failure(self) -> None:
        class FailOnCloseTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                return httpx.Response(
                    400,
                    headers={"content-type": "application/json"},
                    json={
                        "error": {
                            "type": "invalid_request_error",
                            "message": "not persisted",
                        }
                    },
                    request=request,
                )

            async def aclose(self) -> None:
                raise RuntimeError("close failed")

        with self.assertRaises(KimiChatTransportError) as caught:
            await run_kimi_nonstreaming_completion(
                _first_request(),
                api_key=SAFE_KEY,
                confirm_online=True,
                _transport_factory=FailOnCloseTransport,
            )
        self.assertEqual(caught.exception.code, "kimi_chat_invalid_request")
        self.assertEqual(caught.exception.http_status, 400)

    async def test_http_400_uses_existing_stable_error_taxonomy(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                400,
                headers={"content-type": "application/json"},
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "not persisted",
                    }
                },
            )

        with self.assertRaises(KimiChatTransportError) as caught:
            await run_kimi_nonstreaming_completion(
                _first_request(),
                api_key=SAFE_KEY,
                confirm_online=True,
                _transport_factory=lambda: httpx.MockTransport(handler),
            )
        self.assertEqual(caught.exception.code, "kimi_chat_invalid_request")
        self.assertEqual(caught.exception.http_status, 400)
        self.assertEqual(caught.exception.network_calls, 1)

    async def test_confirmation_and_key_gates_precede_transport(self) -> None:
        def forbidden_key_loader() -> str:
            raise AssertionError("key loader must not run")

        def forbidden_transport() -> httpx.AsyncBaseTransport:
            raise AssertionError("transport must not be created")

        with self.assertRaises(KimiChatTransportError) as not_confirmed:
            await run_kimi_nonstreaming_completion(
                _first_request(),
                confirm_online=False,
                _key_loader=forbidden_key_loader,
                _transport_factory=forbidden_transport,
            )
        self.assertEqual(not_confirmed.exception.code, "kimi_chat_confirmation_required")

        with self.assertRaises(KimiChatTransportError) as missing_key:
            await run_kimi_nonstreaming_completion(
                _first_request(),
                confirm_online=True,
                _key_loader=lambda: None,
                _transport_factory=forbidden_transport,
            )
        self.assertEqual(missing_key.exception.code, "kimi_chat_key_missing")

    def test_contract_is_synthetic_non_authorizing_and_non_streaming(self) -> None:
        contract = kimi_nonstreaming_contract()
        self.assertEqual(contract["transport_id"], KIMI_NONSTREAMING_TRANSPORT_ID)
        self.assertFalse(contract["stream"])
        self.assertTrue(contract["synthetic_only"])
        self.assertFalse(contract["non_synthetic_or_private_allowed"])
        self.assertFalse(contract["specified_function_tool_choice_allowed"])
        self.assertTrue(contract["effective_terms_allow_model_service_optimization_use"])
        self.assertEqual(
            contract["response_schema_profile"]["profile_id"],
            "kimi-k3-json-2026-08-31-v1",
        )
        self.assertEqual(
            contract["usage_schema_profile"]["accepted_cache_projections"],
            [
                "cached_tokens",
                "prompt_tokens_details.cached_tokens",
                "both_equal",
            ],
        )
        self.assertTrue(
            contract["usage_schema_profile"][
                "validated_usage_attached_to_later_protocol_errors"
            ]
        )
        self.assertTrue(
            contract["usage_schema_profile"]["missing_usage_outcome_unknown"]
        )
        self.assertFalse(
            contract["error_privacy"]["raw_json_retained_in_exception_cause"]
        )
        self.assertEqual(contract["network_calls"], 0)
        self.assertEqual(contract["model_calls"], 0)
        self.assertEqual(len(contract["official_sources"]), 6)
        snapshot = json.loads(
            (ROOT / "evals/v2/kimi_k3_nonstreaming_contract.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(snapshot, contract)

    def test_request_body_is_canonical_non_streaming_json(self) -> None:
        body = request_body_bytes(_first_request())
        payload = json.loads(body)
        self.assertFalse(payload["stream"])
        self.assertEqual(
            body,
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
