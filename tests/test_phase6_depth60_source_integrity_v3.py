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
    DEPTH60_SUCCESSOR_V3_PLAN_ID,
    DEPTH60_SUCCESSOR_V3_PLAN_PATH,
    DEPTH60_V2_PLAN_BYTES,
    DEPTH60_V2_PLAN_FILE_SHA256,
    build_depth60_component_hashes,
    build_depth60_component_hashes_v3,
    build_depth60_successor_plan_v3,
    run_phase6_depth60_online,
    validate_phase6_depth60_plan,
)
from researchops.phase6_runner import Phase6RunError
from researchops.phase6_source_bundle import (
    completion_telemetry_contract_bundle_sha256,
    completion_telemetry_contract_files,
    completion_telemetry_runtime_bundle_sha256,
    completion_telemetry_runtime_files,
)


ROOT = Path(__file__).resolve().parents[1]
V3_PLAN = ROOT / DEPTH60_SUCCESSOR_V3_PLAN_PATH
V1_PLAN_SHA256 = "f5d43283e3506663383359d24736bd3b82a910e45cc092954f94d86a80e6cd20"
V1_PLAN_BYTES = 5398
V3_PLAN_BYTES = 2850
V3_PLAN_FILE_SHA256 = (
    "5602f940a8627b9c785a1b785d757119a616050ebbc2e31e9dd26aacf448c05e"
)
V3_PLAN_COMMITMENT = (
    "979202e96a5304ad1ba73c54e55f0d38f80baedf8278e37ea8535bb5560ce6af"
)
V3_SOURCE_BUNDLE = (
    "f5af13c7475f7c152a3cbe2053c7b0f81f6d4b15034e8a58786ab9e7124a19bf"
)
V3_RUNTIME_BUNDLE = (
    "694c948a1d79fd38532304846577a94a4c75ca1410dd97c363df71a4ba944a63"
)
V3_CONTRACT_BUNDLE = (
    "c9c54c425932770254b9f460d7ab5120401ba02f6802626fb7399d3333700011"
)
RUNTIME_DOMAIN = b"researchops-phase6-completion-telemetry-runtime-bundle-v1\0"
CONTRACT_DOMAIN = b"researchops-phase6-completion-telemetry-contract-bundle-v1\0"


def _copy_file(root: Path, relative: Path) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / relative, target)


def _copy_v3_root(directory: str) -> Path:
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


def _manual_digest(root: Path, domain: bytes, names: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(domain)
    for name in names:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _refresh_file_commitment(commitment: dict[str, object], path: Path) -> None:
    payload = path.read_bytes()
    commitment["bytes"] = len(payload)
    commitment["sha256"] = hashlib.sha256(payload).hexdigest()


class Phase6Depth60TelemetryBundleTests(unittest.TestCase):
    def test_historical_v1_v2_plan_bytes_and_hashes_are_unchanged(self) -> None:
        v1 = (ROOT / DEPTH60_PLAN_PATH).read_bytes()
        v2 = (ROOT / DEPTH60_SUCCESSOR_PLAN_PATH).read_bytes()
        self.assertEqual(len(v1), V1_PLAN_BYTES)
        self.assertEqual(hashlib.sha256(v1).hexdigest(), V1_PLAN_SHA256)
        self.assertEqual(len(v2), DEPTH60_V2_PLAN_BYTES)
        self.assertEqual(
            hashlib.sha256(v2).hexdigest(), DEPTH60_V2_PLAN_FILE_SHA256
        )
        self.assertTrue(V3_PLAN.is_file())

    def test_runtime_bundle_is_all_python_sorted_domain_separated_and_exact(self) -> None:
        names = completion_telemetry_runtime_files(ROOT)
        self.assertEqual(names, tuple(sorted(names)))
        self.assertEqual(len(names), 5)
        self.assertTrue(
            all(
                name.startswith("src/researchops_completion_telemetry/")
                and name.endswith(".py")
                for name in names
            )
        )
        observed = completion_telemetry_runtime_bundle_sha256(ROOT)
        self.assertEqual(observed, _manual_digest(ROOT, RUNTIME_DOMAIN, names))
        self.assertNotEqual(observed, _manual_digest(ROOT, b"wrong-domain\0", names))

    def test_contract_bundle_is_manifest_driven_and_exact(self) -> None:
        names = completion_telemetry_contract_files(ROOT)
        self.assertEqual(names, tuple(sorted(names)))
        self.assertEqual(len(names), 13)
        self.assertIn(
            "evals/provider_completion_telemetry_v1/"
            "provider_completion_record_contract_v1.json",
            names,
        )
        self.assertIn(
            "evals/provider_completion_telemetry_v1/"
            "schemas/provider_completion_record_v1.schema.json",
            names,
        )
        self.assertIn(
            "evals/provider_completion_telemetry_v1/"
            "provider_completion_runtime_hardening_contract_v2.json",
            names,
        )
        self.assertIn(
            "evals/provider_completion_telemetry_v2/"
            "provider_completion_surface_registry_v2.json",
            names,
        )
        self.assertIn("probe_out_v3.json", names)
        observed = completion_telemetry_contract_bundle_sha256(ROOT)
        self.assertEqual(observed, _manual_digest(ROOT, CONTRACT_DOMAIN, names))

    def test_every_runtime_and_contract_input_moves_only_its_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v3_root(directory)
            runtime_names = completion_telemetry_runtime_files(root)
            contract_names = completion_telemetry_contract_files(root)
            runtime_before = completion_telemetry_runtime_bundle_sha256(root)
            contract_before = completion_telemetry_contract_bundle_sha256(root)
            v2_before = build_depth60_component_hashes(root, "v2")[
                "source_bundle_sha256"
            ]

            for name in runtime_names:
                path = root / name
                original = path.read_bytes()
                path.write_bytes(original + b"\n# telemetry runtime drift\n")
                self.assertNotEqual(
                    completion_telemetry_runtime_bundle_sha256(root), runtime_before
                )
                self.assertEqual(
                    completion_telemetry_contract_bundle_sha256(root), contract_before
                )
                self.assertEqual(
                    build_depth60_component_hashes(root, "v2")[
                        "source_bundle_sha256"
                    ],
                    v2_before,
                )
                path.write_bytes(original)

            for name in contract_names:
                path = root / name
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                self.assertEqual(
                    completion_telemetry_runtime_bundle_sha256(root), runtime_before
                )
                try:
                    changed_contract = (
                        completion_telemetry_contract_bundle_sha256(root)
                    )
                except ValueError:
                    # Manifest-bound inputs fail strict runtime verification
                    # before a digest can legitimize their drift.
                    pass
                else:
                    self.assertNotEqual(changed_contract, contract_before)
                self.assertEqual(
                    build_depth60_component_hashes(root, "v2")[
                        "source_bundle_sha256"
                    ],
                    v2_before,
                )
                path.write_bytes(original)

    def test_contract_manifest_escape_and_missing_runtime_package_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v3_root(directory)
            manifest_path = (
                root
                / "evals/provider_completion_telemetry_v2/fixture_manifest_v2.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["fixtures"][0]["file"] = "../../outside.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                completion_telemetry_contract_files(root)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                completion_telemetry_runtime_files(Path(directory))

        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v3_root(directory)
            missing = (
                root
                / "evals/provider_completion_telemetry_v1/"
                "provider_completion_record_contract_v1.json"
            )
            missing.unlink()
            with self.assertRaises(ValueError):
                completion_telemetry_contract_files(root)

    def test_contract_manifest_registry_and_fixture_hash_mismatches_fail_closed(
        self,
    ) -> None:
        for target_kind in ("registry", "fixture"):
            with self.subTest(
                target_kind=target_kind
            ), tempfile.TemporaryDirectory() as directory:
                root = _copy_v3_root(directory)
                manifest_path = (
                    root
                    / "evals/provider_completion_telemetry_v2/fixture_manifest_v2.json"
                )
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                commitment = (
                    manifest["registry"]
                    if target_kind == "registry"
                    else manifest["fixtures"][0]
                )
                commitment["sha256"] = "0" * 64
                _write_json(manifest_path, manifest)

                with self.assertRaisesRegex(
                    ValueError,
                    "failed strict runtime verification",
                ):
                    completion_telemetry_contract_bundle_sha256(root)

    def test_contract_probe_hash_mismatch_fails_after_registry_hash_refresh(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v3_root(directory)
            manifest_path = (
                root
                / "evals/provider_completion_telemetry_v2/fixture_manifest_v2.json"
            )
            registry_path = (
                root
                / "evals/provider_completion_telemetry_v2/"
                "provider_completion_surface_registry_v2.json"
            )
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            inline = next(
                item
                for item in registry["entries"]
                if item["mapping_source"] == "inline_successor"
            )
            inline["source"]["sha256"] = "0" * 64
            _write_json(registry_path, registry)

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            _refresh_file_commitment(manifest["registry"], registry_path)
            _write_json(manifest_path, manifest)

            with self.assertRaisesRegex(
                ValueError,
                "failed strict runtime verification",
            ):
                completion_telemetry_contract_bundle_sha256(root)

    def test_refreshed_outer_fixture_hash_cannot_legitimize_semantic_drift(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v3_root(directory)
            manifest_path = (
                root
                / "evals/provider_completion_telemetry_v2/fixture_manifest_v2.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            fixture_commitment = manifest["fixtures"][0]
            fixture_path = (
                manifest_path.parent / str(fixture_commitment["file"])
            )
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            fixture["provider_id"] = "openai"
            _write_json(fixture_path, fixture)
            _refresh_file_commitment(fixture_commitment, fixture_path)
            _write_json(manifest_path, manifest)

            with self.assertRaisesRegex(
                ValueError,
                "failed strict runtime verification",
            ):
                completion_telemetry_contract_bundle_sha256(root)

    def test_runtime_and_contract_symlinks_fail_closed_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v3_root(directory)
            runtime = root / "src/researchops_completion_telemetry/mapping.py"
            runtime_target = runtime.with_name("mapping-target.py")
            runtime.rename(runtime_target)
            try:
                runtime.symlink_to(runtime_target)
            except OSError:
                self.skipTest("local filesystem does not permit symlink creation")
            with self.assertRaises(ValueError):
                completion_telemetry_runtime_files(root)

        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v3_root(directory)
            contract = (
                root
                / "evals/provider_completion_telemetry_v1/"
                "provider_completion_record_contract_v1.json"
            )
            contract_target = contract.with_name("contract-target.json")
            contract.rename(contract_target)
            contract.symlink_to(contract_target)
            with self.assertRaises(ValueError):
                completion_telemetry_contract_files(root)


class Phase6Depth60V3PlanTests(unittest.TestCase):
    def test_frozen_v3_plan_is_preserved_and_now_reports_component_drift(self) -> None:
        payload = V3_PLAN.read_bytes()
        self.assertEqual(len(payload), V3_PLAN_BYTES)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), V3_PLAN_FILE_SHA256)
        plan = json.loads(payload.decode("utf-8"))
        self.assertEqual(plan["plan_id"], DEPTH60_SUCCESSOR_V3_PLAN_ID)
        self.assertEqual(plan["plan_commitment_sha256"], V3_PLAN_COMMITMENT)
        components = plan["component_hashes"]
        self.assertEqual(components["source_bundle_sha256"], V3_SOURCE_BUNDLE)
        self.assertEqual(
            components["completion_telemetry_runtime_bundle_sha256"],
            V3_RUNTIME_BUNDLE,
        )
        self.assertEqual(
            components["completion_telemetry_contract_bundle_sha256"],
            V3_CONTRACT_BUNDLE,
        )
        with self.assertRaises(Phase6RunError) as caught:
            validate_phase6_depth60_plan(ROOT, V3_PLAN)
        self.assertEqual(
            caught.exception.code,
            "phase6_depth60_v3_component_drift",
        )

    def test_generator_help_does_not_change_any_frozen_plan(self) -> None:
        v1_before = (ROOT / DEPTH60_PLAN_PATH).read_bytes()
        v2_before = (ROOT / DEPTH60_SUCCESSOR_PLAN_PATH).read_bytes()
        v3_before = V3_PLAN.read_bytes()
        completed = subprocess.run(
            [sys.executable, "scripts/build_depth60_successor_plan_v3.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(V3_PLAN.read_bytes(), v3_before)
        self.assertEqual((ROOT / DEPTH60_PLAN_PATH).read_bytes(), v1_before)
        self.assertEqual((ROOT / DEPTH60_SUCCESSOR_PLAN_PATH).read_bytes(), v2_before)

        refusal = subprocess.run(
            [sys.executable, "scripts/build_depth60_successor_plan_v3.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(refusal.returncode, 2)
        self.assertEqual(refusal.stdout, "")
        self.assertEqual(
            refusal.stderr.strip(),
            "successor v3 plan already exists: "
            "evals/phase6_deepseek_depth60_plan_v3.json",
        )
        self.assertEqual(V3_PLAN.read_bytes(), v3_before)
        self.assertEqual((ROOT / DEPTH60_PLAN_PATH).read_bytes(), v1_before)
        self.assertEqual((ROOT / DEPTH60_SUCCESSOR_PLAN_PATH).read_bytes(), v2_before)

    def test_temporary_v3_plan_validates_and_is_offline_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v3_root(directory)
            plan = build_depth60_successor_plan_v3(
                root, locked_at_utc="2026-09-03T00:00:00.000Z"
            )
            target = root / DEPTH60_SUCCESSOR_V3_PLAN_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result = validate_phase6_depth60_plan(root, target)
            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["plan_id"], DEPTH60_SUCCESSOR_V3_PLAN_ID)
            self.assertEqual(result["supersedes_plan_id"], "phase6-deepseek-depth60-v2")
            self.assertFalse(result["online_execution_authorized"])
            self.assertEqual(result["network_calls"], 0)
            self.assertEqual(result["model_calls"], 0)
            self.assertIn(
                "completion_telemetry_runtime_bundle_sha256",
                result["plan"]["component_hashes"],
            )
            self.assertIn(
                "completion_telemetry_contract_bundle_sha256",
                result["plan"]["component_hashes"],
            )

    def test_v3_lineage_recomputes_v2_commitment_after_byte_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v3_root(directory)
            plan = build_depth60_successor_plan_v3(
                root, locked_at_utc="2026-09-03T00:00:00.000Z"
            )
            target = root / DEPTH60_SUCCESSOR_V3_PLAN_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(plan), encoding="utf-8")
            v2 = root / DEPTH60_SUCCESSOR_PLAN_PATH
            text = v2.read_text(encoding="utf-8")
            self.assertIn("2026-09-02T14:32:25.824Z", text)
            v2.write_text(
                text.replace(
                    "2026-09-02T14:32:25.824Z",
                    "2026-09-02T14:32:25.825Z",
                ),
                encoding="utf-8",
                newline="",
            )
            self.assertEqual(len(v2.read_bytes()), DEPTH60_V2_PLAN_BYTES)
            tampered_hash = hashlib.sha256(v2.read_bytes()).hexdigest()
            with patch.object(
                depth60_module,
                "DEPTH60_V2_PLAN_FILE_SHA256",
                tampered_hash,
            ):
                with self.assertRaises(Phase6RunError) as caught:
                    validate_phase6_depth60_plan(root, target)
            self.assertEqual(
                caught.exception.code,
                "phase6_depth60_v2_lineage_invalid",
            )

        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v3_root(directory)
            plan = build_depth60_successor_plan_v3(
                root, locked_at_utc="2026-09-03T00:00:00.000Z"
            )
            target = root / DEPTH60_SUCCESSOR_V3_PLAN_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(plan), encoding="utf-8")
            v1 = root / DEPTH60_PLAN_PATH
            historical = json.loads(v1.read_text(encoding="utf-8"))
            historical["budget"]["local_observed_cost_stop_cny"] = "7.000000"
            v1.write_text(json.dumps(historical), encoding="utf-8")
            with self.assertRaises(Phase6RunError) as caught:
                validate_phase6_depth60_plan(root, target)
            self.assertEqual(
                caught.exception.code,
                "phase6_depth60_v2_lineage_invalid",
            )

    def test_v3_path_whitelist_lineage_component_and_commitment_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v3_root(directory)
            plan = build_depth60_successor_plan_v3(
                root, locked_at_utc="2026-09-03T00:00:00.000Z"
            )
            target = root / DEPTH60_SUCCESSOR_V3_PLAN_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            copy_path = target.with_name("phase6_deepseek_depth60_plan_v3_copy.json")
            shutil.copy2(target, copy_path)
            with self.assertRaises(Phase6RunError) as path_error:
                validate_phase6_depth60_plan(root, copy_path)
            self.assertEqual(path_error.exception.code, "phase6_depth60_plan_path_invalid")

            telemetry = root / "src/researchops_completion_telemetry/mapping.py"
            telemetry.write_bytes(telemetry.read_bytes() + b"\n# drift\n")
            with self.assertRaises(Phase6RunError) as component_error:
                validate_phase6_depth60_plan(root, target)
            self.assertEqual(
                component_error.exception.code,
                "phase6_depth60_v3_component_drift",
            )

        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v3_root(directory)
            plan = build_depth60_successor_plan_v3(
                root, locked_at_utc="2026-09-03T00:00:00.000Z"
            )
            target = root / DEPTH60_SUCCESSOR_V3_PLAN_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(plan), encoding="utf-8")
            v2 = root / DEPTH60_SUCCESSOR_PLAN_PATH
            v2.write_bytes(v2.read_bytes() + b"\n")
            with self.assertRaises(Phase6RunError) as lineage_error:
                validate_phase6_depth60_plan(root, target)
            self.assertEqual(
                lineage_error.exception.code,
                "phase6_depth60_v2_lineage_invalid",
            )

        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v3_root(directory)
            plan = build_depth60_successor_plan_v3(
                root, locked_at_utc="2026-09-03T00:00:00.000Z"
            )
            plan["locked_at_utc"] = "2026-09-03T00:00:01.000Z"
            target = root / DEPTH60_SUCCESSOR_V3_PLAN_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaises(Phase6RunError) as commitment_error:
                validate_phase6_depth60_plan(root, target)
            self.assertEqual(
                commitment_error.exception.code,
                "phase6_depth60_v3_plan_invalid",
            )


class Phase6Depth60V3ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_v3_is_rejected_before_environment_or_output(self) -> None:
        class ForbiddenEnvironment(dict[str, str]):
            def get(self, key, default=None):
                del key, default
                raise AssertionError("environment must not be read")

        with tempfile.TemporaryDirectory() as directory:
            root = _copy_v3_root(directory)
            plan = build_depth60_successor_plan_v3(
                root, locked_at_utc="2026-09-03T00:00:00.000Z"
            )
            target = root / DEPTH60_SUCCESSOR_V3_PLAN_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(plan), encoding="utf-8")
            output = root / "artifacts/v3-must-not-run"
            with self.assertRaises(Phase6RunError) as caught:
                await run_phase6_depth60_online(
                    project_root=root,
                    plan_path=target,
                    output_directory=output,
                    authorization_id="v3-must-not-run",
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
