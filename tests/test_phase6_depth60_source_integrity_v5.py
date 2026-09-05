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
    DEPTH60_SUCCESSOR_V4_PLAN_PATH,
    DEPTH60_SUCCESSOR_V5_PLAN_ID,
    DEPTH60_SUCCESSOR_V5_PLAN_PATH,
    build_depth60_component_hashes,
    build_depth60_successor_plan_v5,
    depth60_successor_v5_plan_commitment_sha256,
    run_phase6_depth60_online,
    validate_phase6_depth60_plan,
)
from researchops.phase6_runner import Phase6RunError
from researchops.phase6_source_bundle import (
    completion_telemetry_contract_bundle_sha256,
    completion_telemetry_contract_files,
    completion_telemetry_runtime_bundle_sha256,
)
from tests.test_phase6_depth60_source_integrity_v4 import _copy_file, _copy_v4_root


ROOT = Path(__file__).resolve().parents[1]
V5_PLAN = ROOT / DEPTH60_SUCCESSOR_V5_PLAN_PATH
V5_PLAN_BYTES = 3120
V5_PLAN_FILE_SHA256 = (
    "ff39dd5a1aa09b7bc92b27f9d800b5d51fbd2fd69c2a599b5bf0f25aed490aae"
)
V5_PLAN_COMMITMENT = (
    "8a5474db1e9ad59d501bf109d4a7ecbf616f40599763a20188581e336d379bd7"
)
V5_SOURCE_BUNDLE = (
    "42ad232ad73792453ff6025d836cd3449d27974a633a1a59a019023d676c64a5"
)
V5_RUNTIME_BUNDLE = (
    "606913e5570769e8b5aa430c621c5761d557e747c8b3f82eca67e20540b97acf"
)
V5_CONTRACT_BUNDLE = (
    "7c2bcfba1d7f6d2195ec389c7f98e1ef6e2ef400c6d7101292f6bbc77942bc15"
)
HISTORICAL = {
    DEPTH60_PLAN_PATH: (
        5398,
        "f5d43283e3506663383359d24736bd3b82a910e45cc092954f94d86a80e6cd20",
    ),
    DEPTH60_SUCCESSOR_PLAN_PATH: (
        2170,
        "fc4ca5cc2131efb36d82f1d739f65ad2a026e1c7534f0da9c873942a40c1002f",
    ),
    DEPTH60_SUCCESSOR_V3_PLAN_PATH: (
        2850,
        "5602f940a8627b9c785a1b785d757119a616050ebbc2e31e9dd26aacf448c05e",
    ),
    DEPTH60_SUCCESSOR_V4_PLAN_PATH: (
        3087,
        "ae961e069afa9c842c294f7fb6951e0cf3a4ad86dfcdd16cb96a2c264c232956",
    ),
}
DESIGN_CONTRACT = (
    "evals/provider_completion_first_live_validation_v1/"
    "deepseek_responses_adapter_validation_contract_v1.json"
)
IMPLEMENTATION_CONTRACT = (
    "evals/provider_completion_first_live_validation_v1/"
    "deepseek_responses_adapter_validation_implementation_v2.json"
)


def _copy_v5_root(directory: str) -> Path:
    root = _copy_v4_root(directory)
    _copy_file(root, DEPTH60_SUCCESSOR_V4_PLAN_PATH)
    return root


def _write_plan(root: Path, plan: object) -> Path:
    target = root / DEPTH60_SUCCESSOR_V5_PLAN_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


class Phase6Depth60V5IntegrityTests(unittest.TestCase):
    def test_v1_through_v4_plan_bytes_are_immutable(self) -> None:
        for relative, expected in HISTORICAL.items():
            payload = (ROOT / relative).read_bytes()
            self.assertEqual(
                (len(payload), hashlib.sha256(payload).hexdigest()), expected
            )

    def test_first_live_contracts_are_in_the_fixed_contract_bundle(self) -> None:
        names = completion_telemetry_contract_files(ROOT)
        self.assertEqual(len(names), 13)
        self.assertIn(DESIGN_CONTRACT, names)
        self.assertIn(IMPLEMENTATION_CONTRACT, names)
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v5_root(directory)
            source_before = build_depth60_component_hashes(root, "v2")[
                "source_bundle_sha256"
            ]
            runtime_before = completion_telemetry_runtime_bundle_sha256(root)
            contract_before = completion_telemetry_contract_bundle_sha256(root)
            path = root / IMPLEMENTATION_CONTRACT
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

    def test_temporary_v5_validates_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v5_root(directory)
            plan = build_depth60_successor_plan_v5(
                root, locked_at_utc="2026-09-04T00:00:00.000Z"
            )
            target = _write_plan(root, plan)
            result = validate_phase6_depth60_plan(root, target)
            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["plan_id"], DEPTH60_SUCCESSOR_V5_PLAN_ID)
            self.assertEqual(
                result["plan_commitment_sha256"],
                depth60_successor_v5_plan_commitment_sha256(plan),
            )
            self.assertEqual(
                result["supersedes_plan_id"], "phase6-deepseek-depth60-v4"
            )
            self.assertFalse(result["online_execution_authorized"])
            self.assertEqual((result["network_calls"], result["model_calls"]), (0, 0))

    def test_source_runtime_and_contract_drift_are_independent(self) -> None:
        cases = {
            "source": Path("src/researchops/deepseek_completion_first_live_validation.py"),
            "runtime": Path("src/researchops_completion_telemetry/surface_mapping.py"),
            "contract": Path(IMPLEMENTATION_CONTRACT),
        }
        for component, relative in cases.items():
            with self.subTest(component=component), tempfile.TemporaryDirectory() as directory:
                root = _copy_v5_root(directory)
                plan = build_depth60_successor_plan_v5(
                    root, locked_at_utc="2026-09-04T00:00:00.000Z"
                )
                target = _write_plan(root, plan)
                path = root / relative
                path.write_bytes(path.read_bytes() + b"\n")
                with self.assertRaises(Phase6RunError) as caught:
                    validate_phase6_depth60_plan(root, target)
                self.assertEqual(caught.exception.code, "phase6_depth60_v5_component_drift")

    def test_v5_recomputes_complete_v4_predecessor_commitment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v5_root(directory)
            plan = build_depth60_successor_plan_v5(
                root, locked_at_utc="2026-09-04T00:00:00.000Z"
            )
            target = _write_plan(root, plan)
            v4_path = root / DEPTH60_SUCCESSOR_V4_PLAN_PATH
            original = v4_path.read_bytes()
            changed = original.replace(
                b"2026-09-03T05:11:04.046Z",
                b"2026-09-03T05:11:05.046Z",
            )
            self.assertEqual(len(changed), len(original))
            self.assertNotEqual(changed, original)
            v4_path.write_bytes(changed)
            with patch.object(
                depth60_module,
                "DEPTH60_V4_PLAN_FILE_SHA256",
                hashlib.sha256(changed).hexdigest(),
            ):
                with self.assertRaises(Phase6RunError) as caught:
                    validate_phase6_depth60_plan(root, target)
            self.assertEqual(caught.exception.code, "phase6_depth60_v4_lineage_invalid")

    def test_v5_path_lineage_and_commitment_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v5_root(directory)
            plan = build_depth60_successor_plan_v5(
                root, locked_at_utc="2026-09-04T00:00:00.000Z"
            )
            target = _write_plan(root, plan)
            copy = target.with_name("phase6_deepseek_depth60_plan_v5_copy.json")
            shutil.copy2(target, copy)
            with self.assertRaises(Phase6RunError) as path_error:
                validate_phase6_depth60_plan(root, copy)
            self.assertEqual(path_error.exception.code, "phase6_depth60_plan_path_invalid")

            wrong_lineage = json.loads(json.dumps(plan))
            wrong_lineage["supersedes"]["plan_id"] = "phase6-deepseek-depth60-v3"
            wrong_lineage["plan_commitment_sha256"] = (
                depth60_successor_v5_plan_commitment_sha256(wrong_lineage)
            )
            _write_plan(root, wrong_lineage)
            with self.assertRaises(Phase6RunError) as lineage:
                validate_phase6_depth60_plan(root, target)
            self.assertEqual(lineage.exception.code, "phase6_depth60_v5_lineage_invalid")

            changed = dict(plan)
            changed["locked_at_utc"] = "2026-09-04T00:00:01.000Z"
            _write_plan(root, changed)
            with self.assertRaises(Phase6RunError) as commitment:
                validate_phase6_depth60_plan(root, target)
            self.assertEqual(commitment.exception.code, "phase6_depth60_v5_plan_invalid")

    def test_generated_v5_plan_has_exact_identity_and_validates(self) -> None:
        payload = V5_PLAN.read_bytes()
        self.assertEqual(len(payload), V5_PLAN_BYTES)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), V5_PLAN_FILE_SHA256)
        result = validate_phase6_depth60_plan(ROOT, V5_PLAN)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["plan_commitment_sha256"], V5_PLAN_COMMITMENT)
        self.assertEqual(result["supersedes_plan_id"], "phase6-deepseek-depth60-v4")
        components = result["plan"]["component_hashes"]
        self.assertEqual(components["source_bundle_sha256"], V5_SOURCE_BUNDLE)
        self.assertEqual(
            components["completion_telemetry_runtime_bundle_sha256"],
            V5_RUNTIME_BUNDLE,
        )
        self.assertEqual(
            components["completion_telemetry_contract_bundle_sha256"],
            V5_CONTRACT_BUNDLE,
        )
        self.assertFalse(result["online_execution_authorized"])
        self.assertEqual((result["network_calls"], result["model_calls"]), (0, 0))

    def test_v5_generator_refuses_overwrite_and_preserves_v1_through_v4(self) -> None:
        before = {
            path: (ROOT / path).read_bytes() for path in HISTORICAL
        }
        v5_before = V5_PLAN.read_bytes()
        completed = subprocess.run(
            [sys.executable, "scripts/build_depth60_successor_plan_v5.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(
            completed.stderr.strip(),
            "successor v5 plan already exists: "
            "evals/phase6_deepseek_depth60_plan_v5.json",
        )
        self.assertEqual(V5_PLAN.read_bytes(), v5_before)
        for path, payload in before.items():
            self.assertEqual((ROOT / path).read_bytes(), payload)


class Phase6Depth60V5ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_v5_is_rejected_before_environment_or_output(self) -> None:
        class ForbiddenEnvironment(dict[str, str]):
            def get(self, key, default=None):
                del key, default
                raise AssertionError("environment must not be read")

        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v5_root(directory)
            plan = build_depth60_successor_plan_v5(
                root, locked_at_utc="2026-09-04T00:00:00.000Z"
            )
            target = _write_plan(root, plan)
            output = root / "artifacts/v5-must-not-run"
            with self.assertRaises(Phase6RunError) as caught:
                await run_phase6_depth60_online(
                    project_root=root,
                    plan_path=target,
                    output_directory=output,
                    authorization_id="v5-must-not-run",
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
