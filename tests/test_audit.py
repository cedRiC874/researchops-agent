from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from researchops.audit import (
    COMPLETION_TELEMETRY_RESERVED_EVENT_TYPES,
    AuditError,
    AuditLedger,
    canonical_json,
    safe_audit_value,
)


class AuditLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "audit.sqlite3"
        self.now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        self.ledger = AuditLedger(self.database_path, clock=lambda: self.now)

    def test_hash_chain_is_valid_and_events_are_immutable(self) -> None:
        run_id = self.ledger.start_run(
            mode="test", request_summary={"objective": "aggregate analysis"}
        )
        self.ledger.append_event(run_id, "test_event", {"value": 3})

        verification = self.ledger.verify_chain(run_id)
        self.assertTrue(verification.valid)
        self.assertEqual(verification.event_count, 2)

        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE audit_events SET event_type = 'tampered' WHERE run_id = ?",
                        (run_id,),
                    )

    def test_chain_verifier_detects_database_tampering(self) -> None:
        run_id = self.ledger.start_run(mode="test", request_summary={"objective": "x"})
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute("DROP TRIGGER audit_events_no_update")
                connection.execute(
                    "UPDATE audit_events SET safe_payload_json = '{}' WHERE run_id = ?",
                    (run_id,),
                )
        verification = self.ledger.verify_chain(run_id)
        self.assertFalse(verification.valid)
        self.assertEqual(verification.error_code, "audit_event_hash_mismatch")

    def test_defensive_scrubber_removes_secrets_paths_and_row_ids(self) -> None:
        payload = safe_audit_value(
            {
                "api_key": "sk-secret-canary-123456",
                "input_tokens": "not-a-metric-secret",
                "message": r"participant P0001 at C:\Users\analyst\study.csv",
                "rows": [{"participant_id": "P0001"}],
            }
        )
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("sk-secret", text)
        self.assertNotIn("not-a-metric-secret", text)
        self.assertNotIn("P0001", text)
        self.assertNotIn(r"C:\Users\analyst", text)
        self.assertIn("[ROW_DATA_OMITTED]", text)

    def test_canonical_json_is_order_stable_and_rejects_non_finite(self) -> None:
        self.assertEqual(canonical_json({"b": 2, "a": 1}), canonical_json({"a": 1, "b": 2}))
        with self.assertRaises(Exception):
            canonical_json({"value": float("nan")})

    def test_model_usage_is_recorded_without_prompt_or_response(self) -> None:
        run_id = self.ledger.start_run(mode="sdk", request_summary={"objective": "summary"})
        self.ledger.record_model_call(
            run_id,
            provider="openai",
            model="fixture-model",
            started_at_utc=self.now.isoformat(),
            latency_ms=123.4,
            input_tokens=100,
            output_tokens=20,
            cached_tokens=40,
            cost_usd=None,
            outcome="succeeded",
        )
        exported = self.ledger.export_run(run_id)
        self.assertEqual(len(exported["model_calls"]), 1)
        self.assertEqual(exported["model_calls"][0]["input_tokens"], 100)
        self.assertEqual(exported["model_calls"][0]["output_tokens"], 20)
        self.assertEqual(exported["model_calls"][0]["cached_tokens"], 40)
        text = json.dumps(exported, ensure_ascii=False)
        self.assertNotIn("prompt", text.lower())
        self.assertNotIn("response", text.lower())

    def test_generic_append_rejects_completion_telemetry_reserved_events(self) -> None:
        run_id = self.ledger.start_run(
            mode="test", request_summary={"objective": "reserved events"}
        )
        for event_type in COMPLETION_TELEMETRY_RESERVED_EVENT_TYPES:
            with self.subTest(event_type=event_type):
                with self.assertRaises(AuditError) as caught:
                    self.ledger.append_event(run_id, event_type, {})
                self.assertEqual(
                    caught.exception.code,
                    "audit_completion_reserved_event_type",
                )


if __name__ == "__main__":
    unittest.main()
