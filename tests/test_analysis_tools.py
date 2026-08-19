from __future__ import annotations

import json
import unittest
from pathlib import Path

import pandas as pd

from researchops.analysis_tools import AnalysisExecutionError, run_ancova, run_welch_t_test
from researchops.contracts import ResearchDesign
from researchops.data_quality import profile_csv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = PROJECT_ROOT / "data" / "synthetic_trial.csv"
DESIGN_PATH = PROJECT_ROOT / "data" / "synthetic_trial_design.json"


class AnalysisToolGoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.frame = pd.read_csv(DATA_PATH, encoding="utf-8-sig", low_memory=False)
        cls.profile = profile_csv(DATA_PATH)
        cls.design = ResearchDesign.from_dict(
            json.loads(DESIGN_PATH.read_text(encoding="utf-8"))
        )

    def test_welch_matches_golden_values(self) -> None:
        result = run_welch_t_test(self.frame, self.profile, self.design)
        contrast = result.estimates["contrast"]

        self.assertEqual(result.sample_flow.source_rows, 240)
        self.assertEqual(result.sample_flow.included_rows, 212)
        self.assertEqual(result.sample_flow.by_group["control"]["included_rows"], 102)
        self.assertEqual(result.sample_flow.by_group["treatment"]["included_rows"], 110)
        self.assertAlmostEqual(
            result.estimates["contrast_group"]["mean"], 125.0809090909, places=9
        )
        self.assertAlmostEqual(
            result.estimates["reference_group"]["mean"], 131.8696078431, places=9
        )
        self.assertAlmostEqual(contrast["mean_difference"], -6.7886987522, places=9)
        self.assertAlmostEqual(contrast["standard_error"], 2.0559860391, places=9)
        self.assertAlmostEqual(result.test["statistic"], -3.3019187014, places=9)
        self.assertAlmostEqual(
            result.test["degrees_of_freedom"], 203.5589436285, places=8
        )
        self.assertAlmostEqual(result.test["p_value"], 0.001134077492, places=11)
        self.assertAlmostEqual(
            contrast["confidence_interval"]["lower"], -10.8424584272, places=8
        )
        self.assertAlmostEqual(
            contrast["confidence_interval"]["upper"], -2.7349390772, places=8
        )
        self.assertAlmostEqual(contrast["hedges_g_pooled_sd"], -0.4540138715, places=9)

    def test_ancova_hc3_matches_golden_values(self) -> None:
        result = run_ancova(self.frame, self.profile, self.design)
        contrast = result.estimates["contrast"]
        interaction = result.diagnostics["slope_homogeneity"][0]

        self.assertEqual(result.sample_flow.included_rows, 212)
        self.assertEqual(result.input_spec["covariance_estimator"], "HC3")
        self.assertTrue(result.input_spec["use_t_distribution"])
        self.assertAlmostEqual(
            result.input_spec["covariate_center_values"]["baseline_sbp"],
            132.0660377358,
            places=9,
        )
        self.assertAlmostEqual(
            contrast["adjusted_mean_difference"], -5.6069303056, places=9
        )
        self.assertAlmostEqual(contrast["standard_error_hc3"], 1.1810071282, places=9)
        self.assertAlmostEqual(result.test["statistic"], -4.7475837967, places=9)
        self.assertAlmostEqual(result.test["degrees_of_freedom"], 209.0, places=9)
        self.assertAlmostEqual(result.test["p_value"], 3.817575933e-6, places=13)
        self.assertAlmostEqual(
            contrast["confidence_interval"]["lower"], -7.9351435021, places=8
        )
        self.assertAlmostEqual(
            contrast["confidence_interval"]["upper"], -3.2787171092, places=8
        )
        self.assertAlmostEqual(
            result.estimates["reference_group"]["adjusted_mean"]["estimate"],
            131.256426102,
            places=8,
        )
        self.assertAlmostEqual(
            result.estimates["contrast_group"]["adjusted_mean"]["estimate"],
            125.649495796,
            places=8,
        )
        self.assertAlmostEqual(interaction["slope_difference"], -0.01430784439, places=9)
        self.assertAlmostEqual(interaction["p_value"], 0.8799005632, places=9)

    def test_row_order_does_not_change_contrast_direction(self) -> None:
        shuffled = self.frame.sample(frac=1, random_state=17).reset_index(drop=True)
        original = run_welch_t_test(self.frame, self.profile, self.design)
        reordered = run_welch_t_test(shuffled, self.profile, self.design)
        self.assertEqual(original.evidence_id, reordered.evidence_id)
        self.assertEqual(
            original.estimates["contrast"]["mean_difference"],
            reordered.estimates["contrast"]["mean_difference"],
        )
        self.assertEqual(
            reordered.estimates["contrast"]["direction"], "treatment - control"
        )

    def test_constant_outcome_stops_with_stable_error(self) -> None:
        frame = self.frame.copy()
        frame["followup_sbp"] = 1.0
        with self.assertRaises(AnalysisExecutionError) as context:
            run_welch_t_test(frame, self.profile, self.design)
        self.assertEqual(context.exception.code, "constant_outcome")

    def test_rank_deficient_ancova_stops(self) -> None:
        frame = self.frame.copy()
        frame["duplicate_baseline"] = frame["baseline_sbp"]
        payload = json.loads(DESIGN_PATH.read_text(encoding="utf-8"))
        payload["covariates"] = ["baseline_sbp", "duplicate_baseline"]
        payload["covariate_timing"]["duplicate_baseline"] = "pre_treatment"
        design = ResearchDesign.from_dict(payload)
        with self.assertRaises(AnalysisExecutionError) as context:
            run_ancova(frame, self.profile, design)
        self.assertEqual(context.exception.code, "rank_deficient_design")


if __name__ == "__main__":
    unittest.main()
