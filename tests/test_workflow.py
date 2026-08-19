from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from researchops.analysis_tools import AnalysisExecutionError
from researchops.contracts import ResearchDesign
from researchops.workflow import run_phase3_analysis, validate_cli_output_directory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = PROJECT_ROOT / "data" / "synthetic_trial.csv"
DESIGN_PATH = PROJECT_ROOT / "data" / "synthetic_trial_design.json"


class Phase3WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.design = ResearchDesign.from_dict(
            json.loads(DESIGN_PATH.read_text(encoding="utf-8"))
        )

    def test_workflow_publishes_verified_private_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            first_output = Path(temporary_directory) / "first"
            second_output = Path(temporary_directory) / "second"
            first = run_phase3_analysis(DATA_PATH, self.design, first_output)
            second = run_phase3_analysis(DATA_PATH, self.design, second_output)

            bundle_path = first_output / "analysis_bundle.json"
            chart_path = first_output / "effect_estimates.png"
            self.assertTrue(bundle_path.is_file())
            self.assertTrue(chart_path.is_file())
            self.assertEqual(first.run_id, second.run_id)
            self.assertEqual(
                [item.evidence_id for item in first.evidence],
                [item.evidence_id for item in second.evidence],
            )
            self.assertEqual(
                first.artifacts[0].plot_spec_sha256,
                second.artifacts[0].plot_spec_sha256,
            )

            payload_text = bundle_path.read_text(encoding="utf-8")
            self.assertNotIn("P0001", payload_text)
            self.assertNotIn(str(PROJECT_ROOT), payload_text)
            payload = json.loads(payload_text)
            self.assertFalse(payload["dataset"]["raw_data_embedded"])
            self.assertEqual(payload["dataset"]["source_rows"], 240)

            chart_bytes = chart_path.read_bytes()
            self.assertTrue(chart_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
            digest = hashlib.sha256(chart_bytes).hexdigest()
            self.assertEqual(digest, first.artifacts[0].sha256)
            self.assertEqual(len(chart_bytes), first.artifacts[0].byte_size)
            with Image.open(chart_path) as image:
                self.assertEqual(
                    image.size,
                    (first.artifacts[0].width_px, first.artifacts[0].height_px),
                )
                self.assertGreater(image.width, 800)
                self.assertGreater(image.height, 400)

            with self.assertRaises(AnalysisExecutionError) as context:
                run_phase3_analysis(DATA_PATH, self.design, first_output)
            self.assertEqual(context.exception.code, "artifact_directory_exists")
            self.assertEqual(hashlib.sha256(chart_path.read_bytes()).hexdigest(), digest)

    def test_cli_output_path_cannot_escape_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(AnalysisExecutionError) as context:
                validate_cli_output_directory(Path(temporary_directory) / "run")
        self.assertEqual(context.exception.code, "output_path_not_allowed")


if __name__ == "__main__":
    unittest.main()
