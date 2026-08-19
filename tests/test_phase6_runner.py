from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import researchops.phase6_runner as phase6_runner_module

from researchops.phase6_agent import (
    AgentRunRecord,
    AgentToolCall,
    AgentToolObservation,
    AgentUsage,
    LogicalAgentRequest,
)
from researchops.cli import build_parser
from researchops.model_providers import ProviderConfigurationError
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
        self.assertEqual(validation["task_schema_version"], "1.2")
        self.assertEqual(validation["task_count"], 20)
        self.assertEqual(
            validation["split_counts"], {"development": 16, "holdout": 4}
        )

    def test_safe_text_preserves_slash_terms_and_redacts_absolute_paths(self) -> None:
        terminology = (
            "隐私建议包括 k-匿名/l-多样性，并比较 sensitivity/specificity、"
            "input/output 与 and/or。"
        )
        self.assertEqual(phase6_runner_module._safe_text(terminology), terminology)

        path_cases = (
            "/tmp/secret.csv",
            "路径 '/home/user/file' 不应保留",
            "路径=(/tmp/inside-parentheses.json)",
            "path=/home/user/after-equals.txt",
            r"C:\secret\study.csv",
            "C:/secret/study.csv",
            r"\\server\share\study.csv",
            "//server/share/study.csv",
            "https://example.test/private/report.csv",
        )
        for value in path_cases:
            with self.subTest(value=value):
                cleaned = phase6_runner_module._safe_text(value)
                self.assertIsInstance(cleaned, str)
                self.assertIn("PATH_REDACTED", cleaned)
                self.assertNotIn("secret", cleaned.casefold())
                self.assertNotIn("study.csv", cleaned.casefold())
                self.assertNotIn("inside-parentheses", cleaned.casefold())
                self.assertNotIn("after-equals", cleaned.casefold())
                self.assertNotIn("example.test", cleaned.casefold())

        mixed = "保留 k-匿名/l-多样性；路径=\"/tmp/secret.csv\"。"
        cleaned_mixed = phase6_runner_module._safe_text(mixed)
        self.assertIn("k-匿名/l-多样性", cleaned_mixed)
        self.assertIn("PATH_REDACTED", cleaned_mixed)
        self.assertNotIn("/tmp/secret.csv", cleaned_mixed)

    def test_safe_record_preserves_completion_projection(self) -> None:
        record = AgentRunRecord(
            status="completed",
            model="fixture-model",
            final_output="partial",
            tool_calls=(),
            usage=AgentUsage(1, 1, 1, 2, 0, True),
            latency_ms=1.0,
            cost_usd=None,
            approval_interruptions=(),
            tracing_disabled=True,
            completion_integrity=False,
            completion_error_code="output_limit_suspected",
        )
        projected = phase6_runner_module._safe_record(record)
        self.assertFalse(projected["completion_integrity"])
        self.assertEqual(
            projected["completion_error_code"], "output_limit_suspected"
        )

    def test_provider_status_is_offline_and_key_scoped(self) -> None:
        deepseek_missing = phase6_status(
            provider="deepseek",
            environment={"OPENAI_API_KEY": "openai-only"},
        )
        self.assertEqual(deepseek_missing["evaluation_mode"], "online_agents_sdk")
        self.assertEqual(deepseek_missing["provider"], "deepseek")
        self.assertEqual(
            deepseek_missing["transport"], "openai_compatible_responses"
        )
        self.assertEqual(deepseek_missing["online_run_status"], "not_run")
        self.assertEqual(deepseek_missing["not_run_reason"], "api_key_missing")
        self.assertFalse(deepseek_missing["sdk"]["api_key_configured"])
        self.assertEqual(
            deepseek_missing["sdk"]["api_key_environment_variable"],
            "DEEPSEEK_API_KEY",
        )
        self.assertEqual(deepseek_missing["network_calls"], 0)

        deepseek_ready = phase6_status(
            provider="deepseek",
            environment={"DEEPSEEK_API_KEY": "deepseek-only"},
        )
        self.assertEqual(
            deepseek_ready["online_run_status"],
            "ready_requires_explicit_confirmation",
        )
        self.assertTrue(deepseek_ready["sdk"]["api_key_configured"])

        openai_missing = phase6_status(
            provider="openai",
            environment={"DEEPSEEK_API_KEY": "deepseek-only"},
        )
        self.assertEqual(openai_missing["online_run_status"], "not_run")
        self.assertFalse(openai_missing["sdk"]["api_key_configured"])
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "ambient-openai", "DEEPSEEK_API_KEY": "ambient-ds"},
            clear=False,
        ):
            explicit_empty = phase6_status(provider="deepseek", environment={})
        self.assertEqual(explicit_empty["online_run_status"], "not_run")
        self.assertFalse(explicit_empty["sdk"]["api_key_configured"])
        with self.assertRaises(ProviderConfigurationError):
            phase6_status(provider="unknown", environment={})

    async def test_deepseek_preflight_rejects_model_key_and_legacy_pricing_before_output(self) -> None:
        calls: list[LogicalAgentRequest] = []

        async def forbidden_runner(request, backend, **kwargs):
            del backend, kwargs
            calls.append(request)
            raise AssertionError("runner must not start")

        with tempfile.TemporaryDirectory(dir=ROOT / "artifacts") as directory:
            invalid_model_output = Path(directory) / "invalid-model"
            with self.assertRaises(ProviderConfigurationError) as invalid_model:
                await run_phase6_online_evaluation(
                    project_root=ROOT,
                    tasks_path=CORPUS,
                    split_manifest_path=SPLITS,
                    output_directory=invalid_model_output,
                    provider="deepseek",
                    model="gpt-5.4-mini",
                    split="development",
                    max_cases=1,
                    confirm_online=True,
                    environment={"DEEPSEEK_API_KEY": "test-key"},
                    agent_runner=forbidden_runner,
                )
            self.assertEqual(
                invalid_model.exception.code, "provider_model_not_allowed"
            )
            self.assertFalse(invalid_model_output.exists())

            wrong_key_output = Path(directory) / "wrong-key"
            with self.assertRaises(Phase6RunError) as missing_key:
                await run_phase6_online_evaluation(
                    project_root=ROOT,
                    tasks_path=CORPUS,
                    split_manifest_path=SPLITS,
                    output_directory=wrong_key_output,
                    provider="deepseek",
                    model="deepseek-v4-flash",
                    split="development",
                    max_cases=1,
                    confirm_online=True,
                    environment={"OPENAI_API_KEY": "wrong-provider-key"},
                    agent_runner=forbidden_runner,
                )
            self.assertEqual(missing_key.exception.code, "api_key_missing")
            self.assertTrue(missing_key.exception.not_run)
            self.assertFalse(wrong_key_output.exists())

            priced_output = Path(directory) / "legacy-price"
            with self.assertRaises(Phase6RunError) as unsupported_price:
                await run_phase6_online_evaluation(
                    project_root=ROOT,
                    tasks_path=CORPUS,
                    split_manifest_path=SPLITS,
                    output_directory=priced_output,
                    provider="deepseek",
                    model="deepseek-v4-flash",
                    split="development",
                    max_cases=1,
                    confirm_online=True,
                    input_price_per_million_usd=0.14,
                    output_price_per_million_usd=0.28,
                    environment={"DEEPSEEK_API_KEY": "test-key"},
                    agent_runner=forbidden_runner,
                )
            self.assertEqual(
                unsupported_price.exception.code,
                "phase6_pricing_unsupported_for_provider",
            )
            self.assertFalse(priced_output.exists())
        self.assertEqual(calls, [])

    def test_cli_provider_selection_is_explicit_for_online_runs(self) -> None:
        status = build_parser().parse_args(["phase6-status", "--provider", "deepseek"])
        self.assertEqual(status.provider, "deepseek")
        online = build_parser().parse_args(
            [
                "phase6-run-online",
                "--output-dir",
                "artifacts/test-provider-cli",
                "--provider",
                "deepseek",
                "--model",
                "deepseek-v4-flash",
                "--split",
                "development",
                "--max-cases",
                "1",
            ]
        )
        self.assertEqual(online.provider, "deepseek")
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "phase6-run-online",
                    "--output-dir",
                    "artifacts/test-provider-cli",
                    "--model",
                    "deepseek-v4-flash",
                    "--split",
                    "development",
                    "--max-cases",
                    "1",
                ]
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
            self.assertEqual(result["evaluation_mode"], "online_agents_sdk")
            self.assertEqual(result["provider"], "openai")
            self.assertEqual(result["transport"], "openai_responses")
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
            self.assertEqual(manifest["schema_version"], "1.1")
            self.assertEqual(manifest["runner_version"], "1.6.0")
            self.assertEqual(manifest["selection"]["max_output_tokens"], 2000)
            self.assertEqual(manifest["provider"], "openai")
            self.assertEqual(manifest["transport"], "openai_responses")
            self.assertEqual(manifest["runtime"]["provider"], "openai")
            self.assertEqual(
                manifest["audit"]["provider_client_max_retries"], 0
            )
            self.assertEqual(
                manifest["task_corpus"]["golden_isolation"],
                "only Phase6Task.public_input is transformed into LogicalAgentRequest",
            )
            self.assertEqual(manifest["task_corpus"]["schema_version"], "1.2")
            summary = (output / "phase6_summary.md").read_text(encoding="utf-8")
            self.assertIn("单次响应输出上限：2000 tokens", summary)
            self.assertIn("回答完整性准确率/覆盖率", summary)
            self.assertIn("Evidence 标签完整性准确率", summary)
            self.assertIn("Numeric CLAIM 任务准确率", summary)
            self.assertIn("Evidence precision", summary)
            result_row = json.loads(
                (output / "phase6_results.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
            self.assertTrue(result_row["observation"]["completion_integrity"])
            self.assertIsNone(result_row["observation"]["completion_error_code"])
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
                providers = connection.execute(
                    "SELECT DISTINCT provider FROM model_calls"
                ).fetchall()
                self.assertEqual(providers, [("openai",)])
            finally:
                connection.close()
            self.assertFalse(
                any(path.name.startswith(".researchops-phase6-") for path in Path(directory).iterdir())
            )

    async def test_deepseek_injected_runner_records_provider_and_unknown_cost(self) -> None:
        seen_provider = None

        async def fake_runner(
            request: LogicalAgentRequest,
            backend,
            *,
            api_key: str,
            provider,
            model: str,
            max_turns: int,
            tracing_disabled: bool,
        ) -> AgentRunRecord:
            nonlocal seen_provider
            del max_turns
            self.assertEqual(api_key, "deepseek-test-secret")
            self.assertTrue(tracing_disabled)
            self.assertEqual(provider.provider_id, "deepseek")
            self.assertEqual(provider.api_key_env, "DEEPSEEK_API_KEY")
            self.assertEqual(provider.transport_id, "openai_compatible_responses")
            seen_provider = provider
            payload = backend.inspect_dataset(request.dataset_id)
            self.assertEqual(payload["row_count"], 240)
            call_id = "sdk-deepseek-inspect-1"
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
                latency_ms=8.5,
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
                        hashlib.sha256(b"deepseek-aggregate-profile").hexdigest(),
                    ),
                ),
                provider="deepseek",
                transport="openai_compatible_responses",
            )

        with tempfile.TemporaryDirectory(dir=ROOT / "artifacts") as directory:
            output = Path(directory) / "deepseek-online-eval"
            result = await run_phase6_online_evaluation(
                project_root=ROOT,
                tasks_path=CORPUS,
                split_manifest_path=SPLITS,
                output_directory=output,
                provider="deepseek",
                model="deepseek-v4-flash",
                split="development",
                max_cases=1,
                confirm_online=True,
                environment={"DEEPSEEK_API_KEY": "deepseek-test-secret"},
                agent_runner=fake_runner,
            )
            self.assertIsNotNone(seen_provider)
            report = result["report"]
            self.assertEqual(report["provider"], "deepseek")
            self.assertEqual(report["transport"], "openai_compatible_responses")
            self.assertEqual(report["included"], 1)
            self.assertEqual(report["passed"], 1)
            self.assertEqual(report["usage"]["status"], "complete")
            self.assertEqual(report["cost"]["status"], "unavailable")
            self.assertIsNone(report["cost"]["total_usd"])
            self.assertEqual(report["pricing"]["status"], "not_provided")
            self.assertEqual(report["pricing"]["provider"], "deepseek")

            manifest = json.loads(
                (output / "phase6_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["provider"], "deepseek")
            self.assertEqual(
                manifest["transport"], "openai_compatible_responses"
            )
            self.assertTrue(manifest["audit"]["all_chains_valid"])
            connection = sqlite3.connect(output / "phase6_audit.sqlite3")
            try:
                provider_rows = connection.execute(
                    "SELECT provider, model, cost_usd FROM model_calls"
                ).fetchall()
                self.assertEqual(
                    provider_rows,
                    [("deepseek", "deepseek-v4-flash", None)],
                )
            finally:
                connection.close()
            artifact_bytes = b"".join(
                path.read_bytes() for path in output.iterdir() if path.is_file()
            )
            self.assertNotIn(b"deepseek-test-secret", artifact_bytes)

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
