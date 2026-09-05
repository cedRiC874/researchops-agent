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
        linux_corpus = (
            PROJECT_ROOT / "evals" / "tasks.linux-x86_64.jsonl"
        ).read_bytes()

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
        self.assertNotIn(b"\r\n", linux_corpus)
        self.assertEqual(
            hashlib.sha256(linux_corpus).hexdigest(),
            "68ea85b79a43d8bb32834ae5d990aa2135bbfe5775c50ee7d7b2f239f4b68b23",
        )
        translated = corpus.replace(b"E-36034128278C", b"E-14EBFFCA843E")
        translated = translated.replace(b"E-E5D03B8E6EB8", b"E-5FBD2DA79692")
        translated = translated.replace(b"CH-6D27DA2CB989", b"CH-BF5193D84458")
        self.assertEqual(linux_corpus, translated)

    def test_phase5_ci_profile_accepts_exact_release_thresholds(self) -> None:
        for profile in ("phase5-ci-v1", "phase5-linux-x86-ci-v1"):
            with self.subTest(profile=profile):
                gate = VERIFIER._build_quality_gate(profile, _perfect_report())

                self.assertEqual(gate["status"], "valid")
                self.assertIsNone(gate["error_code"])
                self.assertEqual(gate["mismatches"], [])

    def test_verifier_resolves_only_manifest_bound_eval_corpora(self) -> None:
        manifest = {"task_corpus": {"file_name": "tasks.linux-x86_64.jsonl"}}
        self.assertEqual(
            VERIFIER._manifest_task_corpus_path(PROJECT_ROOT, manifest),
            (PROJECT_ROOT / "evals" / "tasks.linux-x86_64.jsonl").resolve(),
        )
        for file_name in (
            "../tasks.jsonl",
            "tasks.json",
            "unfrozen-tasks.jsonl",
            "",
        ):
            with self.subTest(file_name=file_name):
                manifest["task_corpus"]["file_name"] = file_name
                self.assertIsNone(
                    VERIFIER._manifest_task_corpus_path(PROJECT_ROOT, manifest)
                )

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
        self.assertIn("phase5_numeric_identity_mismatch", workflow)
        self.assertIn("Canonical x86-v2 ANCOVA evidence identity verified", workflow)
        self.assertIn("E-36034128278C", workflow)
        self.assertIn("--quality-profile phase5-ci-v1", workflow)
        self.assertIn("$evaluationExitCode = $LASTEXITCODE", workflow)
        self.assertIn("$verificationExitCode = $LASTEXITCODE", workflow)
        self.assertIn("phase5_offline_quality_gate_failed", workflow)
        self.assertIn("exit 1", workflow)

    def test_portfolio_demo_uses_strict_platform_numerical_baselines(self) -> None:
        core = (PROJECT_ROOT / "scripts" / "portfolio_demo.py").read_text(
            encoding="utf-8"
        )
        powershell = (PROJECT_ROOT / "scripts" / "portfolio_demo.ps1").read_text(
            encoding="utf-8"
        )
        shell = (PROJECT_ROOT / "scripts" / "portfolio_demo.sh").read_text(
            encoding="utf-8"
        )

        for name, value in (
            ("OPENBLAS_CORETYPE", "NEHALEM"),
            ("OPENBLAS_NUM_THREADS", "1"),
            ("OMP_NUM_THREADS", "1"),
            ("MKL_NUM_THREADS", "1"),
            ("NUMEXPR_NUM_THREADS", "1"),
            ("NPY_DISABLE_CPU_FEATURES", "X86_V3,X86_V4"),
        ):
            self.assertIn(f'"{name}": "{value}"', core)
        self.assertIn("Core:\\s*Nehalem", core)
        self.assertIn("E-36034128278C", core)
        self.assertIn("E-14EBFFCA843E", core)
        self.assertIn('"phase5-ci-v1"', core)
        self.assertIn('"phase5-linux-x86-ci-v1"', core)
        self.assertIn("PROVIDER_CREDENTIAL_VARIABLES", core)
        self.assertIn("output.is_relative_to(artifacts_root)", core)

        self.assertIn("scripts\\portfolio_demo.py", powershell)
        self.assertIn("ForEach-Object { Write-Host", powershell)
        self.assertNotIn("$capturedOutput = @(", powershell)
        self.assertIn("$LASTEXITCODE", powershell)
        self.assertTrue(shell.startswith("#!/usr/bin/env bash\nset -euo pipefail\n"))
        self.assertIn("${PYTHON_PATH:-${repo_root}/.venv/bin/python}", shell)
        self.assertIn('scripts/portfolio_demo.py" "$@"', shell)

    def test_supported_demo_platforms_are_documented_and_checked_in_ci(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        english_readme = (PROJECT_ROOT / "README.en.md").read_text(encoding="utf-8")
        windows_requirements = (PROJECT_ROOT / "requirements.lock").read_text(
            encoding="utf-8"
        )
        linux_requirements = (PROJECT_ROOT / "requirements.linux.lock").read_text(
            encoding="utf-8"
        )
        workflow = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("Python 3.12", readme)
        self.assertNotIn("Python 3.11+", readme)
        self.assertIn("Windows x86-64", readme)
        self.assertIn("Linux x86-64", readme)
        self.assertIn("macOS 与 ARM", readme)
        self.assertIn("requirements.linux.lock", readme)
        self.assertIn("Python 3.12", english_readme)
        self.assertNotIn("Python 3.11+", english_readme)
        self.assertIn("Windows x86-64", english_readme)
        self.assertIn("Linux x86-64", english_readme)
        self.assertIn("macOS and ARM", english_readme)
        self.assertIn("requirements.linux.lock", english_readme)
        self.assertIn("scripts\\portfolio_demo.ps1", readme)
        self.assertIn("scripts/portfolio_demo.sh", readme)
        windows_requirement_lines = windows_requirements.splitlines()
        linux_requirement_lines = linux_requirements.splitlines()
        self.assertEqual(windows_requirement_lines.count("pywin32==312"), 1)
        self.assertIn("pywin32==312", windows_requirement_lines)
        self.assertNotIn("pywin32==312", linux_requirement_lines)
        self.assertEqual(
            [
                requirement
                for requirement in windows_requirement_lines
                if requirement != "pywin32==312"
            ],
            linux_requirement_lines,
        )
        self.assertIn("linux-x86-demo:", workflow)
        self.assertIn("runs-on: ubuntu-latest", workflow)
        self.assertIn("python -m pip install -r requirements.linux.lock", workflow)
        self.assertIn("python -m pip check", workflow)
        self.assertIn('test "$(uname -m)" = "x86_64"', workflow)
        self.assertIn("bash -n scripts/portfolio_demo.sh", workflow)
        self.assertIn(
            "bash scripts/portfolio_demo.sh --output-dir artifacts/ci_linux_x86_demo",
            workflow,
        )
        self.assertIn("test ! -e handoff.md", workflow)
        self.assertIn("test -f docs/internal/handoff.md", workflow)
        self.assertNotIn("Validate macOS and Linux", workflow)


if __name__ == "__main__":
    unittest.main()
