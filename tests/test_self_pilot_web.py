from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import researchops.cli as cli_module
import researchops.self_pilot_web as self_pilot_web_module
from researchops.eval_v2_contracts import EvalV2ContractError
from researchops.model_providers import ProviderModel
from researchops.self_pilot import create_self_pilot_session
from researchops.self_pilot_web import (
    SelfPilotWebController,
    _build_handler,
    feedback_field_order,
    load_prompt_translations,
    split_bilingual_output,
    verify_provider_model_access,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = REPO_ROOT / "evals" / "v2" / "public_tasks.jsonl"
DATASETS_PATH = REPO_ROOT / "evals" / "v2" / "external_datasets.json"
TRANSLATIONS_PATH = (
    REPO_ROOT / "evals" / "v2" / "self_pilot_translations.zh-CN.json"
)
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
        if model_id != "deepseek-v4-flash":
            raise AssertionError("unexpected model")
        return model_id

    @asynccontextmanager
    async def open_model(self, *, model_id, api_key, timeout_seconds=120.0):
        del timeout_seconds
        if api_key != "WEB-TEST-KEY":
            raise AssertionError("unexpected key")
        yield ProviderModel(
            provider_id=self.provider_id,
            model_id=model_id,
            transport_id=self.transport_id,
            sdk_model="fake-model",
        )


class FakeAnthropicProvider:
    provider_id = "anthropic"
    api_key_env = "ANTHROPIC_API_KEY"
    transport_id = "litellm_anthropic_chat_completions"

    def validate_model(self, model_id: str) -> str:
        return model_id


class FakeRunner:
    def __init__(self, output: str | None = None) -> None:
        self.calls = 0
        self.agent = None
        self.output = output or (
            "## English\nThe controlled profile contains 344 rows.\n"
            "## 中文\n受控数据概况包含 344 行。"
        )

    async def run(self, agent, prompt, *, context, max_turns, run_config):
        del prompt, context, max_turns, run_config
        self.calls += 1
        self.agent = agent
        usage = SimpleNamespace(requests=1, input_tokens=25, output_tokens=20)
        return SimpleNamespace(
            final_output=self.output,
            context_wrapper=SimpleNamespace(usage=usage),
        )


class FakeClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeAvailabilityChecker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, float]] = []

    def __call__(self, provider, model_id, api_key, timeout_seconds):
        self.calls.append(
            (provider.provider_id, model_id, api_key, timeout_seconds)
        )
        return {
            "status": "verified",
            "provider_id": provider.provider_id,
            "model_id": model_id,
            "verification_method": "test_models_list",
            "network_calls": 1,
            "model_token_calls": 0,
        }


class FakeModelsClient:
    model_ids = ["deepseek-v4-flash"]
    instances: list["FakeModelsClient"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.models = self
        self.__class__.instances.append(self)

    async def list(self):
        return SimpleNamespace(
            data=[SimpleNamespace(id=model_id) for model_id in self.model_ids]
        )

    async def close(self) -> None:
        self.closed = True


class SelfPilotWebTests(unittest.TestCase):
    def test_anthropic_models_list_helper_denies_before_client_construction(self) -> None:
        with patch(
            "openai.AsyncOpenAI",
            side_effect=AssertionError("OpenAI client must not receive Anthropic Key"),
        ):
            with self.assertRaises(EvalV2ContractError) as caught:
                verify_provider_model_access(
                    FakeAnthropicProvider(),
                    "claude-sonnet-5",
                    "must-not-be-forwarded",
                    5.0,
                )

        self.assertEqual(
            caught.exception.code, "anthropic_generic_online_entrypoint_disabled"
        )

    def test_anthropic_web_configuration_denies_before_key_normalization(self) -> None:
        session = self._create_session()
        controller = self._controller(session, preconfigured=False)
        with patch.object(
            self_pilot_web_module,
            "_normalize_api_key",
            side_effect=AssertionError("Key must not be normalized"),
        ):
            with self.assertRaises(EvalV2ContractError) as caught:
                controller.configure(
                    {
                        "provider_id": "anthropic",
                        "model_id": "claude-sonnet-5",
                        "api_key": "must-not-be-read",
                    }
                )

        self.assertEqual(
            caught.exception.code, "anthropic_generic_online_entrypoint_disabled"
        )

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
            entries.append(
                {
                    "dataset_id": dataset_id,
                    "relative_path": path.name,
                    "prepared_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
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
            "registry_id": "self-pilot-web-test-registry",
            "dataset_manifest_sha256": "b" * 64,
            "entries": entries,
        }
        registry_path = registry_root / "logical_dataset_registry.json"
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        return registry_path

    def _create_session(self, task_count: int = 1) -> Path:
        session = self.root / "artifacts" / "web-session"
        create_self_pilot_session(
            project_root=self.root,
            output_directory=session,
            tasks_path=TASKS_PATH,
            dataset_manifest_path=DATASETS_PATH,
            task_count=task_count,
        )
        return session

    def _controller(
        self,
        session: Path,
        *,
        translations_path: Path = TRANSLATIONS_PATH,
        clock: FakeClock | None = None,
        runner: FakeRunner | None = None,
        preconfigured: bool = True,
        availability_checker: FakeAvailabilityChecker | None = None,
    ) -> SelfPilotWebController:
        arguments = {
            "project_root": self.root,
            "session_directory": session,
            "tasks_path": TASKS_PATH,
            "dataset_manifest_path": DATASETS_PATH,
            "registry_path": self.registry_path,
            "translations_path": translations_path,
            "confirm_online": True,
            "sdk_runner": runner or FakeRunner(),
            "clock": clock or FakeClock(10.0),
            "availability_checker": availability_checker
            or FakeAvailabilityChecker(),
        }
        if preconfigured:
            arguments.update(
                {
                    "provider": FakeProvider(),
                    "model_id": "deepseek-v4-flash",
                    "api_key": "WEB-TEST-KEY",
                }
            )
        return SelfPilotWebController(
            **arguments
        )

    def _usability_feedback(
        self,
        *,
        task_id: str = "V2-DEV-001",
        clarification_useful=None,
        notes: str = "Clear enough for a non-expert usability review.",
    ) -> dict:
        return {
            "task_id": task_id,
            "understandable": True,
            "useful": True,
            "confidence": "medium",
            "needs_expert_review": True,
            "obvious_problem": False,
            "missing_information": False,
            "safety_concern": False,
            "clarification_useful": clarification_useful,
            "notes": notes,
        }

    def test_default_translation_sidecar_covers_default_twelve_tasks(self) -> None:
        session = self._create_session(task_count=12)
        controller = self._controller(session)

        state = controller.state()

        self.assertEqual(state["status"], "pending_provider_run")
        self.assertEqual(state["progress"]["task_count"], 12)
        self.assertIn("Palmer Penguins", state["task"]["prompt_en"])
        self.assertIn("Palmer Penguins", state["task"]["prompt_zh"])
        self.assertNotIn("WEB-TEST-KEY", repr(controller))

    def test_translation_must_match_exact_source_prompt(self) -> None:
        session = self._create_session()
        payload = {
            "schema_version": "1.0",
            "locale": "zh-CN",
            "translations": {
                "V2-DEV-001": {
                    "source_prompt": "stale prompt",
                    "prompt_zh": "过期翻译",
                }
            },
        }
        path = self.root / "stale-translations.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(EvalV2ContractError) as caught:
            self._controller(session, translations_path=path)

        self.assertEqual(caught.exception.code, "self_pilot_translation_missing")

    def test_click_run_starts_server_timer_and_feedback_stops_it(self) -> None:
        session = self._create_session()
        clock = FakeClock(100.0)
        runner = FakeRunner()
        controller = self._controller(session, clock=clock, runner=runner)

        answer_state = controller.run_current_task()

        self.assertEqual(answer_state["status"], "pending_human_feedback")
        self.assertIn("Palmer Penguins", answer_state["task"]["prompt_en"])
        self.assertEqual(
            answer_state["task"]["prompt_en"],
            answer_state["answer"]["task"]["prompt_en"],
        )
        self.assertTrue(answer_state["timer_running"])
        self.assertTrue(answer_state["agent_output_available"])
        self.assertEqual(answer_state["answer"]["english"], "The controlled profile contains 344 rows.")
        self.assertEqual(answer_state["answer"]["chinese"], "受控数据概况包含 344 行。")
        self.assertTrue(answer_state["answer"]["bilingual_complete"])
        self.assertEqual(
            answer_state["answer"]["feedback_fields"],
            [
                "understandable",
                "useful",
                "confidence",
                "needs_expert_review",
                "obvious_problem",
                "missing_information",
                "safety_concern",
                "notes",
            ],
        )
        self.assertNotIn("machine_pass", answer_state["answer"])
        self.assertEqual(runner.calls, 1)
        self.assertEqual(runner.agent.model_settings.max_tokens, 10000)

        clock.value = 148.5
        recorded = controller.record_feedback(
            self._usability_feedback(notes="Bilingual answer was clear.")
        )

        self.assertEqual(recorded["duration_seconds"], 48.5)
        self.assertEqual(recorded["next"]["status"], "complete")
        state_text = (session / "pilot_state.json").read_text(encoding="utf-8")
        self.assertIn('"duration_seconds": 48.5', state_text)
        self.assertNotIn("controlled profile", state_text)
        self.assertNotIn("受控数据概况", state_text)
        self.assertNotIn("WEB-TEST-KEY", state_text)
        self.assertIn('"feedback_schema": "non_expert_usability_v2"', state_text)
        self.assertNotIn('"accepted"', state_text)
        summary = controller.summary()
        self.assertEqual(summary["non_expert_usability_feedback_count"], 1)
        self.assertEqual(summary["understandable_rate"], 1.0)
        self.assertEqual(summary["useful_rate"], 1.0)
        self.assertEqual(summary["needs_expert_review_rate"], 1.0)
        self.assertEqual(summary["missing_information_rate"], 0.0)
        self.assertIsNone(summary["human_acceptance_rate"])

    def test_missing_confirmation_and_key_fail_before_server_use(self) -> None:
        session = self._create_session()
        common = {
            "project_root": self.root,
            "session_directory": session,
            "tasks_path": TASKS_PATH,
            "dataset_manifest_path": DATASETS_PATH,
            "registry_path": self.registry_path,
            "translations_path": TRANSLATIONS_PATH,
            "provider": FakeProvider(),
            "model_id": "deepseek-v4-flash",
        }
        with self.assertRaises(EvalV2ContractError) as confirmation:
            SelfPilotWebController(
                **common, api_key="WEB-TEST-KEY", confirm_online=False
            )
        self.assertEqual(
            confirmation.exception.code, "eval_v2_online_confirmation_required"
        )

        with self.assertRaises(EvalV2ContractError) as key:
            SelfPilotWebController(**common, api_key=" ", confirm_online=True)
        self.assertEqual(key.exception.code, "self_pilot_web_key_invalid")

        unconfigured = SelfPilotWebController(
            project_root=self.root,
            session_directory=session,
            tasks_path=TASKS_PATH,
            dataset_manifest_path=DATASETS_PATH,
            registry_path=self.registry_path,
            translations_path=TRANSLATIONS_PATH,
            confirm_online=True,
            availability_checker=FakeAvailabilityChecker(),
        )
        self.assertEqual(unconfigured.state()["status"], "configuration_required")

    def test_initial_configuration_requires_available_provider_model_and_key(self) -> None:
        session = self._create_session()
        checker = FakeAvailabilityChecker()
        controller = self._controller(
            session, preconfigured=False, availability_checker=checker
        )

        initial = controller.state()
        self.assertEqual(initial["status"], "configuration_required")
        self.assertEqual(
            {item["provider_id"] for item in initial["provider_catalog"]},
            {"openai", "deepseek"},
        )
        with self.assertRaises(EvalV2ContractError) as not_ready:
            controller.run_current_task()
        self.assertEqual(
            not_ready.exception.code, "self_pilot_web_configuration_required"
        )

        configured = controller.configure(
            {
                "provider_id": "deepseek",
                "model_id": "deepseek-v4-flash",
                "api_key": "BROWSER-ONLY-KEY",
            }
        )

        self.assertEqual(configured["status"], "configured")
        self.assertEqual(configured["next"]["status"], "pending_provider_run")
        self.assertEqual(configured["verification"]["model_token_calls"], 0)
        self.assertEqual(checker.calls[0][0:2], ("deepseek", "deepseek-v4-flash"))
        serialized = json.dumps(configured, ensure_ascii=False)
        self.assertNotIn("BROWSER-ONLY-KEY", serialized)
        self.assertNotIn("BROWSER-ONLY-KEY", repr(controller))
        self.assertNotIn(
            "BROWSER-ONLY-KEY",
            (session / "pilot_state.json").read_text(encoding="utf-8"),
        )

    def test_models_list_preflight_verifies_model_without_token_call(self) -> None:
        FakeModelsClient.instances.clear()
        with patch("openai.AsyncOpenAI", FakeModelsClient):
            result = verify_provider_model_access(
                FakeProvider(), "deepseek-v4-flash", "MODEL-LIST-KEY", 5.0
            )

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["model_token_calls"], 0)
        client = FakeModelsClient.instances[-1]
        self.assertEqual(client.kwargs["base_url"], "https://api.deepseek.com")
        self.assertEqual(client.kwargs["max_retries"], 0)
        self.assertTrue(client.closed)

    def test_models_list_preflight_rejects_model_not_visible_to_key(self) -> None:
        FakeModelsClient.instances.clear()
        original_ids = FakeModelsClient.model_ids
        FakeModelsClient.model_ids = ["deepseek-v4-pro"]
        self.addCleanup(setattr, FakeModelsClient, "model_ids", original_ids)

        with patch("openai.AsyncOpenAI", FakeModelsClient):
            with self.assertRaises(EvalV2ContractError) as caught:
                verify_provider_model_access(
                    FakeProvider(), "deepseek-v4-flash", "HIDDEN-MODEL-KEY", 5.0
                )

        self.assertEqual(caught.exception.code, "self_pilot_web_model_unavailable")
        self.assertNotIn("HIDDEN-MODEL-KEY", str(caught.exception))
        self.assertTrue(FakeModelsClient.instances[-1].closed)

    def test_models_list_auth_failure_is_stable_and_key_free(self) -> None:
        class AuthenticationFailure(RuntimeError):
            status_code = 401

        class AuthFailClient(FakeModelsClient):
            async def list(self):
                raise AuthenticationFailure(
                    "Authorization: Bearer AUTH-FAIL-SECRET"
                )

        AuthFailClient.instances.clear()
        with patch("openai.AsyncOpenAI", AuthFailClient):
            with self.assertRaises(EvalV2ContractError) as caught:
                verify_provider_model_access(
                    FakeProvider(), "deepseek-v4-flash", "AUTH-FAIL-SECRET", 5.0
                )

        self.assertEqual(
            caught.exception.code, "self_pilot_web_provider_auth_failed"
        )
        self.assertNotIn("AUTH-FAIL-SECRET", str(caught.exception))
        self.assertTrue(AuthFailClient.instances[-1].closed)

    def test_existing_session_run_locks_initial_provider_and_model_choices(self) -> None:
        session = self._create_session(task_count=2)
        clock = FakeClock(10.0)
        first_controller = self._controller(session, clock=clock)
        first_controller.run_current_task()
        clock.value = 20.0
        first_controller.record_feedback(
            self._usability_feedback(notes="First task complete.")
        )

        restarted = self._controller(session, preconfigured=False)
        state = restarted.state()

        self.assertEqual(state["status"], "configuration_required")
        self.assertEqual(len(state["provider_catalog"]), 1)
        self.assertEqual(state["provider_catalog"][0]["provider_id"], "deepseek")
        self.assertEqual(
            state["provider_catalog"][0]["models"],
            [{"model_id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash"}],
        )
        self.assertTrue(
            state["provider_catalog"][0]["locked_to_existing_session"]
        )

    def test_bilingual_split_preserves_control_prefix(self) -> None:
        value = split_bilingual_output(
            "[REFUSED]\n## English\nNo raw rows.\n## 中文\n不能返回原始行。"
        )
        self.assertTrue(value["bilingual_complete"])
        self.assertTrue(value["english"].startswith("[REFUSED]"))
        self.assertTrue(value["chinese"].startswith("[REFUSED]"))

    def test_clarification_field_depends_on_observed_outcome_and_requires_choice(self) -> None:
        session = self._create_session()
        clock = FakeClock(20.0)
        runner = FakeRunner(
            "[CLARIFICATION_REQUIRED]\n## English\nPlease clarify the design.\n"
            "## 中文\n请澄清研究设计。"
        )
        controller = self._controller(session, clock=clock, runner=runner)
        state = controller.run_current_task()

        self.assertEqual(state["answer"]["outcome"], "clarification_required")
        self.assertEqual(
            tuple(state["answer"]["feedback_fields"]),
            feedback_field_order("clarification_required"),
        )
        self.assertEqual(
            feedback_field_order("clarification_required"),
            (
                "understandable",
                "useful",
                "confidence",
                "needs_expert_review",
                "obvious_problem",
                "missing_information",
                "safety_concern",
                "clarification_useful",
                "notes",
            ),
        )
        payload = self._usability_feedback(
            notes="The clarification was specific."
        )
        with self.assertRaises(EvalV2ContractError) as missing:
            controller.record_feedback(payload)
        self.assertEqual(missing.exception.code, "self_pilot_web_feedback_invalid")

        payload["clarification_useful"] = True
        clock.value = 35.0
        recorded = controller.record_feedback(payload)
        self.assertEqual(recorded["duration_seconds"], 15.0)

    def test_cli_exposes_local_web_command(self) -> None:
        args = cli_module.build_parser().parse_args(
            [
                "self-pilot-web",
                "--session-dir",
                "artifacts/self_pilot/session-01",
                "--registry",
                "artifacts/self_pilot_data/run-01/logical_dataset_registry.json",
                "--confirm-online",
            ]
        )
        self.assertEqual(args.command, "self-pilot-web")
        self.assertEqual(args.port, 8765)
        self.assertTrue(args.confirm_online)
        self.assertFalse(hasattr(args, "provider"))

    def test_translation_file_loader_rejects_unknown_top_level_field(self) -> None:
        payload = json.loads(TRANSLATIONS_PATH.read_text(encoding="utf-8"))
        payload["unexpected"] = True
        path = self.root / "invalid-translations.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(EvalV2ContractError) as caught:
            load_prompt_translations(path)

        self.assertEqual(caught.exception.code, "self_pilot_translation_invalid")

    def test_http_page_is_local_no_store_and_post_requires_process_token(self) -> None:
        session = self._create_session()
        runner = FakeRunner()
        checker = FakeAvailabilityChecker()
        controller = self._controller(
            session,
            runner=runner,
            preconfigured=False,
            availability_checker=checker,
        )
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _build_handler(controller, "PROCESS-TOKEN", 0)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        self.addCleanup(connection.close)

        connection.request("GET", "/")
        page = connection.getresponse()
        page_body = page.read().decode("utf-8")
        self.assertEqual(page.status, 200)
        self.assertEqual(page.getheader("Cache-Control"), "no-store")
        self.assertIn("双语人工评测台", page_body)
        self.assertIn("Content-Security-Policy", dict(page.getheaders()))

        connection.request("GET", "/app.js")
        javascript_response = connection.getresponse()
        javascript = javascript_response.read().decode("utf-8")
        self.assertEqual(javascript_response.status, 200)
        ordered_build_calls = [
            "yesNoField('understandable'",
            "yesNoField('useful'",
            "choiceField('confidence'",
            "yesNoField('needs_expert_review'",
            "yesNoField('obvious_problem'",
            "yesNoField('missing_information'",
            "yesNoField('safety_concern'",
            "yesNoField('clarification_useful'",
            "grid.append(notesField())",
        ]
        positions = [javascript.index(value) for value in ordered_build_calls]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("feedbackFields.includes('clarification_useful')", javascript)
        self.assertNotIn('value="na"', javascript)
        render_answer = javascript[javascript.index("function renderAnswer") :]
        self.assertLess(
            render_answer.index("workspace.append(renderPrompt(state.task))"),
            render_answer.index("languageCard('Agent output · English'"),
        )
        self.assertIn("你不需要判断医学、统计或科研结论是否专业正确", javascript)
        self.assertIn("安全拒绝和等待人工审批本身不算错误", javascript)
        self.assertNotIn("manual_revisions", javascript)
        self.assertIn("function renderSafeMarkdown", javascript)
        self.assertIn(
            "languageCard('Agent output · English', 'answer', answer.english, true)",
            javascript,
        )
        self.assertIn("document.createElement('table')", javascript)
        self.assertIn("document.createElement('pre')", javascript)
        self.assertIn("['http:', 'https:'].includes(parsed.protocol)", javascript)
        self.assertIn("node.rel = 'noopener noreferrer'", javascript)
        self.assertNotIn("innerHTML = answer.english", javascript)
        self.assertNotIn("innerHTML = answer.chinese", javascript)

        connection.request("GET", "/api/state")
        configuration_response = connection.getresponse()
        configuration = json.loads(configuration_response.read().decode("utf-8"))
        self.assertEqual(configuration["status"], "configuration_required")

        connection.request(
            "POST",
            "/api/run",
            body="{}",
            headers={"Content-Type": "application/json"},
        )
        denied = connection.getresponse()
        denied_payload = json.loads(denied.read().decode("utf-8"))
        self.assertEqual(denied.status, 400)
        self.assertEqual(denied_payload["error_code"], "self_pilot_web_csrf_denied")
        self.assertEqual(runner.calls, 0)

        configuration_body = json.dumps(
            {
                "provider_id": "deepseek",
                "model_id": "deepseek-v4-flash",
                "api_key": "HTTP-ONLY-KEY",
            }
        )
        connection.request(
            "POST",
            "/api/configure",
            body=configuration_body,
            headers={
                "Content-Type": "application/json",
                "X-ResearchOps-Token": "PROCESS-TOKEN",
            },
        )
        configured_response = connection.getresponse()
        configured_body = configured_response.read().decode("utf-8")
        self.assertEqual(configured_response.status, 200)
        self.assertNotIn("HTTP-ONLY-KEY", configured_body)
        self.assertEqual(checker.calls[-1][0:2], ("deepseek", "deepseek-v4-flash"))


if __name__ == "__main__":
    unittest.main()
