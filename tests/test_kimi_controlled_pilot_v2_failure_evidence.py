from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/evidence/kimi-controlled-pilot-v2-response-failure-v1"

_FORBIDDEN_KEYS = {
    "authorization_id",
    "authorization_id_sha256",
    "authorization_binding_sha256",
    "terms_authorization_binding_sha256",
    "pricing_authorization_binding_sha256",
    "legacy_successor_tombstone_sha256",
    "event_chain_head_sha256",
    "event_hash",
    "prev_hash",
    "api_key",
    "authorization_header",
    "raw_prompt",
    "raw_request",
    "raw_response",
    "reasoning_content",
    "tool_arguments",
    "tool_results",
    "account_id",
    "organization_id",
    "project_id",
    "participant_id",
    "provider_request_id",
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


def _load(name: str) -> dict[str, Any]:
    value = json.loads(
        (EVIDENCE / name).read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def _scan(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _FORBIDDEN_KEYS:
                raise AssertionError(f"forbidden key: {key}")
            _scan(child)
    elif isinstance(value, list):
        for child in value:
            _scan(child)
    elif isinstance(value, str):
        for pattern in _FORBIDDEN_VALUES:
            if pattern.search(value):
                raise AssertionError("forbidden value")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class KimiControlledPilotV2FailureEvidenceTests(unittest.TestCase):
    def test_public_bundle_files_are_byte_frozen(self) -> None:
        expected = {
            "README.md": "da0ed36a09fafcff4eb4129350a8cb11a069972ff95b75342918dd1d7abd741b",
            "artifact_commitments.json": "9c8b3180bfdac1268a6e1811f47f1e59c5e1fdbce67c0c3fc1db7aa8feaec219",
            "public_receipt_projection.json": "694f871d9a66673f5357330240c2ee63f09c3aff3e50089117cae168cc2bc680",
            "public_source_commitments.json": "51e4e21de458a5e309a277d5ee7dd6c74df6cdc6248ab4d08d470d95cb9423af",
        }
        self.assertEqual(
            {name: _sha(EVIDENCE / name) for name in expected}, expected
        )

    def test_bundle_is_minimal_strict_json_and_sanitized(self) -> None:
        self.assertEqual(
            {path.name for path in EVIDENCE.iterdir()},
            {
                "README.md",
                "public_receipt_projection.json",
                "artifact_commitments.json",
                "public_source_commitments.json",
            },
        )
        for name in (
            "public_receipt_projection.json",
            "artifact_commitments.json",
            "public_source_commitments.json",
        ):
            _scan(_load(name))
        _scan((EVIDENCE / "README.md").read_text(encoding="utf-8"))

    def test_public_projection_preserves_failure_and_unknown_measurements(self) -> None:
        payload = _load("public_receipt_projection.json")
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "public_evidence_id",
                "documented_at_utc",
                "candidate_snapshot",
                "contracts",
                "outcome",
                "usage",
                "cost",
                "observation_boundary",
                "authorization_and_claim_boundary",
            },
        )
        self.assertEqual(
            set(payload["candidate_snapshot"]),
            {
                "candidate_id", "candidate_commitment_sha256", "candidate_file_sha256",
                "predecessor_candidate_commitment_sha256", "result_inherited",
            },
        )
        self.assertEqual(
            set(payload["contracts"]),
            {
                "chat_contract_id", "chat_contract_sha256", "pilot_contract_id",
                "pilot_contract_sha256",
            },
        )
        self.assertEqual(
            set(payload["outcome"]),
            {
                "status", "error_code", "event_description", "g4_decision",
                "scenarios_planned", "scenarios_completed", "model_request_limit",
                "model_request_count", "network_attempts", "network_calls",
                "requested_tool_call_count", "deduplicated_tool_call_count",
                "executed_tool_call_count", "expected_invalid_request_count",
                "outcome_unknown", "wall_elapsed_ms", "provider_latency_ms",
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
            set(payload["observation_boundary"]),
            {
                "raw_provider_headers_persisted", "raw_provider_body_persisted",
                "observed_provider_payload_shape_known", "causal_root_cause",
                "causal_provider_fault_claim_allowed", "usage_compatibility_verified",
                "tool_compatibility_verified", "error_semantics_verified",
            },
        )
        self.assertEqual(
            set(payload["authorization_and_claim_boundary"]),
            {
                "authorization_consumed", "authorization_identifier_or_hash_published",
                "authorization_derived_binding_hashes_published", "candidate_result_created",
                "authorizes_retry", "authorizes_resume", "authorizes_model_quality_claim",
                "authorizes_provider_registration", "authorizes_private_evaluation",
                "authorizes_non_synthetic_data", "compatibility_verified",
            },
        )
        outcome = payload["outcome"]
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["error_code"], "kimi_chat_response_invalid")
        self.assertEqual(outcome["event_description"], "local_response_validation_failure")
        self.assertEqual(outcome["g4_decision"], "planned_not_registered")
        self.assertEqual(outcome["model_request_count"], 1)
        self.assertEqual(outcome["network_attempts"], 1)
        self.assertEqual(outcome["network_calls"], 1)
        self.assertEqual(outcome["scenarios_completed"], 0)
        self.assertEqual(outcome["executed_tool_call_count"], 0)
        self.assertFalse(outcome["outcome_unknown"])
        self.assertEqual(outcome["wall_elapsed_ms"], 21378)
        self.assertIsNone(outcome["provider_latency_ms"])

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

        observation = payload["observation_boundary"]
        self.assertFalse(observation["raw_provider_headers_persisted"])
        self.assertFalse(observation["raw_provider_body_persisted"])
        self.assertFalse(observation["observed_provider_payload_shape_known"])
        self.assertEqual(
            observation["causal_root_cause"],
            "undetermined_without_raw_provider_payload",
        )
        self.assertFalse(observation["causal_provider_fault_claim_allowed"])
        self.assertFalse(observation["usage_compatibility_verified"])
        self.assertFalse(observation["tool_compatibility_verified"])
        self.assertFalse(observation["error_semantics_verified"])

        claims = payload["authorization_and_claim_boundary"]
        self.assertTrue(claims["authorization_consumed"])
        for key, value in claims.items():
            if key != "authorization_consumed":
                self.assertFalse(value, key)

    def test_opaque_commitments_and_offline_verifier_projection_are_exact(self) -> None:
        artifacts = _load("artifact_commitments.json")
        self.assertEqual(
            set(artifacts),
            {
                "schema_version", "public_evidence_id", "original_artifacts_published",
                "original_artifacts_present_at_verification_utc",
                "opaque_commitments_are_intentionally_linkable", "opaque_commitments",
                "event_count", "offline_verification", "publication_omissions",
            },
        )
        self.assertFalse(artifacts["original_artifacts_published"])
        self.assertTrue(artifacts["opaque_commitments_are_intentionally_linkable"])
        self.assertEqual(artifacts["event_count"], 4)
        self.assertEqual(
            artifacts["opaque_commitments"],
            {
                "event_chain.jsonl": {
                    "bytes": 7845,
                    "sha256": "9cc743a7704569f81bddf720a1cf59ce3075b67b27c85591191a51aa07d3cf6e",
                },
                "checkpoint.json": {
                    "bytes": 4131,
                    "sha256": "71f05ffe794a3aaecddc44cd2b854c6b50795dbe140e033c8ff514d9d6e83068",
                },
                "receipt.json": {
                    "bytes": 4693,
                    "sha256": "6288004b430d0dd99e3cd6c4edfda3a741fa4c921671efe3f39c81807138296e",
                },
            },
        )
        verification = artifacts["offline_verification"]
        self.assertEqual(
            set(verification),
            {
                "strict_json_valid", "hash_chain_valid", "event_fsm_valid",
                "checkpoint_receipt_projection_valid", "candidate_binding_valid",
                "legacy_tombstone_valid", "artifact_sanitizer_valid", "network_calls",
            },
        )
        self.assertEqual(
            set(artifacts["publication_omissions"]),
            {
                "authorization_identifier_or_hash", "authorization_derived_bindings",
                "auth_bound_attestation_or_tombstone_hashes", "event_hashes_or_chain_head",
                "exact_pilot_operation_times", "raw_request_headers_or_response",
                "prompt_reasoning_or_tool_payload",
                "path_email_account_project_user_or_provider_identifier",
            },
        )
        for key, value in verification.items():
            if key == "network_calls":
                self.assertEqual(value, 0)
            else:
                self.assertTrue(value, key)

    def test_public_source_commitments_are_fresh_and_non_authorizing(self) -> None:
        sources = _load("public_source_commitments.json")
        self.assertEqual(
            set(sources),
            {
                "schema_version", "captured_at_utc_upper_bound", "capture_method",
                "sources", "boundary",
            },
        )
        self.assertEqual(
            set(sources["sources"]),
            {"service_agreement", "privacy_policy", "payment_agreement", "kimi_k3_pricing"},
        )
        for source_id in ("service_agreement", "privacy_policy", "payment_agreement"):
            self.assertEqual(
                set(sources["sources"][source_id]),
                {"url", "http_status", "bytes", "sha256"},
            )
        self.assertEqual(
            set(sources["sources"]["kimi_k3_pricing"]),
            {
                "url", "http_status", "bytes", "sha256", "currency",
                "billing_unit_tokens", "cached_input_per_million",
                "uncached_input_per_million", "output_per_million",
            },
        )
        self.assertEqual(
            set(sources["boundary"]),
            {
                "displayed_updated_date", "displayed_effective_date",
                "g2a_material_delta_observed", "is_t2b_final_effective_terms_evidence",
                "is_t6_governance_completion_evidence", "authorizes_future_provider_call",
            },
        )
        self.assertEqual(
            sources["sources"]["service_agreement"]["sha256"],
            "b535f31fe6ab4ebdb3b209a6c37fd3a9d695f401a88a9da8c0f2c994b35a3d39",
        )
        self.assertEqual(
            sources["sources"]["privacy_policy"]["sha256"],
            "a7319475ba358d1f4f0fb1090b92bbf218b7636513bdde0d95d9a056d2593470",
        )
        self.assertEqual(
            sources["sources"]["payment_agreement"]["sha256"],
            "dfb152944996c0263ce17fa97cf7e3898ab6cf2c0f2aa6c997df670464b10a31",
        )
        self.assertEqual(
            sources["sources"]["kimi_k3_pricing"]["sha256"],
            "03069960d8418331f3fc789e4e7b6750d1192bbc7d2a6994af0742146a7a69b4",
        )
        for key, value in sources["boundary"].items():
            if key in {"displayed_updated_date", "displayed_effective_date"}:
                continue
            self.assertFalse(value, key)

    def test_candidate_contracts_remain_frozen_and_online_template_removed(self) -> None:
        self.assertEqual(
            _sha(ROOT / "evals/v2/public_regression_candidate_v7.json"),
            "efc6ca2bbd97abe6c659983a386784938a74614cd1028861c25e20478ce7b278",
        )
        self.assertEqual(
            _sha(ROOT / "evals/v2/kimi_chat_completions_contract_v2.json"),
            "eb226578df2555813fbef005e366bb014d05bbb0dfde039b170689fc5a00916c",
        )
        self.assertEqual(
            _sha(ROOT / "evals/v2/kimi_controlled_pilot_contract_v2.json"),
            "6c305f7bf53ec2b10dca16a4fdbec157d3cbfcb8a6804e58641c5c0d848ff605",
        )
        runbook = (ROOT / "docs/KIMI_CONTROLLED_PILOT_V2_RUNBOOK.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("--confirm-online", runbook)
        for retired_online_flag in (
            "--accept-successor-v7-locked-caps",
            "--authorized-candidate-commitment",
            "--authorization-id",
            "--authorization-expires-at-utc",
            "--terms-retrieved-at-utc",
            "--terms-service-sha256",
            "--terms-privacy-sha256",
            "--terms-payment-sha256",
            "--attest-no-material-terms-delta",
            "--pricing-retrieved-at-utc",
            "--pricing-source-sha256",
            "--pricing-source-bytes",
            "--attest-kimi-k3-pricing-unchanged",
        ):
            self.assertNotIn(retired_online_flag, runbook)
        self.assertIn("Consumed v7 execution — no online command", runbook)


if __name__ == "__main__":
    unittest.main()
