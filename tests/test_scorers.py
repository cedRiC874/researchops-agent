from __future__ import annotations

import unittest

from researchops.eval_contracts import (
    EvalContractError,
    EvalTask,
    ExpectedResult,
    NumericExpectation,
    TaskResult,
)
from researchops.scorers import build_eval_report, resolve_json_path, score_task


def task(
    task_id: str,
    *,
    expected: ExpectedResult | None = None,
    expected_outcome: str = "success",
) -> EvalTask:
    return EvalTask(
        schema_version="1.0",
        task_id=task_id,
        category="analysis_evidence",
        title="test",
        runner="analysis_evidence",
        input={},
        expected=expected or ExpectedResult(status="succeeded"),
        tags=(),
        expected_outcome=expected_outcome,
    )


class ScorerTests(unittest.TestCase):
    def test_scores_exact_numeric_evidence_and_operational_assertions(self) -> None:
        eval_task = task(
            "EVAL-001",
            expected=ExpectedResult(
                status="succeeded",
                exact={"sample.included": 212, "/test/name": "welch"},
                numeric=(
                    NumericExpectation(
                        path="evidence[0].estimate",
                        value=-5.6,
                        atol=0.05,
                    ),
                ),
                required_evidence_ids=("E-ONE", "E-TWO"),
                approval_state="approved",
                attempt_count=2,
                handler_invocations=1,
                safety_violation=False,
            ),
        )
        result = TaskResult(
            task_id="EVAL-001",
            status="succeeded",
            actual={
                "sample": {"included": 212},
                "test": {"name": "welch"},
                "evidence": [
                    {"estimate": -5.606, "evidence_id": "E-ONE"},
                    {"evidence_id": "E-TWO"},
                ],
            },
            approval_state="approved",
            attempt_count=2,
            handler_invocations=1,
        )

        score = score_task(eval_task, result)

        self.assertTrue(score.passed)
        self.assertEqual(score.evidence_citations_matched, 2)
        self.assertEqual(score.failures, ())

    def test_reports_failed_paths_without_throwing(self) -> None:
        eval_task = task(
            "EVAL-001",
            expected=ExpectedResult(
                status="succeeded",
                exact={"sample.included": 212},
                numeric=(NumericExpectation("estimate", 1.0, atol=0.01),),
                required_evidence_ids=("E-MISSING",),
            ),
        )
        result = TaskResult(
            task_id="EVAL-001",
            status="succeeded",
            actual={"sample": {"included": 211}, "estimate": "not numeric"},
        )

        score = score_task(eval_task, result)

        self.assertFalse(score.passed)
        self.assertIn("exact", score.failures)
        self.assertIn("numeric", score.failures)
        self.assertIn("required_evidence", score.failures)

    def test_expected_error_is_not_an_unexpected_tool_error(self) -> None:
        eval_task = task(
            "EVAL-ERR",
            expected=ExpectedResult(
                status="failed", error_codes=("transient_timeout",)
            ),
            expected_outcome="expected_error",
        )
        result = TaskResult(
            task_id="EVAL-ERR",
            status="failed",
            actual={},
            error_codes=("transient_timeout",),
            tool_call_count=1,
            tool_attempt_count=1,
            tool_error_codes=("transient_timeout",),
            latency_ms=10,
        )

        score = score_task(eval_task, result)
        report = build_eval_report([eval_task], [result])

        self.assertTrue(score.passed)
        self.assertEqual(score.unexpected_tool_error_count, 0)
        self.assertEqual(report.unexpected_tool_error_rate, 0.0)

    def test_aggregate_metrics_have_documented_denominators_and_percentiles(self) -> None:
        tasks = [task("EVAL-1"), task("EVAL-2"), task("EVAL-3")]
        results = [
            TaskResult(
                task_id="EVAL-1",
                status="succeeded",
                actual={},
                tool_call_count=2,
                tool_attempt_count=2,
                latency_ms=10,
                cost_usd=0.01,
                model_call_count=1,
                priced_model_call_count=1,
                evidence_ids=("E-1",),
            ),
            TaskResult(
                task_id="EVAL-2",
                status="failed",
                actual={},
                error_codes=("bad",),
                tool_call_count=1,
                tool_attempt_count=1,
                tool_error_codes=("bad",),
                latency_ms=20,
                cost_usd=0.02,
                model_call_count=1,
                priced_model_call_count=1,
            ),
            TaskResult(
                task_id="EVAL-3",
                status="succeeded",
                actual={},
                tool_call_count=1,
                tool_attempt_count=1,
                latency_ms=30,
                cost_usd=0.03,
                model_call_count=1,
                priced_model_call_count=1,
                safety_violation=True,
            ),
        ]
        tasks[0] = task(
            "EVAL-1",
            expected=ExpectedResult(
                status="succeeded", required_evidence_ids=("E-1",)
            ),
        )
        tasks[1] = task(
            "EVAL-2",
            expected=ExpectedResult(
                status="succeeded", required_evidence_ids=("E-2",)
            ),
        )

        report = build_eval_report(tasks, results)

        self.assertEqual(report.task_count, 3)
        self.assertEqual(report.passed_count, 1)
        self.assertAlmostEqual(report.success_rate, 1 / 3)
        self.assertEqual(report.unexpected_tool_error_count, 1)
        self.assertAlmostEqual(report.unexpected_tool_error_rate, 1 / 4)
        self.assertAlmostEqual(report.gross_tool_error_rate, 1 / 4)
        self.assertAlmostEqual(report.safety_violation_rate, 1 / 3)
        self.assertAlmostEqual(report.evidence_citation_accuracy, 0.5)
        self.assertEqual(report.p50_latency_ms, 20.0)
        self.assertEqual(report.p95_latency_ms, 29.0)
        self.assertAlmostEqual(report.total_cost_usd, 0.06)
        self.assertAlmostEqual(report.mean_cost_usd, 0.02)
        self.assertEqual(report.cost_coverage, 1.0)
        self.assertEqual(report.cost_status, "complete")
        self.assertEqual(
            report.category_success_rates, {"analysis_evidence": 1 / 3}
        )
        self.assertEqual(report.to_dict()["task_scores"][0]["task_id"], "EVAL-1")

    def test_missing_duplicate_or_unknown_results_fail_closed(self) -> None:
        eval_task = task("EVAL-1")
        result = TaskResult(task_id="EVAL-1", status="succeeded", actual={})

        with self.assertRaises(EvalContractError) as missing:
            build_eval_report([eval_task], [])
        self.assertEqual(missing.exception.code, "eval_result_coverage_mismatch")

        with self.assertRaises(EvalContractError) as duplicate:
            build_eval_report([eval_task], [result, result])
        self.assertEqual(duplicate.exception.code, "eval_duplicate_result_id")

        unknown_result = TaskResult(
            task_id="EVAL-X", status="succeeded", actual={}
        )
        with self.assertRaises(EvalContractError) as unknown:
            build_eval_report([eval_task], [unknown_result])
        self.assertEqual(unknown.exception.code, "eval_result_coverage_mismatch")

    def test_json_path_supports_pointer_dot_and_array_syntax(self) -> None:
        payload = {"a": {"b": [{"c/d": 7}]}}

        self.assertEqual(resolve_json_path(payload, "a.b[0].c/d"), 7)
        self.assertEqual(resolve_json_path(payload, "/a/b/0/c~1d"), 7)
        self.assertIsNone(resolve_json_path(payload, "missing", default=None))

    def test_boolean_does_not_pass_numeric_or_integer_exact_assertion(self) -> None:
        eval_task = task(
            "EVAL-BOOL",
            expected=ExpectedResult(
                status="succeeded",
                exact={"count": 1},
                numeric=(NumericExpectation("estimate", 1.0),),
            ),
        )
        result = TaskResult(
            task_id="EVAL-BOOL",
            status="succeeded",
            actual={"count": True, "estimate": True},
        )

        score = score_task(eval_task, result)
        self.assertFalse(score.exact_match)
        self.assertFalse(score.numeric_match)

    def test_expected_tool_errors_are_consumed_as_a_multiset(self) -> None:
        eval_task = task(
            "EVAL-RETRY",
            expected=ExpectedResult(
                status="succeeded",
                tool_error_codes=("transient_timeout",),
                attempt_count=2,
                handler_invocations=1,
            ),
        )
        expected_retry = TaskResult(
            task_id="EVAL-RETRY",
            status="succeeded",
            actual={},
            tool_call_count=1,
            tool_attempt_count=2,
            tool_error_codes=("transient_timeout",),
            attempt_count=2,
            handler_invocations=1,
        )
        excessive_retry = TaskResult(
            task_id="EVAL-RETRY",
            status="succeeded",
            actual={},
            tool_call_count=1,
            tool_attempt_count=3,
            tool_error_codes=("transient_timeout", "transient_timeout"),
            attempt_count=2,
            handler_invocations=1,
        )

        expected_score = score_task(eval_task, expected_retry)
        excessive_score = score_task(eval_task, excessive_retry)

        self.assertTrue(expected_score.passed)
        self.assertFalse(excessive_score.passed)
        self.assertEqual(excessive_score.unexpected_tool_error_count, 1)

    def test_unknown_cost_has_explicit_coverage_instead_of_false_zero(self) -> None:
        eval_task = task("EVAL-COST")
        result = TaskResult(
            task_id="EVAL-COST",
            status="succeeded",
            actual={},
            model_call_count=1,
            cost_usd=None,
        )

        report = build_eval_report([eval_task], [result])

        self.assertEqual(report.cost_status, "unavailable")
        self.assertEqual(report.cost_coverage, 0.0)
        self.assertIsNone(report.total_cost_usd)
        self.assertIsNone(report.mean_cost_usd)


if __name__ == "__main__":
    unittest.main()
