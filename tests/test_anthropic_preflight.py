from __future__ import annotations

import asyncio
import gzip
import hashlib
import inspect
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import researchops.cli as cli_module
from researchops import anthropic_preflight as preflight_module
from researchops.anthropic_preflight import (
    ANTHROPIC_PREFLIGHT_RECEIPT_FIELDS,
    anthropic_models_preflight_contract,
    run_anthropic_models_preflight,
)


_MODEL_ID = "claude-sonnet-5"
_KEY_CANARY = "offline-anthropic-key-CANARY-0123456789"
_RAW_REQUEST_ID = "req_offline_raw_identifier"
_ROOT = Path(__file__).resolve().parents[1]


def _model_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": _MODEL_ID,
        "type": "model",
        "display_name": "Claude Sonnet 5",
        "created_at": "2026-08-01T12:34:56Z",
        "capabilities": {"tool_use": True},
        "max_input_tokens": 200_000,
        "max_tokens": 64_000,
    }
    payload.update(overrides)
    return payload


def _json_body(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _model_payload_without(field: str) -> dict[str, object]:
    payload = _model_payload()
    del payload[field]
    return payload


class _TrackingMockTransport(httpx.MockTransport):
    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


class _CloseFailingTransport(_TrackingMockTransport):
    async def aclose(self) -> None:
        self.closed = True
        await httpx.MockTransport.aclose(self)
        raise RuntimeError(f"close failure {_KEY_CANARY}")


class _HangingCloseTransport(_TrackingMockTransport):
    async def aclose(self) -> None:
        self.closed = True
        await asyncio.Event().wait()


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _run_with_handler(handler, **overrides: object):
    transport = _TrackingMockTransport(handler)
    kwargs: dict[str, object] = {
        "provider_id": "anthropic",
        "model_id": _MODEL_ID,
        "api_key": _KEY_CANARY,
        "confirm_online": True,
        "_transport_factory": lambda: transport,
    }
    kwargs.update(overrides)
    result = asyncio.run(run_anthropic_models_preflight(**kwargs))
    return result, transport


class AnthropicModelsPreflightTests(unittest.TestCase):
    def test_cli_confirmation_gate_precedes_environment_key_lookup(self) -> None:
        class ForbiddenEnvironment(dict[str, str]):
            def get(self, key, default=None):
                if key == "ANTHROPIC_API_KEY":
                    raise AssertionError("unconfirmed preflight must not read Key")
                return super().get(key, default)

        parsed = cli_module.build_parser().parse_args(
            ["anthropic-models-preflight", "--model", _MODEL_ID]
        )
        fixed_parser = SimpleNamespace(parse_args=lambda: parsed)
        output = io.StringIO()
        with (
            patch.object(cli_module, "build_parser", return_value=fixed_parser),
            patch.object(cli_module.os, "environ", ForbiddenEnvironment()),
            redirect_stdout(output),
        ):
            exit_code = cli_module.main()

        receipt = json.loads(output.getvalue())
        self.assertEqual(exit_code, 4)
        self.assertEqual(receipt["status"], "not_run")
        self.assertEqual(
            receipt["error_code"], "anthropic_preflight_confirmation_required"
        )
        self.assertEqual(receipt["network_calls"], 0)

    def test_cli_confirmed_path_passes_memory_key_to_injected_preflight(self) -> None:
        parsed = cli_module.build_parser().parse_args(
            [
                "anthropic-models-preflight",
                "--model",
                _MODEL_ID,
                "--confirm-online",
            ]
        )
        fixed_parser = SimpleNamespace(parse_args=lambda: parsed)
        observed: dict[str, object] = {}

        async def fake_preflight(**kwargs):
            observed.update(kwargs)
            observed["loaded_key"] = kwargs["_key_loader"]()
            return {
                "status": "verified",
                "provider_id": "anthropic",
                "requested_model_id": _MODEL_ID,
                "network_calls": 1,
                "model_token_calls": 0,
                "token_usage": None,
                "cost": None,
                "authorizes_model_run": False,
            }

        output = io.StringIO()
        with (
            patch.object(cli_module, "build_parser", return_value=fixed_parser),
            patch.object(
                cli_module,
                "run_anthropic_models_preflight",
                side_effect=fake_preflight,
            ),
            patch.object(
                cli_module.os,
                "environ",
                {"ANTHROPIC_API_KEY": _KEY_CANARY},
            ),
            redirect_stdout(output),
        ):
            exit_code = cli_module.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(observed["provider_id"], "anthropic")
        self.assertEqual(observed["model_id"], _MODEL_ID)
        self.assertIsNone(observed["api_key"])
        self.assertEqual(observed["loaded_key"], _KEY_CANARY)
        self.assertIs(observed["confirm_online"], True)
        self.assertNotIn(_KEY_CANARY, output.getvalue())

    def test_cli_invalid_model_denies_before_environment_key_lookup(self) -> None:
        class ForbiddenEnvironment(dict[str, str]):
            def get(self, key, default=None):
                if key == "ANTHROPIC_API_KEY":
                    raise AssertionError("invalid model must precede Key lookup")
                return super().get(key, default)

        parsed = cli_module.build_parser().parse_args(
            [
                "anthropic-models-preflight",
                "--model",
                f" {_MODEL_ID}",
                "--confirm-online",
            ]
        )
        fixed_parser = SimpleNamespace(parse_args=lambda: parsed)
        output = io.StringIO()
        with (
            patch.object(cli_module, "build_parser", return_value=fixed_parser),
            patch.object(cli_module.os, "environ", ForbiddenEnvironment()),
            redirect_stdout(output),
        ):
            exit_code = cli_module.main()

        receipt = json.loads(output.getvalue())
        self.assertEqual(exit_code, 4)
        self.assertEqual(receipt["status"], "not_run")
        self.assertEqual(
            receipt["error_code"], "anthropic_preflight_model_not_allowed"
        )
        self.assertEqual(receipt["network_calls"], 0)

    def test_machine_contract_matches_runtime_without_network(self) -> None:
        snapshot = json.loads(
            (
                _ROOT
                / "evals"
                / "v2"
                / "anthropic_models_preflight_contract.json"
            ).read_text(encoding="utf-8")
        )
        runtime = anthropic_models_preflight_contract()

        self.assertEqual(snapshot, runtime)
        self.assertEqual(
            runtime["implementation_status"],
            "implemented_offline_tested_not_run",
        )
        self.assertEqual(runtime["evaluation_boundary"]["offline_test_network_calls"], 0)
        self.assertFalse(runtime["evaluation_boundary"]["online_calls_performed"])
        self.assertFalse(runtime["entry_points"]["controlled_anthropic_pilot_enabled"])

    def assert_strict_receipt(self, receipt: dict[str, object]) -> None:
        self.assertEqual(tuple(receipt), ANTHROPIC_PREFLIGHT_RECEIPT_FIELDS)
        self.assertEqual(receipt["schema_version"], "anthropic-models-preflight/1.0")
        self.assertEqual(receipt["provider_id"], "anthropic")
        self.assertEqual(
            receipt["verification_method"], "anthropic_models_retrieve"
        )
        self.assertEqual(receipt["api_origin"], "https://api.anthropic.com")
        self.assertEqual(receipt["anthropic_version"], "2023-06-01")
        self.assertEqual(receipt["model_token_calls"], 0)
        self.assertIsNone(receipt["token_usage"])
        self.assertIsNone(receipt["cost"])
        for field in (
            "messages_api_verified",
            "tool_calling_verified",
            "usage_semantics_verified",
            "model_quality_claim_allowed",
            "authorizes_model_run",
        ):
            self.assertIs(receipt[field], False)
        self.assertRegex(
            receipt["checked_at_utc"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
        )

    def test_confirmation_gate_stops_before_key_or_client_use(self) -> None:
        factory_calls = 0

        def forbidden_factory():
            nonlocal factory_calls
            factory_calls += 1
            raise AssertionError("client must not be created")

        receipt = asyncio.run(
            run_anthropic_models_preflight(
                provider_id="anthropic",
                model_id=_MODEL_ID,
                api_key=_KEY_CANARY,
                confirm_online=False,
                _transport_factory=forbidden_factory,
            )
        )
        self.assert_strict_receipt(receipt)
        self.assertEqual(receipt["status"], "not_run")
        self.assertEqual(
            receipt["error_code"], "anthropic_preflight_confirmation_required"
        )
        self.assertEqual(receipt["http_attempts"], 0)
        self.assertEqual(receipt["network_calls"], 0)
        self.assertNotIn(_KEY_CANARY, repr(receipt))
        self.assertEqual(factory_calls, 0)

    def test_missing_key_stops_before_client_creation(self) -> None:
        for api_key in (None, "", "   "):
            with self.subTest(api_key=api_key):
                factory_calls = 0

                def forbidden_factory():
                    nonlocal factory_calls
                    factory_calls += 1
                    raise AssertionError("client must not be created")

                receipt = asyncio.run(
                    run_anthropic_models_preflight(
                        provider_id="anthropic",
                        model_id=_MODEL_ID,
                        api_key=api_key,
                        confirm_online=True,
                        _transport_factory=forbidden_factory,
                    )
                )
                self.assertEqual(receipt["status"], "not_run")
                self.assertEqual(
                    receipt["error_code"], "anthropic_preflight_key_missing"
                )
                self.assertEqual(receipt["network_calls"], 0)
                self.assertEqual(factory_calls, 0)

    def test_unsafe_header_key_stops_before_client_creation_without_reflection(self) -> None:
        for api_key in (
            " leading-space",
            "trailing-space ",
            "line\r\nbreak",
            "non-ascii-密钥",
            "x" * 513,
        ):
            with self.subTest(length=len(api_key)):
                factory_calls = 0

                def forbidden_factory():
                    nonlocal factory_calls
                    factory_calls += 1
                    raise AssertionError("client must not be created")

                receipt = asyncio.run(
                    run_anthropic_models_preflight(
                        provider_id="anthropic",
                        model_id=_MODEL_ID,
                        api_key=api_key,
                        confirm_online=True,
                        _transport_factory=forbidden_factory,
                    )
                )
                self.assertEqual(receipt["status"], "not_run")
                self.assertEqual(
                    receipt["error_code"], "anthropic_preflight_key_invalid"
                )
                self.assertEqual(receipt["network_calls"], 0)
                self.assertEqual(factory_calls, 0)
                self.assertNotIn(api_key, repr(receipt))

    def test_provider_and_model_must_be_exact_before_client_creation(self) -> None:
        cases = (
            (
                {"provider_id": " Anthropic ", "model_id": _MODEL_ID},
                "anthropic_preflight_configuration_invalid",
                _MODEL_ID,
            ),
            (
                {"provider_id": "deepseek", "model_id": _MODEL_ID},
                "anthropic_preflight_configuration_invalid",
                _MODEL_ID,
            ),
            (
                {"provider_id": "anthropic", "model_id": f" {_MODEL_ID}"},
                "anthropic_preflight_model_not_allowed",
                None,
            ),
            (
                {
                    "provider_id": "anthropic",
                    "model_id": "claude-sonnet-5-latest",
                },
                "anthropic_preflight_model_not_allowed",
                None,
            ),
            (
                {"provider_id": "anthropic", "model_id": "https://evil.invalid"},
                "anthropic_preflight_model_not_allowed",
                None,
            ),
        )
        for inputs, expected_code, expected_model in cases:
            with self.subTest(inputs=inputs):
                factory_calls = 0

                def forbidden_factory():
                    nonlocal factory_calls
                    factory_calls += 1
                    raise AssertionError("client must not be created")

                receipt = asyncio.run(
                    run_anthropic_models_preflight(
                        api_key=_KEY_CANARY,
                        confirm_online=True,
                        _transport_factory=forbidden_factory,
                        **inputs,
                    )
                )
                self.assertEqual(receipt["status"], "not_run")
                self.assertEqual(receipt["error_code"], expected_code)
                self.assertEqual(receipt["requested_model_id"], expected_model)
                self.assertNotIn("evil.invalid", repr(receipt))
                self.assertEqual(factory_calls, 0)

    def test_dependency_and_timeout_drift_stop_before_client_creation(self) -> None:
        for target, replacement in (
            (
                "researchops.anthropic_preflight.importlib.metadata.version",
                lambda name: "9.9.9" if name == "httpx" else "unexpected",
            ),
            (
                "researchops.anthropic_preflight.importlib.metadata.version",
                lambda name: {
                    "httpx": "0.28.1",
                    "certifi": "0.0.0",
                }[name],
            ),
            (
                "researchops.anthropic_preflight.importlib.metadata.version",
                lambda name: {
                    "httpx": "0.28.1",
                    "certifi": "2026.7.22",
                    "httpcore": "0.0.0",
                }[name],
            ),
            (
                "researchops.anthropic_preflight.importlib.metadata.version",
                lambda name: {
                    "httpx": "0.28.1",
                    "certifi": "2026.7.22",
                    "httpcore": "1.0.9",
                    "h11": "0.0.0",
                }[name],
            ),
            ("researchops.anthropic_preflight._READ_TIMEOUT_SECONDS", 0.0),
            ("researchops.anthropic_preflight._TOTAL_DEADLINE_SECONDS", 1.0),
            ("researchops.anthropic_preflight._CLOSE_TIMEOUT_SECONDS", 0.0),
            (
                "researchops.anthropic_preflight.logging.getLogger",
                lambda name: SimpleNamespace(
                    isEnabledFor=lambda level: name == "httpcore.http11"
                ),
            ),
        ):
            with self.subTest(target=target), patch(target, replacement):
                factory_calls = 0

                def forbidden_factory():
                    nonlocal factory_calls
                    factory_calls += 1
                    raise AssertionError("client must not be created")

                receipt = asyncio.run(
                    run_anthropic_models_preflight(
                        provider_id="anthropic",
                        model_id=_MODEL_ID,
                        api_key=_KEY_CANARY,
                        confirm_online=True,
                        _transport_factory=forbidden_factory,
                    )
                )
                self.assertEqual(receipt["status"], "not_run")
                self.assertEqual(
                    receipt["error_code"],
                    "anthropic_preflight_configuration_invalid",
                )
                self.assertEqual(receipt["network_calls"], 0)
                self.assertEqual(factory_calls, 0)

    def test_exact_request_identity_headers_and_owned_client_controls(self) -> None:
        calls: list[httpx.Request] = []
        observed_client: dict[str, object] = {}
        original_build_client = preflight_module._build_client

        def recording_build_client(transport):
            client = original_build_client(transport)
            observed_client.update(
                {
                    "base_url": str(client.base_url),
                    "follow_redirects": client.follow_redirects,
                    "trust_env": client._trust_env,
                    "event_hooks": client.event_hooks,
                    "timeout": client.timeout,
                }
            )
            return client

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            self.assertEqual(request.method, "GET")
            self.assertEqual(
                str(request.url),
                "https://api.anthropic.com/v1/models/claude-sonnet-5",
            )
            self.assertEqual(await request.aread(), b"")
            self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
            self.assertEqual(request.headers["x-api-key"], _KEY_CANARY)
            self.assertEqual(request.headers["accept"], "application/json")
            self.assertEqual(request.headers["accept-encoding"], "identity")
            self.assertEqual(
                request.headers["user-agent"],
                "researchops-agent/0.2.0 anthropic-models-preflight/1.0",
            )
            for forbidden in (
                "authorization",
                "content-type",
                "content-length",
                "anthropic-beta",
            ):
                self.assertNotIn(forbidden, request.headers)
            self.assertNotIn("messages", request.url.path)
            return httpx.Response(
                200,
                headers={"request-id": _RAW_REQUEST_ID},
                content=_json_body(_model_payload()),
            )

        environment = {
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1",
            "NETRC": "Z:/must-not-be-read.netrc",
        }
        with patch.dict(os.environ, environment, clear=False), patch(
            "researchops.anthropic_preflight._build_client",
            side_effect=recording_build_client,
        ):
            receipt, transport = _run_with_handler(handler)

        self.assertTrue(transport.closed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(observed_client["base_url"], "https://api.anthropic.com")
        self.assertIs(observed_client["follow_redirects"], False)
        self.assertIs(observed_client["trust_env"], False)
        self.assertEqual(
            observed_client["event_hooks"], {"request": [], "response": []}
        )
        timeout = observed_client["timeout"]
        self.assertEqual(timeout.connect, 5.0)
        self.assertEqual(timeout.read, 10.0)
        self.assertEqual(timeout.write, 5.0)
        self.assertEqual(timeout.pool, 5.0)
        signature = inspect.signature(run_anthropic_models_preflight)
        for prohibited_argument in ("base_url", "url", "headers", "proxy", "body"):
            self.assertNotIn(prohibited_argument, signature.parameters)
        self.assertEqual(receipt["status"], "verified")

    def test_success_receipt_is_strict_redacted_and_non_authorizing(self) -> None:
        provider_only_canary = "UNKNOWN-PROVIDER-FIELD-CANARY"

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            payload = _model_payload(
                extra_provider_field=provider_only_canary,
                capabilities={"unknown_future_capability": {"enabled": True}},
            )
            return httpx.Response(
                200,
                headers={"request-id": _RAW_REQUEST_ID},
                content=_json_body(payload),
            )

        receipt, transport = _run_with_handler(handler)
        self.assertTrue(transport.closed)
        self.assert_strict_receipt(receipt)
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["requested_model_id"], _MODEL_ID)
        self.assertEqual(receipt["returned_model_id"], _MODEL_ID)
        self.assertEqual(receipt["http_status"], 200)
        self.assertEqual(receipt["http_attempts"], 1)
        self.assertEqual(receipt["network_calls"], 1)
        self.assertGreaterEqual(receipt["latency_ms"], 0)
        self.assertIs(receipt["models_api_authenticated"], True)
        self.assertIs(receipt["exact_model_visible"], True)
        self.assertEqual(
            receipt["request_id_sha256"],
            hashlib.sha256(_RAW_REQUEST_ID.encode()).hexdigest(),
        )
        self.assertIsNone(receipt["error_code"])
        serialized = repr(receipt)
        for forbidden in (
            _KEY_CANARY,
            _RAW_REQUEST_ID,
            provider_only_canary,
            "display_name",
            "capabilities",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_success_does_not_mutate_candidate_campaign_pilot_or_private_state(self) -> None:
        protected = (
            _ROOT / "evals" / "v2" / "campaign.json",
            _ROOT / "evals" / "v2" / "public_regression_candidate_v4.json",
            _ROOT / "evals" / "v2" / "public_regression_candidate_v5.json",
            _ROOT / "evals" / "v2" / "public_regression_candidate_v6.json",
            _ROOT / "evals" / "v2" / "public_regression_candidate_v7.json",
            _ROOT
            / "services"
            / "pilot_staging"
            / "content"
            / "pilot_pack.supervised_v5.json",
            _ROOT
            / "services"
            / "pilot_staging"
            / "content"
            / "pilot_pack.supervised_v6.json",
            _ROOT
            / "services"
            / "pilot_staging"
            / "content"
            / "pilot_pack.supervised_v7.json",
            _ROOT
            / "services"
            / "pilot_staging"
            / "content"
            / "pilot_pack.supervised_v8.json",
            _ROOT / "evals" / "v2" / "private_holdout_kit" / "protocol.json",
        )
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in protected
        }

        receipt, _ = _run_with_handler(
            lambda request: httpx.Response(
                200, content=_json_body(_model_payload())
            )
        )

        self.assertEqual(receipt["status"], "verified")
        self.assertFalse(receipt["authorizes_model_run"])
        self.assertEqual(
            before,
            {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in protected
            },
        )

    def test_unsafe_or_key_related_request_id_is_not_hashed_or_persisted(self) -> None:
        cases = (
            (_KEY_CANARY, _KEY_CANARY),
            (f"req_prefix-{_KEY_CANARY}-suffix", _KEY_CANARY),
            ("raw request id with spaces", _KEY_CANARY),
            ("req_short", "prefix-req_short-suffix"),
        )
        for request_id, api_key in cases:
            with self.subTest(request_id=request_id):
                receipt, _ = _run_with_handler(
                    lambda request, request_id=request_id: httpx.Response(
                        200,
                        headers={"request-id": request_id},
                        content=_json_body(_model_payload()),
                    ),
                    api_key=api_key,
                )
                self.assertEqual(receipt["status"], "verified")
                self.assertIsNone(receipt["request_id_sha256"])
                self.assertNotIn(api_key, repr(receipt))
                self.assertNotIn(
                    hashlib.sha256(api_key.encode()).hexdigest(), repr(receipt)
                )

    def test_only_complete_documented_model_schema_succeeds(self) -> None:
        invalid_payloads: tuple[tuple[object, str], ...] = (
            ({**_model_payload(), "id": "claude-opus-4-8"}, "identity"),
            ({**_model_payload(), "id": "claude-sonnet-5-latest"}, "identity"),
            ({**_model_payload(), "type": "model_alias"}, "identity"),
            (_model_payload_without("id"), "invalid"),
            (_model_payload_without("type"), "invalid"),
            (_model_payload_without("created_at"), "invalid"),
            (_model_payload_without("display_name"), "invalid"),
            (_model_payload_without("capabilities"), "invalid"),
            (_model_payload_without("max_input_tokens"), "invalid"),
            (_model_payload_without("max_tokens"), "invalid"),
            ({**_model_payload(), "id": 7}, "invalid"),
            ({**_model_payload(), "created_at": "2026-08-01"}, "invalid"),
            ({**_model_payload(), "display_name": "  "}, "invalid"),
            ({**_model_payload(), "capabilities": []}, "invalid"),
            ({**_model_payload(), "max_input_tokens": True}, "invalid"),
            ({**_model_payload(), "max_tokens": -1}, "invalid"),
            ([], "invalid"),
        )
        for payload, expected_class in invalid_payloads:
            with self.subTest(payload=payload):
                receipt, transport = _run_with_handler(
                    lambda request, payload=payload: httpx.Response(
                        200, content=_json_body(payload)
                    )
                )
                self.assertTrue(transport.closed)
                self.assertEqual(receipt["status"], "failed")
                expected_code = (
                    "anthropic_preflight_identity_mismatch"
                    if expected_class == "identity"
                    else "anthropic_preflight_response_invalid"
                )
                self.assertEqual(receipt["error_code"], expected_code)
                self.assertEqual(receipt["http_attempts"], 1)
                self.assertIs(receipt["models_api_authenticated"], True)
                self.assertIsNone(receipt["exact_model_visible"])

    def test_malformed_duplicate_and_nonfinite_json_are_rejected(self) -> None:
        bodies = (
            b"{not-json",
            b'{"id":"claude-sonnet-5","id":"claude-sonnet-5"}',
            b'{"id":"claude-sonnet-5","unknown":NaN}',
            b"\xff\xfe\xfd",
        )
        for body in bodies:
            with self.subTest(body=body):
                receipt, _ = _run_with_handler(
                    lambda request, body=body: httpx.Response(200, content=body)
                )
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(
                    receipt["error_code"], "anthropic_preflight_response_invalid"
                )

    def test_decoded_body_limit_accepts_64k_and_aborts_at_byte_65537(self) -> None:
        payload = _model_payload(padding="")
        base = _json_body(payload)
        payload["padding"] = "x" * (64 * 1024 - len(base))
        exact_body = _json_body(payload)
        self.assertEqual(len(exact_body), 64 * 1024)

        exact_stream = _ChunkStream([exact_body])
        exact_receipt, _ = _run_with_handler(
            lambda request: httpx.Response(200, stream=exact_stream)
        )
        self.assertEqual(exact_receipt["status"], "verified")
        self.assertEqual(exact_stream.yielded, 1)
        self.assertTrue(exact_stream.closed)

        oversized_stream = _ChunkStream(
            [b"x" * (64 * 1024), b"y", b"MUST-NOT-BE-READ"]
        )
        oversized_receipt, _ = _run_with_handler(
            lambda request: httpx.Response(200, stream=oversized_stream)
        )
        self.assertEqual(oversized_receipt["status"], "failed")
        self.assertEqual(
            oversized_receipt["error_code"],
            "anthropic_preflight_response_invalid",
        )
        self.assertEqual(oversized_stream.yielded, 2)
        self.assertTrue(oversized_stream.closed)

    def test_nonidentity_encoding_is_rejected_before_body_decompression(
        self,
    ) -> None:
        decoded_body = b"x" * (64 * 1024 + 1)
        compressed_body = gzip.compress(decoded_body)
        compressed_stream = _ChunkStream([compressed_body])

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["accept-encoding"], "identity")
            return httpx.Response(
                200,
                headers={
                    "content-encoding": "gzip",
                    "content-length": "1",
                },
                stream=compressed_stream,
            )

        receipt, _ = _run_with_handler(handler)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(
            receipt["error_code"], "anthropic_preflight_response_invalid"
        )
        self.assertEqual(compressed_stream.yielded, 0)
        self.assertTrue(compressed_stream.closed)

        corrupt_receipt, _ = _run_with_handler(
            lambda request: httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                content=b"not-a-gzip-stream",
            )
        )
        self.assertEqual(corrupt_receipt["status"], "failed")
        self.assertEqual(
            corrupt_receipt["error_code"],
            "anthropic_preflight_response_invalid",
        )

    def test_http_failure_taxonomy_is_stable_and_never_reads_error_body(self) -> None:
        cases = {
            301: "anthropic_preflight_redirect_denied",
            307: "anthropic_preflight_redirect_denied",
            400: "anthropic_preflight_invalid_request_or_spend_limit",
            401: "anthropic_preflight_auth_failed",
            402: "anthropic_preflight_billing_blocked",
            403: "anthropic_preflight_permission_denied",
            404: "anthropic_preflight_model_unavailable",
            408: "anthropic_preflight_provider_timeout",
            409: "anthropic_preflight_conflict",
            413: "anthropic_preflight_protocol_failed",
            429: "anthropic_preflight_rate_or_spend_limited",
            500: "anthropic_preflight_provider_unavailable",
            502: "anthropic_preflight_provider_unavailable",
            503: "anthropic_preflight_provider_unavailable",
            504: "anthropic_preflight_provider_timeout",
            529: "anthropic_preflight_provider_unavailable",
            418: "anthropic_preflight_failed",
        }
        for status, expected_code in cases.items():
            with self.subTest(status=status):
                calls = 0
                error_stream = _ChunkStream(
                    [f"provider error {_KEY_CANARY}".encode()]
                )

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal calls
                    calls += 1
                    return httpx.Response(
                        status,
                        headers={
                            "location": "https://evil.invalid/messages",
                            "request-id": _RAW_REQUEST_ID,
                        },
                        stream=error_stream,
                    )

                receipt, transport = _run_with_handler(handler)
                self.assertTrue(transport.closed)
                self.assertEqual(calls, 1)
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(receipt["error_code"], expected_code)
                self.assertEqual(receipt["http_status"], status)
                self.assertEqual(receipt["http_attempts"], 1)
                self.assertEqual(receipt["network_calls"], 1)
                self.assertEqual(error_stream.yielded, 0)
                self.assertTrue(error_stream.closed)
                self.assertNotIn(_KEY_CANARY, repr(receipt))
                if status == 401:
                    self.assertIs(receipt["models_api_authenticated"], False)
                else:
                    self.assertIsNone(receipt["models_api_authenticated"])
                self.assertIsNone(receipt["exact_model_visible"])

    def test_timeout_network_and_unknown_exceptions_are_redacted_and_not_retried(
        self,
    ) -> None:
        cases = (
            (
                lambda request: httpx.ReadTimeout(
                    f"timeout {_KEY_CANARY}", request=request
                ),
                "anthropic_preflight_timeout",
            ),
            (
                lambda request: httpx.ConnectTimeout(
                    f"connect timeout {_KEY_CANARY}", request=request
                ),
                "anthropic_preflight_timeout",
            ),
            (
                lambda request: httpx.ConnectError(
                    f"dns or tls {_KEY_CANARY}", request=request
                ),
                "anthropic_preflight_network_failed",
            ),
            (
                lambda request: RuntimeError(f"unknown {_KEY_CANARY}"),
                "anthropic_preflight_failed",
            ),
        )
        for exception_factory, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                calls = 0

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal calls
                    calls += 1
                    raise exception_factory(request)

                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    receipt, transport = _run_with_handler(handler)
                self.assertTrue(transport.closed)
                self.assertEqual(calls, 1)
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(receipt["error_code"], expected_code)
                self.assertIsNone(receipt["http_status"])
                self.assertEqual(receipt["http_attempts"], 1)
                self.assertEqual(receipt["network_calls"], 1)
                self.assertNotIn(_KEY_CANARY, repr(receipt))
                self.assertNotIn(_KEY_CANARY, stdout.getvalue())
                self.assertNotIn(_KEY_CANARY, stderr.getvalue())

    def test_caller_cancellation_closes_transport_and_propagates(self) -> None:
        entered = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        transport = _TrackingMockTransport(handler)

        async def exercise() -> None:
            task = asyncio.create_task(
                run_anthropic_models_preflight(
                    provider_id="anthropic",
                    model_id=_MODEL_ID,
                    api_key=_KEY_CANARY,
                    confirm_online=True,
                    _transport_factory=lambda: transport,
                )
            )
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(exercise())
        self.assertTrue(transport.closed)

    def test_client_close_failure_fails_success_but_preserves_primary_failure(
        self,
    ) -> None:
        success_transport = _CloseFailingTransport(
            lambda request: httpx.Response(200, content=_json_body(_model_payload()))
        )
        success_receipt = asyncio.run(
            run_anthropic_models_preflight(
                provider_id="anthropic",
                model_id=_MODEL_ID,
                api_key=_KEY_CANARY,
                confirm_online=True,
                _transport_factory=lambda: success_transport,
            )
        )
        self.assertTrue(success_transport.closed)
        self.assertEqual(success_receipt["status"], "failed")
        self.assertEqual(
            success_receipt["error_code"],
            "anthropic_preflight_client_close_failed",
        )
        self.assertNotIn(_KEY_CANARY, repr(success_receipt))

        failure_transport = _CloseFailingTransport(
            lambda request: httpx.Response(
                401, content=f"provider {_KEY_CANARY}".encode()
            )
        )
        failure_receipt = asyncio.run(
            run_anthropic_models_preflight(
                provider_id="anthropic",
                model_id=_MODEL_ID,
                api_key=_KEY_CANARY,
                confirm_online=True,
                _transport_factory=lambda: failure_transport,
            )
        )
        self.assertTrue(failure_transport.closed)
        self.assertEqual(failure_receipt["status"], "failed")
        self.assertEqual(
            failure_receipt["error_code"], "anthropic_preflight_auth_failed"
        )
        self.assertNotIn(_KEY_CANARY, repr(failure_receipt))

    def test_hanging_client_close_is_bounded_and_fails_success(self) -> None:
        transport = _HangingCloseTransport(
            lambda request: httpx.Response(
                200, content=_json_body(_model_payload())
            )
        )
        with patch(
            "researchops.anthropic_preflight._CLOSE_TIMEOUT_SECONDS", 0.01
        ):
            receipt = asyncio.run(
                run_anthropic_models_preflight(
                    provider_id="anthropic",
                    model_id=_MODEL_ID,
                    api_key=_KEY_CANARY,
                    confirm_online=True,
                    _transport_factory=lambda: transport,
                )
            )

        self.assertTrue(transport.closed)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(
            receipt["error_code"],
            "anthropic_preflight_client_close_failed",
        )

    def test_client_construction_failure_closes_owned_transport_without_attempt(
        self,
    ) -> None:
        transport = _TrackingMockTransport(
            lambda request: self.fail("request must not be attempted")
        )
        with patch(
            "researchops.anthropic_preflight._build_client",
            side_effect=RuntimeError(f"constructor {_KEY_CANARY}"),
        ):
            receipt = asyncio.run(
                run_anthropic_models_preflight(
                    provider_id="anthropic",
                    model_id=_MODEL_ID,
                    api_key=_KEY_CANARY,
                    confirm_online=True,
                    _transport_factory=lambda: transport,
                )
            )
        self.assertTrue(transport.closed)
        self.assertEqual(receipt["status"], "not_run")
        self.assertEqual(receipt["error_code"], "anthropic_preflight_failed")
        self.assertEqual(receipt["http_attempts"], 0)
        self.assertEqual(receipt["network_calls"], 0)
        self.assertNotIn(_KEY_CANARY, repr(receipt))

    def test_default_transport_is_tls_verified_environment_isolated_and_zero_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            keylog = Path(directory) / "must-not-exist.keys"
            with patch.dict(
                os.environ,
                {"SSLKEYLOGFILE": str(keylog)},
                clear=False,
            ):
                transport = preflight_module._default_transport_factory()
            try:
                pool = transport._pool
                self.assertFalse(keylog.exists())
                self.assertEqual(pool._retries, 0)
                self.assertEqual(pool._ssl_context.verify_mode, 2)
                self.assertTrue(pool._ssl_context.check_hostname)
                self.assertIsNone(pool._ssl_context.keylog_filename)
                self.assertGreater(len(pool._ssl_context.get_ca_certs()), 0)
            finally:
                asyncio.run(transport.aclose())


if __name__ == "__main__":
    unittest.main()
