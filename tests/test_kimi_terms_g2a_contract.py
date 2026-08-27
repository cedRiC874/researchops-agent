from __future__ import annotations

import copy
import json
import os
import re
import socket
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_PATH = _ROOT / "evals" / "v2" / "kimi_terms_g2a_provisional_contract.json"
_SNAKE_CASE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")

_TOP_LEVEL_KEYS = {
    "schema_version",
    "contract_id",
    "reviewed_on_utc",
    "decision",
    "source_evidence",
    "verified_deltas",
    "comparison_boundary",
    "common_risks",
    "announced_preview_observations",
    "provisional_scope",
    "pre_effective_expiry",
    "pre_effective_online_gate",
    "delta_gate",
    "g2a",
    "post_lock_receipt_boundary",
    "next_state",
    "audit_activity",
}

_PASS_CONDITIONS = (
    "old_cache_is_labeled_partial_and_non_authoritative",
    "announced_text_is_labeled_pre_effective_preview",
    "synthetic_only_and_private_denied_are_machine_enforced",
    "no_no_training_or_zero_retention_claim_is_made",
    "no_provider_call_is_part_of_g2a",
    "candidate_and_prior_results_are_not_inherited",
    "current_user_task_authorization_is_checked_separately",
    "fresh_first_party_source_attestation_is_bound_to_separate_user_authorization",
    "operator_source_recheck_is_completed_immediately_before_online_run",
)

_FAIL_CONDITIONS = (
    "effective_date_reached_without_final_source_capture_and_diff",
    "source_version_date_or_content_changes_before_expiry",
    "old_cache_or_preview_is_presented_as_final_effective_text",
    "non_synthetic_private_personal_or_secret_data_is_proposed",
    "no_training_zero_retention_or_complete_subprocessor_claim_is_asserted",
    "provider_call_chat_tool_or_pilot_is_treated_as_authorized",
    "post_lock_metadata_receipt_is_used_as_terms_compliance_quality_or_registration_evidence",
    "material_delta_is_unreviewed",
    "fresh_first_party_source_attestation_is_missing",
    "first_party_source_attestation_is_older_than_3600_seconds",
    "operator_source_recheck_is_not_completed_immediately_before_online_run",
    "source_update_is_observed_or_cannot_be_ruled_out",
)

_MATERIAL_CATEGORIES = (
    "legal_entity",
    "customer_content_use",
    "training_or_model_improvement",
    "retention_and_deletion",
    "data_location_and_cross_border_transfer",
    "subprocessors",
    "confidentiality_and_security",
    "incident_notification",
    "ownership_and_license",
    "availability_sla",
    "liability",
    "pricing_refund_and_termination",
    "regional_or_sanctions_eligibility",
)

_FINAL_SOURCES = (
    "final_first_party_service_agreement",
    "final_first_party_privacy_policy",
    "final_first_party_payment_agreement",
)

_CAPTURE_FIELDS = (
    "retrieved_at_utc",
    "displayed_updated_date",
    "displayed_effective_date",
    "raw_or_canonical_text_sha256",
    "canonicalization_version",
)

_OBSERVATION_IDS = (
    "privacy_financial_management_details",
    "payment_and_transaction_processor_sharing",
    "third_party_processing_and_login_sdk",
    "customer_business_data_instruction_language",
)

_PRE_EFFECTIVE_SOURCE_IDS = (
    "service_agreement",
    "privacy_policy",
    "payment_agreement",
)

_PRE_EFFECTIVE_ATTESTATION_FIELDS = (
    "retrieved_at_utc",
    "source_urls",
    "displayed_updated_dates",
    "displayed_effective_dates",
    "raw_or_canonical_text_sha256",
    "canonicalization_version",
    "material_delta_review_status",
    "authorization_binding_sha256",
)


class _ContractInvalid(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _ContractInvalid(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise _ContractInvalid(f"non-finite JSON constant: {value}")


def _load_contract() -> dict[str, object]:
    payload = json.loads(
        _CONTRACT_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
    )
    if not isinstance(payload, dict):
        raise _ContractInvalid("contract root must be an object")
    return payload


def _exact_keys(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise _ContractInvalid(f"{label} fields drifted")
    return value


def _require(value: bool, message: str) -> None:
    if not value:
        raise _ContractInvalid(message)


def _validate_contract(payload: dict[str, object]) -> None:
    _exact_keys(payload, _TOP_LEVEL_KEYS, "root")
    _require(payload["schema_version"] == "1.0", "schema version drift")
    _require(
        payload["contract_id"] == "moonshot-kimi-terms-g2a-provisional-v1",
        "contract id drift",
    )
    _require(payload["reviewed_on_utc"] == "2026-08-26", "review date drift")

    decision = _exact_keys(
        payload["decision"],
        {
            "gate",
            "status",
            "final_effective_terms_verified",
            "does_not_grant_user_task_authorization",
            "does_not_authorize_implementation",
            "does_not_authorize_provider_calls",
            "does_not_authorize_chat_or_pilot",
            "separate_pre_effective_user_authorization_may_enable_synthetic_pilot",
            "does_not_authorize_provider_registration",
            "does_not_authorize_private_or_non_synthetic",
        },
        "decision",
    )
    _require(decision["gate"] == "G2a", "wrong gate")
    _require(
        decision["status"] == "provisional_synthetic_only_pass",
        "decision may not become final",
    )
    _require(decision["final_effective_terms_verified"] is False, "preview is not final")
    for key in (
        "does_not_grant_user_task_authorization",
        "does_not_authorize_implementation",
        "does_not_authorize_provider_calls",
        "does_not_authorize_chat_or_pilot",
        "does_not_authorize_provider_registration",
        "does_not_authorize_private_or_non_synthetic",
    ):
        _require(decision[key] is True, f"{key} must remain true")
    _require(
        decision[
            "separate_pre_effective_user_authorization_may_enable_synthetic_pilot"
        ]
        is True,
        "separate pre-effective authority must remain explicit",
    )

    sources = _exact_keys(
        payload["source_evidence"],
        {"old_effective_cache", "announced_preview"},
        "source_evidence",
    )
    old = _exact_keys(
        sources["old_effective_cache"],
        {
            "displayed_effective_date",
            "displayed_updated_date",
            "named_legal_entity",
            "source_kind",
            "capture_completeness",
            "service_agreement_url",
            "privacy_policy_url",
            "payment_agreement_url",
            "full_text_sha256",
            "authoritative_full_text_available",
            "evidence_strength",
            "may_be_treated_as_final_full_text",
        },
        "old_effective_cache",
    )
    _require(old["displayed_effective_date"] == "2025-04-28", "old date drift")
    _require(old["displayed_updated_date"] == "2025-04-28", "old update date drift")
    _require(old["named_legal_entity"] == "北京月之暗面科技有限公司", "old entity drift")
    _require(old["source_kind"] == "secondary_search_cache_partial", "old source overclaim")
    _require(old["capture_completeness"] == "partial_relevant_excerpts_only", "old completeness overclaim")
    _require(old["full_text_sha256"] is None, "partial cache cannot have invented full hash")
    _require(old["authoritative_full_text_available"] is False, "old cache is not authoritative")
    _require(old["evidence_strength"] == "limited", "old evidence strength drift")
    _require(old["may_be_treated_as_final_full_text"] is False, "old cache cannot be final")
    _require(
        old["service_agreement_url"]
        == "https://platform.kimi.com/docs/agreement/modeluse",
        "old service URL drift",
    )
    _require(
        old["privacy_policy_url"]
        == "https://platform.kimi.com/docs/agreement/userprivacy",
        "old privacy URL drift",
    )
    _require(
        old["payment_agreement_url"]
        == "https://platform.kimi.com/docs/agreement/payment",
        "old payment URL drift",
    )

    preview = _exact_keys(
        sources["announced_preview"],
        {
            "displayed_updated_date",
            "announced_effective_date",
            "named_legal_entity",
            "source_kind",
            "capture_completeness",
            "service_agreement_url",
            "privacy_policy_url",
            "payment_agreement_url",
            "full_text_sha256",
            "is_final_effective_text",
            "evidence_strength",
            "may_be_treated_as_current_effective_text",
        },
        "announced_preview",
    )
    _require(preview["displayed_updated_date"] == "2026-08-24", "preview update drift")
    _require(preview["announced_effective_date"] == "2026-08-31", "preview effective date drift")
    _require(preview["named_legal_entity"] == "北京月之暗面科技股份有限公司", "preview entity drift")
    _require(preview["source_kind"] == "first_party_pre_effective_preview", "preview source drift")
    _require(
        preview["capture_completeness"]
        == "reviewed_relevant_sections_not_versioned_archive",
        "preview completeness overclaim",
    )
    _require(preview["full_text_sha256"] is None, "unarchived preview cannot have invented full hash")
    _require(preview["is_final_effective_text"] is False, "preview cannot be final")
    _require(preview["evidence_strength"] == "moderate", "preview evidence strength drift")
    _require(preview["may_be_treated_as_current_effective_text"] is False, "preview cannot be current terms")
    _require(
        preview["service_agreement_url"]
        == "https://platform.kimi.com/docs/agreement/modeluse",
        "preview service URL drift",
    )
    _require(
        preview["privacy_policy_url"]
        == "https://platform.kimi.com/docs/agreement/userprivacy",
        "preview privacy URL drift",
    )
    _require(
        preview["payment_agreement_url"]
        == "https://platform.kimi.com/docs/agreement/payment",
        "preview payment URL drift",
    )

    deltas = payload["verified_deltas"]
    _require(isinstance(deltas, list) and len(deltas) == 2, "only two deltas are verified")
    _require(
        [delta.get("field") for delta in deltas if isinstance(delta, dict)]
        == ["version_dates", "named_legal_entity_form"],
        "verified delta scope drift",
    )
    for delta in deltas:
        exact = _exact_keys(
            delta,
            {
                "field",
                "old_value",
                "announced_value",
                "evidence_strength",
                "requires_final_reverification",
            },
            "verified_delta",
        )
        _require(exact["requires_final_reverification"] is True, "delta must be reverified")
    _require(
        deltas[0]
        == {
            "field": "version_dates",
            "old_value": "effective_and_updated_2025_04_28",
            "announced_value": "updated_2026_08_24_effective_2026_08_31",
            "evidence_strength": "high",
            "requires_final_reverification": True,
        },
        "version-date delta drift",
    )
    _require(
        deltas[1]
        == {
            "field": "named_legal_entity_form",
            "old_value": "北京月之暗面科技有限公司",
            "announced_value": "北京月之暗面科技股份有限公司",
            "evidence_strength": "moderate",
            "requires_final_reverification": True,
        },
        "entity delta drift",
    )

    comparison = _exact_keys(
        payload["comparison_boundary"],
        {
            "complete_clause_level_diff_performed",
            "old_full_text_baseline_available",
            "absence_of_observed_delta_means_no_delta",
            "old_cache_may_be_used_as_final_text",
            "announced_preview_may_be_called_effective",
            "unobserved_or_uncomparable_clauses_status",
        },
        "comparison_boundary",
    )
    for key in (
        "complete_clause_level_diff_performed",
        "old_full_text_baseline_available",
        "absence_of_observed_delta_means_no_delta",
        "old_cache_may_be_used_as_final_text",
        "announced_preview_may_be_called_effective",
    ):
        _require(comparison[key] is False, f"{key} must remain false")
    _require(comparison["unobserved_or_uncomparable_clauses_status"] == "unknown", "unknowns were overclaimed")

    risks = _exact_keys(
        payload["common_risks"],
        {
            "input_output_feedback_model_optimization_use",
            "dialogue_content_model_optimization_use",
            "public_no_training_commitment_verified",
            "public_training_opt_out_verified",
            "zero_data_retention_verified",
            "personal_information_storage_location",
            "request_retention_days",
            "response_retention_days",
            "security_log_retention_days",
            "backup_retention_days",
            "deletion_sla_days",
            "complete_api_subprocessor_list_verified",
            "public_dpa_verified",
        },
        "common_risks",
    )
    for key in (
        "input_output_feedback_model_optimization_use",
        "dialogue_content_model_optimization_use",
    ):
        risk = _exact_keys(
            risks[key],
            {"value", "evidence_strength", "old_support", "announced_support"},
            key,
        )
        _require(risk["value"] is True, f"{key} risk lost")
        _require(risk["evidence_strength"] == "moderate", f"{key} strength drift")
        _require(risk["old_support"] == "observed_in_partial_cache", f"{key} old support drift")
        _require(
            risk["announced_support"] == "observed_in_first_party_preview",
            f"{key} preview support drift",
        )
    for key in set(risks) - {
        "input_output_feedback_model_optimization_use",
        "dialogue_content_model_optimization_use",
    }:
        _exact_keys(risks[key], {"value", "evidence_strength"}, key)
    for key in (
        "public_no_training_commitment_verified",
        "public_training_opt_out_verified",
        "zero_data_retention_verified",
        "complete_api_subprocessor_list_verified",
        "public_dpa_verified",
    ):
        _require(risks[key]["value"] is False, f"{key} overclaim")
    for key in (
        "request_retention_days",
        "response_retention_days",
        "security_log_retention_days",
        "backup_retention_days",
        "deletion_sla_days",
    ):
        _require(risks[key]["value"] is None, f"{key} must remain unknown")
        _require(risks[key]["evidence_strength"] == "limited", f"{key} strength drift")
    _require(
        risks["personal_information_storage_location"]
        == {"value": "peoples_republic_of_china", "evidence_strength": "moderate"},
        "storage location drift",
    )

    observations = payload["announced_preview_observations"]
    _require(isinstance(observations, list) and len(observations) == 4, "preview observations drift")
    _require(
        tuple(
            observation.get("observation_id")
            for observation in observations
            if isinstance(observation, dict)
        )
        == _OBSERVATION_IDS,
        "preview observation ids drift",
    )
    for observation in observations:
        exact = _exact_keys(
            observation,
            {
                "observation_id",
                "classification",
                "summary",
                "evidence_strength",
                "old_comparison_status",
                "may_be_called_new_or_expanded",
            },
            "preview_observation",
        )
        _require(
            exact["classification"] == "announced_preview_observation_not_verified_delta",
            "preview observation became a verified delta",
        )
        _require(exact["evidence_strength"] == "limited", "preview expansion strength overclaim")
        _require(exact["old_comparison_status"] == "unknown_due_partial_cache", "old comparison overclaim")
        _require(exact["may_be_called_new_or_expanded"] is False, "preview may not be called an expansion")

    scope = _exact_keys(
        payload["provisional_scope"],
        {
            "data_class",
            "personal_information_allowed",
            "business_secrets_allowed",
            "private_holdout_allowed",
            "non_synthetic_allowed",
            "repo_local_holdout_allowed",
            "previously_run_public_cases_allowed",
            "previously_run_supervised_cases_allowed",
            "file_upload_allowed",
            "hosted_search_or_memory_allowed",
            "result_may_be_used_for_prompt_tuning",
        },
        "provisional_scope",
    )
    _require(scope["data_class"] == "fresh_synthetic_non_sensitive_only", "scope widened")
    for key, value in scope.items():
        if key != "data_class":
            _require(value is False, f"{key} must remain false")

    expiry = _exact_keys(
        payload["pre_effective_expiry"],
        {
            "effective_date_timezone_assumption",
            "expires_at_utc",
            "validity_rule",
            "state_before_expiry",
            "state_at_or_after_expiry",
            "expires_on_any_source_update",
            "automatic_extension_allowed",
        },
        "pre_effective_expiry",
    )
    _require(expiry["expires_at_utc"] == "2026-08-30T16:00:00Z", "expiry drift")
    _require(
        expiry["validity_rule"]
        == "current_time_utc_must_be_before_expires_at_utc_and_sources_unchanged",
        "validity rule drift",
    )
    _require(
        expiry["state_before_expiry"] == "provisional_synthetic_only_pass",
        "pre-expiry state drift",
    )
    _require(
        expiry["state_at_or_after_expiry"]
        == "blocked_pending_final_effective_terms_review",
        "expiry must fail closed",
    )
    _require(expiry["expires_on_any_source_update"] is True, "source update must expire G2a")
    _require(expiry["automatic_extension_allowed"] is False, "G2a cannot auto-extend")

    online_gate = _exact_keys(
        payload["pre_effective_online_gate"],
        {
            "fresh_first_party_source_attestation_required",
            "required_source_ids",
            "required_attestation_fields",
            "attestation_max_age_seconds",
            "source_update_detection_implemented",
            "operator_source_recheck_required_immediately_before_online_run",
            "attestation_must_be_bound_to_separate_user_authorization",
            "attestation_must_confirm_no_material_or_unclassifiable_delta",
            "separate_user_authorization_must_include_locked_caps_and_budget",
            "missing_stale_or_unreviewed_attestation_action",
            "valid_only_before_expires_at_utc",
            "g2b_required_at_or_after_expiry",
        },
        "pre_effective_online_gate",
    )
    _require(
        online_gate["fresh_first_party_source_attestation_required"] is True,
        "fresh source attestation required",
    )
    _require(
        tuple(online_gate["required_source_ids"]) == _PRE_EFFECTIVE_SOURCE_IDS,
        "pre-effective source ids drift",
    )
    _require(
        tuple(online_gate["required_attestation_fields"])
        == _PRE_EFFECTIVE_ATTESTATION_FIELDS,
        "pre-effective attestation fields drift",
    )
    _require(
        online_gate["attestation_max_age_seconds"] == 3600,
        "source attestation must expire after one hour",
    )
    _require(
        online_gate["source_update_detection_implemented"] is False,
        "automatic source-update detection is not implemented",
    )
    for key in (
        "operator_source_recheck_required_immediately_before_online_run",
        "attestation_must_be_bound_to_separate_user_authorization",
        "attestation_must_confirm_no_material_or_unclassifiable_delta",
        "separate_user_authorization_must_include_locked_caps_and_budget",
        "valid_only_before_expires_at_utc",
        "g2b_required_at_or_after_expiry",
    ):
        _require(online_gate[key] is True, f"{key} must remain true")
    _require(
        online_gate["missing_stale_or_unreviewed_attestation_action"]
        == "blocked_manual_re_review",
        "missing or stale attestation must block",
    )

    delta_gate = _exact_keys(
        payload["delta_gate"],
        {
            "required_final_sources",
            "required_capture_fields",
            "material_categories",
            "preview_hash_or_date_mismatch_action",
            "material_or_unclassifiable_delta_action",
            "no_material_delta_auto_authorizes_online_use",
            "manual_signoff_required",
        },
        "delta_gate",
    )
    _require(tuple(delta_gate["required_final_sources"]) == _FINAL_SOURCES, "final source drift")
    _require(tuple(delta_gate["required_capture_fields"]) == _CAPTURE_FIELDS, "capture field drift")
    _require(tuple(delta_gate["material_categories"]) == _MATERIAL_CATEGORIES, "material category drift")
    _require(delta_gate["preview_hash_or_date_mismatch_action"] == "blocked_manual_re_review", "preview mismatch must block")
    _require(delta_gate["material_or_unclassifiable_delta_action"] == "blocked_manual_re_review", "material delta must block")
    _require(delta_gate["no_material_delta_auto_authorizes_online_use"] is False, "no delta cannot authorize online")
    _require(delta_gate["manual_signoff_required"] is True, "manual signoff required")

    g2a = _exact_keys(
        payload["g2a"],
        {
            "pass_conditions",
            "fail_conditions",
            "pass_effect",
            "pass_automatically_authorizes_implementation",
            "pass_automatically_authorizes_online_use",
            "failure_state",
        },
        "g2a",
    )
    _require(tuple(g2a["pass_conditions"]) == _PASS_CONDITIONS, "pass condition drift")
    _require(tuple(g2a["fail_conditions"]) == _FAIL_CONDITIONS, "fail condition drift")
    for condition in (*g2a["pass_conditions"], *g2a["fail_conditions"]):
        _require(isinstance(condition, str) and _SNAKE_CASE.fullmatch(condition) is not None, "condition is not snake_case")
    _require(
        g2a["pass_effect"]
        == "eligible_for_separately_authorized_pre_effective_synthetic_pilot_or_offline_design",
        "pass scope drift",
    )
    _require(g2a["pass_automatically_authorizes_implementation"] is False, "G2a cannot authorize implementation")
    _require(g2a["pass_automatically_authorizes_online_use"] is False, "G2a cannot authorize online use")
    _require(g2a["failure_state"] == "blocked_manual_re_review", "failure state drift")

    receipt = _exact_keys(
        payload["post_lock_receipt_boundary"],
        {"may_support", "may_not_support"},
        "post_lock_receipt_boundary",
    )
    _require(
        tuple(receipt["may_support"])
        == ("authentication_at_one_point_in_time", "exact_model_visibility_at_one_point_in_time"),
        "receipt support scope widened",
    )
    _require(
        set(receipt["may_not_support"])
        == {
            "terms_compliance",
            "chat_or_tool_compatibility",
            "usage_or_cost_semantics",
            "model_quality",
            "provider_registration",
            "non_synthetic_or_private_use",
        },
        "receipt exclusions drift",
    )

    next_state = _exact_keys(
        payload["next_state"],
        {
            "name",
            "earliest_date",
            "requires_manual_signoff",
            "requires_separate_user_task_authorization",
            "requires_separate_key_budget_caps_and_one_time_authorization_for_online_use",
        },
        "next_state",
    )
    _require(next_state["name"] == "G2b_final_effective_terms_review", "next gate drift")
    _require(next_state["earliest_date"] == "2026-08-31", "G2b date drift")
    for key in (
        "requires_manual_signoff",
        "requires_separate_user_task_authorization",
        "requires_separate_key_budget_caps_and_one_time_authorization_for_online_use",
    ):
        _require(next_state[key] is True, f"{key} must remain true")

    audit = _exact_keys(
        payload["audit_activity"],
        {
            "public_document_review_performed",
            "provider_api_key_read",
            "provider_network_calls",
            "model_token_calls",
            "cost",
        },
        "audit_activity",
    )
    _require(audit["public_document_review_performed"] is True, "document review missing")
    _require(audit["provider_api_key_read"] is False, "G2a must not read a key")
    _require(audit["provider_network_calls"] == 0, "G2a must not call Provider")
    _require(audit["model_token_calls"] == 0, "G2a must not call a model")
    _require(audit["cost"] is None, "unknown cost must remain null")


class KimiTermsG2aContractTests(unittest.TestCase):
    def test_repository_contract_is_strict_and_provisional(self) -> None:
        payload = _load_contract()
        _validate_contract(payload)

    def test_optimistic_or_authorizing_mutations_are_rejected(self) -> None:
        mutations = (
            ("decision", "status", "final_pass"),
            ("decision", "final_effective_terms_verified", True),
            ("decision", "does_not_authorize_provider_calls", False),
            ("decision", "does_not_authorize_implementation", False),
            (
                "decision",
                "separate_pre_effective_user_authorization_may_enable_synthetic_pilot",
                False,
            ),
            ("provisional_scope", "private_holdout_allowed", True),
            ("provisional_scope", "previously_run_public_cases_allowed", True),
            ("pre_effective_expiry", "expires_at_utc", "2026-09-01T00:00:00Z"),
            ("pre_effective_expiry", "automatic_extension_allowed", True),
            ("pre_effective_online_gate", "attestation_max_age_seconds", 3601),
            (
                "pre_effective_online_gate",
                "source_update_detection_implemented",
                True,
            ),
            (
                "pre_effective_online_gate",
                "operator_source_recheck_required_immediately_before_online_run",
                False,
            ),
            (
                "pre_effective_online_gate",
                "attestation_must_be_bound_to_separate_user_authorization",
                False,
            ),
            ("comparison_boundary", "complete_clause_level_diff_performed", True),
            ("comparison_boundary", "absence_of_observed_delta_means_no_delta", True),
            ("audit_activity", "provider_api_key_read", True),
            ("audit_activity", "provider_network_calls", 1),
        )
        source = _load_contract()
        for section, field, value in mutations:
            with self.subTest(section=section, field=field):
                payload = copy.deepcopy(source)
                payload[section][field] = value
                with self.assertRaises(_ContractInvalid):
                    _validate_contract(payload)

    def test_expiry_boundary_is_automatic_and_fail_closed(self) -> None:
        payload = _load_contract()
        _validate_contract(payload)
        expiry = datetime.fromisoformat(
            payload["pre_effective_expiry"]["expires_at_utc"].replace("Z", "+00:00")
        )

        def state_at(now: datetime) -> str:
            self.assertEqual(now.tzinfo, timezone.utc)
            if now >= expiry:
                return payload["pre_effective_expiry"]["state_at_or_after_expiry"]
            return payload["pre_effective_expiry"]["state_before_expiry"]

        self.assertEqual(
            state_at(datetime(2026, 8, 30, 15, 59, 59, tzinfo=timezone.utc)),
            "provisional_synthetic_only_pass",
        )
        self.assertEqual(
            state_at(datetime(2026, 8, 30, 16, 0, 0, tzinfo=timezone.utc)),
            "blocked_pending_final_effective_terms_review",
        )

    def test_preview_observations_cannot_be_promoted_to_verified_deltas(self) -> None:
        payload = _load_contract()
        payload["announced_preview_observations"][0]["classification"] = "verified_delta"
        with self.assertRaises(_ContractInvalid):
            _validate_contract(payload)

    def test_unknown_fields_are_rejected_at_root_and_nested_boundaries(self) -> None:
        payload = _load_contract()
        payload["unexpected"] = True
        with self.assertRaises(_ContractInvalid):
            _validate_contract(payload)

        payload = _load_contract()
        payload["common_risks"]["request_retention_days"]["invented"] = 0
        with self.assertRaises(_ContractInvalid):
            _validate_contract(payload)

        payload = _load_contract()
        payload["announced_preview_observations"][0]["may_be_called_new_or_expanded"] = True
        with self.assertRaises(_ContractInvalid):
            _validate_contract(payload)

    def test_conditions_are_strict_snake_case(self) -> None:
        payload = _load_contract()
        payload["g2a"]["fail_conditions"][5] = "provider call is authorized"
        with self.assertRaises(_ContractInvalid):
            _validate_contract(payload)

    def test_loading_and_validation_do_not_read_key_or_connect(self) -> None:
        class ForbiddenEnvironment(dict[str, str]):
            def get(self, key, default=None):
                if key == "MOONSHOT_API_KEY":
                    raise AssertionError("G2a contract validation must not read a Provider Key")
                return super().get(key, default)

            def __getitem__(self, key):
                if key == "MOONSHOT_API_KEY":
                    raise AssertionError("G2a contract validation must not read a Provider Key")
                return super().__getitem__(key)

        with (
            patch.object(os, "environ", ForbiddenEnvironment()),
            patch.object(
                socket,
                "create_connection",
                side_effect=AssertionError("G2a contract validation must not connect"),
            ),
        ):
            payload = _load_contract()
            _validate_contract(payload)

        self.assertFalse(payload["audit_activity"]["provider_api_key_read"])
        self.assertEqual(payload["audit_activity"]["provider_network_calls"], 0)
        self.assertEqual(payload["audit_activity"]["model_token_calls"], 0)
        self.assertIsNone(payload["audit_activity"]["cost"])

    def test_contract_contains_no_secret_or_receipt_identifier(self) -> None:
        raw = _CONTRACT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("offline-moonshot-key", raw)
        self.assertNotIn("request_id_sha256", raw)
        self.assertNotIn("92a112fdef34bf1bc35aaf5073752bc379a753b3ee0b16c14223080f1332229c", raw)


if __name__ == "__main__":
    unittest.main()
