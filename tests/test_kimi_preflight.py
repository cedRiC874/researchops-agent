from __future__ import annotations

import asyncio
import hashlib
import io
import inspect
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import researchops.cli as cli_module
from researchops import kimi_preflight as preflight_module
from researchops.kimi_preflight import (
    KIMI_PREFLIGHT_RECEIPT_FIELDS,
    kimi_models_preflight_contract,
    run_kimi_models_preflight,
)


_MODEL_ID = "kimi-k3"
_KEY_CANARY = "offline-moonshot-key-CANARY-0123456789"
_RAW_REQUEST_ID = "4f8ac10b-58cc-4372-a567-0e02b2c3d479"
_ROOT = Path(__file__).resolve().parents[1]


def _model_entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": _MODEL_ID,
        "object": "model",
        "created": 1_787_702_400,
        "owned_by": "moonshot",
        "context_length": 1_048_576,
        "supports_image_in": True,
        "supports_video_in": True,
        "supports_reasoning": True,
    }
    entry.update(overrides)
    return entry


def _list_payload(
    *, entries: list[object] | None = None, **overrides: object
) -> dict[str, object]:
    payload: dict[str, object] = {
        "object": "list",
        "data": entries if entries is not None else [_model_entry()],
    }
    payload.update(overrides)
    return payload


def _json_body(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class _TrackingMockTransport(httpx.MockTransport):
    def __init__(self, handler) -> None:
        async def documented_json_handler(request: httpx.Request) -> httpx.Response:
            response = handler(request)
            if inspect.isawaitable(response):
                response = await response
            if response.status_code in (200, 429) and "content-type" not in response.headers:
                response.headers["content-type"] = "application/json"
            return response

        super().__init__(documented_json_handler)
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
        "provider_id": "moonshot_kimi",
        "model_id": _MODEL_ID,
        "api_key": _KEY_CANARY,
        "confirm_online": True,
        "_transport_factory": lambda: transport,
    }
    kwargs.update(overrides)
    result = asyncio.run(run_kimi_models_preflight(**kwargs))
    return result, transport


class KimiModelsPreflightTests(unittest.TestCase):
    def test_cli_confirmation_gate_precedes_environment_key_lookup(self) -> None:
        class ForbiddenEnvironment(dict[str, str]):
            def get(self, key, default=None):
                if key == "MOONSHOT_API_KEY":
                    raise AssertionError("unconfirmed preflight must not read Key")
                return super().get(key, default)

        parsed = cli_module.build_parser().parse_args(
            ["kimi-models-preflight", "--model", _MODEL_ID]
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
        self.assertEqual(receipt["error_code"], "kimi_preflight_confirmation_required")
        self.assertEqual(receipt["network_calls"], 0)

    def test_cli_confirmed_path_passes_memory_key_to_injected_preflight(self) -> None:
        parsed = cli_module.build_parser().parse_args(
            [
                "kimi-models-preflight",
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
                "provider_id": "moonshot_kimi",
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
                "run_kimi_models_preflight",
                side_effect=fake_preflight,
            ),
            patch.object(
                cli_module.os,
                "environ",
                {"MOONSHOT_API_KEY": _KEY_CANARY},
            ),
            redirect_stdout(output),
        ):
            exit_code = cli_module.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(observed["provider_id"], "moonshot_kimi")
        self.assertEqual(observed["model_id"], _MODEL_ID)
        self.assertIsNone(observed["api_key"])
        self.assertEqual(observed["loaded_key"], _KEY_CANARY)
        self.assertIs(observed["confirm_online"], True)
        self.assertNotIn(_KEY_CANARY, output.getvalue())

    def test_cli_invalid_model_denies_before_environment_key_lookup(self) -> None:
        class ForbiddenEnvironment(dict[str, str]):
            def get(self, key, default=None):
                if key == "MOONSHOT_API_KEY":
                    raise AssertionError("invalid model must precede Key lookup")
                return super().get(key, default)

        parsed = cli_module.build_parser().parse_args(
            [
                "kimi-models-preflight",
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
        self.assertEqual(receipt["error_code"], "kimi_preflight_model_not_allowed")
        self.assertEqual(receipt["network_calls"], 0)

    def assert_strict_receipt(self, receipt: dict[str, object]) -> None:
        self.assertEqual(tuple(receipt), KIMI_PREFLIGHT_RECEIPT_FIELDS)
        self.assertEqual(receipt["provider_id"], "moonshot_kimi")
        self.assertEqual(receipt["api_origin"], "https://api.moonshot.cn")
        self.assertEqual(receipt["model_token_calls"], 0)
        self.assertIsNone(receipt["token_usage"])
        self.assertIsNone(receipt["cost"])
        self.assertFalse(receipt["chat_completions_verified"])
        self.assertFalse(receipt["responses_api_verified"])
        self.assertFalse(receipt["tool_calling_verified"])
        self.assertFalse(receipt["usage_semantics_verified"])
        self.assertFalse(receipt["model_quality_claim_allowed"])
        self.assertFalse(receipt["authorizes_model_run"])
        self.assertFalse(receipt["authorizes_provider_registration"])
        self.assertNotIn(_KEY_CANARY, repr(receipt))

    def test_offline_contract_is_fixed_non_authorizing_and_does_not_network(
        self,
    ) -> None:
        contract = kimi_models_preflight_contract()
        self.assertEqual(
            contract["implementation_status"],
            "implemented_offline_tested_not_run",
        )
        self.assertEqual(contract["provider"]["provider_id"], "moonshot_kimi")
        self.assertEqual(
            contract["provider"]["api_origin"], "https://api.moonshot.cn"
        )
        self.assertEqual(contract["provider"]["allowed_model_ids"], [_MODEL_ID])
        self.assertEqual(
            contract["provider"]["api_key_environment_variable"],
            "MOONSHOT_API_KEY",
        )
        self.assertEqual(contract["request_contract"]["method"], "GET")
        self.assertEqual(contract["request_contract"]["path"], "/v1/models")
        self.assertEqual(contract["request_contract"]["model_token_calls"], 0)
        self.assertEqual(
            contract["response_contract"]["success_media_type"],
            "application/json",
        )
        self.assertFalse(
            contract["response_contract"]
            ["missing_or_non_json_success_media_type_allowed"]
        )
        self.assertFalse(contract["runtime_controls"]["trust_env"])
        self.assertFalse(contract["runtime_controls"]["follow_redirects"])
        self.assertEqual(contract["runtime_controls"]["http_attempts_max"], 1)
        self.assertEqual(contract["runtime_controls"]["provider_managed_retries"], 0)
        self.assertFalse(contract["runtime_controls"]["fallbacks_allowed"])
        self.assertFalse(
            contract["runtime_controls"]["offline_test_transport_factory_single_use"]
        )
        self.assertEqual(
            contract["runtime_controls"]["max_decoded_body_bytes"], 64 * 1024
        )
        self.assertFalse(contract["receipt_contract"]["authorizes_model_run"])
        self.assertFalse(
            contract["receipt_contract"]["authorizes_provider_registration"]
        )
        self.assertFalse(contract["capability_boundary"]["responses_api_documented"])
        self.assertTrue(contract["entry_points"]["models_preflight_cli_enabled"])
        self.assertFalse(contract["evaluation_boundary"]["online_calls_performed"])
        self.assertEqual(
            contract["evaluation_boundary"]["offline_test_network_calls"], 0
        )

    def test_machine_contract_matches_runtime_without_network(self) -> None:
        snapshot = json.loads(
            (
                _ROOT / "evals" / "v2" / "kimi_models_preflight_contract.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot, kimi_models_preflight_contract())

    def test_default_call_is_not_run_and_never_loads_key_or_transport(self) -> None:
        def forbidden_key_loader() -> str:
            self.fail("unconfirmed preflight must not load a Key")

        receipt = asyncio.run(
            run_kimi_models_preflight(
                _key_loader=forbidden_key_loader,
                _transport_factory=lambda: self.fail(
                    "unconfirmed preflight must not construct transport"
                ),
            )
        )
        self.assert_strict_receipt(receipt)
        self.assertEqual(receipt["status"], "not_run")
        self.assertEqual(
            receipt["error_code"], "kimi_preflight_confirmation_required"
        )
        self.assertEqual(receipt["network_calls"], 0)

    def test_provider_and_exact_model_gates_precede_key_lookup(self) -> None:
        def forbidden_key_loader() -> str:
            self.fail("invalid local identity must precede Key lookup")

        cases = (
            (
                {"provider_id": "moonshot", "model_id": _MODEL_ID},
                _MODEL_ID,
                "kimi_preflight_configuration_invalid",
            ),
            (
                {"provider_id": "moonshot_kimi", "model_id": "kimi-k3-latest"},
                None,
                "kimi_preflight_model_not_allowed",
            ),
            (
                {"provider_id": "moonshot_kimi", "model_id": f" {_MODEL_ID}"},
                None,
                "kimi_preflight_model_not_allowed",
            ),
            (
                {"provider_id": "moonshot_kimi", "model_id": None},
                None,
                "kimi_preflight_model_not_allowed",
            ),
        )
        for values, expected_model, expected_code in cases:
            with self.subTest(values=values):
                receipt = asyncio.run(
                    run_kimi_models_preflight(
                        **values,
                        api_key=None,
                        confirm_online=True,
                        _key_loader=forbidden_key_loader,
                        _transport_factory=lambda: self.fail(
                            "invalid local identity must not construct transport"
                        ),
                    )
                )
                self.assertEqual(receipt["status"], "not_run")
                self.assertEqual(receipt["requested_model_id"], expected_model)
                self.assertEqual(receipt["error_code"], expected_code)
                self.assertEqual(receipt["network_calls"], 0)

    def test_key_is_loaded_only_after_gates_and_never_returned(self) -> None:
        loaded = 0

        def key_loader() -> str:
            nonlocal loaded
            loaded += 1
            return _KEY_CANARY

        receipt, transport = _run_with_handler(
            lambda request: httpx.Response(
                200, content=_json_body(_list_payload())
            ),
            api_key=None,
            _key_loader=key_loader,
        )
        self.assertEqual(loaded, 1)
        self.assertTrue(transport.closed)
        self.assertEqual(receipt["status"], "verified")
        self.assertNotIn(_KEY_CANARY, repr(receipt))

    def test_direct_key_and_loader_combination_is_denied_locally(self) -> None:
        receipt = asyncio.run(
            run_kimi_models_preflight(
                provider_id="moonshot_kimi",
                model_id=_MODEL_ID,
                api_key=_KEY_CANARY,
                confirm_online=True,
                _key_loader=lambda: _KEY_CANARY,
                _transport_factory=lambda: self.fail(
                    "ambiguous key source must not construct transport"
                ),
            )
        )
        self.assertEqual(receipt["status"], "not_run")
        self.assertEqual(
            receipt["error_code"], "kimi_preflight_configuration_invalid"
        )

    def test_missing_and_unsafe_keys_are_rejected_without_transport(self) -> None:
        cases = (
            (None, "kimi_preflight_key_missing"),
            ("", "kimi_preflight_key_missing"),
            ("   ", "kimi_preflight_key_missing"),
            (" key", "kimi_preflight_key_invalid"),
            ("key\nvalue", "kimi_preflight_key_invalid"),
            ("x" * 513, "kimi_preflight_key_invalid"),
        )
        for api_key, expected_code in cases:
            with self.subTest(api_key_length=len(api_key or "")):
                receipt = asyncio.run(
                    run_kimi_models_preflight(
                        provider_id="moonshot_kimi",
                        model_id=_MODEL_ID,
                        api_key=api_key,
                        confirm_online=True,
                        _transport_factory=lambda: self.fail(
                            "invalid Key must not construct transport"
                        ),
                    )
                )
                self.assertEqual(receipt["status"], "not_run")
                self.assertEqual(receipt["error_code"], expected_code)
                self.assertEqual(receipt["http_attempts"], 0)

    def test_request_is_one_fixed_bodyless_china_models_get(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            self.assertEqual(request.method, "GET")
            self.assertEqual(str(request.url), "https://api.moonshot.cn/v1/models")
            self.assertEqual(request.url.scheme, "https")
            self.assertEqual(request.url.host, "api.moonshot.cn")
            self.assertEqual(request.url.path, "/v1/models")
            self.assertEqual(request.url.query, b"")
            self.assertEqual(request.content, b"")
            self.assertEqual(
                request.headers["authorization"], f"Bearer {_KEY_CANARY}"
            )
            self.assertEqual(request.headers["accept"], "application/json")
            self.assertEqual(request.headers["accept-encoding"], "identity")
            self.assertEqual(
                request.headers["user-agent"],
                "researchops-agent/0.2.0 kimi-models-preflight/1.0",
            )
            self.assertNotIn("content-type", request.headers)
            self.assertNotIn("x-api-key", request.headers)
            return httpx.Response(200, content=_json_body(_list_payload()))

        receipt, transport = _run_with_handler(handler)
        self.assertTrue(transport.closed)
        self.assertEqual(calls, 1)
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["http_attempts"], 1)
        self.assertEqual(receipt["network_calls"], 1)

    def test_success_receipt_is_strict_redacted_and_non_authorizing(self) -> None:
        provider_canary = "UNKNOWN-PROVIDER-FIELD-CANARY"

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"msh-request-id": _RAW_REQUEST_ID},
                content=_json_body(
                    _list_payload(
                        entries=[
                            _model_entry(extra_provider_field=provider_canary),
                            {
                                "id": "kimi-k2.6",
                                "object": "model",
                                "unknown": provider_canary,
                            },
                        ],
                        provider_page_field=provider_canary,
                    )
                ),
            )

        receipt, transport = _run_with_handler(handler)
        self.assertTrue(transport.closed)
        self.assert_strict_receipt(receipt)
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["requested_model_id"], _MODEL_ID)
        self.assertEqual(receipt["returned_model_id"], _MODEL_ID)
        self.assertEqual(receipt["http_status"], 200)
        self.assertGreaterEqual(receipt["latency_ms"], 0)
        self.assertIs(receipt["models_api_authenticated"], True)
        self.assertIs(receipt["exact_model_visible"], True)
        self.assertEqual(
            receipt["request_id_sha256"],
            hashlib.sha256(_RAW_REQUEST_ID.encode()).hexdigest(),
        )
        self.assertIsNone(receipt["error_code"])
        serialized = repr(receipt)
        self.assertNotIn(_RAW_REQUEST_ID, serialized)
        self.assertNotIn(provider_canary, serialized)
        self.assertNotIn("context_length", serialized)

    def test_success_does_not_mutate_candidate_campaign_pilot_or_private_state(self) -> None:
        protected = (
            _ROOT / "evals" / "v2" / "campaign.json",
            _ROOT / "evals" / "v2" / "public_regression_candidate_v4.json",
            _ROOT / "evals" / "v2" / "public_regression_candidate_v5.json",
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
            _ROOT / "evals" / "v2" / "private_holdout_kit" / "protocol.json",
        )
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in protected
        }

        receipt, _ = _run_with_handler(
            lambda request: httpx.Response(
                200,
                content=_json_body(_list_payload()),
            )
        )

        self.assertEqual(receipt["status"], "verified")
        self.assertFalse(receipt["authorizes_model_run"])
        self.assertFalse(receipt["authorizes_provider_registration"])
        self.assertEqual(
            before,
            {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in protected
            },
        )

    def test_missing_exact_model_is_authenticated_but_not_visible(self) -> None:
        receipt, _ = _run_with_handler(
            lambda request: httpx.Response(
                200,
                content=_json_body(
                    _list_payload(
                        entries=[
                            {"id": "kimi-k2.6", "object": "model"},
                            {
                                "id": "kimi-k2.7-code",
                                "object": "model",
                            },
                        ]
                    )
                ),
            )
        )
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error_code"], "kimi_preflight_model_not_visible")
        self.assertIs(receipt["models_api_authenticated"], True)
        self.assertIs(receipt["exact_model_visible"], False)
        self.assertIsNone(receipt["returned_model_id"])

    def test_only_complete_moonshot_owned_exact_model_succeeds(self) -> None:
        invalid_entries: tuple[tuple[object, str], ...] = (
            ({**_model_entry(), "owned_by": "third-party"}, "identity"),
            ({**_model_entry(), "id": "kimi-k3-latest"}, "not_visible"),
            ({**_model_entry(), "object": "model_alias"}, "invalid"),
            ({key: value for key, value in _model_entry().items() if key != "created"}, "invalid"),
            ({key: value for key, value in _model_entry().items() if key != "owned_by"}, "invalid"),
            ({key: value for key, value in _model_entry().items() if key != "context_length"}, "invalid"),
            ({**_model_entry(), "created": True}, "invalid"),
            ({**_model_entry(), "context_length": 0}, "invalid"),
            ({**_model_entry(), "supports_reasoning": 1}, "invalid"),
            ([], "invalid"),
        )
        for entry, expected_class in invalid_entries:
            with self.subTest(expected_class=expected_class):
                receipt, _ = _run_with_handler(
                    lambda request, entry=entry: httpx.Response(
                        200,
                        content=_json_body(_list_payload(entries=[entry])),
                    )
                )
                expected_code = {
                    "identity": "kimi_preflight_identity_mismatch",
                    "not_visible": "kimi_preflight_model_not_visible",
                    "invalid": "kimi_preflight_response_invalid",
                }[expected_class]
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(receipt["error_code"], expected_code)
                expected_authenticated = (
                    True if expected_class == "not_visible" else None
                )
                self.assertIs(
                    receipt["models_api_authenticated"], expected_authenticated
                )
                expected_visible = False if expected_class == "not_visible" else None
                self.assertIs(receipt["exact_model_visible"], expected_visible)

    def test_malformed_duplicate_and_nonfinite_json_are_rejected(self) -> None:
        bodies = (
            b"{not-json",
            b'{"object":"list","object":"list","data":[]}',
            b'{"object":"list","data":[],"unknown":NaN}',
            b"\xff\xfe\xfd",
            b"[]",
        )
        for body in bodies:
            with self.subTest(body=body):
                receipt, _ = _run_with_handler(
                    lambda request, body=body: httpx.Response(200, content=body)
                )
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(
                    receipt["error_code"], "kimi_preflight_response_invalid"
                )
                self.assertIsNone(receipt["models_api_authenticated"])

    def test_duplicate_model_ids_are_rejected(self) -> None:
        receipt, _ = _run_with_handler(
            lambda request: httpx.Response(
                200,
                content=_json_body(
                    _list_payload(entries=[_model_entry(), _model_entry()])
                ),
            )
        )
        self.assertEqual(receipt["error_code"], "kimi_preflight_response_invalid")
        self.assertIsNone(receipt["returned_model_id"])
        self.assertIsNone(receipt["models_api_authenticated"])

    def test_decoded_body_limit_accepts_64k_and_aborts_at_byte_65537(self) -> None:
        payload = _list_payload(padding="")
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
            oversized_receipt["error_code"], "kimi_preflight_response_invalid"
        )
        self.assertIsNone(oversized_receipt["models_api_authenticated"])
        self.assertEqual(oversized_stream.yielded, 2)
        self.assertTrue(oversized_stream.closed)

    def test_nonidentity_success_encoding_is_rejected_before_body_read(self) -> None:
        stream = _ChunkStream([b"MUST-NOT-BE-READ"])
        receipt, _ = _run_with_handler(
            lambda request: httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                stream=stream,
            )
        )
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error_code"], "kimi_preflight_response_invalid")
        self.assertIsNone(receipt["models_api_authenticated"])
        self.assertEqual(stream.yielded, 0)
        self.assertTrue(stream.closed)

    def test_non_json_success_media_type_is_rejected_before_body_read(self) -> None:
        for content_type in ("text/plain", "text/html; charset=utf-8", ""):
            with self.subTest(content_type=content_type):
                stream = _ChunkStream([b"MUST-NOT-BE-READ"])
                receipt, _ = _run_with_handler(
                    lambda request, stream=stream, content_type=content_type: httpx.Response(
                        200,
                        headers={"content-type": content_type},
                        stream=stream,
                    )
                )
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(
                    receipt["error_code"], "kimi_preflight_response_invalid"
                )
                self.assertIsNone(receipt["models_api_authenticated"])
                self.assertEqual(stream.yielded, 0)
                self.assertTrue(stream.closed)

    def test_401_403_404_are_distinct_and_error_bodies_are_not_read(self) -> None:
        cases = {
            401: "kimi_preflight_auth_failed",
            403: "kimi_preflight_permission_denied",
            404: "kimi_preflight_resource_not_found",
        }
        for status, expected_code in cases.items():
            with self.subTest(status=status):
                stream = _ChunkStream([f"provider {_KEY_CANARY}".encode()])
                receipt, transport = _run_with_handler(
                    lambda request, status=status, stream=stream: httpx.Response(
                        status,
                        headers={"msh-request-id": _RAW_REQUEST_ID},
                        stream=stream,
                    )
                )
                self.assertTrue(transport.closed)
                self.assertEqual(receipt["error_code"], expected_code)
                self.assertEqual(receipt["http_status"], status)
                self.assertEqual(receipt["http_attempts"], 1)
                self.assertEqual(receipt["network_calls"], 1)
                self.assertEqual(stream.yielded, 0)
                self.assertTrue(stream.closed)
                expected_auth = False if status == 401 else None
                self.assertIs(receipt["models_api_authenticated"], expected_auth)
                self.assertNotIn(_KEY_CANARY, repr(receipt))

    def test_429_documented_error_types_are_distinguished_once(self) -> None:
        cases = {
            "engine_overloaded_error": "kimi_preflight_engine_overloaded",
            "exceeded_current_quota_error": "kimi_preflight_quota_exceeded",
            "rate_limit_reached_error": "kimi_preflight_rate_limited",
        }
        for error_type, expected_code in cases.items():
            with self.subTest(error_type=error_type):
                calls = 0
                body = _json_body(
                    {
                        "error": {
                            "type": error_type,
                            "message": f"provider detail {_KEY_CANARY}",
                        }
                    }
                )
                stream = _ChunkStream([body])

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal calls
                    calls += 1
                    return httpx.Response(429, stream=stream)

                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    receipt, transport = _run_with_handler(handler)
                self.assertTrue(transport.closed)
                self.assertEqual(calls, 1)
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(receipt["error_code"], expected_code)
                self.assertEqual(receipt["http_status"], 429)
                self.assertEqual(receipt["http_attempts"], 1)
                self.assertEqual(receipt["network_calls"], 1)
                self.assertEqual(stream.yielded, 1)
                self.assertTrue(stream.closed)
                self.assertNotIn(error_type, repr(receipt))
                self.assertNotIn(_KEY_CANARY, repr(receipt))
                self.assertNotIn(_KEY_CANARY, stdout.getvalue())
                self.assertNotIn(_KEY_CANARY, stderr.getvalue())

    def test_unknown_malformed_or_oversized_429_is_generic_and_redacted(self) -> None:
        bodies = (
            _json_body({"error": {"type": "future_rate_type"}}),
            b"<html>rate limited</html>",
            _json_body({"error": "not-an-object"}),
            b"x" * (64 * 1024 + 1),
        )
        for body in bodies:
            with self.subTest(body_length=len(body)):
                receipt, _ = _run_with_handler(
                    lambda request, body=body: httpx.Response(429, content=body)
                )
                self.assertEqual(
                    receipt["error_code"],
                    "kimi_preflight_rate_or_quota_limited",
                )
                self.assertEqual(receipt["http_attempts"], 1)

    def test_html_504_is_classified_without_reading_or_recording_body(self) -> None:
        html_canary = f"<html>gateway timeout {_KEY_CANARY}</html>".encode()
        stream = _ChunkStream([html_canary])
        receipt, transport = _run_with_handler(
            lambda request: httpx.Response(
                504,
                headers={"content-type": "text/html"},
                stream=stream,
            )
        )
        self.assertTrue(transport.closed)
        self.assertEqual(receipt["error_code"], "kimi_preflight_provider_timeout")
        self.assertEqual(receipt["http_status"], 504)
        self.assertEqual(stream.yielded, 0)
        self.assertTrue(stream.closed)
        self.assertNotIn(_KEY_CANARY, repr(receipt))
        self.assertNotIn("gateway timeout", repr(receipt))

    def test_other_http_taxonomy_is_stable_and_never_retried(self) -> None:
        cases = {
            301: "kimi_preflight_redirect_denied",
            307: "kimi_preflight_redirect_denied",
            400: "kimi_preflight_invalid_request",
            402: "kimi_preflight_billing_blocked",
            408: "kimi_preflight_provider_timeout",
            409: "kimi_preflight_conflict",
            413: "kimi_preflight_protocol_failed",
            418: "kimi_preflight_failed",
            499: "kimi_preflight_client_closed_request",
            500: "kimi_preflight_provider_unavailable",
            502: "kimi_preflight_provider_unavailable",
            503: "kimi_preflight_provider_unavailable",
            529: "kimi_preflight_provider_unavailable",
        }
        for status, expected_code in cases.items():
            with self.subTest(status=status):
                calls = 0
                stream = _ChunkStream([b"MUST-NOT-BE-READ"])

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal calls
                    calls += 1
                    return httpx.Response(
                        status,
                        headers={
                            "location": "https://evil.invalid/v1/models",
                            "msh-request-id": _RAW_REQUEST_ID,
                        },
                        stream=stream,
                    )

                receipt, transport = _run_with_handler(handler)
                self.assertTrue(transport.closed)
                self.assertEqual(calls, 1)
                self.assertEqual(receipt["error_code"], expected_code)
                self.assertEqual(receipt["http_attempts"], 1)
                self.assertEqual(receipt["network_calls"], 1)
                self.assertEqual(stream.yielded, 0)
                self.assertTrue(stream.closed)

    def test_timeout_network_and_unknown_exceptions_are_redacted_and_not_retried(
        self,
    ) -> None:
        cases = (
            (
                lambda request: httpx.ReadTimeout(
                    f"timeout {_KEY_CANARY}", request=request
                ),
                "kimi_preflight_timeout",
            ),
            (
                lambda request: httpx.ConnectError(
                    f"dns or tls {_KEY_CANARY}", request=request
                ),
                "kimi_preflight_network_failed",
            ),
            (
                lambda request: RuntimeError(f"unknown {_KEY_CANARY}"),
                "kimi_preflight_failed",
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
                self.assertEqual(receipt["error_code"], expected_code)
                self.assertEqual(receipt["http_attempts"], 1)
                self.assertEqual(receipt["network_calls"], 1)
                self.assertNotIn(_KEY_CANARY, repr(receipt))
                self.assertNotIn(_KEY_CANARY, stdout.getvalue())
                self.assertNotIn(_KEY_CANARY, stderr.getvalue())

    def test_unsafe_or_key_related_request_id_is_not_hashed(self) -> None:
        cases = (
            (_KEY_CANARY, _KEY_CANARY),
            (f"prefix-{_KEY_CANARY}-suffix", _KEY_CANARY),
            ("raw request id with spaces", _KEY_CANARY),
            ("short-id", "prefix-short-id-suffix"),
        )
        for request_id, api_key in cases:
            with self.subTest(request_id=request_id):
                receipt, _ = _run_with_handler(
                    lambda request, request_id=request_id: httpx.Response(
                        200,
                        headers={"msh-request-id": request_id},
                        content=_json_body(_list_payload()),
                    ),
                    api_key=api_key,
                )
                self.assertEqual(receipt["status"], "verified")
                self.assertIsNone(receipt["request_id_sha256"])
                self.assertNotIn(api_key, repr(receipt))
                self.assertNotIn(
                    hashlib.sha256(api_key.encode()).hexdigest(), repr(receipt)
                )

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
                run_kimi_models_preflight(
                    provider_id="moonshot_kimi",
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
            lambda request: httpx.Response(
                200, content=_json_body(_list_payload())
            )
        )
        success_receipt = asyncio.run(
            run_kimi_models_preflight(
                provider_id="moonshot_kimi",
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
            "kimi_preflight_client_close_failed",
        )

        failure_transport = _CloseFailingTransport(
            lambda request: httpx.Response(401, content=b"not read")
        )
        failure_receipt = asyncio.run(
            run_kimi_models_preflight(
                provider_id="moonshot_kimi",
                model_id=_MODEL_ID,
                api_key=_KEY_CANARY,
                confirm_online=True,
                _transport_factory=lambda: failure_transport,
            )
        )
        self.assertTrue(failure_transport.closed)
        self.assertEqual(
            failure_receipt["error_code"], "kimi_preflight_auth_failed"
        )

    def test_hanging_client_close_is_bounded_and_fails_success(self) -> None:
        transport = _HangingCloseTransport(
            lambda request: httpx.Response(
                200, content=_json_body(_list_payload())
            )
        )
        with patch("researchops.kimi_preflight._CLOSE_TIMEOUT_SECONDS", 0.01):
            receipt = asyncio.run(
                run_kimi_models_preflight(
                    provider_id="moonshot_kimi",
                    model_id=_MODEL_ID,
                    api_key=_KEY_CANARY,
                    confirm_online=True,
                    _transport_factory=lambda: transport,
                )
            )
        self.assertTrue(transport.closed)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(
            receipt["error_code"], "kimi_preflight_client_close_failed"
        )

    def test_client_construction_failure_closes_owned_mock_without_attempt(
        self,
    ) -> None:
        transport = _TrackingMockTransport(
            lambda request: self.fail("request must not be attempted")
        )
        with patch(
            "researchops.kimi_preflight._build_client",
            side_effect=RuntimeError(f"constructor {_KEY_CANARY}"),
        ):
            receipt = asyncio.run(
                run_kimi_models_preflight(
                    provider_id="moonshot_kimi",
                    model_id=_MODEL_ID,
                    api_key=_KEY_CANARY,
                    confirm_online=True,
                    _transport_factory=lambda: transport,
                )
            )
        self.assertTrue(transport.closed)
        self.assertEqual(receipt["status"], "not_run")
        self.assertEqual(receipt["error_code"], "kimi_preflight_failed")
        self.assertEqual(receipt["network_calls"], 0)
        self.assertNotIn(_KEY_CANARY, repr(receipt))

    def test_default_transport_configuration_is_tls_verified_isolated_and_zero_retry(
        self,
    ) -> None:
        mock_transport = _TrackingMockTransport(
            lambda request: self.fail("configuration test must not request")
        )
        with patch.object(
            preflight_module.httpx,
            "AsyncHTTPTransport",
            return_value=mock_transport,
        ) as constructor:
            result = preflight_module._default_transport_factory()
        self.assertIs(result, mock_transport)
        kwargs = constructor.call_args.kwargs
        self.assertFalse(kwargs["trust_env"])
        self.assertEqual(kwargs["retries"], 0)
        tls_context = kwargs["verify"]
        self.assertEqual(tls_context.verify_mode, 2)
        self.assertTrue(tls_context.check_hostname)
        self.assertIsNone(tls_context.keylog_filename)
        self.assertGreater(len(tls_context.get_ca_certs()), 0)
        asyncio.run(mock_transport.aclose())


if __name__ == "__main__":
    unittest.main()
