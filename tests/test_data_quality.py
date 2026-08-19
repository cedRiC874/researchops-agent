from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from researchops.data_quality import CsvProfiler, CsvValidationError


class CsvProfilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temp_path = Path(self.temporary_directory.name)

    def write_csv(self, name: str, frame: pd.DataFrame) -> Path:
        path = self.temp_path / name
        frame.to_csv(path, index=False, encoding="utf-8")
        return path

    def test_profiles_structure_missingness_and_identifier(self) -> None:
        path = self.write_csv(
            "study.csv",
            pd.DataFrame(
                {
                    "participant_id": [f"P{i:03d}" for i in range(1, 11)],
                    "group": ["treatment", "control"] * 5,
                    "score": [1.0, 2.0, None, 4.0, 5.0, 6.0, None, 8.0, 9.0, 10.0],
                }
            ),
        )

        profile = CsvProfiler().profile(path)

        self.assertEqual(profile.row_count, 10)
        self.assertEqual(profile.column_count, 3)
        self.assertEqual(profile.rows_with_missing, 2)
        score = next(column for column in profile.columns if column.name == "score")
        self.assertEqual(score.null_count, 2)
        self.assertAlmostEqual(score.missing_rate, 0.2)
        warning_codes = {(warning.code, warning.column) for warning in profile.warnings}
        self.assertIn(("possible_identifier", "participant_id"), warning_codes)
        self.assertIn(("high_missingness", "score"), warning_codes)
        participant = next(
            column for column in profile.columns if column.name == "participant_id"
        )
        self.assertEqual(participant.sample_values, ["[REDACTED]"])

    def test_detects_formula_injection_risk(self) -> None:
        path = self.write_csv(
            "notes.csv",
            pd.DataFrame({"note": ["normal", "=HYPERLINK(\"https://example.test\")"]}),
        )
        profile = CsvProfiler().profile(path)
        self.assertTrue(
            any(warning.code == "formula_injection_risk" for warning in profile.warnings)
        )

    def test_redacts_high_cardinality_text_even_without_identifier_name(self) -> None:
        path = self.write_csv(
            "keys.csv",
            pd.DataFrame(
                {
                    "opaque_key": [f"token-{index:03d}" for index in range(20)],
                    "value": range(20),
                }
            ),
        )
        profile = CsvProfiler().profile(path)
        key_profile = next(column for column in profile.columns if column.name == "opaque_key")
        self.assertEqual(key_profile.sample_values, ["[REDACTED]"])
        self.assertTrue(
            any(
                warning.code == "possible_identifier" and warning.column == "opaque_key"
                for warning in profile.warnings
            )
        )

    def test_rejects_non_csv_file(self) -> None:
        path = self.temp_path / "study.txt"
        path.write_text("a,b\n1,2\n", encoding="utf-8")
        with self.assertRaisesRegex(CsvValidationError, "\\.csv"):
            CsvProfiler().profile(path)

    def test_rejects_duplicate_headers(self) -> None:
        path = self.temp_path / "duplicate.csv"
        path.write_text("value,value\n1,2\n", encoding="utf-8")
        with self.assertRaisesRegex(CsvValidationError, "重复列名"):
            CsvProfiler().profile(path)


if __name__ == "__main__":
    unittest.main()
