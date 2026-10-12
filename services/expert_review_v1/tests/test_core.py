from __future__ import annotations

import hashlib
import inspect
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.tool_context import ToolContext

from researchops import eval_v2_provider_executor, self_pilot, self_pilot_web
from researchops.eval_v2_contracts import EvalV2ContractError
from researchops.eval_v2_dataset_prep import EvalV2LogicalDatasetRegistry
from researchops.eval_v2_inspect_backend import EvalV2InspectDatasetBackend
from researchops.eval_v2_public import EvalV2PublicTask
from researchops.eval_v2_runner import EvalV2ToolGateway
from researchops.model_providers import ProviderModel
from researchops_expert_review import core


REPO_ROOT = Path(__file__).resolve().parents[3]
DATASETS = ("palmer_penguins_v0_1_0", "uci_parkinsons_telemonitoring_189")
FAKE_KEY = "offline-credential-canary"
BILINGUAL = "## English\nThe authorized dataset contains 2 rows.\n## 中文\n授权数据集包含 2 行。"


class FakeProvider:
    provider_id = "deepseek"
    api_key_env = "DEEPSEEK_API_KEY"
    transport_id = "openai_compatible_responses"

    def __init__(self):
        self.opens = 0

    def validate_model(self, model_id):
        return model_id

    @asynccontextmanager
    async def open_model(self, *, model_id, api_key, timeout_seconds):
        if api_key != FAKE_KEY:
            raise AssertionError("Only the synthetic process credential is allowed")
        self.opens += 1
        self.timeout_seconds = timeout_seconds
        yield ProviderModel(
            provider_id=self.provider_id, model_id=model_id,
            transport_id=self.transport_id, sdk_model="fake-model",
        )


class OfflineRunner:
    """Private test capability; never exposed by the product CLI or web API."""

    def __init__(self, runner):
        self.runner = runner


def resolve_test_runner(value):
    if type(value) is OfflineRunner:
        return value.runner, True
    return value, False


class FakeRunner:
    def __init__(self, output=BILINGUAL, *, invoke_tool=False, usage=True):
        self.output = output
        self.invoke_tool = invoke_tool
        self.with_usage = usage
        self.calls = 0
        self.tool_results = []

    async def run(self, agent, prompt, *, context, max_turns, run_config):
        self.calls += 1
        self.agent, self.prompt, self.context = agent, prompt, context
        self.max_turns, self.run_config = max_turns, run_config
        if self.invoke_tool:
            tool = agent.tools[0]
            arguments = (
                {"bundle_id": context["bundle_id"], "release_name": context["release_name"]}
                if tool.name == "publish_aggregate_results"
                else {"dataset_id": context["dataset_id"]}
            )
            raw_arguments = json.dumps(arguments)
            wrapped = ToolContext(
                context, tool_name=tool.name,
                tool_call_id="offline-tool-call", tool_arguments=raw_arguments,
            )
            self.tool_results.append(json.loads(await tool.on_invoke_tool(wrapped, raw_arguments)))
        result = SimpleNamespace(
            final_output=self.output,
            # This synthetic body must never be copied to session records.
            raw_responses=[SimpleNamespace(body="complete-response-body-canary")],
        )
        if self.with_usage:
            result.context_wrapper = SimpleNamespace(
                usage=SimpleNamespace(requests=1, input_tokens=30, output_tokens=12)
            )
        return result


class ExpertReviewCoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry_path = self._write_registry()
        source = REPO_ROOT / "evals/v2/public_tasks.jsonl"
        self.tasks_bytes = source.read_bytes()
        copied = self.root / "evals/v2/public_tasks.jsonl"
        copied.parent.mkdir(parents=True)
        copied.write_bytes(self.tasks_bytes)
        self.tasks = {task["task_id"]: task for task in map(json.loads, self.tasks_bytes.decode().splitlines())}
        self.session = self.root / "artifacts/session"
        core.create_session(project_root=self.root, output_directory=self.session)
        self.provider = FakeProvider()
        provider_patch = patch.object(core.model_providers, "get_provider", return_value=self.provider)
        provider_patch.start()
        self.addCleanup(provider_patch.stop)

    def _write_registry(self):
        prepared = self.root / "artifacts/prepared"
        prepared.mkdir(parents=True)
        entries = []
        for dataset_id in DATASETS:
            data = prepared / (dataset_id + ".csv")
            data.write_text("value,group\n1,A\n2,B\n", encoding="utf-8")
            entries.append({
                "dataset_id": dataset_id, "relative_path": data.name,
                "prepared_sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
                "prepared_bytes": data.stat().st_size, "row_count": 2, "column_count": 2,
                "source_asset_sha256": "a" * 64, "preparation_version": "1.0",
                "privacy_class": "public_test_data", "model_access": "aggregate_tools_only",
                "domain": "offline_fixture", "repeated_subjects": "parkinsons" in dataset_id,
                "analysis_boundaries": ["test_only", "no_row_access"],
                "transformations": ["offline_fixture"],
            })
        registry = prepared / "logical_dataset_registry.json"
        registry.write_text(json.dumps({
            "schema_version": "1.0", "registry_id": "expert-review-offline-fixture",
            "dataset_manifest_sha256": "b" * 64, "entries": entries,
        }), encoding="utf-8")
        return registry

    def review(self, runner=None):
        return core.ExpertReview(
            project_root=self.root, session_directory=self.session,
            registry_path=self.registry_path, provider_id="deepseek", model_id="fake-model",
            sdk_runner=OfflineRunner(runner) if runner is not None else None,
        )

    def ask_offline(self, runner=None, **kwargs):
        runner = runner or FakeRunner()
        review = self.review(runner)
        with patch.object(eval_v2_provider_executor, "_resolve_eval_v2_sdk_runner", side_effect=resolve_test_runner), patch.object(
            core.os.environ, "get", side_effect=lambda name, default=None: FAKE_KEY if name == "DEEPSEEK_API_KEY" else default
        ):
            result = review.ask(confirm_online=True, **kwargs)
        return result, runner

    def session_text(self):
        return "\n".join(path.read_text(encoding="utf-8") for path in self.session.glob("*.json"))

    def test_unconfirmed_question_refuses_before_key_read_and_execution(self):
        review = self.review()
        with patch.object(core.os.environ, "get", side_effect=AssertionError("No key access")) as key, patch.object(core, "EvalV2ProviderExecutor") as executor:
            with self.assertRaises(core.ExpertReviewError) as caught:
                review.ask(confirm_online=False, dataset_id=DATASETS[0], question="Summarize the dataset.")
        self.assertEqual(caught.exception.code, "online_confirmation_required")
        key.assert_not_called()
        executor.assert_not_called()
        self.assertEqual(review.history(), [])

    def test_unknown_dataset_refuses_before_key_read_and_execution(self):
        review = self.review()
        with patch.object(core.os.environ, "get", side_effect=AssertionError("No key access")) as key, patch.object(core, "EvalV2ProviderExecutor") as executor:
            with self.assertRaises(core.ExpertReviewError) as caught:
                review.ask(confirm_online=True, dataset_id="unknown_dataset", question="Summarize the dataset.")
        self.assertEqual(caught.exception.code, "dataset_not_authorized")
        key.assert_not_called()
        executor.assert_not_called()

    def test_sensitive_input_is_rejected_locally_with_hash_and_type_only(self):
        cases = (
            ("identity_number", "Check 110101199001011234"),
            ("phone_number", "Contact 13800138000"),
            ("email", "Contact researcher@example.test"),
            ("file_path", r"Open C:\private\data.csv"),
            ("api_key", "Use sk-synthetic-private-token"),
            ("subject_id", "Describe SUBJ-1234ABCD"),
            ("subject_id", "编号SUBJ-ABC123"),
            ("api_key", "密钥sk-demo-secret"),
            ("file_path", "路径：/opt/private"),
            ("file_path", "data/private"),
        )
        review = self.review()
        with patch.object(core.os.environ, "get", side_effect=AssertionError("No key access")) as key, patch.object(core, "EvalV2ProviderExecutor") as executor:
            for kind, question in cases:
                with self.subTest(kind=kind):
                    result = review.ask(confirm_online=True, dataset_id=DATASETS[0], question=question)
                    self.assertEqual(result["status"], "拒绝")
                    self.assertIsNone(result["question"])
                    self.assertIn(kind, result["input_rejection_types"])
                    self.assertEqual(result["question_sha256"], core.question_sha256(question))
                    self.assertIn("不是完备检测", result["answers"]["chinese"])
                    self.assertNotIn(question, self.session_text())
        key.assert_not_called()
        executor.assert_not_called()

    def test_length_limit_rejects_before_key_read(self):
        with patch.object(core.os.environ, "get", side_effect=AssertionError("No key access")):
            record = self.review().ask(confirm_online=True, dataset_id=DATASETS[0], question="问" * 2001)
        self.assertEqual(record["input_rejection_types"], ["too_long"])
        self.assertIsNone(record["question"])
        self.assertEqual(core.input_rejection_types("问" * 2000), [])

    def test_duplicate_normalized_question_refuses_across_review_instances(self):
        first, runner = self.ask_offline(dataset_id=DATASETS[0], question="Summarize  the\n dataset.")
        self.assertEqual(first["status"], "完成")
        with patch.object(core.os.environ, "get", side_effect=AssertionError("No repeat key access")):
            with self.assertRaises(core.ExpertReviewError) as caught:
                self.review().ask(confirm_online=True, dataset_id=DATASETS[1], question="  Summarize the dataset. ")
        self.assertEqual(caught.exception.code, "question_already_run")
        self.assertEqual(runner.calls, 1)
        self.assertEqual(len(self.review().history()), 1)

    def test_example_049_preserves_context_and_exposes_only_publish_tool(self):
        record, runner = self.ask_offline(FakeRunner(invoke_tool=True), from_task="V2-DEV-049")
        task = self.tasks["V2-DEV-049"]
        self.assertEqual(runner.context, task["context"])
        self.assertEqual(record["question"], task["prompt"])
        self.assertEqual(record["dataset_id"], task["dataset_id"])
        self.assertEqual(record["source"], "V2-DEV-049")
        self.assertEqual([tool.name for tool in runner.agent.tools], ["publish_aggregate_results"])
        self.assertEqual(runner.tool_results[0]["status"], "awaiting_approval")
        self.assertFalse(runner.tool_results[0]["side_effect_occurred"])
        self.assertEqual(record["outcome"], "waiting_approval")
        self.assertEqual(record["tool_call_count"], 1)
        self.assertIn("未实际发布", record["notice"])

    def test_example_001_preserves_context_and_exposes_only_inspect_tool(self):
        record, runner = self.ask_offline(FakeRunner(invoke_tool=True), from_task="V2-DEV-001")
        task = self.tasks["V2-DEV-001"]
        self.assertEqual(runner.context, task["context"])
        self.assertEqual([tool.name for tool in runner.agent.tools], ["inspect_dataset"])
        self.assertEqual(record["question"], task["prompt"])
        self.assertEqual(record["tool_call_count"], 1)
        self.assertEqual(runner.tool_results[0], EvalV2InspectDatasetBackend.from_registry_path(self.registry_path).inspect_dataset(task["dataset_id"]))
        self.assertRegex(runner.prompt, r"^Task ID: ADHOC-[0-9a-f]{8}\n")
        self.assertTrue(runner.prompt.endswith(task["prompt"]))
        self.assertNotIn("expected", runner.prompt)

    def test_inherited_preclassification_clarifies_with_no_tools(self):
        record, runner = self.ask_offline(from_task="V2-DEV-002")
        self.assertEqual(record["status"], "先提问澄清")
        self.assertEqual(record["outcome"], "clarification_required")
        self.assertEqual(runner.agent.tools, [])
        self.assertEqual(record["tool_call_count"], 0)
        self.assertIn("[CLARIFICATION_REQUIRED]", record["answers"]["english"])

    def test_executor_uses_self_pilot_bilingual_limits_and_defaults(self):
        with patch.object(core, "EvalV2ProviderExecutor", wraps=eval_v2_provider_executor.EvalV2ProviderExecutor) as executor:
            record, runner = self.ask_offline(dataset_id=DATASETS[0], question="Summarize the dataset.")
        defaults = inspect.signature(self_pilot.run_self_pilot_task).parameters
        arguments = executor.call_args.kwargs
        self.assertTrue(arguments["bilingual_output"])
        self.assertEqual(arguments["max_output_tokens"], self_pilot_web.SELF_PILOT_WEB_MAX_OUTPUT_TOKENS)
        self.assertEqual(arguments["max_turns"], defaults["max_turns"].default)
        self.assertEqual(arguments["run_timeout_seconds"], defaults["run_timeout_seconds"].default)
        self.assertEqual(record["answers"], self_pilot_web.split_bilingual_output(BILINGUAL))
        self.assertTrue(runner.run_config.tracing_disabled)
        self.assertFalse(runner.run_config.trace_include_sensitive_data)

    def test_lightweight_gateway_matches_formal_task_authorization(self):
        backend = EvalV2InspectDatasetBackend.from_registry_path(self.registry_path)
        for task_id in ("V2-DEV-001", "V2-DEV-049"):
            task = EvalV2PublicTask.from_dict(self.tasks[task_id])
            lightweight = SimpleNamespace(task_id="ADHOC-1234abcd", context=dict(task.context))
            operations = [
                ("inspect_dataset", {"dataset_id": task.dataset_id}),
                ("inspect_dataset", {"dataset_id": next(value for value in DATASETS if value != task.dataset_id)}),
                ("publish_aggregate_results", {"bundle_id": "parkinsons_summary", "release_name": "parkinsons-aggregate-v2"}),
                ("publish_aggregate_results", {"bundle_id": "other_bundle", "release_name": "other_release"}),
            ]
            for name, arguments in operations:
                with self.subTest(task_id=task_id, tool=name, arguments=arguments):
                    outcomes = []
                    for scope in (task, lightweight):
                        gateway = EvalV2ToolGateway(scope, backend)
                        try:
                            value = ("result", gateway.call(name, arguments))
                        except EvalV2ContractError as exc:
                            value = ("error", exc.code)
                        outcomes.append((value, gateway.tool_calls, gateway.backend_executed_tool_call_count))
                    self.assertEqual(outcomes[0], outcomes[1])
                    if name == "inspect_dataset" and arguments["dataset_id"] != task.dataset_id:
                        self.assertEqual(outcomes[0][0], ("error", "eval_v2_tool_unauthorized"))

    def test_preview_is_exact_backend_result_without_paths_or_network(self):
        expected = EvalV2InspectDatasetBackend.from_registry_path(self.registry_path).inspect_dataset(DATASETS[0])
        with patch.object(core, "EvalV2ProviderExecutor", side_effect=AssertionError("No model")), patch.object(
            core.EvalV2InspectDatasetBackend, "inspect_dataset", autospec=True, return_value=expected
        ) as inspect_tool:
            output = core.preview(registry_path=self.registry_path, dataset_id=DATASETS[0])
        self.assertEqual(output, json.dumps(expected, ensure_ascii=False, indent=2))
        self.assertEqual(inspect_tool.call_args.args[1], DATASETS[0])
        self.assertNotIn(str(self.root), output)
        self.assertNotIn("relative_path", output)
        self.assertNotIn("value,group", output)

    def test_preview_local_head_is_separate_and_limited_to_five(self):
        output = core.preview(registry_path=self.registry_path, dataset_id=DATASETS[0], local_head=2)
        head = "本机文件前 2 行（只在本机显示，不会发送）\nvalue,group\n1,A"
        self.assertTrue(output.endswith(head))
        self.assertNotIn(str(self.registry_path), output)
        self.assertNotIn("2,B", output)
        for count in (-1, 6, True):
            with self.subTest(count=count), self.assertRaises(core.ExpertReviewError):
                core.preview(registry_path=self.registry_path, dataset_id=DATASETS[0], local_head=count)

    def test_output_filter_blocks_both_complete_language_sections(self):
        unsafe_outputs = (
            r"C:\private\output.csv", "/home/private/output.csv",
            "/opt/data.csv", "data/file.csv", "sk-output-canary",
            "SUBJ-1234567890ABCDEF", "SUBJ-ABC123", "SUBJ-001", FAKE_KEY,
        )
        for index, unsafe in enumerate(unsafe_outputs):
            with self.subTest(unsafe=unsafe):
                output = "## English\nSafe prefix " + unsafe + "\n## 中文\n不应保留的整个回答"
                record, _ = self.ask_offline(FakeRunner(output), dataset_id=DATASETS[0], question=f"Summarize aggregate count number {index}.")
                self.assertTrue(record["output_redacted"])
                self.assertEqual(record["answers"]["english"], "[OUTPUT_REDACTED_BY_SELF_PILOT_SAFETY_FILTER]")
                self.assertEqual(record["answers"]["english"], record["answers"]["chinese"])
                self.assertNotIn(unsafe, self.session_text())
                self.assertNotIn("不应保留的整个回答", self.session_text())

    def test_records_preserve_displayed_fields_but_not_credentials_paths_or_requests(self):
        record, runner = self.ask_offline(FakeRunner(invoke_tool=True), dataset_id=DATASETS[0], question="Summarize the dataset.")
        text = self.session_text()
        for excluded in (FAKE_KEY, str(self.root), str(self.registry_path), runner.prompt, "complete-response-body-canary", "raw_responses", "Authorized context:"):
            self.assertNotIn(excluded, text)
        self.assertEqual(record["token_usage"], {"input_tokens": 30, "output_tokens": 12})
        self.assertEqual(record["question_sha256"], hashlib.sha256(b"Summarize the dataset.").hexdigest())
        self.assertTrue(record["created_at_utc"].endswith("Z"))
        self.assertTrue(record["completed_at_utc"].endswith("Z"))
        self.assertIsInstance(record["elapsed_seconds"], float)
        self.assertFalse(record["automatic_scoring"])
        self.assertEqual(record["notice"], "无自动评分")
        saved = json.loads((self.session / (record["run_id"] + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(saved, record)
        index = json.loads((self.session / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["records"][0]["run_id"], record["run_id"])
        self.assertEqual(self.review().history(), [{key: record[key] for key in ("sequence", "dataset_id", "status", "run_id")}])

    def test_missing_token_usage_is_null(self):
        record, _ = self.ask_offline(FakeRunner(usage=False), dataset_id=DATASETS[0], question="Summarize the dataset.")
        self.assertEqual(record["status"], "完成")
        self.assertEqual(record["token_usage"], {"input_tokens": None, "output_tokens": None})

    def test_executor_returned_preclassification_is_recorded_when_available(self):
        result = SimpleNamespace(final_output=BILINGUAL, outcome="completed", input_tokens=None, output_tokens=None, preclassification="normal")
        with patch.object(core, "EvalV2ProviderExecutor") as executor, patch.object(core.os.environ, "get", return_value=FAKE_KEY):
            executor.return_value.execute.return_value = result
            record = self.review().ask(confirm_online=True, dataset_id=DATASETS[0], question="Summarize the dataset.")
        self.assertEqual(record["preclassification"], "normal")

    def test_executor_online_gate_is_preserved_and_never_opens_provider(self):
        with patch.object(core.os.environ, "get", return_value=FAKE_KEY):
            record = self.review().ask(confirm_online=True, dataset_id=DATASETS[0], question="Summarize the dataset.")
        self.assertEqual(record["error_code"], "eval_v2_completion_telemetry_session_required")
        self.assertEqual(record["status"], "出错")
        self.assertEqual(self.provider.opens, 0)
        self.assertNotIn(FAKE_KEY, self.session_text())

    def test_missing_key_attempt_is_reserved_and_cannot_be_repeated(self):
        review = self.review()
        with patch.object(core.os.environ, "get", return_value=None):
            record = review.ask(confirm_online=True, dataset_id=DATASETS[0], question="Summarize the dataset.")
        self.assertEqual(record["error_code"], "provider_key_missing")
        with self.assertRaises(core.ExpertReviewError) as caught:
            review.ask(confirm_online=True, dataset_id=DATASETS[0], question="Summarize the dataset.")
        self.assertEqual(caught.exception.code, "question_already_run")

    def test_session_must_be_new_and_below_artifacts(self):
        for directory in (self.root / "outside", self.root / "artifacts", self.root / "artifacts/../escape"):
            with self.subTest(directory=directory), self.assertRaises(core.ExpertReviewError) as caught:
                core.create_session(project_root=self.root, output_directory=directory)
            self.assertEqual(caught.exception.code, "session_path_not_allowed")
        before = (self.session / "index.json").read_bytes()
        with self.assertRaises(core.ExpertReviewError) as caught:
            core.create_session(project_root=self.root, output_directory=self.session)
        self.assertEqual(caught.exception.code, "session_exists")
        self.assertEqual((self.session / "index.json").read_bytes(), before)

    def test_catalog_and_examples_only_expose_registered_public_metadata(self):
        review = self.review()
        catalog = review.catalog()
        self.assertEqual({item["dataset_id"] for item in catalog}, set(DATASETS))
        self.assertNotIn(str(self.root), json.dumps(catalog))
        examples = review.examples(DATASETS[1])
        task049 = next(item for item in examples if item["task_id"] == "V2-DEV-049")
        self.assertEqual(task049, {key: self.tasks["V2-DEV-049"][key] for key in ("task_id", "prompt", "context")})
        self.assertNotIn("expected", json.dumps(examples))
        self.assertEqual((self.root / "evals/v2/public_tasks.jsonl").read_bytes(), self.tasks_bytes)


if __name__ == "__main__":
    unittest.main()
