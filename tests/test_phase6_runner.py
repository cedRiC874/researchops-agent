from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import researchops.phase6_runner as phase6_runner_module

from researchops.phase6_agent import (
    AgentRunRecord,
    AgentToolCall,
    AgentToolObservation,
    AgentUsage,
    LogicalAgentRequest,
)
from researchops.phase6_runner import (
    Phase6RunError,
    phase6_status,
    run_phase6_online_evaluation,
    validate_phase6_suite,
)


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evals" / "phase6_agent_tasks.jsonl"
SPLITS = ROOT / "evals" / "phase6_splits.json"


def _usage() -> AgentUsage:
    return AgentUsage(
        requests=1,
        input_tokens=100,
        output_tokens=25,
        total_tokens=125,
        cached_input_tokens=0,
        complete=True,
    )


class Phase6RunnerPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_and_key_gates_precede_output_and_runner(self) -> None:
        calls: list[LogicalAgentRequest] = []

        async def forbidden_runner(request, backend, **kwargs):
            del backend, kwargs
            calls.append(request)
            raise AssertionError("runner must not start")

        with tempfile.TemporaryDirectory(dir=ROOT / "artifacts") as directory:
            first = Path(directory) / "unconfirmed"
            with self.assertRaises(Phase6RunError) as unconfirmed:
                await run_phase6_online_evaluation(
                    project_root=ROOT,
                    tasks_path=CORPUS,
                    split_manifest_path=SPLITS,
                    output_directory=first,
                    model="test-model",
                    split="development",
                    max_cases=1,
                    environment={"OPENAI_API_KEY": "test-key"},
                    agent_runner=forbidden_runner,
                )
            self.assertEqual(unconfirmed.exception.code, "online_confirmation_required")
            self.assertTrue(unconfirmed.exception.not_run)
            self.assertFalse(first.exists())

            second = Path(directory) / "missing-key"
            with self.assertRaises(Phase6RunError) as missing_key:
                await run_phase6_online_evaluation(
                    project_root=ROOT,
                    tasks_path=CORPUS,
                    split_manifest_path=SPLITS,
                    output_directory=second,
                    model="test-model",
                    split="development",
                    max_cases=1,
                    confirm_online=True,
                    environment={},
                    agent_runner=forbidden_runner,
                )
            self.assertEqual(missing_key.exception.code, "api_key_missing")
            self.assertTrue(missing_key.exception.not_run)
            self.assertFalse(second.exists())
        self.assertEqual(calls, [])

    def test_status_and_contract_validation_are_offline(self) -> None:
        status = phase6_status(environment={})
        self.assertEqual(status["online_run_status"], "not_run")
        self.assertEqual(status["not_run_reason"], "api_key_missing")
        self.assertEqual(status["network_calls"], 0)

        validation = validate_phase6_suite(CORPUS, SPLITS)
        self.assertEqual(validation["status"], "valid")
        self.assertEqual(validation["task_count"], 20)
        self.assertEqual(
            validation["split_counts"], {"development": 16, "holdout": 4}
        )

    def test_cost_estimation_requires_consistent_nonempty_usage(self) -> None:
        base = AgentRunRecord(
            status="completed",
            model="test-model",
            final_output="done",
            tool_calls=(),
            usage=_usage(),
            latency_ms=1.0,
            cost_usd=None,
            approval_interruptions=(),
            tracing_disabled=True,
        )
        prices = (1.0, 2.0)
        self.assertAlmostEqual(
            phase6_runner_module._estimate_cost(base, prices), 0.00015
        )
        invalid = (
            replace(base, usage=replace(_usage(), requests=0)),
            replace(base, usage=replace(_usage(), total_tokens=126)),
            replace(base, usage=replace(_usage(), cached_input_tokens=101)),
        )
        for record in invalid:
            with self.subTest(usage=record.usage):
                self.assertIsNone(
                    phase6_runner_module._estimate_cost(record, prices)
                )


class Phase6RunnerArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def test_injected_runner_publishes_audited_atomic_artifacts(self) -> None:
        requests: list[LogicalAgentRequest] = []

        async def fake_runner(
            request: LogicalAgentRequest,
            backend,
            *,
            api_key: str,
            model: str,
            max_turns: int,
            tracing_disabled: bool,
        ) -> AgentRunRecord:
            self.assertEqual(api_key, "test-secret-key")
            self.assertEqual(model, "test-model")
            self.assertEqual(max_turns, 5)
            self.assertTrue(tracing_disabled)
            requests.append(request)
            payload = backend.inspect_dataset(request.dataset_id)
            self.assertEqual(payload["row_count"], 240)
            call_id = "sdk-inspect-1"
            return AgentRunRecord(
                status="completed",
                model=model,
                final_output="聚合检查：240 行、10 列，其中 38 行存在缺失。",
                tool_calls=(
                    AgentToolCall(
                        call_id,
                        "inspect_dataset",
                        {"dataset_id": "synthetic_trial"},
                        "succeeded",
                    ),
                ),
                usage=_usage(),
                latency_ms=12.5,
                cost_usd=None,
                approval_interruptions=(),
                tracing_disabled=True,
                tool_observations=(
                    AgentToolObservation(
                        call_id,
                        "inspect_dataset",
                        "succeeded",
                        (),
                        None,
                        hashlib.sha256(b"aggregate-profile").hexdigest(),
                    ),
                ),
            )

        with tempfile.TemporaryDirectory(dir=ROOT / "artifacts") as directory:
            output = Path(directory) / "online-eval"
            result = await run_phase6_online_evaluation(
                project_root=ROOT,
                tasks_path=CORPUS,
                split_manifest_path=SPLITS,
                output_directory=output,
                model="test-model",
                split="development",
                max_cases=1,
                max_turns=5,
                confirm_online=True,
                input_price_per_million_usd=2.0,
                output_price_per_million_usd=8.0,
                environment={"OPENAI_API_KEY": "test-secret-key"},
                agent_runner=fake_runner,
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["report"]["included"], 1)
            self.assertEqual(result["report"]["passed"], 1)
            self.assertEqual(result["report"]["cost"]["status"], "complete")
            self.assertAlmostEqual(result["report"]["cost"]["total_usd"], 0.0004)
            self.assertEqual(len(requests), 1)
            self.assertIn("synthetic_trial", requests[0].research_question)
            self.assertEqual(requests[0].dataset_id, "synthetic_trial")

            expected_files = {
                "phase6_audit.sqlite3",
                "phase6_audit_index.json",
                "phase6_manifest.json",
                "phase6_report.json",
                "phase6_results.jsonl",
                "phase6_summary.md",
            }
            self.assertEqual(
                {path.name for path in output.iterdir()}, expected_files
            )
            manifest = json.loads(
                (output / "phase6_manifest.json").read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["audit"]["all_chains_valid"])
            self.assertEqual(
                manifest["task_corpus"]["golden_isolation"],
                "only Phase6Task.public_input is transformed into LogicalAgentRequest",
            )
            database_bytes = (output / "phase6_audit.sqlite3").read_bytes()
            self.assertNotIn(b"test-secret-key", database_bytes)
            connection = sqlite3.connect(output / "phase6_audit.sqlite3")
            try:
                run = connection.execute(
                    "SELECT status FROM runs WHERE run_id = ?",
                    ("RUN-PHASE6-P6-DEV-001",),
                ).fetchone()
                self.assertEqual(run, ("completed",))
                attempts = connection.execute(
                    "SELECT COUNT(*) FROM tool_attempts"
                ).fetchone()
                self.assertEqual(attempts, (1,))
            finally:
                connection.close()
            self.assertFalse(
                any(path.name.startswith(".researchops-phase6-") for path in Path(directory).iterdir())
            )

    async def test_runner_error_is_included_failure_and_audited(self) -> None:
        async def failing_runner(request, backend, **kwargs):
            del request, backend, kwargs
            raise ConnectionError("do not persist this remote body")

        with tempfile.TemporaryDirectory(dir=ROOT / "artifacts") as directory:
            output = Path(directory) / "failed-online-eval"
            result = await run_phase6_online_evaluation(
                project_root=ROOT,
                tasks_path=CORPUS,
                split_manifest_path=SPLITS,
                output_directory=output,
                model="test-model",
                split="development",
                max_cases=1,
                confirm_online=True,
                environment={"OPENAI_API_KEY": "test-key"},
                agent_runner=failing_runner,
            )
            report = result["report"]
            self.assertEqual(report["included"], 1)
            self.assertEqual(report["excluded_not_run"], 0)
            self.assertEqual(report["passed"], 0)
            self.assertEqual(report["harness_error_count"], 0)
            self.assertEqual(report["execution_failure_count"], 1)
            self.assertEqual(report["usage"]["status"], "unavailable")
            self.assertEqual(report["cost"]["status"], "unavailable")
            row = json.loads(
                (output / "phase6_results.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
            self.assertEqual(row["execution_status"], "runner_error")
            self.assertEqual(row["error_code"], "phase6_agent_runner_failed")
            self.assertNotIn(
                "remote body",
                (output / "phase6_results.jsonl").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
