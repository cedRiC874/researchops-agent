from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from researchops.contracts import ResearchDesign
from researchops.data_quality import profile_csv
from researchops.method_selection import MethodSelectionError, recommend_method


class StatisticalMethodSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temp_path = Path(self.temporary_directory.name)

    def profile(self, frame: pd.DataFrame):
        path = self.temp_path / "study.csv"
        frame.to_csv(path, index=False, encoding="utf-8")
        return profile_csv(path)

    @staticmethod
    def design(**overrides: object) -> ResearchDesign:
        values: dict[str, object] = {
            "question": "两组结局是否不同？",
            "objective": "group_difference",
            "outcome": "outcome",
            "outcome_type": "continuous",
            "predictor": "group",
            "predictor_type": "categorical",
            "group_count": 2,
            "reference_level": "a",
            "contrast_level": "b",
        }
        values.update(overrides)
        return ResearchDesign(**values)

    def test_baseline_adjusted_rct_recommends_ancova(self) -> None:
        profile = self.profile(
            pd.DataFrame(
                {
                    "participant_id": [f"P{i:03d}" for i in range(20)],
                    "group": ["treatment", "control"] * 10,
                    "baseline": list(range(110, 130)),
                    "outcome": [value - (4 if i % 2 == 0 else 0) for i, value in enumerate(range(112, 132))],
                }
            )
        )
        design = self.design(
            covariates=("baseline",),
            covariate_timing={"baseline": "pre_treatment"},
            analysis_population="intention_to_treat",
            randomized=True,
            reference_level="control",
            contrast_level="treatment",
        )

        result = recommend_method(profile, design)

        self.assertEqual(result.primary_method.code, "ancova_linear_model")
        self.assertEqual(result.sensitivity_methods[0].code, "welch_t_test")

    def test_independent_two_group_continuous_defaults_to_welch(self) -> None:
        profile = self.profile(
            pd.DataFrame({"group": ["a", "b"] * 10, "outcome": list(range(20))})
        )
        result = recommend_method(profile, self.design())
        self.assertEqual(result.primary_method.code, "welch_t_test")
        self.assertTrue(any("随机分组未知" in warning for warning in result.warnings))

    def test_paired_non_normal_continuous_uses_signed_rank(self) -> None:
        profile = self.profile(
            pd.DataFrame(
                {
                    "subject_id": [f"S{i:02d}" for i in range(10) for _ in range(2)],
                    "time": ["before", "after"] * 10,
                    "group": ["cohort"] * 20,
                    "outcome": list(range(20)),
                }
            )
        )
        design = self.design(
            group_count=2,
            paired=True,
            subject_id="subject_id",
            time_variable="time",
            normality="violated",
            predictor="time",
            reference_level="before",
            contrast_level="after",
        )
        result = recommend_method(profile, design)
        self.assertEqual(result.primary_method.code, "wilcoxon_signed_rank")

    def test_sparse_binary_two_by_two_uses_fisher(self) -> None:
        profile = self.profile(
            pd.DataFrame(
                {
                    "group": ["a"] * 10 + ["b"] * 10,
                    "outcome": [1] + [0] * 9 + [1] * 2 + [0] * 8,
                }
            )
        )
        design = self.design(
            outcome_type="binary",
            expected_cell_count="sparse",
        )
        result = recommend_method(profile, design)
        self.assertEqual(result.primary_method.code, "fisher_exact_test")

    def test_nonnormal_continuous_association_uses_spearman(self) -> None:
        profile = self.profile(
            pd.DataFrame({"exposure": range(20), "outcome": [value**2 for value in range(20)]})
        )
        design = self.design(
            objective="association",
            predictor="exposure",
            predictor_type="continuous",
            group_count=None,
            normality="violated",
        )
        result = recommend_method(profile, design)
        self.assertEqual(result.primary_method.code, "spearman_correlation")

    def test_identifier_cannot_be_used_as_outcome(self) -> None:
        profile = self.profile(
            pd.DataFrame(
                {
                    "participant_id": [f"P{i:03d}" for i in range(20)],
                    "group": ["a", "b"] * 10,
                    "outcome": range(20),
                }
            )
        )
        design = self.design(outcome="participant_id", outcome_type="categorical")
        with self.assertRaisesRegex(MethodSelectionError, "标识符"):
            recommend_method(profile, design)

    def test_declared_group_count_must_match_data(self) -> None:
        profile = self.profile(
            pd.DataFrame({"group": ["a", "b", "c"] * 7, "outcome": range(21)})
        )
        with self.assertRaisesRegex(MethodSelectionError, "group_count=2"):
            recommend_method(profile, self.design(group_count=2))

    def test_missing_analysis_variable_stops_safely(self) -> None:
        profile = self.profile(
            pd.DataFrame({"group": ["a", "b"] * 10, "outcome": range(20)})
        )
        with self.assertRaisesRegex(MethodSelectionError, "missing_covariate"):
            recommend_method(
                profile,
                self.design(
                    covariates=("missing_covariate",),
                    covariate_timing={"missing_covariate": "pre_treatment"},
                ),
            )

    def test_unknown_covariate_timing_stops_safely_with_stable_code(self) -> None:
        profile = self.profile(
            pd.DataFrame(
                {
                    "group": ["a", "b"] * 10,
                    "baseline": range(20),
                    "outcome": range(20),
                }
            )
        )
        with self.assertRaises(MethodSelectionError) as context:
            recommend_method(
                profile,
                self.design(
                    covariates=("baseline",),
                    covariate_timing={"baseline": "unknown"},
                ),
            )
        codes = {issue["code"] for issue in context.exception.to_dict()["issues"]}
        self.assertIn("covariate_timing_unknown", codes)

    def test_post_treatment_covariate_is_rejected(self) -> None:
        profile = self.profile(
            pd.DataFrame(
                {
                    "group": ["a", "b"] * 10,
                    "adherence": range(20),
                    "outcome": range(20),
                }
            )
        )
        with self.assertRaises(MethodSelectionError) as context:
            recommend_method(
                profile,
                self.design(
                    covariates=("adherence",),
                    covariate_timing={"adherence": "post_treatment"},
                ),
            )
        codes = {issue["code"] for issue in context.exception.to_dict()["issues"]}
        self.assertIn("post_treatment_covariate", codes)

    def test_recommendation_is_deterministic(self) -> None:
        profile = self.profile(
            pd.DataFrame({"group": ["a", "b"] * 10, "outcome": range(20)})
        )
        design = self.design(randomized=False)
        first = recommend_method(profile, design).to_dict()
        second = recommend_method(profile, design).to_dict()
        self.assertEqual(first, second)

    def test_two_group_contrast_levels_are_required(self) -> None:
        profile = self.profile(
            pd.DataFrame({"group": ["a", "b"] * 10, "outcome": range(20)})
        )
        with self.assertRaises(MethodSelectionError) as context:
            recommend_method(
                profile,
                self.design(reference_level=None, contrast_level=None),
            )
        codes = {issue["code"] for issue in context.exception.to_dict()["issues"]}
        self.assertEqual(codes, {"reference_level_required", "contrast_level_required"})


if __name__ == "__main__":
    unittest.main()
