from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from researchops import cli
from researchops.eval_v2_contracts import EvalV2ContractError
from researchops.eval_v2_public_runner import run_public_regression_online


ROOT = Path(__file__).resolve().parents[1]
V7_CANDIDATE = Path("evals/v2/public_regression_candidate_v7.json")
V8_CANDIDATE = Path("evals/v2/public_regression_candidate_v8.json")
V5_CANDIDATE = Path("evals/v2/public_regression_candidate_v5.json")


def _invoke(arguments: list[str]) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with patch.object(sys, "argv", ["researchops", *arguments]), redirect_stdout(output):
        exit_code = cli.main()
    return exit_code, json.loads(output.getvalue())


class KimiControlledPilotCliGateTests(unittest.TestCase):
    def test_v6_and_v7_are_permanent_zero_call_tombstones(self) -> None:
        cases = (
            (
                [
                    "kimi-controlled-synthetic-pilot",
                    "--confirm-online",
                    "--accept-locked-caps",
                    "--authorization-id",
                    "AUTH-OLD-V6-0001",
                    "--authorization-expires-at-utc",
                    "2030-01-01T00:00:00Z",
                ],
                "kimi_pilot_v6_online_permanently_disabled",
                "kimi-controlled-synthetic-pilot-v1",
            ),
            (
                [
                    "kimi-controlled-synthetic-pilot-v7",
                    "--confirm-online",
                    "--accept-successor-v7-locked-caps",
                    "--authorized-candidate-commitment",
                    "f" * 64,
                    "--authorization-id",
                    "AUTH-OLD-V7-0001",
                    "--authorization-expires-at-utc",
                    "2030-01-01T00:00:00Z",
                ],
                "kimi_pilot_v7_online_permanently_disabled",
                "kimi-controlled-synthetic-pilot-v2",
            ),
        )
        for arguments, expected_error, expected_contract in cases:
            with self.subTest(command=arguments[0]):
                exit_code, result = _invoke(arguments)
            self.assertEqual(exit_code, 4)
            self.assertEqual(result["status"], "not_run")
            self.assertEqual(result["error_code"], expected_error)
            self.assertEqual(result["contract_id"], expected_contract)
            self.assertEqual(result["network_attempts"], 0)
            self.assertEqual(result["network_calls"], 0)
            self.assertEqual(result["model_request_count"], 0)
            self.assertFalse(result["key_loader_passed_to_runner"])
            for key, value in result.items():
                if key.startswith("authorizes_"):
                    self.assertFalse(value, key)

    def test_historical_verifiers_are_read_only_and_version_explicit(self) -> None:
        with patch.object(
            cli,
            "verify_kimi_controlled_pilot_artifacts",
            return_value={"status": "valid", "network_calls": 0},
        ) as v6_verify:
            exit_code, result = _invoke(
                ["kimi-controlled-pilot-verify", "--authorization-id", "AUTH-V6-VERIFY"]
            )
        self.assertEqual((exit_code, result["status"]), (0, "valid"))
        v6_verify.assert_called_once()

        with patch.object(
            cli,
            "verify_kimi_controlled_pilot_v2_artifacts",
            return_value={"status": "valid", "network_calls": 0},
        ) as v7_verify:
            exit_code, result = _invoke(
                ["kimi-controlled-pilot-v7-verify", "--authorization-id", "AUTH-V7-VERIFY"]
            )
        self.assertEqual((exit_code, result["status"]), (0, "valid"))
        v7_verify.assert_called_once()

        with patch.object(
            cli,
            "verify_kimi_controlled_pilot_v3_artifacts",
            return_value={"status": "valid", "network_calls": 0},
        ) as v8_verify:
            exit_code, result = _invoke(
                ["kimi-controlled-pilot-v8-verify", "--authorization-id", "AUTH-V8-VERIFY"]
            )
        self.assertEqual((exit_code, result["status"]), (0, "valid"))
        v8_verify.assert_called_once()

    def test_diagnostic_freeze_and_active_public_defaults_are_separate(self) -> None:
        parser = cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if action.__class__.__name__ == "_SubParsersAction"
        )
        freeze = subparsers.choices["eval-v2-verify-public-freeze"]
        public_run = subparsers.choices["eval-v2-run-public-online"]
        freeze_default = freeze.get_default("candidate")
        public_default = public_run.get_default("candidate")
        self.assertEqual(freeze_default, V8_CANDIDATE)
        self.assertEqual(public_default, V5_CANDIDATE)

    def test_v8_default_gate_is_zero_call_and_does_not_load_key(self) -> None:
        with (
            patch.object(
                cli,
                "validate_public_regression_candidate",
                side_effect=AssertionError("Candidate loader must not run before confirmation"),
            ),
        ):
            exit_code, result = _invoke(["kimi-controlled-synthetic-pilot-v8"])
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["status"], "not_run")
        self.assertEqual(result["error_code"], "kimi_pilot_v8_confirmation_required")
        self.assertEqual(result["contract_id"], "kimi-controlled-synthetic-pilot-v3")
        self.assertEqual(result["network_attempts"], 0)
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["model_request_count"], 0)
        self.assertFalse(result["key_loader_passed_to_runner"])
        for key, value in result.items():
                if key.startswith("authorizes_"):
                    self.assertFalse(value, key)

    def test_v8_confirmed_cli_remains_not_authorized_before_key_lookup(self) -> None:
        key_canary = "MOONSHOT-KEY-MUST-NOT-BE-READ-OR-RETURNED"
        with patch.dict(cli.os.environ, {"MOONSHOT_API_KEY": key_canary}):
            exit_code, result = _invoke(
                ["kimi-controlled-synthetic-pilot-v8", "--confirm-online"]
            )
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["status"], "not_run")
        self.assertEqual(result["error_code"], "kimi_pilot_v8_online_not_authorized")
        self.assertEqual(
            result["candidate_commitment_sha256"],
            "b41269ac6db96e2999fedc95f08f3b77a48699f8c0b50b63764bcb6e1f9e962c",
        )
        self.assertEqual(result["network_attempts"], 0)
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["model_request_count"], 0)
        self.assertFalse(result["key_loader_passed_to_runner"])
        self.assertNotIn(key_canary, repr(result))
        self.assertFalse(hasattr(cli, "run_kimi_controlled_pilot_v3"))

    def test_dependency_environment_cli_is_candidate_independent(self) -> None:
        expected = {
            "status": "valid",
            "verification_scope": "requirements_lock_and_installed_environment_only",
            "candidate_verified": False,
            "historical_snapshot_verified": False,
            "dependency_lock": {
                "environment_verified": True,
                "environment_mismatch_count": 0,
            },
            "network_calls": 0,
        }
        with patch.object(
            cli, "validate_eval_v2_dependency_environment", return_value=expected
        ) as verify:
            exit_code, result = _invoke(["eval-v2-verify-environment"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(result, expected)
        verify.assert_called_once_with(ROOT)

    def test_public_runner_never_executes_historical_candidate(self) -> None:
        output = ROOT / "artifacts" / f"historical-run-denied-{uuid4().hex}"
        with (
            patch(
                "researchops.eval_v2_public_runner.validate_eval_v2_dependency_environment",
                side_effect=AssertionError(
                    "environment validation must follow historical rejection"
                ),
            ),
            patch(
                "researchops.eval_v2_public_runner.get_provider",
                side_effect=AssertionError("Provider must not be constructed"),
            ),
            self.assertRaises(EvalV2ContractError) as caught,
        ):
            run_public_regression_online(
                project_root=ROOT,
                candidate_path=ROOT / V7_CANDIDATE,
                registry_path=ROOT / "artifacts/missing-registry.json",
                output_directory=output,
                api_key="offline-canary-key-not-forwarded",
                budget_cny=6,
                confirm_online=True,
            )
        self.assertEqual(
            caught.exception.code,
            "eval_v2_historical_candidate_execution_forbidden",
        )
        self.assertFalse(output.exists())

    def test_active_v5_source_drift_fails_before_environment_or_provider(self) -> None:
        output = ROOT / "artifacts" / f"active-v5-drift-denied-{uuid4().hex}"
        with (
            patch(
                "researchops.eval_v2_public_runner.validate_eval_v2_dependency_environment",
                side_effect=AssertionError(
                    "environment validation must follow Candidate source validation"
                ),
            ),
            patch(
                "researchops.eval_v2_public_runner.get_provider",
                side_effect=AssertionError("Provider must not be constructed"),
            ),
            self.assertRaises(EvalV2ContractError) as caught,
        ):
            run_public_regression_online(
                project_root=ROOT,
                candidate_path=ROOT / V5_CANDIDATE,
                registry_path=ROOT / "artifacts/missing-registry.json",
                output_directory=output,
                api_key="offline-canary-key-not-forwarded",
                budget_cny=6,
                confirm_online=True,
            )
        self.assertEqual(
            caught.exception.code,
            "eval_v2_public_candidate_component_drift",
        )
        self.assertFalse(output.exists())

    def test_diagnostic_v8_public_run_is_denied_before_environment_or_provider(self) -> None:
        output = ROOT / "artifacts" / f"diagnostic-v8-denied-{uuid4().hex}"
        with (
            patch(
                "researchops.eval_v2_public_runner.validate_eval_v2_dependency_environment",
                side_effect=AssertionError(
                    "environment validation must follow diagnostic rejection"
                ),
            ),
            patch(
                "researchops.eval_v2_public_runner.get_provider",
                side_effect=AssertionError("Provider must not be constructed"),
            ),
            self.assertRaises(EvalV2ContractError) as caught,
        ):
            run_public_regression_online(
                project_root=ROOT,
                candidate_path=ROOT / V8_CANDIDATE,
                registry_path=ROOT / "artifacts/missing-registry.json",
                output_directory=output,
                api_key="offline-canary-key-not-forwarded",
                budget_cny=6,
                confirm_online=True,
            )
        self.assertEqual(
            caught.exception.code,
            "eval_v2_historical_candidate_execution_forbidden",
        )
        self.assertFalse(output.exists())

    def test_public_cli_rejects_v8_before_deepseek_key_lookup(self) -> None:
        output = ROOT / "artifacts" / f"diagnostic-v8-cli-denied-{uuid4().hex}"
        with patch.object(
            cli,
            "_load_deepseek_api_key",
            side_effect=AssertionError("DeepSeek Key lookup must not run"),
        ):
            exit_code, result = _invoke(
                [
                    "eval-v2-run-public-online",
                    "--candidate",
                    str(V8_CANDIDATE),
                    "--registry",
                    "artifacts/missing-registry.json",
                    "--output-dir",
                    str(output),
                    "--budget-cny",
                    "6",
                    "--confirm-online",
                ]
            )
        self.assertEqual(exit_code, 4)
        self.assertEqual(
            result["error_code"], "eval_v2_historical_candidate_execution_forbidden"
        )
        self.assertFalse(output.exists())

    def test_pr_a_workflow_binds_the_historical_v7_control_plane(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn(
            "--candidate evals/v2/public_regression_candidate_v7.json",
            workflow,
        )
        self.assertIn("eval-v2-verify-environment", workflow)
        self.assertNotIn(
            "--candidate evals/v2/public_regression_candidate_v7.json `\n"
            "            --verify-environment",
            workflow,
        )
        self.assertIn("kimi-controlled-synthetic-pilot-v7", workflow)
        self.assertIn("kimi-controlled-synthetic-pilot-v8", workflow)
        self.assertIn("public_regression_candidate_v8.json", workflow)
        self.assertIn("kimi_pilot_v8_online_not_authorized", workflow)

    def test_ci_contract_commands_fail_fast_and_separate_environment_scope(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        lines = workflow.splitlines()
        for command in (
            "python -m researchops.cli eval-validate",
            "python -m researchops.cli phase6-validate",
            "python -m researchops.cli phase6-status",
            "python -m researchops.cli eval-v2-validate",
        ):
            index = next(i for i, line in enumerate(lines) if line.strip() == command)
            self.assertEqual(
                lines[index + 1].strip(),
                "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
                command,
            )
        self.assertIn(
            'eval-v2-verify-environment | Out-String)',
            workflow,
        )
        self.assertIn(
            '$environment.verification_scope -ne "requirements_lock_and_installed_environment_only"',
            workflow,
        )
        self.assertIn("$environment.candidate_verified -ne $false", workflow)
        self.assertIn("$environment.historical_snapshot_verified -ne $false", workflow)
        self.assertIn("$historicalV7.historical_snapshot_only -ne $true", workflow)
        self.assertIn("$diagnosticV8.historical_snapshot_only -ne $true", workflow)


if __name__ == "__main__":
    unittest.main()
