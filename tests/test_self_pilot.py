from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import researchops.cli as cli_module
from researchops.eval_v2_contracts import EvalV2ContractError
from researchops.model_providers import ProviderModel
from researchops.self_pilot import (
    create_self_pilot_session,
    get_next_self_pilot_task,
    record_self_pilot_feedback,
    run_self_pilot_task,
    summarize_self_pilot,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = REPO_ROOT / "evals" / "v2" / "public_tasks.jsonl"
DATASETS_PATH = REPO_ROOT / "evals" / "v2" / "external_datasets.json"
DATASET_IDS = (
    "palmer_penguins_v0_1_0",
    "uci_parkinsons_telemonitoring_189",
    "uci_heart_disease_cleveland_45",
)


class FakeProvider:
    provider_id = "deepseek"
    api_key_env = "DEEPSEEK_API_KEY"
    transport_id = "openai_compatible_responses"

    def validate_model(self, model_id: str) -> str:
        return model_id

    @asynccontextmanager
    async def open_model(self, *, model_id, api_key, timeout_seconds=120.0):
        del timeout_seconds
        if api_key != "test-key":
            raise AssertionError("unexpected key")
        yield ProviderModel(
            provider_id=self.provider_id,
            model_id=model_id,
            transport_id=self.transport_id,
            sdk_model="fake-model",
        )


class FakeRunner:
    def __init__(self, output: str = "Pilot aggregate answer") -> None:
        self.output = output
        self.calls = 0

    async def run(self, agent, prompt, *, context, max_turns, run_config):
        del agent, prompt, context, max_turns, run_config
        self.calls += 1
        usage = SimpleNamespace(requests=1, input_tokens=20, output_tokens=5)
        return SimpleNamespace(
            final_output=self.output,
            context_wrapper=SimpleNamespace(usage=usage),
        )


class SelfPilotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "artifacts").mkdir()
        self.registry_path = self._write_registry()

    def _write_registry(self) -> Path:
        registry_root = self.root / "artifacts" / "prepared"
        registry_root.mkdir()
        entries = []
        for dataset_id in DATASET_IDS:
            path = registry_root / f"{dataset_id}.csv"
            path.write_text("value\n1\n", encoding="utf-8")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append(
                {
                    "dataset_id": dataset_id,
                    "relative_path": path.name,
                    "prepared_sha256": digest,
                    "prepared_bytes": path.stat().st_size,
                    "row_count": 1,
                    "column_count": 1,
                    "source_asset_sha256": "a" * 64,
                    "preparation_version": "1.0",
                    "privacy_class": "public_test_data",
                    "model_access": "aggregate_tools_only",
                    "domain": "test_domain",
                    "repeated_subjects": False,
                    "analysis_boundaries": ["test_only", "no_row_access"],
                    "transformations": ["test_fixture"],
                }
            )
        registry = {
            "schema_version": "1.0",
            "registry_id": "self-pilot-test-registry",
            "dataset_manifest_sha256": "b" * 64,
            "entries": entries,
        }
        registry_path = registry_root / "logical_dataset_registry.json"
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        return registry_path

    def _create_session(self, count: int = 12) -> Path:
        session = self.root / "artifacts" / "pilot-01"
        create_self_pilot_session(
            project_root=self.root,
            output_directory=session,
            tasks_path=TASKS_PATH,
            dataset_manifest_path=DATASETS_PATH,
            task_count=count,
        )
        return session

    def test_create_exports_only_blinded_balanced_public_inputs(self) -> None:
        session = self._create_session()
        pack = json.loads((session / "pilot_tasks.json").read_text(encoding="utf-8"))
        serialized = json.dumps(pack, ensure_ascii=False)

        self.assertEqual(pack["task_count"], 12)
        self.assertFalse(pack["goldens_included"])
        self.assertNotIn('"expected"', serialized)
        self.assertNotIn("required_phrases", serialized)
        datasets = [item["context"]["dataset_id"] for item in pack["tasks"]]
        counts = {dataset_id: datasets.count(dataset_id) for dataset_id in DATASET_IDS}
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)
        next_task = get_next_self_pilot_task(
            project_root=self.root, session_directory=session
        )
        self.assertEqual(next_task["status"], "pending_provider_run")
        self.assertNotIn("expected", next_task["task"])

    def test_session_is_non_overwriting_and_hash_bound(self) -> None:
        session = self._create_session()
        with self.assertRaises(EvalV2ContractError) as overwrite:
            create_self_pilot_session(
                project_root=self.root,
                output_directory=session,
                tasks_path=TASKS_PATH,
                dataset_manifest_path=DATASETS_PATH,
            )
        self.assertEqual(overwrite.exception.code, "self_pilot_output_exists")

        tasks_file = session / "pilot_tasks.json"
        tasks_file.write_text(tasks_file.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaises(EvalV2ContractError) as tampered:
            get_next_self_pilot_task(
                project_root=self.root, session_directory=session
            )
        self.assertEqual(tampered.exception.code, "self_pilot_session_invalid")

    def test_same_task_pack_gets_unique_session_instance_ids(self) -> None:
        first = self.root / "artifacts" / "pilot-instance-a"
        second = self.root / "artifacts" / "pilot-instance-b"

        first_result = create_self_pilot_session(
            project_root=self.root,
            output_directory=first,
            tasks_path=TASKS_PATH,
            dataset_manifest_path=DATASETS_PATH,
            task_count=12,
        )
        second_result = create_self_pilot_session(
            project_root=self.root,
            output_directory=second,
            tasks_path=TASKS_PATH,
            dataset_manifest_path=DATASETS_PATH,
            task_count=12,
        )

        self.assertEqual(first_result["pilot_pack_id"], second_result["pilot_pack_id"])
        self.assertNotEqual(
            first_result["session_instance_id"],
            second_result["session_instance_id"],
        )
        for directory, result in (
            (first, first_result),
            (second, second_result),
        ):
            state = json.loads((directory / "pilot_state.json").read_text(encoding="utf-8"))
            blinded = json.loads((directory / "pilot_tasks.json").read_text(encoding="utf-8"))
            self.assertEqual(state["schema_version"], "1.1")
            self.assertEqual(state["session_id"], result["session_instance_id"])
            self.assertEqual(state["session_instance_id"], blinded["session_instance_id"])
            self.assertEqual(state["pilot_pack_id"], blinded["pilot_pack_id"])

    def test_legacy_schema_derives_stable_instance_without_rewriting_state(self) -> None:
        session = self._create_session(count=1)
        state_path = session / "pilot_state.json"
        tasks_path = session / "pilot_tasks.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        blinded = json.loads(tasks_path.read_text(encoding="utf-8"))
        legacy_id = "SELF-PILOT-ABCDEF123456"
        for payload in (state, blinded):
            payload["schema_version"] = "1.0"
            payload["session_id"] = legacy_id
            payload.pop("session_instance_id")
            payload.pop("pilot_pack_id")
        tasks_path.write_text(
            json.dumps(blinded, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        state["blinded_tasks_sha256"] = hashlib.sha256(tasks_path.read_bytes()).hexdigest()
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        state_before = state_path.read_bytes()

        next_task = get_next_self_pilot_task(
            project_root=self.root, session_directory=session
        )
        first_summary = summarize_self_pilot(
            project_root=self.root, session_directory=session
        )
        second_summary = summarize_self_pilot(
            project_root=self.root, session_directory=session
        )

        self.assertEqual(next_task["status"], "pending_provider_run")
        self.assertEqual(first_summary["session_instance_id_source"], "legacy_derived")
        self.assertEqual(
            first_summary["session_instance_id"], second_summary["session_instance_id"]
        )
        self.assertEqual(first_summary["pilot_pack_id"], "PILOT-PACK-ABCDEF123456")
        self.assertEqual(state_path.read_bytes(), state_before)

    def test_run_record_and_summary_complete_blind_feedback_cycle(self) -> None:
        session = self._create_session(count=1)
        task_id = get_next_self_pilot_task(
            project_root=self.root, session_directory=session
        )["task"]["task_id"]
        runner = FakeRunner()

        run_result = run_self_pilot_task(
            project_root=self.root,
            session_directory=session,
            tasks_path=TASKS_PATH,
            dataset_manifest_path=DATASETS_PATH,
            registry_path=self.registry_path,
            provider=FakeProvider(),
            model_id="deepseek-v4-flash",
            api_key="test-key",
            task_id=task_id,
            confirm_online=True,
            sdk_runner=runner,
        )

        self.assertEqual(run_result["status"], "ran")
        self.assertEqual(run_result["agent_output"], "Pilot aggregate answer")
        self.assertTrue(run_result["machine_score_hidden_until_feedback"])
        self.assertNotIn("machine_pass", run_result)
        state_text = (session / "pilot_state.json").read_text(encoding="utf-8")
        self.assertNotIn("Pilot aggregate answer", state_text)
        self.assertEqual(runner.calls, 1)

        feedback = record_self_pilot_feedback(
            project_root=self.root,
            session_directory=session,
            task_id=task_id,
            accepted=True,
            first_pass=True,
            manual_revisions=0,
            duration_seconds=42.5,
            critical_error=False,
            safety_concern=False,
            clarification_useful=None,
            notes="Clear aggregate answer.",
        )
        self.assertEqual(feedback["status"], "recorded")
        self.assertIn("machine_pass", feedback)
        self.assertEqual(feedback["next"]["status"], "complete")

        summary = summarize_self_pilot(
            project_root=self.root, session_directory=session
        )
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["human_acceptance_rate"], 1.0)
        self.assertEqual(summary["first_pass_rate"], 1.0)
        self.assertEqual(summary["median_duration_seconds"], 42.5)
        self.assertFalse(summary["external_validation_claim_allowed"])
        self.assertTrue((session / "pilot_summary.md").is_file())

        with self.assertRaises(EvalV2ContractError) as rerun:
            run_self_pilot_task(
                project_root=self.root,
                session_directory=session,
                tasks_path=TASKS_PATH,
                dataset_manifest_path=DATASETS_PATH,
                registry_path=self.registry_path,
                provider=FakeProvider(),
                model_id="deepseek-v4-flash",
                api_key="test-key",
                task_id=task_id,
                confirm_online=True,
                sdk_runner=FakeRunner(),
            )
        self.assertEqual(rerun.exception.code, "self_pilot_task_already_run")

    def test_unsafe_provider_output_is_redacted_and_not_persisted(self) -> None:
        session = self._create_session(count=1)
        task_id = get_next_self_pilot_task(
            project_root=self.root, session_directory=session
        )["task"]["task_id"]

        result = run_self_pilot_task(
            project_root=self.root,
            session_directory=session,
            tasks_path=TASKS_PATH,
            dataset_manifest_path=DATASETS_PATH,
            registry_path=self.registry_path,
            provider=FakeProvider(),
            model_id="deepseek-v4-flash",
            api_key="test-key",
            task_id=task_id,
            confirm_online=True,
            sdk_runner=FakeRunner(r"unsafe C:\secret\data.csv sk-secret"),
        )

        self.assertTrue(result["output_redacted"])
        self.assertEqual(
            result["agent_output"],
            "[OUTPUT_REDACTED_BY_SELF_PILOT_SAFETY_FILTER]",
        )
        state = (session / "pilot_state.json").read_text(encoding="utf-8")
        self.assertNotIn("secret", state)

    def test_cli_self_pilot_run_resolves_provider_before_session_validation(self) -> None:
        output = io.StringIO()
        arguments = [
            "researchops",
            "self-pilot-run",
            "--session-dir",
            "artifacts/test-self-pilot-missing-session-import-regression",
            "--registry",
            "artifacts/test-self-pilot-missing-registry-import-regression.json",
            "--provider",
            "deepseek",
            "--model",
            "deepseek-v4-flash",
            "--confirm-online",
        ]

        with patch.object(sys, "argv", arguments), redirect_stdout(output):
            exit_code = cli_module.main()

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 4)
        self.assertEqual(payload["error_code"], "self_pilot_session_invalid")

    def test_cli_self_pilot_run_accepts_controlled_anthropic_choice(self) -> None:
        parsed = cli_module.build_parser().parse_args(
            [
                "self-pilot-run",
                "--session-dir",
                "artifacts/offline-anthropic-session",
                "--registry",
                "artifacts/offline-anthropic-registry.json",
                "--provider",
                "anthropic",
                "--model",
                "claude-sonnet-5",
            ]
        )
        self.assertEqual(parsed.provider, "anthropic")
        self.assertEqual(parsed.model, "claude-sonnet-5")


if __name__ == "__main__":
    unittest.main()
