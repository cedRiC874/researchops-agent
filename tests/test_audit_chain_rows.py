from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from researchops.audit import AuditLedger, verify_audit_chain_rows


class AuditChainRowsTests(unittest.TestCase):
    def test_pure_rows_verifier_matches_ledger_and_rejects_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "audit.sqlite3"
            ledger = AuditLedger(database)
            run_id = ledger.start_run(
                mode="test",
                request_summary={"safe": True},
                run_id="RUN-AUDIT-CHAIN-ROWS",
            )
            ledger.append_event(run_id, "safe_event", {"value": 1})
            ledger.set_run_status(run_id, "completed")
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                rows = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM audit_events WHERE run_id = ? ORDER BY sequence",
                        (run_id,),
                    )
                ]
            finally:
                connection.close()
            self.assertEqual(
                verify_audit_chain_rows(run_id, rows),
                ledger.verify_chain(run_id),
            )
            for field, value, expected in (
                ("run_id", "RUN-OTHER", "audit_event_row_invalid"),
                ("sequence", 2, "audit_sequence_gap"),
                ("prev_hash", "f" * 64, "audit_prev_hash_mismatch"),
                ("event_hash", "f" * 64, "audit_event_hash_mismatch"),
            ):
                forged = copy.deepcopy(rows)
                forged[0][field] = value
                result = verify_audit_chain_rows(run_id, forged)
                self.assertFalse(result.valid)
                self.assertEqual(result.error_code, expected)

    def test_malformed_arguments_return_stable_invalid_results(self) -> None:
        self.assertEqual(
            verify_audit_chain_rows("", []).error_code,
            "audit_run_id_invalid",
        )
        self.assertEqual(
            verify_audit_chain_rows("RUN", object()).error_code,  # type: ignore[arg-type]
            "audit_event_rows_invalid",
        )
        self.assertEqual(
            verify_audit_chain_rows("RUN", [object()]).error_code,  # type: ignore[list-item]
            "audit_event_row_invalid",
        )


if __name__ == "__main__":
    unittest.main()
