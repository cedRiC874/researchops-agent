from __future__ import annotations

import copy
import hashlib
import json
import re
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "evals/provider_completion_external_preregistration_v1"
CONTRACT_PATH = PACKAGE / "external_preregistration_design_contract_v1.json"
SCHEMA_ROOT = PACKAGE / "schemas"


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate key: {key}")
        value[key] = item
    return value


def _load(path: Path):
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_object,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"nonfinite value: {value}")
        ),
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _signature(role: str, key_id: str) -> dict:
    return {
        "role": role,
        "key_id": key_id,
        "algorithm": "ed25519",
        "signed_sha256": "a" * 64,
        "signature_b64": "A" * 86 + "==",
    }


def _representative_documents() -> dict[str, dict]:
    h = "a" * 64
    h2 = "b" * 64
    h3 = "c" * 64
    zero = "0" * 64
    commit = "a" * 40
    tree = "b" * 40
    freeze_key = "PCEKEY-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    custodian_key = "PCEKEY-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    witness_key = "PCEKEY-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    candidate = {
        "schema_version": "provider-completion-candidate-freeze-receipt/1.0",
        "document_type": "completion_telemetry_candidate_freeze",
        "receipt_id": "PCEFREEZE-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "frozen_at_utc": "2026-09-04T00:00:00Z",
        "repository": "cedRiC874/researchops-agent",
        "candidate_commit": commit,
        "candidate_tree": tree,
        "source_integrity_commitment_sha256": h,
        "runtime_registry_commitment_sha256": h2,
        "implementation_commitment_sha256": h3,
        "runner_config_commitment_sha256": h,
        "closure_evidence_contract_commitment_sha256": "a96b39a1f4bafa3bcf512fd88a5e7b0ca3987ca2bc9804642a1d1259cbf8cad0",
        "dependency_lock_sha256": h3,
        "prompt_or_task_adjustment_after_freeze_allowed": False,
        "online_execution_authorized": False,
        "document_sha256": h,
        "signatures": [_signature("freeze_authority", freeze_key)],
    }
    trust = {
        "schema_version": "provider-completion-external-trust-manifest/1.0",
        "document_type": "completion_telemetry_external_trust_manifest",
        "manifest_id": "PCETRUST-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "created_at_utc": "2026-09-03T23:00:00Z",
        "valid_from_utc": "2026-09-03T00:00:00Z",
        "expires_at_utc": "2026-09-05T00:00:00Z",
        "roles": [
            {
                "role": role,
                "key_id": key_id,
                "algorithm": "ed25519",
                "public_key_b64": letter * 43 + "=",
                "organization_commitment_sha256": digest,
                "not_before_utc": "2026-09-03T00:00:00Z",
                "expires_at_utc": "2026-09-05T00:00:00Z",
                "revoked": False,
            }
            for role, key_id, letter, digest in (
                ("freeze_authority", freeze_key, "A", h),
                ("task_custodian", custodian_key, "B", h2),
                ("ledger_witness", witness_key, "C", h3),
            )
        ],
        "expected_manifest_hash_must_be_supplied_out_of_band": True,
        "document_sha256": h,
        "signatures": [
            _signature("freeze_authority", freeze_key),
            _signature("task_custodian", custodian_key),
            _signature("ledger_witness", witness_key),
        ],
    }
    seen_classes = [
        "candidate_repository_evals_and_task_manifests",
        "candidate_repository_tests_and_synthetic_fixtures",
        "candidate_repository_docs_and_published_evidence_examples",
        "historical_provider_request_inputs_represented_by_project_supplied_digests",
        "manual_development_review_and_prompt_tuning_inputs_represented_by_project_supplied_digests",
        "public_article_and_demo_task_inputs_represented_by_project_supplied_digests",
    ]
    seen = {
        "schema_version": "provider-completion-seen-case-exclusion-manifest/1.0",
        "document_type": "completion_telemetry_seen_task_exclusion_manifest",
        "manifest_id": "PCESEEN-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "created_at_utc": "2026-09-04T00:30:00Z",
        "candidate_freeze_receipt_sha256": h,
        "source_inventory": [
            {
                "source_class": source_class,
                "source_snapshot_commitment_sha256": h2,
                "task_digest_count": 1,
            }
            for source_class in seen_classes
        ],
        "source_inventory_commitment_sha256": h2,
        "exclusion_digest_set_count": 6,
        "exclusion_digest_set_commitment_sha256": h3,
        "source_inventory_complete_attested": True,
        "software_proves_source_inventory_complete": False,
        "task_or_prompt_plaintext_included": False,
        "individual_task_digests_included": False,
        "document_sha256": h,
        "signatures": [
            _signature("freeze_authority", freeze_key),
            _signature("task_custodian", custodian_key),
        ],
    }
    envelope = {
        "schema_version": "provider-completion-external-preregistration/1.0",
        "document_type": "completion_telemetry_closure_preregistration",
        "envelope_id": "PCEPR-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "status": "frozen_not_authorized_not_run",
        "candidate_frozen_at_utc": "2026-09-04T00:00:00Z",
        "task_bundle_committed_at_utc": "2026-09-04T01:00:00Z",
        "envelope_frozen_at_utc": "2026-09-04T02:00:00Z",
        "valid_until_utc": "2026-09-05T00:00:00Z",
        "purpose": {
            "synthetic_only": True,
            "telemetry_closure_only": True,
            "agent_runner_required": True,
            "is_evaluation": False,
            "produces_task_pass": False,
            "prompt_or_task_adjustment_after_freeze_allowed": False,
        },
        "execution_binding": {
            "repository": "cedRiC874/researchops-agent",
            "candidate_freeze_receipt_sha256": h,
            "candidate_freeze_ledger_entry_sha256": h2,
            "execution_commit": commit,
            "execution_tree": tree,
            "source_integrity_commitment_sha256": h,
            "first_live_evidence_commitment_sha256": h2,
            "first_live_validation_status": "reviewed_success",
            "runtime_registry_commitment_sha256": h3,
            "runtime_registry_binding_allowed": True,
            "implementation_commitment_sha256": h,
            "runner_config_commitment_sha256": h2,
            "telemetry_schema_sha256": h3,
            "mapping_sha256": h,
            "sanitizer_sha256": h2,
            "closure_verifier_sha256": h3,
            "closure_evidence_contract_commitment_sha256": "a96b39a1f4bafa3bcf512fd88a5e7b0ca3987ca2bc9804642a1d1259cbf8cad0",
            "dependency_lock_sha256": h2,
            "provider_id": "deepseek",
            "model_id": "deepseek-v4-flash",
            "api_origin": "https://api.deepseek.com",
            "api_surface": "responses",
            "transport_id": "openai_compatible_responses",
            "adapter_version": "deepseek-responses-adapter/1.0",
        },
        "task_custody": {
            "task_bundle_commitment_sha256": h,
            "task_bundle_commitment_algorithm": "secret-salted-canonical-bundle-sha256-v1",
            "task_bundle_schema_version": "completion-telemetry-unseen-case-bundle/1.0",
            "task_bundle_canonicalization": "utf8-sorted-keys-no-whitespace-reject-duplicate-and-nonfinite-v1",
            "minimum_secret_salt_bytes": 32,
            "planned_case_count": 2,
            "case_handle_set_commitment_sha256": h2,
            "case_order_commitment_sha256": h3,
            "execution_input_order_commitment_sha256": h,
            "opaque_case_handles_only": True,
            "seen_task_exclusion_manifest_schema_version": "provider-completion-seen-case-exclusion-manifest/1.0",
            "seen_task_exclusion_manifest_sha256": h,
            "seen_task_exclusion_set_commitment_sha256": h2,
            "seen_task_exclusion_set_count": 6,
            "seen_source_inventory_commitment_sha256": h3,
            "seen_task_exclusion_set_complete_attested": True,
            "overlap_check_algorithm": "canonical-case-digest-set-intersection-v1",
            "exact_overlap_count": 0,
            "salt_length_is_custodian_attestation": True,
            "overlap_result_is_custodian_attestation": True,
            "unseen_to_candidate_developers_attested": True,
            "semantic_overlap_excluded_by_software": False,
            "plaintext_disclosed_before_consumption": False,
            "task_plaintext_in_envelope": False,
            "goldens_or_scorer_present": False,
            "participant_derived_data_present": False,
            "private_or_non_synthetic_data_present": False,
            "direct_or_quasi_identifier_data_present": False,
        },
        "runtime_plan": {
            "schema_version": "provider-completion-external-runtime-plan/1.0",
            "external_plan_binding_sha256": h2,
            "denominator_plan_commitment_sha256": h,
            "denominator_plan": {
                "schema_version": "provider-completion-runtime-denominator-plan/1.0",
                "provider_id": "deepseek",
                "api_surface": "responses",
                "transport_id": "openai_compatible_responses",
                "adapter_version": "deepseek-responses-adapter/1.0",
                "telemetry_schema_sha256": h,
                "mapping_schema_version": "provider-completion-mapping/2.0",
                "mapping_version": "deepseek-responses-probe-v3-successor-v1",
                "mapping_sha256": h2,
                "case_ids": [
                    "PCECASE-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "PCECASE-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                ],
                "case_ids_sha256": h3,
                "max_turns_per_case": 2,
                "total_model_request_cap": 4,
                "agents_sdk_retries": 0,
                "http_client_retries": 0,
                "denominator_algorithm": "transport-response-finalization-v1",
                "exact_response_count_preregistered": False,
            },
            "telemetry_interpretation": {
                "output_counter_comparability": "comparable",
                "output_counter_path": "output_tokens",
            },
            "campaign_topology": {
                "campaign_id": "PCECAMP-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                "case_handle_set_commitment_sha256": h2,
                "case_handle_order_commitment_sha256": h3,
                "audit_run_id_derivation": "sha256-domain-separated-envelope-id-and-case-handle-v1",
                "audit_run_set_commitment_sha256": h,
                "one_audit_run_per_case_handle": True,
            },
            "transport_limits": {
                "network_attempt_cap": 4,
                "concurrency": 1,
                "resume": False,
                "fallback": False,
                "tools": 0,
                "request_timeout_seconds": 120,
                "total_timeout_seconds": 600,
            },
            "budget_policy": {
                "budget_policy_commitment_sha256": h,
                "pricing_snapshot_date": "2026-09-04",
                "pricing_source_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                "pricing_source_snapshot_sha256": h2,
                "pricing_retrieved_at_utc": "2026-09-04T02:45:00Z",
                "official_pricing_attestation_current": True,
                "currency": "CNY",
                "token_price_unit": "per_million_tokens",
                "input_price_per_million_cny": "3.000000",
                "output_price_per_million_cny": "9.000000",
                "cache_discount_assumed": False,
                "input_token_limit_total": 4096,
                "output_token_limit_per_request": 512,
                "output_token_limit_total": 2048,
                "local_observed_cost_stop_cny": "1.000000",
                "request_count_and_output_caps_enforced_pre_send": True,
                "input_token_limit_enforcement": "post_response_observed_stop",
                "cost_stop_enforcement": "post_response_observed_stop",
                "single_in_flight_request_may_overshoot_observed_limits": True,
                "provider_invoice_hard_cap": False,
                "provider_side_retention_verified": False,
            },
        },
        "closure_evidence": {
            "closure_evidence_contract_commitment_sha256": "a96b39a1f4bafa3bcf512fd88a5e7b0ca3987ca2bc9804642a1d1259cbf8cad0",
            "bundle_manifest_schema_version": "provider-completion-closure-bundle-manifest/1.0",
            "outer_telemetry_schema_version": "phase6-completion-telemetry/1.0",
            "runtime_denominator_schema_version": "provider-completion-runtime-denominator-artifact/1.0",
            "audit_index_schema_version": "1.1",
            "ledger_event_schema_version": "provider-completion-ledger-event/1.1",
            "bridge_commitment_schema_version": "provider-completion-ledger-bridge-commitment/1.0",
            "audit_database_schema_commitment_sha256": "c0df1f54a96fe3c4f5ee196e654203a62e5e775a3e21fd5f47ace9e349169385",
            "closure_algorithm": "provider-completion-full-outer-closure-v1",
            "reason_ordering": "fixed-contract-order-deduplicated-v1",
            "exact_bundle_file_set": [
                "completion_closure_manifest.json",
                "phase6_audit.sqlite3",
                "phase6_audit_index.json",
                "phase6_completion_telemetry.json",
            ],
            "post_run_closure_receipt_schema_version": "provider-completion-closure-receipt/1.0",
            "post_run_closure_receipt_hash_domain": "researchops-provider-completion-closure-receipt-v1",
            "post_run_ledger_entry_required": True,
            "expected_closure_receipt_hash_must_be_supplied_out_of_band": True,
            "expected_final_ledger_sequence_and_head_must_be_supplied_out_of_band": True,
            "runtime_authority_granted_by_verifier": False,
        },
        "external_trust": {
            "trust_manifest_sha256": h,
            "freeze_authority_key_id": freeze_key,
            "task_custodian_key_id": custodian_key,
            "ledger_witness_key_id": witness_key,
            "freeze_authority_organization_commitment_sha256": h,
            "task_custodian_organization_commitment_sha256": h2,
            "ledger_witness_organization_commitment_sha256": h3,
            "ledger_id": "PCELEDGER-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "ledger_base_sequence": 1,
            "ledger_base_head_sha256": h,
            "ledger_base_zero_state_consistent": True,
            "ledger_entry_schema_version": "provider-completion-external-ledger-entry/1.0",
            "ledger_entry_hash_domain": "researchops-provider-completion-external-ledger-entry-v1",
            "ledger_head_hash_domain": "researchops-provider-completion-external-ledger-head-v1",
            "expected_envelope_hash_must_be_supplied_out_of_band": True,
            "future_preregistration_freeze_entry_required": True,
            "future_authorization_must_bind_freeze_entry": True,
            "future_closure_receipt_entry_required": True,
            "independent_channel_verified_by_software": False,
            "hidden_ledger_fork_excluded_by_software": False,
        },
        "authorization_boundary": {
            "online_execution_authorized": False,
            "provider_key_use_authorized": False,
            "separate_single_use_grant_required": True,
            "consume_before_task_release_and_key_load": True,
            "retry_after_any_outcome_allowed": False,
        },
        "claim_boundary": {
            "envelope_alone_closes_status": False,
            "claim_scope": "exact_provider_surface_and_single_frozen_campaign_only",
            "unseen_is_external_attestation_not_software_proof": True,
            "model_quality_claim_allowed": False,
            "provider_registration_allowed": False,
            "private_or_non_synthetic_evaluation_allowed": False,
            "historical_attribution_recovered": False,
            "unknown_distribution_generalization_claim_allowed": False,
            "production_sla_claim_allowed": False,
        },
        "document_sha256": h,
        "signatures": [
            _signature("freeze_authority", freeze_key),
            _signature("task_custodian", custodian_key),
        ],
    }
    grant = {
        "schema_version": "provider-completion-external-authorization-grant/1.0",
        "document_type": "completion_telemetry_closure_authorization_grant",
        "grant_id": "PCEGRANT-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "authorized_at_utc": "2026-09-04T03:00:00Z",
        "not_before_utc": "2026-09-04T03:00:01Z",
        "expires_at_utc": "2026-09-04T04:00:00Z",
        "explicit_user_authorization_id_sha256": h,
        "explicit_user_authorization_binding_sha256": h2,
        "preregistration_envelope_sha256": h3,
        "preregistration_freeze_entry_sha256": h,
        "ledger_sequence": 2,
        "ledger_head_sha256": h2,
        "candidate_commit": commit,
        "candidate_tree": tree,
        "source_integrity_commitment_sha256": h,
        "runtime_registry_commitment_sha256": h2,
        "denominator_plan_commitment_sha256": h3,
        "external_plan_binding_sha256": h,
        "budget_policy_commitment_sha256": h2,
        "pricing_snapshot_date": "2026-09-04",
        "pricing_source_snapshot_sha256": h3,
        "pricing_retrieved_at_utc": "2026-09-04T02:45:00Z",
        "official_pricing_attestation_current": True,
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-flash",
        "api_origin": "https://api.deepseek.com",
        "api_surface": "responses",
        "transport_id": "openai_compatible_responses",
        "single_use": True,
        "consume_before_task_release_and_key_load": True,
        "agents_sdk_retries": 0,
        "http_client_retries": 0,
        "resume": False,
        "fallback": False,
        "status": "authorized",
        "closure_claim_allowed_by_grant": False,
        "provider_registration_allowed": False,
        "model_quality_claim_allowed": False,
        "document_sha256": h,
        "signatures": [
            _signature("freeze_authority", freeze_key),
            _signature("task_custodian", custodian_key),
        ],
    }
    consumption = {
        "schema_version": "provider-completion-external-consumption-receipt/1.0",
        "document_type": "completion_telemetry_closure_consumption_receipt",
        "receipt_id": "PCECONSUME-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "consumed_at_utc": "2026-09-04T03:00:02Z",
        "preregistration_envelope_sha256": h,
        "preregistration_freeze_entry_sha256": h2,
        "authorization_grant_sha256": h3,
        "explicit_user_authorization_id_sha256": h,
        "candidate_commit": commit,
        "denominator_plan_commitment_sha256": h2,
        "external_plan_binding_sha256": h3,
        "task_released_at_consumption": False,
        "provider_key_loaded_at_consumption": False,
        "external_consumption_ledger_entry_required_before_task_release_or_key_load": True,
        "network_attempts_at_consumption": 0,
        "model_requests_at_consumption": 0,
        "single_use_consumed": True,
        "retry_authorized": False,
        "resume_authorized": False,
        "status": "consumed",
        "provider_key_persisted": False,
        "task_content_persisted": False,
        "document_sha256": h,
    }
    ledger = {
        "schema_version": "provider-completion-external-ledger-entry/1.0",
        "document_type": "completion_telemetry_external_ledger_entry",
        "entry_type": "preregistration_frozen",
        "ledger_id": "PCELEDGER-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "sequence": 2,
        "previous_head_sha256": h,
        "occurred_at_utc": "2026-09-04T02:30:00Z",
        "candidate_freeze_receipt_sha256": h2,
        "preregistration_envelope_sha256": h3,
        "authorization_grant_sha256": None,
        "consumption_receipt_sha256": None,
        "closure_receipt_sha256": None,
        "entry_sha256": h,
        "resulting_head_sha256": h2,
        "signatures": [_signature("ledger_witness", witness_key)],
    }
    closure = {
        "schema_version": "provider-completion-closure-receipt/1.0",
        "document_type": "completion_telemetry_closure_receipt",
        "receipt_id": "PCECLOSE-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "campaign_id": "PCECAMP-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "completed_at_utc": "2026-09-04T03:30:00Z",
        "receipt_issued_at_utc": "2026-09-04T03:31:00Z",
        "bundle_status": "complete",
        "artifact_publication_disposition": "normal",
        "terminal_stage": "none",
        "local_terminal_outcome": "succeeded",
        "terminal_provider_outcome_state": "response_observed",
        "terminal_error_code": None,
        "last_completed_artifact_write_stage": "manifest_written",
        "artifact_error_code": None,
        "preregistration_envelope_sha256": h,
        "preregistration_freeze_entry_sha256": h2,
        "authorization_grant_sha256": h3,
        "consumption_receipt_sha256": h,
        "authorization_consumption_entry_sha256": h2,
        "task_released": True,
        "task_released_at_utc": "2026-09-04T03:00:04Z",
        "provider_key_loaded": True,
        "provider_key_loaded_at_utc": "2026-09-04T03:00:05Z",
        "released_task_bundle_commitment_sha256": h,
        "released_case_handle_order_commitment_sha256": h3,
        "task_bundle_opening_verified_in_memory": True,
        "task_bundle_opening_persisted": False,
        "custodian_private_leak_canary_scan_status": "passed",
        "public_generic_privacy_scan_status": "passed",
        "canary_injection_verified_for_every_send": True,
        "database_origin_status": "fresh_path_confirmed",
        "database_mutation_status": "normative_only",
        "released_execution_input_order_commitment_sha256": h2,
        "closure_bundle_manifest_sha256": h2,
        "partial_artifacts": {},
        "observed_audit_run_set_commitment_sha256": h3,
        "ordered_final_chain_heads_commitment_sha256": h,
        "planned_case_count": 2,
        "attempt_count": 2,
        "accepted_response_count": 2,
        "network_attempt_count_observed": 2,
        "model_request_count_observed": 2,
        "usage_complete": True,
        "observed_input_tokens": 100,
        "observed_output_tokens": 20,
        "observed_cost_cny": "0.000480000000",
        "evidence_valid": True,
        "evidence_error_code": None,
        "pre_anchor_closure_eligible": True,
        "closure_reasons": [],
        "closure_reasons_commitment_sha256": "2507147c335dbc6ec940d57f870df0275bc8143fec60f99385ede8d5bc2642cf",
        "task_content_included_in_receipt": False,
        "case_ids_included_in_receipt": False,
        "records_included_in_receipt": False,
        "provider_content_included_in_receipt": False,
        "raw_response_cleanup_status": "completed",
        "post_request_cleanup_status": "completed",
        "provider_key_reference_release_status": "released",
        "document_sha256": h,
        "signatures": [_signature("task_custodian", custodian_key)],
    }
    unseen_bundle = {
        "schema_version": "completion-telemetry-unseen-case-bundle/1.0",
        "bundle_id": "PCEBUNDLE-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "synthetic_only": True,
        "candidate_freeze_receipt_sha256": h,
        "seen_case_exclusion_manifest_sha256": h2,
        "leak_canary_b64": "A" * 43 + "=",
        "leak_canary_injected_into_every_request": True,
        "cases": [
            {
                "case_handle": "PCECASE-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                "task_schema_version": "completion-telemetry-synthetic-task/1.0",
                "user_input": "Return one short synthetic response.",
                "max_turns_per_case": 2,
                "tools": [],
                "golden": None,
                "scorer": None,
            },
            {
                "case_handle": "PCECASE-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                "task_schema_version": "completion-telemetry-synthetic-task/1.0",
                "user_input": "Return a second short synthetic response.",
                "max_turns_per_case": 2,
                "tools": [],
                "golden": None,
                "scorer": None,
            },
        ],
        "participant_derived_data_present": False,
        "private_or_non_synthetic_data_present": False,
        "direct_or_quasi_identifier_data_present": False,
    }
    closure_manifest = {
        "schema_version": "provider-completion-closure-bundle-manifest/1.0",
        "status": "complete",
        "campaign_id": "PCECAMP-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "preregistration_envelope_sha256": h,
        "preregistration_freeze_entry_sha256": h2,
        "authorization_grant_sha256": h3,
        "consumption_receipt_sha256": h,
        "closure_evidence_contract_commitment_sha256": "a96b39a1f4bafa3bcf512fd88a5e7b0ca3987ca2bc9804642a1d1259cbf8cad0",
        "denominator_plan_commitment_sha256": h3,
        "external_plan_binding_sha256": h,
        "audit_database_schema_commitment_sha256": h2,
        "files": {
            name: {"bytes": 100, "sha256": h3}
            for name in (
                "phase6_audit.sqlite3",
                "phase6_audit_index.json",
                "phase6_completion_telemetry.json",
            )
        },
        "manifest_self_hash_included": False,
        "raw_provider_content_persisted": False,
        "task_content_persisted": False,
        "api_key_persisted": False,
    }
    transport_send = {
        "schema_version": "provider-completion-transport-send/1.0",
        "case_id": "PCECASE-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "attempt_index": 0,
        "case_attempt_index": 0,
        "network_call_index": 0,
        "provider_id": "deepseek",
        "api_surface": "responses",
        "transport_id": "openai_compatible_responses",
        "adapter_version": "deepseek-responses-adapter/1.0",
        "method": "POST",
        "origin": "https://api.deepseek.com",
        "path": "/responses",
        "requested_output_token_cap": 512,
        "execution_input_commitment_sha256": h3,
        "audit_request_sha256": h,
        "external_plan_binding_sha256": h,
        "authorization_grant_sha256": h2,
        "headers_persisted": False,
        "request_body_persisted": False,
        "task_content_persisted": False,
    }
    postrun_facts = {
        "schema_version": "provider-completion-postrun-attested-facts/1.0",
        "campaign_id": "PCECAMP-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "receipt_id": "PCECLOSE-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "completed_at_utc": "2026-09-04T03:30:00Z",
        "receipt_issued_at_utc": "2026-09-04T03:31:00Z",
        "terminal_stage": "none",
        "local_terminal_outcome": "succeeded",
        "terminal_provider_outcome_state": "response_observed",
        "terminal_error_code": None,
        "last_completed_artifact_write_stage": "manifest_written",
        "artifact_error_code": None,
        "task_released": True,
        "task_released_at_utc": "2026-09-04T03:00:04Z",
        "provider_key_loaded": True,
        "provider_key_loaded_at_utc": "2026-09-04T03:00:05Z",
        "released_task_bundle_commitment_sha256": h,
        "released_case_handle_order_commitment_sha256": h3,
        "released_execution_input_order_commitment_sha256": h2,
        "task_bundle_opening_verified_in_memory": True,
        "task_bundle_opening_persisted": False,
        "custodian_private_leak_canary_scan_status": "passed",
        "public_generic_privacy_scan_status": "passed",
        "canary_injection_verified_for_every_send": True,
        "database_origin_status": "fresh_path_confirmed",
        "database_mutation_status": "normative_only",
        "network_attempt_count_observed": 2,
        "model_request_count_observed": 2,
        "raw_response_cleanup_status": "completed",
        "post_request_cleanup_status": "completed",
        "provider_key_reference_release_status": "released",
        "task_content_included_in_facts": False,
        "provider_content_included_in_facts": False,
        "derived_closure_claim_included": False,
    }
    run_event = {
        "schema_version": "provider-completion-closure-run-event-projection/1.0",
        "event_type": "run_started",
        "actor_kind": "system",
        "safe_payload": {
            "mode": "provider_completion_external_closure",
            "request_sha256": h,
            "dataset_sha256": None,
        },
    }
    observation_source = "d" * 64
    ledger_id = "PCELEDGER-" + "A" * 32

    def ledger_anchor(entry_type: str, sequence: int, occurred: str, observed: str) -> dict:
        return {
            "entry_sha256": h,
            "ledger_id": ledger_id,
            "entry_type": entry_type,
            "sequence": sequence,
            "previous_head_sha256": zero if sequence == 1 else h2,
            "resulting_head_sha256": h3,
            "entry_occurred_at_utc": occurred,
            "observed_at_utc": observed,
            "observation_source_commitment_sha256": observation_source,
        }

    observation_bundle = {
        "schema_version": "provider-completion-external-observation-bundle/1.0",
        "mode": "pre_receipt",
        "trust_manifest_observation": {
            "document_sha256": h,
            "observed_at_utc": "2026-09-03T23:30:00Z",
            "observation_source_commitment_sha256": observation_source,
        },
        "preregistration_envelope_observation": {
            "document_sha256": h2,
            "observed_at_utc": "2026-09-04T02:15:00Z",
            "observation_source_commitment_sha256": observation_source,
        },
        "user_authorization_observation": {
            "authorization_id_sha256": h,
            "authorization_binding_sha256": h2,
            "observed_at_utc": "2026-09-04T02:45:00Z",
            "observation_source_commitment_sha256": observation_source,
        },
        "candidate_freeze_anchor": ledger_anchor(
            "candidate_frozen", 1, "2026-09-04T00:05:00Z", "2026-09-04T00:10:00Z"
        ),
        "preregistration_freeze_anchor": ledger_anchor(
            "preregistration_frozen", 2, "2026-09-04T02:20:00Z", "2026-09-04T02:30:00Z"
        ),
        "authorization_consumed_anchor": ledger_anchor(
            "authorization_consumed", 3, "2026-09-04T03:00:03Z", "2026-09-04T03:00:04Z"
        ),
        "manifest_observation": {
            "state": "present",
            "manifest_sha256": h3,
            "observed_at_utc": "2026-09-04T03:30:30Z",
            "observation_source_commitment_sha256": observation_source,
            "last_completed_artifact_write_stage": "manifest_written",
        },
        "closure_receipt_observation": None,
        "closure_evidence_anchor": None,
        "artifact_values_may_supply_expected_values": False,
    }
    return {
        "authorization_grant_v1.schema.json": grant,
        "candidate_freeze_receipt_v1.schema.json": candidate,
        "closure_bundle_manifest_v1.schema.json": closure_manifest,
        "closure_receipt_v1.schema.json": closure,
        "closure_run_event_projection_v1.schema.json": run_event,
        "consumption_receipt_v1.schema.json": consumption,
        "external_ledger_entry_v1.schema.json": ledger,
        "external_observation_bundle_v1.schema.json": observation_bundle,
        "external_preregistration_envelope_v1.schema.json": envelope,
        "external_trust_manifest_v1.schema.json": trust,
        "postrun_attested_facts_v1.schema.json": postrun_facts,
        "seen_task_exclusion_manifest_v1.schema.json": seen,
        "transport_send_payload_v1.schema.json": transport_send,
        "unseen_case_bundle_v1.schema.json": unseen_bundle,
    }


def _invalid_evidence_receipt(
    source: dict,
    *,
    disposition: str,
    error_code: str,
) -> dict:
    value = copy.deepcopy(source)
    value.update(
        {
            "artifact_publication_disposition": disposition,
            "closure_bundle_manifest_sha256": None,
            "partial_artifacts": {},
            "observed_audit_run_set_commitment_sha256": None,
            "ordered_final_chain_heads_commitment_sha256": None,
            "attempt_count": None,
            "accepted_response_count": None,
            "usage_complete": None,
            "observed_input_tokens": None,
            "observed_output_tokens": None,
            "observed_cost_cny": None,
            "evidence_valid": False,
            "evidence_error_code": error_code,
            "pre_anchor_closure_eligible": False,
            "closure_reasons": [],
            "closure_reasons_commitment_sha256": (
                "2507147c335dbc6ec940d57f870df0275bc8143fec60f99385ede8d5bc2642cf"
            ),
        }
    )
    return value


class ProviderCompletionExternalPreregistrationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = _load(CONTRACT_PATH)

    def test_design_commitment_and_all_bound_files_recompute(self) -> None:
        body = copy.deepcopy(self.contract)
        commitment = body["contract_commitment"]
        expected = commitment.pop("sha256")
        canonical = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        actual = hashlib.sha256(
            commitment["domain"].encode("utf-8") + b"\0" + canonical
        ).hexdigest()
        self.assertEqual(actual, expected)
        for binding in self.contract["schema_bindings"].values():
            if not isinstance(binding, dict) or "relative_path" not in binding:
                continue
            path = (ROOT / binding["relative_path"]).resolve()
            self.assertTrue(path.is_relative_to(ROOT.resolve()))
            self.assertEqual(path.stat().st_size, binding["bytes"])
            self.assertEqual(_sha(path), binding["file_sha256"])
            Draft202012Validator.check_schema(_load(path))
        for predecessor in self.contract["predecessors"].values():
            path = (ROOT / predecessor["relative_path"]).resolve()
            self.assertTrue(path.is_relative_to(ROOT.resolve()))
            self.assertEqual(path.stat().st_size, predecessor["bytes"])
            self.assertEqual(_sha(path), predecessor["file_sha256"])
        for supporting in self.contract["supporting_contracts"].values():
            path = (ROOT / supporting["relative_path"]).resolve()
            self.assertEqual(path.stat().st_size, supporting["bytes"])
            self.assertEqual(_sha(path), supporting["file_sha256"])
            value = _load(path)
            semantic = copy.deepcopy(value)
            nested = semantic["contract_commitment"]
            expected_semantic = nested.pop("sha256")
            canonical_semantic = json.dumps(
                semantic,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            self.assertEqual(
                hashlib.sha256(
                    nested["domain"].encode("utf-8")
                    + b"\0"
                    + canonical_semantic
                ).hexdigest(),
                expected_semantic,
            )
            self.assertEqual(
                supporting["semantic_commitment_sha256"],
                expected_semantic,
            )

    def test_transitive_schema_binding_set_and_exact_contract_enums_match(self) -> None:
        closure_contract = _load(PACKAGE / "closure_evidence_contract_v1.json")
        direct = {
            Path(binding["relative_path"]).name
            for binding in self.contract["schema_bindings"].values()
            if isinstance(binding, dict) and "relative_path" in binding
        }
        supporting_bindings = [
            closure_contract["manifest_schema"],
            closure_contract["completion_event_contract"][
                "transport_send_payload_schema"
            ],
            closure_contract["completion_event_contract"][
                "non_completion_event_projection_schema"
            ],
            closure_contract["postrun_facts_contract"]["schema"],
            closure_contract["offline_verifier_boundary"][
                "external_observation_bundle_schema"
            ],
        ]
        supporting = {
            Path(binding["relative_path"]).name for binding in supporting_bindings
        }
        actual = {path.name for path in SCHEMA_ROOT.glob("*.schema.json")}
        self.assertEqual(direct | supporting, actual)
        self.assertFalse(direct & supporting)
        for binding in supporting_bindings:
            path = ROOT / binding["relative_path"]
            self.assertEqual(path.stat().st_size, binding["bytes"])
            self.assertEqual(_sha(path), binding["file_sha256"])
            Draft202012Validator.check_schema(_load(path))

        receipt_schema = _load(SCHEMA_ROOT / "closure_receipt_v1.schema.json")
        expected_body = [
            field
            for field in receipt_schema["required"]
            if field not in {"document_sha256", "signatures"}
        ]
        self.assertEqual(
            self.contract["closure_evidence_contract"][
                "closure_receipt_unsigned_body_exact_fields"
            ],
            expected_body,
        )
        self.assertEqual(
            closure_contract["closure_contract"][
                "closure_receipt_unsigned_body_exact_fields"
            ],
            expected_body,
        )
        self.assertEqual(
            closure_contract["closure_contract"]["reason_order"],
            receipt_schema["$defs"]["closure_reason"]["enum"],
        )
        self.assertEqual(
            set(closure_contract["closure_contract"]["evidence_error_priority_order"]),
            set(receipt_schema["$defs"]["evidence_error_code"]["enum"]),
        )
        postrun_schema = _load(SCHEMA_ROOT / "postrun_attested_facts_v1.schema.json")
        self.assertEqual(
            receipt_schema["$defs"]["terminal_error_code"]["enum"],
            postrun_schema["$defs"]["terminal_error_code"]["enum"],
        )
        self.assertEqual(
            receipt_schema["$defs"]["artifact_error_code"]["enum"],
            postrun_schema["$defs"]["artifact_error_code"]["enum"],
        )
        self.assertNotIn("closure_claim_allowed", receipt_schema["required"])
        self.assertIn("pre_anchor_closure_eligible", receipt_schema["required"])

    def test_all_schema_representatives_are_strictly_valid(self) -> None:
        documents = _representative_documents()
        self.assertEqual(
            set(documents),
            {path.name for path in SCHEMA_ROOT.glob("*.schema.json")},
        )
        for filename, document in documents.items():
            with self.subTest(filename=filename):
                schema = _load(SCHEMA_ROOT / filename)
                Draft202012Validator(
                    schema,
                    format_checker=FormatChecker(),
                ).validate(document)
                extra = copy.deepcopy(document)
                extra["forbidden_extra"] = None
                with self.assertRaises(ValidationError):
                    Draft202012Validator(schema).validate(extra)

    def test_schema_required_nested_and_role_constraints_are_executable(self) -> None:
        documents = _representative_documents()
        for filename, document in documents.items():
            schema = _load(SCHEMA_ROOT / filename)
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            missing = copy.deepcopy(document)
            missing.pop(schema["required"][0])
            with self.subTest(filename=filename, mutation="missing_required"):
                with self.assertRaises(ValidationError):
                    validator.validate(missing)

        nested_mutations = []
        envelope = copy.deepcopy(documents["external_preregistration_envelope_v1.schema.json"])
        envelope["runtime_plan"]["budget_policy"]["forbidden_extra"] = None
        nested_mutations.append(("external_preregistration_envelope_v1.schema.json", envelope))
        trust = copy.deepcopy(documents["external_trust_manifest_v1.schema.json"])
        trust["roles"][0]["forbidden_extra"] = None
        nested_mutations.append(("external_trust_manifest_v1.schema.json", trust))
        seen = copy.deepcopy(documents["seen_task_exclusion_manifest_v1.schema.json"])
        seen["source_inventory"][0].pop("source_snapshot_commitment_sha256")
        nested_mutations.append(("seen_task_exclusion_manifest_v1.schema.json", seen))
        seen_duplicate = copy.deepcopy(
            documents["seen_task_exclusion_manifest_v1.schema.json"]
        )
        seen_duplicate["source_inventory"][1]["source_class"] = (
            seen_duplicate["source_inventory"][0]["source_class"]
        )
        nested_mutations.append(
            ("seen_task_exclusion_manifest_v1.schema.json", seen_duplicate)
        )
        grant = copy.deepcopy(documents["authorization_grant_v1.schema.json"])
        grant["signatures"][1]["role"] = "freeze_authority"
        nested_mutations.append(("authorization_grant_v1.schema.json", grant))
        trust_duplicate = copy.deepcopy(
            documents["external_trust_manifest_v1.schema.json"]
        )
        trust_duplicate["roles"][1]["role"] = "freeze_authority"
        nested_mutations.append(("external_trust_manifest_v1.schema.json", trust_duplicate))
        for filename, document in nested_mutations:
            with self.subTest(filename=filename, mutation="nested_or_role"):
                with self.assertRaises(ValidationError):
                    Draft202012Validator(
                        _load(SCHEMA_ROOT / filename),
                        format_checker=FormatChecker(),
                    ).validate(document)

    def test_closure_and_ledger_condition_matrices_reject_contradictions(self) -> None:
        documents = _representative_documents()
        closure_schema = _load(SCHEMA_ROOT / "closure_receipt_v1.schema.json")
        closure_validator = Draft202012Validator(
            closure_schema,
            format_checker=FormatChecker(),
        )
        base = documents["closure_receipt_v1.schema.json"]
        contradictions = []
        invalid_evidence = copy.deepcopy(base)
        invalid_evidence["evidence_valid"] = False
        contradictions.append(invalid_evidence)
        release = copy.deepcopy(base)
        release["task_released"] = False
        contradictions.append(release)
        outcome = copy.deepcopy(base)
        outcome.update(
            {
                "local_terminal_outcome": "timed_out",
                "terminal_error_code": "pce_response_failed",
                "pre_anchor_closure_eligible": False,
                "closure_reasons": ["local_terminal_outcome_not_succeeded"],
            }
        )
        contradictions.append(outcome)
        cleanup = copy.deepcopy(base)
        cleanup.update(
            {
                "local_terminal_outcome": "failed",
                "terminal_error_code": "pce_raw_response_cleanup_failed",
                "terminal_stage": "raw_response_cleanup",
                "raw_response_cleanup_status": "completed",
                "post_request_cleanup_status": "not_applicable",
                "pre_anchor_closure_eligible": False,
                "closure_reasons": ["local_terminal_outcome_not_succeeded"],
            }
        )
        contradictions.append(cleanup)
        disposition = copy.deepcopy(base)
        disposition["artifact_publication_disposition"] = "sensitive_quarantined"
        contradictions.append(disposition)
        prefix = copy.deepcopy(base)
        prefix.update(
            {
                "bundle_status": "manifestless_failure",
                "last_completed_artifact_write_stage": "audit_database_written",
                "artifact_error_code": "pce_artifact_audit_index_failed",
                "closure_bundle_manifest_sha256": None,
                "partial_artifacts": {},
                "evidence_valid": False,
                "evidence_error_code": "closure_bundle_manifest_invalid",
                "pre_anchor_closure_eligible": False,
                "observed_audit_run_set_commitment_sha256": None,
                "ordered_final_chain_heads_commitment_sha256": None,
                "attempt_count": None,
                "accepted_response_count": None,
                "usage_complete": None,
                "observed_input_tokens": None,
                "observed_output_tokens": None,
                "observed_cost_cny": None,
                "closure_reasons": [],
            }
        )
        contradictions.append(prefix)
        for index, document in enumerate(contradictions):
            with self.subTest(index=index):
                with self.assertRaises(ValidationError):
                    closure_validator.validate(document)

        ledger_schema = _load(SCHEMA_ROOT / "external_ledger_entry_v1.schema.json")
        bad_ledger = copy.deepcopy(documents["external_ledger_entry_v1.schema.json"])
        bad_ledger["entry_type"] = "candidate_frozen"
        bad_ledger["candidate_freeze_receipt_sha256"] = None
        bad_ledger["preregistration_envelope_sha256"] = None
        with self.assertRaises(ValidationError):
            Draft202012Validator(ledger_schema).validate(bad_ledger)

    def test_privacy_priority_matrix_is_satisfiable_without_overwriting_writer_stage(self) -> None:
        base = _representative_documents()["closure_receipt_v1.schema.json"]
        validator = Draft202012Validator(
            _load(SCHEMA_ROOT / "closure_receipt_v1.schema.json"),
            format_checker=FormatChecker(),
        )
        for canary in ("passed", "unavailable", "leak_detected"):
            for generic in ("passed", "unavailable", "sensitive_detected"):
                for origin in ("fresh_path_confirmed", "path_preexisted", "unavailable"):
                    for mutation in ("normative_only", "reuse_or_delete_detected", "unavailable"):
                        if canary == "leak_detected" or generic == "sensitive_detected":
                            disposition = "sensitive_quarantined"
                            error = "closure_sensitive_content_detected"
                        elif canary == "unavailable" or generic == "unavailable":
                            disposition = "privacy_unverified_quarantined"
                            error = "closure_sensitive_scan_unavailable"
                        elif origin != "fresh_path_confirmed" or mutation != "normative_only":
                            disposition = "privacy_unverified_quarantined"
                            error = "closure_database_origin_untrusted"
                        else:
                            disposition = "normal"
                            error = None
                        document = copy.deepcopy(base)
                        document.update(
                            {
                                "custodian_private_leak_canary_scan_status": canary,
                                "public_generic_privacy_scan_status": generic,
                                "database_origin_status": origin,
                                "database_mutation_status": mutation,
                            }
                        )
                        if error is not None:
                            document = _invalid_evidence_receipt(
                                document,
                                disposition=disposition,
                                error_code=error,
                            )
                        with self.subTest(
                            canary=canary,
                            generic=generic,
                            origin=origin,
                            mutation=mutation,
                        ):
                            validator.validate(document)
                            self.assertEqual(
                                document["last_completed_artifact_write_stage"],
                                "manifest_written",
                            )

    def test_manifestless_failure_has_a_valid_exact_writer_prefix(self) -> None:
        base = _representative_documents()["closure_receipt_v1.schema.json"]
        document = _invalid_evidence_receipt(
            base,
            disposition="normal",
            error_code="closure_bundle_manifest_invalid",
        )
        document.update(
            {
                "bundle_status": "manifestless_failure",
                "last_completed_artifact_write_stage": "audit_database_written",
                "artifact_error_code": "pce_artifact_audit_index_failed",
                "local_terminal_outcome": "failed",
                "terminal_error_code": "pce_key_load_failed",
                "terminal_stage": "key_load",
                "terminal_provider_outcome_state": "not_applicable",
                "provider_key_loaded": False,
                "provider_key_loaded_at_utc": None,
                "provider_key_reference_release_status": "not_loaded",
                "network_attempt_count_observed": 0,
                "model_request_count_observed": 0,
                "raw_response_cleanup_status": "not_applicable",
                "post_request_cleanup_status": "not_applicable",
                "partial_artifacts": {
                    "phase6_audit.sqlite3": {"bytes": 100, "sha256": "a" * 64}
                },
            }
        )
        Draft202012Validator(
            _load(SCHEMA_ROOT / "closure_receipt_v1.schema.json"),
            format_checker=FormatChecker(),
        ).validate(document)

    def test_event_projection_exercises_every_non_completion_branch(self) -> None:
        h = "a" * 64
        schema = _load(SCHEMA_ROOT / "closure_run_event_projection_v1.schema.json")
        validator = Draft202012Validator(schema)
        documents = [
            {
                "schema_version": "provider-completion-closure-run-event-projection/1.0",
                "event_type": "run_started",
                "actor_kind": "system",
                "safe_payload": {
                    "mode": "provider_completion_external_closure",
                    "request_sha256": h,
                    "dataset_sha256": None,
                },
            },
            {
                "schema_version": "provider-completion-closure-run-event-projection/1.0",
                "event_type": "model_call_recorded",
                "actor_kind": "agent_sdk",
                "safe_payload": {
                    "model_call_id": "MODEL-AAAAAAAAAAAAAAAA",
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                    "latency_ms": 1.0,
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cached_tokens": 0,
                    "cost_usd": None,
                    "outcome": "succeeded",
                    "error_code": None,
                },
            },
            {
                "schema_version": "provider-completion-closure-run-event-projection/1.0",
                "event_type": "model_run_usage_recorded",
                "actor_kind": "agent_sdk",
                "safe_payload": {
                    "usage_complete": True,
                    "request_count": 1,
                    "input_unit_count": 1,
                    "output_unit_count": 1,
                    "total_unit_count": 2,
                    "cached_input_unit_count": 0,
                    "response_detail_count": 1,
                    "estimated_cost_usd": None,
                    "model_call_rows_recorded": 1,
                    "latency_allocation": "equal_share_of_agent_segment",
                    "model_call_cost_method": "cny_recomputed_only_in_closure",
                    "provider": "deepseek",
                    "transport": "openai_compatible_responses",
                },
            },
            {
                "schema_version": "provider-completion-closure-run-event-projection/1.0",
                "event_type": "run_status_changed",
                "actor_kind": "system",
                "safe_payload": {"from": "running", "to": "completed", "error_code": None},
            },
        ]
        for document in documents:
            validator.validate(document)
            wrong_actor = copy.deepcopy(document)
            wrong_actor["actor_kind"] = (
                "agent_sdk" if document["actor_kind"] == "system" else "system"
            )
            with self.assertRaises(ValidationError):
                validator.validate(wrong_actor)

    def test_every_terminal_error_code_has_one_executable_truth_tuple(self) -> None:
        documents = _representative_documents()
        base = documents["postrun_attested_facts_v1.schema.json"]
        schema = _load(SCHEMA_ROOT / "postrun_attested_facts_v1.schema.json")
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        error_codes = schema["$defs"]["terminal_error_code"]["enum"]
        prefix_to_stage = {
            "pce_post_consumption_": "post_consumption",
            "pce_task_release_": "task_release",
            "pce_task_opening_": "task_opening",
            "pce_key_load_": "key_load",
            "pce_pre_send_": "pre_send",
            "pce_transport_": "transport",
            "pce_response_": "response",
            "pce_raw_response_cleanup_": "raw_response_cleanup",
            "pce_post_request_cleanup_": "post_request_cleanup",
            "pce_key_reference_release_": "key_reference_release",
            "pce_run_finalization_": "run_finalization",
        }
        local_only = {
            "post_consumption",
            "task_release",
            "task_opening",
            "key_load",
            "post_request_cleanup",
            "key_reference_release",
            "run_finalization",
        }

        def canonical(code: str) -> dict:
            value = copy.deepcopy(base)
            stage = next(
                stage for prefix, stage in prefix_to_stage.items() if code.startswith(prefix)
            )
            if code.endswith("_timed_out"):
                outcome = "timed_out"
            elif code.endswith("_cancelled"):
                outcome = "cancelled"
            else:
                outcome = "failed"
            if stage in local_only:
                provider_state = "not_applicable"
            elif stage == "pre_send":
                provider_state = "not_sent"
            elif stage == "transport":
                provider_state = "outcome_unknown"
            elif stage == "raw_response_cleanup":
                provider_state = "response_observed"
            else:
                provider_state = (
                    "response_observed"
                    if code == "pce_response_failed"
                    else "outcome_unknown"
                )
            value.update(
                {
                    "terminal_stage": stage,
                    "local_terminal_outcome": outcome,
                    "terminal_provider_outcome_state": provider_state,
                    "terminal_error_code": code,
                }
            )
            if stage in {"post_consumption", "task_release"}:
                value.update(
                    {
                        "task_released": False,
                        "task_released_at_utc": None,
                        "released_task_bundle_commitment_sha256": None,
                        "released_case_handle_order_commitment_sha256": None,
                        "released_execution_input_order_commitment_sha256": None,
                        "task_bundle_opening_verified_in_memory": False,
                        "provider_key_loaded": False,
                        "provider_key_loaded_at_utc": None,
                        "provider_key_reference_release_status": "not_loaded",
                        "network_attempt_count_observed": 0,
                        "model_request_count_observed": 0,
                        "raw_response_cleanup_status": "not_applicable",
                        "post_request_cleanup_status": "not_applicable",
                    }
                )
            elif stage in {"task_opening", "key_load"}:
                value.update(
                    {
                        "task_released": True,
                        "task_bundle_opening_verified_in_memory": stage == "key_load",
                        "provider_key_loaded": False,
                        "provider_key_loaded_at_utc": None,
                        "provider_key_reference_release_status": "not_loaded",
                        "network_attempt_count_observed": 0,
                        "model_request_count_observed": 0,
                        "raw_response_cleanup_status": "not_applicable",
                        "post_request_cleanup_status": "not_applicable",
                    }
                )
            elif stage == "pre_send":
                value.update(
                    {
                        "network_attempt_count_observed": 2,
                        "model_request_count_observed": 3,
                        "raw_response_cleanup_status": "not_applicable",
                        "post_request_cleanup_status": "not_applicable",
                    }
                )
            elif stage == "transport":
                value.update(
                    {
                        "network_attempt_count_observed": 2,
                        "model_request_count_observed": 2,
                        "raw_response_cleanup_status": "not_applicable",
                        "post_request_cleanup_status": "not_applicable",
                    }
                )
            elif stage == "response":
                value.update(
                    {
                        "network_attempt_count_observed": 2,
                        "model_request_count_observed": 2,
                        "raw_response_cleanup_status": (
                            "completed" if provider_state == "response_observed" else "not_applicable"
                        ),
                        "post_request_cleanup_status": "not_applicable",
                    }
                )
            if code == "pce_raw_response_cleanup_failed":
                value["raw_response_cleanup_status"] = "failed"
                value["post_request_cleanup_status"] = "not_applicable"
            elif code == "pce_raw_response_cleanup_timed_out":
                value["raw_response_cleanup_status"] = "timed_out"
                value["post_request_cleanup_status"] = "not_applicable"
            elif code == "pce_raw_response_cleanup_cancelled":
                value["raw_response_cleanup_status"] = "cancelled"
                value["post_request_cleanup_status"] = "not_applicable"
            elif code == "pce_post_request_cleanup_failed":
                value["raw_response_cleanup_status"] = "completed"
                value["post_request_cleanup_status"] = "failed"
            elif code == "pce_post_request_cleanup_timed_out":
                value["raw_response_cleanup_status"] = "completed"
                value["post_request_cleanup_status"] = "timed_out"
            elif code == "pce_post_request_cleanup_cancelled":
                value["raw_response_cleanup_status"] = "completed"
                value["post_request_cleanup_status"] = "cancelled"
            elif code.startswith("pce_key_reference_release_"):
                value["provider_key_reference_release_status"] = outcome
            return value

        self.assertEqual(len(error_codes), 33)
        for code in error_codes:
            value = canonical(code)
            with self.subTest(code=code, case="canonical"):
                validator.validate(value)
            wrong_stage = copy.deepcopy(value)
            wrong_stage["terminal_stage"] = (
                "run_finalization"
                if value["terminal_stage"] != "run_finalization"
                else "pre_send"
            )
            with self.subTest(code=code, case="wrong_stage"):
                with self.assertRaises(ValidationError):
                    validator.validate(wrong_stage)
            wrong_outcome = copy.deepcopy(value)
            wrong_outcome["local_terminal_outcome"] = (
                "cancelled"
                if value["local_terminal_outcome"] != "cancelled"
                else "failed"
            )
            with self.subTest(code=code, case="wrong_outcome"):
                with self.assertRaises(ValidationError):
                    validator.validate(wrong_outcome)
            wrong_provider = copy.deepcopy(value)
            wrong_provider["terminal_provider_outcome_state"] = (
                "not_sent"
                if value["terminal_provider_outcome_state"] != "not_sent"
                else "response_observed"
            )
            if value["terminal_stage"] in {"run_finalization"}:
                continue
            with self.subTest(code=code, case="wrong_provider_state"):
                with self.assertRaises(ValidationError):
                    validator.validate(wrong_provider)

        none = copy.deepcopy(base)
        validator.validate(none)
        none["local_terminal_outcome"] = "failed"
        none["terminal_error_code"] = "pce_run_finalization_failed"
        with self.assertRaises(ValidationError):
            validator.validate(none)

    def test_every_writer_prefix_and_bundle_status_is_executable(self) -> None:
        base = _representative_documents()["closure_receipt_v1.schema.json"]
        validator = Draft202012Validator(
            _load(SCHEMA_ROOT / "closure_receipt_v1.schema.json"),
            format_checker=FormatChecker(),
        )
        ordered = [
            "phase6_audit.sqlite3",
            "phase6_audit_index.json",
            "phase6_completion_telemetry.json",
        ]
        stages = [
            ("not_started", "pce_artifact_not_started_failed", 0),
            ("audit_database_written", "pce_artifact_audit_index_failed", 1),
            ("audit_index_written", "pce_artifact_completion_telemetry_failed", 2),
            ("completion_telemetry_written", "pce_artifact_manifest_failed", 3),
        ]
        for stage, error, count in stages:
            value = _invalid_evidence_receipt(
                base,
                disposition="normal",
                error_code="closure_bundle_manifest_invalid",
            )
            value.update(
                {
                    "bundle_status": "manifestless_failure",
                    "last_completed_artifact_write_stage": stage,
                    "artifact_error_code": error,
                    "partial_artifacts": {
                        name: {"bytes": 1, "sha256": "a" * 64}
                        for name in ordered[:count]
                    },
                }
            )
            with self.subTest(stage=stage):
                validator.validate(value)
                wrong = copy.deepcopy(value)
                if count == 0:
                    wrong["partial_artifacts"][ordered[0]] = {
                        "bytes": 1,
                        "sha256": "a" * 64,
                    }
                else:
                    wrong["partial_artifacts"].pop(ordered[count - 1])
                with self.assertRaises(ValidationError):
                    validator.validate(wrong)

        invalid_manifest = _invalid_evidence_receipt(
            base,
            disposition="normal",
            error_code="closure_bundle_manifest_invalid",
        )
        invalid_manifest.update(
            {
                "bundle_status": "manifest_present_invalid",
                "last_completed_artifact_write_stage": "manifest_written",
                "artifact_error_code": None,
                "partial_artifacts": {
                    name: {"bytes": 1, "sha256": "a" * 64}
                    for name in (*ordered, "completion_closure_manifest.json")
                },
            }
        )
        validator.validate(invalid_manifest)
        validator.validate(base)

    def test_all_external_ledger_entry_shapes_are_strict(self) -> None:
        base = _representative_documents()["external_ledger_entry_v1.schema.json"]
        validator = Draft202012Validator(
            _load(SCHEMA_ROOT / "external_ledger_entry_v1.schema.json")
        )
        fields = [
            "candidate_freeze_receipt_sha256",
            "preregistration_envelope_sha256",
            "authorization_grant_sha256",
            "consumption_receipt_sha256",
            "closure_receipt_sha256",
        ]
        matrix = {
            "candidate_frozen": 1,
            "preregistration_frozen": 2,
            "authorization_consumed": 4,
            "closure_evidence_anchored": 5,
        }
        for entry_type, non_null_count in matrix.items():
            value = copy.deepcopy(base)
            value["entry_type"] = entry_type
            for index, field in enumerate(fields):
                value[field] = "a" * 64 if index < non_null_count else None
            with self.subTest(entry_type=entry_type, case="valid"):
                validator.validate(value)
            missing = copy.deepcopy(value)
            missing[fields[0]] = None
            with self.subTest(entry_type=entry_type, case="missing"):
                with self.assertRaises(ValidationError):
                    validator.validate(missing)
            if non_null_count < len(fields):
                extra = copy.deepcopy(value)
                extra[fields[non_null_count]] = "b" * 64
                with self.subTest(entry_type=entry_type, case="extra"):
                    with self.assertRaises(ValidationError):
                        validator.validate(extra)

    def test_external_observation_bundle_modes_and_manifest_states(self) -> None:
        base = _representative_documents()["external_observation_bundle_v1.schema.json"]
        validator = Draft202012Validator(
            _load(SCHEMA_ROOT / "external_observation_bundle_v1.schema.json"),
            format_checker=FormatChecker(),
        )
        validator.validate(base)
        final = copy.deepcopy(base)
        final["mode"] = "final"
        final["closure_receipt_observation"] = {
            "document_sha256": "a" * 64,
            "observed_at_utc": "2026-09-04T03:32:00Z",
            "observation_source_commitment_sha256": "d" * 64,
        }
        final["closure_evidence_anchor"] = copy.deepcopy(
            final["authorization_consumed_anchor"]
        )
        final["closure_evidence_anchor"].update(
            {
                "entry_type": "closure_evidence_anchored",
                "sequence": 4,
                "entry_occurred_at_utc": "2026-09-04T03:33:00Z",
                "observed_at_utc": "2026-09-04T03:34:00Z",
            }
        )
        validator.validate(final)
        withheld = copy.deepcopy(base)
        withheld["manifest_observation"] = {
            "state": "withheld_quarantined",
            "manifest_sha256": None,
            "observed_at_utc": "2026-09-04T03:30:30Z",
            "observation_source_commitment_sha256": "d" * 64,
            "last_completed_artifact_write_stage": "manifest_written",
            "quarantine_reason": "privacy_unverified",
        }
        validator.validate(withheld)
        broken = copy.deepcopy(final)
        broken["closure_evidence_anchor"] = None
        with self.assertRaises(ValidationError):
            validator.validate(broken)

    def test_authority_and_claim_state_remain_false(self) -> None:
        self.assertEqual(
            self.contract["status"],
            "design_only_not_implemented_not_authorized",
        )
        self.assertTrue(self.contract["implementation_state"]["field_contract_frozen"])
        self.assertTrue(
            _load(PACKAGE / "closure_evidence_contract_v1.json")[
                "implementation_state"
            ]["semantic_contract_frozen"]
        )
        self.assertFalse(self.contract["admission_prerequisites"]["admission_allowed"])
        self.assertFalse(self.contract["scope"]["runtime_authority_granted"])
        self.assertEqual(self.contract["scope"]["online_calls_performed"], 0)
        self.assertFalse(self.contract["scope"]["provider_key_loaded"])
        self.assertFalse(
            self.contract["authorization_boundary"]["online_execution_authorized"]
        )
        self.assertFalse(
            self.contract["authorization_boundary"]["provider_key_use_authorized"]
        )
        self.assertFalse(
            self.contract["claim_boundary"]["contract_or_schema_closes_status"]
        )

    def test_readme_publishes_exact_frozen_local_anchors(self) -> None:
        readme = (PACKAGE / "README.md").read_text(encoding="utf-8")
        design_sha = _sha(CONTRACT_PATH)
        closure_path = PACKAGE / "closure_evidence_contract_v1.json"
        closure = _load(closure_path)
        self.assertIn(f"{CONTRACT_PATH.stat().st_size:,} bytes", readme)
        self.assertIn(design_sha, readme)
        self.assertIn(self.contract["contract_commitment"]["sha256"], readme)
        self.assertIn(f"{closure_path.stat().st_size:,} bytes", readme)
        self.assertIn(_sha(closure_path), readme)
        self.assertIn(closure["contract_commitment"]["sha256"], readme)
        self.assertIn("not merged / not implemented / not authorized / not run", readme)

    def test_runtime_plan_and_ledger_relationships_are_explicit(self) -> None:
        runtime = self.contract["runtime_plan_contract"]
        self.assertEqual(
            runtime["maximum_total_model_request_cap"],
            "planned_case_count_times_max_turns_per_case",
        )
        self.assertTrue(runtime["network_attempt_cap_equals_total_model_request_cap"])
        self.assertTrue(runtime["planned_case_count_equals_case_ids_length"])
        self.assertTrue(runtime["one_planned_audit_run_id_per_case_handle"])
        ledger = self.contract["external_ledger_contract"]
        self.assertEqual(
            ledger["entry_types_in_order"],
            [
                "candidate_frozen",
                "preregistration_frozen",
                "authorization_consumed",
                "closure_evidence_anchored",
            ],
        )
        self.assertTrue(
            ledger[
                "future_authorization_binds_preregistration_entry_hash_sequence_and_resulting_head"
            ]
        )
        self.assertTrue(
            ledger[
                "future_closure_verifier_requires_external_final_entry_hash_sequence_and_head"
            ]
        )

    def test_unseen_is_only_a_signed_external_attestation(self) -> None:
        unseen = self.contract["new_unseen_attestation"]
        self.assertTrue(
            unseen[
                "source_inventory_completeness_is_signed_attestation_not_software_proof"
            ]
        )
        self.assertFalse(unseen["software_proves_no_prior_human_exposure"])
        self.assertFalse(unseen["software_proves_custodian_independence"])
        self.assertFalse(unseen["software_excludes_semantic_or_translation_overlap"])
        self.assertFalse(unseen["software_excludes_hidden_ledger_fork"])

    def test_new_contract_files_contain_no_local_path_or_secret_shape(self) -> None:
        forbidden = (
            re.compile(r"[A-Za-z]:\\Users\\"),
            re.compile(r"(?i)authorization\s*:\s*bearer"),
            re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
            re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        )
        for path in PACKAGE.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                for pattern in forbidden:
                    self.assertIsNone(pattern.search(text), path)


if __name__ == "__main__":
    unittest.main()
