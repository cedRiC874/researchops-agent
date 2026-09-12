from __future__ import annotations

import ast
import base64
import copy
import hashlib
import inspect
import json
import unittest
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from researchops_external_closure.documents import (
    VerifiedPreReceiptDocuments,
    verify_pre_receipt_documents,
)
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.primitives import (
    build_signature_message,
    canonical_json_bytes,
    compute_document_sha256,
    compute_ledger_entry_sha256,
    compute_ledger_resulting_head,
    derive_ed25519_key_id,
)
from researchops_external_closure.types import PreReceiptDocumentBytes


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = (
    ROOT / "evals" / "provider_completion_external_preregistration_v1" / "schemas"
)
SUPPORTING_COMMITMENT = (
    "a96b39a1f4bafa3bcf512fd88a5e7b0ca3987ca2bc9804642a1d1259cbf8cad0"
)

USER_BINDING_DOMAIN = (
    "researchops-provider-completion-user-authorization-binding-v1"
)
SEEN_SET_DOMAIN = "researchops-provider-completion-seen-case-exclusion-set-v1"
CASE_SET_DOMAIN = "researchops-provider-completion-case-handle-set-v1"
CASE_ORDER_DOMAIN = "researchops-provider-completion-case-handle-order-v1"
CAMPAIGN_DOMAIN = "researchops-provider-completion-campaign-id-v1"
AUDIT_RUN_DOMAIN = "researchops-provider-completion-audit-run-id-v1"
AUDIT_SET_DOMAIN = "researchops-provider-completion-audit-run-set-v1"
RUNTIME_PLAN_DOMAIN = "researchops-provider-completion-external-runtime-plan-v1"


def _h(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _domain_hash(domain: str, *parts: bytes) -> str:
    return hashlib.sha256(
        domain.encode("ascii") + b"\0" + b"\0".join(parts)
    ).hexdigest()


def _public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def _schemas() -> dict[str, dict]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in SCHEMA_ROOT.glob("*.schema.json")
    }


class SyntheticPreReceipt:
    """A deterministic, genuinely signed, content-free verifier fixture."""

    def __init__(
        self,
        *,
        same_custodian_key: bool = False,
        bad_key_derivation: bool = False,
        witness_expires_early: bool = False,
        bad_case_ids_commitment: bool = False,
        bad_candidate_projection: bool = False,
        bad_user_binding: bool = False,
    ) -> None:
        self.keys = {
            "freeze_authority": Ed25519PrivateKey.from_private_bytes(bytes(range(32))),
            "task_custodian": Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33))),
            "ledger_witness": Ed25519PrivateKey.from_private_bytes(bytes(range(2, 34))),
        }
        if same_custodian_key:
            self.keys["task_custodian"] = self.keys["freeze_authority"]
        self.key_ids = {
            role: derive_ed25519_key_id(_public_key_b64(key))
            for role, key in self.keys.items()
        }
        if bad_key_derivation:
            self.key_ids["freeze_authority"] = (
                "PCEKEY-FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
            )
        self.documents: dict[str, dict] = {}
        self._build(
            witness_expires_early=witness_expires_early,
            bad_case_ids_commitment=bad_case_ids_commitment,
            bad_candidate_projection=bad_candidate_projection,
            bad_user_binding=bad_user_binding,
        )

    def _signatures(self, document: dict, roles: tuple[str, ...]) -> list[dict]:
        subject = (
            document["entry_sha256"]
            if document["document_type"]
            == "completion_telemetry_external_ledger_entry"
            else document["document_sha256"]
        )
        result = []
        for role in roles:
            key_id = self.key_ids[role]
            message = build_signature_message(
                document_type=document["document_type"],
                role=role,
                key_id=key_id,
                signed_sha256=subject,
            )
            result.append(
                {
                    "role": role,
                    "key_id": key_id,
                    "algorithm": "ed25519",
                    "signed_sha256": subject,
                    "signature_b64": base64.b64encode(
                        self.keys[role].sign(message)
                    ).decode("ascii"),
                }
            )
        return result

    def _finalize_document(
        self, document: dict, roles: tuple[str, ...] = ()
    ) -> dict:
        document["document_sha256"] = compute_document_sha256(document)
        if roles:
            document["signatures"] = self._signatures(document, roles)
        return document

    def _ledger_entry(
        self,
        *,
        entry_type: str,
        sequence: int,
        previous_head: str,
        occurred_at: str,
        candidate_hash: str,
        envelope_hash: str | None,
        grant_hash: str | None,
        consumption_hash: str | None,
    ) -> dict:
        entry = {
            "schema_version": "provider-completion-external-ledger-entry/1.0",
            "document_type": "completion_telemetry_external_ledger_entry",
            "entry_type": entry_type,
            "ledger_id": "PCELEDGER-0123456789ABCDEF0123456789ABCDEF",
            "sequence": sequence,
            "previous_head_sha256": previous_head,
            "occurred_at_utc": occurred_at,
            "candidate_freeze_receipt_sha256": candidate_hash,
            "preregistration_envelope_sha256": envelope_hash,
            "authorization_grant_sha256": grant_hash,
            "consumption_receipt_sha256": consumption_hash,
            "closure_receipt_sha256": None,
            "entry_sha256": "0" * 64,
            "resulting_head_sha256": "0" * 64,
            "signatures": [],
        }
        entry["entry_sha256"] = compute_ledger_entry_sha256(entry)
        entry["resulting_head_sha256"] = compute_ledger_resulting_head(
            previous_head, entry["entry_sha256"]
        )
        entry["signatures"] = self._signatures(entry, ("ledger_witness",))
        return entry

    def _build(
        self,
        *,
        witness_expires_early: bool,
        bad_case_ids_commitment: bool,
        bad_candidate_projection: bool,
        bad_user_binding: bool,
    ) -> None:
        role_expires = {
            "freeze_authority": "2026-09-06T00:00:00Z",
            "task_custodian": "2026-09-06T00:00:00Z",
            "ledger_witness": (
                "2026-09-04T00:00:03.5Z"
                if witness_expires_early
                else "2026-09-06T00:00:00Z"
            ),
        }
        trust = {
            "schema_version": "provider-completion-external-trust-manifest/1.0",
            "document_type": "completion_telemetry_external_trust_manifest",
            "manifest_id": "PCETRUST-0123456789ABCDEF0123456789ABCDEF",
            "created_at_utc": "2026-09-04T00:00:01Z",
            "valid_from_utc": "2026-09-04T00:00:00Z",
            "expires_at_utc": "2026-09-06T00:00:00Z",
            "roles": [
                {
                    "role": role,
                    "key_id": self.key_ids[role],
                    "algorithm": "ed25519",
                    "public_key_b64": _public_key_b64(self.keys[role]),
                    "organization_commitment_sha256": _h("organization-" + role),
                    "not_before_utc": "2026-09-04T00:00:00Z",
                    "expires_at_utc": role_expires[role],
                    "revoked": False,
                }
                for role in (
                    "freeze_authority",
                    "task_custodian",
                    "ledger_witness",
                )
            ],
            "expected_manifest_hash_must_be_supplied_out_of_band": True,
            "document_sha256": "0" * 64,
            "signatures": [],
        }
        self._finalize_document(
            trust, ("freeze_authority", "task_custodian", "ledger_witness")
        )

        candidate = {
            "schema_version": "provider-completion-candidate-freeze-receipt/1.0",
            "document_type": "completion_telemetry_candidate_freeze",
            "receipt_id": "PCEFREEZE-0123456789ABCDEF0123456789ABCDEF",
            "frozen_at_utc": "2026-09-04T00:00:03Z",
            "repository": "cedRiC874/researchops-agent",
            "candidate_commit": "1" * 40,
            "candidate_tree": "2" * 40,
            "source_integrity_commitment_sha256": _h("source"),
            "runtime_registry_commitment_sha256": _h("registry"),
            "implementation_commitment_sha256": _h("implementation"),
            "runner_config_commitment_sha256": _h("runner"),
            "closure_evidence_contract_commitment_sha256": SUPPORTING_COMMITMENT,
            "dependency_lock_sha256": _h("dependency"),
            "prompt_or_task_adjustment_after_freeze_allowed": False,
            "online_execution_authorized": False,
            "document_sha256": "0" * 64,
            "signatures": [],
        }
        self._finalize_document(candidate, ("freeze_authority",))
        candidate_entry = self._ledger_entry(
            entry_type="candidate_frozen",
            sequence=7,
            previous_head=_h("prior-ledger-head"),
            occurred_at="2026-09-04T00:00:04Z",
            candidate_hash=candidate["document_sha256"],
            envelope_hash=None,
            grant_hash=None,
            consumption_hash=None,
        )

        source_classes = (
            "candidate_repository_evals_and_task_manifests",
            "candidate_repository_tests_and_synthetic_fixtures",
            "candidate_repository_docs_and_published_evidence_examples",
            "historical_provider_request_inputs_represented_by_project_supplied_digests",
            "manual_development_review_and_prompt_tuning_inputs_represented_by_project_supplied_digests",
            "public_article_and_demo_task_inputs_represented_by_project_supplied_digests",
        )
        inventory = [
            {
                "source_class": source_class,
                "source_snapshot_commitment_sha256": _h("snapshot-" + source_class),
                "task_digest_count": index + 1,
            }
            for index, source_class in enumerate(source_classes)
        ]
        inventory_hash = _domain_hash(
            SEEN_SET_DOMAIN,
            b"source_inventory",
            canonical_json_bytes(inventory),
        )
        seen = {
            "schema_version": "provider-completion-seen-case-exclusion-manifest/1.0",
            "document_type": "completion_telemetry_seen_task_exclusion_manifest",
            "manifest_id": "PCESEEN-0123456789ABCDEF0123456789ABCDEF",
            "created_at_utc": "2026-09-04T00:00:06Z",
            "candidate_freeze_receipt_sha256": candidate["document_sha256"],
            "source_inventory": inventory,
            "source_inventory_commitment_sha256": inventory_hash,
            "exclusion_digest_set_count": 21,
            "exclusion_digest_set_commitment_sha256": _h("seen-union"),
            "source_inventory_complete_attested": True,
            "software_proves_source_inventory_complete": False,
            "task_or_prompt_plaintext_included": False,
            "individual_task_digests_included": False,
            "document_sha256": "0" * 64,
            "signatures": [],
        }
        self._finalize_document(
            seen, ("freeze_authority", "task_custodian")
        )

        case_ids = [
            "PCECASE-0123456789ABCDEF0123456789ABCDEF",
            "PCECASE-FEDCBA9876543210FEDCBA9876543210",
        ]
        case_ids_hash = hashlib.sha256(canonical_json_bytes(case_ids)).hexdigest()
        if bad_case_ids_commitment:
            case_ids_hash = _h("wrong-case-id-order")
        denominator = {
            "schema_version": "provider-completion-runtime-denominator-plan/1.0",
            "provider_id": "deepseek",
            "api_surface": "responses",
            "transport_id": "openai_compatible_responses",
            "adapter_version": "deepseek-responses-adapter/1.0",
            "telemetry_schema_sha256": _h("telemetry-schema"),
            "mapping_schema_version": "provider-completion-mapping/2.0",
            "mapping_version": "deepseek-responses-probe-v3-successor-v1",
            "mapping_sha256": _h("mapping"),
            "case_ids": case_ids,
            "case_ids_sha256": case_ids_hash,
            "max_turns_per_case": 2,
            "total_model_request_cap": 4,
            "agents_sdk_retries": 0,
            "http_client_retries": 0,
            "denominator_algorithm": "transport-response-finalization-v1",
            "exact_response_count_preregistered": False,
        }
        denominator_hash = hashlib.sha256(
            canonical_json_bytes(denominator)
        ).hexdigest()
        case_set_hash = _domain_hash(
            CASE_SET_DOMAIN, canonical_json_bytes(sorted(case_ids))
        )
        case_order_hash = _domain_hash(
            CASE_ORDER_DOMAIN, canonical_json_bytes(case_ids)
        )
        envelope_id = "PCEPR-0123456789ABCDEF0123456789ABCDEF"
        task_bundle_hash = _h("secret-salted-unseen-bundle")
        campaign_digest = hashlib.sha256(
            CAMPAIGN_DOMAIN.encode("ascii")
            + b"\0"
            + envelope_id.encode("ascii")
            + b"\0"
            + bytes.fromhex(task_bundle_hash)
        ).hexdigest()
        campaign_id = "PCECAMP-" + campaign_digest[:32].upper()
        run_ids = [
            "PCERUN-"
            + hashlib.sha256(
                AUDIT_RUN_DOMAIN.encode("ascii")
                + b"\0"
                + envelope_id.encode("ascii")
                + b"\0"
                + case_id.encode("ascii")
            ).hexdigest()[:32].upper()
            for case_id in case_ids
        ]
        run_set_hash = _domain_hash(
            AUDIT_SET_DOMAIN, b"planned", canonical_json_bytes(run_ids)
        )
        budget = {
            "budget_policy_commitment_sha256": "0" * 64,
            "pricing_snapshot_date": "2026-09-04",
            "pricing_source_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            "pricing_source_snapshot_sha256": _h("pricing"),
            "pricing_retrieved_at_utc": "2026-09-04T00:00:11.5Z",
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
        }
        budget_body = dict(budget)
        budget_body.pop("budget_policy_commitment_sha256")
        budget["budget_policy_commitment_sha256"] = _domain_hash(
            RUNTIME_PLAN_DOMAIN,
            b"budget_policy",
            canonical_json_bytes(budget_body),
        )
        runtime_plan = {
            "schema_version": "provider-completion-external-runtime-plan/1.0",
            "external_plan_binding_sha256": "0" * 64,
            "denominator_plan_commitment_sha256": denominator_hash,
            "denominator_plan": denominator,
            "telemetry_interpretation": {
                "output_counter_comparability": "comparable",
                "output_counter_path": "output_tokens",
            },
            "campaign_topology": {
                "campaign_id": campaign_id,
                "case_handle_set_commitment_sha256": case_set_hash,
                "case_handle_order_commitment_sha256": case_order_hash,
                "audit_run_id_derivation": "sha256-domain-separated-envelope-id-and-case-handle-v1",
                "audit_run_set_commitment_sha256": run_set_hash,
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
            "budget_policy": budget,
        }
        plan_body = copy.deepcopy(runtime_plan)
        plan_body.pop("external_plan_binding_sha256")
        runtime_plan["external_plan_binding_sha256"] = _domain_hash(
            RUNTIME_PLAN_DOMAIN, canonical_json_bytes(plan_body)
        )
        execution_commit = (
            "3" * 40 if bad_candidate_projection else candidate["candidate_commit"]
        )
        envelope = {
            "schema_version": "provider-completion-external-preregistration/1.0",
            "document_type": "completion_telemetry_closure_preregistration",
            "envelope_id": envelope_id,
            "status": "frozen_not_authorized_not_run",
            "candidate_frozen_at_utc": candidate["frozen_at_utc"],
            "task_bundle_committed_at_utc": "2026-09-04T00:00:07Z",
            "envelope_frozen_at_utc": "2026-09-04T00:00:08.0000001Z",
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
                "repository": candidate["repository"],
                "candidate_freeze_receipt_sha256": candidate["document_sha256"],
                "candidate_freeze_ledger_entry_sha256": candidate_entry["entry_sha256"],
                "execution_commit": execution_commit,
                "execution_tree": candidate["candidate_tree"],
                "source_integrity_commitment_sha256": candidate[
                    "source_integrity_commitment_sha256"
                ],
                "first_live_evidence_commitment_sha256": _h("first-live"),
                "first_live_validation_status": "reviewed_success",
                "runtime_registry_commitment_sha256": candidate[
                    "runtime_registry_commitment_sha256"
                ],
                "runtime_registry_binding_allowed": True,
                "implementation_commitment_sha256": candidate[
                    "implementation_commitment_sha256"
                ],
                "runner_config_commitment_sha256": candidate[
                    "runner_config_commitment_sha256"
                ],
                "telemetry_schema_sha256": denominator["telemetry_schema_sha256"],
                "mapping_sha256": denominator["mapping_sha256"],
                "sanitizer_sha256": _h("sanitizer"),
                "closure_verifier_sha256": _h("closure-verifier"),
                "closure_evidence_contract_commitment_sha256": SUPPORTING_COMMITMENT,
                "dependency_lock_sha256": candidate["dependency_lock_sha256"],
                "provider_id": "deepseek",
                "model_id": "deepseek-v4-flash",
                "api_origin": "https://api.deepseek.com",
                "api_surface": "responses",
                "transport_id": "openai_compatible_responses",
                "adapter_version": "deepseek-responses-adapter/1.0",
            },
            "task_custody": {
                "task_bundle_commitment_sha256": task_bundle_hash,
                "task_bundle_commitment_algorithm": "secret-salted-canonical-bundle-sha256-v1",
                "task_bundle_schema_version": "completion-telemetry-unseen-case-bundle/1.0",
                "task_bundle_canonicalization": "utf8-sorted-keys-no-whitespace-reject-duplicate-and-nonfinite-v1",
                "minimum_secret_salt_bytes": 32,
                "planned_case_count": len(case_ids),
                "case_handle_set_commitment_sha256": case_set_hash,
                "case_order_commitment_sha256": case_order_hash,
                "execution_input_order_commitment_sha256": _h("execution-input-order"),
                "opaque_case_handles_only": True,
                "seen_task_exclusion_manifest_schema_version": "provider-completion-seen-case-exclusion-manifest/1.0",
                "seen_task_exclusion_manifest_sha256": seen["document_sha256"],
                "seen_task_exclusion_set_commitment_sha256": seen[
                    "exclusion_digest_set_commitment_sha256"
                ],
                "seen_task_exclusion_set_count": seen["exclusion_digest_set_count"],
                "seen_source_inventory_commitment_sha256": inventory_hash,
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
            "runtime_plan": runtime_plan,
            "closure_evidence": {
                "closure_evidence_contract_commitment_sha256": SUPPORTING_COMMITMENT,
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
                "trust_manifest_sha256": trust["document_sha256"],
                "freeze_authority_key_id": self.key_ids["freeze_authority"],
                "task_custodian_key_id": self.key_ids["task_custodian"],
                "ledger_witness_key_id": self.key_ids["ledger_witness"],
                "freeze_authority_organization_commitment_sha256": trust["roles"][0][
                    "organization_commitment_sha256"
                ],
                "task_custodian_organization_commitment_sha256": trust["roles"][1][
                    "organization_commitment_sha256"
                ],
                "ledger_witness_organization_commitment_sha256": trust["roles"][2][
                    "organization_commitment_sha256"
                ],
                "ledger_id": candidate_entry["ledger_id"],
                "ledger_base_sequence": candidate_entry["sequence"],
                "ledger_base_head_sha256": candidate_entry["resulting_head_sha256"],
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
            "document_sha256": "0" * 64,
            "signatures": [],
        }
        self._finalize_document(
            envelope, ("freeze_authority", "task_custodian")
        )
        prereg_entry = self._ledger_entry(
            entry_type="preregistration_frozen",
            sequence=8,
            previous_head=candidate_entry["resulting_head_sha256"],
            occurred_at="2026-09-04T00:00:09Z",
            candidate_hash=candidate["document_sha256"],
            envelope_hash=envelope["document_sha256"],
            grant_hash=None,
            consumption_hash=None,
        )
        grant = {
            "schema_version": "provider-completion-external-authorization-grant/1.0",
            "document_type": "completion_telemetry_closure_authorization_grant",
            "grant_id": "PCEGRANT-0123456789ABCDEF0123456789ABCDEF",
            "authorized_at_utc": "2026-09-04T00:00:12Z",
            "not_before_utc": "2026-09-04T00:00:13Z",
            "expires_at_utc": "2026-09-04T01:00:00Z",
            "explicit_user_authorization_id_sha256": _h("user-authorization-id"),
            "explicit_user_authorization_binding_sha256": "0" * 64,
            "preregistration_envelope_sha256": envelope["document_sha256"],
            "preregistration_freeze_entry_sha256": prereg_entry["entry_sha256"],
            "ledger_sequence": prereg_entry["sequence"],
            "ledger_head_sha256": prereg_entry["resulting_head_sha256"],
            "candidate_commit": envelope["execution_binding"]["execution_commit"],
            "candidate_tree": envelope["execution_binding"]["execution_tree"],
            "source_integrity_commitment_sha256": envelope["execution_binding"][
                "source_integrity_commitment_sha256"
            ],
            "runtime_registry_commitment_sha256": envelope["execution_binding"][
                "runtime_registry_commitment_sha256"
            ],
            "denominator_plan_commitment_sha256": denominator_hash,
            "external_plan_binding_sha256": runtime_plan[
                "external_plan_binding_sha256"
            ],
            "budget_policy_commitment_sha256": budget[
                "budget_policy_commitment_sha256"
            ],
            "pricing_snapshot_date": budget["pricing_snapshot_date"],
            "pricing_source_snapshot_sha256": budget[
                "pricing_source_snapshot_sha256"
            ],
            "pricing_retrieved_at_utc": budget["pricing_retrieved_at_utc"],
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
            "document_sha256": "0" * 64,
            "signatures": [],
        }
        grant_body = dict(grant)
        for omitted in (
            "explicit_user_authorization_binding_sha256",
            "document_sha256",
            "signatures",
        ):
            grant_body.pop(omitted)
        grant["explicit_user_authorization_binding_sha256"] = (
            _h("wrong-user-binding")
            if bad_user_binding
            else _domain_hash(
                USER_BINDING_DOMAIN, canonical_json_bytes(grant_body)
            )
        )
        self._finalize_document(
            grant, ("freeze_authority", "task_custodian")
        )
        consumption = {
            "schema_version": "provider-completion-external-consumption-receipt/1.0",
            "document_type": "completion_telemetry_closure_consumption_receipt",
            "receipt_id": "PCECONSUME-0123456789ABCDEF0123456789ABCDEF",
            "consumed_at_utc": "2026-09-04T00:00:14Z",
            "preregistration_envelope_sha256": envelope["document_sha256"],
            "preregistration_freeze_entry_sha256": prereg_entry["entry_sha256"],
            "authorization_grant_sha256": grant["document_sha256"],
            "explicit_user_authorization_id_sha256": grant[
                "explicit_user_authorization_id_sha256"
            ],
            "candidate_commit": grant["candidate_commit"],
            "denominator_plan_commitment_sha256": denominator_hash,
            "external_plan_binding_sha256": runtime_plan[
                "external_plan_binding_sha256"
            ],
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
            "document_sha256": "0" * 64,
        }
        self._finalize_document(consumption)
        consumed_entry = self._ledger_entry(
            entry_type="authorization_consumed",
            sequence=9,
            previous_head=prereg_entry["resulting_head_sha256"],
            occurred_at="2026-09-04T00:00:15Z",
            candidate_hash=candidate["document_sha256"],
            envelope_hash=envelope["document_sha256"],
            grant_hash=grant["document_sha256"],
            consumption_hash=consumption["document_sha256"],
        )

        def anchor(entry: dict, observed_at: str) -> dict:
            return {
                "entry_sha256": entry["entry_sha256"],
                "ledger_id": entry["ledger_id"],
                "entry_type": entry["entry_type"],
                "sequence": entry["sequence"],
                "previous_head_sha256": entry["previous_head_sha256"],
                "resulting_head_sha256": entry["resulting_head_sha256"],
                "entry_occurred_at_utc": entry["occurred_at_utc"],
                "observed_at_utc": observed_at,
                "observation_source_commitment_sha256": _h("observation-source"),
            }

        observation = {
            "schema_version": "provider-completion-external-observation-bundle/1.0",
            "mode": "pre_receipt",
            "trust_manifest_observation": {
                "document_sha256": trust["document_sha256"],
                "observed_at_utc": "2026-09-04T00:00:02Z",
                "observation_source_commitment_sha256": _h("observation-source"),
            },
            "preregistration_envelope_observation": {
                "document_sha256": envelope["document_sha256"],
                "observed_at_utc": "2026-09-04T00:00:08.0000002Z",
                "observation_source_commitment_sha256": _h("observation-source"),
            },
            "user_authorization_observation": {
                "authorization_id_sha256": grant[
                    "explicit_user_authorization_id_sha256"
                ],
                "authorization_binding_sha256": grant[
                    "explicit_user_authorization_binding_sha256"
                ],
                "observed_at_utc": "2026-09-04T00:00:11Z",
                "observation_source_commitment_sha256": _h("observation-source"),
            },
            "candidate_freeze_anchor": anchor(
                candidate_entry, "2026-09-04T00:00:05Z"
            ),
            "preregistration_freeze_anchor": anchor(
                prereg_entry, "2026-09-04T00:00:10Z"
            ),
            "authorization_consumed_anchor": anchor(
                consumed_entry, "2026-09-04T00:00:16Z"
            ),
            "manifest_observation": {
                "state": "present",
                "manifest_sha256": _h("closure-manifest"),
                "observed_at_utc": "2026-09-04T00:00:20Z",
                "observation_source_commitment_sha256": _h("observation-source"),
                "last_completed_artifact_write_stage": "manifest_written",
            },
            "closure_receipt_observation": None,
            "closure_evidence_anchor": None,
            "artifact_values_may_supply_expected_values": False,
        }
        self.documents = {
            "trust_manifest": trust,
            "candidate_freeze_receipt": candidate,
            "candidate_frozen_ledger_entry": candidate_entry,
            "seen_case_exclusion_manifest": seen,
            "preregistration_envelope": envelope,
            "preregistration_frozen_ledger_entry": prereg_entry,
            "authorization_grant": grant,
            "consumption_receipt": consumption,
            "authorization_consumed_ledger_entry": consumed_entry,
        }
        self.observation = observation

    @staticmethod
    def _bytes(value: dict) -> bytes:
        return canonical_json_bytes(value)

    def inputs(self) -> tuple[PreReceiptDocumentBytes, bytes]:
        return (
            PreReceiptDocumentBytes(
                **{
                    name: self._bytes(value)
                    for name, value in self.documents.items()
                }
            ),
            self._bytes(self.observation),
        )


class ExternalClosureDocumentVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = _schemas()

    def assert_error(self, expected: str, fixture: SyntheticPreReceipt) -> None:
        documents, observation = fixture.inputs()
        with self.assertRaises(ExternalClosurePrimitiveError) as caught:
            verify_pre_receipt_documents(
                documents,
                schemas=self.schemas,
                external_observation_bundle=observation,
            )
        self.assertEqual(caught.exception.code, expected)
        self.assertEqual(str(caught.exception), expected)

    def test_real_signature_fixture_verifies_to_frozen_hash_timestamp_summary(self) -> None:
        fixture = SyntheticPreReceipt()
        documents, observation = fixture.inputs()
        result = verify_pre_receipt_documents(
            documents,
            schemas=self.schemas,
            external_observation_bundle=observation,
        )
        self.assertIsInstance(result, VerifiedPreReceiptDocuments)
        self.assertEqual(
            result.preregistration_envelope_sha256,
            fixture.documents["preregistration_envelope"]["document_sha256"],
        )
        self.assertEqual(
            result.authorization_consumed_ledger_head_sha256,
            fixture.documents["authorization_consumed_ledger_entry"][
                "resulting_head_sha256"
            ],
        )
        for name in asdict(result):
            self.assertTrue(
                name.endswith("_sha256")
                or name.endswith("_at_utc")
                or name.endswith("_until_utc")
            )
            self.assertNotIn("case_ids", name)
            self.assertNotIn("payload", name)
        with self.assertRaises(FrozenInstanceError):
            result.grant_expires_at_utc = "2030-01-01T00:00:00Z"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            VerifiedPreReceiptDocuments()

    def test_schema_hash_and_signature_tampering_fail_closed(self) -> None:
        schema_fixture = SyntheticPreReceipt()
        schema_fixture.documents["candidate_freeze_receipt"]["extra"] = False
        self.assert_error("external_closure_document_schema_invalid", schema_fixture)

        hash_fixture = SyntheticPreReceipt()
        hash_fixture.documents["candidate_freeze_receipt"]["candidate_commit"] = "f" * 40
        self.assert_error("external_closure_document_hash_mismatch", hash_fixture)

        signature_fixture = SyntheticPreReceipt()
        signature = signature_fixture.documents["authorization_grant"]["signatures"][0]
        raw = bytearray(base64.b64decode(signature["signature_b64"]))
        raw[0] ^= 1
        signature["signature_b64"] = base64.b64encode(bytes(raw)).decode("ascii")
        self.assert_error("external_closure_signature_invalid", signature_fixture)

        key_projection_fixture = SyntheticPreReceipt()
        candidate = key_projection_fixture.documents["candidate_freeze_receipt"]
        signature = candidate["signatures"][0]
        custodian_id = key_projection_fixture.key_ids["task_custodian"]
        signature["key_id"] = custodian_id
        message = build_signature_message(
            document_type=candidate["document_type"],
            role="freeze_authority",
            key_id=custodian_id,
            signed_sha256=candidate["document_sha256"],
        )
        signature["signature_b64"] = base64.b64encode(
            key_projection_fixture.keys["task_custodian"].sign(message)
        ).decode("ascii")
        self.assert_error(
            "external_closure_signature_projection_invalid",
            key_projection_fixture,
        )

    def test_caller_schema_cannot_trigger_transitive_remote_retrieval(self) -> None:
        fixture = SyntheticPreReceipt()
        documents, observation = fixture.inputs()
        for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
            schemas = dict(self.schemas)
            schemas["external_trust_manifest_v1.schema.json"] = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                keyword: "https://attacker.invalid/schema.json",
            }
            with self.subTest(keyword=keyword), patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("schema retrieval attempted"),
            ) as retrieve:
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "external_closure_schema_mapping_invalid",
                ):
                    verify_pre_receipt_documents(
                        documents,
                        schemas=schemas,
                        external_observation_bundle=observation,
                    )
                retrieve.assert_not_called()

    def test_key_derivation_role_separation_and_key_validity_are_enforced(self) -> None:
        self.assert_error(
            "external_closure_trust_role_separation_invalid",
            SyntheticPreReceipt(same_custodian_key=True),
        )
        self.assert_error(
            "external_closure_signature_time_invalid",
            SyntheticPreReceipt(witness_expires_early=True),
        )
        self.assert_error(
            "external_closure_trust_invalid",
            SyntheticPreReceipt(bad_key_derivation=True),
        )

        fixture = SyntheticPreReceipt()
        fixture.documents["trust_manifest"]["roles"][0]["key_id"] = (
            "PCEKEY-FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
        )
        self.assert_error("external_closure_document_hash_mismatch", fixture)

    def test_internal_commitments_and_cross_document_bindings_are_recomputed(self) -> None:
        self.assert_error(
            "external_closure_plan_commitment_invalid",
            SyntheticPreReceipt(bad_case_ids_commitment=True),
        )
        self.assert_error(
            "external_closure_candidate_projection_invalid",
            SyntheticPreReceipt(bad_candidate_projection=True),
        )
        self.assert_error(
            "external_closure_user_authorization_projection_invalid",
            SyntheticPreReceipt(bad_user_binding=True),
        )

    def test_external_anchor_and_timeline_are_not_self_sourced_from_documents(self) -> None:
        anchor_fixture = SyntheticPreReceipt()
        anchor_fixture.observation["authorization_consumed_anchor"][
            "entry_sha256"
        ] = _h("unobserved-entry")
        self.assert_error("external_closure_observation_mismatch", anchor_fixture)

        hash_fixture = SyntheticPreReceipt()
        hash_fixture.observation["trust_manifest_observation"][
            "document_sha256"
        ] = _h("different-trust")
        self.assert_error("external_closure_observation_mismatch", hash_fixture)

        timeline_fixture = SyntheticPreReceipt()
        timeline_fixture.observation["user_authorization_observation"][
            "observed_at_utc"
        ] = "2026-09-04T00:00:12.0000001Z"
        self.assert_error("external_closure_timeline_invalid", timeline_fixture)

        precise_fixture = SyntheticPreReceipt()
        precise_fixture.observation["preregistration_envelope_observation"][
            "observed_at_utc"
        ] = "2026-09-04T00:00:08.00000009Z"
        self.assert_error("external_closure_timeline_invalid", precise_fixture)

    def test_ledger_chain_and_document_projection_are_exact(self) -> None:
        fixture = SyntheticPreReceipt()
        entry = fixture.documents["authorization_consumed_ledger_entry"]
        entry["previous_head_sha256"] = _h("coordinated-wrong-head")
        entry["entry_sha256"] = compute_ledger_entry_sha256(entry)
        entry["resulting_head_sha256"] = compute_ledger_resulting_head(
            entry["previous_head_sha256"], entry["entry_sha256"]
        )
        entry["signatures"] = fixture._signatures(entry, ("ledger_witness",))
        anchor = fixture.observation["authorization_consumed_anchor"]
        for target, source in (
            ("entry_sha256", "entry_sha256"),
            ("previous_head_sha256", "previous_head_sha256"),
            ("resulting_head_sha256", "resulting_head_sha256"),
        ):
            anchor[target] = entry[source]
        self.assert_error("external_closure_ledger_projection_invalid", fixture)

        projection_fixture = SyntheticPreReceipt()
        candidate_entry = projection_fixture.documents[
            "candidate_frozen_ledger_entry"
        ]
        candidate_entry["candidate_freeze_receipt_sha256"] = _h("wrong-candidate")
        candidate_entry["entry_sha256"] = compute_ledger_entry_sha256(candidate_entry)
        candidate_entry["resulting_head_sha256"] = compute_ledger_resulting_head(
            candidate_entry["previous_head_sha256"], candidate_entry["entry_sha256"]
        )
        candidate_entry["signatures"] = projection_fixture._signatures(
            candidate_entry, ("ledger_witness",)
        )
        # Keep the independently supplied anchor coordinated, but do not rewrite
        # the signed candidate receipt that the ledger entry must reference.
        candidate_anchor = projection_fixture.observation["candidate_freeze_anchor"]
        candidate_anchor["entry_sha256"] = candidate_entry["entry_sha256"]
        candidate_anchor["resulting_head_sha256"] = candidate_entry[
            "resulting_head_sha256"
        ]
        # The envelope now becomes stale first; that is still a required exact
        # cross-document failure and cannot be repaired by the ledger signature.
        self.assert_error("external_closure_candidate_projection_invalid", projection_fixture)

    def test_api_and_module_have_no_io_clock_runtime_or_provider_capability(self) -> None:
        self.assertEqual(
            tuple(inspect.signature(verify_pre_receipt_documents).parameters),
            ("documents", "schemas", "external_observation_bundle"),
        )
        module_path = (
            ROOT / "src" / "researchops_external_closure" / "documents.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        forbidden_calls = {"open", "print", "input", "exec", "eval", "compile"}
        forbidden_attributes = {
            "connect",
            "getenv",
            "now",
            "open",
            "read",
            "read_bytes",
            "read_text",
            "request",
            "send",
            "utcnow",
            "write",
        }
        calls: set[str] = set()
        attributes: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                attributes.add(node.func.attr)
        forbidden_import_prefixes = (
            "os",
            "pathlib",
            "socket",
            "subprocess",
            "httpx",
            "openai",
            "anthropic",
            "requests",
            "sqlite3",
            "researchops.",
            "researchops_completion_telemetry",
        )
        self.assertFalse(
            any(
                name == prefix.rstrip(".") or name.startswith(prefix)
                for name in imported
                for prefix in forbidden_import_prefixes
            ),
            imported,
        )
        self.assertFalse(calls & forbidden_calls)
        self.assertFalse(attributes & forbidden_attributes)


if __name__ == "__main__":
    unittest.main()
