from __future__ import annotations

import json
import unittest
from pathlib import Path

from researchops.kimi_chat_transport import (
    KIMI_CHAT_API_ORIGIN,
    KIMI_CHAT_MODEL_ID,
    KIMI_CHAT_PATH,
    KIMI_CHAT_PROVIDER_ID,
    KIMI_CHAT_STABLE_ERROR_CODES,
    KIMI_CHAT_TRANSPORT_ID,
    KIMI_INVALID_REQUEST_PROBE_BODY,
    KIMI_INVALID_REQUEST_PROBE_BODY_SHA256,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "evals" / "v2" / "kimi_chat_completions_contract.json"


class KimiChatCompletionsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_provider_and_request_identity_match_transport(self) -> None:
        provider = self.contract["provider"]
        request = self.contract["request_contract"]

        self.assertEqual(self.contract["schema_version"], "1.0")
        self.assertEqual(
            self.contract["contract_id"], "eval-v2-kimi-chat-completions-v1"
        )
        self.assertEqual(
            self.contract["status"], "implemented_offline_tested_not_run"
        )
        self.assertEqual(provider["provider_id"], KIMI_CHAT_PROVIDER_ID)
        self.assertEqual(provider["model_id"], KIMI_CHAT_MODEL_ID)
        self.assertEqual(provider["transport_id"], KIMI_CHAT_TRANSPORT_ID)
        self.assertEqual(provider["api_origin"], KIMI_CHAT_API_ORIGIN)
        self.assertEqual(provider["path"], KIMI_CHAT_PATH)
        self.assertFalse(provider["generic_provider_registry_enabled"])
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["max_canonical_body_bytes"], 6144)
        self.assertEqual(request["max_completion_tokens_max"], 1536)
        self.assertEqual(request["reasoning_effort"], "low")
        self.assertTrue(request["stream"])
        self.assertTrue(request["stream_options_include_usage"])
        probe = request["fixed_invalid_request_probe"]
        self.assertEqual(len(KIMI_INVALID_REQUEST_PROBE_BODY), 124)
        self.assertEqual(probe["canonical_body_bytes"], 124)
        self.assertEqual(
            probe["canonical_body_sha256"],
            KIMI_INVALID_REQUEST_PROBE_BODY_SHA256,
        )
        self.assertTrue(probe["required_messages_intentionally_omitted"])
        self.assertFalse(probe["prompt_present"])
        self.assertFalse(probe["tools_present"])
        self.assertEqual(probe["max_completion_tokens"], 1)
        self.assertEqual(probe["accepted_http_status"], 400)
        self.assertEqual(
            probe["accepted_provider_error_type"], "invalid_request_error"
        )

    def test_stream_usage_and_transport_are_fail_closed(self) -> None:
        stream = self.contract["stream_contract"]
        usage = self.contract["usage_contract"]
        transport = self.contract["transport_controls"]

        self.assertEqual(stream["event_decoded_bytes_max"], 65536)
        self.assertEqual(stream["response_decoded_bytes_max"], 524288)
        self.assertEqual(stream["done_marker_exact_count"], 1)
        self.assertEqual(stream["final_usage_chunk_exact_count"], 1)
        self.assertTrue(stream["length_finish_reason_is_failure"])
        self.assertTrue(usage["usage_missing_stops_run"])
        self.assertFalse(usage["cache_discount_may_relax_local_budget"])
        self.assertFalse(transport["trust_environment"])
        self.assertFalse(transport["follow_redirects"])
        self.assertEqual(transport["client_retries"], 0)
        self.assertFalse(transport["fallbacks_allowed"])
        self.assertFalse(transport["http_debug_logging_allowed"])

    def test_error_and_receipt_boundaries_match_runtime(self) -> None:
        errors = self.contract["error_contract"]
        security = self.contract["security_and_receipt_boundary"]
        audit = self.contract["audit_activity"]

        self.assertEqual(tuple(errors["stable_codes"]), KIMI_CHAT_STABLE_ERROR_CODES)
        self.assertFalse(errors["raw_error_body_persisted"])
        self.assertFalse(errors["raw_provider_message_persisted"])
        self.assertFalse(errors["html_504_body_read"])
        self.assertFalse(errors["retry_after_header_persisted"])
        self.assertFalse(errors["retry_after_followed"])
        for key in (
            "api_key_persisted_logged_or_hashed",
            "authorization_header_persisted",
            "raw_request_id_persisted",
            "raw_completion_id_persisted",
            "raw_assistant_content_persisted",
            "raw_reasoning_content_persisted",
            "raw_tool_arguments_or_results_persisted",
            "model_quality_claim_allowed",
            "authorizes_provider_registration",
            "authorizes_public_regression",
            "authorizes_private_evaluation",
            "non_synthetic_release_supported",
        ):
            self.assertFalse(security[key], key)
        self.assertEqual(audit["live_chat_requests_performed"], 0)
        self.assertEqual(audit["model_token_calls"], 0)
        self.assertFalse(audit["api_key_read_or_used"])
        self.assertIsNone(audit["usage"])
        self.assertIsNone(audit["cost"])


if __name__ == "__main__":
    unittest.main()
