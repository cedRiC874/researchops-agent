from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from researchops.eval_v2_artifacts import (
    aggregate_eval_v2_repetitions,
    write_eval_v2_artifacts,
)
from researchops.eval_v2_contracts import EvalV2ContractError


def report(
    provider_id: str,
    repetition: int,
    statuses: tuple[bool, ...] = (True, True),
) -> dict[str, object]:
    passed = sum(statuses)
    return {
        "schema_version": "2.0",
        "runner_version": "1.0",
        "evaluation_mode": "provider_online",
        "evidence_status": "online_run_unfrozen",
        "model_quality_claim_allowed": False,
        "repetition_index": repetition,
        "provider": {
            "provider_id": provider_id,
            "model_id": f"{provider_id}-model-v1",
            "transport_id": f"{provider_id}-transport",
        },
        "task_count": len(statuses),
        "passed": passed,
        "failed": len(statuses) - passed,
        "success_rate": passed / len(statuses),
        "safety_violation_count": 0,
        "model_call_count": len(statuses),
        "usage_complete": True,
        "task_scores": [
            {"task_id": f"V2-DEV-{index:03d}", "passed": status}
            for index, status in enumerate(statuses, start=1)
        ],
    }


class EvalV2ArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "artifacts").mkdir()
        source = self.root / "src" / "researchops"
        source.mkdir(parents=True)
        (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    def test_three_repetitions_per_provider_are_aggregated(self) -> None:
        reports = [
            report("provider_a", 1, (True, True)),
            report("provider_a", 2, (True, False)),
            report("provider_a", 3, (True, True)),
            report("provider_b", 1, (True, True)),
            report("provider_b", 2, (True, True)),
            report("provider_b", 3, (True, True)),
        ]
        reports[1]["task_scores"].reverse()
        reports[4]["task_scores"].reverse()

        result = aggregate_eval_v2_repetitions(
            reports, minimum_provider_count=2
        )

        self.assertEqual(result["provider_count"], 2)
        self.assertEqual(result["repetitions_per_provider"], 3)
        provider_a = next(
            item for item in result["providers"] if item["provider_id"] == "provider_a"
        )
        self.assertEqual(provider_a["success_rates"], [1.0, 0.5, 1.0])
        self.assertEqual(provider_a["task_stability_rate"], 0.5)
        self.assertEqual(provider_a["all_repetitions_pass_rate"], 0.5)
        self.assertFalse(result["model_quality_claim_allowed"])
        self.assertEqual(result["task_alignment"], "by_task_id")

    def test_missing_repetition_and_scope_mismatch_fail_closed(self) -> None:
        with self.assertRaises(EvalV2ContractError) as missing:
            aggregate_eval_v2_repetitions(
                [report("provider_a", 1), report("provider_a", 3)]
            )
        self.assertEqual(missing.exception.code, "eval_v2_repetition_count_mismatch")

        mismatched = report("provider_a", 3)
        mismatched["task_scores"][1]["task_id"] = "V2-DEV-999"
        with self.assertRaises(EvalV2ContractError) as scope:
            aggregate_eval_v2_repetitions(
                [report("provider_a", 1), report("provider_a", 2), mismatched]
            )
        self.assertEqual(scope.exception.code, "eval_v2_repetition_scope_mismatch")

    def test_artifacts_are_atomic_hash_verified_and_non_overwriting(self) -> None:
        run_report = report("provider_a", 1)
        aggregation = aggregate_eval_v2_repetitions(
            [
                report("provider_a", 1),
                report("provider_a", 2),
                report("provider_a", 3),
            ]
        )
        output = self.root / "artifacts" / "eval-v2-run"

        result = write_eval_v2_artifacts(
            project_root=self.root,
            output_directory=output,
            report=run_report,
            repetition_aggregation=aggregation,
            run_metadata={"campaign_id": "eval-v2-test"},
        )

        self.assertEqual(result["status"], "published_atomic_local")
        self.assertEqual(result["artifact_count"], 4)
        manifest = json.loads(
            (output / "eval_v2_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(manifest["artifacts"]), 3)
        self.assertFalse(manifest["model_quality_claim_allowed"])
        self.assertRegex(manifest["source_tree_sha256"], r"^[a-f0-9]{64}$")
        self.assertFalse(any(output.parent.glob(".eval-v2-artifacts-*")))

        with self.assertRaises(EvalV2ContractError) as overwrite:
            write_eval_v2_artifacts(
                project_root=self.root,
                output_directory=output,
                report=run_report,
            )
        self.assertEqual(overwrite.exception.code, "eval_v2_artifact_output_exists")

    def test_artifact_path_and_sensitive_values_are_rejected(self) -> None:
        with self.assertRaises(EvalV2ContractError) as boundary:
            write_eval_v2_artifacts(
                project_root=self.root,
                output_directory=self.root / "outside",
                report=report("provider_a", 1),
            )
        self.assertEqual(boundary.exception.code, "eval_v2_artifact_path_not_allowed")

        sensitive = report("provider_a", 1)
        sensitive["api_key"] = "sk-secret"
        with self.assertRaises(EvalV2ContractError) as secret:
            write_eval_v2_artifacts(
                project_root=self.root,
                output_directory=self.root / "artifacts" / "sensitive",
                report=sensitive,
            )
        self.assertEqual(secret.exception.code, "eval_v2_artifact_sensitive_field")


if __name__ == "__main__":
    unittest.main()
