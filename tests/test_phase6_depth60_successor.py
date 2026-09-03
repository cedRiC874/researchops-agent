from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import researchops.phase6_depth60 as depth60_module
from researchops.phase6_depth60 import (
    DEPTH60_HISTORICAL_PLAN_COMMITMENT_SHA256,
    DEPTH60_PLAN_PATH,
    DEPTH60_SUCCESSOR_PLAN_ID,
    DEPTH60_SUCCESSOR_PLAN_PATH,
    depth60_plan_commitment_sha256,
    run_phase6_depth60_online,
    validate_phase6_depth60_plan,
)
from researchops.phase6_runner import Phase6RunError


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_PLAN = ROOT / DEPTH60_PLAN_PATH
SUCCESSOR_PLAN = ROOT / DEPTH60_SUCCESSOR_PLAN_PATH
EXPECTED_SUCCESSOR_SOURCE_BUNDLE = (
    "cd46dc03771fc0ebca7ea50798fe2b32fa76248882881f7249c777cd3270ab25"
)
EXPECTED_SUCCESSOR_COMMITMENT = (
    "3077a55e09f3f2137155a68d96a5bda60d8553cc9b5dd36ca83d33bbbc3dcf7e"
)
EXPECTED_SUCCESSOR_FILE_SHA256 = (
    "fc4ca5cc2131efb36d82f1d739f65ad2a026e1c7534f0da9c873942a40c1002f"
)


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("test fixture must be an object")
    return value


class Phase6Depth60SuccessorPlanTests(unittest.TestCase):
    def test_generator_refuses_overwrite_without_printing_absolute_path(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/build_depth60_successor_plan.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(
            completed.stderr.strip(),
            "successor plan already exists: "
            "evals/phase6_deepseek_depth60_plan_v2.json",
        )
        self.assertNotIn(str(ROOT), completed.stderr)

    def test_successor_plan_bytes_are_preserved_and_current_tree_reports_drift(self) -> None:
        payload = SUCCESSOR_PLAN.read_bytes()
        self.assertEqual(len(payload), 2170)
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            EXPECTED_SUCCESSOR_FILE_SHA256,
        )

        plan = _read_json(SUCCESSOR_PLAN)
        self.assertEqual(plan["plan_id"], DEPTH60_SUCCESSOR_PLAN_ID)
        self.assertEqual(
            plan["plan_commitment_sha256"], EXPECTED_SUCCESSOR_COMMITMENT
        )
        self.assertEqual(
            plan["component_hashes"]["source_bundle_sha256"],
            EXPECTED_SUCCESSOR_SOURCE_BUNDLE,
        )
        with self.assertRaises(Phase6RunError) as caught:
            validate_phase6_depth60_plan(ROOT, SUCCESSOR_PLAN)
        self.assertEqual(
            caught.exception.code,
            "phase6_depth60_successor_component_drift",
        )

    def test_validator_rejects_non_whitelisted_plan_paths(self) -> None:
        for relative_path in (
            "evals/phase6_deepseek_depth60_plan_copy.json",
            "evals/../phase6_deepseek_depth60_plan_v2.json",
        ):
            with self.subTest(relative_path=relative_path):
                with self.assertRaises(Phase6RunError) as caught:
                    validate_phase6_depth60_plan(ROOT, relative_path)
                self.assertEqual(
                    caught.exception.code,
                    "phase6_depth60_plan_path_invalid",
                )

    def test_successor_lineage_recomputes_the_entire_historical_plan(self) -> None:
        successor = _read_json(SUCCESSOR_PLAN)
        historical = _read_json(HISTORICAL_PLAN)
        historical["budget"]["local_observed_cost_stop_cny"] = "7.000000"
        self.assertEqual(
            historical["plan_commitment_sha256"],
            DEPTH60_HISTORICAL_PLAN_COMMITMENT_SHA256,
        )
        self.assertNotEqual(
            depth60_plan_commitment_sha256(historical),
            DEPTH60_HISTORICAL_PLAN_COMMITMENT_SHA256,
        )

        def load(path: Path) -> dict[str, object]:
            resolved = Path(path).resolve()
            if resolved == SUCCESSOR_PLAN.resolve():
                return copy.deepcopy(successor)
            if resolved == HISTORICAL_PLAN.resolve():
                return copy.deepcopy(historical)
            raise AssertionError(f"unexpected plan path: {resolved}")

        with patch.object(depth60_module, "_load_json_object", side_effect=load):
            with self.assertRaises(Phase6RunError) as caught:
                validate_phase6_depth60_plan(ROOT, SUCCESSOR_PLAN)
        self.assertEqual(
            caught.exception.code,
            "phase6_depth60_historical_commitment_missing",
        )

    def test_successor_component_drift_fails_closed(self) -> None:
        plan = _read_json(SUCCESSOR_PLAN)
        drifted = dict(plan["component_hashes"])
        drifted["source_bundle_sha256"] = "0" * 64
        with patch.object(
            depth60_module,
            "build_depth60_component_hashes",
            return_value=drifted,
        ):
            with self.assertRaises(Phase6RunError) as caught:
                validate_phase6_depth60_plan(ROOT, SUCCESSOR_PLAN)
        self.assertEqual(
            caught.exception.code,
            "phase6_depth60_successor_component_drift",
        )

    def test_frozen_v2_plan_does_not_silently_rebind_a_new_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            shutil.copytree(
                ROOT / "src" / "researchops",
                tmp / "src" / "researchops",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            for relative in (
                DEPTH60_PLAN_PATH,
                DEPTH60_SUCCESSOR_PLAN_PATH,
                Path("evals/phase6_agent_tasks.jsonl"),
                Path("evals/phase6_splits.json"),
                Path("requirements.lock"),
                Path("pyproject.toml"),
                Path("data/synthetic_trial.csv"),
                Path("data/synthetic_trial_design.json"),
                Path("artifacts/phase3/analysis_bundle.json"),
                Path("artifacts/phase3/effect_estimates.png"),
            ):
                target = tmp / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)

            with self.assertRaises(Phase6RunError) as initial:
                validate_phase6_depth60_plan(
                    tmp,
                    tmp / DEPTH60_SUCCESSOR_PLAN_PATH,
                )
            self.assertEqual(
                initial.exception.code,
                "phase6_depth60_successor_component_drift",
            )

            runner = tmp / "src/researchops/phase6_runner.py"
            runner.write_text(
                runner.read_text(encoding="utf-8") + "\n# real successor drift\n",
                encoding="utf-8",
            )
            with self.assertRaises(Phase6RunError) as caught:
                validate_phase6_depth60_plan(
                    tmp,
                    tmp / DEPTH60_SUCCESSOR_PLAN_PATH,
                )
            self.assertEqual(
                caught.exception.code,
                "phase6_depth60_successor_component_drift",
            )

    def test_successor_commitment_and_lineage_mutations_fail_closed(self) -> None:
        original_load = depth60_module._load_json_object
        base = _read_json(SUCCESSOR_PLAN)
        cases = (
            ("commitment", "phase6_depth60_successor_plan_invalid"),
            ("lineage", "phase6_depth60_successor_lineage_invalid"),
        )
        for case, expected_code in cases:
            with self.subTest(case=case):
                mutated = copy.deepcopy(base)
                if case == "commitment":
                    mutated["locked_at_utc"] = "2026-09-02T00:00:00.000Z"
                else:
                    mutated["supersedes"]["historical_run_superseded"] = True

                def load(path: Path) -> dict[str, object]:
                    if Path(path).resolve() == SUCCESSOR_PLAN.resolve():
                        return copy.deepcopy(mutated)
                    return original_load(Path(path))

                with patch.object(
                    depth60_module,
                    "_load_json_object",
                    side_effect=load,
                ), patch.object(
                    depth60_module,
                    "build_depth60_component_hashes",
                    return_value=copy.deepcopy(base["component_hashes"]),
                ):
                    with self.assertRaises(Phase6RunError) as caught:
                        validate_phase6_depth60_plan(ROOT, SUCCESSOR_PLAN)
                self.assertEqual(caught.exception.code, expected_code)


class Phase6Depth60SuccessorExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_historical_successor_drift_precedes_environment_or_output(self) -> None:
        class ForbiddenEnvironment(dict[str, str]):
            def get(self, key, default=None):
                del key, default
                raise AssertionError("environment must not be read")

        output = ROOT / "artifacts/depth60-successor-must-not-run"
        self.assertFalse(output.exists())
        with self.assertRaises(Phase6RunError) as caught:
            await run_phase6_depth60_online(
                project_root=ROOT,
                plan_path=SUCCESSOR_PLAN,
                output_directory=output,
                authorization_id="successor-must-not-run",
                expected_plan_commitment=EXPECTED_SUCCESSOR_COMMITMENT,
                authorization_expires_at_utc="2099-01-01T00:00:00Z",
                confirm_online=True,
                environment=ForbiddenEnvironment(),
            )
        self.assertEqual(
            caught.exception.code,
            "phase6_depth60_successor_component_drift",
        )
        self.assertTrue(caught.exception.not_run)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
