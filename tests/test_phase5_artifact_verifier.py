from __future__ import annotations

import hashlib
import importlib.util
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = PROJECT_ROOT / "scripts" / "verify_phase5_artifacts.py"
SPEC = importlib.util.spec_from_file_location("phase5_artifact_verifier", VERIFIER_PATH)
assert SPEC is not None and SPEC.loader is not None
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


def _perfect_report() -> dict[str, int | float]:
    return {
        "task_count": 50,
        "passed_count": 50,
        "failed_count": 0,
        "success_rate": 1.0,
        "evidence_citations_required": 21,
        "evidence_citations_matched": 21,
        "evidence_citation_accuracy": 1.0,
    }


class Phase5ArtifactQualityGateTests(unittest.TestCase):
    def test_phase5_corpus_is_bound_to_canonical_lf_provenance(self) -> None:
        dataset = (PROJECT_ROOT / "data" / "synthetic_trial.csv").read_bytes()
        corpus = (PROJECT_ROOT / "evals" / "tasks.jsonl").read_bytes()

        self.assertNotIn(b"\r\n", dataset)
        self.assertEqual(
            hashlib.sha256(dataset).hexdigest(),
            "7ae3c201ccb543b5c647c8c50b2a754294d1d62aaaa458d0f2fb4b0af990ca00",
        )
        self.assertNotIn(b"\r\n", corpus)
        self.assertEqual(
            hashlib.sha256(corpus).hexdigest(),
            "ffa82ef11ff3e030a9b62cfa7801deab4930e131f180ab67539e774a7d88debf",
        )
        self.assertEqual(corpus.count(b"E-36034128278C"), 19)
        self.assertEqual(corpus.count(b"E-E5D03B8E6EB8"), 14)
        self.assertEqual(corpus.count(b"CH-6D27DA2CB989"), 4)
        self.assertNotIn(b"E-8EDFAE7ED8F0", corpus)
        self.assertNotIn(b"CH-11F349FABC44", corpus)
        self.assertNotIn(b"E-7C87BB6C88EB", corpus)
        self.assertNotIn(b"E-B93CD9DC7751", corpus)
        self.assertNotIn(b"CH-F675F0E546C6", corpus)

    def test_phase5_ci_profile_accepts_exact_release_thresholds(self) -> None:
        gate = VERIFIER._build_quality_gate("phase5-ci-v1", _perfect_report())

        self.assertEqual(gate["status"], "valid")
        self.assertIsNone(gate["error_code"])
        self.assertEqual(gate["mismatches"], [])

    def test_phase5_ci_profile_rejects_main_regression_with_stable_order(self) -> None:
        report = _perfect_report()
        report.update(
            {
                "passed_count": 44,
                "failed_count": 6,
                "success_rate": 0.88,
                "evidence_citations_matched": 10,
                "evidence_citation_accuracy": 10 / 21,
            }
        )

        gate = VERIFIER._build_quality_gate("phase5-ci-v1", report)

        self.assertEqual(gate["status"], "invalid")
        self.assertEqual(gate["error_code"], "phase5_quality_threshold_mismatch")
        self.assertEqual(
            [item["field"] for item in gate["mismatches"]],
            [
                "passed_count",
                "failed_count",
                "success_rate",
                "evidence_citations_matched",
                "evidence_citation_accuracy",
            ],
        )

    def test_phase5_ci_profile_rejects_zero_over_zero_evidence(self) -> None:
        report = _perfect_report()
        report.update(
            {
                "evidence_citations_required": 0,
                "evidence_citations_matched": 0,
                "evidence_citation_accuracy": 1.0,
            }
        )

        gate = VERIFIER._build_quality_gate("phase5-ci-v1", report)

        self.assertEqual(gate["status"], "invalid")
        self.assertEqual(
            [item["field"] for item in gate["mismatches"]],
            ["evidence_citations_required", "evidence_citations_matched"],
        )

    def test_phase5_ci_profile_rejects_boolean_numeric_values(self) -> None:
        report = _perfect_report()
        report["passed_count"] = True

        gate = VERIFIER._build_quality_gate("phase5-ci-v1", report)

        self.assertEqual(gate["status"], "invalid")
        self.assertEqual(gate["mismatches"][0]["field"], "passed_count")

    def test_no_profile_preserves_integrity_only_verifier_semantics(self) -> None:
        gate = VERIFIER._build_quality_gate(None, None)

        self.assertEqual(
            gate,
            {
                "profile": None,
                "status": "not_enforced",
                "error_code": None,
                "mismatches": [],
            },
        )

    def test_unreadable_quality_report_fails_with_stable_reason(self) -> None:
        gate = VERIFIER._build_quality_gate(
            "phase5-ci-v1", None, report_readable=False
        )

        self.assertEqual(gate["status"], "invalid")
        self.assertEqual(gate["error_code"], "phase5_quality_report_unreadable")
        self.assertEqual(gate["mismatches"], [])

    def test_workflow_preserves_both_native_exit_codes_and_enforces_profile(self) -> None:
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("OPENBLAS_CORETYPE: NEHALEM", workflow)
        self.assertIn('OPENBLAS_NUM_THREADS: "1"', workflow)
        self.assertIn('OMP_NUM_THREADS: "1"', workflow)
        self.assertIn('MKL_NUM_THREADS: "1"', workflow)
        self.assertIn('NUMEXPR_NUM_THREADS: "1"', workflow)
        self.assertIn('NPY_DISABLE_CPU_FEATURES: "X86_V3,X86_V4"', workflow)
        self.assertIn("phase5_blas_kernel_mismatch", workflow)
        self.assertIn("Core:\\s*Nehalem", workflow)
        self.assertIn("phase5_numpy_dispatch_mismatch", workflow)
        self.assertIn("NumPy dispatch verified: x86-v2", workflow)
        self.assertIn("phase5_numeric_identity_mismatch", workflow)
        self.assertIn("E-36034128278C", workflow)
        self.assertIn("--quality-profile phase5-ci-v1", workflow)
        self.assertIn("$evaluationExitCode = $LASTEXITCODE", workflow)
        self.assertIn("$verificationExitCode = $LASTEXITCODE", workflow)
        self.assertIn("phase5_offline_quality_gate_failed", workflow)
        self.assertIn("exit 1", workflow)


if __name__ == "__main__":
    unittest.main()
