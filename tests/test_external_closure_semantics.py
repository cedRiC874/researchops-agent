from __future__ import annotations

import ast
import copy
import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from types import SimpleNamespace

import researchops_external_closure.semantics as semantics_module
from researchops_completion_telemetry.capture import RuntimeDenominatorTracker
from researchops_completion_telemetry.persisted_evidence import (
    create_persisted_live_evidence_validation_context,
    summarize_persisted_live_runtime_denominator_artifact,
)
from researchops_completion_telemetry.sanitization import (
    sanitize_completion_capture,
)
from researchops_completion_telemetry.surface_mapping import (
    load_and_select_surface_mapping,
)
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.primitives import canonical_json_bytes
from researchops_external_closure.semantics import (
    VerifiedArtifactContentSemantics,
    verify_artifact_content_semantics,
)
from tests.test_provider_completion_dynamic_denominator import _capture, _plan_binding
from tests.manual_completion_denominator import manual_denominator


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "researchops_external_closure" / "semantics.py"
AUDIT_RUN_ID_DOMAIN = "researchops-provider-completion-audit-run-id-v1"
AUDIT_RUN_SET_DOMAIN = "researchops-provider-completion-audit-run-set-v1"
EXECUTION_ORDER_DOMAIN = "researchops-provider-completion-execution-input-order-v1"
RUNTIME_PLAN_DOMAIN = "researchops-provider-completion-external-runtime-plan-v1"


def _h(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _domain_hash(domain: str, *parts: bytes) -> str:
    return hashlib.sha256(
        domain.encode("utf-8") + b"\0" + b"\0".join(parts)
    ).hexdigest()


def _run_id(envelope_id: str, case_id: str) -> str:
    return "PCERUN-" + hashlib.sha256(
        AUDIT_RUN_ID_DOMAIN.encode("ascii")
        + b"\0"
        + envelope_id.encode("ascii")
        + b"\0"
        + case_id.encode("ascii")
    ).hexdigest()[:32].upper()


def _event_hash(row: dict) -> str:
    body = {
        "run_id": row["run_id"],
        "sequence": row["sequence"],
        "event_type": row["event_type"],
        "occurred_at_utc": row["occurred_at_utc"],
        "actor_kind": row["actor_kind"],
        "safe_payload_json": row["safe_payload_json"],
        "prev_hash": row["prev_hash"],
    }
    return hashlib.sha256(
        b"researchops.audit.v1\0" + canonical_json_bytes(body)
    ).hexdigest()


def _plan_snapshot(plan) -> dict:
    binding = plan.runtime_binding().runtime_snapshot()
    return {
        "schema_version": "provider-completion-runtime-denominator-plan/1.0",
        **{
            field: binding[field]
            for field in (
                "provider_id",
                "api_surface",
                "transport_id",
                "adapter_version",
                "telemetry_schema_sha256",
                "mapping_schema_version",
                "mapping_version",
                "mapping_sha256",
            )
        },
        "case_ids": list(plan.case_ids),
        "case_ids_sha256": plan.case_ids_sha256,
        "max_turns_per_case": plan.max_turns_per_case,
        "total_model_request_cap": plan.total_model_request_cap,
        "agents_sdk_retries": 0,
        "http_client_retries": 0,
        "denominator_algorithm": plan.denominator_algorithm,
        "exact_response_count_preregistered": False,
    }


def _external_plan(
    denominator: dict,
    *,
    campaign_id: str,
    run_ids: tuple[str, ...],
) -> dict:
    budget_body = {
        "pricing_snapshot_date": "2026-09-04",
        "pricing_source_url": "https://pricing.example.invalid/snapshot",
        "pricing_source_snapshot_sha256": _h("pricing"),
        "pricing_retrieved_at_utc": "2026-09-04T00:00:00Z",
        "official_pricing_attestation_current": True,
        "currency": "CNY",
        "token_price_unit": "per_million_tokens",
        "input_price_per_million_cny": "1.000000",
        "output_price_per_million_cny": "2.000000",
        "cache_discount_assumed": False,
        "input_token_limit_total": 100,
        "output_token_limit_per_request": 2000,
        "output_token_limit_total": 4000,
        "local_observed_cost_stop_cny": "1.000000",
        "request_count_and_output_caps_enforced_pre_send": True,
        "input_token_limit_enforcement": "post_response_observed_stop",
        "cost_stop_enforcement": "post_response_observed_stop",
        "single_in_flight_request_may_overshoot_observed_limits": True,
        "provider_invoice_hard_cap": False,
        "provider_side_retention_verified": False,
    }
    budget = {
        "budget_policy_commitment_sha256": _domain_hash(
            RUNTIME_PLAN_DOMAIN,
            b"budget_policy",
            canonical_json_bytes(budget_body),
        ),
        **budget_body,
    }
    body = {
        "schema_version": "provider-completion-external-runtime-plan/1.0",
        "denominator_plan_commitment_sha256": _canonical_hash(denominator),
        "denominator_plan": denominator,
        "telemetry_interpretation": {
            "output_counter_comparability": "comparable",
            "output_counter_path": "output_tokens",
        },
        "campaign_topology": {
            "campaign_id": campaign_id,
            "case_handle_set_commitment_sha256": _h("case-set"),
            "case_handle_order_commitment_sha256": _h("case-order"),
            "audit_run_id_derivation": (
                "sha256-domain-separated-envelope-id-and-case-handle-v1"
            ),
            "audit_run_set_commitment_sha256": _domain_hash(
                AUDIT_RUN_SET_DOMAIN,
                b"planned",
                canonical_json_bytes(list(run_ids)),
            ),
            "one_audit_run_per_case_handle": True,
        },
        "transport_limits": {
            "network_attempt_cap": denominator["total_model_request_cap"],
            "concurrency": 1,
            "resume": False,
            "fallback": False,
            "tools": 0,
            "request_timeout_seconds": 60,
            "total_timeout_seconds": 600,
        },
        "budget_policy": budget,
    }
    return {
        **body,
        "external_plan_binding_sha256": _domain_hash(
            RUNTIME_PLAN_DOMAIN, canonical_json_bytes(body)
        ),
    }


class _Fixture(dict):
    pass


class ExternalClosureSemanticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.selection = load_and_select_surface_mapping(
            ROOT,
            "deepseek",
            "responses",
            "openai_compatible_responses",
            purpose="offline_validation",
        )
        cls.expected_binding = {
            "telemetry_schema_sha256": cls.selection.telemetry_schema_sha256,
            "adapter_version": cls.selection.adapter_version,
            "mapping_schema_version": cls.selection.mapping_schema_version,
            "mapping_version": cls.selection.mapping_version,
            "mapping_sha256": cls.selection.mapping_sha256,
            "provider_id": cls.selection.provider_id,
            "api_surface": cls.selection.api_surface,
            "transport_id": cls.selection.transport_id,
            "output_counter_comparability": (
                cls.selection.output_counter_comparability
            ),
            "output_counter_path": cls.selection.output_counter_path,
        }

    def _fixture(
        self,
        *,
        observed_cases: int = 2,
        incomplete_usage: bool = False,
        input_tokens: int = 10,
        rejected_responses: bool = False,
    ) -> _Fixture:
        case_ids = (
            "PCECASE-00000000000000000000000000000001",
            "PCECASE-00000000000000000000000000000002",
        )
        envelope_id = "PCEPR-11111111111111111111111111111111"
        campaign_id = "PCECAMP-22222222222222222222222222222222"
        grant_sha = _h("grant")
        denominator_plan, denominator = manual_denominator(
            self.selection, self.expected_binding, case_ids,
            observed_cases=observed_cases, incomplete_usage=incomplete_usage,
            input_tokens=input_tokens, rejected_responses=rejected_responses,
        )
        context = create_persisted_live_evidence_validation_context(
            self.selection,
            expected_binding=self.expected_binding,
            expected_denominator_plan=denominator_plan,
            expected_denominator_plan_commitment_sha256=(
                _canonical_hash(denominator_plan)
            ),
        )
        run_ids = tuple(_run_id(envelope_id, case_id) for case_id in case_ids)
        external_plan = _external_plan(
            denominator_plan, campaign_id=campaign_id, run_ids=run_ids
        )
        binding = {
            **{
                field: denominator_plan[field]
                for field in (
                    "provider_id",
                    "api_surface",
                    "transport_id",
                    "adapter_version",
                    "telemetry_schema_sha256",
                    "mapping_schema_version",
                    "mapping_version",
                    "mapping_sha256",
                )
            },
            **external_plan["telemetry_interpretation"],
        }
        execution_commitments = tuple(_h(f"execution-{i}") for i in range(2))
        execution_order = _domain_hash(
            EXECUTION_ORDER_DOMAIN,
            canonical_json_bytes(list(execution_commitments)),
        )

        run_rows: list[dict] = []
        event_rows: list[dict] = []
        model_rows: list[dict] = []
        index_runs: list[dict] = []
        event_id = 0
        for ordinal, (case_id, run_id, execution_commitment) in enumerate(
            zip(
                case_ids[:observed_cases],
                run_ids[:observed_cases],
                execution_commitments[:observed_cases],
            ),
            start=0,
        ):
            record = (
                None if rejected_responses else denominator["records"][ordinal]
            )
            request_projection = {
                "campaign_id": campaign_id,
                "case_handle": case_id,
                "execution_input_commitment_sha256": execution_commitment,
                "external_plan_binding_sha256": external_plan[
                    "external_plan_binding_sha256"
                ],
                "authorization_grant_sha256": grant_sha,
                "provider_id": denominator_plan["provider_id"],
                "model_id": "deepseek-v4-flash",
                "api_surface": denominator_plan["api_surface"],
                "transport_id": denominator_plan["transport_id"],
            }
            request_sha = _canonical_hash(request_projection)
            minute = ordinal + 1
            payloads = [
                (
                    "run_started",
                    "system",
                    {
                        "mode": "provider_completion_external_closure",
                        "request_sha256": request_sha,
                        "dataset_sha256": None,
                    },
                ),
                (
                    "model_request_started",
                    "provider_adapter",
                    {
                        "schema_version": "provider-completion-ledger-event/1.1",
                        "case_id": case_id,
                        "attempt_index": ordinal,
                        "case_attempt_index": 0,
                        "binding": binding,
                    },
                ),
                (
                    "provider_transport_request_sent",
                    "provider_adapter",
                    {
                        "schema_version": "provider-completion-transport-send/1.0",
                        "case_id": case_id,
                        "attempt_index": ordinal,
                        "case_attempt_index": 0,
                        "network_call_index": ordinal,
                        "provider_id": denominator_plan["provider_id"],
                        "api_surface": denominator_plan["api_surface"],
                        "transport_id": denominator_plan["transport_id"],
                        "adapter_version": denominator_plan["adapter_version"],
                        "method": "POST",
                        "origin": "https://api.deepseek.com",
                        "path": "/responses",
                        "requested_output_token_cap": 2000,
                        "execution_input_commitment_sha256": execution_commitment,
                        "audit_request_sha256": request_sha,
                        "external_plan_binding_sha256": external_plan[
                            "external_plan_binding_sha256"
                        ],
                        "authorization_grant_sha256": grant_sha,
                        "headers_persisted": False,
                        "request_body_persisted": False,
                        "task_content_persisted": False,
                    },
                ),
                (
                    (
                        "model_response_telemetry_rejected"
                        if rejected_responses
                        else "model_response_telemetry_recorded"
                    ),
                    "provider_adapter",
                    {
                        "schema_version": "provider-completion-ledger-event/1.1",
                        "case_id": case_id,
                        "attempt_index": ordinal,
                        "case_attempt_index": 0,
                        "terminal_kind": (
                            "response_rejected"
                            if rejected_responses
                            else "response_accepted"
                        ),
                        "response_index": ordinal,
                        "error_code": (
                            "provider_completion_capture_failed"
                            if rejected_responses
                            else None
                        ),
                        "binding": binding,
                    },
                ),
            ]
            if record is not None:
                payloads[3][2]["completion_record"] = record
            model_row = {
                "model_call_id": f"MODEL-{ordinal + 1:016X}",
                "run_id": run_id,
                "provider": denominator_plan["provider_id"],
                "model": "deepseek-v4-flash",
                "started_at_utc": f"2026-09-04T03:{minute:02d}:02Z",
                "latency_ms": 12.5,
                "input_tokens": None if incomplete_usage else input_tokens,
                "output_tokens": 2,
                "cached_tokens": None if incomplete_usage else 0,
                "cost_usd": None,
                "outcome": "succeeded",
                "error_code": None,
            }
            model_rows.append(model_row)
            payloads.extend(
                [
                    (
                        "model_call_recorded",
                        "agent_sdk",
                        {
                            field: model_row[field]
                            for field in (
                                "model_call_id",
                                "provider",
                                "model",
                                "latency_ms",
                                "input_tokens",
                                "output_tokens",
                                "cached_tokens",
                                "cost_usd",
                                "outcome",
                                "error_code",
                            )
                        },
                    ),
                    (
                        "model_run_usage_recorded",
                        "agent_sdk",
                        {
                            "usage_complete": not incomplete_usage,
                            "request_count": 1,
                            "input_unit_count": (
                                None if incomplete_usage else input_tokens
                            ),
                            "output_unit_count": 2,
                            "total_unit_count": (
                                None if incomplete_usage else input_tokens + 2
                            ),
                            "cached_input_unit_count": None if incomplete_usage else 0,
                            "response_detail_count": 1,
                            "estimated_cost_usd": None,
                            "model_call_rows_recorded": 1,
                            "latency_allocation": "equal_share_of_agent_segment",
                            "model_call_cost_method": "cny_recomputed_only_in_closure",
                            "provider": denominator_plan["provider_id"],
                            "transport": denominator_plan["transport_id"],
                        },
                    ),
                    (
                        "run_status_changed",
                        "system",
                        {"from": "running", "to": "completed", "error_code": None},
                    ),
                ]
            )
            run_events: list[dict] = []
            previous = "0" * 64
            for sequence, (event_type, actor, payload) in enumerate(payloads, start=1):
                event_id += 1
                row = {
                    "event_id": event_id,
                    "run_id": run_id,
                    "sequence": sequence,
                    "event_type": event_type,
                    "occurred_at_utc": f"2026-09-04T03:{minute:02d}:0{sequence - 1}Z",
                    "actor_kind": actor,
                    "safe_payload_json": canonical_json_bytes(payload).decode("utf-8"),
                    "prev_hash": previous,
                    "event_hash": "",
                }
                row["event_hash"] = _event_hash(row)
                previous = row["event_hash"]
                event_rows.append(row)
                run_events.append(row)
            run_rows.append(
                {
                    "run_id": run_id,
                    "mode": "provider_completion_external_closure",
                    "status": "completed",
                    "request_sha256": request_sha,
                    "dataset_sha256": None,
                    "created_at_utc": run_events[0]["occurred_at_utc"],
                    "updated_at_utc": run_events[-1]["occurred_at_utc"],
                    "terminal_error_code": None,
                }
            )
            bridge_body = {
                "schema_version": "provider-completion-ledger-bridge-commitment/1.0",
                "case_id": case_id,
                "binding_sha256": _canonical_hash(binding),
                "started": [
                    {"attempt_index": ordinal, "event_hash": run_events[1]["event_hash"]}
                ],
                "terminals": [
                    {"attempt_index": ordinal, "event_hash": run_events[3]["event_hash"]}
                ],
                "all_started_attempts_terminal": True,
                "write_failed": False,
            }
            index_runs.append(
                {
                    "task_id": case_id,
                    "run_id": run_id,
                    "provider": denominator_plan["provider_id"],
                    "transport": denominator_plan["transport_id"],
                    "chain_verification": {
                        "valid": True,
                        "run_id": run_id,
                        "event_count": len(run_events),
                        "chain_head": run_events[-1]["event_hash"],
                        "error_code": None,
                        "error_sequence": None,
                    },
                    "completion_telemetry_event_commitment": {
                        **bridge_body,
                        "commitment_sha256": _canonical_hash(bridge_body),
                    },
                }
            )

        event_counts = (
            {}
            if observed_cases == 0
            else dict(
                sorted(
                    {
                        "model_call_recorded": observed_cases,
                        "model_request_started": observed_cases,
                        (
                            "model_response_telemetry_rejected"
                            if rejected_responses
                            else "model_response_telemetry_recorded"
                        ): observed_cases,
                        "model_run_usage_recorded": observed_cases,
                        "provider_transport_request_sent": observed_cases,
                        "run_started": observed_cases,
                        "run_status_changed": observed_cases,
                    }.items()
                )
            )
        )
        closure_reasons: list[str] = []
        if observed_cases < len(case_ids):
            closure_reasons.append("planned_cases_not_finalized")
        if observed_cases == 0:
            closure_reasons.extend(
                ["no_model_attempts_observed", "no_provider_responses_observed"]
            )
        if observed_cases < len(case_ids) or rejected_responses:
            closure_reasons.append("planned_case_without_accepted_response")
        if rejected_responses and observed_cases:
            closure_reasons.append("response_telemetry_rejected")
            closure_reasons.append("completion_record_denominator_mismatch")
        telemetry = {
            "schema_version": "phase6-completion-telemetry/1.0",
            "status": "recorded",
            "runtime_plan": denominator_plan,
            "runtime_denominator": denominator,
            "ledger_reconciliation": {
                "all_chains_valid": True,
                "ledger_export_failed": False,
                "ledger_failure_observed": False,
                "event_counts": event_counts,
                "reasons": [],
            },
            "closure": {
                "claim_allowed": not closure_reasons,
                "claim_scope": "derived_observed_provider_responses_in_this_run_only",
                "denominator_algorithm": "transport-response-finalization-v1",
                "exact_response_count_preregistered": False,
                "derived_after_run": True,
                "observed_response_count": observed_cases,
                "reasons": closure_reasons,
            },
            "historical_backfill_performed": False,
        }
        return _Fixture(
            telemetry=telemetry,
            audit_index={"schema_version": "1.1", "runs": index_runs},
            run_rows=run_rows,
            event_rows=event_rows,
            model_call_rows=model_rows,
            context=context,
            envelope_id=envelope_id,
            campaign_id=campaign_id,
            expected_runtime_plan=external_plan,
            authorization_grant_sha256=grant_sha,
            execution_input_order_commitment_sha256=execution_order,
        )

    def _verify(self, fixture: _Fixture) -> VerifiedArtifactContentSemantics:
        return verify_artifact_content_semantics(
            fixture["telemetry"],
            fixture["audit_index"],
            run_rows=fixture["run_rows"],
            event_rows=fixture["event_rows"],
            model_call_rows=fixture["model_call_rows"],
            persisted_evidence_context=fixture["context"],
            envelope_id=fixture["envelope_id"],
            campaign_id=fixture["campaign_id"],
            expected_runtime_plan=fixture["expected_runtime_plan"],
            authorization_grant_sha256=fixture["authorization_grant_sha256"],
            execution_input_order_commitment_sha256=fixture[
                "execution_input_order_commitment_sha256"
            ],
            api_origin="https://api.deepseek.com",
            api_path="/responses",
            model_id="deepseek-v4-flash",
            postrun_completed_at_utc="2026-09-04T03:30:00Z",
            authorization_consumed_at_utc="2026-09-04T02:50:00Z",
            task_released_at_utc="2026-09-04T02:55:00Z",
            provider_key_loaded_at_utc="2026-09-04T02:56:00Z",
        )

    def _failure_fixture(self, kind: str, *, observed_cases: int = 2) -> _Fixture:
        """One final failed run, optionally following a successful case."""
        fixture = self._fixture(observed_cases=observed_cases)
        case_ids = fixture["expected_runtime_plan"]["denominator_plan"]["case_ids"]
        tracker = RuntimeDenominatorTracker(
            _plan_binding(case_ids=tuple(case_ids), max_turns=2, request_cap=2)
        )
        for ordinal, case_id in enumerate(case_ids[:observed_cases]):
            handle = tracker.begin_attempt(case_id)
            if ordinal + 1 < observed_cases:
                tracker.finalize_response_accepted(handle, _capture())
                tracker.seal_case(
                    case_id, sdk_raw_response_count=1, sdk_usage_request_count=1,
                    sdk_request_usage_indices_by_response={0: (0,)},
                )
            else:
                if kind == "cancelled":
                    tracker.finalize_cancelled(handle)
                else:
                    getattr(tracker, f"finalize_{kind}")(
                        handle, f"provider_completion_{kind}"
                    )
                tracker.seal_case(
                    case_id, sdk_raw_response_count=0, sdk_usage_request_count=0,
                    sdk_request_usage_indices_by_response={},
                )
        denominator = json.loads(json.dumps(tracker.seal_runtime().to_dict()))
        fixture["telemetry"]["runtime_denominator"] = denominator
        failed_run = fixture["run_rows"][-1]
        failed_id = failed_run["run_id"]
        cancelled = kind == "cancelled"
        failed_run["status"] = "cancelled" if cancelled else "failed"
        failed_run["terminal_error_code"] = {
            "cancelled": "pce_transport_cancelled",
            "outcome_unknown": "pce_transport_outcome_unknown",
            "http_error": "pce_response_failed",
            "no_response": "pce_transport_failed",
        }[kind]
        sends = kind in {"http_error", "outcome_unknown"}
        kept_events = []
        for row in fixture["event_rows"]:
            payload = json.loads(row["safe_payload_json"])
            event_type = row["event_type"]
            if row["run_id"] != failed_id:
                if event_type == "model_response_telemetry_recorded":
                    payload["completion_record"] = denominator["records"][0]
                elif event_type == "model_call_recorded":
                    payload["cached_tokens"] = None
                elif event_type == "model_run_usage_recorded":
                    payload["cached_input_unit_count"] = None
                    payload["usage_complete"] = False
            else:
                if event_type == "model_call_recorded" or (
                    event_type == "provider_transport_request_sent" and not sends
                ):
                    continue
                if event_type == "model_response_telemetry_recorded":
                    row["event_type"] = semantics_module._TERMINAL_EVENT_BY_KIND[kind]
                    payload.pop("completion_record")
                    payload["terminal_kind"] = kind
                    payload["response_index"] = None
                    payload["error_code"] = (
                        None if cancelled else f"provider_completion_{kind}"
                    )
                elif event_type == "model_run_usage_recorded":
                    payload.update(
                        usage_complete=False, request_count=None,
                        input_unit_count=None, output_unit_count=None,
                        total_unit_count=None, cached_input_unit_count=None,
                        response_detail_count=0, model_call_rows_recorded=0,
                    )
                elif event_type == "run_status_changed":
                    payload.update(
                        to=failed_run["status"], error_code=failed_run["terminal_error_code"]
                    )
            row["safe_payload_json"] = canonical_json_bytes(payload).decode("utf-8")
            kept_events.append(row)
        fixture["event_rows"] = kept_events
        fixture["model_call_rows"] = [
            row for row in fixture["model_call_rows"] if row["run_id"] != failed_id
        ]
        for row in fixture["model_call_rows"]:
            row["cached_tokens"] = None
        for ordinal, run in enumerate(fixture["run_rows"]):
            rows = [row for row in kept_events if row["run_id"] == run["run_id"]]
            for sequence, row in enumerate(rows, 1):
                row["sequence"] = sequence
            self._rehash_run(fixture, ordinal)
        fixture["telemetry"]["ledger_reconciliation"]["event_counts"] = dict(
            sorted(semantics_module.Counter(row["event_type"] for row in kept_events).items())
        )
        summary = summarize_persisted_live_runtime_denominator_artifact(
            denominator, context=fixture["context"]
        )
        fixture["telemetry"]["closure"].update(
            claim_allowed=summary.inner_closure_claim_allowed,
            observed_response_count=summary.observed_response_count,
            reasons=list(summary.inner_closure_reasons),
        )
        return fixture

    def _rehash_run(self, fixture: _Fixture, ordinal: int) -> None:
        run_id = fixture["run_rows"][ordinal]["run_id"]
        events = [row for row in fixture["event_rows"] if row["run_id"] == run_id]
        events.sort(key=lambda row: row["sequence"])
        previous = "0" * 64
        for row in events:
            row["prev_hash"] = previous
            row["event_hash"] = _event_hash(row)
            previous = row["event_hash"]
        entry = fixture["audit_index"]["runs"][ordinal]
        entry["chain_verification"]["event_count"] = len(events)
        entry["chain_verification"]["chain_head"] = previous
        bridge = entry["completion_telemetry_event_commitment"]
        bridge["started"] = [
            {
                "attempt_index": json.loads(row["safe_payload_json"])["attempt_index"],
                "event_hash": row["event_hash"],
            }
            for row in events
            if row["event_type"] == "model_request_started"
        ]
        bridge["terminals"] = [
            {
                "attempt_index": json.loads(row["safe_payload_json"])["attempt_index"],
                "event_hash": row["event_hash"],
            }
            for row in events
            if row["event_type"]
            in {
                "model_response_telemetry_recorded",
                "provider_completion_mapping_unmapped",
                "model_response_telemetry_rejected",
                "model_request_http_error",
                "model_request_no_response",
                "model_request_cancelled",
                "model_request_outcome_unknown",
            }
        ]
        body = dict(bridge)
        body.pop("commitment_sha256")
        bridge["commitment_sha256"] = _canonical_hash(body)

    def assert_semantic_error(self, fixture: _Fixture, code: str) -> None:
        with self.assertRaises(ExternalClosurePrimitiveError) as caught:
            self._verify(fixture)
        self.assertEqual(caught.exception.code, code)

    def test_success_recomputes_safe_frozen_summary(self) -> None:
        fixture = self._fixture()
        summary = self._verify(fixture)
        self.assertEqual(
            (
                summary.observed_run_count,
                summary.planned_case_count,
                summary.attempt_count,
                summary.accepted_response_count,
                summary.network_attempt_count_observed,
                summary.model_request_count_observed,
                summary.model_call_count,
            ),
            (2, 2, 2, 2, 2, 2, 2),
        )
        self.assertTrue(summary.full_plan_observed)
        self.assertTrue(summary.usage_complete)
        self.assertEqual(summary.observed_input_tokens, 20)
        self.assertEqual(summary.observed_output_tokens, 4)
        self.assertEqual(summary.observed_cost_cny, "0.000028000000")
        self.assertTrue(summary.semantic_closure_claim_allowed)
        self.assertEqual(summary.semantic_closure_reasons, ())
        projection = asdict(summary)
        rendered = repr(projection)
        for case_id in fixture["expected_runtime_plan"]["denominator_plan"][
            "case_ids"
        ]:
            self.assertNotIn(case_id, rendered)
        for forbidden in ("records", "payload", "case_id", "run_id"):
            self.assertNotIn(forbidden, projection)
        with self.assertRaises(FrozenInstanceError):
            summary.attempt_count = 0  # type: ignore[misc]
        with self.assertRaises(TypeError):
            VerifiedArtifactContentSemantics()

        fixture["telemetry"]["closure"]["claim_allowed"] = False
        self.assertTrue(summary.telemetry_closure_claim_allowed)

    def test_exact_missing_suffix_is_valid_noneligible_evidence(self) -> None:
        fixture = self._fixture(observed_cases=1)
        summary = self._verify(fixture)
        self.assertEqual(summary.observed_run_count, 1)
        self.assertFalse(summary.full_plan_observed)
        self.assertFalse(summary.semantic_closure_claim_allowed)
        self.assertEqual(
            summary.semantic_closure_reasons,
            (
                "planned_cases_not_finalized",
                "planned_case_without_accepted_response",
            ),
        )
        expected = _domain_hash(
            AUDIT_RUN_SET_DOMAIN,
            b"observed",
            canonical_json_bytes([fixture["run_rows"][0]["run_id"]]),
        )
        self.assertEqual(
            summary.observed_audit_run_set_commitment_sha256, expected
        )

        empty = self._verify(self._fixture(observed_cases=0))
        self.assertEqual(empty.observed_run_count, 0)
        self.assertFalse(empty.usage_complete)
        self.assertIsNone(empty.observed_cost_cny)
        self.assertEqual(
            empty.semantic_closure_reasons,
            (
                "planned_cases_not_finalized",
                "no_model_attempts_observed",
                "no_provider_responses_observed",
                "planned_case_without_accepted_response",
            ),
        )

    def test_incomplete_record_usage_is_valid_but_noneligible_and_uncosted(self) -> None:
        summary = self._verify(self._fixture(incomplete_usage=True))
        self.assertTrue(summary.telemetry_closure_claim_allowed)
        self.assertFalse(summary.usage_complete)
        self.assertIsNone(summary.observed_input_tokens)
        self.assertIsNone(summary.observed_output_tokens)
        self.assertIsNone(summary.observed_cost_cny)
        self.assertFalse(summary.semantic_closure_claim_allowed)
        self.assertEqual(
            summary.semantic_closure_reasons, ("record_usage_incomplete",)
        )

    def test_budget_caps_are_decimal_recomputed_closure_reasons(self) -> None:
        summary = self._verify(self._fixture(input_tokens=60))
        self.assertTrue(summary.usage_complete)
        self.assertEqual(summary.observed_input_tokens, 120)
        self.assertEqual(summary.observed_output_tokens, 4)
        self.assertEqual(summary.observed_cost_cny, "0.000128000000")
        self.assertEqual(
            summary.semantic_closure_reasons,
            ("observed_input_token_cap_exceeded",),
        )
        self.assertFalse(summary.semantic_closure_claim_allowed)

    def test_rejected_responses_remain_valid_noneligible_evidence(self) -> None:
        summary = self._verify(self._fixture(rejected_responses=True))
        self.assertEqual(summary.accepted_response_count, 0)
        self.assertFalse(summary.usage_complete)
        self.assertEqual(
            summary.semantic_closure_reasons,
            (
                "planned_case_without_accepted_response",
                "response_telemetry_rejected",
                "completion_record_denominator_mismatch",
                "record_usage_index_coverage_mismatch",
            ),
        )

    def test_no_send_and_post_send_failures_are_valid_but_never_complete_usage(self) -> None:
        for observed_cases in (1, 2):
            for kind, reason in (
                ("no_response", "request_failed_without_response"),
                ("cancelled", "model_request_cancelled"),
                ("http_error", "http_error_response_observed"),
                ("outcome_unknown", "model_request_outcome_unknown"),
            ):
                with self.subTest(kind=kind, observed_cases=observed_cases):
                    summary = self._verify(self._failure_fixture(
                        kind, observed_cases=observed_cases
                    ))
                    self.assertEqual(summary.attempt_count, observed_cases)
                    self.assertEqual(summary.accepted_response_count, observed_cases - 1)
                    self.assertEqual(
                        summary.network_attempt_count_observed,
                        observed_cases - (kind in {"no_response", "cancelled"}),
                    )
                    self.assertFalse(summary.semantic_closure_claim_allowed)
                    self.assertFalse(summary.usage_complete)
                    self.assertIsNone(summary.observed_input_tokens)
                    self.assertIsNone(summary.observed_output_tokens)
                    self.assertIsNone(summary.observed_cost_cny)
                    self.assertIn(reason, summary.semantic_closure_reasons)
                    self.assertIn("record_usage_index_coverage_mismatch", summary.semantic_closure_reasons)

    def test_model_usage_claim_requires_cached_count_when_complete(self) -> None:
        fixture = self._failure_fixture("no_response")
        usage = next(row for row in fixture["event_rows"]
                     if row["event_type"] == "model_run_usage_recorded")
        payload = json.loads(usage["safe_payload_json"])
        self.assertIsNone(payload["cached_input_unit_count"])
        payload["usage_complete"] = True
        usage["safe_payload_json"] = canonical_json_bytes(payload).decode("utf-8")
        self._rehash_run(fixture, 0)
        self.assert_semantic_error(fixture, "closure_event_projection_invalid")

    def test_missing_model_usage_rows_are_invalid_even_with_consistent_event_summary(self) -> None:
        fixture = self._fixture()
        run_id = fixture["run_rows"][0]["run_id"]
        fixture["model_call_rows"] = [row for row in fixture["model_call_rows"]
                                      if row["run_id"] != run_id]
        fixture["event_rows"] = [row for row in fixture["event_rows"] if not (
            row["run_id"] == run_id and row["event_type"] == "model_call_recorded"
        )]
        for sequence, row in enumerate(
            [row for row in fixture["event_rows"] if row["run_id"] == run_id], 1
        ):
            row["sequence"] = sequence
            if row["event_type"] == "model_run_usage_recorded":
                payload = json.loads(row["safe_payload_json"])
                payload.update(
                    usage_complete=False, request_count=None,
                    input_unit_count=None, output_unit_count=None,
                    total_unit_count=None, cached_input_unit_count=None,
                    model_call_rows_recorded=0,
                )
                row["safe_payload_json"] = canonical_json_bytes(payload).decode("utf-8")
        self._rehash_run(fixture, 0)
        self.assert_semantic_error(fixture, "closure_event_projection_invalid")

    def test_budget_checks_rejected_response_counters_and_campaign_totals(self) -> None:
        for input_tokens in (4_503_599_627_370_496, 9_007_199_254_740_992):
            with self.subTest(input_tokens=input_tokens):
                fixture = self._fixture(
                    input_tokens=input_tokens, rejected_responses=True
                )
                self.assertEqual(fixture["telemetry"]["runtime_denominator"]["records"], [])
                self.assert_semantic_error(fixture, "closure_budget_invalid")

    def test_pairwise_semantic_priority_uses_first_independent_fault(self) -> None:
        def corrupt(value: _Fixture, layer: str) -> None:
            if layer == "chain":
                value["event_rows"][0]["event_hash"] = "0" * 64
            elif layer == "run_set":
                value["run_rows"][0]["status"] = "unregistered"
            elif layer == "event":
                row = next(row for row in value["event_rows"]
                           if row["event_type"] == "provider_transport_request_sent")
                payload = json.loads(row["safe_payload_json"])
                payload["authorization_grant_sha256"] = _h("wrong-grant")
                row["safe_payload_json"] = canonical_json_bytes(payload).decode("utf-8")
                self._rehash_run(value, 0)
            elif layer == "denominator":
                value["telemetry"]["runtime_denominator"] = None
            elif layer == "outer":
                value["telemetry"]["closure"]["claim_allowed"] = False
            elif layer == "timeline":
                value["model_call_rows"][0]["started_at_utc"] = "2026-09-04T04:00:00Z"

        layers = ("chain", "run_set", "event", "denominator", "outer", "budget", "timeline")
        for higher_index, higher in enumerate(layers):
            for lower in layers[higher_index + 1:]:
                with self.subTest(higher=higher, lower=lower):
                    fixture = self._fixture(
                        input_tokens=4_503_599_627_370_496
                        if "budget" in (higher, lower) else 10
                    )
                    # Hash mutations must happen before the deliberately broken chain.
                    corrupt(fixture, lower)
                    corrupt(fixture, higher)
                    code = {
                        "chain": "closure_audit_chain_invalid",
                        "run_set": "closure_audit_run_set_invalid",
                        "event": "closure_event_projection_invalid",
                        "denominator": "closure_denominator_invalid",
                        "outer": "closure_outer_projection_invalid",
                        "budget": "closure_budget_invalid",
                    }[higher]
                    self.assert_semantic_error(fixture, code)

    def test_mixed_type_chain_sequence_returns_frozen_error(self) -> None:
        fixture = self._fixture()
        fixture["event_rows"][1]["sequence"] = []
        self.assert_semantic_error(fixture, "closure_audit_chain_invalid")

    def test_terminal_record_indices_and_send_truth_table_precede_bad_denominator(self) -> None:
        for field in ("request_index", "response_index"):
            with self.subTest(field=field):
                fixture = self._fixture()
                row = next(row for row in fixture["event_rows"]
                           if row["event_type"] == "model_response_telemetry_recorded")
                payload = json.loads(row["safe_payload_json"])
                payload["completion_record"][field] = 1
                row["safe_payload_json"] = canonical_json_bytes(payload).decode("utf-8")
                self._rehash_run(fixture, 0)
                fixture["telemetry"]["runtime_denominator"] = None
                self.assert_semantic_error(fixture, "closure_event_projection_invalid")

        fixture = self._failure_fixture("outcome_unknown")
        row = next(row for row in fixture["event_rows"]
                   if row["event_type"] == "model_request_outcome_unknown")
        payload = json.loads(row["safe_payload_json"])
        row["event_type"] = "model_request_cancelled"
        payload.update(terminal_kind="cancelled", error_code=None)
        row["safe_payload_json"] = canonical_json_bytes(payload).decode("utf-8")
        self._rehash_run(fixture, 1)
        fixture["telemetry"]["runtime_denominator"] = None
        self.assert_semantic_error(fixture, "closure_event_projection_invalid")

    def test_frozen_error_priority_codes_cover_every_semantic_layer(self) -> None:
        cases: list[tuple[str, _Fixture]] = []

        value = self._fixture()
        value["event_rows"][0]["event_hash"] = "0" * 64
        cases.append(("closure_audit_chain_invalid", value))

        value = self._fixture()
        value["audit_index"]["runs"].reverse()
        cases.append(("closure_audit_run_set_invalid", value))

        value = self._fixture()
        value["audit_index"]["runs"][0][
            "completion_telemetry_event_commitment"
        ]["binding_sha256"] = _h("forged")
        cases.append(("closure_event_projection_invalid", value))

        value = self._fixture()
        value["telemetry"]["runtime_denominator"]["accepted_response_count"] = 1
        cases.append(("closure_denominator_invalid", value))

        value = self._fixture()
        value["telemetry"]["closure"]["claim_allowed"] = False
        cases.append(("closure_outer_projection_invalid", value))

        # Each individual counter is safe and all projections agree; only the
        # accumulated campaign total exceeds the frozen exact-integer bound.
        value = self._fixture(input_tokens=4_503_599_627_370_496)
        cases.append(("closure_budget_invalid", value))

        value = self._fixture()
        value["model_call_rows"][0]["started_at_utc"] = "2026-09-04T04:00:00Z"
        cases.append(("closure_timeline_invalid", value))

        for code, fixture in cases:
            with self.subTest(code=code):
                self.assert_semantic_error(fixture, code)

    def test_legacy_usage_payload_and_record_model_call_tampering_are_rejected(self) -> None:
        value = self._fixture()
        event = next(
            row
            for row in value["event_rows"]
            if row["event_type"] == "model_call_recorded"
        )
        event["event_type"] = "model_response_usage_recorded"
        self._rehash_run(value, 0)
        self.assert_semantic_error(value, "closure_event_projection_invalid")

    def test_run_terminal_registry_and_transport_cap_reason_are_exact(self) -> None:
        for status, error_code in (
            ("failed", "arbitrary_failure"),
            ("failed", "pce_transport_cancelled"),
            ("cancelled", "pce_transport_failed"),
        ):
            value = self._fixture()
            row = value["run_rows"][0]
            row["status"] = status
            row["terminal_error_code"] = error_code
            status_event = next(
                item
                for item in value["event_rows"]
                if item["run_id"] == row["run_id"]
                and item["event_type"] == "run_status_changed"
            )
            payload = json.loads(status_event["safe_payload_json"])
            payload["to"] = status
            payload["error_code"] = error_code
            status_event["safe_payload_json"] = canonical_json_bytes(payload).decode(
                "utf-8"
            )
            self._rehash_run(value, 0)
            with self.subTest(status=status, error_code=error_code):
                self.assert_semantic_error(
                    value, "closure_audit_run_set_invalid"
                )

        for status, error_code in (
            ("failed", "pce_response_timed_out"),
            ("cancelled", "pce_response_cancelled"),
        ):
            value = self._fixture()
            row = value["run_rows"][1]
            row["status"] = status
            row["terminal_error_code"] = error_code
            status_event = next(
                item
                for item in value["event_rows"]
                if item["run_id"] == row["run_id"]
                and item["event_type"] == "run_status_changed"
            )
            payload = json.loads(status_event["safe_payload_json"])
            payload["to"] = status
            payload["error_code"] = error_code
            status_event["safe_payload_json"] = canonical_json_bytes(payload).decode(
                "utf-8"
            )
            self._rehash_run(value, 1)
            with self.subTest(valid_status=status, valid_error_code=error_code):
                summary = self._verify(value)
                self.assertEqual(
                    summary.last_observed_run_terminal_error_code, error_code
                )

        self.assertEqual(
            semantics_module._ledger_reconciliation_reasons(
                SimpleNamespace(
                    ledger_failure_observed=False,
                    transport_retry_or_cap_violation=True,
                )
            ),
            ("transport_retry_or_cap_violation",),
        )

        value = self._fixture()
        row = value["model_call_rows"][0]
        row["input_tokens"] = 11
        event = next(
            item
            for item in value["event_rows"]
            if item["run_id"] == row["run_id"]
            and item["event_type"] == "model_call_recorded"
        )
        payload = json.loads(event["safe_payload_json"])
        payload["input_tokens"] = 11
        event["safe_payload_json"] = canonical_json_bytes(payload).decode("utf-8")
        usage = next(
            item
            for item in value["event_rows"]
            if item["run_id"] == row["run_id"]
            and item["event_type"] == "model_run_usage_recorded"
        )
        payload = json.loads(usage["safe_payload_json"])
        payload["input_unit_count"] = 11
        payload["total_unit_count"] = 13
        usage["safe_payload_json"] = canonical_json_bytes(payload).decode("utf-8")
        self._rehash_run(value, 0)
        self.assert_semantic_error(value, "closure_event_projection_invalid")

        value = self._fixture()
        terminal = next(
            row
            for row in value["event_rows"]
            if row["event_type"] == "model_response_telemetry_recorded"
        )
        payload = json.loads(terminal["safe_payload_json"])
        payload["completion_record"]["request_index"] = 99
        terminal["safe_payload_json"] = canonical_json_bytes(payload).decode("utf-8")
        self._rehash_run(value, 0)
        self.assert_semantic_error(value, "closure_event_projection_invalid")

    def test_mapping_inputs_are_snapshotted_and_module_has_no_io_or_clock(self) -> None:
        fixture = self._fixture()
        summary = self._verify(fixture)
        fixture["audit_index"]["runs"].clear()
        fixture["run_rows"].clear()
        fixture["event_rows"].clear()
        fixture["model_call_rows"].clear()
        self.assertEqual(summary.observed_run_count, 2)

        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        self.assertFalse(
            imports.intersection(
                {
                    "os",
                    "pathlib",
                    "sqlite3",
                    "socket",
                    "subprocess",
                    "httpx",
                    "requests",
                    "openai",
                    "agents",
                }
            )
        )
        attributes = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        self.assertFalse(
            attributes.intersection(
                {
                    "open",
                    "connect",
                    "read_text",
                    "read_bytes",
                    "write_text",
                    "write_bytes",
                    "environ",
                    "now",
                    "utcnow",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
