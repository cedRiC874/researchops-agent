from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from researchops import kimi_controlled_pilot as pilot_module
from researchops.kimi_controlled_pilot import (
    ARTIFACT_SUBDIRECTORY,
    KimiPricingAttestation,
    KimiTermsAttestation,
    RECEIPT_FIELDS,
    KimiControlledPilotError,
    build_kimi_pricing_attestation,
    build_kimi_terms_attestation,
    controlled_pilot_contract_sha256,
    create_kimi_pilot_capability,
    kimi_controlled_pilot_contract,
    load_kimi_pricing_attestation,
    load_kimi_terms_attestation,
    _run_kimi_controlled_pilot_for_test as run_kimi_controlled_pilot,
    verify_kimi_controlled_pilot_artifacts,
)
from researchops.kimi_controlled_pilot import run_kimi_controlled_pilot as production_run


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
TERMS_EXPIRY = datetime(2026, 8, 30, 16, 0, tzinfo=timezone.utc)
KEY_CANARY = "offline-kimi-controlled-key-CANARY"
AUTHORIZATION_ID = "AUTH-KIMI-SYNTH-0001"
TERMS_TEXT_HASHES = {
    "service_agreement": hashlib.sha256(b"service-agreement").hexdigest(),
    "privacy_policy": hashlib.sha256(b"privacy-policy").hexdigest(),
    "payment_agreement": hashlib.sha256(b"payment-agreement").hexdigest(),
}
PRICING_SOURCE_HASH = hashlib.sha256(b"official-kimi-k3-pricing").hexdigest()
PRICING_SOURCE_BYTES = 4096


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _event(value: object) -> bytes:
    return b"data: " + _json_bytes(value) + b"\n\n"


def _chunk(
    index: int,
    *,
    delta: dict[str, object],
    finish_reason: str | None = None,
) -> dict[str, object]:
    return {
        "id": f"cmpl-controlled-{index}",
        "object": "chat.completion.chunk",
        "created": 1_787_702_400,
        "model": "kimi-k3",
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def _usage_chunk(
    index: int,
    *,
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
) -> dict[str, object]:
    return {
        "id": f"cmpl-controlled-{index}",
        "object": "chat.completion.chunk",
        "created": 1_787_702_400,
        "model": "kimi-k3",
        "choices": [],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cached_tokens": 50,
        },
    }


def _stop_stream(
    index: int,
    *,
    include_usage: bool = True,
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
) -> bytes:
    values = [
        _event(
            _chunk(
                index,
                delta={
                    "role": "assistant",
                    "reasoning_content": "RAW-REASONING-CANARY",
                    "content": "RAW-OUTPUT-CANARY",
                },
                finish_reason="stop",
            )
        )
    ]
    if include_usage:
        values.append(
            _event(
                _usage_chunk(
                    index,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            )
        )
    values.append(b"data: [DONE]\n\n")
    return b"".join(values)


def _tool_stream(index: int, *, call_id: str, arguments: str) -> bytes:
    return b"".join(
        (
            _event(
                _chunk(
                    index,
                    delta={
                        "role": "assistant",
                        "reasoning_content": "RAW-REASONING-CANARY",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": "lookup_synthetic_metric",
                                    "arguments": arguments,
                                },
                            }
                        ],
                    },
                    finish_reason="tool_calls",
                )
            ),
            _event(_usage_chunk(index)),
            b"data: [DONE]\n\n",
        )
    )


def _copy_contract_root(directory: str) -> Path:
    root = Path(directory)
    target = root / "evals" / "v2"
    target.mkdir(parents=True)
    for name in (
        "kimi_controlled_pilot_contract.json",
        "kimi_controlled_pilot_scenarios_v1.json",
        "kimi_terms_g2a_provisional_contract.json",
    ):
        shutil.copyfile(ROOT / "evals" / "v2" / name, target / name)
    return root


def _attestation(
    root: Path,
    authorization_id: str = AUTHORIZATION_ID,
    *,
    retrieved_at_utc: datetime | None = None,
    contract_sha256: str | None = None,
    canonical_text_sha256: dict[str, str] | None = None,
) -> KimiTermsAttestation:
    return build_kimi_terms_attestation(
        authorization_id=authorization_id,
        contract_sha256=(
            controlled_pilot_contract_sha256(root)
            if contract_sha256 is None
            else contract_sha256
        ),
        retrieved_at_utc=(
            NOW - timedelta(minutes=5)
            if retrieved_at_utc is None
            else retrieved_at_utc
        ),
        canonical_text_sha256=(
            TERMS_TEXT_HASHES
            if canonical_text_sha256 is None
            else canonical_text_sha256
        ),
    )


def _capability(
    root: Path,
    authorization_id: str = AUTHORIZATION_ID,
    *,
    contract_sha256: str | None = None,
    terms_attestation: KimiTermsAttestation | None = None,
    pricing_attestation: KimiPricingAttestation | None = None,
    expires_at_utc: datetime | None = None,
):
    bound_contract_sha256 = (
        controlled_pilot_contract_sha256(root)
        if contract_sha256 is None
        else contract_sha256
    )
    bound_terms_attestation = (
        _attestation(
            root,
            authorization_id,
            contract_sha256=bound_contract_sha256,
        )
        if terms_attestation is None
        else terms_attestation
    )
    bound_pricing_attestation = (
        build_kimi_pricing_attestation(
            authorization_id=authorization_id,
            contract_sha256=bound_contract_sha256,
            terms_attestation_sha256=bound_terms_attestation.attestation_sha256,
            retrieved_at_utc=NOW - timedelta(minutes=5),
            canonical_source_sha256=PRICING_SOURCE_HASH,
            canonical_source_bytes=PRICING_SOURCE_BYTES,
        )
        if pricing_attestation is None
        else pricing_attestation
    )
    return create_kimi_pilot_capability(
        authorization_id=authorization_id,
        contract_sha256=bound_contract_sha256,
        expires_at_utc=(
            NOW + timedelta(hours=1)
            if expires_at_utc is None
            else expires_at_utc
        ),
        terms_attestation=bound_terms_attestation,
        pricing_attestation=bound_pricing_attestation,
    )


class _SuccessfulHandler:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.raw_bodies: list[bytes] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        self.raw_bodies.append(request.content)
        self.headers.append(dict(request.headers))
        index = len(self.requests)
        if index == 1:
            body = _tool_stream(
                index,
                call_id="call_scenario_1",
                arguments=(
                    '{"dataset_id":"kimi_synth_success_v1",'
                    '"metric_id":"effect_size"}'
                ),
            )
            return _sse_response(request, body)
        if index == 2:
            return _sse_response(request, _stop_stream(index))
        if index == 3:
            body = _tool_stream(
                index,
                call_id="call_scenario_2",
                arguments=(
                    '{"dataset_id":"kimi_synth_missing_v1",'
                    '"metric_id":"effect_size"}'
                ),
            )
            return _sse_response(request, body)
        if index == 4:
            return _sse_response(request, _stop_stream(index))
        if index == 5:
            self.assert_probe_request(payload, request.content)
            return httpx.Response(
                400,
                headers={"content-type": "application/json"},
                content=_json_bytes(
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "message": "RAW-PROVIDER-BODY-CANARY",
                        }
                    }
                ),
                request=request,
            )
        raise AssertionError("orchestrator retried or exceeded the state machine")

    @staticmethod
    def assert_probe_request(payload: dict[str, object], body: bytes) -> None:
        if body != pilot_module.KIMI_INVALID_REQUEST_PROBE_BODY:
            raise AssertionError("invalid-request probe body drift")
        if len(body) != 124 or hashlib.sha256(body).hexdigest() != (
            "b07a395baa11d449dcb58666363e56daa60a3686edb27bf4e57eb1d8cfa76cd7"
        ):
            raise AssertionError("invalid-request probe commitment drift")
        if "messages" in payload or "tools" in payload:
            raise AssertionError("invalid-request probe gained prompt or tools")


def _sse_response(request: httpx.Request, body: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        headers={
            "content-type": "text/event-stream",
            "msh-request-id": "request_controlled_001",
        },
        content=body,
        request=request,
    )


class KimiControlledPilotTests(unittest.TestCase):
    def test_machine_contract_is_exact_and_all_claims_false(self) -> None:
        snapshot = json.loads(
            (ROOT / "evals/v2/kimi_controlled_pilot_contract.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(snapshot, kimi_controlled_pilot_contract())
        self.assertEqual(
            snapshot["caps"],
            {
                "scenario_count": 3,
                "model_requests": 8,
                "concurrency": 1,
                "client_retries": 0,
                "input_tokens_per_request": 8000,
                "input_tokens_total": 40000,
                "output_tokens_per_request": 1536,
                "output_tokens_total": 10000,
                "tool_executions": 6,
                "request_timeout_seconds": 90,
                "run_timeout_seconds": 600,
                "local_estimated_reservation_limit_cny": "5.000000",
                "fallbacks_allowed": False,
                "request_body_bytes": 6144,
            },
        )
        self.assertEqual(snapshot["receipt"]["fields"], list(RECEIPT_FIELDS))
        self.assertEqual(
            snapshot["authorization"]["window_bound"],
            "min_terms_policy_capability_terms_attestation_and_pricing_expiry",
        )
        self.assertTrue(
            snapshot["state_machine"][
                "all_time_bounds_rechecked_before_each_request_and_terminal"
            ]
        )
        self.assertIs(
            snapshot["evaluation_boundary"]["synthetic_only"], True
        )
        self.assertEqual(
            snapshot["evaluation_boundary"]["local_cost_claim_scope"],
            "local_conservative_hard_stop_only",
        )
        for name, value in snapshot["evaluation_boundary"].items():
            if name not in {"synthetic_only", "local_cost_claim_scope"}:
                self.assertIs(value, False)
        production_parameters = inspect.signature(production_run).parameters
        self.assertNotIn("_now", production_parameters)
        self.assertNotIn("_transport_factory", production_parameters)
        with self.assertRaises(ValueError):
            asyncio.run(
                pilot_module._run_kimi_controlled_pilot_for_test(
                    ROOT,
                    confirm_online=False,
                    capability=None,
                    _key_loader=None,
                    _transport_factory=None,
                    _now=lambda: NOW,
                )
            )

    def test_full_terms_attestation_load_and_binding_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_contract_root(directory)
            contract_sha256 = controlled_pilot_contract_sha256(root)
            attestation = _attestation(root)
            payload = attestation.to_dict()
            path = root / "terms-attestation.json"
            path.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            loaded = load_kimi_terms_attestation(
                path,
                authorization_id=AUTHORIZATION_ID,
                contract_sha256=contract_sha256,
            )
            self.assertEqual(loaded.to_dict(), payload)

            mutations = {
                "missing": lambda item: item.pop("canonicalization_version"),
                "service_url": lambda item: item["source_urls"].__setitem__(
                    "service_agreement", "https://example.invalid/terms"
                ),
                "updated_date": lambda item: item[
                    "displayed_updated_dates"
                ].__setitem__("privacy_policy", "2026-08-23"),
                "effective_date": lambda item: item[
                    "displayed_effective_dates"
                ].__setitem__("payment_agreement", "2026-08-30"),
                "same_dates_changed_hash": lambda item: item[
                    "canonical_text_sha256"
                ].__setitem__("service_agreement", "f" * 64),
                "unbound": lambda item: item.__setitem__(
                    "authorization_binding_sha256", "0" * 64
                ),
                "canonicalization": lambda item: item.__setitem__(
                    "canonicalization_version", "unknown"
                ),
                "review_status": lambda item: item.__setitem__(
                    "material_delta_review_status", "material_delta"
                ),
            }
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    changed = json.loads(json.dumps(payload))
                    mutate(changed)
                    with self.assertRaises(ValueError):
                        load_kimi_terms_attestation(
                            changed,
                            authorization_id=AUTHORIZATION_ID,
                            contract_sha256=contract_sha256,
                        )
            with self.assertRaises(ValueError):
                load_kimi_terms_attestation(
                    payload,
                    authorization_id="AUTH-KIMI-DIFFERENT-01",
                    contract_sha256=contract_sha256,
                )

    def test_fresh_pricing_attestation_is_exact_and_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_contract_root(directory)
            contract_sha256 = controlled_pilot_contract_sha256(root)
            terms = _attestation(root)
            pricing = build_kimi_pricing_attestation(
                authorization_id=AUTHORIZATION_ID,
                contract_sha256=contract_sha256,
                terms_attestation_sha256=terms.attestation_sha256,
                retrieved_at_utc=NOW - timedelta(minutes=5),
                canonical_source_sha256=PRICING_SOURCE_HASH,
                canonical_source_bytes=PRICING_SOURCE_BYTES,
            )
            payload = pricing.to_dict()
            loaded = load_kimi_pricing_attestation(
                payload,
                authorization_id=AUTHORIZATION_ID,
                contract_sha256=contract_sha256,
                terms_attestation_sha256=terms.attestation_sha256,
            )
            self.assertEqual(loaded.to_dict(), payload)
            self.assertEqual(payload["model_id"], "kimi-k3")
            self.assertEqual(payload["currency"], "CNY")
            self.assertEqual(payload["billing_unit_tokens"], 1_000_000)
            self.assertEqual(payload["cached_input_cny_per_million"], "2.000000")
            self.assertEqual(payload["uncached_input_cny_per_million"], "20.000000")
            self.assertEqual(payload["output_cny_per_million"], "100.000000")
            mutations = {
                "url": lambda item: item.__setitem__(
                    "source_url", "https://example.invalid/pricing"
                ),
                "hash": lambda item: item.__setitem__(
                    "canonical_source_sha256", "f" * 64
                ),
                "bytes": lambda item: item.__setitem__(
                    "canonical_source_bytes", 0
                ),
                "model": lambda item: item.__setitem__("model_id", "kimi-k3-alias"),
                "rate": lambda item: item.__setitem__(
                    "uncached_input_cny_per_million", "19.000000"
                ),
                "binding": lambda item: item.__setitem__(
                    "authorization_binding_sha256", "0" * 64
                ),
                "unknown": lambda item: item.__setitem__("extra", True),
            }
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    changed = json.loads(json.dumps(payload))
                    mutate(changed)
                    with self.assertRaises(ValueError):
                        load_kimi_pricing_attestation(
                            changed,
                            authorization_id=AUTHORIZATION_ID,
                            contract_sha256=contract_sha256,
                            terms_attestation_sha256=terms.attestation_sha256,
                        )
            with self.assertRaises(ValueError):
                load_kimi_pricing_attestation(
                    payload,
                    authorization_id=AUTHORIZATION_ID,
                    contract_sha256=contract_sha256,
                    terms_attestation_sha256="f" * 64,
                )

    def test_success_runs_five_attempts_and_writes_strict_sanitized_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_contract_root(directory)
            handler = _SuccessfulHandler()
            key_reads = 0

            def key_loader() -> str:
                nonlocal key_reads
                key_reads += 1
                return KEY_CANARY

            clock_values = iter(
                (
                    NOW,
                    NOW + timedelta(milliseconds=100),
                    NOW + timedelta(milliseconds=200),
                    NOW + timedelta(milliseconds=300),
                    NOW + timedelta(milliseconds=400),
                    NOW + timedelta(milliseconds=500),
                    NOW + timedelta(seconds=2),
                )
            )
            receipt = asyncio.run(
                run_kimi_controlled_pilot(
                    root,
                    confirm_online=True,
                    capability=_capability(root),
                    _key_loader=key_loader,
                    _transport_factory=lambda: httpx.MockTransport(handler),
                    _now=lambda: next(clock_values),
                )
            )

            self.assertEqual(tuple(receipt), RECEIPT_FIELDS)
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(receipt["scenarios_completed"], 3)
            self.assertEqual(receipt["model_request_count"], 5)
            self.assertEqual(receipt["network_attempts"], 5)
            self.assertEqual(receipt["network_calls"], 5)
            self.assertEqual(receipt["requested_tool_call_count"], 2)
            self.assertEqual(receipt["deduplicated_tool_call_count"], 0)
            self.assertEqual(receipt["executed_tool_call_count"], 2)
            self.assertEqual(receipt["expected_invalid_request_count"], 1)
            self.assertEqual(receipt["input_tokens"], 400)
            self.assertEqual(receipt["output_tokens"], 80)
            self.assertEqual(receipt["reserved_input_tokens"], 40000)
            self.assertEqual(receipt["reserved_output_tokens"], 7680)
            self.assertEqual(receipt["usage_observed_request_count"], 4)
            self.assertIs(receipt["usage_complete"], True)
            self.assertEqual(
                receipt["reserved_estimated_cost_cny"], "1.568000"
            )
            self.assertEqual(receipt["usage_based_estimated_cost_cny"], "0.016000")
            self.assertIsNone(receipt["actual_billed_cost_cny"])
            self.assertEqual(
                receipt["local_cost_claim_scope"],
                "local_conservative_hard_stop_only",
            )
            self.assertEqual(
                receipt["terms_attestation"]["displayed_updated_dates"],
                {
                    "service_agreement": "2026-08-24",
                    "privacy_policy": "2026-08-24",
                    "payment_agreement": "2026-08-24",
                },
            )
            self.assertEqual(
                receipt["terms_attestation"]["displayed_effective_dates"],
                {
                    "service_agreement": "2026-08-31",
                    "privacy_policy": "2026-08-31",
                    "payment_agreement": "2026-08-31",
                },
            )
            self.assertEqual(receipt["terms_attestation_max_age_seconds"], 3600)
            self.assertEqual(
                receipt["terms_attestation_expires_at_utc"],
                "2026-08-27T08:55:00.000Z",
            )
            self.assertEqual(receipt["pricing_attestation_max_age_seconds"], 3600)
            self.assertEqual(
                receipt["pricing_attestation"]["source_url"],
                "https://platform.kimi.com/docs/pricing/chat-k3",
            )
            self.assertEqual(
                receipt["pricing_attestation"]["canonical_source_sha256"],
                PRICING_SOURCE_HASH,
            )
            self.assertEqual(
                receipt["pricing_attestation"]["uncached_input_cny_per_million"],
                "20.000000",
            )
            self.assertEqual(
                receipt["pricing_attestation"]["output_cny_per_million"],
                "100.000000",
            )
            self.assertLess(receipt["checked_at_utc"], receipt["completed_at_utc"])
            self.assertIs(receipt["outcome_unknown"], False)
            self.assertIsNone(receipt["error_code"])
            for field in RECEIPT_FIELDS:
                if field.startswith("authorizes_"):
                    self.assertIs(receipt[field], False)
            self.assertIs(receipt["candidate_result_created"], False)
            self.assertEqual(key_reads, 1)
            self.assertEqual(len(handler.requests), 5)
            self.assertEqual(
                [item["tool_choice"] for item in handler.requests[:4]],
                [
                    "required",
                    "none",
                    "required",
                    "none",
                ],
            )
            expected_keys = {
                "max_completion_tokens",
                "messages",
                "model",
                "reasoning_effort",
                "stream",
                "stream_options",
                "tool_choice",
                "tools",
            }
            for payload in handler.requests[:4]:
                self.assertEqual(set(payload), expected_keys)
                self.assertEqual(payload["model"], "kimi-k3")
                self.assertEqual(payload["max_completion_tokens"], 1536)
                self.assertEqual(payload["reasoning_effort"], "low")
                self.assertIs(payload["stream"], True)
                self.assertEqual(payload["stream_options"], {"include_usage": True})
                self.assertLessEqual(len(_json_bytes(payload)), 6144)
            self.assertEqual(
                set(handler.requests[4]),
                {
                    "max_completion_tokens",
                    "model",
                    "reasoning_effort",
                    "stream",
                    "stream_options",
                },
            )
            self.assertEqual(handler.requests[4]["max_completion_tokens"], 1)
            self.assertNotIn("messages", handler.requests[4])
            self.assertNotIn("tools", handler.requests[4])
            self.assertNotIn(KEY_CANARY, repr(receipt))

            verified = verify_kimi_controlled_pilot_artifacts(
                root, authorization_id=AUTHORIZATION_ID
            )
            self.assertEqual(verified["status"], "valid")
            self.assertEqual(verified["event_count"], receipt["event_count"])
            auth_hash = hashlib.sha256(AUTHORIZATION_ID.encode()).hexdigest()
            run_dir = root / ARTIFACT_SUBDIRECTORY / auth_hash
            self.assertEqual(
                sorted(path.name for path in run_dir.iterdir()),
                ["checkpoint.json", "event_chain.jsonl", "receipt.json"],
            )
            serialized = "".join(
                path.read_text(encoding="utf-8") for path in run_dir.iterdir()
            )
            events = [
                json.loads(line)
                for line in (run_dir / "event_chain.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            expected_completed = 0
            for event in events:
                if event["event_type"] == "scenario_completed":
                    expected_completed += 1
                self.assertEqual(event["scenarios_completed"], expected_completed)
                self.assertEqual(
                    event["terms_attestation_sha256"],
                    receipt["terms_attestation_sha256"],
                )
                self.assertEqual(
                    event["terms_attestation_expires_at_utc"],
                    receipt["terms_attestation_expires_at_utc"],
                )
                self.assertEqual(
                    event["pricing_attestation_sha256"],
                    receipt["pricing_attestation_sha256"],
                )
            for forbidden in (
                KEY_CANARY,
                AUTHORIZATION_ID,
                "RAW-REASONING-CANARY",
                "RAW-OUTPUT-CANARY",
                "RAW-PROVIDER-BODY-CANARY",
                "call_scenario_1",
                "kimi_synth_success_v1",
                "effect_size",
                "@",
                str(root),
            ):
                self.assertNotIn(forbidden, serialized)

    def test_unconfirmed_wrong_contract_and_expired_terms_never_read_key_or_call(self) -> None:
        cases = (
            "unconfirmed",
            "wrong_contract",
            "wrong_capability",
            "stale_attestation",
            "stale_pricing",
            "changed_hash_unbound",
            "changed_pricing_unbound",
            "expired_terms",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = _copy_contract_root(directory)
                capability = _capability(root)
                if case == "wrong_capability":
                    capability = _capability(
                        root, contract_sha256="0" * 64
                    )
                elif case == "stale_attestation":
                    capability = _capability(
                        root,
                        terms_attestation=_attestation(
                            root,
                            retrieved_at_utc=NOW - timedelta(seconds=3601),
                        ),
                    )
                elif case == "changed_hash_unbound":
                    valid = _attestation(root)
                    changed_hashes = dict(TERMS_TEXT_HASHES)
                    changed_hashes["service_agreement"] = "f" * 64
                    capability = _capability(
                        root,
                        terms_attestation=KimiTermsAttestation(
                            retrieved_at_utc=valid.retrieved_at_utc,
                            canonical_text_sha256=changed_hashes,
                            canonicalization_version="kimi-terms-canonical-v1",
                            material_delta_review_status=(
                                "no_material_or_unclassifiable_delta_observed"
                            ),
                            authorization_binding_sha256=(
                                valid.authorization_binding_sha256
                            ),
                        ),
                    )
                elif case in {"stale_pricing", "changed_pricing_unbound"}:
                    terms = _attestation(root)
                    valid_pricing = build_kimi_pricing_attestation(
                        authorization_id=AUTHORIZATION_ID,
                        contract_sha256=controlled_pilot_contract_sha256(root),
                        terms_attestation_sha256=terms.attestation_sha256,
                        retrieved_at_utc=(
                            NOW - timedelta(seconds=3601)
                            if case == "stale_pricing"
                            else NOW - timedelta(minutes=5)
                        ),
                        canonical_source_sha256=PRICING_SOURCE_HASH,
                        canonical_source_bytes=PRICING_SOURCE_BYTES,
                    )
                    if case == "changed_pricing_unbound":
                        valid_pricing = KimiPricingAttestation(
                            retrieved_at_utc=valid_pricing.retrieved_at_utc,
                            canonical_source_sha256="f" * 64,
                            canonical_source_bytes=PRICING_SOURCE_BYTES,
                            canonicalization_version="kimi-pricing-canonical-v1",
                            material_delta_review_status=(
                                "official_k3_rates_rechecked_no_material_delta"
                            ),
                            authorization_binding_sha256=(
                                valid_pricing.authorization_binding_sha256
                            ),
                        )
                    capability = _capability(
                        root,
                        terms_attestation=terms,
                        pricing_attestation=valid_pricing,
                    )
                if case == "wrong_contract":
                    path = root / "evals/v2/kimi_controlled_pilot_contract.json"
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload["caps"]["model_requests"] = 9
                    path.write_text(json.dumps(payload), encoding="utf-8")
                key_reads = 0
                calls = 0

                def key_loader() -> str:
                    nonlocal key_reads
                    key_reads += 1
                    return KEY_CANARY

                def transport_factory():
                    nonlocal calls
                    calls += 1
                    return httpx.MockTransport(lambda request: _sse_response(request, b""))

                receipt = asyncio.run(
                    run_kimi_controlled_pilot(
                        root,
                        confirm_online=case != "unconfirmed",
                        capability=capability,
                        _key_loader=key_loader,
                        _transport_factory=transport_factory,
                        _now=(lambda: TERMS_EXPIRY) if case == "expired_terms" else (lambda: NOW),
                    )
                )
                self.assertEqual(receipt["status"], "not_run")
                self.assertEqual(receipt["network_calls"], 0)
                self.assertEqual(receipt["model_request_count"], 0)
                self.assertEqual(key_reads, 0)
                self.assertEqual(calls, 0)
                self.assertFalse((root / "artifacts").exists())

    def test_capability_is_single_use_and_run_is_not_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_contract_root(directory)
            handler = _SuccessfulHandler()
            capability = _capability(root)
            first = asyncio.run(
                run_kimi_controlled_pilot(
                    root,
                    confirm_online=True,
                    capability=capability,
                    _key_loader=lambda: KEY_CANARY,
                    _transport_factory=lambda: httpx.MockTransport(handler),
                    _now=lambda: NOW,
                )
            )
            self.assertEqual(first["status"], "completed")
            key_reads = 0

            def key_loader() -> str:
                nonlocal key_reads
                key_reads += 1
                return KEY_CANARY

            second = asyncio.run(
                run_kimi_controlled_pilot(
                    root,
                    confirm_online=True,
                    capability=capability,
                    _key_loader=key_loader,
                    _transport_factory=lambda: self.fail("must not call transport"),
                    _now=lambda: NOW,
                )
            )
            self.assertEqual(second["status"], "not_run")
            self.assertEqual(second["error_code"], "kimi_pilot_capability_consumed")
            self.assertEqual(key_reads, 0)

            replacement = _capability(root)
            third = asyncio.run(
                run_kimi_controlled_pilot(
                    root,
                    confirm_online=True,
                    capability=replacement,
                    _key_loader=key_loader,
                    _transport_factory=lambda: self.fail("must not call transport"),
                    _now=lambda: NOW,
                )
            )
            self.assertEqual(third["error_code"], "kimi_pilot_resume_not_supported")
            self.assertEqual(key_reads, 0)

    def test_full_run_window_is_reserved_before_key_or_request(self) -> None:
        cases = ("terms", "capability", "terms_attestation", "pricing")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = _copy_contract_root(directory)
                if case == "terms":
                    start = TERMS_EXPIRY - timedelta(seconds=1)
                    authorization_id = "AUTH-KIMI-TERMS-WINDOW-01"
                    attestation = _attestation(
                        root,
                        authorization_id,
                        retrieved_at_utc=start - timedelta(minutes=5),
                    )
                    expires_at = start + timedelta(hours=1)
                    expected_error = "kimi_pilot_terms_window_insufficient"
                    pricing = build_kimi_pricing_attestation(
                        authorization_id=authorization_id,
                        contract_sha256=controlled_pilot_contract_sha256(root),
                        terms_attestation_sha256=attestation.attestation_sha256,
                        retrieved_at_utc=start - timedelta(minutes=5),
                        canonical_source_sha256=PRICING_SOURCE_HASH,
                        canonical_source_bytes=PRICING_SOURCE_BYTES,
                    )
                elif case == "capability":
                    start = NOW
                    authorization_id = "AUTH-KIMI-CAP-WINDOW-01"
                    attestation = _attestation(root, authorization_id)
                    expires_at = start + timedelta(seconds=599)
                    expected_error = "kimi_pilot_capability_window_insufficient"
                    pricing = None
                elif case == "terms_attestation":
                    start = NOW
                    authorization_id = "AUTH-KIMI-TERMS-ATTEST-WINDOW-01"
                    attestation = _attestation(
                        root,
                        authorization_id,
                        retrieved_at_utc=start - timedelta(seconds=3599),
                    )
                    expires_at = start + timedelta(hours=1)
                    pricing = None
                    expected_error = (
                        "kimi_pilot_terms_attestation_window_insufficient"
                    )
                else:
                    start = NOW
                    authorization_id = "AUTH-KIMI-PRICE-WINDOW-01"
                    attestation = _attestation(root, authorization_id)
                    expires_at = start + timedelta(hours=1)
                    pricing = build_kimi_pricing_attestation(
                        authorization_id=authorization_id,
                        contract_sha256=controlled_pilot_contract_sha256(root),
                        terms_attestation_sha256=attestation.attestation_sha256,
                        retrieved_at_utc=start - timedelta(seconds=3001),
                        canonical_source_sha256=PRICING_SOURCE_HASH,
                        canonical_source_bytes=PRICING_SOURCE_BYTES,
                    )
                    expected_error = "kimi_pilot_pricing_window_insufficient"
                key_reads = 0
                transport_calls = 0

                def key_loader() -> str:
                    nonlocal key_reads
                    key_reads += 1
                    return KEY_CANARY

                def transport_factory():
                    nonlocal transport_calls
                    transport_calls += 1
                    return httpx.MockTransport(
                        lambda request: self.fail("window-denied request sent")
                    )

                receipt = asyncio.run(
                    run_kimi_controlled_pilot(
                        root,
                        confirm_online=True,
                        capability=_capability(
                            root,
                            authorization_id,
                            terms_attestation=attestation,
                            pricing_attestation=pricing,
                            expires_at_utc=expires_at,
                        ),
                        _key_loader=key_loader,
                        _transport_factory=transport_factory,
                        _now=lambda: start,
                    )
                )
                self.assertEqual(receipt["status"], "not_run")
                self.assertEqual(receipt["error_code"], expected_error)
                self.assertEqual(receipt["model_request_count"], 0)
                self.assertEqual(receipt["network_attempts"], 0)
                self.assertEqual(receipt["network_calls"], 0)
                self.assertEqual(key_reads, 0)
                self.assertEqual(transport_calls, 0)
                self.assertFalse((root / "artifacts").exists())

    def test_terminal_clock_crossing_capability_expiry_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_contract_root(directory)
            handler = _SuccessfulHandler()
            expires_at = NOW + timedelta(seconds=600)
            clock_values = iter(
                (
                    NOW,
                    NOW,
                    NOW,
                    NOW,
                    NOW,
                    NOW,
                    NOW + timedelta(seconds=601),
                    NOW + timedelta(seconds=601),
                )
            )
            receipt = asyncio.run(
                run_kimi_controlled_pilot(
                    root,
                    confirm_online=True,
                    capability=_capability(
                        root, expires_at_utc=expires_at
                    ),
                    _key_loader=lambda: KEY_CANARY,
                    _transport_factory=lambda: httpx.MockTransport(handler),
                    _now=lambda: next(clock_values),
                )
            )
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["error_code"], "kimi_pilot_capability_expired")
            self.assertEqual(receipt["network_attempts"], 5)
            self.assertEqual(receipt["network_calls"], 5)
            with self.assertRaises(KimiControlledPilotError):
                verify_kimi_controlled_pilot_artifacts(
                    root, authorization_id=AUTHORIZATION_ID
                )

    def test_terminal_clock_crossing_terms_attestation_expiry_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_contract_root(directory)
            handler = _SuccessfulHandler()
            terms = _attestation(
                root, retrieved_at_utc=NOW - timedelta(seconds=3000)
            )
            clock_values = iter(
                (
                    NOW,
                    NOW,
                    NOW,
                    NOW,
                    NOW,
                    NOW,
                    NOW + timedelta(seconds=601),
                    NOW + timedelta(seconds=601),
                )
            )
            receipt = asyncio.run(
                run_kimi_controlled_pilot(
                    root,
                    confirm_online=True,
                    capability=_capability(root, terms_attestation=terms),
                    _key_loader=lambda: KEY_CANARY,
                    _transport_factory=lambda: httpx.MockTransport(handler),
                    _now=lambda: next(clock_values),
                )
            )
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(
                receipt["error_code"], "kimi_pilot_terms_attestation_stale"
            )
            self.assertEqual(receipt["network_attempts"], 5)
            self.assertEqual(receipt["network_calls"], 5)
            with self.assertRaises(KimiControlledPilotError):
                verify_kimi_controlled_pilot_artifacts(
                    root, authorization_id=AUTHORIZATION_ID
                )

    def test_usage_missing_and_usage_over_limit_stop_without_retry(self) -> None:
        for case in ("missing", "over_input", "over_output"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = _copy_contract_root(directory)
                calls = 0

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal calls
                    calls += 1
                    if case == "missing":
                        body = _stop_stream(1, include_usage=False)
                    elif case == "over_input":
                        body = _stop_stream(1, prompt_tokens=8001)
                    else:
                        body = _stop_stream(1, completion_tokens=1537)
                    return _sse_response(request, body)

                receipt = asyncio.run(
                    run_kimi_controlled_pilot(
                        root,
                        confirm_online=True,
                        capability=_capability(root, f"AUTH-KIMI-{case.upper()}-01"),
                        _key_loader=lambda: KEY_CANARY,
                        _transport_factory=lambda: httpx.MockTransport(handler),
                        _now=lambda: NOW,
                    )
                )
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(receipt["model_request_count"], 1)
                self.assertEqual(calls, 1)
                expected = (
                    "kimi_chat_usage_missing"
                    if case == "missing"
                    else "kimi_pilot_usage_limit_exceeded"
                )
                self.assertEqual(receipt["error_code"], expected)

    def test_token_reservation_plus_one_boundaries_fail_before_send(self) -> None:
        counters = pilot_module._Counters()
        for _ in range(5):
            counters.reserve_request()
        self.assertEqual(counters.reserved_input_tokens, 40000)
        self.assertEqual(counters.reserved_output_tokens, 7680)
        with self.assertRaises(KimiControlledPilotError) as caught:
            counters.reserve_request()
        self.assertEqual(
            caught.exception.code, "kimi_pilot_token_reservation_limit"
        )
        self.assertEqual(counters.model_requests, 5)

        output_boundary = pilot_module._Counters()
        output_boundary.reserved_output_tokens = 10000
        with self.assertRaises(KimiControlledPilotError) as output_caught:
            output_boundary.reserve_request()
        self.assertEqual(
            output_caught.exception.code,
            "kimi_pilot_token_reservation_limit",
        )
        self.assertEqual(output_boundary.model_requests, 0)

    def test_wrong_tool_count_or_scenario_arguments_stop_before_second_request(self) -> None:
        cases = ("two_calls", "wrong_arguments")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = _copy_contract_root(directory)
                calls = 0

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal calls
                    calls += 1
                    if case == "wrong_arguments":
                        arguments = (
                            '{"dataset_id":"kimi_synth_missing_v1",'
                            '"metric_id":"effect_size"}'
                        )
                        return _sse_response(
                            request,
                            _tool_stream(
                                1,
                                call_id="call_wrong_arguments",
                                arguments=arguments,
                            ),
                        )
                    arguments = (
                        '{"dataset_id":"kimi_synth_success_v1",'
                        '"metric_id":"effect_size"}'
                    )
                    first_call = {
                        "index": 0,
                        "id": "call_first",
                        "type": "function",
                        "function": {
                            "name": "lookup_synthetic_metric",
                            "arguments": arguments,
                        },
                    }
                    second_call = {
                        "index": 1,
                        "id": "call_second",
                        "type": "function",
                        "function": {
                            "name": "lookup_synthetic_metric",
                            "arguments": arguments,
                        },
                    }
                    body = b"".join(
                        (
                            _event(
                                _chunk(
                                    1,
                                    delta={
                                        "role": "assistant",
                                        "tool_calls": [first_call, second_call],
                                    },
                                    finish_reason="tool_calls",
                                )
                            ),
                            _event(_usage_chunk(1)),
                            b"data: [DONE]\n\n",
                        )
                    )
                    return _sse_response(request, body)

                receipt = asyncio.run(
                    run_kimi_controlled_pilot(
                        root,
                        confirm_online=True,
                        capability=_capability(root, f"AUTH-KIMI-WRONG-{case}"),
                        _key_loader=lambda: KEY_CANARY,
                        _transport_factory=lambda: httpx.MockTransport(handler),
                        _now=lambda: NOW,
                    )
                )
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(receipt["model_request_count"], 1)
                self.assertEqual(calls, 1)
                expected = (
                    "kimi_pilot_tool_response_invalid"
                    if case == "two_calls"
                    else "kimi_pilot_scenario_arguments_mismatch"
                )
                self.assertEqual(receipt["error_code"], expected)

    def test_cancel_and_timeout_are_terminal_outcome_unknown(self) -> None:
        async def hanging_handler(request: httpx.Request) -> httpx.Response:
            del request
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        with tempfile.TemporaryDirectory() as directory:
            root = _copy_contract_root(directory)
            authorization_id = "AUTH-KIMI-CANCEL-01"

            async def cancel_run():
                task = asyncio.create_task(
                    run_kimi_controlled_pilot(
                        root,
                        confirm_online=True,
                        capability=_capability(root, authorization_id),
                        _key_loader=lambda: KEY_CANARY,
                        _transport_factory=lambda: httpx.MockTransport(hanging_handler),
                        _now=lambda: NOW,
                    )
                )
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            asyncio.run(cancel_run())
            auth_hash = hashlib.sha256(authorization_id.encode()).hexdigest()
            receipt = json.loads(
                (
                    root
                    / ARTIFACT_SUBDIRECTORY
                    / auth_hash
                    / "receipt.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["status"], "outcome_unknown")
            self.assertIs(receipt["outcome_unknown"], True)
            self.assertEqual(receipt["error_code"], "kimi_pilot_cancelled")
            self.assertEqual(receipt["network_attempts"], 1)

        with tempfile.TemporaryDirectory() as directory:
            root = _copy_contract_root(directory)
            with patch(
                "researchops.kimi_chat_transport._REQUEST_DEADLINE_SECONDS",
                0.01,
            ):
                receipt = asyncio.run(
                    run_kimi_controlled_pilot(
                        root,
                        confirm_online=True,
                        capability=_capability(root, "AUTH-KIMI-TIMEOUT-01"),
                        _key_loader=lambda: KEY_CANARY,
                        _transport_factory=lambda: httpx.MockTransport(hanging_handler),
                        _now=lambda: NOW,
                    )
                )
            self.assertEqual(receipt["status"], "outcome_unknown")
            self.assertIs(receipt["outcome_unknown"], True)
            self.assertEqual(receipt["error_code"], "kimi_chat_timeout")
            self.assertEqual(receipt["network_attempts"], 1)

    def test_process_wide_concurrency_denial_precedes_key_and_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_contract_root(directory)

            async def exercise() -> None:
                started = asyncio.Event()

                async def hanging_handler(request: httpx.Request) -> httpx.Response:
                    del request
                    started.set()
                    await asyncio.Event().wait()
                    raise AssertionError("unreachable")

                first = asyncio.create_task(
                    run_kimi_controlled_pilot(
                        root,
                        confirm_online=True,
                        capability=_capability(root, "AUTH-KIMI-CONCURRENT-01"),
                        _key_loader=lambda: KEY_CANARY,
                        _transport_factory=lambda: httpx.MockTransport(hanging_handler),
                        _now=lambda: NOW,
                    )
                )
                await started.wait()
                second_key_reads = 0
                second_transport_calls = 0

                def second_key_loader() -> str:
                    nonlocal second_key_reads
                    second_key_reads += 1
                    return KEY_CANARY

                def second_transport_factory():
                    nonlocal second_transport_calls
                    second_transport_calls += 1
                    return httpx.MockTransport(
                        lambda request: self.fail("concurrent transport called")
                    )

                second = await run_kimi_controlled_pilot(
                    root,
                    confirm_online=True,
                    capability=_capability(root, "AUTH-KIMI-CONCURRENT-02"),
                    _key_loader=second_key_loader,
                    _transport_factory=second_transport_factory,
                    _now=lambda: NOW,
                )
                self.assertEqual(second["status"], "not_run")
                self.assertEqual(
                    second["error_code"], "kimi_pilot_concurrency_denied"
                )
                self.assertEqual(second["model_request_count"], 0)
                self.assertEqual(second["network_calls"], 0)
                self.assertEqual(second_key_reads, 0)
                self.assertEqual(second_transport_calls, 0)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first

            asyncio.run(exercise())

    def test_initial_artifact_io_failure_is_normalized_and_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_contract_root(directory)
            with patch.object(pilot_module._Journal, "create", side_effect=OSError):
                failed = asyncio.run(
                    run_kimi_controlled_pilot(
                        root,
                        confirm_online=True,
                        capability=_capability(root, "AUTH-KIMI-FS-FAIL-01"),
                        _key_loader=lambda: self.fail("must not read key"),
                        _transport_factory=lambda: self.fail("must not connect"),
                        _now=lambda: NOW,
                    )
                )
            self.assertEqual(failed["status"], "not_run")
            self.assertEqual(
                failed["error_code"], "kimi_pilot_artifact_io_failed"
            )
            after = asyncio.run(
                run_kimi_controlled_pilot(
                    root,
                    confirm_online=False,
                    capability=None,
                    _key_loader=lambda: self.fail("must not read key"),
                    _transport_factory=lambda: self.fail("must not connect"),
                    _now=lambda: NOW,
                )
            )
            self.assertEqual(after["error_code"], "kimi_pilot_confirmation_required")

    def test_crash_leaves_atomic_in_flight_checkpoint_and_no_resume_receipt(self) -> None:
        class CrashSignal(BaseException):
            pass

        def crash_handler(request: httpx.Request) -> httpx.Response:
            del request
            raise CrashSignal

        with tempfile.TemporaryDirectory() as directory:
            root = _copy_contract_root(directory)
            authorization_id = "AUTH-KIMI-CRASH-01"
            with self.assertRaises(CrashSignal):
                asyncio.run(
                    run_kimi_controlled_pilot(
                        root,
                        confirm_online=True,
                        capability=_capability(root, authorization_id),
                        _key_loader=lambda: KEY_CANARY,
                        _transport_factory=lambda: httpx.MockTransport(crash_handler),
                        _now=lambda: NOW,
                    )
                )
            auth_hash = hashlib.sha256(authorization_id.encode()).hexdigest()
            run_dir = root / ARTIFACT_SUBDIRECTORY / auth_hash
            checkpoint = json.loads(
                (run_dir / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertTrue(checkpoint["state"].endswith("_in_flight"))
            self.assertIs(checkpoint["outcome_unknown"], True)
            self.assertEqual(checkpoint["network_attempts"], 1)
            self.assertFalse((run_dir / "receipt.json").exists())

    def test_hash_chain_tamper_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_contract_root(directory)
            handler = _SuccessfulHandler()
            asyncio.run(
                run_kimi_controlled_pilot(
                    root,
                    confirm_online=True,
                    capability=_capability(root),
                    _key_loader=lambda: KEY_CANARY,
                    _transport_factory=lambda: httpx.MockTransport(handler),
                    _now=lambda: NOW,
                )
            )
            auth_hash = hashlib.sha256(AUTHORIZATION_ID.encode()).hexdigest()
            path = root / ARTIFACT_SUBDIRECTORY / auth_hash / "event_chain.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            event = json.loads(lines[0])
            event["state"] = "tampered"
            lines[0] = json.dumps(event, sort_keys=True, separators=(",", ":"))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with self.assertRaises(KimiControlledPilotError) as caught:
                verify_kimi_controlled_pilot_artifacts(
                    root, authorization_id=AUTHORIZATION_ID
                )
            self.assertEqual(caught.exception.code, "kimi_pilot_hash_chain_invalid")

    def test_receipt_checkpoint_and_current_contract_tamper_are_detected(self) -> None:
        for target in (
            "receipt",
            "checkpoint",
            "contract",
            "scenarios",
            "joint_scenarios",
            "joint_attestation",
        ):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                root = _copy_contract_root(directory)
                handler = _SuccessfulHandler()
                asyncio.run(
                    run_kimi_controlled_pilot(
                        root,
                        confirm_online=True,
                        capability=_capability(root),
                        _key_loader=lambda: KEY_CANARY,
                        _transport_factory=lambda: httpx.MockTransport(handler),
                        _now=lambda: NOW,
                    )
                )
                auth_hash = hashlib.sha256(AUTHORIZATION_ID.encode()).hexdigest()
                run_dir = root / ARTIFACT_SUBDIRECTORY / auth_hash
                if target == "receipt":
                    path = run_dir / "receipt.json"
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload["authorizes_chat"] = True
                elif target == "checkpoint":
                    path = run_dir / "checkpoint.json"
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload["network_calls"] += 1
                elif target == "contract":
                    path = root / "evals/v2/kimi_controlled_pilot_contract.json"
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload["caps"]["model_requests"] = 7
                elif target == "scenarios":
                    path = root / "evals/v2/kimi_controlled_pilot_scenarios_v1.json"
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload["scenario_set_id"] += "-tampered"
                elif target == "joint_scenarios":
                    for name in ("receipt.json", "checkpoint.json"):
                        path = run_dir / name
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        payload["scenarios_completed"] = 2
                        path.write_text(
                            json.dumps(payload, sort_keys=True, separators=(",", ":"))
                            + "\n",
                            encoding="utf-8",
                        )
                    path = None
                else:
                    receipt_path = run_dir / "receipt.json"
                    checkpoint_path = run_dir / "checkpoint.json"
                    receipt_payload = json.loads(
                        receipt_path.read_text(encoding="utf-8")
                    )
                    changed_hashes = dict(TERMS_TEXT_HASHES)
                    changed_hashes["service_agreement"] = "f" * 64
                    changed_attestation = build_kimi_terms_attestation(
                        authorization_id=AUTHORIZATION_ID,
                        contract_sha256=controlled_pilot_contract_sha256(root),
                        retrieved_at_utc=datetime.fromisoformat(
                            receipt_payload["terms_attestation"][
                                "retrieved_at_utc"
                            ].replace("Z", "+00:00")
                        ),
                        canonical_text_sha256=changed_hashes,
                    )
                    for path in (receipt_path, checkpoint_path):
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        payload["terms_attestation"] = changed_attestation.to_dict()
                        payload["terms_attestation_sha256"] = (
                            changed_attestation.attestation_sha256
                        )
                        payload["terms_authorization_binding_sha256"] = (
                            changed_attestation.authorization_binding_sha256
                        )
                        path.write_text(
                            json.dumps(payload, sort_keys=True, separators=(",", ":"))
                            + "\n",
                            encoding="utf-8",
                        )
                    path = None
                if path is not None:
                    path.write_text(
                        json.dumps(payload, sort_keys=True, separators=(",", ":"))
                        + "\n",
                        encoding="utf-8",
                    )
                with self.assertRaises(KimiControlledPilotError):
                    verify_kimi_controlled_pilot_artifacts(
                        root, authorization_id=AUTHORIZATION_ID
                    )

    def test_failed_artifacts_cannot_fake_a_completed_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _copy_contract_root(directory)

            def handler(request: httpx.Request) -> httpx.Response:
                return _sse_response(
                    request,
                    _tool_stream(
                        1,
                        call_id="call_wrong_arguments",
                        arguments=(
                            '{"dataset_id":"kimi_synth_missing_v1",'
                            '"metric_id":"effect_size"}'
                        ),
                    ),
                )

            clock_values = iter(
                (
                    NOW,
                    NOW + timedelta(milliseconds=100),
                    NOW + timedelta(seconds=1),
                )
            )
            receipt = asyncio.run(
                run_kimi_controlled_pilot(
                    root,
                    confirm_online=True,
                    capability=_capability(root),
                    _key_loader=lambda: KEY_CANARY,
                    _transport_factory=lambda: httpx.MockTransport(handler),
                    _now=lambda: next(clock_values),
                )
            )
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["scenarios_completed"], 0)
            self.assertLess(receipt["checked_at_utc"], receipt["completed_at_utc"])
            self.assertEqual(
                verify_kimi_controlled_pilot_artifacts(
                    root, authorization_id=AUTHORIZATION_ID
                )["status"],
                "valid",
            )
            auth_hash = hashlib.sha256(AUTHORIZATION_ID.encode()).hexdigest()
            run_dir = root / ARTIFACT_SUBDIRECTORY / auth_hash
            for name in ("receipt.json", "checkpoint.json"):
                path = run_dir / name
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["scenarios_completed"] = 1
                path.write_text(
                    json.dumps(payload, sort_keys=True, separators=(",", ":"))
                    + "\n",
                    encoding="utf-8",
                )
            with self.assertRaises(KimiControlledPilotError):
                verify_kimi_controlled_pilot_artifacts(
                    root, authorization_id=AUTHORIZATION_ID
                )


if __name__ == "__main__":
    unittest.main()
