from __future__ import annotations

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
    DEPTH60_PLAN_PATH,
    DEPTH60_SUCCESSOR_PLAN_PATH,
    DEPTH60_SUCCESSOR_V3_PLAN_PATH,
    DEPTH60_SUCCESSOR_V4_PLAN_ID,
    DEPTH60_SUCCESSOR_V4_PLAN_PATH,
    DEPTH60_V2_PLAN_BYTES,
    DEPTH60_V2_PLAN_FILE_SHA256,
    DEPTH60_V3_PLAN_BYTES,
    DEPTH60_V3_PLAN_FILE_SHA256,
    build_depth60_component_hashes,
    build_depth60_component_hashes_v4,
    build_depth60_successor_plan_v4,
    depth60_successor_v4_plan_commitment_sha256,
    run_phase6_depth60_online,
    validate_phase6_depth60_plan,
)
from researchops.phase6_runner import Phase6RunError
from researchops.phase6_source_bundle import (
    completion_telemetry_contract_bundle_sha256,
    completion_telemetry_contract_files,
    completion_telemetry_runtime_bundle_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
V4_PLAN = ROOT / DEPTH60_SUCCESSOR_V4_PLAN_PATH
V1_PLAN_BYTES = 5398
V1_PLAN_SHA256 = "f5d43283e3506663383359d24736bd3b82a910e45cc092954f94d86a80e6cd20"
V3_PLAN_COMMITMENT = (
    "979202e96a5304ad1ba73c54e55f0d38f80baedf8278e37ea8535bb5560ce6af"
)
V4_PLAN_BYTES = 3087
V4_PLAN_FILE_SHA256 = (
    "ae961e069afa9c842c294f7fb6951e0cf3a4ad86dfcdd16cb96a2c264c232956"
)
V4_PLAN_COMMITMENT = (
    "c36dc0dd0487aa350dc2bd636b45bb494381e0c732c80be7b410be4b9beda612"
)
V4_SOURCE_BUNDLE = (
    "4bd5a48be256b124a7c297f5f98ef4bbadd09df07a038067d7aca211e1fc772c"
)
V4_RUNTIME_BUNDLE = (
    "b0e6f0feb3af416ca73f04df9e8c1cc7f10b5c700e2a20c2a3a7c688273f42c2"
)
V4_CONTRACT_BUNDLE = (
    "1e4028e3bc9c128391a4a73c6753bb5da5e9d19079cbf1068d9acb64980fa56f"
)
HARDENING_CONTRACT = (
    "evals/provider_completion_telemetry_v1/"
    "provider_completion_runtime_hardening_contract_v2.json"
)


def _copy_file(root: Path, relative: Path) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / relative, target)


def _copy_v4_root(directory: str) -> Path:
    root = Path(directory)
    shutil.copytree(
        ROOT / "src/researchops",
        root / "src/researchops",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        ROOT / "src/researchops_completion_telemetry",
        root / "src/researchops_completion_telemetry",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        ROOT / "evals/provider_completion_telemetry_v1",
        root / "evals/provider_completion_telemetry_v1",
    )
    shutil.copytree(
        ROOT / "evals/provider_completion_telemetry_v2",
        root / "evals/provider_completion_telemetry_v2",
    )
    shutil.copytree(
        ROOT / "evals/provider_completion_first_live_validation_v1",
        root / "evals/provider_completion_first_live_validation_v1",
    )
    for relative in (
        DEPTH60_PLAN_PATH,
        DEPTH60_SUCCESSOR_PLAN_PATH,
        DEPTH60_SUCCESSOR_V3_PLAN_PATH,
        Path("evals/phase6_agent_tasks.jsonl"),
        Path("evals/phase6_splits.json"),
        Path("requirements.lock"),
        Path("pyproject.toml"),
        Path("data/synthetic_trial.csv"),
        Path("data/synthetic_trial_design.json"),
        Path("artifacts/phase3/analysis_bundle.json"),
        Path("artifacts/phase3/effect_estimates.png"),
        Path("probe_out_v3.json"),
    ):
        _copy_file(root, relative)
    return root


def _write_plan(root: Path, plan: object) -> Path:
    target = root / DEPTH60_SUCCESSOR_V4_PLAN_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


class Phase6Depth60V4IntegrityTests(unittest.TestCase):
    def test_v1_v2_v3_plan_bytes_and_hashes_are_immutable(self) -> None:
        v1 = (ROOT / DEPTH60_PLAN_PATH).read_bytes()
        v2 = (ROOT / DEPTH60_SUCCESSOR_PLAN_PATH).read_bytes()
        v3 = (ROOT / DEPTH60_SUCCESSOR_V3_PLAN_PATH).read_bytes()
        self.assertEqual((len(v1), hashlib.sha256(v1).hexdigest()), (
            V1_PLAN_BYTES,
            V1_PLAN_SHA256,
        ))
        self.assertEqual((len(v2), hashlib.sha256(v2).hexdigest()), (
            DEPTH60_V2_PLAN_BYTES,
            DEPTH60_V2_PLAN_FILE_SHA256,
        ))
        self.assertEqual((len(v3), hashlib.sha256(v3).hexdigest()), (
            DEPTH60_V3_PLAN_BYTES,
            DEPTH60_V3_PLAN_FILE_SHA256,
        ))

    def test_hardening_contract_is_fixed_and_moves_only_contract_component(self) -> None:
        names = completion_telemetry_contract_files(ROOT)
        self.assertEqual(len(names), 13)
        self.assertIn(HARDENING_CONTRACT, names)
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v4_root(directory)
            source_before = build_depth60_component_hashes(root, "v2")[
                "source_bundle_sha256"
            ]
            runtime_before = completion_telemetry_runtime_bundle_sha256(root)
            contract_before = completion_telemetry_contract_bundle_sha256(root)
            path = root / HARDENING_CONTRACT
            path.write_bytes(path.read_bytes() + b"\n")
            self.assertEqual(
                build_depth60_component_hashes(root, "v2")["source_bundle_sha256"],
                source_before,
            )
            self.assertEqual(
                completion_telemetry_runtime_bundle_sha256(root), runtime_before
            )
            self.assertNotEqual(
                completion_telemetry_contract_bundle_sha256(root), contract_before
            )

    def test_temporary_v4_plan_validates_and_is_offline_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v4_root(directory)
            plan = build_depth60_successor_plan_v4(
                root, locked_at_utc="2026-09-03T00:00:00.000Z"
            )
            target = _write_plan(root, plan)
            result = validate_phase6_depth60_plan(root, target)
            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["plan_id"], DEPTH60_SUCCESSOR_V4_PLAN_ID)
            self.assertEqual(
                result["plan_commitment_sha256"],
                depth60_successor_v4_plan_commitment_sha256(plan),
            )
            self.assertEqual(
                result["supersedes_plan_id"], "phase6-deepseek-depth60-v3"
            )
            self.assertFalse(result["online_execution_authorized"])
            self.assertEqual(result["network_calls"], 0)
            self.assertEqual(result["model_calls"], 0)

    def test_source_runtime_and_contract_component_drift_fail_closed(self) -> None:
        cases = {
            "source": Path("src/researchops/audit.py"),
            "runtime": Path("src/researchops_completion_telemetry/mapping.py"),
            "contract": Path(HARDENING_CONTRACT),
        }
        for component, relative in cases.items():
            with self.subTest(component=component), tempfile.TemporaryDirectory() as directory:
                root = _copy_v4_root(directory)
                plan = build_depth60_successor_plan_v4(
                    root, locked_at_utc="2026-09-03T00:00:00.000Z"
                )
                target = _write_plan(root, plan)
                path = root / relative
                path.write_bytes(path.read_bytes() + b"\n")
                with self.assertRaises(Phase6RunError) as caught:
                    validate_phase6_depth60_plan(root, target)
                self.assertEqual(
                    caught.exception.code,
                    "phase6_depth60_v4_component_drift",
                )

    def test_v4_recomputes_complete_v3_predecessor_commitment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v4_root(directory)
            plan = build_depth60_successor_plan_v4(
                root, locked_at_utc="2026-09-03T00:00:00.000Z"
            )
            target = _write_plan(root, plan)
            v3_path = root / DEPTH60_SUCCESSOR_V3_PLAN_PATH
            original_payload = v3_path.read_bytes()
            tampered_payload = original_payload.replace(
                b"2026-09-03T03:20:01.775Z",
                b"2026-09-03T03:20:02.775Z",
            )
            self.assertNotEqual(tampered_payload, original_payload)
            v3_path.write_bytes(tampered_payload)
            self.assertEqual(len(tampered_payload), DEPTH60_V3_PLAN_BYTES)
            with patch.object(
                depth60_module,
                "DEPTH60_V3_PLAN_FILE_SHA256",
                hashlib.sha256(tampered_payload).hexdigest(),
            ):
                with self.assertRaises(Phase6RunError) as caught:
                    validate_phase6_depth60_plan(root, target)
            self.assertEqual(
                caught.exception.code,
                "phase6_depth60_v3_lineage_invalid",
            )

    def test_v4_path_lineage_and_commitment_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v4_root(directory)
            plan = build_depth60_successor_plan_v4(
                root, locked_at_utc="2026-09-03T00:00:00.000Z"
            )
            target = _write_plan(root, plan)
            copied = target.with_name("phase6_deepseek_depth60_plan_v4_copy.json")
            shutil.copy2(target, copied)
            with self.assertRaises(Phase6RunError) as path_error:
                validate_phase6_depth60_plan(root, copied)
            self.assertEqual(
                path_error.exception.code, "phase6_depth60_plan_path_invalid"
            )

            wrong_lineage = dict(plan)
            wrong_lineage["supersedes"] = dict(plan["supersedes"])
            wrong_lineage["supersedes"]["plan_id"] = "phase6-deepseek-depth60-v2"
            wrong_lineage["plan_commitment_sha256"] = (
                depth60_successor_v4_plan_commitment_sha256(wrong_lineage)
            )
            _write_plan(root, wrong_lineage)
            with self.assertRaises(Phase6RunError) as lineage_error:
                validate_phase6_depth60_plan(root, target)
            self.assertEqual(
                lineage_error.exception.code,
                "phase6_depth60_v4_lineage_invalid",
            )

            changed = dict(plan)
            changed["locked_at_utc"] = "2026-09-03T00:00:01.000Z"
            _write_plan(root, changed)
            with self.assertRaises(Phase6RunError) as commitment_error:
                validate_phase6_depth60_plan(root, target)
            self.assertEqual(
                commitment_error.exception.code,
                "phase6_depth60_v4_plan_invalid",
            )

    @unittest.skipUnless(V4_PLAN.is_file(), "generated v4 plan not present yet")
    def test_generated_v4_plan_is_preserved_and_now_reports_component_drift(self) -> None:
        payload = V4_PLAN.read_bytes()
        self.assertEqual(len(payload), V4_PLAN_BYTES)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), V4_PLAN_FILE_SHA256)
        plan = json.loads(payload.decode("utf-8"))
        self.assertEqual(plan["plan_id"], "phase6-deepseek-depth60-v4")
        self.assertEqual(plan["plan_commitment_sha256"], V4_PLAN_COMMITMENT)
        components = plan["component_hashes"]
        self.assertEqual(components["source_bundle_sha256"], V4_SOURCE_BUNDLE)
        self.assertEqual(
            components["completion_telemetry_runtime_bundle_sha256"],
            V4_RUNTIME_BUNDLE,
        )
        self.assertEqual(
            components["completion_telemetry_contract_bundle_sha256"],
            V4_CONTRACT_BUNDLE,
        )
        with self.assertRaises(Phase6RunError) as caught:
            validate_phase6_depth60_plan(ROOT, V4_PLAN)
        self.assertEqual(caught.exception.code, "phase6_depth60_v4_component_drift")

    @unittest.skipUnless(V4_PLAN.is_file(), "generated v4 plan not present yet")
    def test_v4_generator_refuses_overwrite_and_preserves_v1_v2_v3(self) -> None:
        historical_paths = (
            DEPTH60_PLAN_PATH,
            DEPTH60_SUCCESSOR_PLAN_PATH,
            DEPTH60_SUCCESSOR_V3_PLAN_PATH,
        )
        historical_before = {
            path: (ROOT / path).read_bytes() for path in historical_paths
        }
        v4_before = V4_PLAN.read_bytes()
        completed = subprocess.run(
            [sys.executable, "scripts/build_depth60_successor_plan_v4.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(
            completed.stderr.strip(),
            "successor v4 plan already exists: "
            "evals/phase6_deepseek_depth60_plan_v4.json",
        )
        self.assertEqual(V4_PLAN.read_bytes(), v4_before)
        for path, expected in historical_before.items():
            self.assertEqual((ROOT / path).read_bytes(), expected)


class Phase6Depth60V4ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_v4_is_rejected_before_environment_or_output(self) -> None:
        class ForbiddenEnvironment(dict[str, str]):
            def get(self, key, default=None):
                del key, default
                raise AssertionError("environment must not be read")

        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v4_root(directory)
            plan = build_depth60_successor_plan_v4(
                root, locked_at_utc="2026-09-03T00:00:00.000Z"
            )
            target = _write_plan(root, plan)
            output = root / "artifacts/v4-must-not-run"
            with self.assertRaises(Phase6RunError) as caught:
                await run_phase6_depth60_online(
                    project_root=root,
                    plan_path=target,
                    output_directory=output,
                    authorization_id="v4-must-not-run",
                    authorization_expires_at_utc="2099-01-01T00:00:00Z",
                    expected_plan_commitment=plan["plan_commitment_sha256"],
                    confirm_online=True,
                    environment=ForbiddenEnvironment(),
                )
            self.assertEqual(
                caught.exception.code,
                "phase6_depth60_successor_plan_not_executable",
            )
            self.assertTrue(caught.exception.not_run)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
