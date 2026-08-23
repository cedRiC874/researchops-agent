from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from researchops.eval_v2_artifacts import (
    _parse_completion_telemetry,
    aggregate_eval_v2_repetitions,
    write_eval_v2_artifacts,
)
from researchops.eval_v2_contracts import EvalV2ContractError


def completion_telemetry(
    *sources: str,
    legacy_unknown_count: int = 0,
) -> dict[str, object]:
    eligible = len(sources) + legacy_unknown_count
    counts = [
        {"source": source, "case_count": sources.count(source)}
        for source in (
            "final_output_missing",
            "response_output_item_incomplete",
            "response_not_completed",
            "output_limit_suspected",
        )
        if source in sources
    ]
    return {
        "contract_version": "completion-telemetry-v2",
        "diagnostic_only": True,
        "eligible_failure_count": eligible,
        "classified_failure_count": len(sources),
        "legacy_unknown_count": legacy_unknown_count,
        "classified_failure_coverage": len(sources) / eligible if eligible else None,
        "coverage_status": (
            "no_applicable_failures"
            if eligible == 0
            else ("complete" if legacy_unknown_count == 0 else "partial")
        ),
        "coverage_complete": legacy_unknown_count == 0,
        "failure_source_counts": counts,
    }


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
        self.assertEqual(
            result["completion_telemetry"]["structured_report_coverage"], 0.0
        )
        self.assertEqual(
            result["completion_telemetry"]["coverage_status"], "partial"
        )
        self.assertFalse(result["completion_telemetry"]["coverage_complete"])

    def test_completion_telemetry_aggregation_preserves_legacy_coverage(self) -> None:
        reports = [report("provider_a", index) for index in (1, 2, 3)]
        reports[0]["completion_telemetry"] = completion_telemetry(
            "final_output_missing"
        )
        reports[1]["completion_telemetry"] = completion_telemetry(
            legacy_unknown_count=1
        )

        result = aggregate_eval_v2_repetitions(reports)
        telemetry = result["completion_telemetry"]

        self.assertEqual(telemetry["report_count"], 3)
        self.assertEqual(telemetry["structured_report_count"], 2)
        self.assertEqual(telemetry["structured_report_coverage"], 2 / 3)
        self.assertEqual(telemetry["eligible_failure_count"], 2)
        self.assertEqual(telemetry["classified_failure_count"], 1)
        self.assertEqual(telemetry["legacy_unknown_count"], 1)
        self.assertEqual(telemetry["classified_failure_coverage"], 0.5)
        self.assertEqual(telemetry["coverage_status"], "partial")
        self.assertFalse(telemetry["coverage_complete"])
        self.assertEqual(
            telemetry["failure_source_counts"],
            [{"source": "final_output_missing", "case_count": 1}],
        )
        self.assertEqual(
            result["providers"][0]["completion_telemetry"], telemetry
        )

    def test_completion_telemetry_parser_rejects_drift_and_bad_order(self) -> None:
        for mutation in (
            "diagnostic_only",
            "coverage",
            "coverage_status",
            "source_order",
            "source_duplicate",
        ):
            reports = [report("provider_a", index) for index in (1, 2, 3)]
            telemetry = completion_telemetry(
                "final_output_missing", "output_limit_suspected"
            )
            if mutation == "diagnostic_only":
                telemetry["diagnostic_only"] = False
            elif mutation == "coverage":
                telemetry["classified_failure_coverage"] = None
            elif mutation == "coverage_status":
                telemetry["coverage_status"] = "partial"
            elif mutation == "source_duplicate":
                telemetry["failure_source_counts"].append(
                    dict(telemetry["failure_source_counts"][0])
                )
            else:
                telemetry["failure_source_counts"].reverse()
            reports[0]["completion_telemetry"] = telemetry

            with self.subTest(mutation=mutation):
                with self.assertRaises(EvalV2ContractError) as captured:
                    aggregate_eval_v2_repetitions(reports)
                self.assertEqual(
                    captured.exception.code, "eval_v2_repetition_report_invalid"
                )

    def test_aggregated_parser_rejects_false_complete_legacy_coverage(self) -> None:
        reports = [report("provider_a", index) for index in (1, 2, 3)]
        reports[0]["completion_telemetry"] = completion_telemetry(
            "final_output_missing"
        )
        aggregate = aggregate_eval_v2_repetitions(reports)[
            "completion_telemetry"
        ]
        tampered = json.loads(json.dumps(aggregate))
        tampered["coverage_complete"] = True
        tampered["coverage_status"] = "complete"

        with self.assertRaises(EvalV2ContractError) as captured:
            _parse_completion_telemetry(tampered)
        self.assertEqual(
            captured.exception.code, "eval_v2_repetition_report_invalid"
        )

    def test_no_applicable_completion_failures_have_null_coverage(self) -> None:
        reports = [report("provider_a", index) for index in (1, 2, 3)]
        for item in reports:
            item["completion_telemetry"] = completion_telemetry()

        telemetry = aggregate_eval_v2_repetitions(reports)[
            "completion_telemetry"
        ]

        self.assertIsNone(telemetry["classified_failure_coverage"])
        self.assertEqual(
            telemetry["coverage_status"], "no_applicable_failures"
        )
        self.assertTrue(telemetry["coverage_complete"])

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

        for forbidden_key in (
            "api_key",
            "provider_response_body",
            "provider_output_body",
            "provider_status_raw_value",
            "incomplete_details",
            "credentials",
            "direct_identifiers",
        ):
            sensitive = report("provider_a", 1)
            sensitive["nested"] = {forbidden_key: "must-not-persist"}
            with self.subTest(forbidden_key=forbidden_key):
                with self.assertRaises(EvalV2ContractError) as secret:
                    write_eval_v2_artifacts(
                        project_root=self.root,
                        output_directory=self.root / "artifacts" / "sensitive",
                        report=sensitive,
                    )
                self.assertEqual(
                    secret.exception.code, "eval_v2_artifact_sensitive_field"
                )


if __name__ == "__main__":
    unittest.main()
