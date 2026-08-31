from __future__ import annotations

import asyncio
import inspect
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from researchops import kimi_k3_handshake as handshake_module
from researchops import kimi_k3_handshake_cli as handshake_cli
from researchops.kimi_chat_nonstreaming import KimiNonstreamingResponse
from researchops.kimi_chat_transport import (
    KimiAssistantMessage,
    KimiChatTransportError,
    KimiInvalidRequestProbeResult,
    KimiToolCall,
    KimiToolResultMessage,
)
from researchops.kimi_chat_transport_v3 import KimiChatUsageV3
from researchops.kimi_k3_handshake import (
    HANDSHAKE_ID,
    PLAN_COMMITMENT_SHA256,
    kimi_k3_handshake_contract,
    kimi_k3_handshake_plan,
    run_kimi_k3_handshake,
    validate_kimi_k3_handshake,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 31, 15, 0, 0, tzinfo=timezone.utc)
EXPIRY = "2026-08-31T16:00:00Z"
AUTHORIZATION_ID = "kimi-k3-handshake-test-001"
SAFE_KEY = "sk-kimi-SECRET-KEY-MUST-NOT-PERSIST-123456"
SECRET_REASONING = "SECRET-REASONING-MUST-NOT-PERSIST"
SECRET_TOOL_CONTENT = "SECRET-TOOL-CONTENT-MUST-NOT-PERSIST"
SECRET_FINAL = "SECRET-FINAL-MUST-NOT-PERSIST 0.375"


def _usage(prompt: int, completion: int) -> KimiChatUsageV3:
    return KimiChatUsageV3(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cached_tokens=0,
        cached_tokens_reported=False,
    )


def _response(
    assistant: KimiAssistantMessage,
    finish_reason: str,
    prompt: int,
    completion: int,
    latency: int,
) -> KimiNonstreamingResponse:
    return KimiNonstreamingResponse(
        assistant_message=assistant,
        finish_reason=finish_reason,
        usage=_usage(prompt, completion),
        latency_ms=latency,
        http_status=200,
        http_attempts=1,
        network_calls=1,
    )


def _probe() -> KimiInvalidRequestProbeResult:
    return KimiInvalidRequestProbeResult(
        latency_ms=17,
        http_status=400,
        http_attempts=1,
        network_calls=1,
        provider_error_type="invalid_request_error",
    )


def _invoke(arguments: list[str]) -> tuple[int, dict]:
    stdout = io.StringIO()
    with patch("sys.stdout", stdout):
        exit_code = handshake_cli.main(arguments)
    return exit_code, json.loads(stdout.getvalue())


class KimiK3HandshakeTests(unittest.IsolatedAsyncioTestCase):
    def test_frozen_plan_contract_and_commitment_validate_offline(self) -> None:
        result = validate_kimi_k3_handshake(ROOT)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["plan_commitment_sha256"], PLAN_COMMITMENT_SHA256)
        self.assertEqual(result["network_calls"], 0)
        self.assertFalse(result["key_loaded"])
        plan = kimi_k3_handshake_plan()
        self.assertEqual(plan["locked_caps"]["network_attempts"], 3)
        self.assertEqual(plan["locked_caps"]["network_calls"], 3)
        self.assertEqual(plan["locked_caps"]["model_requests"], 2)
        self.assertEqual(plan["locked_caps"]["input_tokens_total"], 16_000)
        self.assertEqual(plan["locked_caps"]["output_tokens_total"], 3_072)
        self.assertEqual(plan["locked_caps"]["tool_executions"], 1)
        self.assertEqual(plan["locked_caps"]["total_timeout_seconds"], 300)
        self.assertEqual(
            plan["pricing"]["uncached_input_per_million"], "20.000000"
        )
        self.assertEqual(plan["pricing"]["output_per_million"], "100.000000")
        self.assertEqual(
            plan["terms_review"]["displayed_effective_date"], "2026-08-31"
        )
        self.assertEqual(len(plan["terms_review"]["official_source_commitments"]), 6)
        self.assertEqual(len(plan["component_hashes"]), 8)
        self.assertEqual(
            plan["component_hashes"]["handshake_runner_source_sha256"],
            handshake_module._sha256_file(
                ROOT / "src/researchops/kimi_k3_handshake.py"
            ),
        )
        self.assertFalse(kimi_k3_handshake_contract()["cli_api_key_argument_allowed"])
        self.assertTrue(plan["authorization"]["absolute_deadline_enforced"])
        self.assertTrue(
            plan["authorization"]["network_attempt_preflight_expiry_check"]
        )
        self.assertTrue(
            kimi_k3_handshake_contract()[
                "absolute_authorization_deadline_enforced"
            ]
        )

    def test_component_drift_invalidates_plan_before_online_authorization(self) -> None:
        actual = handshake_module._implementation_component_hashes(ROOT)
        drifted = dict(actual)
        drifted["nonstreaming_transport_source_sha256"] = "0" * 64
        with patch.object(
            handshake_module,
            "_implementation_component_hashes",
            return_value=drifted,
        ):
            with self.assertRaises(handshake_module.KimiK3HandshakeError) as caught:
                validate_kimi_k3_handshake(ROOT)
        self.assertEqual(caught.exception.code, "kimi_k3_handshake_plan_drift")

    def test_offline_ci_binds_zero_call_handshake_validation(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("python -m researchops.kimi_k3_handshake_cli", workflow)
        self.assertIn(
            'validate | Out-String)',
            workflow,
        )
        self.assertIn(PLAN_COMMITMENT_SHA256, workflow)
        self.assertIn("$kimiHandshake.network_attempts -ne 0", workflow)
        self.assertIn("$kimiHandshake.key_loaded -ne $false", workflow)
        self.assertIn("$kimiHandshake.online_authorized -ne $false", workflow)

    async def test_success_replays_complete_assistant_and_tool_result_then_probe(self) -> None:
        calls: list[str] = []
        first_assistant = KimiAssistantMessage(
            content=SECRET_TOOL_CONTENT,
            reasoning_content=SECRET_REASONING,
            tool_calls=(
                KimiToolCall(
                    "call_synth_success_01",
                    "lookup_synthetic_metric",
                    '{"dataset_id":"kimi_synth_success_v1","metric_id":"effect_size"}',
                ),
            ),
        )

        async def completion(request, *, api_key, confirm_online):
            self.assertEqual(api_key, SAFE_KEY)
            self.assertTrue(confirm_online)
            calls.append("model")
            if len(calls) == 1:
                self.assertEqual(request.tool_choice, "required")
                self.assertEqual(len(request.messages), 2)
                return _response(first_assistant, "tool_calls", 100, 20, 11)
            self.assertEqual(request.tool_choice, "none")
            self.assertEqual(len(request.messages), 4)
            self.assertIs(request.messages[2], first_assistant)
            self.assertIsInstance(request.messages[3], KimiToolResultMessage)
            self.assertEqual(
                json.loads(request.messages[3].content),
                {
                    "status": "ok",
                    "dataset_id": "kimi_synth_success_v1",
                    "metric_id": "effect_size",
                    "value": 0.375,
                    "unit": "synthetic_standardized_units",
                },
            )
            return _response(
                KimiAssistantMessage(
                    content=SECRET_FINAL,
                    reasoning_content="SECRET-SECOND-REASONING",
                ),
                "stop",
                150,
                30,
                13,
            )

        async def probe(*, api_key, confirm_online):
            self.assertEqual(api_key, SAFE_KEY)
            self.assertTrue(confirm_online)
            calls.append("probe")
            return _probe()

        with tempfile.TemporaryDirectory() as directory:
            artifact_directory = Path(directory)
            result = await run_kimi_k3_handshake(
                project_root=ROOT,
                authorization_id=AUTHORIZATION_ID,
                authorization_expires_at_utc=EXPIRY,
                expected_plan_commitment_sha256=PLAN_COMMITMENT_SHA256,
                confirm_online=True,
                accept_locked_caps=True,
                attest_terms_and_pricing_unchanged=True,
                _key_loader=lambda: SAFE_KEY,
                _completion_runner=completion,
                _probe_runner=probe,
                _clock=lambda: NOW,
                _artifact_directory=artifact_directory,
            )
            self.assertEqual(calls, ["model", "model", "probe"])
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["error_code"], None)
            self.assertEqual(result["network_calls"], 3)
            self.assertEqual(result["network_attempts"], 3)
            self.assertTrue(result["network_call_observation_complete"])
            self.assertEqual(result["model_requests"], 2)
            self.assertEqual(result["tool_executions"], 1)
            self.assertEqual(result["invalid_request_probes"], 1)
            self.assertEqual(result["input_tokens"], 250)
            self.assertEqual(result["output_tokens"], 50)
            self.assertTrue(result["usage_complete"])
            self.assertIsNone(result["cached_input_tokens"])
            self.assertFalse(result["cached_input_usage_complete"])
            self.assertEqual(result["local_observed_usage_cost_cny"], "0.010000")
            self.assertEqual(
                result["local_observed_usage_cost_coverage"], "complete"
            )
            self.assertTrue(result["invalid_request_probe_semantics_verified"])
            self.assertEqual(result["invalid_request_probe_http_status"], 400)
            self.assertEqual(
                result["invalid_request_probe_provider_error_type"],
                "invalid_request_error",
            )
            self.assertEqual(
                result["authorization_expires_at_utc"],
                "2026-08-31T16:00:00.000Z",
            )
            self.assertEqual(len(list(artifact_directory.glob("*.consumption.json"))), 1)
            self.assertEqual(len(list(artifact_directory.glob("*.terminal.json"))), 1)
            persisted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in artifact_directory.iterdir()
            )
            for secret in (
                SAFE_KEY,
                AUTHORIZATION_ID,
                SECRET_REASONING,
                SECRET_TOOL_CONTENT,
                SECRET_FINAL,
                "call_synth_success_01",
            ):
                self.assertNotIn(secret, persisted)
            self.assertNotIn("api_key", inspect.signature(run_kimi_k3_handshake).parameters)

    async def test_authorization_is_consumed_once_before_key_load(self) -> None:
        first_assistant = KimiAssistantMessage(
            content="",
            tool_calls=(
                KimiToolCall(
                    "call_once",
                    "lookup_synthetic_metric",
                    '{"dataset_id":"kimi_synth_success_v1","metric_id":"effect_size"}',
                ),
            ),
        )
        response_queue = [
            _response(first_assistant, "tool_calls", 10, 2, 1),
            _response(KimiAssistantMessage(content="done"), "stop", 12, 3, 1),
        ]

        async def completion(*args, **kwargs):
            del args, kwargs
            return response_queue.pop(0)

        async def probe(**kwargs):
            del kwargs
            return _probe()

        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            first = await run_kimi_k3_handshake(
                project_root=ROOT,
                authorization_id=AUTHORIZATION_ID,
                authorization_expires_at_utc=EXPIRY,
                expected_plan_commitment_sha256=PLAN_COMMITMENT_SHA256,
                confirm_online=True,
                accept_locked_caps=True,
                attest_terms_and_pricing_unchanged=True,
                _key_loader=lambda: SAFE_KEY,
                _completion_runner=completion,
                _probe_runner=probe,
                _clock=lambda: NOW,
                _artifact_directory=artifacts,
            )
            self.assertEqual(first["status"], "success")

            def forbidden_key_loader():
                raise AssertionError("consumed authorization must fail before Key load")

            second = await run_kimi_k3_handshake(
                project_root=ROOT,
                authorization_id=AUTHORIZATION_ID,
                authorization_expires_at_utc=EXPIRY,
                expected_plan_commitment_sha256=PLAN_COMMITMENT_SHA256,
                confirm_online=True,
                accept_locked_caps=True,
                attest_terms_and_pricing_unchanged=True,
                _key_loader=forbidden_key_loader,
                _completion_runner=AsyncMock(side_effect=AssertionError("no model")),
                _probe_runner=AsyncMock(side_effect=AssertionError("no probe")),
                _clock=lambda: NOW,
                _artifact_directory=artifacts,
            )
            self.assertEqual(second["status"], "not_run")
            self.assertEqual(
                second["error_code"],
                "kimi_k3_handshake_authorization_already_consumed",
            )

    async def test_failure_after_consumption_is_terminal_and_not_retryable(self) -> None:
        async def failed_completion(*args, **kwargs):
            del args, kwargs
            raise KimiChatTransportError(
                "kimi_chat_response_invalid", http_status=200, network_calls=1
            )

        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            result = await run_kimi_k3_handshake(
                project_root=ROOT,
                authorization_id="kimi-k3-handshake-failure-001",
                authorization_expires_at_utc=EXPIRY,
                expected_plan_commitment_sha256=PLAN_COMMITMENT_SHA256,
                confirm_online=True,
                accept_locked_caps=True,
                attest_terms_and_pricing_unchanged=True,
                _key_loader=lambda: SAFE_KEY,
                _completion_runner=failed_completion,
                _probe_runner=AsyncMock(side_effect=AssertionError("probe forbidden")),
                _clock=lambda: NOW,
                _artifact_directory=artifacts,
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error_code"], "kimi_chat_response_invalid")
            self.assertEqual(result["network_calls"], 1)
            self.assertEqual(result["network_attempts"], 1)
            self.assertTrue(result["network_call_observation_complete"])
            self.assertEqual(result["model_requests"], 1)
            self.assertEqual(result["tool_executions"], 0)
            self.assertFalse(result["authorizes_retry"])
            self.assertEqual(len(list(artifacts.glob("*.consumption.json"))), 1)
            self.assertEqual(len(list(artifacts.glob("*.terminal.json"))), 1)

    async def test_transport_error_usage_still_enforces_token_caps(self) -> None:
        async def failed_completion(*args, **kwargs):
            del args, kwargs
            raise KimiChatTransportError(
                "kimi_chat_response_invalid",
                http_status=200,
                network_calls=1,
                usage=_usage(8_001, 1),
            )

        with tempfile.TemporaryDirectory() as directory:
            result = await run_kimi_k3_handshake(
                project_root=ROOT,
                authorization_id="kimi-k3-handshake-usage-cap-001",
                authorization_expires_at_utc=EXPIRY,
                expected_plan_commitment_sha256=PLAN_COMMITMENT_SHA256,
                confirm_online=True,
                accept_locked_caps=True,
                attest_terms_and_pricing_unchanged=True,
                _key_loader=lambda: SAFE_KEY,
                _completion_runner=failed_completion,
                _probe_runner=AsyncMock(side_effect=AssertionError("no probe")),
                _clock=lambda: NOW,
                _artifact_directory=Path(directory),
            )
            self.assertEqual(
                result["error_code"],
                "kimi_k3_handshake_input_token_cap_exceeded",
            )
            self.assertEqual(result["input_tokens"], 8_001)
            self.assertTrue(result["model_request_usage_complete"])

    async def test_abnormal_probe_marks_usage_and_cost_coverage_incomplete(self) -> None:
        first_assistant = KimiAssistantMessage(
            content="",
            tool_calls=(
                KimiToolCall(
                    "call_probe_failure",
                    "lookup_synthetic_metric",
                    '{"dataset_id":"kimi_synth_success_v1","metric_id":"effect_size"}',
                ),
            ),
        )
        responses = [
            _response(first_assistant, "tool_calls", 100, 20, 3),
            _response(KimiAssistantMessage(content="done"), "stop", 120, 30, 4),
        ]

        async def completion(*args, **kwargs):
            del args, kwargs
            return responses.pop(0)

        async def abnormal_probe(**kwargs):
            del kwargs
            raise KimiChatTransportError(
                "kimi_chat_invalid_request_probe_unexpected_status",
                http_status=200,
                network_calls=1,
            )

        with tempfile.TemporaryDirectory() as directory:
            result = await run_kimi_k3_handshake(
                project_root=ROOT,
                authorization_id="kimi-k3-handshake-probe-failure-001",
                authorization_expires_at_utc=EXPIRY,
                expected_plan_commitment_sha256=PLAN_COMMITMENT_SHA256,
                confirm_online=True,
                accept_locked_caps=True,
                attest_terms_and_pricing_unchanged=True,
                _key_loader=lambda: SAFE_KEY,
                _completion_runner=completion,
                _probe_runner=abnormal_probe,
                _clock=lambda: NOW,
                _artifact_directory=Path(directory),
            )
            self.assertEqual(
                result["error_code"],
                "kimi_chat_invalid_request_probe_unexpected_status",
            )
            self.assertEqual(result["network_attempts"], 3)
            self.assertEqual(result["network_calls"], 3)
            self.assertTrue(result["invalid_request_probe_attempted"])
            self.assertFalse(result["invalid_request_probe_semantics_verified"])
            self.assertEqual(result["invalid_request_probe_http_status"], 200)
            self.assertFalse(result["usage_complete"])
            self.assertEqual(
                result["local_observed_usage_cost_coverage"], "incomplete"
            )

    async def test_cancelled_inflight_call_records_attempt_and_unknown_outcome(self) -> None:
        async def cancelled_completion(*args, **kwargs):
            del args, kwargs
            raise asyncio.CancelledError

        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            with self.assertRaises(asyncio.CancelledError):
                await run_kimi_k3_handshake(
                    project_root=ROOT,
                    authorization_id="kimi-k3-handshake-cancel-001",
                    authorization_expires_at_utc=EXPIRY,
                    expected_plan_commitment_sha256=PLAN_COMMITMENT_SHA256,
                    confirm_online=True,
                    accept_locked_caps=True,
                    attest_terms_and_pricing_unchanged=True,
                    _key_loader=lambda: SAFE_KEY,
                    _completion_runner=cancelled_completion,
                    _probe_runner=AsyncMock(side_effect=AssertionError("no probe")),
                    _clock=lambda: NOW,
                    _artifact_directory=artifacts,
                )
            terminal_path = next(artifacts.glob("*.terminal.json"))
            terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            self.assertEqual(terminal["network_attempts"], 1)
            self.assertEqual(terminal["network_calls"], 0)
            self.assertFalse(terminal["network_call_observation_complete"])
            self.assertTrue(terminal["outcome_unknown"])

    async def test_expiry_after_consumption_stops_before_key_load(self) -> None:
        expiry = NOW + timedelta(seconds=300)
        values = iter((NOW, expiry, expiry))

        def clock() -> datetime:
            return next(values, expiry)

        def forbidden_key_loader() -> str:
            raise AssertionError("expired authorization must stop before Key load")

        with tempfile.TemporaryDirectory() as directory:
            result = await run_kimi_k3_handshake(
                project_root=ROOT,
                authorization_id="kimi-k3-handshake-expired-after-consume-001",
                authorization_expires_at_utc=(
                    expiry.isoformat().replace("+00:00", "Z")
                ),
                expected_plan_commitment_sha256=PLAN_COMMITMENT_SHA256,
                confirm_online=True,
                accept_locked_caps=True,
                attest_terms_and_pricing_unchanged=True,
                _key_loader=forbidden_key_loader,
                _completion_runner=AsyncMock(side_effect=AssertionError("no model")),
                _probe_runner=AsyncMock(side_effect=AssertionError("no probe")),
                _clock=clock,
                _artifact_directory=Path(directory),
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(
                result["error_code"],
                "kimi_k3_handshake_authorization_expired_during_run",
            )
            self.assertEqual(result["network_attempts"], 0)
            self.assertEqual(result["network_calls"], 0)
            self.assertEqual(len(list(Path(directory).iterdir())), 2)

    async def test_each_network_attempt_rechecks_absolute_expiry(self) -> None:
        expiry = NOW + timedelta(seconds=300)
        clock_values = iter((NOW, NOW, NOW, expiry, expiry))

        def clock() -> datetime:
            return next(clock_values, expiry)

        first_assistant = KimiAssistantMessage(
            content="",
            tool_calls=(
                KimiToolCall(
                    "call_deadline",
                    "lookup_synthetic_metric",
                    '{"dataset_id":"kimi_synth_success_v1","metric_id":"effect_size"}',
                ),
            ),
        )
        calls = 0

        async def completion(*args, **kwargs):
            nonlocal calls
            del args, kwargs
            calls += 1
            if calls > 1:
                raise AssertionError("second model request must not start")
            return _response(first_assistant, "tool_calls", 100, 20, 3)

        with tempfile.TemporaryDirectory() as directory:
            result = await run_kimi_k3_handshake(
                project_root=ROOT,
                authorization_id="kimi-k3-handshake-attempt-deadline-001",
                authorization_expires_at_utc=(
                    expiry.isoformat().replace("+00:00", "Z")
                ),
                expected_plan_commitment_sha256=PLAN_COMMITMENT_SHA256,
                confirm_online=True,
                accept_locked_caps=True,
                attest_terms_and_pricing_unchanged=True,
                _key_loader=lambda: SAFE_KEY,
                _completion_runner=completion,
                _probe_runner=AsyncMock(side_effect=AssertionError("no probe")),
                _clock=clock,
                _artifact_directory=Path(directory),
            )
            self.assertEqual(
                result["error_code"],
                "kimi_k3_handshake_authorization_expired_during_run",
            )
            self.assertEqual(calls, 1)
            self.assertEqual(result["model_requests"], 1)
            self.assertEqual(result["network_attempts"], 1)
            self.assertEqual(result["network_calls"], 1)
            self.assertEqual(result["tool_executions"], 1)
            self.assertFalse(result["invalid_request_probe_attempted"])

    async def test_probe_rechecks_absolute_expiry_after_two_model_responses(self) -> None:
        expiry = NOW + timedelta(seconds=300)
        clock_values = iter((NOW, NOW, NOW, NOW, expiry, expiry))

        def clock() -> datetime:
            return next(clock_values, expiry)

        first_assistant = KimiAssistantMessage(
            content="",
            tool_calls=(
                KimiToolCall(
                    "call_probe_deadline",
                    "lookup_synthetic_metric",
                    '{"dataset_id":"kimi_synth_success_v1","metric_id":"effect_size"}',
                ),
            ),
        )
        responses = [
            _response(first_assistant, "tool_calls", 100, 20, 3),
            _response(KimiAssistantMessage(content="done"), "stop", 120, 30, 4),
        ]

        async def completion(*args, **kwargs):
            del args, kwargs
            return responses.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            result = await run_kimi_k3_handshake(
                project_root=ROOT,
                authorization_id="kimi-k3-handshake-probe-deadline-001",
                authorization_expires_at_utc=(
                    expiry.isoformat().replace("+00:00", "Z")
                ),
                expected_plan_commitment_sha256=PLAN_COMMITMENT_SHA256,
                confirm_online=True,
                accept_locked_caps=True,
                attest_terms_and_pricing_unchanged=True,
                _key_loader=lambda: SAFE_KEY,
                _completion_runner=completion,
                _probe_runner=AsyncMock(
                    side_effect=AssertionError("expired probe must not start")
                ),
                _clock=clock,
                _artifact_directory=Path(directory),
            )
            self.assertEqual(
                result["error_code"],
                "kimi_k3_handshake_authorization_expired_during_run",
            )
            self.assertEqual(result["model_requests"], 2)
            self.assertEqual(result["network_attempts"], 2)
            self.assertEqual(result["network_calls"], 2)
            self.assertEqual(result["tool_executions"], 1)
            self.assertFalse(result["invalid_request_probe_attempted"])

    async def test_tool_execution_is_counted_before_result_validation(self) -> None:
        first_assistant = KimiAssistantMessage(
            content="",
            tool_calls=(
                KimiToolCall(
                    "call_invalid_result",
                    "lookup_synthetic_metric",
                    '{"dataset_id":"kimi_synth_success_v1","metric_id":"effect_size"}',
                ),
            ),
        )

        class InvalidResultExecutor:
            executed_total = 0

            def execute_batch(self, calls):
                del calls
                self.executed_total = 1
                return {
                    "status": "completed",
                    "executed_tool_call_count": 1,
                    "results": [{"result": {"status": "wrong"}}],
                }

        with tempfile.TemporaryDirectory() as directory, patch.object(
            handshake_module,
            "KimiSyntheticToolExecutor",
            InvalidResultExecutor,
        ):
            result = await run_kimi_k3_handshake(
                project_root=ROOT,
                authorization_id="kimi-k3-handshake-tool-count-001",
                authorization_expires_at_utc=EXPIRY,
                expected_plan_commitment_sha256=PLAN_COMMITMENT_SHA256,
                confirm_online=True,
                accept_locked_caps=True,
                attest_terms_and_pricing_unchanged=True,
                _key_loader=lambda: SAFE_KEY,
                _completion_runner=AsyncMock(
                    return_value=_response(
                        first_assistant, "tool_calls", 10, 2, 1
                    )
                ),
                _probe_runner=AsyncMock(side_effect=AssertionError("no probe")),
                _clock=lambda: NOW,
                _artifact_directory=Path(directory),
            )
            self.assertEqual(
                result["error_code"], "kimi_k3_handshake_tool_result_invalid"
            )
            self.assertEqual(result["tool_executions"], 1)

    async def test_all_preconsumption_gates_are_zero_call_and_do_not_load_key(self) -> None:
        cases = (
            (
                {"confirm_online": False},
                "kimi_k3_handshake_confirmation_required",
            ),
            (
                {"accept_locked_caps": False},
                "kimi_k3_handshake_locked_caps_not_accepted",
            ),
            (
                {"attest_terms_and_pricing_unchanged": False},
                "kimi_k3_handshake_terms_and_pricing_attestation_required",
            ),
            (
                {"expected_plan_commitment_sha256": "0" * 64},
                "kimi_k3_handshake_commitment_not_authorized",
            ),
        )
        for index, (override, expected) in enumerate(cases):
            with self.subTest(error=expected), tempfile.TemporaryDirectory() as directory:
                arguments = {
                    "project_root": ROOT,
                    "authorization_id": f"kimi-k3-handshake-gate-{index:03d}",
                    "authorization_expires_at_utc": EXPIRY,
                    "expected_plan_commitment_sha256": PLAN_COMMITMENT_SHA256,
                    "confirm_online": True,
                    "accept_locked_caps": True,
                    "attest_terms_and_pricing_unchanged": True,
                    "_key_loader": lambda: (_ for _ in ()).throw(
                        AssertionError("Key must not load")
                    ),
                    "_completion_runner": AsyncMock(
                        side_effect=AssertionError("model must not run")
                    ),
                    "_probe_runner": AsyncMock(
                        side_effect=AssertionError("probe must not run")
                    ),
                    "_clock": lambda: NOW,
                    "_artifact_directory": Path(directory),
                }
                arguments.update(override)
                result = await run_kimi_k3_handshake(**arguments)
                self.assertEqual(result["status"], "not_run")
                self.assertEqual(result["error_code"], expected)
                self.assertEqual(result["network_calls"], 0)
                self.assertFalse(any(Path(directory).iterdir()))

    async def test_missing_key_after_consumption_still_gets_terminal_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            result = await run_kimi_k3_handshake(
                project_root=ROOT,
                authorization_id="kimi-k3-handshake-missing-key-001",
                authorization_expires_at_utc=EXPIRY,
                expected_plan_commitment_sha256=PLAN_COMMITMENT_SHA256,
                confirm_online=True,
                accept_locked_caps=True,
                attest_terms_and_pricing_unchanged=True,
                _key_loader=lambda: None,
                _completion_runner=AsyncMock(side_effect=AssertionError("no model")),
                _probe_runner=AsyncMock(side_effect=AssertionError("no probe")),
                _clock=lambda: NOW,
                _artifact_directory=artifacts,
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error_code"], "kimi_k3_handshake_key_missing")
            self.assertEqual(result["network_calls"], 0)
            self.assertFalse(result["usage_complete"])
            self.assertEqual(len(list(artifacts.iterdir())), 2)

    async def test_expired_or_overlong_authorization_never_consumes(self) -> None:
        cases = (
            (
                "2026-08-31T15:00:00Z",
                "kimi_k3_handshake_authorization_expired",
            ),
            (
                "2026-08-31T15:04:59Z",
                "kimi_k3_handshake_authorization_window_too_short",
            ),
            (
                "2026-09-02T15:00:00Z",
                "kimi_k3_handshake_authorization_horizon_exceeded",
            ),
        )
        for index, (expiry, expected_error) in enumerate(cases):
            with self.subTest(error=expected_error), tempfile.TemporaryDirectory() as directory:
                artifacts = Path(directory)
                result = await run_kimi_k3_handshake(
                    project_root=ROOT,
                    authorization_id=f"kimi-k3-handshake-window-{index:03d}",
                    authorization_expires_at_utc=expiry,
                    expected_plan_commitment_sha256=PLAN_COMMITMENT_SHA256,
                    confirm_online=True,
                    accept_locked_caps=True,
                    attest_terms_and_pricing_unchanged=True,
                    _key_loader=lambda: (_ for _ in ()).throw(
                        AssertionError("Key must not load")
                    ),
                    _completion_runner=AsyncMock(
                        side_effect=AssertionError("model must not run")
                    ),
                    _probe_runner=AsyncMock(
                        side_effect=AssertionError("probe must not run")
                    ),
                    _clock=lambda: NOW,
                    _artifact_directory=artifacts,
                )
                self.assertEqual(result["status"], "not_run")
                self.assertEqual(result["error_code"], expected_error)
                self.assertFalse(any(artifacts.iterdir()))


class KimiK3HandshakeCliTests(unittest.TestCase):
    def test_validate_cli_is_zero_call(self) -> None:
        exit_code, result = _invoke(["validate"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["plan_commitment_sha256"], PLAN_COMMITMENT_SHA256)
        self.assertEqual(result["network_calls"], 0)
        self.assertFalse(result["key_loaded"])

    def test_run_cli_has_no_key_argument_and_passes_private_loader(self) -> None:
        parser = handshake_cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if action.__class__.__name__ == "_SubParsersAction"
        )
        run_parser = subparsers.choices["run"]
        options = {
            option
            for action in run_parser._actions
            for option in action.option_strings
        }
        self.assertNotIn("--api-key", options)
        success = {
            "status": "success",
            "handshake_id": HANDSHAKE_ID,
            "network_calls": 3,
        }
        runner = AsyncMock(return_value=success)
        with patch.object(handshake_cli, "run_kimi_k3_handshake", runner):
            exit_code, result = _invoke(
                [
                    "run",
                    "--confirm-online",
                    "--accept-locked-caps",
                    "--attest-terms-and-pricing-unchanged",
                    "--authorization-id",
                    AUTHORIZATION_ID,
                    "--authorization-expires-at-utc",
                    EXPIRY,
                    "--expected-plan-commitment",
                    PLAN_COMMITMENT_SHA256,
                ]
            )
        self.assertEqual((exit_code, result["status"]), (0, "success"))
        kwargs = runner.await_args.kwargs
        self.assertNotIn("api_key", kwargs)
        self.assertTrue(callable(kwargs["_key_loader"]))
        self.assertEqual(kwargs["expected_plan_commitment_sha256"], PLAN_COMMITMENT_SHA256)


if __name__ == "__main__":
    unittest.main()
