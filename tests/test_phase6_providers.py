from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from researchops.model_providers import (
    DeepSeekProvider,
    OpenAIProvider,
    ProviderConfigurationError,
    ProviderModel,
    get_provider,
)


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


class Phase6ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeAsyncOpenAI.instances.clear()

    def test_get_provider_resolves_only_the_two_supported_providers(self) -> None:
        self.assertIsInstance(get_provider("openai"), OpenAIProvider)
        self.assertIsInstance(get_provider(" DeepSeek "), DeepSeekProvider)
        with self.assertRaises(ProviderConfigurationError) as caught:
            get_provider("untrusted-provider")
        self.assertEqual(caught.exception.code, "provider_invalid")

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

    def test_openai_builds_concrete_model_without_base_url_and_closes(self) -> None:
        provider = OpenAIProvider()

        async def exercise() -> ProviderModel:
            async with provider.open_model(
                model_id="gpt-5.4-mini", api_key=" test-openai-key "
            ) as bound:
                self.assertFalse(_FakeAsyncOpenAI.instances[-1].closed)
                return bound

        with patch("openai.AsyncOpenAI", _FakeAsyncOpenAI), patch(
            "agents.OpenAIResponsesModel", _FakeResponsesModel
        ):
            bound = asyncio.run(exercise())

        client = _FakeAsyncOpenAI.instances[-1]
        self.assertEqual(client.kwargs["api_key"], "test-openai-key")
        self.assertEqual(client.kwargs["max_retries"], 0)
        self.assertEqual(client.kwargs["timeout"], 120.0)
        self.assertNotIn("base_url", client.kwargs)
        self.assertTrue(client.closed)
        self.assertEqual(bound.provider_id, "openai")
        self.assertEqual(bound.model_id, "gpt-5.4-mini")
        self.assertEqual(bound.transport_id, "openai_responses")
        self.assertIsInstance(bound.sdk_model, _FakeResponsesModel)
        self.assertNotIn("_FakeResponsesModel", repr(bound))

    def test_deepseek_uses_fixed_base_url_and_closes_after_exception(self) -> None:
        provider = DeepSeekProvider()

        async def exercise() -> None:
            async with provider.open_model(
                model_id="deepseek-v4-flash", api_key="test-deepseek-key"
            ) as bound:
                self.assertEqual(bound.provider_id, "deepseek")
                self.assertEqual(bound.transport_id, "openai_compatible_responses")
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
        self.assertTrue(client.closed)

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


if __name__ == "__main__":
    unittest.main()
