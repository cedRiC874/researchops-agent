from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from researchops.kimi_chat_transport import KIMI_CHAT_STABLE_ERROR_CODES
from researchops.kimi_chat_transport_v3 import (
    KIMI_CHAT_V3_OFFICIAL_SOURCE_COMMITMENTS,
    RESPONSE_VALIDATION_DIAGNOSTIC_CODES,
    RESPONSE_VALIDATION_DIAGNOSTIC_SCHEMA_VERSION,
    kimi_chat_completions_v3_contract,
)


_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_PATH = _ROOT / "evals" / "v2" / "kimi_chat_completions_contract_v3.json"
_CANONICAL_SHA256 = "40e3257e4740b9c578b8f1471e777cc088250947a10adc7ab36267654a6f6200"
_RAW_SHA256 = "464b87bb4a1db9b66c252ef502a73dfb5037637ad75c3619fbc61349fad873c2"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _load_contract() -> dict[str, object]:
    value = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("v3 contract must be an object")
    return value


class KimiChatCompletionsContractV3Tests(unittest.TestCase):
    def test_json_is_exact_factory_output_and_canonical_snapshot(self) -> None:
        raw = _CONTRACT_PATH.read_bytes()
        snapshot = _load_contract()
        self.assertEqual(snapshot, kimi_chat_completions_v3_contract())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), _RAW_SHA256)
        self.assertEqual(
            hashlib.sha256(_canonical_bytes(snapshot)).hexdigest(),
            _CANONICAL_SHA256,
        )

    def test_factory_returns_defensive_deterministic_objects(self) -> None:
        first = kimi_chat_completions_v3_contract()
        second = kimi_chat_completions_v3_contract()
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first["provider"]["model_id"] = "mutated"  # type: ignore[index]
        self.assertEqual(second["provider"]["model_id"], "kimi-k3")  # type: ignore[index]

    def test_predecessor_contract_and_source_commitments_are_real(self) -> None:
        snapshot = _load_contract()
        predecessor = snapshot["predecessor"]
        self.assertEqual(
            predecessor,
            {
                "contract_id": "eval-v2-kimi-chat-completions-v2",
                "contract_path": "evals/v2/kimi_chat_completions_contract_v2.json",
                "contract_sha256": "eb226578df2555813fbef005e366bb014d05bbb0dfde039b170689fc5a00916c",
                "transport_source_path": "src/researchops/kimi_chat_transport_v2.py",
                "transport_source_sha256": "0e62e6b696a43804ef36bd1b7c1422cb0b9d7a974544d2afe50f5b7c6e2af8ae",
                "results_inherited": False,
                "authorization_inherited": False,
            },
        )
        self.assertEqual(
            hashlib.sha256(
                (_ROOT / predecessor["contract_path"]).read_bytes()  # type: ignore[index]
            ).hexdigest(),
            predecessor["contract_sha256"],  # type: ignore[index]
        )
        self.assertEqual(
            hashlib.sha256(
                (_ROOT / predecessor["transport_source_path"]).read_bytes()  # type: ignore[index]
            ).hexdigest(),
            predecessor["transport_source_sha256"],  # type: ignore[index]
        )

    def test_three_first_party_source_commitments_are_exact(self) -> None:
        snapshot = _load_contract()
        expected = [
            {
                "source_id": source.source_id,
                "url": source.url,
                "captured_at_utc": source.captured_at_utc,
                "decoded_bytes": source.decoded_bytes,
                "sha256": source.sha256,
            }
            for source in KIMI_CHAT_V3_OFFICIAL_SOURCE_COMMITMENTS
        ]
        sources = snapshot["official_source_commitments"]  # type: ignore[index]
        self.assertEqual(sources["sources"], expected)
        self.assertEqual(len(expected), 3)
        self.assertTrue(
            all(source["url"].startswith("https://platform.kimi.com/") for source in expected)
        )
        self.assertFalse(sources["authorizes_provider_call"])
        self.assertFalse(sources["is_effective_terms_evidence"])

    def test_terminal_and_usage_semantics_are_fail_closed(self) -> None:
        snapshot = _load_contract()
        stream = snapshot["stream_contract"]  # type: ignore[index]
        usage = snapshot["usage_contract"]  # type: ignore[index]
        self.assertEqual(stream["choice_count"], 1)
        self.assertEqual(stream["choice_index"], 0)
        self.assertTrue(stream["terminal_choice_nonempty_required"])
        self.assertTrue(stream["terminal_finish_and_usage_same_event_required"])
        self.assertEqual(
            stream["terminal_usage_projection_allowlist"],
            ["top_level_only", "choice_level_only", "both_reconciled"],
        )
        self.assertFalse(stream["openai_empty_choices_usage_only_allowed"])
        self.assertEqual(stream["finish_reason_exact_count"], 1)
        self.assertEqual(stream["logical_usage_exact_count"], 1)
        self.assertEqual(stream["done_marker_exact_count"], 1)
        self.assertTrue(stream["done_must_immediately_follow_terminal_data"])
        self.assertFalse(stream["data_after_terminal_allowed"])
        self.assertFalse(stream["partial_tool_result_exposed"])

        self.assertEqual(
            usage["required_fields"],
            ["prompt_tokens", "completion_tokens", "total_tokens"],
        )
        self.assertEqual(usage["optional_fields"], ["cached_tokens"])
        self.assertFalse(usage["unknown_fields_allowed"])
        reconciliation = usage["both_projection_reconciliation"]
        self.assertTrue(reconciliation["core_three_fields_must_match"])
        self.assertTrue(reconciliation["cached_must_match_when_reported_by_both"])
        self.assertTrue(reconciliation["cached_reported_by_one_projection_is_adopted"])
        self.assertTrue(reconciliation["cached_omitted_by_one_projection_is_not_a_conflict"])
        self.assertEqual(usage["cached_missing_internal_conservative_value"], 0)
        self.assertIsNone(usage["cached_missing_receipt_projection"])
        self.assertEqual(usage["cached_missing_budget_assumption"], "all_input_uncached")
        self.assertTrue(usage["cache_discount_requires_provider_reported_cached_tokens"])
        self.assertFalse(usage["cache_discount_may_relax_local_reservation"])

    def test_existing_transport_security_and_error_boundaries_remain_locked(self) -> None:
        snapshot = _load_contract()
        controls = snapshot["transport_controls"]  # type: ignore[index]
        self.assertEqual(
            controls,
            {
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
        )
        errors = snapshot["error_contract"]  # type: ignore[index]
        self.assertEqual(errors["stable_codes"], list(KIMI_CHAT_STABLE_ERROR_CODES))
        for field in (
            "raw_error_body_persisted",
            "raw_provider_message_persisted",
            "html_redirect_or_504_body_read",
            "retry_after_header_persisted",
            "retry_after_followed",
        ):
            self.assertFalse(errors[field])
        self.assertTrue(errors["timeout_or_network_outcome_unknown"])
        self.assertTrue(errors["missing_done_outcome_unknown"])
        self.assertTrue(
            errors["premature_done_without_terminal_outcome_unknown"]
        )
        self.assertTrue(errors["missing_terminal_usage_outcome_unknown"])
        self.assertTrue(errors["primary_error_preserved_over_close_error"])

    def test_response_validation_diagnostic_is_closed_and_noncausal(self) -> None:
        diagnostic = _load_contract()["response_validation_diagnostic_contract"]
        self.assertEqual(
            diagnostic["schema_version"],
            RESPONSE_VALIDATION_DIAGNOSTIC_SCHEMA_VERSION,
        )
        self.assertEqual(
            diagnostic["object_exact_fields"], ["schema_version", "code"]
        )
        self.assertEqual(
            diagnostic["required_only_for_error_code"],
            "kimi_chat_response_invalid",
        )
        self.assertTrue(diagnostic["must_be_null_for_other_errors"])
        self.assertEqual(
            diagnostic["codes"], sorted(RESPONSE_VALIDATION_DIAGNOSTIC_CODES)
        )
        self.assertEqual(
            diagnostic["source"], "fixed_local_validation_branch_only"
        )
        for field in (
            "v2_acceptance_or_precedence_changed",
            "causal_provider_fault_claim_allowed",
            "raw_header_body_or_identifier_persisted",
            "actual_field_name_value_offset_or_size_persisted",
            "free_text_exception_persisted",
        ):
            self.assertFalse(diagnostic[field])

    def test_contract_authorizes_nothing_and_records_zero_activity(self) -> None:
        snapshot = _load_contract()
        security = snapshot["security_and_receipt_boundary"]  # type: ignore[index]
        for field in (
            "api_key_persisted_logged_or_hashed",
            "authorization_header_persisted",
            "raw_request_id_persisted",
            "request_id_hash_persisted",
            "request_id_hash_exposed_on_success_response",
            "raw_completion_id_persisted",
            "completion_id_hash_persisted",
            "completion_id_hash_exposed_on_success_response",
            "raw_assistant_content_persisted",
            "raw_reasoning_content_persisted",
            "raw_tool_arguments_or_results_persisted",
            "model_quality_claim_allowed",
            "authorizes_provider_registration",
            "authorizes_public_regression",
            "authorizes_private_evaluation",
            "non_synthetic_release_supported",
        ):
            self.assertFalse(security[field])
        self.assertEqual(
            snapshot["audit_activity"],
            {
                "live_chat_requests_performed": 0,
                "model_token_calls": 0,
                "api_key_read_or_used": False,
                "usage": None,
                "cost": None,
            },
        )

    def test_semantic_mutations_break_exact_canonical_snapshot(self) -> None:
        snapshot = _load_contract()
        mutations = (
            (("predecessor", "contract_sha256"), "0" * 64),
            (("provider", "api_origin"), "https://proxy.invalid"),
            (("request_contract", "reasoning_effort"), "high"),
            (("tool_contract", "execution_before_terminal_usage_and_done_validation_allowed"), True),
            (("stream_contract", "terminal_usage_projection_allowlist"), ["top_level_only"]),
            (("stream_contract", "openai_empty_choices_usage_only_allowed"), True),
            (("stream_contract", "done_marker_exact_count"), 2),
            (("usage_contract", "optional_fields"), []),
            (("usage_contract", "cached_missing_receipt_projection"), 0),
            (("usage_contract", "cache_discount_may_relax_local_reservation"), True),
            (("transport_controls", "trust_environment"), True),
            (("transport_controls", "client_retries"), 1),
            (("response_validation_diagnostic_contract", "codes"), ["unknown"]),
            (("response_validation_diagnostic_contract", "causal_provider_fault_claim_allowed"), True),
            (("security_and_receipt_boundary", "authorizes_provider_registration"), True),
            (("audit_activity", "live_chat_requests_performed"), 1),
        )
        for path, replacement in mutations:
            with self.subTest(path=path):
                changed = copy.deepcopy(snapshot)
                target = changed
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = replacement
                self.assertNotEqual(changed, kimi_chat_completions_v3_contract())
                self.assertNotEqual(
                    hashlib.sha256(_canonical_bytes(changed)).hexdigest(),
                    _CANONICAL_SHA256,
                )


if __name__ == "__main__":
    unittest.main()
