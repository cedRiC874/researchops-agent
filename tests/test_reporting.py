from __future__ import annotations

import json
import unittest
from pathlib import Path

from researchops.reporting import (
    ReportGenerationError,
    build_structured_evidence_report,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StructuredReportingTests(unittest.TestCase):
    def load_bundle(self) -> dict:
        return json.loads(
            (PROJECT_ROOT / "artifacts" / "phase3" / "analysis_bundle.json").read_text(
                encoding="utf-8"
            )
        )

    def test_claims_are_bound_to_aggregate_evidence(self) -> None:
        report = build_structured_evidence_report(self.load_bundle()).to_dict()

        self.assertEqual(
            report["claims"]["primary"]["evidence_id"], "E-7C87BB6C88EB"
        )
        self.assertEqual(
            report["claim_manifest"][0]["metric_path"],
            "estimates.contrast.adjusted_mean_difference",
        )
        self.assertFalse(report["limitations"]["full_itt_claimed"])
        self.assertNotIn("P0001", report["markdown"])
        self.assertNotIn("participant_id", report["markdown"])

    def test_wrong_contrast_direction_fails_closed(self) -> None:
        bundle = self.load_bundle()
        bundle["evidence"][0]["estimates"]["contrast"]["direction"] = (
            "control - treatment"
        )

        with self.assertRaises(ReportGenerationError) as context:
            build_structured_evidence_report(bundle)

        self.assertEqual(context.exception.code, "report_contrast_direction_invalid")

    def test_missing_sensitivity_evidence_fails_closed(self) -> None:
        bundle = self.load_bundle()
        bundle["evidence"] = [bundle["evidence"][0]]

        with self.assertRaises(ReportGenerationError) as context:
            build_structured_evidence_report(bundle)

        self.assertEqual(context.exception.code, "report_required_evidence_missing")


if __name__ == "__main__":
    unittest.main()
