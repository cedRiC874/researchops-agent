from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from researchops.audit import AuditLedger
from researchops.eval_contracts import load_eval_tasks
from researchops.eval_scenarios import OfflineScenarioExecutor


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class OfflineScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.ledger = AuditLedger(root / "audit.sqlite3")
        self.executor = OfflineScenarioExecutor(
            project_root=PROJECT_ROOT,
            workspace=root / "work",
            ledger=self.ledger,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start_run(self, task_id: str) -> str:
        run_id = "RUN-TEST-" + task_id
        self.ledger.start_run(
            mode="test", run_id=run_id, request_summary={"task_id": task_id}
        )
        return run_id

    def test_constant_column_fixture_does_not_add_duplicate_warning(self) -> None:
        run_id = self.start_run("DQ-006")
        result = self.executor.execute(
            runner="dataset_profile",
            input_payload={"scenario": "constant_column"},
            task_id="DQ-006",
            run_id=run_id,
        )

        self.assertEqual(result.actual["warnings"][0]["code"], "constant_column")
        self.assertEqual(result.actual["columns"][0]["unique_count"], 1)
        self.assertTrue(self.ledger.verify_chain(run_id).valid)

    def test_transient_fault_has_attempt_level_oracle(self) -> None:
        run_id = self.start_run("TR-001")
        result = self.executor.execute(
            runner="tool_resilience",
            input_payload={
                "scenario": "transient_then_success",
                "failure_count": 1,
                "max_attempts": 3,
            },
            task_id="TR-001",
            run_id=run_id,
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.tool_attempt_count, 2)
        self.assertEqual(
            result.tool_error_codes, ["fixture_temporarily_unavailable"]
        )
        self.assertEqual(result.handler_invocations, 2)
        self.assertTrue(self.ledger.verify_chain(run_id).valid)

    def test_executor_receives_public_input_without_goldens(self) -> None:
        task = load_eval_tasks(PROJECT_ROOT / "evals" / "tasks.jsonl")[0]
        public = task.public_input()

        self.assertNotIn("expected", public)
        self.assertNotIn("expected_outcome", public)
        self.assertEqual(public["input"]["scenario"], "synthetic_profile")


if __name__ == "__main__":
    unittest.main()
