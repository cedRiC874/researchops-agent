"""Offline SQLite writes with test-created runtime bindings, not admission proof."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from researchops.audit import AuditError, AuditLedger, COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION
from researchops.completion_telemetry_ledger import LedgerCompletionTelemetrySession
from researchops_completion_telemetry.capture import CompletionTelemetryCollector
from researchops_completion_telemetry.sanitization import build_completion_record
from researchops_completion_timing.contract import commitment, digest, TimingContractError
from researchops_completion_timing.ledger_event import EVENT_VERSION, load_ledger_contract, prepare_timed_terminal
from researchops_external_closure.primitives import canonical_json_bytes
from tests.test_completion_telemetry_ledger import _test_runtime_binding, _plan_binding, _capture


class TimedLedgerTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.socket", side_effect=AssertionError("network forbidden"))
        self.network.start(); self.addCleanup(self.network.stop)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "audit.sqlite3"
        self.ledger = AuditLedger(self.path)
        self.run_id = self.ledger.start_run(mode="offline-timed-ledger", request_summary={"purpose": "synthetic fixture"})
        self.runtime = _test_runtime_binding()
        self.case = "PCECASE-" + "A" * 32
        self.plan = _plan_binding(self.runtime, case_ids=(self.case,))
        self.tracker = CompletionTelemetryCollector.for_runtime(self.plan)
        self.session = self.tracker.bind_case(self.case)
        self.bridge = LedgerCompletionTelemetrySession(self.session, ledger=self.ledger, run_id=self.run_id, runtime_plan_binding=self.plan)
        self.expected = {"plan_commitment_sha256": "1" * 64, "execution_binding_sha256": "2" * 64,
                         "clock_domain_id": "PCECLOCK-" + "A" * 32}

    def fixture(self, kind="response_accepted", status="completed"):
        handle = self.bridge.begin_attempt()
        if kind == "response_accepted":
            terminal = self.session.finalize_response_accepted(handle, _capture(status))
        elif kind == "cancelled":
            terminal = self.session.finalize_cancelled(handle)
        else:
            terminal = getattr(self.session, "finalize_" + kind)(handle, "provider_failure")
        payload = dict(schema_version=COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION,
            case_id=self.case, attempt_index=terminal.attempt_index, case_attempt_index=terminal.case_attempt_index,
            terminal_kind=kind, response_index=terminal.response_index, error_code=terminal.error_code,
            binding=self.runtime.runtime_snapshot())
        if kind == "response_accepted":
            payload["completion_record"] = build_completion_record(terminal._capture._collector_snapshot(),
                binding=self.runtime, response_index=terminal.response_index, request_index=terminal.attempt_index)
        record = payload.get("completion_record")
        sent = kind in {"response_accepted", "response_rejected", "http_error", "outcome_unknown"}
        segment = dict(schema_version="provider-completion-timing-segment/1.0", document_type="completion_timing_segment",
            **self.expected, case_handle=self.case, attempt_index=handle.attempt_index,
            case_attempt_index=handle.case_attempt_index, request_index=handle.attempt_index,
            response_index=terminal.response_index, terminal_kind=kind, attempt_started_ns=10,
            send_started_ns=20 if sent else None, transport_terminal_ns=30,
            raw_cleanup_terminal_ns=40 if sent else None, raw_cleanup_status="completed" if sent else "not_applicable",
            lifetime_ended_ns=40 if sent else 30,
            completion_record_sha256=digest(canonical_json_bytes(record)) if record else None,
            usage_sha256=digest(canonical_json_bytes(record["usage"])) if record else None)
        segment["segment_commitment_sha256"] = commitment("segment", segment)
        event = {"response_accepted": "model_response_telemetry_recorded", "response_rejected": "model_response_telemetry_rejected",
                 "http_error": "model_request_http_error", "no_response": "model_request_no_response",
                 "cancelled": "model_request_cancelled", "outcome_unknown": "model_request_outcome_unknown"}[kind]
        if record and record["normalized_completion_state"] == "unmapped":
            event = "provider_completion_mapping_unmapped"
        return handle, terminal, event, payload, segment

    def write(self, values, **overrides):
        handle, terminal, event, payload, segment = values
        arguments = dict(capability=self.bridge._capability, attempt_handle=handle, terminal=terminal,
                         segment_bytes=canonical_json_bytes(segment), expected_timing_binding=self.expected)
        arguments.update(overrides)
        return AuditLedger.append_timed_completion_telemetry_event(self.ledger, event, payload, **arguments)

    def rows(self):
        return self.ledger.export_run(self.run_id)["events"]

    def test_frozen_contract_and_same_terminal_row_bind_record_usage_and_segment(self):
        protocol = load_ledger_contract()
        self.assertEqual(protocol["event_schema_version"], EVENT_VERSION)
        self.assertFalse(protocol["current_closure_claim_allowed"])
        values = self.fixture()
        before = len(self.rows())
        event_hash = self.write(values)
        rows = self.rows()
        self.assertEqual(len(rows), before + 1)
        terminal = rows[-1]
        self.assertEqual(terminal["event_hash"], event_hash)
        payload = terminal["safe_payload"]
        self.assertEqual(payload["schema_version"], EVENT_VERSION)
        self.assertEqual(payload["timing"]["segment_commitment_sha256"], values[-1]["segment_commitment_sha256"])
        self.assertEqual(payload["completion_record"], values[-2]["completion_record"])
        self.assertTrue(self.ledger.verify_chain(self.run_id).valid)
        self.assertEqual(rows[-2]["safe_payload"]["schema_version"], COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION)

    def test_failure_terminal_has_no_record_or_usage_digest(self):
        values = self.fixture("no_response")
        self.write(values)
        self.assertNotIn("completion_record", self.rows()[-1]["safe_payload"])
        self.assertIsNone(values[-1]["usage_sha256"])
        self.assertTrue(self.ledger.verify_chain(self.run_id).valid)

    def test_all_six_terminal_kinds_use_the_versioned_path_without_changing_taxonomy(self):
        for kind in ("response_accepted", "response_rejected", "http_error", "no_response", "cancelled", "outcome_unknown"):
            with self.subTest(kind=kind):
                isolated = TimedLedgerTests()
                isolated.setUp()
                try:
                    values = isolated.fixture(kind)
                    isolated.write(values)
                    payload = isolated.rows()[-1]["safe_payload"]
                    self.assertEqual(payload["terminal_kind"], kind)
                    self.assertEqual(payload["schema_version"], EVENT_VERSION)
                    self.assertEqual("completion_record" in payload, kind == "response_accepted")
                    self.assertTrue(isolated.ledger.verify_chain(isolated.run_id).valid)
                finally:
                    isolated.doCleanups()

    def test_unmapped_keeps_its_event_type_and_does_not_become_a_completion_claim(self):
        values = self.fixture(status="new_provider_status")
        self.assertEqual(values[2], "provider_completion_mapping_unmapped")
        self.write(values)
        event = self.rows()[-1]
        self.assertEqual(event["event_type"], "provider_completion_mapping_unmapped")
        self.assertEqual(event["safe_payload"]["completion_record"]["normalized_completion_state"], "unmapped")
        self.assertNotIn("current_closure_claim_allowed", event["safe_payload"])

    def test_caller_mutation_during_segment_validation_does_not_change_written_record(self):
        from researchops_completion_timing import ledger_event
        values = self.fixture()
        original = copy.deepcopy(values[-2]["completion_record"])
        validate = ledger_event.validate_segment_link
        def mutate_caller_then_validate(*args, **kwargs):
            values[-2]["completion_record"]["usage"]["normalized"]["output_tokens"] = 999
            return validate(*args, **kwargs)
        with patch.object(ledger_event, "validate_segment_link", side_effect=mutate_caller_then_validate):
            self.write(values)
        self.assertEqual(self.rows()[-1]["safe_payload"]["completion_record"], original)

    def test_original_append_rejects_new_version_and_new_append_rejects_preextended_input(self):
        values = self.fixture()
        handle, terminal, event, payload, segment = values
        extended = prepare_timed_terminal(event, payload, runtime_binding=self.runtime,
            binding=self.runtime.runtime_snapshot(), segment_bytes=canonical_json_bytes(segment), expected_timing_binding=self.expected)
        before = len(self.rows())
        with self.assertRaises(AuditError):
            AuditLedger.append_completion_telemetry_event(self.ledger, event, extended,
                capability=self.bridge._capability, attempt_handle=handle, terminal=terminal)
        with self.assertRaises(AuditError):
            self.write((handle, terminal, event, extended, segment))
        self.assertEqual(len(self.rows()), before)

    def test_missing_or_foreign_capability_and_duplicate_terminal_are_rejected(self):
        values = self.fixture()
        before = len(self.rows())
        with self.assertRaisesRegex(AuditError, "capability"):
            self.write(values, capability=object())
        with self.assertRaises(AuditError):
            self.write(values, attempt_handle=object())
        self.assertEqual(len(self.rows()), before)
        self.write(values)
        with self.assertRaises(AuditError):
            self.write(values)
        self.assertEqual(len(self.rows()), before + 1)

    def test_binding_indices_record_and_usage_mismatches_reject_even_after_rehash(self):
        values = self.fixture()
        before = len(self.rows())
        changes = {"usage_sha256": "3" * 64, "completion_record_sha256": "3" * 64,
                   "plan_commitment_sha256": "3" * 64, "execution_binding_sha256": "3" * 64,
                   "clock_domain_id": "PCECLOCK-" + "B" * 32, "attempt_index": 1,
                   "case_attempt_index": 1, "request_index": 1, "response_index": 1,
                   "case_handle": "PCECASE-" + "B" * 32}
        for key, value in changes.items():
            with self.subTest(field=key):
                segment = copy.deepcopy(values[-1]); segment[key] = value
                segment["segment_commitment_sha256"] = commitment("segment", segment)
                with self.assertRaises(TimingContractError):
                    self.write((*values[:-1], segment))
                self.assertEqual(len(self.rows()), before)

    def test_unhashed_segment_tamper_and_unknown_fields_are_not_discarded(self):
        values = self.fixture()
        segment = copy.deepcopy(values[-1]); segment["attempt_started_ns"] = 11
        with self.assertRaisesRegex(TimingContractError, "timing_commitment_mismatch"):
            self.write((*values[:-1], segment))
        payload = copy.deepcopy(values[-2]); payload["unexpected"] = "ordinary"
        with self.assertRaises(AuditError):
            self.write((*values[:3], payload, values[-1]))

    def test_privacy_scanning_happens_before_sqlite_append(self):
        values = self.fixture()
        before = len(self.rows())
        for secret in ("sk-FAKECANARY12345678", "Authorization: Bearer fake", "C:\\Users\\synthetic\\private",
                       "Traceback (most recent call last):"):
            with self.subTest(kind=secret.split(":")[0]):
                segment = copy.deepcopy(values[-1]); segment["extra"] = secret
                with self.assertRaisesRegex(TimingContractError, "timing_sensitive_content"):
                    self.write((*values[:-1], segment))
                self.assertEqual(len(self.rows()), before)
        with self.assertRaisesRegex(TimingContractError, "timing_sensitive_content"):
            self.write(values, sensitive_canaries=(self.expected["clock_domain_id"].encode(),))
        self.assertEqual(len(self.rows()), before)

    def test_timing_field_database_tamper_breaks_hash_chain(self):
        self.write(self.fixture())
        self.assertTrue(self.ledger.verify_chain(self.run_id).valid)
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute("SELECT sequence,safe_payload_json FROM audit_events WHERE run_id=? ORDER BY sequence DESC LIMIT 1", (self.run_id,)).fetchone()
            payload = json.loads(row[1]); payload["timing"]["clock_domain_id"] = "PCECLOCK-" + "B" * 32
            parameters = (canonical_json_bytes(payload).decode(), self.run_id, row[0])
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute("UPDATE audit_events SET safe_payload_json=? WHERE run_id=? AND sequence=?", parameters)
            # Only this test-owned temporary DB: bypass the trigger to simulate
            # external file tampering, then verify the independent hash defense.
            connection.execute("DROP TRIGGER audit_events_no_update")
            connection.execute("UPDATE audit_events SET safe_payload_json=? WHERE run_id=? AND sequence=?", parameters)
            connection.commit()
        self.assertFalse(self.ledger.verify_chain(self.run_id).valid)


if __name__ == "__main__":
    unittest.main()
