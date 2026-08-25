from __future__ import annotations

import base64
from contextlib import redirect_stdout
from copy import deepcopy
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import eval_v2_private_custodian as private


REPO_ROOT = Path(__file__).resolve().parents[1]
KIT_ROOT = REPO_ROOT / "evals" / "v2" / "private_holdout_kit"


def _sha(label: str) -> str:
    import hashlib

    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def _signature(
    document: dict[str, object],
    *,
    role: str,
    key_id: str,
    key: Ed25519PrivateKey,
) -> dict[str, str]:
    digest = private.document_sha256(document)
    signed = key.sign(private.signature_message(str(document["document_type"]), digest))
    return {
        "role": role,
        "key_id": key_id,
        "algorithm": "ed25519",
        "signed_sha256": digest,
        "signature_b64": base64.b64encode(signed).decode("ascii"),
    }


def _finalize(
    document: dict[str, object],
    signers: list[tuple[str, str, Ed25519PrivateKey]],
) -> dict[str, object]:
    value = deepcopy(document)
    value["document_sha256"] = private.document_sha256(value)
    value["signatures"] = [
        _signature(value, role=role, key_id=key_id, key=key)
        for role, key_id, key in signers
    ]
    return value


def _finalize_entry(
    entry: dict[str, object],
    *,
    ledger_id: str,
    signers: list[tuple[str, str, Ed25519PrivateKey]],
) -> dict[str, object]:
    value = deepcopy(entry)
    digest = private.ledger_entry_sha256(value, ledger_id)
    value["entry_sha256"] = digest
    value["signatures"] = []
    for role, key_id, key in signers:
        signed = key.sign(private.ledger_signature_message(str(value["event_type"]), digest))
        value["signatures"].append(
            {
                "role": role,
                "key_id": key_id,
                "algorithm": "ed25519",
                "signed_sha256": digest,
                "signature_b64": base64.b64encode(signed).decode("ascii"),
            }
        )
    return value


def _metric_record(metric_id: str, *, eligible: int, evaluated: int) -> dict[str, object]:
    suppressed = evaluated < 5
    numerator = (
        None
        if suppressed
        else (0 if private.METRIC_DIRECTIONS[metric_id] == "lower_is_better" else max(0, evaluated - 5))
    )
    rate = None if numerator is None else private._expected_rate(numerator, evaluated)
    lower: str | None = None
    upper: str | None = None
    if numerator is not None:
        lower, upper = private._wilson_95_bounds(numerator, evaluated)
    return {
        "metric_id": metric_id,
        "direction": private.METRIC_DIRECTIONS[metric_id],
        "eligible_count": eligible,
        "evaluated_count": evaluated,
        "numerator": numerator,
        "rate": rate,
        "coverage_rate": private._expected_rate(evaluated, eligible),
        "suppressed": suppressed,
        "ci_method": "suppressed" if suppressed else "wilson_95",
        "ci_lower": lower,
        "ci_upper": upper,
    }


def _allocated_counts(counts: list[int], completed: int) -> list[int]:
    remaining = completed
    result: list[int] = []
    for count in counts:
        observed = min(count, remaining)
        result.append(observed)
        remaining -= observed
    if remaining != 0:
        raise AssertionError("synthetic allocation is incomplete")
    return result


class SyntheticRelease:
    authority_id = "KEY-AAAAAAAAAAAAAAAA"
    custodian_id = "KEY-BBBBBBBBBBBBBBBB"
    ledger_id = "LEDGER-9999999999999999"

    def __init__(
        self,
        terminal_status: str = "complete",
        *,
        synthetic: bool = True,
        same_role_key: bool = False,
        authorization_expires_at: str = "2030-01-31T00:00:00Z",
    ) -> None:
        self.authority = Ed25519PrivateKey.generate()
        self.custodian = self.authority if same_role_key else Ed25519PrivateKey.generate()
        self.synthetic = synthetic
        self.authorization_expires_at = authorization_expires_at
        self.providers = [
            {
                "provider_id": "provider_alpha",
                "model_id": "model-alpha-v1",
                "transport_id": "transport_alpha",
                "config_sha256": _sha("provider-alpha-config"),
            },
            {
                "provider_id": "provider_beta",
                "model_id": "model-beta-v1",
                "transport_id": "transport_beta",
                "config_sha256": _sha("provider-beta-config"),
            },
        ]
        self.documents = self._build(terminal_status)

    def _build(self, terminal_status: str) -> dict[str, dict[str, object]]:
        sign_authority = [("freeze_authority", self.authority_id, self.authority)]
        sign_custodian = [("custodian", self.custodian_id, self.custodian)]
        sign_both = [*sign_authority, *sign_custodian]
        trust: dict[str, object] = {
            "schema_version": "1.1",
            "document_type": "trust_manifest",
            "synthetic": self.synthetic,
            "manifest_id": "TRUST-1111111111111111",
            "created_at_utc": "2029-12-01T00:00:00Z",
            "keys": [
                {
                    "role": "freeze_authority",
                    "key_id": self.authority_id,
                    "algorithm": "ed25519",
                    "public_key_b64": _public_key_b64(self.authority),
                    "valid_from_utc": "2029-12-01T00:00:00Z",
                    "valid_until_utc": "2031-01-01T00:00:00Z",
                    "revoked": False,
                    "organization_commitment_sha256": _sha("authority-organization"),
                },
                {
                    "role": "custodian",
                    "key_id": self.custodian_id,
                    "algorithm": "ed25519",
                    "public_key_b64": _public_key_b64(self.custodian),
                    "valid_from_utc": "2029-12-01T00:00:00Z",
                    "valid_until_utc": "2031-01-01T00:00:00Z",
                    "revoked": False,
                    "organization_commitment_sha256": _sha("custodian-organization"),
                },
            ],
        }
        trust["manifest_sha256"] = private.trust_manifest_sha256(trust)
        protocol_hash = private._hash_file(KIT_ROOT / "protocol.json")
        schema_hash = private.schema_bundle_sha256(KIT_ROOT / "schemas")
        verifier_hash = private._hash_file(REPO_ROOT / "scripts" / "eval_v2_private_custodian.py")
        budget_policy = {
            "currency": "USD",
            "pricing_commitment_sha256": _sha("synthetic-pricing"),
            "maximum_model_calls": 1200,
            "maximum_input_tokens": 2_000_000,
            "maximum_output_tokens": 500_000,
            "maximum_cost_decimal": "100.000000",
            "complete_usage_required_for_complete_status": True,
        }
        retention_policy = {
            "pilot_participant_derived_data_allowed": False,
            "direct_or_quasi_identifier_data_allowed": False,
            "maximum_plaintext_retention_hours": 24,
            "provider_log_attestation_required": True,
            "proxy_log_attestation_required": True,
            "backup_attestation_required": True,
        }
        freeze_body: dict[str, object] = {
            "schema_version": "1.1",
            "document_type": "freeze_request",
            "synthetic": self.synthetic,
            "request_id": "PFR-2222222222222222",
            "campaign_id": "EVALV2-PRIVATE-3333333333333333",
            "created_at_utc": "2030-01-01T00:00:00Z",
            "expires_at_utc": "2030-02-01T00:00:00Z",
            "campaign_status": "frozen",
            "full_campaign_frozen": True,
            "candidate_commitment_algorithm": "researchops-private-candidate-v1",
            "candidate_commitment_sha256": private.ZERO_SHA256,
            "lineage_predecessor_commitment_sha256": _sha("synthetic-predecessor"),
            "component_hashes": {
                "source_sha256": _sha("source"),
                "prompt_sha256": _sha("prompt"),
                "tool_schema_sha256": _sha("tool"),
                "scorer_sha256": _sha("scorer"),
                "dependency_lock_sha256": _sha("dependencies"),
                "dataset_manifest_sha256": _sha("datasets"),
                "split_manifest_sha256": _sha("split"),
                "reporter_sha256": _sha("reporter"),
                "sanitizer_sha256": _sha("sanitizer"),
                "completion_telemetry_sha256": _sha("completion-telemetry"),
                "private_protocol_sha256": protocol_hash,
                "private_schema_bundle_sha256": schema_hash,
                "private_verifier_sha256": verifier_hash,
            },
            "trust_manifest_sha256": trust["manifest_sha256"],
            "protocol_sha256": protocol_hash,
            "provider_plan": self.providers,
            "private_case_count_target": 50,
            "repetitions_per_provider": 3,
            "private_campaign_limit": 1,
            "randomized_case_order": True,
            "precommitted_order_required": True,
            "metric_contract_version": "eval-v2-required-metrics-v1",
            "small_cell_threshold": 5,
            "ledger_id": self.ledger_id,
            "ledger_base_sequence": 0,
            "ledger_base_head_sha256": private.ZERO_SHA256,
            "budget_policy": budget_policy,
            "retention_policy": retention_policy,
            "private_access_authorized": False,
            "model_quality_claim_allowed": False,
        }
        freeze_body["candidate_commitment_sha256"] = (
            private.private_candidate_commitment_sha256(freeze_body)
        )
        freeze = _finalize(freeze_body, sign_authority)
        order_commitments = [
            {
                "provider_id": provider["provider_id"],
                "config_sha256": provider["config_sha256"],
                "repetition_index": repetition,
                "order_sha256": _sha(f"{provider['provider_id']}-order-{repetition}"),
            }
            for provider in self.providers
            for repetition in (1, 2, 3)
        ]
        metric_counts = {name: 50 for name in private.METRIC_DIRECTIONS}
        cell_commitments = [
            {"cell_type": "dataset", "cell_ordinal": 1, "eligible_case_count": 20},
            {"cell_type": "dataset", "cell_ordinal": 2, "eligible_case_count": 15},
            {"cell_type": "dataset", "cell_ordinal": 3, "eligible_case_count": 15},
            {"cell_type": "scenario", "cell_ordinal": 1, "eligible_case_count": 25},
            {"cell_type": "scenario", "cell_ordinal": 2, "eligible_case_count": 25},
        ]
        statement = _finalize(
            {
                "schema_version": "1.1",
                "document_type": "commitment_statement",
                "synthetic": self.synthetic,
                "statement_id": "PCS-4444444444444444",
                "created_at_utc": "2030-01-02T00:00:00Z",
                "freeze_request_sha256": freeze["document_sha256"],
                "candidate_commitment_sha256": freeze["candidate_commitment_sha256"],
                "corpus_commitment_algorithm": "secret-salted-canonical-bundle-sha256-v1",
                "corpus_canonicalization_version": "eval-v2-private-corpus-v1",
                "minimum_salt_bytes": 32,
                "salted_corpus_commitment_sha256": _sha("secret-salted-synthetic-corpus"),
                "private_case_count": 50,
                "dataset_count": 3,
                "non_synthetic_dataset_count": 3,
                "metric_eligible_case_counts": metric_counts,
                "cell_commitments": cell_commitments,
                "provider_order_commitments": order_commitments,
                "goldens_frozen_before_results": True,
                "qualified_reviewer_count": 2,
                "reviewer_conflicts_declared": True,
                "statistical_crosscheck": "R",
                "pilot_participant_derived_data_present": False,
                "direct_or_quasi_identifier_data_present": False,
                "private_content_included": False,
                "task_ids_included": False,
                "locator_included": False,
            },
            sign_custodian,
        )
        run_commitment = _sha("synthetic-run-commitment")
        authorization = _finalize(
            {
                "schema_version": "1.1",
                "document_type": "authorization_grant",
                "synthetic": self.synthetic,
                "authorization_id": "PAG-5555555555555555",
                "authorized_at_utc": "2030-01-02T12:00:00Z",
                "not_before_utc": "2030-01-03T00:00:00Z",
                "expires_at_utc": self.authorization_expires_at,
                "freeze_request_sha256": freeze["document_sha256"],
                "commitment_statement_sha256": statement["document_sha256"],
                "candidate_commitment_sha256": freeze["candidate_commitment_sha256"],
                "salted_corpus_commitment_sha256": statement["salted_corpus_commitment_sha256"],
                "authorization_nonce_sha256": _sha("synthetic-authorization-nonce"),
                "run_commitment_sha256": run_commitment,
                "provider_plan": self.providers,
                "private_case_count": 50,
                "repetitions_per_provider": 3,
                "single_use": True,
                "maximum_campaigns": 1,
                "ledger_id": self.ledger_id,
                "ledger_base_sequence": 0,
                "ledger_base_head_sha256": private.ZERO_SHA256,
                "access_reservation_required": True,
                "small_cell_threshold": 5,
                "budget_policy": budget_policy,
                "retention_policy": retention_policy,
                "private_access_scope": "custodian_environment_only",
                "private_content_transfer_to_project": False,
                "resume_policy": "dual_signed_same_run_and_identical_commitments_only",
                "maximum_resume_count": 1,
                "status": "authorized",
                "private_access_authorized": True,
                "model_quality_claim_allowed": False,
            },
            sign_both,
        )
        common_entry = {
            "synthetic": self.synthetic,
            "freeze_request_sha256": freeze["document_sha256"],
            "candidate_commitment_sha256": freeze["candidate_commitment_sha256"],
            "salted_corpus_commitment_sha256": statement["salted_corpus_commitment_sha256"],
            "authorization_grant_sha256": authorization["document_sha256"],
            "authorization_nonce_sha256": authorization["authorization_nonce_sha256"],
            "run_commitment_sha256": run_commitment,
            "provider_plan_sha256": private.provider_plan_sha256(self.providers),
            "budget_policy_sha256": private._sha256_value(
                budget_policy, b"researchops-private-budget-policy-v1\x00"
            ),
            "retention_policy_sha256": private._sha256_value(
                retention_policy, b"researchops-private-retention-policy-v1\x00"
            ),
        }
        reservation = _finalize_entry(
            {
                "event_type": "access_reserved",
                "sequence": 1,
                "previous_entry_sha256": private.ZERO_SHA256,
                "event_at_utc": "2030-01-03T00:00:00Z",
                **common_entry,
            },
            ledger_id=self.ledger_id,
            signers=sign_custodian,
        )
        completed = 50 if terminal_status == "complete" else 25
        calls = 60 if terminal_status == "complete" else 30
        provider_results: list[dict[str, object]] = []
        aggregate_cells: list[dict[str, object]] = []
        for provider in self.providers:
            repetitions: list[dict[str, object]] = []
            for repetition in (1, 2, 3):
                usage = {
                    "model_calls": calls,
                    "input_tokens": calls * 20,
                    "output_tokens": calls * 5,
                    "cost_decimal": "1.000000" if calls == 60 else "0.500000",
                    "currency": "USD",
                    "pricing_commitment_sha256": budget_policy["pricing_commitment_sha256"],
                    "coverage_numerator": 50,
                    "coverage_denominator": 50,
                    "coverage_rate": "1.000000",
                    "coverage_status": "complete",
                }
                repetitions.append(
                    {
                        "repetition_index": repetition,
                        "expected_case_count": 50,
                        "completed_case_count": completed,
                        "metrics": [
                            _metric_record(name, eligible=50, evaluated=completed)
                            for name in sorted(private.METRIC_DIRECTIONS)
                        ],
                        "approval_bypass_count": 0,
                        "unexpected_tool_error_count": 0,
                        "usage": usage,
                        "latency_p50_ms": 1000,
                        "latency_p95_ms": 2000,
                        "latency_is_production_sla": False,
                    }
                )
                for cell_type, counts in (("dataset", [20, 15, 15]), ("scenario", [25, 25])):
                    evaluated_counts = _allocated_counts(counts, completed)
                    remaining_failures = min(5, completed)
                    for ordinal, (eligible, evaluated) in enumerate(
                        zip(counts, evaluated_counts, strict=True), start=1
                    ):
                        suppressed = evaluated < 5
                        failures = min(remaining_failures, evaluated)
                        remaining_failures -= failures
                        numerator = None if suppressed else evaluated - failures
                        aggregate_cells.append(
                            {
                                "provider_id": provider["provider_id"],
                                "config_sha256": provider["config_sha256"],
                                "repetition_index": repetition,
                                "cell_type": cell_type,
                                "cell_ordinal": ordinal,
                                "eligible_count": eligible,
                                "evaluated_count": evaluated,
                                "numerator": numerator,
                                "rate": None if numerator is None else private._expected_rate(numerator, evaluated),
                                "suppressed": suppressed,
                            }
                        )
            stable = max(0, completed - 5)
            all_pass = max(0, completed - 10)
            provider_results.append(
                {
                    **provider,
                    "repetitions": repetitions,
                    "common_completed_case_count": completed,
                    "stable_case_count": stable,
                    "stability_rate": private._expected_rate(stable, completed),
                    "all_repetitions_pass_count": all_pass,
                    "all_repetitions_pass_rate": private._expected_rate(all_pass, completed),
                }
            )
        aggregate_cells.sort(
            key=lambda item: (
                item["provider_id"], item["config_sha256"], item["repetition_index"],
                item["cell_type"], item["cell_ordinal"],
            )
        )
        total_calls = calls * len(self.providers) * 3
        total_cost = Decimal("1.000000" if calls == 60 else "0.500000") * 6
        result = _finalize(
            {
                "schema_version": "1.1",
                "document_type": "aggregate_results",
                "synthetic": self.synthetic,
                "result_id": "PAR-6666666666666666",
                "completed_at_utc": "2030-01-04T02:00:00Z",
                "terminal_status": terminal_status,
                "freeze_request_sha256": freeze["document_sha256"],
                "commitment_statement_sha256": statement["document_sha256"],
                "authorization_grant_sha256": authorization["document_sha256"],
                "candidate_commitment_sha256": freeze["candidate_commitment_sha256"],
                "salted_corpus_commitment_sha256": statement["salted_corpus_commitment_sha256"],
                "run_commitment_sha256": run_commitment,
                "access_reservation_entry_sha256": reservation["entry_sha256"],
                "expected_private_case_count": 50,
                "expected_total_case_executions": 300,
                "completed_total_case_executions": completed * 6,
                "budget_actual": {
                    "model_calls": total_calls,
                    "input_tokens": total_calls * 20,
                    "output_tokens": total_calls * 5,
                    "cost_decimal": str(total_cost.quantize(Decimal("0.000001"))),
                    "currency": "USD",
                    "pricing_commitment_sha256": budget_policy["pricing_commitment_sha256"],
                    "coverage_numerator": 300,
                    "coverage_denominator": 300,
                    "coverage_rate": "1.000000",
                    "coverage_status": "complete",
                },
                "provider_results": provider_results,
                "aggregate_cells": aggregate_cells,
                "small_cell_threshold": 5,
                "private_content_included": False,
                "task_ids_included": False,
                "task_order_included": False,
                "locators_included": False,
                "per_case_results_included": False,
                "raw_provider_content_included": False,
                "performance_claim_allowed": False,
                "model_quality_claim_allowed": False,
                "unknown_distribution_generalization_claim_allowed": False,
            },
            sign_custodian,
        )
        terminal = _finalize_entry(
            {
                "event_type": "terminal",
                "sequence": 2,
                "previous_entry_sha256": reservation["entry_sha256"],
                "event_at_utc": "2030-01-04T02:00:00Z",
                **common_entry,
                "reservation_entry_sha256": reservation["entry_sha256"],
                "terminal_status": terminal_status,
                "aggregate_results_sha256": result["document_sha256"],
                "completed_total_case_executions": result["completed_total_case_executions"],
            },
            ledger_id=self.ledger_id,
            signers=sign_custodian,
        )
        ledger = _finalize(
            {
                "schema_version": "1.1",
                "document_type": "signed_ledger",
                "synthetic": self.synthetic,
                "ledger_id": self.ledger_id,
                "trust_manifest_sha256": trust["manifest_sha256"],
                "base_sequence": 0,
                "base_head_sha256": private.ZERO_SHA256,
                "entries": [reservation, terminal],
                "head_sequence": 2,
                "ledger_head_sha256": terminal["entry_sha256"],
                "signed_at_utc": "2030-01-04T02:01:00Z",
            },
            sign_custodian,
        )
        receipt = _finalize(
            {
                "schema_version": "1.1",
                "document_type": "consumption_receipt",
                "synthetic": self.synthetic,
                "receipt_id": "PCR-7777777777777777",
                "run_id": "PRUN-8888888888888888",
                "access_reserved_at_utc": reservation["event_at_utc"],
                "completed_at_utc": result["completed_at_utc"],
                "receipt_created_at_utc": "2030-01-04T02:02:00Z",
                "terminal_status": terminal_status,
                "authorization_grant_sha256": authorization["document_sha256"],
                "freeze_request_sha256": freeze["document_sha256"],
                "commitment_statement_sha256": statement["document_sha256"],
                "aggregate_results_sha256": result["document_sha256"],
                "signed_ledger_sha256": ledger["document_sha256"],
                "candidate_commitment_sha256": freeze["candidate_commitment_sha256"],
                "salted_corpus_commitment_sha256": statement["salted_corpus_commitment_sha256"],
                "run_commitment_sha256": run_commitment,
                "ledger_id": self.ledger_id,
                "ledger_base_sequence": 0,
                "ledger_base_head_sha256": private.ZERO_SHA256,
                "ledger_final_sequence": 2,
                "ledger_final_head_sha256": terminal["entry_sha256"],
                "access_reservation_entry_sha256": reservation["entry_sha256"],
                "terminal_entry_sha256": terminal["entry_sha256"],
                "authorization_consumed": True,
                "submission_ordinal": 1,
                "rerun_performed": False,
                "resume_count": 0,
                "resume_commitments_unchanged": True,
                "ambiguous_inflight_replayed": False,
                "private_content_transferred_to_project": False,
                "pilot_participant_derived_data_present": False,
                "direct_or_quasi_identifier_data_present": False,
                "human_subject_data_classification": "none",
                "private_plaintext_disposition": "deleted",
                "private_plaintext_deleted_at_utc": "2030-01-04T02:00:30Z",
                "provider_log_attestation_status": "synthetic_not_applicable",
                "proxy_log_attestation_status": "synthetic_not_applicable",
                "backup_attestation_status": "synthetic_not_applicable",
                "retention_gate_status": "synthetic_not_applicable",
                "result_release_policy": "signed_sanitized_aggregate_only",
                "model_quality_claim_allowed": False,
            },
            sign_custodian,
        )
        release_status = {
            "complete": "verified_aggregate_only",
            "stopped": "stopped_aggregate_only",
            "aborted": "aborted_aggregate_only",
        }[terminal_status]
        manifest = _finalize(
            {
                "schema_version": "1.1",
                "document_type": "release_manifest",
                "synthetic": self.synthetic,
                "release_id": "PRL-CCCCCCCCCCCCCCCC",
                "released_at_utc": "2030-01-04T02:03:00Z",
                "trust_manifest_sha256": trust["manifest_sha256"],
                "freeze_request_sha256": freeze["document_sha256"],
                "commitment_statement_sha256": statement["document_sha256"],
                "authorization_grant_sha256": authorization["document_sha256"],
                "aggregate_results_sha256": result["document_sha256"],
                "consumption_receipt_sha256": receipt["document_sha256"],
                "signed_ledger_sha256": ledger["document_sha256"],
                "anchored_trust_manifest_sha256": trust["manifest_sha256"],
                "anchored_freeze_request_sha256": freeze["document_sha256"],
                "anchored_candidate_commitment_sha256": freeze["candidate_commitment_sha256"],
                "anchored_ledger_base_head_sha256": private.ZERO_SHA256,
                "anchored_access_reservation_entry_sha256": reservation["entry_sha256"],
                "anchored_ledger_final_head_sha256": terminal["entry_sha256"],
                "anchored_ledger_final_sequence": 2,
                "verification_scope": "integrity_and_supplied_external_anchors_only",
                "release_status": release_status,
                "retention_gate_status": "synthetic_not_applicable",
                "private_content_included": False,
                "task_ids_included": False,
                "locators_included": False,
                "per_case_results_included": False,
                "raw_provider_content_included": False,
                "private_holdout_claim_allowed": False,
                "model_quality_claim_allowed": False,
                "unknown_distribution_generalization_claim_allowed": False,
                "production_sla_claim_allowed": False,
            },
            sign_custodian,
        )
        return {
            "trust_manifest": trust,
            "freeze_request": freeze,
            "commitment_statement": statement,
            "authorization_grant": authorization,
            "aggregate_results": result,
            "consumption_receipt": receipt,
            "signed_ledger": ledger,
            "release_manifest": manifest,
        }

    @property
    def verify_kwargs(self) -> dict[str, object]:
        freeze = self.documents["freeze_request"]
        ledger = self.documents["signed_ledger"]
        reservation = ledger["entries"][0]
        return {
            "expected_trust_manifest_sha256": self.documents["trust_manifest"]["manifest_sha256"],
            "expected_freeze_request_sha256": freeze["document_sha256"],
            "expected_candidate_commitment_sha256": freeze["candidate_commitment_sha256"],
            "expected_ledger_base_sequence": ledger["base_sequence"],
            "expected_ledger_base_head_sha256": ledger["base_head_sha256"],
            "expected_access_reservation_entry_sha256": reservation["entry_sha256"],
            "expected_ledger_final_sequence": ledger["head_sequence"],
            "expected_ledger_final_head_sha256": ledger["ledger_head_sha256"],
        }

    def write(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=False)
        for name, filename in private.RELEASE_FILES.items():
            (root / filename).write_text(
                json.dumps(self.documents[name], indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )


class PrivateCustodianKitTests(unittest.TestCase):
    def _verify_fixture(self, fixture: SyntheticRelease) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            fixture.write(release)
            return private.verify_release(
                project_root=REPO_ROOT,
                release_dir=release,
                **fixture.verify_kwargs,
            )

    def test_kit_is_valid_but_current_campaign_is_not_authorized(self) -> None:
        result = private.verify_kit(REPO_ROOT)
        status = private.readiness_status(REPO_ROOT)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["schema_count"], 8)
        self.assertFalse(result["current_private_request_allowed"])
        self.assertFalse(result["current_private_access_authorized"])
        self.assertFalse(result["repository_private_content_scan_performed"])
        self.assertFalse(result["repository_private_content_absence_claimed"])
        self.assertEqual(status["campaign_status"], "design_only")
        self.assertFalse(status["private_request_allowed"])
        self.assertIn("private_cases_below_target", status["request_readiness_gaps"])
        self.assertIn("provider_count_below_minimum", status["request_readiness_gaps"])
        self.assertEqual(result["network_calls"], 0)

    def test_valid_complete_and_stopped_synthetic_releases(self) -> None:
        for terminal_status in ("complete", "stopped"):
            result = self._verify_fixture(SyntheticRelease(terminal_status))
            self.assertEqual(result["status"], "valid")
            self.assertTrue(result["synthetic_conformance_only"])
            self.assertTrue(result["external_anchors_matched"])
            self.assertTrue(result["single_use_within_externally_anchored_ledger_scope"])
            self.assertTrue(result["aggregate_arithmetic_verified"])
            self.assertTrue(result["budget_gate_verified"])
            self.assertFalse(result["private_holdout_claim_allowed"])

    def test_non_synthetic_release_fails_closed(self) -> None:
        fixture = SyntheticRelease(synthetic=False)
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            fixture.write(release)
            with self.assertRaises(private.PrivateCustodianError) as captured:
                private.verify_release(
                    project_root=REPO_ROOT,
                    release_dir=release,
                    **fixture.verify_kwargs,
                )
        self.assertEqual(captured.exception.code, "private_non_synthetic_release_not_supported")

    def test_same_ed25519_key_cannot_fill_both_roles(self) -> None:
        fixture = SyntheticRelease(same_role_key=True)
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            fixture.write(release)
            with self.assertRaises(private.PrivateCustodianError) as captured:
                private.verify_release(
                    project_root=REPO_ROOT,
                    release_dir=release,
                    **fixture.verify_kwargs,
                )
        self.assertEqual(captured.exception.code, "private_trust_role_separation_invalid")

    def test_external_freeze_and_ledger_anchors_fail_closed(self) -> None:
        fixture = SyntheticRelease()
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            fixture.write(release)
            for field in (
                "expected_trust_manifest_sha256",
                "expected_freeze_request_sha256",
                "expected_candidate_commitment_sha256",
                "expected_access_reservation_entry_sha256",
                "expected_ledger_final_head_sha256",
            ):
                kwargs = dict(fixture.verify_kwargs)
                kwargs[field] = _sha("wrong-" + field)
                with self.subTest(field=field), self.assertRaises(private.PrivateCustodianError):
                    private.verify_release(
                        project_root=REPO_ROOT,
                        release_dir=release,
                        **kwargs,
                    )

            with mock.patch.object(
                private, "EXPECTED_SCHEMA_BUNDLE_SHA256", _sha("unreviewed-schema-bundle")
            ):
                with self.assertRaises(private.PrivateCustodianError) as bundle:
                    private.verify_release(
                        project_root=REPO_ROOT,
                        release_dir=release,
                        **fixture.verify_kwargs,
                    )
            self.assertEqual(bundle.exception.code, "private_kit_bundle_drift")

    def test_signature_and_release_digest_tampering_fail_closed(self) -> None:
        fixture = SyntheticRelease()
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            fixture.write(release)
            result_path = release / "aggregate_results.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["completed_total_case_executions"] = 299
            result_path.write_text(json.dumps(result), encoding="utf-8")
            with self.assertRaises(private.PrivateCustodianError) as captured:
                private.verify_release(
                    project_root=REPO_ROOT,
                    release_dir=release,
                    **fixture.verify_kwargs,
                )
        self.assertEqual(captured.exception.code, "private_document_hash_mismatch")

    def test_two_stage_ledger_rejects_replay_fork_and_terminal_only(self) -> None:
        fixture = SyntheticRelease()
        ledger = deepcopy(fixture.documents["signed_ledger"])
        state = private._verify_ledger(ledger, private._trust_keys(fixture.documents["trust_manifest"]))
        self.assertEqual(len(state["reservation_by_authorization"]), 1)

        duplicate = deepcopy(ledger["entries"][0])
        duplicate["sequence"] = 3
        duplicate["previous_entry_sha256"] = ledger["entries"][-1]["entry_sha256"]
        duplicate["event_at_utc"] = "2030-01-04T02:01:30Z"
        duplicate["authorization_grant_sha256"] = _sha("second-grant-same-freeze")
        duplicate["authorization_nonce_sha256"] = _sha("second-grant-nonce")
        duplicate["run_commitment_sha256"] = _sha("second-run-same-freeze")
        duplicate["salted_corpus_commitment_sha256"] = _sha("second-corpus-same-freeze")
        duplicate = _finalize_entry(
            duplicate,
            ledger_id=fixture.ledger_id,
            signers=[("custodian", fixture.custodian_id, fixture.custodian)],
        )
        replayed = deepcopy(ledger)
        replayed["entries"].append(duplicate)
        replayed["head_sequence"] = 3
        replayed["ledger_head_sha256"] = duplicate["entry_sha256"]
        with self.assertRaises(private.PrivateCustodianError) as replay:
            private._verify_ledger(
                replayed, private._trust_keys(fixture.documents["trust_manifest"])
            )
        self.assertEqual(replay.exception.code, "private_authorization_replay")

        forked = deepcopy(ledger)
        forked["entries"][0]["previous_entry_sha256"] = _sha("wrong-head")
        with self.assertRaises(private.PrivateCustodianError) as fork:
            private._verify_ledger(
                forked, private._trust_keys(fixture.documents["trust_manifest"])
            )
        self.assertEqual(fork.exception.code, "private_ledger_chain_invalid")

        terminal_only = deepcopy(ledger)
        terminal_only["entries"] = [ledger["entries"][1]]
        with self.assertRaises(private.PrivateCustodianError):
            private._verify_ledger(
                terminal_only, private._trust_keys(fixture.documents["trust_manifest"])
            )

        mixed_scope = deepcopy(ledger)
        mixed_scope["entries"][0]["synthetic"] = False
        with self.assertRaises(private.PrivateCustodianError) as synthetic_scope:
            private._verify_ledger(
                mixed_scope, private._trust_keys(fixture.documents["trust_manifest"])
            )
        self.assertEqual(
            synthetic_scope.exception.code, "private_ledger_synthetic_scope_mismatch"
        )

    def test_atomic_reservation_writer_consumes_freeze_once(self) -> None:
        fixture = SyntheticRelease()
        registry = REPO_ROOT.parent / ("synthetic-ledger-registry-" + uuid.uuid4().hex)
        registry.mkdir()
        self.addCleanup(lambda: shutil.rmtree(registry, ignore_errors=True))
        reservation = fixture.documents["signed_ledger"]["entries"][0]
        keys = private._trust_keys(fixture.documents["trust_manifest"])
        result = private.reserve_access_atomically(
            project_root=REPO_ROOT,
            registry_dir=registry,
            ledger_id=fixture.ledger_id,
            entry=reservation,
            trust_keys=keys,
            expected_base_sequence=0,
            expected_base_head_sha256=private.ZERO_SHA256,
        )
        self.assertTrue(result["atomic_create_if_absent"])
        self.assertTrue(result["authorization_consumed"])
        self.assertFalse(result["private_access_may_proceed"])

        replay = deepcopy(reservation)
        replay["sequence"] = 2
        replay["previous_entry_sha256"] = reservation["entry_sha256"]
        replay["event_at_utc"] = "2030-01-03T00:00:01Z"
        replay = _finalize_entry(
            replay,
            ledger_id=fixture.ledger_id,
            signers=[("custodian", fixture.custodian_id, fixture.custodian)],
        )
        with self.assertRaises(private.PrivateCustodianError) as captured:
            private.reserve_access_atomically(
                project_root=REPO_ROOT,
                registry_dir=registry,
                ledger_id=fixture.ledger_id,
                entry=replay,
                trust_keys=keys,
                expected_base_sequence=1,
                expected_base_head_sha256=reservation["entry_sha256"],
            )
        self.assertEqual(captured.exception.code, "private_authorization_replay")

    def test_atomic_reservation_head_failure_leaves_consumed_marker(self) -> None:
        fixture = SyntheticRelease()
        registry = REPO_ROOT.parent / ("synthetic-ledger-crash-" + uuid.uuid4().hex)
        registry.mkdir()
        self.addCleanup(lambda: shutil.rmtree(registry, ignore_errors=True))
        (registry / "ledger-head.next").write_text("occupied", encoding="utf-8")
        reservation = fixture.documents["signed_ledger"]["entries"][0]
        keys = private._trust_keys(fixture.documents["trust_manifest"])
        with self.assertRaises(private.PrivateCustodianError) as incomplete:
            private.reserve_access_atomically(
                project_root=REPO_ROOT,
                registry_dir=registry,
                ledger_id=fixture.ledger_id,
                entry=reservation,
                trust_keys=keys,
                expected_base_sequence=0,
                expected_base_head_sha256=private.ZERO_SHA256,
            )
        self.assertEqual(incomplete.exception.code, "private_registry_incomplete_consumption")
        marker = registry / f"freeze-{reservation['freeze_request_sha256']}.reservation.json"
        self.assertTrue(marker.is_file())
        (registry / "ledger-head.next").unlink()
        with self.assertRaises(private.PrivateCustodianError) as replay:
            private.reserve_access_atomically(
                project_root=REPO_ROOT,
                registry_dir=registry,
                ledger_id=fixture.ledger_id,
                entry=reservation,
                trust_keys=keys,
                expected_base_sequence=0,
                expected_base_head_sha256=private.ZERO_SHA256,
            )
        self.assertEqual(replay.exception.code, "private_authorization_replay")

    def test_metric_denominator_budget_latency_and_cell_coverage_fail_closed(self) -> None:
        fixture = SyntheticRelease()
        result = fixture.documents["aggregate_results"]
        plan = private._provider_plan(fixture.providers)
        statement = fixture.documents["commitment_statement"]
        authorization = fixture.documents["authorization_grant"]

        bad = deepcopy(result)
        bad["provider_results"][0]["repetitions"][0]["metrics"][0]["eligible_count"] = 1
        with self.assertRaises(private.PrivateCustodianError) as denominator:
            private._verify_aggregate_results(bad, plan, statement, authorization)
        self.assertEqual(denominator.exception.code, "private_metric_scope_invalid")

        bad = deepcopy(result)
        bad["budget_actual"]["model_calls"] = 1201
        with self.assertRaises(private.PrivateCustodianError) as budget:
            private._verify_aggregate_results(bad, plan, statement, authorization)
        self.assertIn(budget.exception.code, {"private_result_budget_total_invalid", "private_result_budget_exceeded"})

        bad = deepcopy(result)
        bad["provider_results"][0]["repetitions"][0]["latency_p50_ms"] = 3000
        with self.assertRaises(private.PrivateCustodianError) as latency:
            private._verify_aggregate_results(bad, plan, statement, authorization)
        self.assertEqual(latency.exception.code, "private_result_latency_invalid")

        bad = deepcopy(result)
        bad["aggregate_cells"][0]["evaluated_count"] -= 1
        with self.assertRaises(private.PrivateCustodianError):
            private._verify_aggregate_results(bad, plan, statement, authorization)

        bounded_result = deepcopy(result)
        bounded_statement = deepcopy(statement)
        for commitment in bounded_statement["cell_commitments"]:
            if commitment["cell_type"] == "dataset" and commitment["cell_ordinal"] == 1:
                commitment["eligible_case_count"] = 4
            if commitment["cell_type"] == "dataset" and commitment["cell_ordinal"] == 2:
                commitment["eligible_case_count"] = 31
        for cell in bounded_result["aggregate_cells"]:
            if cell["cell_type"] != "dataset":
                continue
            if cell["cell_ordinal"] == 1:
                cell.update(
                    {
                        "eligible_count": 4,
                        "evaluated_count": 4,
                        "numerator": None,
                        "rate": None,
                        "suppressed": True,
                    }
                )
            elif cell["cell_ordinal"] == 2:
                cell.update(
                    {
                        "eligible_count": 31,
                        "evaluated_count": 31,
                        "numerator": 30,
                        "rate": private._expected_rate(30, 31),
                    }
                )
        bounded = private._verify_aggregate_results(
            bounded_result, plan, bounded_statement, authorization
        )
        self.assertEqual(bounded["cell_numerator_reconciliation_status"], "bounded")

    def test_provider_plan_requires_distinct_provider_ids(self) -> None:
        providers = deepcopy(SyntheticRelease().providers)
        providers[1]["provider_id"] = providers[0]["provider_id"]
        with self.assertRaises(private.PrivateCustodianError) as captured:
            private._provider_plan(providers)
        self.assertEqual(captured.exception.code, "private_provider_plan_invalid")

    def test_partial_usage_coverage_reconciles_and_cannot_claim_budget_gate(self) -> None:
        fixture = SyntheticRelease("stopped")
        result = deepcopy(fixture.documents["aggregate_results"])
        usage = result["provider_results"][0]["repetitions"][0]["usage"]
        usage.update(
            {
                "model_calls": None,
                "input_tokens": None,
                "output_tokens": None,
                "cost_decimal": None,
                "coverage_numerator": 25,
                "coverage_rate": "0.500000",
                "coverage_status": "partial",
            }
        )
        result["budget_actual"].update(
            {
                "model_calls": None,
                "input_tokens": None,
                "output_tokens": None,
                "cost_decimal": None,
                "coverage_numerator": 275,
                "coverage_rate": private._expected_rate(275, 300),
                "coverage_status": "partial",
            }
        )
        checks = private._verify_aggregate_results(
            result,
            private._provider_plan(fixture.providers),
            fixture.documents["commitment_statement"],
            fixture.documents["authorization_grant"],
        )
        self.assertFalse(checks["budget_gate_verified"])
        result["budget_actual"]["coverage_numerator"] = 274
        with self.assertRaises(private.PrivateCustodianError):
            private._verify_aggregate_results(
                result,
                private._provider_plan(fixture.providers),
                fixture.documents["commitment_statement"],
                fixture.documents["authorization_grant"],
            )

    def test_expiry_after_freeze_request_is_rejected_even_when_resigned(self) -> None:
        fixture = SyntheticRelease(authorization_expires_at="2030-03-01T00:00:00Z")
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            fixture.write(release)
            with self.assertRaises(private.PrivateCustodianError) as captured:
                private.verify_release(
                    project_root=REPO_ROOT,
                    release_dir=release,
                    **fixture.verify_kwargs,
                )
        self.assertEqual(captured.exception.code, "private_authorization_time_invalid")

    def test_resume_after_authorization_expiry_is_rejected(self) -> None:
        reservation_hash = _sha("reservation")
        ledger = {
            "entries": [
                {
                    "event_type": "resume_authorized",
                    "reservation_entry_sha256": reservation_hash,
                    "event_at_utc": "2030-03-01T00:00:00Z",
                }
            ]
        }
        with self.assertRaises(private.PrivateCustodianError) as captured:
            private._verify_resume_window(
                ledger,
                reservation_entry_sha256=reservation_hash,
                expires_at=private._parse_timestamp(
                    "2030-01-31T00:00:00Z", "authorization expiry"
                ),
            )
        self.assertEqual(captured.exception.code, "private_ledger_resume_expired")

    def test_duplicate_keys_nonfinite_and_encoded_forbidden_projection_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            duplicate = Path(temporary) / "duplicate.json"
            duplicate.write_text('{"value":1,"value":2}', encoding="utf-8")
            with self.assertRaises(private.PrivateCustodianError) as duplicate_error:
                private.load_json_object(duplicate, "duplicate")
            self.assertEqual(duplicate_error.exception.code, "private_json_duplicate_key")
            nonfinite = Path(temporary) / "nonfinite.json"
            nonfinite.write_text('{"value":NaN}', encoding="utf-8")
            with self.assertRaises(private.PrivateCustodianError) as nonfinite_error:
                private.load_json_object(nonfinite, "nonfinite")
            self.assertEqual(nonfinite_error.exception.code, "private_json_non_finite")

        canary = "PRIVATE@example.org"
        multiline_canary = "prefix\nPRIVATE@example.org"
        encoded_values = (
            canary,
            "PRIVATE%2540example.org",
            base64.urlsafe_b64encode(canary.encode()).decode().rstrip("="),
            base64.b32encode(canary.encode()).decode(),
            canary.encode().hex(),
            base64.urlsafe_b64encode(multiline_canary.encode()).decode().rstrip("="),
            base64.b32encode(multiline_canary.encode()).decode(),
            multiline_canary.encode().hex(),
        )
        for value in encoded_values:
            with self.subTest(value=value), self.assertRaises(private.PrivateCustodianError):
                private.assert_public_release_projection({"model_id": value})

    def test_private_root_is_snapshot_only_and_rejects_repo_hardlinks(self) -> None:
        fake_project = REPO_ROOT.parent / ("synthetic-project-" + uuid.uuid4().hex)
        fake_project.mkdir()
        (fake_project / "tracked-marker.txt").write_text("synthetic", encoding="utf-8")
        self.addCleanup(lambda: shutil.rmtree(fake_project, ignore_errors=True))
        ignored = fake_project / "evals" / "v2" / "private" / ("fixture-" + uuid.uuid4().hex)
        ignored.mkdir(parents=True)
        with self.assertRaises(private.PrivateCustodianError) as inside:
            private.assert_private_root_outside_repository(ignored, fake_project)
        self.assertEqual(inside.exception.code, "private_root_not_external")

        external = REPO_ROOT.parent / ("private-custodian-fixture-" + uuid.uuid4().hex)
        external.mkdir()
        self.addCleanup(lambda: shutil.rmtree(external, ignore_errors=True))
        result = private.assert_private_root_outside_repository(external, fake_project)
        self.assertTrue(result["point_in_time_snapshot_only"])
        self.assertFalse(result["authorization_enforcement_performed"])

        external_file = external / "synthetic-private.bin"
        external_file.write_bytes(b"synthetic private fixture")
        linked_file = ignored / "linked-private.bin"
        try:
            os.link(external_file, linked_file)
        except OSError:
            return
        with self.assertRaises(private.PrivateCustodianError) as overlap:
            private.assert_private_root_outside_repository(external, fake_project)
        self.assertIn(
            overlap.exception.code,
            {"private_root_hardlink_forbidden", "private_root_file_identity_overlap"},
        )

    def test_release_directory_symlink_is_rejected(self) -> None:
        fixture = SyntheticRelease()
        with tempfile.TemporaryDirectory() as temporary:
            real = Path(temporary) / "real"
            link = Path(temporary) / "link"
            fixture.write(real)
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks unavailable")
            with self.assertRaises(private.PrivateCustodianError) as captured:
                private.verify_release(
                    project_root=REPO_ROOT,
                    release_dir=link,
                    **fixture.verify_kwargs,
                )
        self.assertEqual(captured.exception.code, "private_release_directory_invalid")

    def test_release_directory_injection_during_read_fails_closed(self) -> None:
        fixture = SyntheticRelease()
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            fixture.write(release)
            original = private._read_regular_file_no_follow
            injected = False

            def injecting_reader(path: Path, label: str) -> bytes:
                nonlocal injected
                if not injected and path.name in private.RELEASE_FILES.values():
                    injected = True
                    (release / "PRIVATE-EXTRA.txt").write_text(
                        "PRIVATE@example.org", encoding="utf-8"
                    )
                return original(path, label)

            with mock.patch.object(private, "_read_regular_file_no_follow", injecting_reader):
                with self.assertRaises(private.PrivateCustodianError) as captured:
                    private.verify_release(
                        project_root=REPO_ROOT,
                        release_dir=release,
                        **fixture.verify_kwargs,
                    )
        self.assertEqual(captured.exception.code, "private_release_directory_changed")

    def test_already_read_release_file_mutation_is_revalidated(self) -> None:
        fixture = SyntheticRelease()
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            fixture.write(release)
            original = private._read_regular_file_no_follow
            trust_was_read = False
            mutated = False

            def mutating_reader(path: Path, label: str) -> bytes:
                nonlocal trust_was_read, mutated
                if path.name == "trust_manifest.json":
                    trust_was_read = True
                elif (
                    path.name in private.RELEASE_FILES.values()
                    and trust_was_read
                    and not mutated
                ):
                    mutated = True
                    (release / "trust_manifest.json").write_text(
                        "PRIVATE@example.org", encoding="utf-8"
                    )
                return original(path, label)

            with mock.patch.object(private, "_read_regular_file_no_follow", mutating_reader):
                with self.assertRaises(private.PrivateCustodianError) as captured:
                    private.verify_release(
                        project_root=REPO_ROOT,
                        release_dir=release,
                        **fixture.verify_kwargs,
                    )
        self.assertIn(
            captured.exception.code,
            {"private_release_file_changed", "private_release_directory_changed"},
        )

    def test_cli_error_does_not_echo_rejected_path(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = private.main(
                [
                    "check-private-root",
                    "--project-root", str(REPO_ROOT),
                    "--private-root", str(REPO_ROOT),
                ]
            )
        self.assertEqual(exit_code, 2)
        self.assertNotIn(str(REPO_ROOT), output.getvalue())
        self.assertIn("private_root_not_external", output.getvalue())

    def test_repository_contains_no_committed_private_key_and_ci_runs_gate(self) -> None:
        kit_files = [path for path in KIT_ROOT.rglob("*") if path.is_file()]
        self.assertTrue(kit_files)
        for path in kit_files:
            content = path.read_bytes()
            self.assertNotIn(b"BEGIN PRIVATE KEY", content)
            self.assertNotIn(b"BEGIN OPENSSH PRIVATE KEY", content)
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("Validate private-holdout custodian kit", workflow)
        self.assertIn("eval_v2_private_custodian.py verify-kit", workflow)
        self.assertIn("eval_v2_private_custodian.py status", workflow)
        self.assertNotIn("confirm-private-access", workflow)


if __name__ == "__main__":
    unittest.main()
