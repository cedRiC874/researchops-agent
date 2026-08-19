from __future__ import annotations

import unittest
from pathlib import Path

from researchops.eval_runner import validate_eval_suite


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class EvaluationRunnerTests(unittest.TestCase):
    def test_standard_suite_has_fixed_coverage(self) -> None:
        result = validate_eval_suite(PROJECT_ROOT / "evals" / "tasks.jsonl")

        self.assertEqual(result["task_count"], 50)
        self.assertEqual(
            result["category_counts"],
            {
                "analysis_evidence": 12,
                "approval_security": 6,
                "data_quality": 10,
                "method_selection": 10,
                "report_evidence": 4,
                "tool_resilience": 8,
            },
        )
        self.assertGreater(result["tag_counts"]["adversarial"], 0)
        self.assertGreater(result["tag_counts"]["typical"], 0)


if __name__ == "__main__":
    unittest.main()
