from __future__ import annotations

import hashlib
import json
import re
import unittest
import asyncio
from pathlib import Path
from typing import Any

import httpx

from researchops.kimi_chat_transport import (
    KIMI_CHAT_MODEL_ID,
    KimiChatRequest,
    KimiChatTransportError,
    KimiTextMessage,
    run_kimi_chat_completion,
)


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence" / "kimi-controlled-pilot-usage-failure-v1"
PROJECTION = EVIDENCE / "public_receipt_projection.json"
ARTIFACTS = EVIDENCE / "artifact_commitments.json"
SOURCES = EVIDENCE / "public_source_commitments.json"

_FORBIDDEN_KEYS = {
    "authorization_id",
    "authorization_id_sha256",
    "authorization_binding_sha256",
    "terms_authorization_binding_sha256",
    "pricing_authorization_binding_sha256",
    "terms_attestation_sha256",
    "pricing_attestation_sha256",
    "api_key",
    "authorization_header",
    "raw_prompt",
    "raw_request",
    "raw_response",
    "reasoning_content",
    "tool_arguments",
    "tool_results",
    "account_id",
    "project_id",
    "participant_id",
    "contact_email",
    "private_locator",
    "private_task_id",
    "golden",
    "per_case_result",
}
_FORBIDDEN_VALUES = (
    re.compile(r"\bBearer\s+", re.IGNORECASE),
    re.compile(r"MOONSHOT_API_KEY", re.IGNORECASE),
    re.compile(r"\b(?:sk|key|token|secret)[-_][A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\b(?:api[_-]?key|token|secret)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"[A-Za-z]:\\"),
    re.compile(r"\\\\[A-Za-z0-9._-]+\\"),
    re.compile(r"(?:^|[/\\])(?:home|tmp|Users)(?:[/\\])", re.IGNORECASE),
    re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE),
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
    )
    if not isinstance(payload, dict):
        raise ValueError("expected object")
    return payload


def _scan_public_value(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _FORBIDDEN_KEYS:
                raise AssertionError(f"forbidden public key: {key}")
            _scan_public_value(child)
    elif isinstance(value, list):
        for child in value:
            _scan_public_value(child)
    elif isinstance(value, str):
        for pattern in _FORBIDDEN_VALUES:
            if pattern.search(value):
                raise AssertionError("forbidden public value")


def _scan_public_text(value: str) -> None:
    for pattern in _FORBIDDEN_VALUES:
        if pattern.search(value):
            raise AssertionError("forbidden public text")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class KimiControlledPilotFailureEvidenceTests(unittest.TestCase):
    def test_public_bundle_files_are_byte_frozen(self) -> None:
        expected = {
            "README.md": "9297dbf27385078515dd3981d068fda32b827bfa46a10e73844f28165b447f03",
            "artifact_commitments.json": "5159ed6ff5d143406fb3fddc06387a98927a107d3e313c1a4244e4df4ad50ba3",
            "public_receipt_projection.json": "156ecbdc73206663aa9c87aa874ce7ace93133dcbba18fb6c6a9431237dad1d1",
            "public_source_commitments.json": "2642583ef8a5c80c3281d808338714b37f389e4a0202a43f4fb2cef967798bc3",
        }
        self.assertEqual(
            {name: _sha256(EVIDENCE / name) for name in expected}, expected
        )

    def test_bundle_contains_only_public_projection_and_commitments(self) -> None:
        self.assertEqual(
            {path.name for path in EVIDENCE.iterdir()},
            {
                "README.md",
                "public_receipt_projection.json",
                "artifact_commitments.json",
                "public_source_commitments.json",
            },
        )
        for payload in (_load(PROJECTION), _load(ARTIFACTS), _load(SOURCES)):
            _scan_public_value(payload)
        _scan_public_text((EVIDENCE / "README.md").read_text(encoding="utf-8"))

    def test_public_projection_preserves_failure_and_unknown_usage_boundary(self) -> None:
        payload = _load(PROJECTION)
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "public_evidence_id",
                "documented_at_utc",
                "candidate_snapshot",
                "contract",
                "outcome",
                "usage",
                "cost",
                "postmortem",
                "authorization_and_claim_boundary",
            },
        )
        self.assertEqual(payload["schema_version"], "kimi-controlled-pilot-public-projection/1.0")
        self.assertEqual(payload["documented_at_utc"], "2026-08-26T16:20:48.119Z")
        self.assertEqual(
            set(payload["candidate_snapshot"]),
            {"candidate_id", "candidate_commitment_sha256", "candidate_file_sha256", "result_inherited"},
        )
        self.assertEqual(set(payload["contract"]), {"contract_id", "contract_sha256"})
        self.assertEqual(
            set(payload["outcome"]),
            {
                "status", "error_code", "g4_decision", "scenarios_planned",
                "scenarios_completed", "model_request_limit", "model_request_count",
                "network_attempts", "network_calls", "requested_tool_call_count",
                "deduplicated_tool_call_count", "executed_tool_call_count",
                "expected_invalid_request_count", "outcome_unknown", "wall_elapsed_ms",
                "provider_latency_ms",
            },
        )
        self.assertEqual(
            set(payload["usage"]),
            {
                "usage_complete", "usage_observed_request_count", "input_tokens",
                "output_tokens", "actual_token_count_unknown", "reserved_input_tokens",
                "reserved_output_tokens",
            },
        )
        self.assertEqual(
            set(payload["cost"]),
            {
                "currency", "local_reserved_estimated_cost",
                "local_estimated_reservation_limit", "usage_based_estimated_cost",
                "actual_billed_cost", "actual_bill_unknown", "claim_scope",
            },
        )
        self.assertEqual(
            set(payload["postmortem"]),
            {
                "raw_provider_body_persisted", "observed_provider_payload_shape_known",
                "documented_parser_contract_mismatch_confirmed", "causal_root_cause",
                "causal_root_cause_claim_allowed",
            },
        )
        self.assertEqual(
            set(payload["authorization_and_claim_boundary"]),
            {
                "authorization_consumed", "authorization_identifier_or_hash_published",
                "authorization_derived_binding_hashes_published", "candidate_result_created",
                "authorizes_retry", "authorizes_resume", "authorizes_chat",
                "authorizes_tools", "authorizes_model_quality_claim",
                "authorizes_provider_registration", "authorizes_private_evaluation",
                "authorizes_non_synthetic_data", "compatibility_verified",
            },
        )
        self.assertEqual(payload["outcome"]["status"], "failed")
        self.assertEqual(payload["outcome"]["error_code"], "kimi_chat_usage_invalid")
        self.assertEqual(payload["outcome"]["g4_decision"], "planned_not_registered")
        self.assertEqual(payload["outcome"]["scenarios_completed"], 0)
        self.assertEqual(payload["outcome"]["model_request_count"], 1)
        self.assertEqual(payload["outcome"]["network_calls"], 1)
        self.assertEqual(payload["outcome"]["executed_tool_call_count"], 0)
        self.assertFalse(payload["outcome"]["outcome_unknown"])
        self.assertEqual(payload["outcome"]["wall_elapsed_ms"], 15921)
        self.assertIsNone(payload["outcome"]["provider_latency_ms"])

        usage = payload["usage"]
        self.assertFalse(usage["usage_complete"])
        self.assertEqual(usage["usage_observed_request_count"], 0)
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["output_tokens"])
        self.assertTrue(usage["actual_token_count_unknown"])

        cost = payload["cost"]
        self.assertEqual(cost["local_reserved_estimated_cost"], "0.313600")
        self.assertIsNone(cost["usage_based_estimated_cost"])
        self.assertIsNone(cost["actual_billed_cost"])
        self.assertTrue(cost["actual_bill_unknown"])

        postmortem = payload["postmortem"]
        self.assertFalse(postmortem["raw_provider_body_persisted"])
        self.assertFalse(postmortem["observed_provider_payload_shape_known"])
        self.assertTrue(postmortem["documented_parser_contract_mismatch_confirmed"])
        self.assertEqual(
            postmortem["causal_root_cause"],
            "undetermined_with_confirmed_parser_contract_mismatch",
        )
        self.assertFalse(postmortem["causal_root_cause_claim_allowed"])

        claims = payload["authorization_and_claim_boundary"]
        self.assertTrue(claims["authorization_consumed"])
        for key, value in claims.items():
            if key != "authorization_consumed":
                self.assertFalse(value, key)

    def test_artifact_and_source_commitments_are_exact_and_non_authorizing(self) -> None:
        artifacts = _load(ARTIFACTS)
        self.assertEqual(
            set(artifacts),
            {
                "schema_version", "public_evidence_id", "original_artifacts_published",
                "original_artifacts_present_at_verification_utc", "opaque_commitments",
                "event_chain", "offline_verification", "publication_omissions",
            },
        )
        self.assertFalse(artifacts["original_artifacts_published"])
        self.assertEqual(
            artifacts["original_artifacts_present_at_verification_utc"],
            "2026-08-26T16:09:04.316Z",
        )
        self.assertEqual(
            artifacts["opaque_commitments"],
            {
                "receipt.json": {
                    "bytes": 4153,
                    "sha256": "1d71c8c92eedaddefd55296ea0b941e80cc0519663a72edf7441fd2f0fa02df5",
                },
                "checkpoint.json": {
                    "bytes": 3591,
                    "sha256": "4c1633fa4301774c0b0039b6db172065ff03ea4b4a4d7b3faaf4837e642c4c67",
                },
                "event_chain.jsonl": {
                    "bytes": 5691,
                    "sha256": "b4d31b3d0918b42f9c8d439d260b4a970799ff44c01bb4c00fa17db282fea5e3",
                },
            },
        )
        self.assertEqual(artifacts["event_chain"]["event_count"], 4)
        self.assertEqual(set(artifacts["event_chain"]), {"event_count", "head_sha256"})
        self.assertEqual(
            artifacts["event_chain"]["head_sha256"],
            "559eab399fcb795dbb5c90495efe39ec90ff1a067df400f8cf2003b280e51f59",
        )
        self.assertTrue(artifacts["offline_verification"]["projection_valid"])
        self.assertEqual(artifacts["offline_verification"]["network_calls"], 0)
        self.assertEqual(
            set(artifacts["offline_verification"]),
            {
                "projection_valid", "hash_chain_recomputed_valid",
                "artifact_sanitizer_valid", "network_calls",
            },
        )
        self.assertEqual(
            set(artifacts["publication_omissions"]),
            {
                "authorization_identifier_or_hash", "authorization_derived_bindings",
                "auth_bound_attestation_hashes", "raw_request_or_response",
                "prompt_reasoning_or_tool_payload",
                "path_email_account_project_or_participant_identifier",
            },
        )

        sources = _load(SOURCES)
        self.assertEqual(
            set(sources),
            {
                "schema_version", "pilot_gate_sources_captured_at_utc",
                "capture_method", "sources", "boundary",
            },
        )
        self.assertEqual(
            set(sources["sources"]),
            {
                "service_agreement", "privacy_policy", "payment_agreement",
                "kimi_k3_pricing", "chat_completions_documentation",
            },
        )
        legal_fields = {"url", "http_status", "bytes", "sha256"}
        for source_id in ("service_agreement", "privacy_policy", "payment_agreement"):
            self.assertEqual(set(sources["sources"][source_id]), legal_fields)
        self.assertEqual(
            set(sources["sources"]["kimi_k3_pricing"]),
            {
                "url", "http_status", "bytes", "sha256", "currency",
                "billing_unit_tokens", "cached_input_per_million",
                "uncached_input_per_million", "output_per_million",
            },
        )
        self.assertEqual(
            set(sources["sources"]["chat_completions_documentation"]),
            {
                "url", "captured_at_utc", "http_status", "bytes", "sha256",
                "documented_terminal_layout",
            },
        )
        self.assertEqual(
            set(sources["boundary"]),
            {
                "displayed_updated_date", "displayed_effective_date",
                "g2a_material_delta_observed",
                "documented_parser_contract_mismatch_reproduced_offline",
                "is_t2b_final_effective_terms_evidence",
                "is_t6_governance_completion_evidence", "authorizes_future_provider_call",
            },
        )
        self.assertFalse(sources["boundary"]["g2a_material_delta_observed"])
        self.assertTrue(
            sources["boundary"]["documented_parser_contract_mismatch_reproduced_offline"]
        )
        self.assertFalse(sources["boundary"]["is_t2b_final_effective_terms_evidence"])
        self.assertFalse(sources["boundary"]["is_t6_governance_completion_evidence"])
        self.assertFalse(sources["boundary"]["authorizes_future_provider_call"])
        self.assertEqual(
            sources["sources"]["kimi_k3_pricing"]["sha256"],
            "8555270b9dc88fcef0704abb8d02033137f6e67c6c23e9b09592658cd33bd38b",
        )
        self.assertEqual(
            sources["sources"]["chat_completions_documentation"],
            {
                "url": "https://platform.kimi.com/docs/api/chat",
                "captured_at_utc": "2026-08-26T16:15:03.000Z",
                "http_status": 200,
                "bytes": 826200,
                "sha256": "8aceb197e56b47dc73c1f06377462ed33e36c126f4e7d5459294b0746b94d43a",
                "documented_terminal_layout": (
                    "finish_reason_and_usage_in_same_final_data_chunk_before_done"
                ),
            },
        )

    def test_documented_same_chunk_shape_reproduces_independent_parser_mismatch(self) -> None:
        completion_id = "cmpl-offline-documented-terminal-shape"

        def event(payload: dict[str, Any]) -> bytes:
            return (
                "data: "
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + "\n\n"
            ).encode("utf-8")

        def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
            return {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": 1_787_702_400,
                "model": KIMI_CHAT_MODEL_ID,
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": finish_reason}
                ],
            }

        final = chunk({}, "stop")
        final["usage"] = {
            "prompt_tokens": 19,
            "completion_tokens": 13,
            "total_tokens": 32,
            "cached_tokens": 12,
        }
        stream = b"".join(
            (
                event(chunk({"role": "assistant", "reasoning_content": "synthetic"})),
                event(chunk({"content": "synthetic answer"})),
                event(final),
                b"data: [DONE]\n\n",
            )
        )

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.assertTrue(body["stream"])
            self.assertEqual(body["stream_options"], {"include_usage": True})
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=stream,
            )

        async def run() -> None:
            await run_kimi_chat_completion(
                KimiChatRequest(
                    messages=(KimiTextMessage("user", "fresh synthetic fixture"),),
                    max_completion_tokens=1,
                    reasoning_effort="low",
                ),
                api_key="offline-nonsecret-test-value-0123456789",
                confirm_online=True,
                _transport_factory=lambda: httpx.MockTransport(handler),
            )

        with self.assertRaises(KimiChatTransportError) as caught:
            asyncio.run(run())
        self.assertEqual(caught.exception.code, "kimi_chat_response_invalid")
        self.assertEqual(caught.exception.http_status, 200)
        self.assertEqual(caught.exception.network_calls, 1)
        self.assertIsNone(caught.exception.usage)

    def test_sanitizer_rejects_synthetic_sensitive_and_extra_field_canaries(self) -> None:
        for payload in (
            {"account_id": "synthetic-account"},
            {"project_id": "synthetic-project"},
            {"participant_id": "synthetic-participant"},
            {"private_locator": "synthetic-private-location"},
            {"safe": "sk-syntheticcanary123456"},
            {"safe": "token=syntheticcanary123456"},
            {"safe": r"\\synthetic-host\share"},
            {"safe": r"C:\synthetic\path"},
            {"safe": "synthetic@example.invalid"},
        ):
            with self.subTest(payload=payload), self.assertRaises(AssertionError):
                _scan_public_value(payload)

        claims = dict(_load(PROJECTION)["authorization_and_claim_boundary"])
        claims["unexpected_extra_field"] = False
        self.assertNotEqual(
            set(claims),
            {
                "authorization_consumed", "authorization_identifier_or_hash_published",
                "authorization_derived_binding_hashes_published", "candidate_result_created",
                "authorizes_retry", "authorizes_resume", "authorizes_chat",
                "authorizes_tools", "authorizes_model_quality_claim",
                "authorizes_provider_registration", "authorizes_private_evaluation",
                "authorizes_non_synthetic_data", "compatibility_verified",
            },
        )

    def test_candidate_and_frozen_contracts_remain_pre_call_snapshots(self) -> None:
        self.assertEqual(
            _sha256(ROOT / "evals" / "v2" / "public_regression_candidate_v6.json"),
            "a6b91f68eda6aee4f435ab091e81034ed93971f4358675945d22d5b70daba657",
        )
        self.assertEqual(
            _sha256(ROOT / "evals" / "v2" / "kimi_chat_completions_contract.json"),
            "a8a7df576175fe003de7ef09f584bd71823473c3aed1addf8977cd307ae38f8d",
        )
        self.assertEqual(
            _sha256(ROOT / "evals" / "v2" / "kimi_runtime_candidate_v6_contract.json"),
            "a8eadab42390f8ad62239387383702ee916d5fa347329a0137edf739d162d3db",
        )
        self.assertEqual(
            _sha256(ROOT / "src" / "researchops" / "kimi_chat_transport.py"),
            "6e4be581ebf3d11c96bec4cb4cb8d58de62c24f8b025912cc1af5a3774977279",
        )
        projection = _load(PROJECTION)
        self.assertFalse(projection["candidate_snapshot"]["result_inherited"])


if __name__ == "__main__":
    unittest.main()
