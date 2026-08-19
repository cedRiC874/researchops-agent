from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from researchops.eval_contracts import (
    EvalContractError,
    EvalTask,
    ExpectedResult,
    NumericExpectation,
    TaskResult,
    load_eval_tasks,
)


def valid_task(index: int = 1) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "task_id": f"EVAL-{index:03d}",
        "category": "data_quality",
        "title": f"任务 {index}",
        "runner": "dataset_profile",
        "input": {"dataset": "synthetic_trial"},
        "expected": {"status": "succeeded", "exact": {"row_count": 240}},
        "tags": ["offline", "deterministic"],
        "expected_outcome": "success",
    }


class EvalContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temp_path = Path(self.temporary_directory.name)

    def write_jsonl(self, rows: list[dict[str, object]], name: str = "tasks.jsonl") -> Path:
        path = self.temp_path / name
        path.write_text(
            "\n".join(
                json.dumps(row, ensure_ascii=False, allow_nan=False) for row in rows
            ),
            encoding="utf-8",
        )
        return path

    def test_loads_exactly_fifty_unique_tasks_by_default(self) -> None:
        tasks = load_eval_tasks(self.write_jsonl([valid_task(i) for i in range(1, 51)]))

        self.assertEqual(len(tasks), 50)
        self.assertEqual(tasks[0].task_id, "EVAL-001")
        self.assertEqual(tasks[-1].task_id, "EVAL-050")

    def test_rejects_wrong_task_count(self) -> None:
        with self.assertRaises(EvalContractError) as context:
            load_eval_tasks(self.write_jsonl([valid_task()]))

        self.assertEqual(context.exception.code, "eval_task_count_mismatch")

    def test_rejects_duplicate_ids(self) -> None:
        rows = [valid_task(i) for i in range(1, 50)] + [valid_task(1)]
        with self.assertRaises(EvalContractError) as context:
            load_eval_tasks(self.write_jsonl(rows))

        self.assertEqual(context.exception.code, "eval_duplicate_task_id")
        self.assertEqual(context.exception.line_number, 50)

    def test_rejects_unknown_schema_and_unknown_fields(self) -> None:
        schema = valid_task()
        schema["schema_version"] = "2.0"
        with self.assertRaises(EvalContractError) as schema_error:
            EvalTask.from_dict(schema)
        self.assertEqual(schema_error.exception.code, "eval_unknown_schema")

        unknown = valid_task()
        unknown["surprise"] = True
        with self.assertRaises(EvalContractError) as field_error:
            EvalTask.from_dict(unknown)
        self.assertEqual(field_error.exception.code, "eval_unknown_field")

        expected_unknown = valid_task()
        expected_unknown["expected"] = {"mystery": 1}
        with self.assertRaises(EvalContractError) as expected_error:
            EvalTask.from_dict(expected_unknown)
        self.assertEqual(expected_error.exception.code, "eval_unknown_field")

        category = valid_task()
        category["category"] = "typo_category"
        with self.assertRaises(EvalContractError) as category_error:
            EvalTask.from_dict(category)
        self.assertEqual(category_error.exception.code, "eval_unknown_category")

    def test_rejects_duplicate_json_object_keys(self) -> None:
        path = self.temp_path / "duplicate-key.jsonl"
        serialized = json.dumps(valid_task(), ensure_ascii=False)
        serialized = serialized.replace(
            '"schema_version": "1.0"',
            '"schema_version": "1.0", "schema_version": "1.0"',
        )
        path.write_text(serialized, encoding="utf-8")

        with self.assertRaises(EvalContractError) as context:
            load_eval_tasks(path, expected_count=1)

        self.assertEqual(context.exception.code, "eval_duplicate_json_key")

    def test_rejects_nan_and_infinity_in_jsonl_and_objects(self) -> None:
        nan_path = self.temp_path / "nan.jsonl"
        nan_path.write_text(
            json.dumps(valid_task(), ensure_ascii=False).replace(
                '"dataset": "synthetic_trial"', '"dataset": NaN'
            ),
            encoding="utf-8",
        )
        with self.assertRaises(EvalContractError) as context:
            load_eval_tasks(nan_path, expected_count=1)
        self.assertEqual(context.exception.code, "eval_non_finite_number")

        with self.assertRaises(EvalContractError) as numeric_error:
            NumericExpectation(path="estimate", value=math.inf)
        self.assertEqual(numeric_error.exception.code, "eval_non_finite_number")

        with self.assertRaises(EvalContractError) as result_error:
            TaskResult(
                task_id="EVAL-001",
                status="succeeded",
                actual={"estimate": math.nan},
            )
        self.assertEqual(result_error.exception.code, "eval_non_finite_number")

    def test_expected_aliases_normalize_to_one_contract(self) -> None:
        expected = ExpectedResult.from_dict(
            {
                "error_code": "missing_column",
                "numeric_tolerance": [
                    {"path": "estimate", "value": 1.2, "atol": 0.01}
                ],
                "required_evidence": ["E-123"],
            }
        )

        self.assertEqual(expected.error_codes, ("missing_column",))
        self.assertEqual(expected.numeric[0].rtol, 0.0)
        self.assertEqual(expected.required_evidence_ids, ("E-123",))

    def test_expected_error_and_approval_contracts_are_explicit(self) -> None:
        expected_error = valid_task()
        expected_error["expected_outcome"] = "expected_error"
        with self.assertRaises(EvalContractError) as error_context:
            EvalTask.from_dict(expected_error)
        self.assertEqual(error_context.exception.code, "eval_missing_expected_error")

        approval = valid_task()
        approval["expected_outcome"] = "approval_required"
        with self.assertRaises(EvalContractError) as approval_context:
            EvalTask.from_dict(approval)
        self.assertEqual(
            approval_context.exception.code, "eval_missing_approval_state"
        )

    def test_task_result_from_dict_is_runner_neutral_and_strict(self) -> None:
        result = TaskResult.from_dict(
            {
                "task_id": "EVAL-001",
                "status": "succeeded",
                "actual": {"estimate": 2.5},
                "tool_call_count": 2,
                "tool_attempt_count": 2,
                "latency_ms": 12.0,
                "cost_usd": 0,
            }
        )
        self.assertEqual(result.actual["estimate"], 2.5)

        with self.assertRaises(EvalContractError) as context:
            TaskResult.from_dict(
                {
                    "task_id": "EVAL-001",
                    "status": "succeeded",
                    "actual": {},
                    "unknown_metric": 1,
                }
            )
        self.assertEqual(context.exception.code, "eval_unknown_field")

    def test_public_input_does_not_leak_golden_canary(self) -> None:
        payload = valid_task()
        payload["expected"] = {
            "status": "succeeded",
            "exact": {"secret": "GOLDEN-CANARY-DO-NOT-LEAK"},
        }
        eval_task = EvalTask.from_dict(payload)

        public_json = json.dumps(eval_task.public_input(), ensure_ascii=False)

        self.assertNotIn("expected", eval_task.public_input())
        self.assertNotIn("expected_outcome", eval_task.public_input())
        self.assertNotIn("GOLDEN-CANARY-DO-NOT-LEAK", public_json)

    def test_unknown_model_price_is_not_reported_as_zero(self) -> None:
        unknown = TaskResult(
            task_id="EVAL-001",
            status="succeeded",
            actual={},
            model_call_count=1,
            priced_model_call_count=0,
            cost_usd=None,
        )
        self.assertIsNone(unknown.cost_usd)

        with self.assertRaises(EvalContractError) as context:
            TaskResult(
                task_id="EVAL-001",
                status="succeeded",
                actual={},
                model_call_count=1,
                priced_model_call_count=0,
                cost_usd=0,
            )
        self.assertEqual(context.exception.code, "eval_inconsistent_cost_metrics")


if __name__ == "__main__":
    unittest.main()
