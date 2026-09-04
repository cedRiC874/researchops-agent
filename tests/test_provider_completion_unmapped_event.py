from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import researchops_completion_telemetry.surface_mapping as surface_mapping
from researchops.audit import (
    COMPLETION_TELEMETRY_TERMINAL_EVENT_TYPES,
    COMPLETION_TELEMETRY_UNMAPPED_EVENT,
    AuditError,
    AuditLedger,
    _event_hash,
    sha256_json,
)
from researchops.completion_telemetry_ledger import (
    LedgerCompletionTelemetrySession,
)
from researchops.phase6_runner import _finalize_runtime_completion_telemetry
from researchops_completion_telemetry.capture import (
    CompletionTelemetryCollector,
    RuntimeDenominatorTracker,
    verify_runtime_denominator_plan,
)
from researchops_completion_telemetry.sanitization import (
    build_completion_record,
    sanitize_completion_capture,
)
from researchops_completion_telemetry.surface_mapping import (
    VerifiedRuntimeCompletionBinding,
    load_and_select_surface_mapping,
)


ROOT = Path(__file__).resolve().parents[1]


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _runtime_binding() -> VerifiedRuntimeCompletionBinding:
    offline = load_and_select_surface_mapping(
        ROOT,
        "deepseek",
        "responses",
        "openai_compatible_responses",
        purpose="offline_validation",
    )
    selection = surface_mapping.VerifiedSurfaceSelection._create(
        surface_mapping._SELECTION_TOKEN,
        purpose="runtime_binding",
        telemetry_schema_sha256=offline.telemetry_schema_sha256,
        mapping=offline.mapping_snapshot(),
        entry={
            "adapter_version": offline.adapter_version,
            "mapping_version": offline.mapping_version,
            "output_counter_comparability": offline.output_counter_comparability,
            "output_counter_path": offline.output_counter_path,
            "runtime_binding_allowed": True,
        },
    )
    return selection.create_runtime_binding()


def _tracker() -> RuntimeDenominatorTracker:
    runtime = _runtime_binding()
    binding = runtime.runtime_snapshot()
    case_ids = ["CASE-UNMAPPED-001"]
    plan = {
        "schema_version": "provider-completion-runtime-denominator-plan/1.0",
        "provider_id": binding["provider_id"],
        "api_surface": binding["api_surface"],
        "transport_id": binding["transport_id"],
        "adapter_version": binding["adapter_version"],
        "telemetry_schema_sha256": binding["telemetry_schema_sha256"],
        "mapping_schema_version": binding["mapping_schema_version"],
        "mapping_version": binding["mapping_version"],
        "mapping_sha256": binding["mapping_sha256"],
        "case_ids": case_ids,
        "case_ids_sha256": _canonical_sha256(case_ids),
        "max_turns_per_case": 2,
        "total_model_request_cap": 2,
        "agents_sdk_retries": 0,
        "http_client_retries": 0,
        "denominator_algorithm": "transport-response-finalization-v1",
        "exact_response_count_preregistered": False,
    }
    verified = verify_runtime_denominator_plan(
        runtime,
        plan,
        preregistration_commitment=_canonical_sha256(plan),
    )
    tracker = CompletionTelemetryCollector.for_runtime(verified)
    if not isinstance(tracker, RuntimeDenominatorTracker):
        raise AssertionError("expected runtime denominator tracker")
    return tracker


def _capture(status: str):
    return sanitize_completion_capture(
        {
            "status": status,
            "incomplete_details": None,
            "usage": {
                "input_tokens": 3,
                "output_tokens": 2,
                "total_tokens": 5,
            },
            "provider_request_id": "req_unmapped_event_test",
            "http_status": 200,
            "requested_output_token_cap": 32,
        },
        normalized_usage={
            "requests": 1,
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "cached_input_tokens": 0,
            "cache_write_tokens": None,
            "reasoning_tokens": None,
        },
    )


class ProviderCompletionUnmappedEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "audit.sqlite3"
        self.ledger = AuditLedger(self.database)
        self.run_id = self.ledger.start_run(
            mode="unmapped-event-test",
            request_summary={"objective": "offline unmapped event verification"},
        )
        self.tracker = _tracker()
        self.bridge = LedgerCompletionTelemetrySession(
            self.tracker.bind_case("CASE-UNMAPPED-001"),
            ledger=self.ledger,
            run_id=self.run_id,
            runtime_plan_binding=self.tracker.plan_binding(),
        )

    def _seal_and_finalize(self) -> dict[str, object]:
        self.tracker.seal_case(
            "CASE-UNMAPPED-001",
            sdk_raw_response_count=1,
            sdk_usage_request_count=1,
            sdk_request_usage_indices_by_response={0: (0,)},
        )
        verification = self.ledger.verify_chain(self.run_id)
        return _finalize_runtime_completion_telemetry(
            self.tracker,
            ledger=self.ledger,
            audit_index=(
                {
                    "task_id": "CASE-UNMAPPED-001",
                    "run_id": self.run_id,
                    "chain_verification": verification.to_dict(),
                    "completion_telemetry_event_commitment": (
                        self.bridge.event_commitment()
                    ),
                },
            ),
            ledger_failure_observed=False,
        )

    def test_unmapped_response_uses_one_atomic_terminal_with_usage(self) -> None:
        handle = self.bridge.begin_attempt()
        terminal = self.bridge.finalize_response_accepted(
            handle, _capture("future_provider_status")
        )
        self.assertEqual(terminal.terminal_kind, "response_accepted")
        events = self.ledger.export_run(self.run_id)["events"]
        terminal_events = [
            event
            for event in events
            if event["event_type"] in COMPLETION_TELEMETRY_TERMINAL_EVENT_TYPES
        ]
        self.assertEqual(len(terminal_events), 1)
        event = terminal_events[0]
        self.assertEqual(event["event_type"], COMPLETION_TELEMETRY_UNMAPPED_EVENT)
        record = event["safe_payload"]["completion_record"]
        self.assertEqual(record["normalized_completion_state"], "unmapped")
        self.assertEqual(record["truncation_signal_source"], "native_status")
        self.assertEqual(record["native_status"]["value"], "future_provider_status")
        self.assertEqual(record["usage"]["normalized"]["output_tokens"], 2)
        self.assertNotIn(
            "model_response_telemetry_recorded",
            [item["event_type"] for item in events],
        )
        commitment = self.bridge.event_commitment()
        self.assertEqual(
            commitment["terminals"],
            [
                {
                    "attempt_index": handle.attempt_index,
                    "event_hash": event["event_hash"],
                }
            ],
        )

        result = self._seal_and_finalize()
        self.assertEqual(result["status"], "recorded")
        self.assertFalse(result["closure"]["claim_allowed"])
        self.assertIn("completion_state_unmapped", result["closure"]["reasons"])
        self.assertEqual(result["ledger_reconciliation"]["reasons"], [])

    def test_recognized_response_keeps_the_recorded_terminal(self) -> None:
        self.bridge.finalize_response_accepted(
            self.bridge.begin_attempt(), _capture("completed")
        )
        event_types = [
            event["event_type"]
            for event in self.ledger.export_run(self.run_id)["events"]
        ]
        self.assertEqual(event_types.count("model_response_telemetry_recorded"), 1)
        self.assertNotIn(COMPLETION_TELEMETRY_UNMAPPED_EVENT, event_types)

    def test_unmapped_reserved_event_cannot_be_spoofed_or_duplicated(self) -> None:
        with self.assertRaises(AuditError) as spoofed:
            self.ledger.append_event(
                self.run_id,
                COMPLETION_TELEMETRY_UNMAPPED_EVENT,
                {},
            )
        self.assertEqual(
            spoofed.exception.code, "audit_completion_reserved_event_type"
        )
        handle = self.bridge.begin_attempt()
        self.bridge.finalize_response_accepted(
            handle, _capture("future_provider_status")
        )
        with self.assertRaises(AuditError) as duplicate:
            self.bridge.finalize_response_accepted(
                handle, _capture("future_provider_status")
            )
        self.assertEqual(
            duplicate.exception.code, "audit_completion_attempt_handle_invalid"
        )

    def test_unmapped_event_type_rejects_a_recognized_record(self) -> None:
        handle = self.bridge.begin_attempt()
        capture = _capture("completed")
        terminal = self.bridge._session.finalize_response_accepted(handle, capture)
        record = build_completion_record(
            terminal._capture._collector_snapshot(),
            binding=self.tracker.runtime_binding(),
            response_index=0,
            request_index=0,
        )
        payload = {
            "schema_version": "provider-completion-ledger-event/1.1",
            "case_id": terminal.case_id,
            "attempt_index": terminal.attempt_index,
            "case_attempt_index": terminal.case_attempt_index,
            "terminal_kind": terminal.terminal_kind,
            "response_index": terminal.response_index,
            "error_code": terminal.error_code,
            "binding": self.tracker.binding_snapshot(),
            "completion_record": record,
        }
        with self.assertRaises(AuditError) as mismatched:
            self.ledger.append_completion_telemetry_event(
                COMPLETION_TELEMETRY_UNMAPPED_EVENT,
                payload,
                capability=self.bridge._capability,
                attempt_handle=handle,
                terminal=terminal,
            )
        self.assertEqual(
            mismatched.exception.code,
            "audit_completion_event_type_state_mismatch",
        )

    def test_recorded_event_type_rejects_an_unmapped_record(self) -> None:
        handle = self.bridge.begin_attempt()
        capture = _capture("future_provider_status")
        terminal = self.bridge._session.finalize_response_accepted(handle, capture)
        record = build_completion_record(
            terminal._capture._collector_snapshot(),
            binding=self.tracker.runtime_binding(),
            response_index=0,
            request_index=0,
        )
        payload = {
            "schema_version": "provider-completion-ledger-event/1.1",
            "case_id": terminal.case_id,
            "attempt_index": terminal.attempt_index,
            "case_attempt_index": terminal.case_attempt_index,
            "terminal_kind": terminal.terminal_kind,
            "response_index": terminal.response_index,
            "error_code": terminal.error_code,
            "binding": self.tracker.binding_snapshot(),
            "completion_record": record,
        }
        with self.assertRaises(AuditError) as mismatched:
            self.ledger.append_completion_telemetry_event(
                "model_response_telemetry_recorded",
                payload,
                capability=self.bridge._capability,
                attempt_handle=handle,
                terminal=terminal,
            )
        self.assertEqual(
            mismatched.exception.code,
            "audit_completion_event_type_state_mismatch",
        )

    def test_wrong_event_schema_version_is_rejected_stably(self) -> None:
        handle = self.bridge.begin_attempt()
        capture = _capture("future_provider_status")
        terminal = self.bridge._session.finalize_response_accepted(handle, capture)
        record = build_completion_record(
            terminal._capture._collector_snapshot(),
            binding=self.tracker.runtime_binding(),
            response_index=0,
            request_index=0,
        )
        payload = {
            "schema_version": "provider-completion-ledger-event/1.0",
            "case_id": terminal.case_id,
            "attempt_index": terminal.attempt_index,
            "case_attempt_index": terminal.case_attempt_index,
            "terminal_kind": terminal.terminal_kind,
            "response_index": terminal.response_index,
            "error_code": terminal.error_code,
            "binding": self.tracker.binding_snapshot(),
            "completion_record": record,
        }
        with self.assertRaises(AuditError) as wrong_version:
            self.ledger.append_completion_telemetry_event(
                COMPLETION_TELEMETRY_UNMAPPED_EVENT,
                payload,
                capability=self.bridge._capability,
                attempt_handle=handle,
                terminal=terminal,
            )
        self.assertEqual(
            wrong_version.exception.code,
            "audit_completion_event_payload_invalid",
        )

    def test_missing_renamed_duplicated_or_tampered_event_fails_closed(self) -> None:
        expected_ledger_reasons = {
            "missing": [
                "model_request_terminal_count_mismatch",
                "accepted_response_event_count_mismatch",
                "ledger_event_commitment_mismatch",
                "ledger_event_payload_mismatch",
            ],
            "renamed": [
                "audit_chain_or_export_invalid",
                "ledger_event_payload_mismatch",
            ],
            "duplicated": [
                "audit_chain_or_export_invalid",
                "model_request_terminal_count_mismatch",
                "accepted_response_event_count_mismatch",
                "ledger_event_commitment_mismatch",
                "ledger_event_payload_mismatch",
            ],
            "tampered": [
                "audit_chain_or_export_invalid",
                "ledger_event_payload_mismatch",
            ],
        }
        for mutation in ("missing", "renamed", "duplicated", "tampered"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "audit.sqlite3"
                ledger = AuditLedger(database)
                run_id = ledger.start_run(
                    mode="unmapped-event-tamper",
                    request_summary={"objective": "offline mutation"},
                )
                tracker = _tracker()
                bridge = LedgerCompletionTelemetrySession(
                    tracker.bind_case("CASE-UNMAPPED-001"),
                    ledger=ledger,
                    run_id=run_id,
                    runtime_plan_binding=tracker.plan_binding(),
                )
                bridge.finalize_response_accepted(
                    bridge.begin_attempt(), _capture("future_provider_status")
                )
                commitment = bridge.event_commitment()
                tracker.seal_case(
                    "CASE-UNMAPPED-001",
                    sdk_raw_response_count=1,
                    sdk_usage_request_count=1,
                    sdk_request_usage_indices_by_response={0: (0,)},
                )
                before = ledger.verify_chain(run_id)
                self.assertTrue(before.valid)

                with closing(sqlite3.connect(database)) as connection:
                    if mutation == "missing":
                        connection.execute("DROP TRIGGER audit_events_no_delete")
                        connection.execute(
                            "DELETE FROM audit_events WHERE run_id = ? AND event_type = ?",
                            (run_id, COMPLETION_TELEMETRY_UNMAPPED_EVENT),
                        )
                    elif mutation in {"renamed", "tampered"}:
                        connection.execute("DROP TRIGGER audit_events_no_update")
                        if mutation == "renamed":
                            connection.execute(
                                "UPDATE audit_events SET event_type = ? "
                                "WHERE run_id = ? AND event_type = ?",
                                (
                                    "model_response_telemetry_recorded",
                                    run_id,
                                    COMPLETION_TELEMETRY_UNMAPPED_EVENT,
                                ),
                            )
                        else:
                            row = connection.execute(
                                "SELECT safe_payload_json FROM audit_events "
                                "WHERE run_id = ? AND event_type = ?",
                                (run_id, COMPLETION_TELEMETRY_UNMAPPED_EVENT),
                            ).fetchone()
                            self.assertIsNotNone(row)
                            payload = json.loads(row[0])
                            payload["completion_record"][
                                "normalized_completion_state"
                            ] = "completed"
                            connection.execute(
                                "UPDATE audit_events SET safe_payload_json = ? "
                                "WHERE run_id = ? AND event_type = ?",
                                (
                                    json.dumps(
                                        payload,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    ),
                                    run_id,
                                    COMPLETION_TELEMETRY_UNMAPPED_EVENT,
                                ),
                            )
                    else:
                        row = connection.execute(
                            "SELECT * FROM audit_events WHERE run_id = ? "
                            "AND event_type = ?",
                            (run_id, COMPLETION_TELEMETRY_UNMAPPED_EVENT),
                        ).fetchone()
                        self.assertIsNotNone(row)
                        connection.execute(
                            "INSERT INTO audit_events "
                            "(run_id, sequence, event_type, occurred_at_utc, actor_kind, "
                            "safe_payload_json, prev_hash, event_hash) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                row[1],
                                row[2] + 1,
                                row[3],
                                row[4],
                                row[5],
                                row[6],
                                row[8],
                                row[8],
                            ),
                        )
                    connection.commit()

                result = _finalize_runtime_completion_telemetry(
                    tracker,
                    ledger=ledger,
                    audit_index=(
                        {
                            "task_id": "CASE-UNMAPPED-001",
                            "run_id": run_id,
                            "chain_verification": before.to_dict(),
                            "completion_telemetry_event_commitment": commitment,
                        },
                    ),
                    ledger_failure_observed=False,
                )
                self.assertFalse(result["closure"]["claim_allowed"])
                self.assertEqual(
                    result["ledger_reconciliation"]["reasons"],
                    expected_ledger_reasons[mutation],
                )

    def test_self_consistent_chain_and_commitment_cannot_hide_type_state_drift(
        self,
    ) -> None:
        self.bridge.finalize_response_accepted(
            self.bridge.begin_attempt(), _capture("future_provider_status")
        )
        original_commitment = self.bridge.event_commitment()
        self.tracker.seal_case(
            "CASE-UNMAPPED-001",
            sdk_raw_response_count=1,
            sdk_usage_request_count=1,
            sdk_request_usage_indices_by_response={0: (0,)},
        )

        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT sequence, occurred_at_utc, actor_kind, safe_payload_json, "
                "prev_hash FROM audit_events WHERE run_id = ? AND event_type = ?",
                (self.run_id, COMPLETION_TELEMETRY_UNMAPPED_EVENT),
            ).fetchone()
            self.assertIsNotNone(row)
            forged_event_type = "model_response_telemetry_recorded"
            forged_event_hash = _event_hash(
                run_id=self.run_id,
                sequence=row[0],
                event_type=forged_event_type,
                occurred_at_utc=row[1],
                actor_kind=row[2],
                safe_payload_json=row[3],
                prev_hash=row[4],
            )
            connection.execute("DROP TRIGGER audit_events_no_update")
            connection.execute(
                "UPDATE audit_events SET event_type = ?, event_hash = ? "
                "WHERE run_id = ? AND event_type = ?",
                (
                    forged_event_type,
                    forged_event_hash,
                    self.run_id,
                    COMPLETION_TELEMETRY_UNMAPPED_EVENT,
                ),
            )
            connection.commit()

        verification = self.ledger.verify_chain(self.run_id)
        self.assertTrue(verification.valid)
        forged_commitment = dict(original_commitment)
        forged_commitment["terminals"] = [
            {
                "attempt_index": original_commitment["terminals"][0][
                    "attempt_index"
                ],
                "event_hash": forged_event_hash,
            }
        ]
        commitment_body = dict(forged_commitment)
        commitment_body.pop("commitment_sha256")
        forged_commitment["commitment_sha256"] = sha256_json(commitment_body)

        result = _finalize_runtime_completion_telemetry(
            self.tracker,
            ledger=self.ledger,
            audit_index=(
                {
                    "task_id": "CASE-UNMAPPED-001",
                    "run_id": self.run_id,
                    "chain_verification": verification.to_dict(),
                    "completion_telemetry_event_commitment": forged_commitment,
                },
            ),
            ledger_failure_observed=False,
        )
        self.assertFalse(result["closure"]["claim_allowed"])
        self.assertEqual(
            result["ledger_reconciliation"]["reasons"],
            ["ledger_event_payload_mismatch"],
        )


if __name__ == "__main__":
    unittest.main()
