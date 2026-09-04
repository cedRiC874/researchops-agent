from __future__ import annotations

import ast
import json
import hashlib
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import researchops.phase6_depth60 as depth60_module
from researchops.cli import build_parser
from researchops.phase6_depth60 import (
    DEPTH60_PLAN_PATH,
    DEPTH60_TASK_IDS,
    depth60_plan_commitment_sha256,
    run_phase6_depth60_online,
    validate_phase6_depth60_plan,
)
from researchops.phase6_runner import Phase6RunError
from researchops.phase6_source_bundle import (
    _local_import_targets,
    phase6_depth60_source_bundle_sha256,
    phase6_depth60_source_files,
)


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / DEPTH60_PLAN_PATH
EXPECTED_COMMITMENT = "8019ef294b5028ab4e44c006f01e02bddb5a3b67b1ed88b84945bf37e75c216e"


def _validate_historical_contract_at_locked_components() -> dict[str, object]:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    with patch.object(
        depth60_module,
        "build_depth60_component_hashes",
        return_value=plan["component_hashes"],
    ):
        return validate_phase6_depth60_plan(ROOT, PLAN)


class Phase6Depth60PlanTests(unittest.TestCase):
    def test_locked_plan_contract_validates_at_historical_components(self) -> None:
        result = _validate_historical_contract_at_locked_components()

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["selected_task_count"], 60)
        self.assertFalse(result["holdout_executed"])
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(
            result["plan_commitment_sha256"],
            EXPECTED_COMMITMENT,
        )
        self.assertEqual(
            result["plan"]["selection"]["selected_task_ids"],
            list(DEPTH60_TASK_IDS),
        )
        self.assertFalse(
            result["plan"]["authorization_boundary"][
                "plan_alone_authorizes_online_run"
            ]
        )

    def test_historical_plan_bytes_and_commitment_are_unchanged(self) -> None:
        payload = PLAN.read_bytes()
        plan = json.loads(payload.decode("utf-8"))
        self.assertEqual(len(payload), 5398)
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            "f5d43283e3506663383359d24736bd3b82a910e45cc092954f94d86a80e6cd20",
        )
        self.assertEqual(plan["plan_commitment_sha256"], EXPECTED_COMMITMENT)
        self.assertEqual(depth60_plan_commitment_sha256(plan), EXPECTED_COMMITMENT)

    def test_current_tree_rejects_the_historical_plan_as_component_drift(self) -> None:
        with self.assertRaises(Phase6RunError) as caught:
            validate_phase6_depth60_plan(ROOT, PLAN)
        self.assertEqual(caught.exception.code, "phase6_depth60_component_drift")

    def test_commitment_changes_when_budget_or_scope_changes(self) -> None:
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
        original = depth60_plan_commitment_sha256(plan)

        changed_budget = json.loads(json.dumps(plan))
        changed_budget["budget"]["local_observed_cost_stop_cny"] = "7.000000"
        changed_scope = json.loads(json.dumps(plan))
        changed_scope["selection"]["selected_task_ids"] = list(
            reversed(changed_scope["selection"]["selected_task_ids"])
        )

        self.assertNotEqual(depth60_plan_commitment_sha256(changed_budget), original)
        self.assertNotEqual(depth60_plan_commitment_sha256(changed_scope), original)

    def test_component_drift_fails_closed(self) -> None:
        actual = depth60_module.build_depth60_component_hashes(ROOT)
        drifted = dict(actual)
        drifted["source_bundle_sha256"] = "0" * 64
        with patch.object(
            depth60_module,
            "build_depth60_component_hashes",
            return_value=drifted,
        ):
            with self.assertRaises(Phase6RunError) as caught:
                validate_phase6_depth60_plan(ROOT, PLAN)
        self.assertEqual(caught.exception.code, "phase6_depth60_component_drift")

    def test_source_bundle_tracks_dependency_closure_not_unrelated_new_modules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "src/researchops", root / "src/researchops")
            original = phase6_depth60_source_bundle_sha256(root)
            files = phase6_depth60_source_files(root)
            self.assertIn("phase6_runner.py", files)
            self.assertIn("phase6_source_bundle.py", files)
            self.assertNotIn("kimi_chat_nonstreaming.py", files)

            (root / "src/researchops/unrelated_successor.py").write_text(
                "VALUE = 1\n", encoding="utf-8"
            )
            self.assertEqual(phase6_depth60_source_bundle_sha256(root), original)

            runner = root / "src/researchops/phase6_runner.py"
            runner.write_text(
                runner.read_text(encoding="utf-8") + "\n# dependency drift\n",
                encoding="utf-8",
            )
            self.assertNotEqual(phase6_depth60_source_bundle_sha256(root), original)

    def test_source_bundle_parser_captures_absolute_import_from_forms(self) -> None:
        tree = ast.parse(
            "from researchops import helper\n"
            "from researchops.second_helper import VALUE\n"
        )
        self.assertEqual(
            _local_import_targets(tree),
            {"helper.py", "second_helper.py"},
        )

    def test_holdout_rows_are_unchanged_historical_tasks(self) -> None:
        lines = (ROOT / "evals/phase6_agent_tasks.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        holdout = [json.loads(line) for line in lines if '"split":"holdout"' in line]
        self.assertEqual(
            [item["task_id"] for item in holdout],
            ["P6-HOLD-001", "P6-HOLD-002", "P6-HOLD-003", "P6-HOLD-004"],
        )
        self.assertTrue(
            all("repo-local-non-secret-holdout" in item["tags"] for item in holdout)
        )
        canonical_rows = (
            "\n".join(
                line for line in lines if '"split":"holdout"' in line
            )
            + "\n"
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(canonical_rows).hexdigest(),
            "b5c56ecd7561f109085d6be2a7aa2f79e44a458cb52b1b971c8e95c3b3285a9f",
        )

    def test_cli_exposes_only_plan_authorization_and_output(self) -> None:
        parsed = build_parser().parse_args(
            [
                "phase6-run-deepseek-depth60-online",
                "--output-dir",
                "artifacts/depth60-test",
                "--authorization-id",
                "depth60-auth-001",
                "--expected-plan-commitment",
                EXPECTED_COMMITMENT,
                "--authorization-expires-at-utc",
                "2099-01-01T00:00:00Z",
            ]
        )
        self.assertEqual(parsed.plan, DEPTH60_PLAN_PATH)
        self.assertFalse(hasattr(parsed, "api_key"))
        self.assertFalse(parsed.confirm_online)

    def test_offline_ci_binds_history_and_zero_call_successor_validation(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("phase6-validate-deepseek-depth60 `", workflow)
        self.assertIn("evals/phase6_deepseek_depth60_plan_v2.json", workflow)
        self.assertIn("evals/phase6_deepseek_depth60_plan_v3.json", workflow)
        self.assertIn("evals/phase6_deepseek_depth60_plan_v4.json", workflow)
        self.assertIn("evals/phase6_deepseek_depth60_plan_v5.json", workflow)
        self.assertIn(
            "979202e96a5304ad1ba73c54e55f0d38f80baedf8278e37ea8535bb5560ce6af",
            workflow,
        )
        self.assertIn(
            "c36dc0dd0487aa350dc2bd636b45bb494381e0c732c80be7b410be4b9beda612",
            workflow,
        )
        self.assertIn(
            "ae47eebb9f60c73031d2bfc23d00ccc114821a11bc113c0302ce0fb6d9c6926b",
            workflow,
        )
        self.assertIn(
            "f5d43283e3506663383359d24736bd3b82a910e45cc092954f94d86a80e6cd20",
            workflow,
        )
        self.assertIn("$historicalDepth60Bytes -ne 5398", workflow)
        self.assertIn("$historicalDepth60V2Bytes -ne 2170", workflow)
        self.assertIn("$depth60V3Bytes -ne 2850", workflow)
        self.assertIn("$depth60V4Bytes -ne 3087", workflow)
        self.assertIn("$depth60V5Bytes -ne 3120", workflow)
        self.assertIn("$depth60.source_bundle_algorithm -ne \"v2\"", workflow)
        self.assertIn("$depth60.online_execution_authorized -ne $false", workflow)
        self.assertIn("$depth60.network_calls -ne 0", workflow)
        self.assertIn("$depth60.model_calls -ne 0", workflow)


class Phase6Depth60ExecutionGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_expected_commitment_mismatch_precedes_authorization_and_key(self) -> None:
        class ForbiddenEnvironment(dict[str, str]):
            def get(self, key, default=None):
                del key, default
                raise AssertionError("environment must not be read")

        validation = _validate_historical_contract_at_locked_components()
        with patch.object(
            depth60_module,
            "validate_phase6_depth60_plan",
            return_value=validation,
        ):
            with self.assertRaises(Phase6RunError) as caught:
                await run_phase6_depth60_online(
                    project_root=ROOT,
                    plan_path=PLAN,
                    output_directory=ROOT / "artifacts/depth60-wrong-commitment",
                    authorization_id="depth60-auth-001",
                    expected_plan_commitment="0" * 64,
                    authorization_expires_at_utc="2099-01-01T00:00:00Z",
                    confirm_online=True,
                    environment=ForbiddenEnvironment(),
                )
        self.assertEqual(
            caught.exception.code,
            "phase6_depth60_expected_commitment_mismatch",
        )

    async def test_confirmation_gate_precedes_plan_and_environment(self) -> None:
        class ForbiddenEnvironment(dict[str, str]):
            def get(self, key, default=None):
                del key, default
                raise AssertionError("environment must not be read")

        with patch.object(
            depth60_module,
            "validate_phase6_depth60_plan",
            side_effect=AssertionError("plan must not be read"),
        ):
            with self.assertRaises(Phase6RunError) as caught:
                await run_phase6_depth60_online(
                    project_root=ROOT,
                    plan_path=PLAN,
                    output_directory=ROOT / "artifacts/depth60-not-confirmed",
                    authorization_id="depth60-auth-001",
                    expected_plan_commitment=EXPECTED_COMMITMENT,
                    authorization_expires_at_utc="2099-01-01T00:00:00Z",
                    confirm_online=False,
                    environment=ForbiddenEnvironment(),
                )
        self.assertEqual(caught.exception.code, "phase6_online_confirmation_required")

    async def test_short_authorization_window_fails_before_runtime_readiness(self) -> None:
        validation = _validate_historical_contract_at_locked_components()
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat().replace(
            "+00:00", "Z"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts").mkdir()
            with patch.object(
                depth60_module,
                "validate_phase6_depth60_plan",
                return_value=validation,
            ), patch.object(
                depth60_module,
                "phase6_status",
                side_effect=AssertionError("runtime readiness must not be read"),
            ):
                with self.assertRaises(Phase6RunError) as caught:
                    await run_phase6_depth60_online(
                        project_root=root,
                        plan_path=root / DEPTH60_PLAN_PATH,
                        output_directory=root / "artifacts/depth60-short-window",
                        authorization_id="depth60-auth-001",
                        expected_plan_commitment=EXPECTED_COMMITMENT,
                        authorization_expires_at_utc=expiry,
                        confirm_online=True,
                        environment={},
                    )
        self.assertEqual(
            caught.exception.code,
            "phase6_depth60_authorization_window_too_short",
        )

    async def test_output_cannot_overlap_single_use_receipt_namespace(self) -> None:
        validation = _validate_historical_contract_at_locked_components()

        class ForbiddenEnvironment(dict[str, str]):
            def get(self, key, default=None):
                del key, default
                raise AssertionError("environment must not be read")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts").mkdir()
            receipt_root = root / "artifacts/phase6_deepseek_depth60"
            terminal_name = EXPECTED_COMMITMENT + ".terminal.json"
            outputs = (
                receipt_root,
                receipt_root / terminal_name,
                receipt_root / "nested-output",
            )
            for output in outputs:
                with self.subTest(output=output), patch.object(
                    depth60_module,
                    "validate_phase6_depth60_plan",
                    return_value=validation,
                ), patch.object(
                    depth60_module,
                    "validate_eval_v2_dependency_environment",
                    side_effect=AssertionError("dependency gate must not run"),
                ), patch.object(
                    depth60_module,
                    "phase6_status",
                    side_effect=AssertionError("runtime readiness must not run"),
                ):
                    with self.assertRaises(Phase6RunError) as caught:
                        await run_phase6_depth60_online(
                            project_root=root,
                            plan_path=root / DEPTH60_PLAN_PATH,
                            output_directory=output,
                            authorization_id="depth60-auth-output-001",
                            expected_plan_commitment=EXPECTED_COMMITMENT,
                            authorization_expires_at_utc="2099-01-01T00:00:00Z",
                            confirm_online=True,
                            environment=ForbiddenEnvironment(),
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "phase6_depth60_receipt_namespace_reserved",
                    )
                    self.assertFalse(receipt_root.exists())

    async def test_single_use_receipt_blocks_second_output(self) -> None:
        validation = _validate_historical_contract_at_locked_components()
        fake_result = {
            "report": {
                "run_status": "completed",
                "attempted_case_count": 60,
                "completed_case_count": 60,
                "not_started_case_count": 0,
                "harness_error_count": 0,
                "stop_reason": None,
            }
        }

        async def fake_run(**kwargs):
            output = Path(kwargs["output_directory"])
            output.mkdir(parents=True)
            (output / "phase6_manifest.json").write_text("{}\n", encoding="utf-8")
            (output / "phase6_report.json").write_text("{}\n", encoding="utf-8")
            return fake_result

        runner_mock = AsyncMock(side_effect=fake_run)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts").mkdir()
            with patch.object(
                depth60_module,
                "validate_phase6_depth60_plan",
                return_value=validation,
            ), patch.object(
                depth60_module,
                "phase6_status",
                return_value={
                    "online_run_status": "ready_requires_explicit_confirmation",
                    "not_run_reason": None,
                },
            ), patch.object(
                depth60_module,
                "validate_eval_v2_dependency_environment",
                return_value={"status": "valid"},
            ), patch.object(
                depth60_module,
                "_run_phase6_online_evaluation_impl",
                new=runner_mock,
            ):
                first = await run_phase6_depth60_online(
                    project_root=root,
                    plan_path=root / DEPTH60_PLAN_PATH,
                    output_directory=root / "artifacts/depth60-first",
                    authorization_id="depth60-auth-001",
                    expected_plan_commitment=EXPECTED_COMMITMENT,
                    authorization_expires_at_utc="2099-01-01T00:00:00Z",
                    confirm_online=True,
                    environment={"DEEPSEEK_API_KEY": "not-persisted"},
                )
                self.assertEqual(
                    first["depth60_plan"]["plan_commitment_sha256"],
                    validation["plan_commitment_sha256"],
                )
                receipt = (
                    root
                    / "artifacts/phase6_deepseek_depth60"
                    / (validation["plan_commitment_sha256"] + ".receipt.json")
                )
                self.assertTrue(receipt.is_file())
                self.assertNotIn("not-persisted", receipt.read_text(encoding="utf-8"))
                terminal = receipt.with_name(
                    validation["plan_commitment_sha256"] + ".terminal.json"
                )
                self.assertTrue(terminal.is_file())
                kwargs = runner_mock.await_args.kwargs
                self.assertEqual(kwargs["provider"], "deepseek")
                self.assertEqual(kwargs["model"], "deepseek-v4-flash")
                self.assertEqual(kwargs["split"], "development")
                self.assertEqual(kwargs["max_cases"], 60)
                self.assertEqual(
                    kwargs["local_observed_cost_stop_cny"], "6.000000"
                )
                self.assertEqual(kwargs["total_input_tokens_cap"], 750_000)
                self.assertEqual(kwargs["total_output_tokens_cap"], 350_000)
                self.assertEqual(kwargs["total_requests_cap"], 450)
                self.assertEqual(kwargs["total_timeout_seconds"], 5_400)
                self.assertEqual(
                    kwargs["_depth60_plan_binding"]["plan_commitment_sha256"],
                    validation["plan_commitment_sha256"],
                )
                self.assertIsNotNone(kwargs["authorization_deadline_utc"])
                with self.assertRaises(Phase6RunError) as caught:
                    await run_phase6_depth60_online(
                        project_root=root,
                        plan_path=root / DEPTH60_PLAN_PATH,
                        output_directory=root / "artifacts/depth60-second",
                        authorization_id="depth60-auth-002",
                        expected_plan_commitment=EXPECTED_COMMITMENT,
                        authorization_expires_at_utc="2099-01-01T00:00:00Z",
                        confirm_online=True,
                        environment={"DEEPSEEK_API_KEY": "not-persisted"},
                    )
        self.assertEqual(caught.exception.code, "phase6_depth60_plan_already_consumed")


if __name__ == "__main__":
    unittest.main()
