from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops.model_providers import (
    SUPPORTED_PROVIDER_IDS,
    AnthropicProvider,
    DeepSeekProvider,
    OpenAIProvider,
    ProviderConfigurationError,
    ProviderModel,
    get_provider,
    provider_transport_status,
)
from researchops import model_providers as provider_module


ROOT = Path(__file__).resolve().parents[1]


def _anthropic_test_authorization():
    return object()


class _FakeAsyncOpenAI:
    instances: list["_FakeAsyncOpenAI"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.__class__.instances.append(self)

    async def close(self) -> None:
        self.closed = True


class _FakeResponsesModel:
    def __init__(self, *, model: str, openai_client: _FakeAsyncOpenAI) -> None:
        self.model = model
        self.openai_client = openai_client


class _FakeLitellmModel:
    instances: list["_FakeLitellmModel"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.model = kwargs["model"]
        self.base_url = kwargs.get("base_url")
        self.api_key = kwargs.get("api_key")
        self.fetch_settings = None
        self.__class__.instances.append(self)

    async def _fetch_response(self, **kwargs):
        self.fetch_settings = kwargs["model_settings"]
        return "controlled-response"


class _FakeAsyncHTTPHandler:
    instances: list["_FakeAsyncHTTPHandler"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.__class__.instances.append(self)

    async def close(self) -> None:
        self.closed = True


class _FakeLitellmModule:
    fallbacks = None
    context_window_fallbacks = None
    content_policy_fallbacks = None
    callbacks = []
    success_callback = []
    failure_callback = []
    input_callback = []
    _async_input_callback = []
    _async_success_callback = []
    _async_failure_callback = []
    service_callback = []
    audit_log_callbacks = []
    callback_settings = {}
    cache = None
    num_retries = None
    retry_policy = None
    context_window_fallback_dict = None
    model_alias_map = {}
    num_retries_per_request = None
    custom_prompt_dict = {}
    additional_drop_params = None
    api_base = None
    api_key = None
    organization = None
    project = None
    drop_params = False
    modify_params = False
    error_logs = {}
    turn_off_message_logging = False
    suppress_debug_info = False


class Phase6ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeAsyncOpenAI.instances.clear()
        _FakeLitellmModel.instances.clear()
        _FakeAsyncHTTPHandler.instances.clear()
        authorization = patch(
            "researchops.model_providers._anthropic_online_transport_authorized",
            return_value=True,
        )
        authorization.start()
        self.addCleanup(authorization.stop)

    def test_get_provider_resolves_only_the_three_supported_providers(self) -> None:
        self.assertEqual(SUPPORTED_PROVIDER_IDS, ("openai", "deepseek", "anthropic"))
        self.assertIsInstance(get_provider("openai"), OpenAIProvider)
        self.assertIsInstance(get_provider(" DeepSeek "), DeepSeekProvider)
        self.assertIsInstance(get_provider(" ANTHROPIC "), AnthropicProvider)
        with self.assertRaises(ProviderConfigurationError) as caught:
            get_provider("untrusted-provider")
        self.assertEqual(caught.exception.code, "provider_invalid")

    def test_litellm_first_import_uses_local_cost_map_without_pre_attempt_network(self) -> None:
        probe = (
            "import os, sys\n"
            "events = []\n"
            "def audit(event, args):\n"
            "    if event == 'socket.connect':\n"
            "        events.append(event)\n"
            "        raise RuntimeError('network forbidden')\n"
            "sys.addaudithook(audit)\n"
            "os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'caller-sentinel'\n"
            "from researchops import model_providers as providers\n"
            "providers._load_litellm_transport()\n"
            "assert events == [], events\n"
            "assert os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] == 'caller-sentinel'\n"
            "print('local-cost-map-import-ok')\n"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(ROOT),
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("local-cost-map-import-ok", completed.stdout)

    def test_openai_model_id_uses_safe_format(self) -> None:
        provider = OpenAIProvider()
        self.assertEqual(provider.validate_model(" gpt-5.4-mini "), "gpt-5.4-mini")
        for value in ("", "../gpt-5", "gpt 5", "gpt/5", "x" * 129):
            with self.subTest(value=value):
                with self.assertRaises(ProviderConfigurationError) as caught:
                    provider.validate_model(value)
                self.assertEqual(caught.exception.code, "provider_model_invalid")

    def test_deepseek_model_id_is_exact_allowlist(self) -> None:
        provider = DeepSeekProvider()
        for model_id in ("deepseek-v4-flash", "deepseek-v4-pro"):
            self.assertEqual(provider.validate_model(model_id), model_id)
        for value in ("deepseek-chat", "deepseek-reasoner", "deepseek-v4", ""):
            with self.subTest(value=value):
                with self.assertRaises(ProviderConfigurationError) as caught:
                    provider.validate_model(value)
                self.assertEqual(caught.exception.code, "provider_model_not_allowed")

    def test_anthropic_model_id_is_exact_official_allowlist(self) -> None:
        provider = AnthropicProvider()
        for model_id in (
            "claude-sonnet-5",
            "claude-opus-4-8",
            "claude-haiku-4-5-20251001",
        ):
            self.assertEqual(provider.validate_model(model_id), model_id)
        for value in (
            "claude-sonnet-5-latest",
            "claude-sonnet-4-6",
            "anthropic/claude-sonnet-5",
            "../claude-sonnet-5",
            "",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ProviderConfigurationError) as caught:
                    provider.validate_model(value)
                self.assertEqual(caught.exception.code, "provider_model_not_allowed")

    def test_anthropic_public_transport_is_hard_disabled_without_test_patch(self) -> None:
        provider = AnthropicProvider()
        self.assertFalse(
            hasattr(provider_module, "_anthropic_offline_test_authorization")
        )
        self.assertFalse(hasattr(provider_module, "_AnthropicRunAuthorization"))
        self.assertFalse(
            hasattr(provider_module, "_ANTHROPIC_OFFLINE_TEST_AUTHORIZATION_TOKEN")
        )

        async def denied(authorization: object | None) -> None:
            async with provider.open_model(
                model_id="claude-sonnet-5",
                api_key="must-not-be-used",
                _authorization=authorization,
            ):
                self.fail("context body must not run")

        with patch(
            "researchops.model_providers._anthropic_online_transport_authorized",
            return_value=False,
        ), patch(
            "researchops.model_providers._load_litellm_transport"
        ) as load_transport:
            for authorization in (None, object()):
                with self.subTest(authorization=authorization):
                    with self.assertRaises(ProviderConfigurationError) as caught:
                        asyncio.run(denied(authorization))
                    self.assertEqual(
                        caught.exception.code,
                        "anthropic_generic_online_entrypoint_disabled",
                    )
        load_transport.assert_not_called()

    def test_openai_pins_official_base_url_and_closes(self) -> None:
        provider = OpenAIProvider()

        async def exercise() -> ProviderModel:
            async with provider.open_model(
                model_id="gpt-5.4-mini", api_key=" test-openai-key "
            ) as bound:
                self.assertFalse(_FakeAsyncOpenAI.instances[-1].closed)
                return bound

        with patch.dict(
            os.environ,
            {"OPENAI_BASE_URL": "https://example.invalid/custom/v1"},
        ), patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "agents.OpenAIResponsesModel", _FakeResponsesModel
        ):
            bound = asyncio.run(exercise())

        client = _FakeAsyncOpenAI.instances[-1]
        self.assertEqual(client.kwargs["api_key"], "test-openai-key")
        self.assertEqual(client.kwargs["max_retries"], 0)
        self.assertEqual(client.kwargs["timeout"], 120.0)
        self.assertEqual(client.kwargs["base_url"], "https://api.openai.com/v1")
        self.assertNotIn("organization", client.kwargs)
        self.assertNotIn("project", client.kwargs)
        self.assertTrue(client.closed)
        self.assertEqual(bound.provider_id, "openai")
        self.assertEqual(bound.model_id, "gpt-5.4-mini")
        self.assertEqual(bound.transport_id, "openai_responses")
        self.assertEqual(bound.api_surface, "responses")
        self.assertEqual(bound.adapter_version, "openai-responses-adapter/1.0")
        self.assertIsNone(bound.completion_telemetry_session)
        self.assertIsInstance(bound.sdk_model, _FakeResponsesModel)
        self.assertNotIn("_FakeResponsesModel", repr(bound))

    def test_openai_real_sdk_ignores_base_url_environment_override(self) -> None:
        provider = OpenAIProvider()
        observed: dict[str, object] = {}

        async def exercise() -> object:
            async with provider.open_model(
                model_id="gpt-5.4-mini",
                api_key="SAFE-OPENAI-TEST-KEY",
            ) as bound:
                client = bound.sdk_model.openai_client
                observed["base_url"] = str(client.base_url)
                return client

        with patch.dict(
            os.environ,
            {"OPENAI_BASE_URL": "https://example.invalid/custom/v1"},
        ), patch("agents.OpenAIResponsesModel", _FakeResponsesModel):
            client = asyncio.run(exercise())

        self.assertEqual(observed["base_url"], "https://api.openai.com/v1/")
        self.assertTrue(client.is_closed())

    def test_deepseek_uses_fixed_base_url_and_closes_after_exception(self) -> None:
        provider = DeepSeekProvider()

        async def exercise() -> None:
            async with provider.open_model(
                model_id="deepseek-v4-flash", api_key="test-deepseek-key"
            ) as bound:
                self.assertEqual(bound.provider_id, "deepseek")
                self.assertEqual(bound.transport_id, "openai_compatible_responses")
                self.assertEqual(bound.api_surface, "responses")
                self.assertEqual(
                    bound.adapter_version, "deepseek-responses-adapter/1.0"
                )
                self.assertIsNone(bound.completion_telemetry_session)
                raise RuntimeError("controlled test failure")

        with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "agents.OpenAIResponsesModel", _FakeResponsesModel
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(exercise())

        client = _FakeAsyncOpenAI.instances[-1]
        self.assertEqual(client.kwargs["base_url"], "https://api.deepseek.com")
        self.assertEqual(client.kwargs["max_retries"], 0)
        self.assertEqual(client.kwargs["timeout"], 120.0)
        self.assertEqual(client.kwargs["organization"], "")
        self.assertEqual(client.kwargs["project"], "")
        self.assertEqual(client.kwargs["admin_api_key"], "")
        self.assertEqual(client.kwargs["webhook_secret"], "")
        self.assertEqual(client.kwargs["default_headers"], {})
        self.assertIsNone(client.organization)
        self.assertIsNone(client.project)
        self.assertIsNone(client.admin_api_key)
        self.assertIsNone(client.webhook_secret)
        self.assertEqual(client._custom_headers, {})
        self.assertTrue(client.closed)

    def test_deepseek_real_sdk_drops_openai_business_environment_before_send(
        self,
    ) -> None:
        provider = DeepSeekProvider()
        observed: dict[str, object] = {}

        async def exercise() -> object:
            async with provider.open_model(
                model_id="deepseek-v4-flash",
                api_key="SAFE-DEEPSEEK-TEST-KEY",
            ) as bound:
                client = bound.sdk_model.openai_client
                observed.update(
                    {
                        "base_url": str(client.base_url),
                        "api_key_from_environment": (
                            client.api_key == "OPENAI-API-ENV-CANARY"
                        ),
                        "organization": client.organization,
                        "project": client.project,
                        "admin_api_key": client.admin_api_key,
                        "webhook_secret": client.webhook_secret,
                        "custom_headers": dict(client._custom_headers),
                        "environment_header_names": sorted(
                            name
                            for name, value in client.default_headers.items()
                            if isinstance(value, str) and "ENV-CANARY" in value
                        ),
                    }
                )
                return client

        environment = {
            "OPENAI_BASE_URL": "https://example.invalid/custom/v1",
            "OPENAI_API_KEY": "OPENAI-API-ENV-CANARY",
            "OPENAI_ORG_ID": "ORG-ENV-CANARY",
            "OPENAI_PROJECT_ID": "PROJECT-ENV-CANARY",
            "OPENAI_ADMIN_KEY": "ADMIN-ENV-CANARY",
            "OPENAI_WEBHOOK_SECRET": "WEBHOOK-ENV-CANARY",
            "OPENAI_CUSTOM_HEADERS": (
                "X-Environment-Canary: CUSTOM-ENV-CANARY\n"
                "Authorization: Bearer AUTH-ENV-CANARY"
            ),
        }
        with patch.dict(os.environ, environment), patch(
            "agents.OpenAIResponsesModel", _FakeResponsesModel
        ):
            client = asyncio.run(exercise())

        self.assertEqual(observed["base_url"], "https://api.deepseek.com")
        self.assertFalse(observed["api_key_from_environment"])
        self.assertIsNone(observed["organization"])
        self.assertIsNone(observed["project"])
        self.assertIsNone(observed["admin_api_key"])
        self.assertIsNone(observed["webhook_secret"])
        self.assertEqual(observed["custom_headers"], {})
        self.assertEqual(observed["environment_header_names"], [])
        self.assertTrue(client.is_closed())

    def test_deepseek_environment_isolation_failure_stops_before_model(self) -> None:
        class UnisolatableClient(_FakeAsyncOpenAI):
            def __setattr__(self, name, value) -> None:
                if name in {
                    "organization",
                    "project",
                    "admin_api_key",
                    "webhook_secret",
                    "_custom_headers",
                }:
                    raise AttributeError("simulated SDK drift")
                super().__setattr__(name, value)

        provider = DeepSeekProvider()

        async def exercise() -> None:
            async with provider.open_model(
                model_id="deepseek-v4-flash",
                api_key="SAFE-DEEPSEEK-TEST-KEY",
            ):
                raise AssertionError("model context must not open")

        with patch("openai.AsyncOpenAI", UnisolatableClient), patch(
            "agents.OpenAIResponsesModel"
        ) as model_class, self.assertRaises(ProviderConfigurationError) as caught:
            asyncio.run(exercise())

        self.assertEqual(
            caught.exception.code,
            "provider_client_environment_isolation_failed",
        )
        model_class.assert_not_called()
        self.assertTrue(UnisolatableClient.instances[-1].closed)

    def test_anthropic_uses_litellm_namespace_and_closes_after_exception(self) -> None:
        provider = AnthropicProvider()

        async def exercise() -> None:
            async with provider.open_model(
                model_id="claude-sonnet-5",
                api_key="test-anthropic-key",
                _authorization=_anthropic_test_authorization(),
            ) as bound:
                self.assertEqual(bound.provider_id, "anthropic")
                self.assertEqual(
                    bound.transport_id, "litellm_anthropic_chat_completions"
                )
                self.assertEqual(bound.api_surface, "messages")
                self.assertEqual(
                    bound.adapter_version, "anthropic-litellm-adapter/1.0"
                )
                self.assertIsNone(bound.completion_telemetry_session)
                self.assertNotIn("test-anthropic-key", repr(bound))
                raise RuntimeError("controlled test failure")

        with patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                _FakeLitellmModel,
                _FakeAsyncHTTPHandler,
                _FakeLitellmModule,
            ),
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(exercise())

        model = _FakeLitellmModel.instances[-1]
        self.assertEqual(model.kwargs["model"], "anthropic/claude-sonnet-5")
        self.assertEqual(model.kwargs["api_key"], "test-anthropic-key")
        self.assertEqual(model.kwargs["base_url"], "https://api.anthropic.com")
        self.assertIsNone(model.api_key)
        handler = _FakeAsyncHTTPHandler.instances[-1]
        self.assertEqual(handler.kwargs["timeout"], 120.0)
        self.assertTrue(handler.closed)

    def test_anthropic_injects_owned_client_timeout_and_zero_retries_after_trace(self) -> None:
        from agents import ModelSettings

        provider = AnthropicProvider()

        async def exercise() -> tuple[object, object]:
            original = ModelSettings(
                extra_args={
                    "timeout": 999,
                    "num_retries": 9,
                    "client": "unsafe",
                    "retry_policy": {"RateLimitErrorRetries": 9},
                    "context_window_fallback_dict": {
                        "anthropic/claude-sonnet-5": "another-provider/model"
                    },
                }
            )
            async with provider.open_model(
                model_id="claude-sonnet-5",
                api_key="test-anthropic-key",
                timeout_seconds=17.0,
                _authorization=_anthropic_test_authorization(),
            ) as bound:
                await bound.sdk_model._fetch_response(
                    system_instructions=None,
                    input="offline",
                    model_settings=original,
                    tools=[],
                    output_schema=None,
                    handoffs=[],
                    span=None,
                    tracing=None,
                    stream=False,
                    prompt=None,
                )
                return original, bound.sdk_model.fetch_settings

        with patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                _FakeLitellmModel,
                _FakeAsyncHTTPHandler,
                _FakeLitellmModule,
            ),
        ), self.assertRaises(ProviderConfigurationError) as caught:
            asyncio.run(exercise())

        self.assertEqual(
            caught.exception.code,
            "provider_completion_telemetry_session_required",
        )
        self.assertIsNone(_FakeLitellmModel.instances[-1].fetch_settings)
        self.assertTrue(_FakeAsyncHTTPHandler.instances[-1].closed)

    def test_anthropic_discards_and_restores_process_global_debug_records(self) -> None:
        from agents import ModelSettings

        class LeakyLitellmModel(_FakeLitellmModel):
            async def _fetch_response(self, **kwargs):
                _FakeLitellmModule.error_logs["PRE_CALL"] = {
                    "api_key": self.api_key,
                    "input": kwargs["input"],
                }
                return "controlled-response"

        original_logs: dict[str, object] = {}
        _FakeLitellmModule.error_logs = original_logs

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="GLOBAL-RETENTION-CANARY",
                _authorization=_anthropic_test_authorization(),
            ) as bound:
                await bound.sdk_model._fetch_response(
                    system_instructions=None,
                    input="PRIVATE-PROMPT-CANARY",
                    model_settings=ModelSettings(),
                    tools=[],
                    output_schema=None,
                    handoffs=[],
                    span=None,
                    tracing=None,
                    stream=False,
                    prompt=None,
                )

        with patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                LeakyLitellmModel,
                _FakeAsyncHTTPHandler,
                _FakeLitellmModule,
            ),
        ), self.assertRaises(ProviderConfigurationError) as caught:
            asyncio.run(exercise())

        self.assertEqual(
            caught.exception.code,
            "provider_completion_telemetry_session_required",
        )
        self.assertIs(_FakeLitellmModule.error_logs, original_logs)
        self.assertEqual(original_logs, {})
        self.assertNotIn("GLOBAL-RETENTION-CANARY", repr(original_logs))
        self.assertNotIn("PRIVATE-PROMPT-CANARY", repr(original_logs))

    def test_missing_key_stops_before_client_creation(self) -> None:
        provider = DeepSeekProvider()

        async def exercise() -> None:
            async with provider.open_model(
                model_id="deepseek-v4-pro", api_key="  "
            ):
                self.fail("context body must not run")

        with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "agents.OpenAIResponsesModel", _FakeResponsesModel
        ):
            with self.assertRaises(ProviderConfigurationError) as caught:
                asyncio.run(exercise())
        self.assertEqual(caught.exception.code, "provider_api_key_missing")
        self.assertEqual(_FakeAsyncOpenAI.instances, [])
        self.assertNotIn("test-deepseek-key", str(caught.exception))

    def test_anthropic_missing_key_stops_before_transport_import(self) -> None:
        provider = AnthropicProvider()

        async def exercise() -> None:
            async with provider.open_model(
                model_id="claude-sonnet-5",
                api_key="  ",
                _authorization=_anthropic_test_authorization(),
            ):
                self.fail("context body must not run")

        with patch(
            "researchops.model_providers._load_litellm_transport"
        ) as load_transport:
            with self.assertRaises(ProviderConfigurationError) as caught:
                asyncio.run(exercise())
        self.assertEqual(caught.exception.code, "provider_api_key_missing")
        load_transport.assert_not_called()

    def test_anthropic_invalid_timeout_stops_before_transport_import(self) -> None:
        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="offline-placeholder",
                timeout_seconds=0,
                _authorization=_anthropic_test_authorization(),
            ):
                self.fail("context body must not run")

        with patch(
            "researchops.model_providers._load_litellm_transport"
        ) as load_transport:
            with self.assertRaises(ProviderConfigurationError) as caught:
                asyncio.run(exercise())
        self.assertEqual(caught.exception.code, "provider_timeout_invalid")
        load_transport.assert_not_called()

    def test_anthropic_transport_status_is_offline_and_exactly_pinned(self) -> None:
        status = provider_transport_status(AnthropicProvider())
        self.assertTrue(status["installed"])
        self.assertTrue(status["compatible"])
        self.assertEqual(status["packages"]["openai-agents"], "0.21.0")
        self.assertEqual(status["packages"]["litellm"], "1.83.0")
        self.assertEqual(
            status["expected_versions"],
            {"openai-agents": "0.21.0", "litellm": "1.83.0"},
        )
        self.assertEqual(status["network_calls"], 0)

    def test_anthropic_open_model_rejects_dependency_drift_before_import(self) -> None:
        def drifted_version(name: str) -> str:
            return {"openai-agents": "0.21.0", "litellm": "9.9.9"}[name]

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="offline-placeholder",
                _authorization=_anthropic_test_authorization(),
            ):
                self.fail("context body must not run")

        with patch(
            "researchops.model_providers.importlib.metadata.version",
            side_effect=drifted_version,
        ), patch(
            "researchops.model_providers._load_litellm_transport"
        ) as load_transport:
            with self.assertRaises(ProviderConfigurationError) as caught:
                asyncio.run(exercise())
        self.assertEqual(
            caught.exception.code, "provider_transport_dependency_drift"
        )
        load_transport.assert_not_called()

    def test_anthropic_rejects_nonempty_litellm_global_routing(self) -> None:
        class UnsafeLitellmModule(_FakeLitellmModule):
            fallbacks = [{"claude-sonnet-5": ["another-provider"]}]

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="test-anthropic-key",
                _authorization=_anthropic_test_authorization(),
            ):
                self.fail("context body must not run")

        with patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                _FakeLitellmModel,
                _FakeAsyncHTTPHandler,
                UnsafeLitellmModule,
            ),
        ):
            with self.assertRaises(ProviderConfigurationError) as caught:
                asyncio.run(exercise())
        self.assertEqual(caught.exception.code, "provider_global_state_not_isolated")
        self.assertEqual(_FakeAsyncHTTPHandler.instances, [])

    def test_anthropic_rejects_sync_and_async_global_callbacks(self) -> None:
        class InputCallbackModule(_FakeLitellmModule):
            input_callback = [lambda details: details]

        class AsyncCallbackModule(_FakeLitellmModule):
            _async_success_callback = [lambda details: details]

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="test-anthropic-key",
                _authorization=_anthropic_test_authorization(),
            ):
                self.fail("context body must not run")

        for module in (InputCallbackModule, AsyncCallbackModule):
            with self.subTest(module=module.__name__), patch(
                "researchops.model_providers._load_litellm_transport",
                return_value=(
                    _FakeLitellmModel,
                    _FakeAsyncHTTPHandler,
                    module,
                ),
            ):
                with self.assertRaises(ProviderConfigurationError) as caught:
                    asyncio.run(exercise())
            self.assertEqual(
                caught.exception.code, "provider_global_state_not_isolated"
            )
            self.assertEqual(_FakeAsyncHTTPHandler.instances, [])

    def test_anthropic_rejects_nonzero_global_litellm_retries(self) -> None:
        class RetryingLitellmModule(_FakeLitellmModule):
            num_retries = 2

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="test-anthropic-key",
                _authorization=_anthropic_test_authorization(),
            ):
                self.fail("context body must not run")

        with patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                _FakeLitellmModel,
                _FakeAsyncHTTPHandler,
                RetryingLitellmModule,
            ),
        ):
            with self.assertRaises(ProviderConfigurationError) as caught:
                asyncio.run(exercise())
        self.assertEqual(caught.exception.code, "provider_global_state_not_isolated")
        self.assertEqual(_FakeAsyncHTTPHandler.instances, [])

    def test_anthropic_rejects_global_model_aliases(self) -> None:
        class AliasedLitellmModule(_FakeLitellmModule):
            model_alias_map = {
                "anthropic/claude-sonnet-5": "another-provider/model"
            }

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="test-anthropic-key",
                _authorization=_anthropic_test_authorization(),
            ):
                self.fail("context body must not run")

        with patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                _FakeLitellmModel,
                _FakeAsyncHTTPHandler,
                AliasedLitellmModule,
            ),
        ):
            with self.assertRaises(ProviderConfigurationError) as caught:
                asyncio.run(exercise())
        self.assertEqual(caught.exception.code, "provider_global_state_not_isolated")
        self.assertEqual(_FakeAsyncHTTPHandler.instances, [])

    def test_anthropic_constructor_failure_still_closes_owned_handler(self) -> None:
        class FailingLitellmModel:
            def __init__(self, **kwargs) -> None:
                del kwargs
                raise RuntimeError("controlled constructor failure")

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="test-anthropic-key",
                _authorization=_anthropic_test_authorization(),
            ):
                self.fail("context body must not run")

        with patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(
                FailingLitellmModel,
                _FakeAsyncHTTPHandler,
                _FakeLitellmModule,
            ),
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(exercise())
        self.assertTrue(_FakeAsyncHTTPHandler.instances[-1].closed)

    def test_anthropic_concurrent_process_global_transport_is_denied(self) -> None:
        self.assertTrue(provider_module._LITELLM_PROCESS_LOCK.acquire(blocking=False))
        self.addCleanup(
            lambda: (
                provider_module._LITELLM_PROCESS_LOCK.release()
                if provider_module._LITELLM_PROCESS_LOCK.locked()
                else None
            )
        )

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="offline-placeholder",
                _authorization=_anthropic_test_authorization(),
            ):
                self.fail("context body must not run")

        with self.assertRaises(ProviderConfigurationError) as caught:
            asyncio.run(exercise())
        self.assertEqual(caught.exception.code, "provider_concurrency_denied")

    def test_real_sdk_deepseek_model_constructs_without_network(self) -> None:
        from agents import OpenAIResponsesModel

        async def exercise() -> tuple[str, str, bool]:
            async with DeepSeekProvider().open_model(
                model_id="deepseek-v4-flash",
                api_key="offline-construction-only",
            ) as bound:
                return (
                    bound.provider_id,
                    bound.transport_id,
                    isinstance(bound.sdk_model, OpenAIResponsesModel),
                )

        provider_id, transport_id, correct_type = asyncio.run(exercise())
        self.assertEqual(provider_id, "deepseek")
        self.assertEqual(transport_id, "openai_compatible_responses")
        self.assertTrue(correct_type)

    def test_real_sdk_anthropic_model_constructs_without_network(self) -> None:
        from agents.extensions.models.litellm_model import LitellmModel

        async def exercise() -> tuple[str, str, bool]:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key="offline-construction-only",
                _authorization=_anthropic_test_authorization(),
            ) as bound:
                return (
                    bound.provider_id,
                    bound.transport_id,
                    isinstance(bound.sdk_model, LitellmModel),
                )

        with patch(
            "litellm.acompletion",
            side_effect=AssertionError("offline construction must not call Provider"),
        ) as completion:
            provider_id, transport_id, correct_type = asyncio.run(exercise())
        self.assertEqual(provider_id, "anthropic")
        self.assertEqual(transport_id, "litellm_anthropic_chat_completions")
        self.assertTrue(correct_type)
        completion.assert_not_called()

    def test_real_litellm_failure_retains_no_key_or_input_in_global_logs(self) -> None:
        with patch.dict(
            os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}
        ):
            import litellm
            from agents import ModelSettings, ModelTracing
            from agents.extensions.models.litellm_model import LitellmModel
            from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

        key_canary = "offline-key-material-0123456789"
        input_canary = "offline-sensitive-input"

        class NoNetworkHandler(AsyncHTTPHandler):
            post_calls = 0

            async def post(self, *args, **kwargs):
                del args, kwargs
                type(self).post_calls += 1
                raise RuntimeError("offline transport stop")

        async def exercise() -> None:
            async with AnthropicProvider().open_model(
                model_id="claude-sonnet-5",
                api_key=key_canary,
                timeout_seconds=1,
                _authorization=_anthropic_test_authorization(),
            ) as bound:
                await bound.sdk_model._fetch_response(
                    system_instructions=None,
                    input=input_canary,
                    model_settings=ModelSettings(max_tokens=1),
                    tools=[],
                    output_schema=None,
                    handoffs=[],
                    span=None,
                    tracing=ModelTracing.DISABLED,
                    stream=False,
                    prompt=None,
                )

        original_logs: dict[str, object] = {}
        NoNetworkHandler.post_calls = 0
        with patch.object(litellm, "error_logs", original_logs), patch(
            "researchops.model_providers._load_litellm_transport",
            return_value=(LitellmModel, NoNetworkHandler, litellm),
        ):
            with self.assertRaises(Exception) as caught:
                asyncio.run(exercise())
            self.assertIs(litellm.error_logs, original_logs)
            self.assertEqual(original_logs, {})
            self.assertNotIn(key_canary, repr(litellm.error_logs))
            self.assertNotIn(input_canary, repr(litellm.error_logs))

        self.assertEqual(NoNetworkHandler.post_calls, 0)
        self.assertIsInstance(caught.exception, ProviderConfigurationError)
        self.assertEqual(
            caught.exception.code,
            "provider_completion_telemetry_session_required",
        )
        self.assertNotIn(key_canary, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
