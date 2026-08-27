from __future__ import annotations

"""Frozen successor orchestrator for Kimi candidate v7.

This module is an additive fork of the immutable v1 orchestrator.  It binds
the documented same-chunk usage parser v2, a new contract/artifact/receipt
namespace and a runtime-supplied candidate-v7 commitment.  It deliberately
does not inherit the consumed v6 authorization or its post-lock result.
"""

import asyncio
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Final, Mapping

import httpx

from .kimi_chat_transport import (
    KIMI_INVALID_REQUEST_PROBE_BODY,
    KIMI_INVALID_REQUEST_PROBE_BODY_SHA256,
    KimiInvalidRequestProbeResult,
    run_kimi_invalid_request_probe,
)
from .kimi_chat_transport_v2 import (
    KIMI_CHAT_MODEL_ID,
    KIMI_CHAT_TRANSPORT_V2_ID,
    KimiChatRequest,
    KimiChatResponse,
    KimiChatTransportError,
    KimiFunctionTool,
    KimiTextMessage,
    KimiToolResultMessage,
    run_kimi_chat_completion_v2,
)
from .kimi_controlled_pilot import _PROCESS_RUN_LOCK
from .kimi_synthetic_tools import (
    KimiSyntheticToolExecutor,
    MAX_TOOL_EXECUTIONS,
    synthetic_tool_schema,
)


CONTRACT_ID: Final = "kimi-controlled-synthetic-pilot-v2"
RECEIPT_SCHEMA_VERSION: Final = "kimi-controlled-pilot-receipt/2.0"
EVENT_SCHEMA_VERSION: Final = "kimi-pilot-event/2.0"
CHECKPOINT_SCHEMA_VERSION: Final = "kimi-pilot-checkpoint/2.0"
AUTHORIZATION_SCHEMA_VERSION: Final = "kimi-pilot-authorization/2.0"
SUCCESSOR_TOMBSTONE_SCHEMA_VERSION: Final = (
    "kimi-pilot-successor-tombstone/2.0"
)
SUCCESSOR_TOMBSTONE_FILENAME: Final = "successor_v2_tombstone.json"
ARTIFACT_SUBDIRECTORY: Final = "artifacts/kimi_controlled_pilot_v2"
LEGACY_ARTIFACT_SUBDIRECTORY: Final = "artifacts/kimi_controlled_pilot"
KIMI_CHAT_PROVIDER_ID: Final = "moonshot_kimi"
KIMI_CHAT_TRANSPORT_ID: Final = KIMI_CHAT_TRANSPORT_V2_ID
EXPECTED_CANDIDATE_ID: Final = (
    "eval-v2-public-regression-deepseek-kimi-controlled-chat-v7"
)
PREDECESSOR_CANDIDATE_COMMITMENT_SHA256: Final = (
    "57d0c1b054367794fa08ec2dbdecdeb0c75bc4be2c45894b69db832a1f6f5641"
)
SCENARIO_COUNT: Final = 3
MODEL_REQUEST_LIMIT: Final = 8
CONCURRENCY_LIMIT: Final = 1
CLIENT_RETRY_LIMIT: Final = 0
INPUT_TOKEN_LIMIT_PER_REQUEST: Final = 8_000
INPUT_TOKEN_LIMIT_TOTAL: Final = 40_000
OUTPUT_TOKEN_LIMIT_PER_REQUEST: Final = 1_536
OUTPUT_TOKEN_LIMIT_TOTAL: Final = 10_000
TOOL_EXECUTION_LIMIT: Final = 6
REQUEST_TIMEOUT_SECONDS: Final = 90
RUN_TIMEOUT_SECONDS: Final = 600
LOCAL_ESTIMATED_RESERVATION_LIMIT_CNY: Final = Decimal("5.000000")
INPUT_PRICE_PER_MILLION_CNY: Final = Decimal("20.000000")
OUTPUT_PRICE_PER_MILLION_CNY: Final = Decimal("100.000000")
REQUEST_BODY_LIMIT_BYTES: Final = 6 * 1024
TERMS_EXPIRES_AT_UTC: Final = "2026-08-30T16:00:00Z"
TERMS_ATTESTATION_MAX_AGE_SECONDS: Final = 3600
PRICING_ATTESTATION_MAX_AGE_SECONDS: Final = 3600
PRICING_SOURCE_URL: Final = "https://platform.kimi.com/docs/pricing/chat-k3"
PRICING_CANONICALIZATION_VERSION: Final = "kimi-pricing-canonical-v1"
PRICING_REVIEW_STATUS: Final = "official_k3_rates_rechecked_no_material_delta"
PRICING_MODEL_ID: Final = "kimi-k3"
PRICING_CURRENCY: Final = "CNY"
PRICING_BILLING_UNIT_TOKENS: Final = 1_000_000
PRICING_CACHED_INPUT_PER_MILLION_CNY: Final = Decimal("2.000000")
TERMS_SERVICE_AGREEMENT_UPDATED_DATE: Final = "2026-08-24"
TERMS_PRIVACY_POLICY_UPDATED_DATE: Final = "2026-08-24"
TERMS_PAYMENT_AGREEMENT_UPDATED_DATE: Final = "2026-08-24"
TERMS_DISPLAYED_EFFECTIVE_DATE: Final = "2026-08-31"
TERMS_CANONICALIZATION_VERSION: Final = "kimi-terms-canonical-v1"
TERMS_MATERIAL_REVIEW_STATUS: Final = (
    "no_material_or_unclassifiable_delta_observed"
)
_TERMS_SOURCE_URLS: Final = {
    "service_agreement": "https://platform.kimi.com/docs/agreement/modeluse",
    "privacy_policy": "https://platform.kimi.com/docs/agreement/userprivacy",
    "payment_agreement": "https://platform.kimi.com/docs/agreement/payment",
}
_TERMS_SOURCE_IDS: Final = tuple(_TERMS_SOURCE_URLS)

_CHAT_TRANSPORT_SHA256: Final = (
    "0e62e6b696a43804ef36bd1b7c1422cb0b9d7a974544d2afe50f5b7c6e2af8ae"
)
_LEGACY_CHAT_TRANSPORT_SHA256: Final = (
    "6e4be581ebf3d11c96bec4cb4cb8d58de62c24f8b025912cc1af5a3774977279"
)
# Filled only after the independent Chat v2 contract is frozen.  This value is
# checked against both the file and every runtime authorization binding.
_CHAT_CONTRACT_SHA256: Final = (
    "eb226578df2555813fbef005e366bb014d05bbb0dfde039b170689fc5a00916c"
)
_SYNTHETIC_TOOLS_SHA256: Final = (
    "e097d5371b36afc5dc7bf5a87cd206d62092a262db4feed35d2a0f4d6b5fc130"
)
_SCENARIOS_SHA256: Final = (
    "f5f51f57f46d5b98677bb9809f28f3e211bb553fd8f4f8d873f848a9ef6f649b"
)
_TERMS_SHA256: Final = (
    "be9b3b609675b6965d81d078c6c260b75bad4b99f98276b811ac533deadfdc49"
)
_TOOL_SCHEMA_SHA256: Final = (
    "e60b7cb31f3aefbe37e417fe73659f21b38b3e3e32cd7dfa26b9e413aceb5512"
)
_CAPABILITY_TOKEN = object()
_SAFE_AUTHORIZATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_MONEY_QUANTUM = Decimal("0.000001")
_MILLION = Decimal("1000000")
_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "sequence",
        "event_type",
        "state",
        "scenario_id",
        "candidate_id",
        "candidate_commitment_sha256",
        "predecessor_candidate_commitment_sha256",
        "chat_contract_sha256",
        "legacy_successor_tombstone_sha256",
        "terms_attestation_sha256",
        "terms_authorization_binding_sha256",
        "terms_attestation_expires_at_utc",
        "pricing_attestation_sha256",
        "pricing_authorization_binding_sha256",
        "pricing_expires_at_utc",
        "authorization_schema_version",
        "authorization_expires_at_utc",
        "checked_at_utc",
        "completed_at_utc",
        "scenarios_completed",
        "model_request_count",
        "network_attempts",
        "network_calls",
        "requested_tool_call_count",
        "deduplicated_tool_call_count",
        "executed_tool_call_count",
        "expected_invalid_request_count",
        "input_tokens",
        "output_tokens",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "reserved_estimated_cost_cny",
        "usage_observed_request_count",
        "usage_complete",
        "usage_based_estimated_cost_cny",
        "outcome_unknown",
        "error_code",
        "prev_hash",
        "event_hash",
    }
)
_CHECKPOINT_FIELDS = frozenset(
    {
        "schema_version",
        "authorization_id_sha256",
        "contract_sha256",
        "candidate_id",
        "candidate_commitment_sha256",
        "predecessor_candidate_commitment_sha256",
        "chat_contract_sha256",
        "legacy_successor_tombstone_sha256",
        "terms_attestation",
        "terms_attestation_sha256",
        "terms_authorization_binding_sha256",
        "terms_attestation_max_age_seconds",
        "terms_attestation_expires_at_utc",
        "pricing_attestation",
        "pricing_attestation_sha256",
        "pricing_authorization_binding_sha256",
        "pricing_attestation_max_age_seconds",
        "pricing_expires_at_utc",
        "authorization_schema_version",
        "authorization_expires_at_utc",
        "checked_at_utc",
        "completed_at_utc",
        "state",
        "scenario_index",
        "scenarios_completed",
        "model_request_count",
        "network_attempts",
        "network_calls",
        "requested_tool_call_count",
        "deduplicated_tool_call_count",
        "executed_tool_call_count",
        "expected_invalid_request_count",
        "input_tokens",
        "output_tokens",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "reserved_estimated_cost_cny",
        "usage_observed_request_count",
        "usage_complete",
        "usage_based_estimated_cost_cny",
        "outcome_unknown",
        "error_code",
        "event_count",
        "event_chain_head_sha256",
    }
)
RECEIPT_FIELDS: Final = (
    "schema_version",
    "status",
    "contract_id",
    "contract_sha256",
    "candidate_id",
    "candidate_commitment_sha256",
    "predecessor_candidate_commitment_sha256",
    "chat_contract_sha256",
    "legacy_successor_tombstone_sha256",
    "authorization_id_sha256",
    "terms_attestation",
    "terms_attestation_sha256",
    "terms_authorization_binding_sha256",
    "terms_attestation_max_age_seconds",
    "terms_attestation_expires_at_utc",
    "pricing_attestation",
    "pricing_attestation_sha256",
    "pricing_authorization_binding_sha256",
    "pricing_attestation_max_age_seconds",
    "pricing_expires_at_utc",
    "authorization_schema_version",
    "authorization_expires_at_utc",
    "checked_at_utc",
    "completed_at_utc",
    "state",
    "scenario_count",
    "scenarios_completed",
    "model_request_count",
    "model_request_limit",
    "network_attempts",
    "network_calls",
    "requested_tool_call_count",
    "deduplicated_tool_call_count",
    "executed_tool_call_count",
    "expected_invalid_request_count",
    "input_tokens",
    "output_tokens",
    "reserved_input_tokens",
    "reserved_output_tokens",
    "usage_observed_request_count",
    "usage_complete",
    "reserved_estimated_cost_cny",
    "usage_based_estimated_cost_cny",
    "actual_billed_cost_cny",
    "local_estimated_reservation_limit_cny",
    "local_cost_claim_scope",
    "all_cache_miss_pricing",
    "outcome_unknown",
    "error_code",
    "event_count",
    "event_chain_head_sha256",
    "candidate_result_created",
    "authorizes_retry",
    "authorizes_resume",
    "authorizes_chat",
    "authorizes_tools",
    "authorizes_model_quality_claim",
    "authorizes_provider_registration",
    "authorizes_private_evaluation",
    "authorizes_non_synthetic_data",
)


class KimiControlledPilotError(RuntimeError):
    """Stable orchestrator failure with no secret or provider body."""

    def __init__(self, code: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True, slots=True)
class KimiTermsAttestation:
    retrieved_at_utc: datetime
    canonical_text_sha256: Mapping[str, str] = field(repr=False)
    canonicalization_version: str
    material_delta_review_status: str
    authorization_binding_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.retrieved_at_utc, datetime)
            or self.retrieved_at_utc.tzinfo is None
            or self.retrieved_at_utc.utcoffset() is None
        ):
            raise ValueError("Kimi terms attestation time must be timezone-aware")
        try:
            hashes = dict(self.canonical_text_sha256)
        except (TypeError, ValueError) as exc:
            raise ValueError("Kimi terms attestation hashes are invalid") from exc
        if set(hashes) != set(_TERMS_SOURCE_IDS) or any(
            type(value) is not str or _SHA256.fullmatch(value) is None
            for value in hashes.values()
        ):
            raise ValueError("Kimi terms attestation hashes are invalid")
        if self.canonicalization_version != TERMS_CANONICALIZATION_VERSION:
            raise ValueError("Kimi terms canonicalization version is invalid")
        if self.material_delta_review_status != TERMS_MATERIAL_REVIEW_STATUS:
            raise ValueError("Kimi terms delta review status is invalid")
        if (
            type(self.authorization_binding_sha256) is not str
            or _SHA256.fullmatch(self.authorization_binding_sha256) is None
        ):
            raise ValueError("Kimi terms authorization binding is invalid")
        object.__setattr__(
            self, "canonical_text_sha256", MappingProxyType(hashes)
        )

    def payload_without_binding(self) -> dict[str, Any]:
        return {
            "schema_version": "kimi-terms-attestation/1.0",
            "retrieved_at_utc": _timestamp(self.retrieved_at_utc),
            "source_urls": dict(_TERMS_SOURCE_URLS),
            "displayed_updated_dates": {
                "service_agreement": TERMS_SERVICE_AGREEMENT_UPDATED_DATE,
                "privacy_policy": TERMS_PRIVACY_POLICY_UPDATED_DATE,
                "payment_agreement": TERMS_PAYMENT_AGREEMENT_UPDATED_DATE,
            },
            "displayed_effective_dates": {
                source_id: TERMS_DISPLAYED_EFFECTIVE_DATE
                for source_id in _TERMS_SOURCE_IDS
            },
            "canonical_text_sha256": dict(self.canonical_text_sha256),
            "canonicalization_version": self.canonicalization_version,
            "material_delta_review_status": self.material_delta_review_status,
        }

    @property
    def attestation_sha256(self) -> str:
        return hashlib.sha256(
            _canonical_bytes(self.payload_without_binding())
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        result = self.payload_without_binding()
        result["authorization_binding_sha256"] = (
            self.authorization_binding_sha256
        )
        return result


@dataclass(frozen=True, slots=True)
class KimiPricingAttestation:
    """Fresh operator-reviewed facts from the fixed official K3 pricing page."""

    retrieved_at_utc: datetime
    canonical_source_sha256: str
    canonical_source_bytes: int
    canonicalization_version: str
    material_delta_review_status: str
    authorization_binding_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.retrieved_at_utc, datetime)
            or self.retrieved_at_utc.tzinfo is None
            or self.retrieved_at_utc.utcoffset() is None
        ):
            raise ValueError("Kimi pricing attestation time must be timezone-aware")
        if (
            type(self.canonical_source_sha256) is not str
            or _SHA256.fullmatch(self.canonical_source_sha256) is None
            or type(self.canonical_source_bytes) is not int
            or not 1 <= self.canonical_source_bytes <= 2_000_000
            or self.canonicalization_version
            != PRICING_CANONICALIZATION_VERSION
            or self.material_delta_review_status != PRICING_REVIEW_STATUS
            or type(self.authorization_binding_sha256) is not str
            or _SHA256.fullmatch(self.authorization_binding_sha256) is None
        ):
            raise ValueError("Kimi pricing attestation is invalid")

    def payload_without_binding(self) -> dict[str, Any]:
        return {
            "schema_version": "kimi-pricing-attestation/1.0",
            "retrieved_at_utc": _timestamp(self.retrieved_at_utc),
            "source_url": PRICING_SOURCE_URL,
            "canonical_source_sha256": self.canonical_source_sha256,
            "canonical_source_bytes": self.canonical_source_bytes,
            "canonicalization_version": self.canonicalization_version,
            "material_delta_review_status": self.material_delta_review_status,
            "model_id": PRICING_MODEL_ID,
            "currency": PRICING_CURRENCY,
            "billing_unit_tokens": PRICING_BILLING_UNIT_TOKENS,
            "cached_input_cny_per_million": _money(
                PRICING_CACHED_INPUT_PER_MILLION_CNY
            ),
            "uncached_input_cny_per_million": _money(
                INPUT_PRICE_PER_MILLION_CNY
            ),
            "output_cny_per_million": _money(OUTPUT_PRICE_PER_MILLION_CNY),
            "cache_discount_used_for_local_hard_stop": False,
            "actual_billed_cost_known": False,
            "claim_scope": "local_conservative_hard_stop_only",
        }

    @property
    def attestation_sha256(self) -> str:
        return hashlib.sha256(
            _canonical_bytes(self.payload_without_binding())
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        result = self.payload_without_binding()
        result["authorization_binding_sha256"] = self.authorization_binding_sha256
        return result


def _locked_caps_binding() -> dict[str, Any]:
    return {
        "scenario_count": SCENARIO_COUNT,
        "model_requests": MODEL_REQUEST_LIMIT,
        "concurrency": CONCURRENCY_LIMIT,
        "client_retries": CLIENT_RETRY_LIMIT,
        "input_tokens_per_request": INPUT_TOKEN_LIMIT_PER_REQUEST,
        "input_tokens_total": INPUT_TOKEN_LIMIT_TOTAL,
        "output_tokens_per_request": OUTPUT_TOKEN_LIMIT_PER_REQUEST,
        "output_tokens_total": OUTPUT_TOKEN_LIMIT_TOTAL,
        "tool_executions": TOOL_EXECUTION_LIMIT,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "run_timeout_seconds": RUN_TIMEOUT_SECONDS,
        "local_estimated_reservation_limit_cny": _money(
            LOCAL_ESTIMATED_RESERVATION_LIMIT_CNY
        ),
        "fallbacks_allowed": False,
        "request_body_bytes": REQUEST_BODY_LIMIT_BYTES,
    }


def _validate_successor_binding(
    *,
    candidate_id: str,
    candidate_commitment_sha256: str,
    chat_contract_sha256: str,
) -> None:
    if (
        candidate_id != EXPECTED_CANDIDATE_ID
        or type(candidate_commitment_sha256) is not str
        or _SHA256.fullmatch(candidate_commitment_sha256) is None
        or type(chat_contract_sha256) is not str
        or _SHA256.fullmatch(chat_contract_sha256) is None
        or chat_contract_sha256 != _CHAT_CONTRACT_SHA256
    ):
        raise ValueError("Kimi successor candidate binding is invalid")


def _canonical_authorization_expiry(value: datetime) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("Kimi authorization expiry must be timezone-aware")
    return _timestamp(value.astimezone(timezone.utc))


def _authorization_binding_sha256(
    *,
    authorization_id_sha256: str,
    contract_sha256: str,
    candidate_id: str,
    candidate_commitment_sha256: str,
    chat_contract_sha256: str,
    authorization_expires_at_utc: datetime,
    attestation_sha256: str,
) -> str:
    canonical_expiry = _canonical_authorization_expiry(
        authorization_expires_at_utc
    )
    return hashlib.sha256(
        _canonical_bytes(
            {
                "authorization_id_sha256": authorization_id_sha256,
                "pilot_contract_sha256": contract_sha256,
                "candidate_id": candidate_id,
                "candidate_commitment_sha256": candidate_commitment_sha256,
                "predecessor_candidate_commitment_sha256": (
                    PREDECESSOR_CANDIDATE_COMMITMENT_SHA256
                ),
                "chat_contract_sha256": chat_contract_sha256,
                "authorization_schema_version": AUTHORIZATION_SCHEMA_VERSION,
                "authorization_expires_at_utc": canonical_expiry,
                "locked_caps": _locked_caps_binding(),
                "terms_attestation_sha256": attestation_sha256,
            }
        )
    ).hexdigest()


def _pricing_authorization_binding_sha256(
    *,
    authorization_id_sha256: str,
    contract_sha256: str,
    candidate_id: str,
    candidate_commitment_sha256: str,
    chat_contract_sha256: str,
    authorization_expires_at_utc: datetime,
    terms_attestation_sha256: str,
    pricing_attestation_sha256: str,
) -> str:
    canonical_expiry = _canonical_authorization_expiry(
        authorization_expires_at_utc
    )
    return hashlib.sha256(
        _canonical_bytes(
            {
                "authorization_id_sha256": authorization_id_sha256,
                "pilot_contract_sha256": contract_sha256,
                "candidate_id": candidate_id,
                "candidate_commitment_sha256": candidate_commitment_sha256,
                "predecessor_candidate_commitment_sha256": (
                    PREDECESSOR_CANDIDATE_COMMITMENT_SHA256
                ),
                "chat_contract_sha256": chat_contract_sha256,
                "authorization_schema_version": AUTHORIZATION_SCHEMA_VERSION,
                "authorization_expires_at_utc": canonical_expiry,
                "locked_caps": _locked_caps_binding(),
                "terms_attestation_sha256": terms_attestation_sha256,
                "pricing_attestation_sha256": pricing_attestation_sha256,
            }
        )
    ).hexdigest()


def build_kimi_terms_attestation(
    *,
    authorization_id: str,
    contract_sha256: str,
    candidate_id: str,
    candidate_commitment_sha256: str,
    chat_contract_sha256: str,
    authorization_expires_at_utc: datetime,
    retrieved_at_utc: datetime,
    canonical_text_sha256: Mapping[str, str],
) -> KimiTermsAttestation:
    if type(contract_sha256) is not str or _SHA256.fullmatch(
        contract_sha256
    ) is None:
        raise ValueError("Kimi authorization contract hash is invalid")
    _validate_successor_binding(
        candidate_id=candidate_id,
        candidate_commitment_sha256=candidate_commitment_sha256,
        chat_contract_sha256=chat_contract_sha256,
    )
    _canonical_authorization_expiry(authorization_expires_at_utc)
    authorization_id_sha256 = _authorization_id_hash(authorization_id)
    provisional = KimiTermsAttestation(
        retrieved_at_utc=retrieved_at_utc,
        canonical_text_sha256=canonical_text_sha256,
        canonicalization_version=TERMS_CANONICALIZATION_VERSION,
        material_delta_review_status=TERMS_MATERIAL_REVIEW_STATUS,
        authorization_binding_sha256="0" * 64,
    )
    return KimiTermsAttestation(
        retrieved_at_utc=retrieved_at_utc,
        canonical_text_sha256=canonical_text_sha256,
        canonicalization_version=TERMS_CANONICALIZATION_VERSION,
        material_delta_review_status=TERMS_MATERIAL_REVIEW_STATUS,
        authorization_binding_sha256=_authorization_binding_sha256(
            authorization_id_sha256=authorization_id_sha256,
            contract_sha256=contract_sha256,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment_sha256,
            chat_contract_sha256=chat_contract_sha256,
            authorization_expires_at_utc=authorization_expires_at_utc,
            attestation_sha256=provisional.attestation_sha256,
        ),
    )


def build_kimi_pricing_attestation(
    *,
    authorization_id: str,
    contract_sha256: str,
    candidate_id: str,
    candidate_commitment_sha256: str,
    chat_contract_sha256: str,
    authorization_expires_at_utc: datetime,
    terms_attestation_sha256: str,
    retrieved_at_utc: datetime,
    canonical_source_sha256: str,
    canonical_source_bytes: int,
) -> KimiPricingAttestation:
    if (
        type(contract_sha256) is not str
        or _SHA256.fullmatch(contract_sha256) is None
        or type(terms_attestation_sha256) is not str
        or _SHA256.fullmatch(terms_attestation_sha256) is None
    ):
        raise ValueError("Kimi pricing authorization hashes are invalid")
    _validate_successor_binding(
        candidate_id=candidate_id,
        candidate_commitment_sha256=candidate_commitment_sha256,
        chat_contract_sha256=chat_contract_sha256,
    )
    _canonical_authorization_expiry(authorization_expires_at_utc)
    authorization_id_sha256 = _authorization_id_hash(authorization_id)
    provisional = KimiPricingAttestation(
        retrieved_at_utc=retrieved_at_utc,
        canonical_source_sha256=canonical_source_sha256,
        canonical_source_bytes=canonical_source_bytes,
        canonicalization_version=PRICING_CANONICALIZATION_VERSION,
        material_delta_review_status=PRICING_REVIEW_STATUS,
        authorization_binding_sha256="0" * 64,
    )
    return KimiPricingAttestation(
        retrieved_at_utc=retrieved_at_utc,
        canonical_source_sha256=canonical_source_sha256,
        canonical_source_bytes=canonical_source_bytes,
        canonicalization_version=PRICING_CANONICALIZATION_VERSION,
        material_delta_review_status=PRICING_REVIEW_STATUS,
        authorization_binding_sha256=_pricing_authorization_binding_sha256(
            authorization_id_sha256=authorization_id_sha256,
            contract_sha256=contract_sha256,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment_sha256,
            chat_contract_sha256=chat_contract_sha256,
            authorization_expires_at_utc=authorization_expires_at_utc,
            terms_attestation_sha256=terms_attestation_sha256,
            pricing_attestation_sha256=provisional.attestation_sha256,
        ),
    )


def load_kimi_terms_attestation(
    source: str | Path | Mapping[str, Any],
    *,
    authorization_id: str,
    contract_sha256: str,
    candidate_id: str,
    candidate_commitment_sha256: str,
    chat_contract_sha256: str,
    authorization_expires_at_utc: datetime,
) -> KimiTermsAttestation:
    if isinstance(source, Mapping):
        try:
            raw = dict(source)
        except Exception as exc:
            raise ValueError("Kimi terms attestation mapping is invalid") from exc
    else:
        path = Path(source)
        try:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > 64 * 1024
            ):
                raise ValueError("Kimi terms attestation file is invalid")
            raw = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except Exception as exc:
            raise ValueError("Kimi terms attestation file is invalid") from exc
    if type(contract_sha256) is not str or _SHA256.fullmatch(
        contract_sha256
    ) is None:
        raise ValueError("Kimi authorization contract hash is invalid")
    _validate_successor_binding(
        candidate_id=candidate_id,
        candidate_commitment_sha256=candidate_commitment_sha256,
        chat_contract_sha256=chat_contract_sha256,
    )
    _canonical_authorization_expiry(authorization_expires_at_utc)
    expected_fields = {
        "schema_version",
        "retrieved_at_utc",
        "source_urls",
        "displayed_updated_dates",
        "displayed_effective_dates",
        "canonical_text_sha256",
        "canonicalization_version",
        "material_delta_review_status",
        "authorization_binding_sha256",
    }
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise ValueError("Kimi terms attestation fields are invalid")
    if (
        raw["schema_version"] != "kimi-terms-attestation/1.0"
        or raw["source_urls"] != _TERMS_SOURCE_URLS
        or raw["displayed_updated_dates"]
        != {
            "service_agreement": TERMS_SERVICE_AGREEMENT_UPDATED_DATE,
            "privacy_policy": TERMS_PRIVACY_POLICY_UPDATED_DATE,
            "payment_agreement": TERMS_PAYMENT_AGREEMENT_UPDATED_DATE,
        }
        or raw["displayed_effective_dates"]
        != {
            source_id: TERMS_DISPLAYED_EFFECTIVE_DATE
            for source_id in _TERMS_SOURCE_IDS
        }
    ):
        raise ValueError("Kimi terms attestation source identity is invalid")
    try:
        retrieved = datetime.fromisoformat(
            str(raw["retrieved_at_utc"]).replace("Z", "+00:00")
        )
        attestation = KimiTermsAttestation(
            retrieved_at_utc=retrieved,
            canonical_text_sha256=raw["canonical_text_sha256"],
            canonicalization_version=raw["canonicalization_version"],
            material_delta_review_status=raw["material_delta_review_status"],
            authorization_binding_sha256=raw["authorization_binding_sha256"],
        )
    except Exception as exc:
        raise ValueError("Kimi terms attestation values are invalid") from exc
    expected_binding = _authorization_binding_sha256(
        authorization_id_sha256=_authorization_id_hash(authorization_id),
        contract_sha256=contract_sha256,
        candidate_id=candidate_id,
        candidate_commitment_sha256=candidate_commitment_sha256,
        chat_contract_sha256=chat_contract_sha256,
        authorization_expires_at_utc=authorization_expires_at_utc,
        attestation_sha256=attestation.attestation_sha256,
    )
    if attestation.authorization_binding_sha256 != expected_binding:
        raise ValueError("Kimi terms attestation is not bound to this authorization")
    return attestation


def load_kimi_pricing_attestation(
    source: str | Path | Mapping[str, Any],
    *,
    authorization_id: str,
    contract_sha256: str,
    candidate_id: str,
    candidate_commitment_sha256: str,
    chat_contract_sha256: str,
    authorization_expires_at_utc: datetime,
    terms_attestation_sha256: str,
) -> KimiPricingAttestation:
    if isinstance(source, Mapping):
        try:
            raw = dict(source)
        except Exception as exc:
            raise ValueError("Kimi pricing attestation mapping is invalid") from exc
    else:
        path = Path(source)
        try:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > 64 * 1024
            ):
                raise ValueError("Kimi pricing attestation file is invalid")
            raw = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except Exception as exc:
            raise ValueError("Kimi pricing attestation file is invalid") from exc
    expected_fields = {
        "schema_version",
        "retrieved_at_utc",
        "source_url",
        "canonical_source_sha256",
        "canonical_source_bytes",
        "canonicalization_version",
        "material_delta_review_status",
        "model_id",
        "currency",
        "billing_unit_tokens",
        "cached_input_cny_per_million",
        "uncached_input_cny_per_million",
        "output_cny_per_million",
        "cache_discount_used_for_local_hard_stop",
        "actual_billed_cost_known",
        "claim_scope",
        "authorization_binding_sha256",
    }
    _validate_successor_binding(
        candidate_id=candidate_id,
        candidate_commitment_sha256=candidate_commitment_sha256,
        chat_contract_sha256=chat_contract_sha256,
    )
    _canonical_authorization_expiry(authorization_expires_at_utc)
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise ValueError("Kimi pricing attestation fields are invalid")
    if (
        raw.get("schema_version") != "kimi-pricing-attestation/1.0"
        or raw.get("source_url") != PRICING_SOURCE_URL
        or raw.get("model_id") != PRICING_MODEL_ID
        or raw.get("currency") != PRICING_CURRENCY
        or raw.get("billing_unit_tokens") != PRICING_BILLING_UNIT_TOKENS
        or raw.get("cached_input_cny_per_million")
        != _money(PRICING_CACHED_INPUT_PER_MILLION_CNY)
        or raw.get("uncached_input_cny_per_million")
        != _money(INPUT_PRICE_PER_MILLION_CNY)
        or raw.get("output_cny_per_million")
        != _money(OUTPUT_PRICE_PER_MILLION_CNY)
        or raw.get("cache_discount_used_for_local_hard_stop") is not False
        or raw.get("actual_billed_cost_known") is not False
        or raw.get("claim_scope") != "local_conservative_hard_stop_only"
    ):
        raise ValueError("Kimi pricing attestation facts are invalid")
    try:
        retrieved = datetime.fromisoformat(
            str(raw["retrieved_at_utc"]).replace("Z", "+00:00")
        )
        attestation = KimiPricingAttestation(
            retrieved_at_utc=retrieved,
            canonical_source_sha256=raw["canonical_source_sha256"],
            canonical_source_bytes=raw["canonical_source_bytes"],
            canonicalization_version=raw["canonicalization_version"],
            material_delta_review_status=raw["material_delta_review_status"],
            authorization_binding_sha256=raw["authorization_binding_sha256"],
        )
    except Exception as exc:
        raise ValueError("Kimi pricing attestation values are invalid") from exc
    expected_binding = _pricing_authorization_binding_sha256(
        authorization_id_sha256=_authorization_id_hash(authorization_id),
        contract_sha256=contract_sha256,
        candidate_id=candidate_id,
        candidate_commitment_sha256=candidate_commitment_sha256,
        chat_contract_sha256=chat_contract_sha256,
        authorization_expires_at_utc=authorization_expires_at_utc,
        terms_attestation_sha256=terms_attestation_sha256,
        pricing_attestation_sha256=attestation.attestation_sha256,
    )
    if (
        attestation.to_dict() != raw
        or attestation.authorization_binding_sha256 != expected_binding
    ):
        raise ValueError("Kimi pricing attestation is not bound to this authorization")
    return attestation


def _authorization_id_hash(authorization_id: str) -> str:
    if (
        type(authorization_id) is not str
        or _SAFE_AUTHORIZATION_ID.fullmatch(authorization_id) is None
    ):
        raise ValueError("Kimi authorization ID is invalid")
    return hashlib.sha256(authorization_id.encode("utf-8")).hexdigest()


class KimiPilotCapability:
    """One-use, process-local authorization capsule; never serialized."""

    __slots__ = (
        "_authorization_id_sha256",
        "_candidate_commitment_sha256",
        "_candidate_id",
        "_chat_contract_sha256",
        "_consumed",
        "_contract_sha256",
        "_expires_at_utc",
        "_pricing_attestation",
        "_terms_attestation",
        "_token",
    )

    def __init__(
        self,
        *,
        authorization_id: str,
        contract_sha256: str,
        candidate_id: str,
        candidate_commitment_sha256: str,
        chat_contract_sha256: str,
        expires_at_utc: datetime,
        terms_attestation: KimiTermsAttestation,
        pricing_attestation: KimiPricingAttestation,
        _token: object,
    ) -> None:
        if (
            type(authorization_id) is not str
            or _SAFE_AUTHORIZATION_ID.fullmatch(authorization_id) is None
        ):
            raise ValueError("Kimi authorization ID is invalid")
        if type(contract_sha256) is not str or _SHA256.fullmatch(
            contract_sha256
        ) is None:
            raise ValueError("Kimi authorization contract hash is invalid")
        _validate_successor_binding(
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment_sha256,
            chat_contract_sha256=chat_contract_sha256,
        )
        if (
            not isinstance(expires_at_utc, datetime)
            or expires_at_utc.tzinfo is None
            or expires_at_utc.utcoffset() is None
        ):
            raise ValueError("Kimi authorization expiry must be timezone-aware")
        if not isinstance(terms_attestation, KimiTermsAttestation):
            raise ValueError("Kimi terms attestation is required")
        if not isinstance(pricing_attestation, KimiPricingAttestation):
            raise ValueError("Kimi pricing attestation is required")
        self._authorization_id_sha256 = hashlib.sha256(
            authorization_id.encode("utf-8")
        ).hexdigest()
        self._contract_sha256 = contract_sha256
        self._candidate_id = candidate_id
        self._candidate_commitment_sha256 = candidate_commitment_sha256
        self._chat_contract_sha256 = chat_contract_sha256
        self._expires_at_utc = expires_at_utc.astimezone(timezone.utc)
        self._terms_attestation = terms_attestation
        self._pricing_attestation = pricing_attestation
        self._consumed = False
        self._token = _token

    @property
    def authorization_id_sha256(self) -> str:
        return self._authorization_id_sha256

    @property
    def candidate_id(self) -> str:
        return self._candidate_id

    @property
    def candidate_commitment_sha256(self) -> str:
        return self._candidate_commitment_sha256

    @property
    def chat_contract_sha256(self) -> str:
        return self._chat_contract_sha256

    @property
    def expires_at_utc(self) -> datetime:
        return self._expires_at_utc

    @property
    def terms_attestation(self) -> KimiTermsAttestation:
        return self._terms_attestation

    @property
    def terms_attestation_expires_at_utc(self) -> datetime:
        return self._terms_attestation.retrieved_at_utc.astimezone(
            timezone.utc
        ) + timedelta(seconds=TERMS_ATTESTATION_MAX_AGE_SECONDS)

    @property
    def pricing_attestation(self) -> KimiPricingAttestation:
        return self._pricing_attestation

    @property
    def pricing_expires_at_utc(self) -> datetime:
        return self._pricing_attestation.retrieved_at_utc.astimezone(
            timezone.utc
        ) + timedelta(seconds=PRICING_ATTESTATION_MAX_AGE_SECONDS)

    def check(
        self,
        *,
        contract_sha256: str,
        candidate_id: str,
        candidate_commitment_sha256: str,
        chat_contract_sha256: str,
        now_utc: datetime,
    ) -> str | None:
        if self._token is not _CAPABILITY_TOKEN:
            return "kimi_pilot_capability_invalid"
        if self._consumed:
            return "kimi_pilot_capability_consumed"
        if self._contract_sha256 != contract_sha256:
            return "kimi_pilot_capability_contract_mismatch"
        if (
            self._candidate_id != candidate_id
            or self._candidate_commitment_sha256
            != candidate_commitment_sha256
            or self._chat_contract_sha256 != chat_contract_sha256
        ):
            return "kimi_pilot_successor_binding_mismatch"
        attestation_age = (
            now_utc - self._terms_attestation.retrieved_at_utc
        ).total_seconds()
        if attestation_age < 0 or attestation_age > TERMS_ATTESTATION_MAX_AGE_SECONDS:
            return "kimi_pilot_terms_attestation_stale"
        expected_binding = _authorization_binding_sha256(
            authorization_id_sha256=self._authorization_id_sha256,
            contract_sha256=contract_sha256,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment_sha256,
            chat_contract_sha256=chat_contract_sha256,
            authorization_expires_at_utc=self._expires_at_utc,
            attestation_sha256=self._terms_attestation.attestation_sha256,
        )
        if self._terms_attestation.authorization_binding_sha256 != expected_binding:
            return "kimi_pilot_terms_attestation_binding_mismatch"
        pricing_age = (
            now_utc - self._pricing_attestation.retrieved_at_utc
        ).total_seconds()
        if pricing_age < 0 or pricing_age > PRICING_ATTESTATION_MAX_AGE_SECONDS:
            return "kimi_pilot_pricing_attestation_stale"
        expected_pricing_binding = _pricing_authorization_binding_sha256(
            authorization_id_sha256=self._authorization_id_sha256,
            contract_sha256=contract_sha256,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment_sha256,
            chat_contract_sha256=chat_contract_sha256,
            authorization_expires_at_utc=self._expires_at_utc,
            terms_attestation_sha256=self._terms_attestation.attestation_sha256,
            pricing_attestation_sha256=self._pricing_attestation.attestation_sha256,
        )
        if (
            self._pricing_attestation.authorization_binding_sha256
            != expected_pricing_binding
        ):
            return "kimi_pilot_pricing_attestation_binding_mismatch"
        if now_utc >= self._expires_at_utc:
            return "kimi_pilot_capability_expired"
        return None

    def consume(self) -> bool:
        if self._consumed or self._token is not _CAPABILITY_TOKEN:
            return False
        self._consumed = True
        return True

    def __repr__(self) -> str:
        return "KimiPilotCapability([REDACTED], single_use=True)"


def create_kimi_pilot_capability(
    *,
    authorization_id: str,
    contract_sha256: str,
    candidate_id: str,
    candidate_commitment_sha256: str,
    chat_contract_sha256: str,
    expires_at_utc: datetime,
    terms_attestation: KimiTermsAttestation,
    pricing_attestation: KimiPricingAttestation,
) -> KimiPilotCapability:
    """Create a capsule after the caller has obtained separate user authority."""

    return KimiPilotCapability(
        authorization_id=authorization_id,
        contract_sha256=contract_sha256,
        candidate_id=candidate_id,
        candidate_commitment_sha256=candidate_commitment_sha256,
        chat_contract_sha256=chat_contract_sha256,
        expires_at_utc=expires_at_utc,
        terms_attestation=terms_attestation,
        pricing_attestation=pricing_attestation,
        _token=_CAPABILITY_TOKEN,
    )


def kimi_controlled_pilot_contract() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "contract_id": CONTRACT_ID,
        "implementation_status": "implemented_offline_tested_not_run",
        "provider": {
            "provider_id": KIMI_CHAT_PROVIDER_ID,
            "model_id": KIMI_CHAT_MODEL_ID,
            "transport_id": KIMI_CHAT_TRANSPORT_ID,
            "fixed_origin": "https://api.moonshot.cn",
            "chat_path": "/v1/chat/completions",
        },
        "successor_binding": {
            "candidate_id": EXPECTED_CANDIDATE_ID,
            "candidate_commitment_bound_at_runtime": True,
            "predecessor_candidate_commitment_sha256": (
                PREDECESSOR_CANDIDATE_COMMITMENT_SHA256
            ),
            "prior_results_inherited": False,
            "legacy_authorization_reuse_allowed": False,
            "authoritative_v7_manifest_required_by_run_and_verifier": True,
            "authoritative_v7_manifest_path": (
                "evals/v2/public_regression_candidate_v7.json"
            ),
        },
        "caps": {
            "scenario_count": SCENARIO_COUNT,
            "model_requests": MODEL_REQUEST_LIMIT,
            "concurrency": CONCURRENCY_LIMIT,
            "client_retries": CLIENT_RETRY_LIMIT,
            "input_tokens_per_request": INPUT_TOKEN_LIMIT_PER_REQUEST,
            "input_tokens_total": INPUT_TOKEN_LIMIT_TOTAL,
            "output_tokens_per_request": OUTPUT_TOKEN_LIMIT_PER_REQUEST,
            "output_tokens_total": OUTPUT_TOKEN_LIMIT_TOTAL,
            "tool_executions": TOOL_EXECUTION_LIMIT,
            "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
            "run_timeout_seconds": RUN_TIMEOUT_SECONDS,
            "local_estimated_reservation_limit_cny": "5.000000",
            "fallbacks_allowed": False,
            "request_body_bytes": REQUEST_BODY_LIMIT_BYTES,
        },
        "pricing": {
            "calculation": "decimal_all_cache_miss_reservation",
            "official_source_url": PRICING_SOURCE_URL,
            "model_id": PRICING_MODEL_ID,
            "currency": PRICING_CURRENCY,
            "billing_unit_tokens": PRICING_BILLING_UNIT_TOKENS,
            "cached_input_cny_per_million": "2.000000",
            "input_cny_per_million": "20.000000",
            "output_cny_per_million": "100.000000",
            "cached_input_discount_applied": False,
            "fresh_attestation_required": True,
            "attestation_max_age_seconds": PRICING_ATTESTATION_MAX_AGE_SECONDS,
            "canonicalization_version": PRICING_CANONICALIZATION_VERSION,
            "material_delta_review_status": PRICING_REVIEW_STATUS,
            "local_cost_claim_scope": "local_conservative_hard_stop_only",
            "unknown_cost_is_null": True,
            "actual_billed_cost_available": False,
        },
        "components": {
            "chat_transport_path": "src/researchops/kimi_chat_transport_v2.py",
            "chat_transport_sha256": _CHAT_TRANSPORT_SHA256,
            "chat_contract_id": "eval-v2-kimi-chat-completions-v2",
            "chat_contract_path": (
                "evals/v2/kimi_chat_completions_contract_v2.json"
            ),
            "chat_contract_sha256": _CHAT_CONTRACT_SHA256,
            "legacy_chat_transport_path": "src/researchops/kimi_chat_transport.py",
            "legacy_chat_transport_sha256": _LEGACY_CHAT_TRANSPORT_SHA256,
            "synthetic_tools_sha256": _SYNTHETIC_TOOLS_SHA256,
            "tool_schema_sha256": _TOOL_SCHEMA_SHA256,
            "scenarios_sha256": _SCENARIOS_SHA256,
            "terms_contract_sha256": _TERMS_SHA256,
        },
        "terms_gate": {
            "required_status": "provisional_synthetic_only_pass",
            "expires_at_utc": TERMS_EXPIRES_AT_UTC,
            "expired_state": "blocked_pending_final_effective_terms_review",
        },
        "authorization": {
            "explicit_confirmation_required": True,
            "single_use_in_process_capability": True,
            "authorization_id_persisted": False,
            "authorization_id_sha256_persisted": True,
            "authorization_schema_version": AUTHORIZATION_SCHEMA_VERSION,
            "authorization_expiry_canonical_utc_bound": True,
            "capability_is_security_boundary": False,
            "process_wide_concurrency_limit": 1,
            "concurrency_acquire": "non_blocking_before_key_load",
            "fresh_terms_attestation_required": True,
            "terms_attestation_max_age_seconds": TERMS_ATTESTATION_MAX_AGE_SECONDS,
            "fresh_pricing_attestation_required": True,
            "pricing_attestation_max_age_seconds": (
                PRICING_ATTESTATION_MAX_AGE_SECONDS
            ),
            "full_run_timeout_window_required_before_key_load": True,
            "required_authorization_window_seconds": RUN_TIMEOUT_SECONDS,
            "window_bound": (
                "min_terms_policy_capability_terms_attestation_and_pricing_expiry"
            ),
            "required_source_urls": dict(_TERMS_SOURCE_URLS),
            "required_displayed_updated_dates": {
                "service_agreement": TERMS_SERVICE_AGREEMENT_UPDATED_DATE,
                "privacy_policy": TERMS_PRIVACY_POLICY_UPDATED_DATE,
                "payment_agreement": TERMS_PAYMENT_AGREEMENT_UPDATED_DATE,
            },
            "required_displayed_effective_dates": {
                source_id: TERMS_DISPLAYED_EFFECTIVE_DATE
                for source_id in _TERMS_SOURCE_IDS
            },
            "canonicalization_version": TERMS_CANONICALIZATION_VERSION,
            "material_delta_review_status": TERMS_MATERIAL_REVIEW_STATUS,
            "attestation_bound_to_capability": True,
            "authorization_binding_components": [
                "authorization_id_sha256",
                "pilot_contract_sha256",
                "candidate_id",
                "candidate_commitment_sha256",
                "predecessor_candidate_commitment_sha256",
                "chat_contract_sha256",
                "authorization_schema_version",
                "authorization_expires_at_utc",
                "locked_caps",
                "terms_attestation_sha256",
                "pricing_attestation_sha256",
            ],
        },
        "state_machine": {
            "scenario_ids": [
                "KIMI-SYNTH-TOOL-001",
                "KIMI-SYNTH-TOOL-002",
                "KIMI-SYNTH-PROVIDER-003",
            ],
            "tool_scenarios_use_two_requests": True,
            "tool_scenarios_tool_choice": "required",
            "tool_scenarios_exactly_one_call": True,
            "tool_scenarios_exact_arguments_and_result_required": True,
            "provider_invalid_request_uses_one_request": True,
            "provider_invalid_request_probe_body_bytes": 124,
            "provider_invalid_request_probe_body_sha256": (
                KIMI_INVALID_REQUEST_PROBE_BODY_SHA256
            ),
            "provider_invalid_request_probe_omits_required_messages": True,
            "provider_invalid_request_probe_contains_prompt": False,
            "provider_invalid_request_probe_contains_tools": False,
            "provider_invalid_request_probe_max_completion_tokens": 1,
            "provider_invalid_request_expected_http_status": 400,
            "provider_invalid_request_expected_error_type": (
                "invalid_request_error"
            ),
            "provider_invalid_request_unexpected_status_action": "stop_no_retry",
            "batch_prevalidation_before_tool_execution": True,
            "in_flight_crash_timeout_cancel_state": "outcome_unknown",
            "production_clock": "system_utc",
            "start_and_terminal_clock_reads_are_distinct": True,
            "test_clock_requires_injected_transport": True,
            "all_time_bounds_rechecked_before_each_request_and_terminal": True,
            "artifact_success_event_count": len(_SUCCESS_EVENT_TEMPLATE),
            "artifact_success_event_sequence_exact": True,
            "artifact_failure_grammar": (
                "valid_success_prefix_optional_single_failure_marker_single_terminal"
            ),
            "artifact_failure_marker_must_immediately_precede_terminal": True,
            "artifact_terminal_unique_and_last": True,
            "artifact_event_counters_monotonic_and_delta_checked": True,
            "artifact_event_reservation_usage_tool_and_invalid_deltas_checked": True,
            "artifact_immutable_bindings_checked_on_every_event": True,
            "retry_supported": False,
            "resume_supported": False,
        },
        "artifacts": {
            "fixed_subdirectory": ARTIFACT_SUBDIRECTORY,
            "legacy_subdirectory_checked_before_key_load": (
                LEGACY_ARTIFACT_SUBDIRECTORY
            ),
            "legacy_successor_tombstone_schema_version": (
                SUCCESSOR_TOMBSTONE_SCHEMA_VERSION
            ),
            "legacy_successor_tombstone_filename": (
                SUCCESSOR_TOMBSTONE_FILENAME
            ),
            "legacy_successor_tombstone_created_before_key_load": True,
            "legacy_successor_tombstone_sha256_bound_to_all_v2_artifacts": True,
            "atomic_checkpoint": True,
            "atomic_receipt": True,
            "event_hash_chain": True,
            "terminal_projection_verified": True,
            "scenarios_completed_hash_bound": True,
            "terms_attestation_digest_hash_bound": True,
            "raw_prompt_reasoning_tool_payload_key_path_email_allowed": False,
        },
        "receipt": {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "fields": list(RECEIPT_FIELDS),
            "statuses": ["not_run", "completed", "failed", "outcome_unknown"],
        },
        "evaluation_boundary": {
            "synthetic_only": True,
            "provider_calls_performed_by_implementation": False,
            "local_cost_claim_scope": "local_conservative_hard_stop_only",
            "actual_billed_cost_known": False,
            "candidate_result_created": False,
            "prior_results_inherited": False,
            "all_authorization_claims": False,
        },
    }


def controlled_pilot_contract_sha256(project_root: str | Path) -> str:
    path = _fixed_path(
        project_root, "evals/v2/kimi_controlled_pilot_contract_v2.json"
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(slots=True)
class _Counters:
    model_requests: int = 0
    network_attempts: int = 0
    network_calls: int = 0
    requested_tools: int = 0
    deduplicated_tools: int = 0
    executed_tools: int = 0
    expected_invalid_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reserved_input_tokens: int = 0
    reserved_output_tokens: int = 0
    usage_observed_requests: int = 0
    usage_complete: bool = True
    reserved_estimated_cost: Decimal = Decimal("0")
    usage_based_estimated_cost: Decimal = Decimal("0")

    def reserve_request(self) -> None:
        if self.model_requests >= MODEL_REQUEST_LIMIT:
            raise KimiControlledPilotError("kimi_pilot_model_request_limit")
        if (
            self.reserved_input_tokens + INPUT_TOKEN_LIMIT_PER_REQUEST
            > INPUT_TOKEN_LIMIT_TOTAL
            or self.reserved_output_tokens + OUTPUT_TOKEN_LIMIT_PER_REQUEST
            > OUTPUT_TOKEN_LIMIT_TOTAL
        ):
            raise KimiControlledPilotError("kimi_pilot_token_reservation_limit")
        reservation = _cost(
            INPUT_TOKEN_LIMIT_PER_REQUEST, OUTPUT_TOKEN_LIMIT_PER_REQUEST
        )
        if (
            self.reserved_estimated_cost + reservation
            > LOCAL_ESTIMATED_RESERVATION_LIMIT_CNY
        ):
            raise KimiControlledPilotError(
                "kimi_pilot_estimated_reservation_limit"
            )
        self.model_requests += 1
        self.network_attempts += 1
        self.reserved_input_tokens += INPUT_TOKEN_LIMIT_PER_REQUEST
        self.reserved_output_tokens += OUTPUT_TOKEN_LIMIT_PER_REQUEST
        self.reserved_estimated_cost += reservation

    def observe_response(self, response: KimiChatResponse) -> None:
        self.network_calls += response.network_calls
        self._observe_usage(response.usage)

    def _observe_usage(self, usage: Any) -> None:
        self.input_tokens += usage.prompt_tokens
        self.output_tokens += usage.completion_tokens
        self.usage_observed_requests += 1
        self.usage_based_estimated_cost += _cost(
            usage.prompt_tokens, usage.completion_tokens
        )
        if (
            usage.prompt_tokens > INPUT_TOKEN_LIMIT_PER_REQUEST
            or usage.completion_tokens > OUTPUT_TOKEN_LIMIT_PER_REQUEST
            or self.input_tokens > INPUT_TOKEN_LIMIT_TOTAL
            or self.output_tokens > OUTPUT_TOKEN_LIMIT_TOTAL
            or self.usage_based_estimated_cost
            > LOCAL_ESTIMATED_RESERVATION_LIMIT_CNY
        ):
            raise KimiControlledPilotError("kimi_pilot_usage_limit_exceeded")

    def observe_error(self, error: KimiChatTransportError) -> None:
        self.network_calls += error.network_calls
        if error.usage is not None:
            self._observe_usage(error.usage)
        else:
            self.usage_complete = False

    def observe_expected_invalid_request(
        self, result: KimiInvalidRequestProbeResult
    ) -> None:
        self.network_calls += result.network_calls
        self.expected_invalid_requests += 1


class _Journal:
    def __init__(
        self,
        run_directory: Path,
        *,
        authorization_id_sha256: str,
        contract_sha256: str,
        candidate_id: str,
        candidate_commitment_sha256: str,
        chat_contract_sha256: str,
        legacy_successor_tombstone_sha256: str,
        terms_attestation: KimiTermsAttestation,
        pricing_attestation: KimiPricingAttestation,
        authorization_expires_at_utc: datetime,
        checked_at_utc: str,
    ) -> None:
        self.run_directory = run_directory
        self.authorization_id_sha256 = authorization_id_sha256
        self.contract_sha256 = contract_sha256
        self.candidate_id = candidate_id
        self.candidate_commitment_sha256 = candidate_commitment_sha256
        self.predecessor_candidate_commitment_sha256 = (
            PREDECESSOR_CANDIDATE_COMMITMENT_SHA256
        )
        self.chat_contract_sha256 = chat_contract_sha256
        self.legacy_successor_tombstone_sha256 = (
            legacy_successor_tombstone_sha256
        )
        self.terms_attestation = terms_attestation.to_dict()
        self.terms_attestation_sha256 = terms_attestation.attestation_sha256
        self.terms_authorization_binding_sha256 = (
            terms_attestation.authorization_binding_sha256
        )
        self.terms_attestation_expires_at_utc = _timestamp(
            terms_attestation.retrieved_at_utc.astimezone(timezone.utc)
            + timedelta(seconds=TERMS_ATTESTATION_MAX_AGE_SECONDS)
        )
        self.pricing_attestation = pricing_attestation.to_dict()
        self.pricing_attestation_sha256 = pricing_attestation.attestation_sha256
        self.pricing_authorization_binding_sha256 = (
            pricing_attestation.authorization_binding_sha256
        )
        self.pricing_expires_at_utc = _timestamp(
            pricing_attestation.retrieved_at_utc.astimezone(timezone.utc)
            + timedelta(seconds=PRICING_ATTESTATION_MAX_AGE_SECONDS)
        )
        self.authorization_expires_at_utc = _timestamp(
            authorization_expires_at_utc
        )
        self.authorization_schema_version = AUTHORIZATION_SCHEMA_VERSION
        self.checked_at_utc = checked_at_utc
        self.completed_at_utc: str | None = None
        self.events: list[dict[str, Any]] = []
        self.state = "authorized"
        self.scenario_index = 0
        self.scenarios_completed = 0
        self.error_code: str | None = None
        self.outcome_unknown = False

    @classmethod
    def create(
        cls,
        project_root: Path,
        *,
        authorization_id_sha256: str,
        contract_sha256: str,
        candidate_id: str,
        candidate_commitment_sha256: str,
        chat_contract_sha256: str,
        legacy_successor_tombstone_sha256: str,
        terms_attestation: KimiTermsAttestation,
        pricing_attestation: KimiPricingAttestation,
        authorization_expires_at_utc: datetime,
        checked_at_utc: str,
    ) -> "_Journal":
        try:
            artifact_root = _fixed_path(project_root, ARTIFACT_SUBDIRECTORY)
            if artifact_root.exists() and artifact_root.is_symlink():
                raise KimiControlledPilotError("kimi_pilot_artifact_root_invalid")
            artifact_root.mkdir(parents=True, exist_ok=True)
            run_directory = artifact_root / authorization_id_sha256
            if run_directory.exists():
                raise KimiControlledPilotError("kimi_pilot_resume_not_supported")
            run_directory.mkdir()
        except KimiControlledPilotError:
            raise
        except OSError as exc:
            raise KimiControlledPilotError(
                "kimi_pilot_artifact_io_failed"
            ) from exc
        return cls(
            run_directory,
            authorization_id_sha256=authorization_id_sha256,
            contract_sha256=contract_sha256,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment_sha256,
            chat_contract_sha256=chat_contract_sha256,
            legacy_successor_tombstone_sha256=(
                legacy_successor_tombstone_sha256
            ),
            terms_attestation=terms_attestation,
            pricing_attestation=pricing_attestation,
            authorization_expires_at_utc=authorization_expires_at_utc,
            checked_at_utc=checked_at_utc,
        )

    def record(
        self,
        event_type: str,
        *,
        state: str,
        counters: _Counters,
        scenario_id: str | None = None,
        outcome_unknown: bool = False,
        error_code: str | None = None,
    ) -> None:
        self.state = state
        self.error_code = error_code
        self.outcome_unknown = outcome_unknown
        prev_hash = self.events[-1]["event_hash"] if self.events else None
        event: dict[str, Any] = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "sequence": len(self.events) + 1,
            "event_type": event_type,
            "state": state,
            "scenario_id": scenario_id,
            "candidate_id": self.candidate_id,
            "candidate_commitment_sha256": self.candidate_commitment_sha256,
            "predecessor_candidate_commitment_sha256": (
                self.predecessor_candidate_commitment_sha256
            ),
            "chat_contract_sha256": self.chat_contract_sha256,
            "legacy_successor_tombstone_sha256": (
                self.legacy_successor_tombstone_sha256
            ),
            "terms_attestation_sha256": self.terms_attestation_sha256,
            "terms_authorization_binding_sha256": (
                self.terms_authorization_binding_sha256
            ),
            "terms_attestation_expires_at_utc": (
                self.terms_attestation_expires_at_utc
            ),
            "pricing_attestation_sha256": self.pricing_attestation_sha256,
            "pricing_authorization_binding_sha256": (
                self.pricing_authorization_binding_sha256
            ),
            "pricing_expires_at_utc": self.pricing_expires_at_utc,
            "authorization_schema_version": self.authorization_schema_version,
            "authorization_expires_at_utc": self.authorization_expires_at_utc,
            "checked_at_utc": self.checked_at_utc,
            "completed_at_utc": self.completed_at_utc,
            "scenarios_completed": self.scenarios_completed,
            "model_request_count": counters.model_requests,
            "network_attempts": counters.network_attempts,
            "network_calls": counters.network_calls,
            "requested_tool_call_count": counters.requested_tools,
            "deduplicated_tool_call_count": counters.deduplicated_tools,
            "executed_tool_call_count": counters.executed_tools,
            "expected_invalid_request_count": (
                counters.expected_invalid_requests
            ),
            "input_tokens": counters.input_tokens,
            "output_tokens": counters.output_tokens,
            "reserved_input_tokens": counters.reserved_input_tokens,
            "reserved_output_tokens": counters.reserved_output_tokens,
            "reserved_estimated_cost_cny": _money(
                counters.reserved_estimated_cost
            ),
            "usage_observed_request_count": counters.usage_observed_requests,
            "usage_complete": counters.usage_complete,
            "usage_based_estimated_cost_cny": (
                _money(counters.usage_based_estimated_cost)
                if counters.usage_complete
                else None
            ),
            "outcome_unknown": outcome_unknown,
            "error_code": error_code,
            "prev_hash": prev_hash,
        }
        event["event_hash"] = hashlib.sha256(_canonical_bytes(event)).hexdigest()
        self.events.append(event)
        event_lines = b"".join(_canonical_bytes(item) + b"\n" for item in self.events)
        _atomic_write(self.run_directory / "event_chain.jsonl", event_lines)
        self.write_checkpoint(counters)

    def write_checkpoint(self, counters: _Counters) -> None:
        checkpoint = self.checkpoint(counters)
        _atomic_write(
            self.run_directory / "checkpoint.json",
            _canonical_bytes(checkpoint) + b"\n",
        )

    def checkpoint(self, counters: _Counters) -> dict[str, Any]:
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "authorization_id_sha256": self.authorization_id_sha256,
            "contract_sha256": self.contract_sha256,
            "candidate_id": self.candidate_id,
            "candidate_commitment_sha256": self.candidate_commitment_sha256,
            "predecessor_candidate_commitment_sha256": (
                self.predecessor_candidate_commitment_sha256
            ),
            "chat_contract_sha256": self.chat_contract_sha256,
            "legacy_successor_tombstone_sha256": (
                self.legacy_successor_tombstone_sha256
            ),
            "terms_attestation": dict(self.terms_attestation),
            "terms_attestation_sha256": self.terms_attestation_sha256,
            "terms_authorization_binding_sha256": (
                self.terms_authorization_binding_sha256
            ),
            "terms_attestation_max_age_seconds": TERMS_ATTESTATION_MAX_AGE_SECONDS,
            "terms_attestation_expires_at_utc": (
                self.terms_attestation_expires_at_utc
            ),
            "pricing_attestation": dict(self.pricing_attestation),
            "pricing_attestation_sha256": self.pricing_attestation_sha256,
            "pricing_authorization_binding_sha256": (
                self.pricing_authorization_binding_sha256
            ),
            "pricing_attestation_max_age_seconds": (
                PRICING_ATTESTATION_MAX_AGE_SECONDS
            ),
            "pricing_expires_at_utc": self.pricing_expires_at_utc,
            "authorization_schema_version": self.authorization_schema_version,
            "authorization_expires_at_utc": self.authorization_expires_at_utc,
            "checked_at_utc": self.checked_at_utc,
            "completed_at_utc": self.completed_at_utc,
            "state": self.state,
            "scenario_index": self.scenario_index,
            "scenarios_completed": self.scenarios_completed,
            "model_request_count": counters.model_requests,
            "network_attempts": counters.network_attempts,
            "network_calls": counters.network_calls,
            "requested_tool_call_count": counters.requested_tools,
            "deduplicated_tool_call_count": counters.deduplicated_tools,
            "executed_tool_call_count": counters.executed_tools,
            "expected_invalid_request_count": (
                counters.expected_invalid_requests
            ),
            "input_tokens": counters.input_tokens,
            "output_tokens": counters.output_tokens,
            "reserved_input_tokens": counters.reserved_input_tokens,
            "reserved_output_tokens": counters.reserved_output_tokens,
            "reserved_estimated_cost_cny": _money(
                counters.reserved_estimated_cost
            ),
            "usage_observed_request_count": counters.usage_observed_requests,
            "usage_complete": counters.usage_complete,
            "usage_based_estimated_cost_cny": (
                _money(counters.usage_based_estimated_cost)
                if counters.usage_complete
                else None
            ),
            "outcome_unknown": self.outcome_unknown,
            "error_code": self.error_code,
            "event_count": len(self.events),
            "event_chain_head_sha256": (
                self.events[-1]["event_hash"] if self.events else None
            ),
        }

    def write_receipt(self, receipt: Mapping[str, Any]) -> None:
        _atomic_write(
            self.run_directory / "receipt.json",
            _canonical_bytes(receipt) + b"\n",
        )


def _cost(input_tokens: int, output_tokens: int) -> Decimal:
    return (
        Decimal(input_tokens) * INPUT_PRICE_PER_MILLION_CNY
        + Decimal(output_tokens) * OUTPUT_PRICE_PER_MILLION_CNY
    ) / _MILLION


def _money(value: Decimal) -> str:
    return format(value.quantize(_MONEY_QUANTUM), "f")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _clock_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("Kimi pilot clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _terms_expiry_utc() -> datetime:
    return datetime.fromisoformat(TERMS_EXPIRES_AT_UTC.replace("Z", "+00:00"))


def _authorization_instant_error(
    now_utc: datetime,
    *,
    authorization_expires_at_utc: datetime,
    terms_attestation_expires_at_utc: datetime,
    pricing_expires_at_utc: datetime,
) -> str | None:
    if now_utc >= _terms_expiry_utc():
        return "kimi_pilot_terms_expired"
    if now_utc >= authorization_expires_at_utc:
        return "kimi_pilot_capability_expired"
    if now_utc >= terms_attestation_expires_at_utc:
        return "kimi_pilot_terms_attestation_stale"
    if now_utc >= pricing_expires_at_utc:
        return "kimi_pilot_pricing_attestation_stale"
    return None


def _authorization_window_error(
    start_utc: datetime,
    *,
    authorization_expires_at_utc: datetime,
    terms_attestation_expires_at_utc: datetime,
    pricing_expires_at_utc: datetime,
) -> str | None:
    required_until = start_utc + timedelta(seconds=RUN_TIMEOUT_SECONDS)
    if required_until > _terms_expiry_utc():
        return "kimi_pilot_terms_window_insufficient"
    if required_until > authorization_expires_at_utc:
        return "kimi_pilot_capability_window_insufficient"
    if required_until > terms_attestation_expires_at_utc:
        return "kimi_pilot_terms_attestation_window_insufficient"
    if required_until > pricing_expires_at_utc:
        return "kimi_pilot_pricing_window_insufficient"
    return None


def _require_authorized_instant(
    now_utc: datetime,
    *,
    authorization_expires_at_utc: datetime,
    terms_attestation_expires_at_utc: datetime,
    pricing_expires_at_utc: datetime,
) -> None:
    error_code = _authorization_instant_error(
        now_utc,
        authorization_expires_at_utc=authorization_expires_at_utc,
        terms_attestation_expires_at_utc=terms_attestation_expires_at_utc,
        pricing_expires_at_utc=pricing_expires_at_utc,
    )
    if error_code is not None:
        raise KimiControlledPilotError(error_code)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".tmp-", suffix=".json", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise KimiControlledPilotError("kimi_pilot_artifact_io_failed") from exc
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _fixed_path(project_root: str | Path, relative: str) -> Path:
    try:
        root = Path(project_root).resolve()
        path = (root / relative).resolve()
    except OSError as exc:
        raise KimiControlledPilotError("kimi_pilot_path_invalid") from exc
    if not path.is_relative_to(root):
        raise KimiControlledPilotError("kimi_pilot_path_invalid")
    return path


def _successor_tombstone_payload(
    *,
    authorization_id_sha256: str,
    contract_sha256: str,
    candidate_id: str,
    candidate_commitment_sha256: str,
    chat_contract_sha256: str,
    authorization_expires_at_utc: datetime,
    created_at_utc: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": SUCCESSOR_TOMBSTONE_SCHEMA_VERSION,
        "authorization_id_sha256": authorization_id_sha256,
        "successor_contract_id": CONTRACT_ID,
        "successor_contract_sha256": contract_sha256,
        "candidate_id": candidate_id,
        "candidate_commitment_sha256": candidate_commitment_sha256,
        "predecessor_candidate_commitment_sha256": (
            PREDECESSOR_CANDIDATE_COMMITMENT_SHA256
        ),
        "chat_contract_sha256": chat_contract_sha256,
        "authorization_schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "authorization_expires_at_utc": _canonical_authorization_expiry(
            authorization_expires_at_utc
        ),
        "created_at_utc": _timestamp(created_at_utc),
        "blocks_legacy_v1_runner": True,
        "authorizes_retry": False,
        "authorizes_resume": False,
        "authorizes_provider_registration": False,
    }


def _create_legacy_successor_tombstone(
    project_root: Path,
    *,
    authorization_id_sha256: str,
    contract_sha256: str,
    candidate_id: str,
    candidate_commitment_sha256: str,
    chat_contract_sha256: str,
    authorization_expires_at_utc: datetime,
    created_at_utc: datetime,
) -> str:
    payload = _successor_tombstone_payload(
        authorization_id_sha256=authorization_id_sha256,
        contract_sha256=contract_sha256,
        candidate_id=candidate_id,
        candidate_commitment_sha256=candidate_commitment_sha256,
        chat_contract_sha256=chat_contract_sha256,
        authorization_expires_at_utc=authorization_expires_at_utc,
        created_at_utc=created_at_utc,
    )
    encoded = _canonical_bytes(payload) + b"\n"
    legacy_root = _fixed_path(project_root, LEGACY_ARTIFACT_SUBDIRECTORY)
    run_directory = legacy_root / authorization_id_sha256
    try:
        legacy_root.mkdir(parents=True, exist_ok=True)
        if legacy_root.is_symlink():
            raise KimiControlledPilotError(
                "kimi_pilot_legacy_tombstone_invalid"
            )
        run_directory.mkdir()
        _atomic_write(run_directory / SUCCESSOR_TOMBSTONE_FILENAME, encoded)
    except KimiControlledPilotError:
        raise
    except FileExistsError as exc:
        raise KimiControlledPilotError(
            "kimi_pilot_authorization_reused_across_versions"
        ) from exc
    except OSError as exc:
        raise KimiControlledPilotError(
            "kimi_pilot_legacy_tombstone_io_failed"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        raise KimiControlledPilotError("kimi_pilot_contract_invalid")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except KimiControlledPilotError:
        raise
    except Exception as exc:
        raise KimiControlledPilotError("kimi_pilot_contract_invalid") from exc
    if not isinstance(value, dict):
        raise KimiControlledPilotError("kimi_pilot_contract_invalid")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise KimiControlledPilotError("kimi_pilot_contract_invalid")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    del value
    raise KimiControlledPilotError("kimi_pilot_contract_invalid")


def _validate_authoritative_candidate(
    project_root: Path,
    *,
    candidate_id: str,
    candidate_commitment_sha256: str,
    candidate_validator: Callable[..., Mapping[str, Any]] | None = None,
) -> None:
    candidate_path = _fixed_path(
        project_root, "evals/v2/public_regression_candidate_v7.json"
    )
    try:
        if candidate_validator is None:
            # Lazy import avoids the eval_v2_freeze -> pilot_v2 import cycle.
            from .eval_v2_freeze import validate_public_regression_candidate

            result = validate_public_regression_candidate(
                project_root=project_root,
                candidate_path=candidate_path,
                verify_environment=False,
            )
        else:
            result = candidate_validator(
                project_root=project_root,
                candidate_path=candidate_path,
                verify_environment=False,
            )
    except Exception as exc:
        raise KimiControlledPilotError(
            "kimi_pilot_candidate_manifest_invalid"
        ) from exc
    if (
        not isinstance(result, Mapping)
        or result.get("status") != "valid"
        or result.get("candidate_id") != candidate_id
        or result.get("candidate_commitment_sha256")
        != candidate_commitment_sha256
        or result.get("predecessor_candidate_commitment_sha256")
        != PREDECESSOR_CANDIDATE_COMMITMENT_SHA256
        or result.get("prior_results_inherited") is not False
        or result.get("predecessor_failure_result_inherited") is not False
        or result.get("predecessor_authorization_reused") is not False
    ):
        raise KimiControlledPilotError(
            "kimi_pilot_candidate_manifest_invalid"
        )


def _validate_local_contracts(
    project_root: Path,
    now_utc: datetime,
    *,
    candidate_id: str,
    candidate_commitment_sha256: str,
    candidate_validator: Callable[..., Mapping[str, Any]] | None,
) -> tuple[str, dict[str, Any]]:
    contract_path = _fixed_path(
        project_root, "evals/v2/kimi_controlled_pilot_contract_v2.json"
    )
    chat_contract_path = _fixed_path(
        project_root, "evals/v2/kimi_chat_completions_contract_v2.json"
    )
    scenarios_path = _fixed_path(
        project_root, "evals/v2/kimi_controlled_pilot_scenarios_v1.json"
    )
    terms_path = _fixed_path(
        project_root, "evals/v2/kimi_terms_g2a_provisional_contract.json"
    )
    contract = _load_json(contract_path)
    scenarios = _load_json(scenarios_path)
    terms = _load_json(terms_path)
    if contract != kimi_controlled_pilot_contract():
        raise KimiControlledPilotError("kimi_pilot_contract_invalid")
    if (
        not chat_contract_path.is_file()
        or chat_contract_path.is_symlink()
        or hashlib.sha256(chat_contract_path.read_bytes()).hexdigest()
        != _CHAT_CONTRACT_SHA256
    ):
        raise KimiControlledPilotError("kimi_pilot_chat_contract_invalid")
    if hashlib.sha256(scenarios_path.read_bytes()).hexdigest() != _SCENARIOS_SHA256:
        raise KimiControlledPilotError("kimi_pilot_contract_invalid")
    if hashlib.sha256(terms_path.read_bytes()).hexdigest() != _TERMS_SHA256:
        raise KimiControlledPilotError("kimi_pilot_terms_invalid")
    if scenarios.get("scenario_set_id") != "kimi-controlled-pilot-synthetic-tools-v1":
        raise KimiControlledPilotError("kimi_pilot_contract_invalid")
    scenario_values = scenarios.get("scenarios")
    if not isinstance(scenario_values, list) or len(scenario_values) != SCENARIO_COUNT:
        raise KimiControlledPilotError("kimi_pilot_contract_invalid")
    decision = terms.get("decision")
    expiry = terms.get("pre_effective_expiry")
    online_gate = terms.get("pre_effective_online_gate")
    announced_preview = terms.get("source_evidence", {}).get(
        "announced_preview"
    )
    if (
        not isinstance(decision, dict)
        or decision.get("status") != "provisional_synthetic_only_pass"
        or decision.get("does_not_authorize_provider_calls") is not True
        or not isinstance(expiry, dict)
        or expiry.get("expires_at_utc") != TERMS_EXPIRES_AT_UTC
        or not isinstance(online_gate, dict)
        or online_gate.get("fresh_first_party_source_attestation_required")
        is not True
        or online_gate.get("attestation_max_age_seconds")
        != TERMS_ATTESTATION_MAX_AGE_SECONDS
        or online_gate.get(
            "operator_source_recheck_required_immediately_before_online_run"
        )
        is not True
        or online_gate.get("attestation_must_be_bound_to_separate_user_authorization")
        is not True
        or online_gate.get(
            "attestation_must_confirm_no_material_or_unclassifiable_delta"
        )
        is not True
        or online_gate.get("source_update_detection_implemented") is not False
        or not isinstance(announced_preview, dict)
        or announced_preview.get("displayed_updated_date")
        != TERMS_SERVICE_AGREEMENT_UPDATED_DATE
    ):
        raise KimiControlledPilotError("kimi_pilot_terms_invalid")
    if now_utc >= _terms_expiry_utc():
        raise KimiControlledPilotError("kimi_pilot_terms_expired")
    _validate_authoritative_candidate(
        project_root,
        candidate_id=candidate_id,
        candidate_commitment_sha256=candidate_commitment_sha256,
        candidate_validator=candidate_validator,
    )
    return hashlib.sha256(contract_path.read_bytes()).hexdigest(), scenarios


def _not_run_receipt(
    *,
    now_utc: datetime,
    error_code: str,
    contract_sha256: str | None = None,
    candidate_id: str | None = None,
    candidate_commitment_sha256: str | None = None,
    chat_contract_sha256: str | None = None,
    legacy_successor_tombstone_sha256: str | None = None,
    authorization_id_sha256: str | None = None,
    terms_attestation: Mapping[str, Any] | None = None,
    terms_attestation_sha256: str | None = None,
    terms_authorization_binding_sha256: str | None = None,
    terms_attestation_expires_at_utc: datetime | None = None,
    pricing_attestation: Mapping[str, Any] | None = None,
    pricing_attestation_sha256: str | None = None,
    pricing_authorization_binding_sha256: str | None = None,
    pricing_expires_at_utc: datetime | None = None,
    authorization_expires_at_utc: datetime | None = None,
) -> dict[str, Any]:
    return _receipt(
        status="not_run",
        contract_sha256=contract_sha256,
        candidate_id=candidate_id,
        candidate_commitment_sha256=candidate_commitment_sha256,
        chat_contract_sha256=chat_contract_sha256,
        legacy_successor_tombstone_sha256=(
            legacy_successor_tombstone_sha256
        ),
        authorization_id_sha256=authorization_id_sha256,
        terms_attestation=terms_attestation,
        terms_attestation_sha256=terms_attestation_sha256,
        terms_authorization_binding_sha256=(
            terms_authorization_binding_sha256
        ),
        terms_attestation_expires_at_utc=(
            _timestamp(terms_attestation_expires_at_utc)
            if terms_attestation_expires_at_utc is not None
            else None
        ),
        pricing_attestation=pricing_attestation,
        pricing_attestation_sha256=pricing_attestation_sha256,
        pricing_authorization_binding_sha256=(
            pricing_authorization_binding_sha256
        ),
        pricing_expires_at_utc=(
            _timestamp(pricing_expires_at_utc)
            if pricing_expires_at_utc is not None
            else None
        ),
        authorization_expires_at_utc=(
            _timestamp(authorization_expires_at_utc)
            if authorization_expires_at_utc is not None
            else None
        ),
        checked_at_utc=_timestamp(now_utc),
        completed_at_utc=_timestamp(now_utc),
        state="not_run",
        scenarios_completed=0,
        counters=_Counters(),
        usage_based_estimated_cost_cny=None,
        outcome_unknown=False,
        error_code=error_code,
        event_count=0,
        chain_head=None,
    )


def _receipt(
    *,
    status: str,
    contract_sha256: str | None,
    candidate_id: str | None,
    candidate_commitment_sha256: str | None,
    chat_contract_sha256: str | None,
    legacy_successor_tombstone_sha256: str | None,
    authorization_id_sha256: str | None,
    terms_attestation: Mapping[str, Any] | None,
    terms_attestation_sha256: str | None,
    terms_authorization_binding_sha256: str | None,
    terms_attestation_expires_at_utc: str | None,
    pricing_attestation: Mapping[str, Any] | None,
    pricing_attestation_sha256: str | None,
    pricing_authorization_binding_sha256: str | None,
    pricing_expires_at_utc: str | None,
    authorization_expires_at_utc: str | None,
    checked_at_utc: str,
    completed_at_utc: str,
    state: str,
    scenarios_completed: int,
    counters: _Counters,
    usage_based_estimated_cost_cny: str | None,
    outcome_unknown: bool,
    error_code: str | None,
    event_count: int,
    chain_head: str | None,
) -> dict[str, Any]:
    result = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": status,
        "contract_id": CONTRACT_ID,
        "contract_sha256": contract_sha256,
        "candidate_id": candidate_id,
        "candidate_commitment_sha256": candidate_commitment_sha256,
        "predecessor_candidate_commitment_sha256": (
            PREDECESSOR_CANDIDATE_COMMITMENT_SHA256
        ),
        "chat_contract_sha256": chat_contract_sha256,
        "legacy_successor_tombstone_sha256": (
            legacy_successor_tombstone_sha256
        ),
        "authorization_id_sha256": authorization_id_sha256,
        "terms_attestation": (
            dict(terms_attestation) if terms_attestation is not None else None
        ),
        "terms_attestation_sha256": terms_attestation_sha256,
        "terms_authorization_binding_sha256": (
            terms_authorization_binding_sha256
        ),
        "terms_attestation_max_age_seconds": TERMS_ATTESTATION_MAX_AGE_SECONDS,
        "terms_attestation_expires_at_utc": terms_attestation_expires_at_utc,
        "pricing_attestation": (
            dict(pricing_attestation) if pricing_attestation is not None else None
        ),
        "pricing_attestation_sha256": pricing_attestation_sha256,
        "pricing_authorization_binding_sha256": (
            pricing_authorization_binding_sha256
        ),
        "pricing_attestation_max_age_seconds": PRICING_ATTESTATION_MAX_AGE_SECONDS,
        "pricing_expires_at_utc": pricing_expires_at_utc,
        "authorization_schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "authorization_expires_at_utc": authorization_expires_at_utc,
        "checked_at_utc": checked_at_utc,
        "completed_at_utc": completed_at_utc,
        "state": state,
        "scenario_count": SCENARIO_COUNT,
        "scenarios_completed": scenarios_completed,
        "model_request_count": counters.model_requests,
        "model_request_limit": MODEL_REQUEST_LIMIT,
        "network_attempts": counters.network_attempts,
        "network_calls": counters.network_calls,
        "requested_tool_call_count": counters.requested_tools,
        "deduplicated_tool_call_count": counters.deduplicated_tools,
        "executed_tool_call_count": counters.executed_tools,
        "expected_invalid_request_count": counters.expected_invalid_requests,
        "input_tokens": counters.input_tokens,
        "output_tokens": counters.output_tokens,
        "reserved_input_tokens": counters.reserved_input_tokens,
        "reserved_output_tokens": counters.reserved_output_tokens,
        "usage_observed_request_count": counters.usage_observed_requests,
        "usage_complete": counters.usage_complete,
        "reserved_estimated_cost_cny": _money(
            counters.reserved_estimated_cost
        ),
        "usage_based_estimated_cost_cny": usage_based_estimated_cost_cny,
        "actual_billed_cost_cny": None,
        "local_estimated_reservation_limit_cny": _money(
            LOCAL_ESTIMATED_RESERVATION_LIMIT_CNY
        ),
        "local_cost_claim_scope": "local_conservative_hard_stop_only",
        "all_cache_miss_pricing": True,
        "outcome_unknown": outcome_unknown,
        "error_code": error_code,
        "event_count": event_count,
        "event_chain_head_sha256": chain_head,
        "candidate_result_created": False,
        "authorizes_retry": False,
        "authorizes_resume": False,
        "authorizes_chat": False,
        "authorizes_tools": False,
        "authorizes_model_quality_claim": False,
        "authorizes_provider_registration": False,
        "authorizes_private_evaluation": False,
        "authorizes_non_synthetic_data": False,
    }
    if tuple(result) != RECEIPT_FIELDS:
        raise AssertionError("Kimi pilot receipt field drift")
    return result


def _build_tool() -> KimiFunctionTool:
    function = synthetic_tool_schema()["function"]
    return KimiFunctionTool.from_schema(
        name=function["name"],
        description=function["description"],
        parameters=function["parameters"],
    )


def _scenario_prompt(scenario_id: str) -> tuple[KimiTextMessage, KimiTextMessage]:
    system = KimiTextMessage(
        "system",
        "Synthetic contract test only. Use only the declared tool and synthetic IDs.",
    )
    prompts = {
        "KIMI-SYNTH-TOOL-001": (
            "Look up effect_size for kimi_synth_success_v1 with the declared tool."
        ),
        "KIMI-SYNTH-TOOL-002": (
            "Look up effect_size for kimi_synth_missing_v1 with the declared tool."
        ),
    }
    return system, KimiTextMessage("user", prompts[scenario_id])


def _validate_tool_scenario_result(
    scenario: Mapping[str, Any],
    *,
    provider_call: Any,
    tool_batch: Mapping[str, Any],
) -> None:
    expected_calls = scenario.get("tool_calls")
    expected = scenario.get("expected")
    if (
        not isinstance(expected_calls, list)
        or len(expected_calls) != 1
        or not isinstance(expected_calls[0], dict)
        or not isinstance(expected, dict)
        or provider_call.name != expected_calls[0].get("name")
    ):
        raise KimiControlledPilotError("kimi_pilot_scenario_contract_invalid")
    try:
        actual_arguments = json.loads(
            provider_call.arguments_json,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        expected_arguments = json.loads(
            expected_calls[0]["arguments"],
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except Exception as exc:
        raise KimiControlledPilotError(
            "kimi_pilot_scenario_contract_invalid"
        ) from exc
    if actual_arguments != expected_arguments:
        raise KimiControlledPilotError("kimi_pilot_scenario_arguments_mismatch")
    if (
        tool_batch.get("requested_tool_call_count") != 1
        or tool_batch.get("deduplicated_tool_call_count") != 0
        or tool_batch.get("executed_tool_call_count") != 1
        or not isinstance(tool_batch.get("results"), list)
        or len(tool_batch["results"]) != 1
        or tool_batch["results"][0].get("result") != expected.get("tool_result")
    ):
        raise KimiControlledPilotError("kimi_pilot_scenario_result_mismatch")


async def run_kimi_controlled_pilot(
    project_root: str | Path,
    *,
    confirm_online: bool,
    capability: KimiPilotCapability | None,
    _key_loader: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    """Production entry point fixed to the system UTC clock/default transport."""

    return await _run_kimi_controlled_pilot_core(
        project_root,
        confirm_online=confirm_online,
        capability=capability,
        _key_loader=_key_loader,
        _transport_factory=None,
        _now=_now_utc,
        _candidate_validator=None,
    )


async def _run_kimi_controlled_pilot_for_test(
    project_root: str | Path,
    *,
    confirm_online: bool,
    capability: KimiPilotCapability | None,
    _key_loader: Callable[[], str | None] | None,
    _transport_factory: Callable[[], httpx.AsyncBaseTransport],
    _now: Callable[[], datetime],
    _candidate_validator: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Private test seam; injected clocks require an injected transport."""

    if _transport_factory is None:
        raise ValueError("Test clock cannot be paired with default transport")
    return await _run_kimi_controlled_pilot_core(
        project_root,
        confirm_online=confirm_online,
        capability=capability,
        _key_loader=_key_loader,
        _transport_factory=_transport_factory,
        _now=_now,
        _candidate_validator=_candidate_validator,
    )


async def _run_kimi_controlled_pilot_core(
    project_root: str | Path,
    *,
    confirm_online: bool,
    capability: KimiPilotCapability | None,
    _key_loader: Callable[[], str | None] | None,
    _transport_factory: Callable[[], httpx.AsyncBaseTransport] | None,
    _now: Callable[[], datetime],
    _candidate_validator: Callable[..., Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Acquire the process lease around every gate, artifact, and run path."""

    auth_hash = (
        capability.authorization_id_sha256
        if isinstance(capability, KimiPilotCapability)
        else None
    )
    candidate_id = (
        capability.candidate_id
        if isinstance(capability, KimiPilotCapability)
        else None
    )
    candidate_commitment = (
        capability.candidate_commitment_sha256
        if isinstance(capability, KimiPilotCapability)
        else None
    )
    chat_contract_sha256 = (
        capability.chat_contract_sha256
        if isinstance(capability, KimiPilotCapability)
        else None
    )
    if not _PROCESS_RUN_LOCK.acquire(blocking=False):
        return _not_run_receipt(
            now_utc=_clock_now(_now),
            error_code="kimi_pilot_concurrency_denied",
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment,
            chat_contract_sha256=chat_contract_sha256,
            authorization_id_sha256=auth_hash,
        )
    try:
        return await _run_kimi_controlled_pilot_core_locked(
            project_root,
            confirm_online=confirm_online,
            capability=capability,
            _key_loader=_key_loader,
            _transport_factory=_transport_factory,
            _now=_now,
            _candidate_validator=_candidate_validator,
        )
    finally:
        _PROCESS_RUN_LOCK.release()


async def _run_kimi_controlled_pilot_core_locked(
    project_root: str | Path,
    *,
    confirm_online: bool,
    capability: KimiPilotCapability | None,
    _key_loader: Callable[[], str | None] | None,
    _transport_factory: Callable[[], httpx.AsyncBaseTransport] | None,
    _now: Callable[[], datetime],
    _candidate_validator: Callable[..., Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Testable core for the fixed three-scenario state machine."""

    now = _clock_now(_now)
    auth_hash = (
        capability.authorization_id_sha256
        if isinstance(capability, KimiPilotCapability)
        else None
    )
    candidate_id = (
        capability.candidate_id
        if isinstance(capability, KimiPilotCapability)
        else None
    )
    candidate_commitment = (
        capability.candidate_commitment_sha256
        if isinstance(capability, KimiPilotCapability)
        else None
    )
    chat_contract_sha256 = (
        capability.chat_contract_sha256
        if isinstance(capability, KimiPilotCapability)
        else None
    )
    if confirm_online is not True:
        return _not_run_receipt(
            now_utc=now,
            error_code="kimi_pilot_confirmation_required",
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment,
            chat_contract_sha256=chat_contract_sha256,
            authorization_id_sha256=auth_hash,
        )
    if not isinstance(capability, KimiPilotCapability):
        return _not_run_receipt(
            now_utc=now, error_code="kimi_pilot_capability_missing"
        )
    try:
        root = Path(project_root).resolve()
        contract_sha256, scenarios = _validate_local_contracts(
            root,
            now,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment,
            candidate_validator=_candidate_validator,
        )
    except KimiControlledPilotError as exc:
        return _not_run_receipt(
            now_utc=now,
            error_code=exc.code,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment,
            chat_contract_sha256=chat_contract_sha256,
            authorization_id_sha256=auth_hash,
        )
    capability_error = capability.check(
        contract_sha256=contract_sha256,
        candidate_id=EXPECTED_CANDIDATE_ID,
        candidate_commitment_sha256=candidate_commitment,
        chat_contract_sha256=_CHAT_CONTRACT_SHA256,
        now_utc=now,
    )
    if capability_error is not None:
        return _not_run_receipt(
            now_utc=now,
            error_code=capability_error,
            contract_sha256=contract_sha256,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment,
            chat_contract_sha256=chat_contract_sha256,
            authorization_id_sha256=auth_hash,
        )
    window_error = _authorization_window_error(
        now,
        authorization_expires_at_utc=capability.expires_at_utc,
        terms_attestation_expires_at_utc=(
            capability.terms_attestation_expires_at_utc
        ),
        pricing_expires_at_utc=capability.pricing_expires_at_utc,
    )
    if window_error is not None:
        return _not_run_receipt(
            now_utc=now,
            error_code=window_error,
            contract_sha256=contract_sha256,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment,
            chat_contract_sha256=chat_contract_sha256,
            authorization_id_sha256=auth_hash,
            terms_attestation=capability.terms_attestation.to_dict(),
            terms_attestation_sha256=(
                capability.terms_attestation.attestation_sha256
            ),
            terms_authorization_binding_sha256=(
                capability.terms_attestation.authorization_binding_sha256
            ),
            terms_attestation_expires_at_utc=(
                capability.terms_attestation_expires_at_utc
            ),
            pricing_attestation=capability.pricing_attestation.to_dict(),
            pricing_attestation_sha256=(
                capability.pricing_attestation.attestation_sha256
            ),
            pricing_authorization_binding_sha256=(
                capability.pricing_attestation.authorization_binding_sha256
            ),
            pricing_expires_at_utc=capability.pricing_expires_at_utc,
            authorization_expires_at_utc=capability.expires_at_utc,
        )
    artifact_root = _fixed_path(root, ARTIFACT_SUBDIRECTORY)
    legacy_artifact_root = _fixed_path(root, LEGACY_ARTIFACT_SUBDIRECTORY)
    run_directory = artifact_root / auth_hash
    legacy_run_directory = legacy_artifact_root / auth_hash
    if run_directory.exists() or run_directory.is_symlink():
        return _not_run_receipt(
            now_utc=now,
            error_code="kimi_pilot_resume_not_supported",
            contract_sha256=contract_sha256,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment,
            chat_contract_sha256=chat_contract_sha256,
            authorization_id_sha256=auth_hash,
        )
    if legacy_run_directory.exists() or legacy_run_directory.is_symlink():
        return _not_run_receipt(
            now_utc=now,
            error_code="kimi_pilot_authorization_reused_across_versions",
            contract_sha256=contract_sha256,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment,
            chat_contract_sha256=chat_contract_sha256,
            authorization_id_sha256=auth_hash,
        )
    try:
        legacy_successor_tombstone_sha256 = (
            _create_legacy_successor_tombstone(
                root,
                authorization_id_sha256=auth_hash,
                contract_sha256=contract_sha256,
                candidate_id=candidate_id,
                candidate_commitment_sha256=candidate_commitment,
                chat_contract_sha256=chat_contract_sha256,
                authorization_expires_at_utc=capability.expires_at_utc,
                created_at_utc=now,
            )
        )
    except KimiControlledPilotError as exc:
        return _not_run_receipt(
            now_utc=now,
            error_code=exc.code,
            contract_sha256=contract_sha256,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment,
            chat_contract_sha256=chat_contract_sha256,
            authorization_id_sha256=auth_hash,
        )
    if not capability.consume():
        return _not_run_receipt(
            now_utc=now,
            error_code="kimi_pilot_capability_consumed",
            contract_sha256=contract_sha256,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment,
            chat_contract_sha256=chat_contract_sha256,
            legacy_successor_tombstone_sha256=(
                legacy_successor_tombstone_sha256
            ),
            authorization_id_sha256=auth_hash,
        )

    counters = _Counters()
    tool_executor = KimiSyntheticToolExecutor()
    scenarios_completed = 0
    checked_at = _timestamp(now)
    api_key: str | None = None
    try:
        journal = _Journal.create(
            root,
            authorization_id_sha256=auth_hash,
            contract_sha256=contract_sha256,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment,
            chat_contract_sha256=chat_contract_sha256,
            legacy_successor_tombstone_sha256=(
                legacy_successor_tombstone_sha256
            ),
            terms_attestation=capability.terms_attestation,
            pricing_attestation=capability.pricing_attestation,
            authorization_expires_at_utc=capability.expires_at_utc,
            checked_at_utc=checked_at,
        )
        journal.record("run_authorized", state="authorized", counters=counters)
    except (KimiControlledPilotError, OSError):
        return _not_run_receipt(
            now_utc=now,
            error_code="kimi_pilot_artifact_io_failed",
            contract_sha256=contract_sha256,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment,
            chat_contract_sha256=chat_contract_sha256,
            legacy_successor_tombstone_sha256=(
                legacy_successor_tombstone_sha256
            ),
            authorization_id_sha256=auth_hash,
            terms_attestation=capability.terms_attestation.to_dict(),
            terms_attestation_sha256=(
                capability.terms_attestation.attestation_sha256
            ),
            terms_authorization_binding_sha256=(
                capability.terms_attestation.authorization_binding_sha256
            ),
            terms_attestation_expires_at_utc=(
                capability.terms_attestation_expires_at_utc
            ),
            pricing_attestation=capability.pricing_attestation.to_dict(),
            pricing_attestation_sha256=(
                capability.pricing_attestation.attestation_sha256
            ),
            pricing_authorization_binding_sha256=(
                capability.pricing_attestation.authorization_binding_sha256
            ),
            pricing_expires_at_utc=capability.pricing_expires_at_utc,
            authorization_expires_at_utc=capability.expires_at_utc,
        )
    try:
        if _key_loader is None:
            raise KimiControlledPilotError("kimi_pilot_key_missing")
        try:
            api_key = _key_loader()
        except Exception as exc:
            raise KimiControlledPilotError("kimi_pilot_key_load_failed") from exc
        if not isinstance(api_key, str) or not api_key or api_key.isspace():
            raise KimiControlledPilotError("kimi_pilot_key_missing")

        async with asyncio.timeout(RUN_TIMEOUT_SECONDS):
            scenario_list = scenarios["scenarios"]
            for index, scenario in enumerate(scenario_list, start=1):
                journal.scenario_index = index
                scenario_id = scenario["scenario_id"]
                if index <= 2:
                    first_request = KimiChatRequest(
                        messages=_scenario_prompt(scenario_id),
                        tools=(_build_tool(),),
                        tool_choice="required",
                        max_completion_tokens=OUTPUT_TOKEN_LIMIT_PER_REQUEST,
                        reasoning_effort="low",
                    )
                    first = await _run_request(
                        first_request,
                        api_key=api_key,
                        counters=counters,
                        journal=journal,
                        scenario_id=scenario_id,
                        phase="tool_request",
                        transport_factory=_transport_factory,
                        clock=_now,
                        authorization_expires_at_utc=capability.expires_at_utc,
                        terms_attestation_expires_at_utc=(
                            capability.terms_attestation_expires_at_utc
                        ),
                        pricing_expires_at_utc=capability.pricing_expires_at_utc,
                    )
                    if (
                        first.finish_reason != "tool_calls"
                        or len(first.assistant_message.tool_calls) != 1
                    ):
                        raise KimiControlledPilotError(
                            "kimi_pilot_tool_response_invalid"
                        )
                    raw_calls = [
                        {
                            "call_id": call.call_id,
                            "name": call.name,
                            "arguments": call.arguments_json,
                        }
                        for call in first.assistant_message.tool_calls
                    ]
                    tool_batch = tool_executor.execute_batch(raw_calls)
                    _validate_tool_scenario_result(
                        scenario,
                        provider_call=first.assistant_message.tool_calls[0],
                        tool_batch=tool_batch,
                    )
                    counters.requested_tools = tool_executor.requested_total
                    counters.executed_tools = tool_executor.executed_total
                    counters.deduplicated_tools += tool_batch[
                        "deduplicated_tool_call_count"
                    ]
                    journal.record(
                        "tool_batch_completed",
                        state=f"scenario_{index}_tool_completed",
                        counters=counters,
                        scenario_id=scenario_id,
                    )
                    tool_messages = tuple(
                        KimiToolResultMessage(
                            tool_call_id=item["call_id"],
                            name=item["name"],
                            content=_canonical_bytes(item["result"]).decode("utf-8"),
                        )
                        for item in tool_batch["results"]
                    )
                    final_request = KimiChatRequest(
                        messages=(
                            *_scenario_prompt(scenario_id),
                            first.assistant_message,
                            *tool_messages,
                        ),
                        tools=(_build_tool(),),
                        tool_choice="none",
                        max_completion_tokens=OUTPUT_TOKEN_LIMIT_PER_REQUEST,
                        reasoning_effort="low",
                    )
                    final = await _run_request(
                        final_request,
                        api_key=api_key,
                        counters=counters,
                        journal=journal,
                        scenario_id=scenario_id,
                        phase="final_response",
                        transport_factory=_transport_factory,
                        clock=_now,
                        authorization_expires_at_utc=capability.expires_at_utc,
                        terms_attestation_expires_at_utc=(
                            capability.terms_attestation_expires_at_utc
                        ),
                        pricing_expires_at_utc=capability.pricing_expires_at_utc,
                    )
                    if final.finish_reason != "stop" or final.assistant_message.tool_calls:
                        raise KimiControlledPilotError(
                            "kimi_pilot_final_response_invalid"
                        )
                else:
                    await _run_invalid_request_probe(
                        api_key=api_key,
                        counters=counters,
                        journal=journal,
                        scenario_id=scenario_id,
                        transport_factory=_transport_factory,
                        clock=_now,
                        authorization_expires_at_utc=capability.expires_at_utc,
                        terms_attestation_expires_at_utc=(
                            capability.terms_attestation_expires_at_utc
                        ),
                        pricing_expires_at_utc=(
                            capability.pricing_expires_at_utc
                        ),
                    )
                    if tool_executor.executed_total != counters.executed_tools:
                        raise KimiControlledPilotError(
                            "kimi_pilot_unexpected_tool_execution"
                        )
                scenarios_completed += 1
                journal.scenarios_completed = scenarios_completed
                journal.record(
                    "scenario_completed",
                    state=f"scenario_{index}_completed",
                    counters=counters,
                    scenario_id=scenario_id,
                )

        terminal_now = _clock_now(_now)
        _require_authorized_instant(
            terminal_now,
            authorization_expires_at_utc=capability.expires_at_utc,
            terms_attestation_expires_at_utc=(
                capability.terms_attestation_expires_at_utc
            ),
            pricing_expires_at_utc=capability.pricing_expires_at_utc,
        )
        journal.completed_at_utc = _timestamp(terminal_now)
        journal.record("run_completed", state="completed", counters=counters)
        receipt = _receipt(
            status="completed",
            contract_sha256=contract_sha256,
            candidate_id=journal.candidate_id,
            candidate_commitment_sha256=(
                journal.candidate_commitment_sha256
            ),
            chat_contract_sha256=journal.chat_contract_sha256,
            legacy_successor_tombstone_sha256=(
                journal.legacy_successor_tombstone_sha256
            ),
            authorization_id_sha256=auth_hash,
            terms_attestation=journal.terms_attestation,
            terms_attestation_sha256=journal.terms_attestation_sha256,
            terms_authorization_binding_sha256=(
                journal.terms_authorization_binding_sha256
            ),
            terms_attestation_expires_at_utc=(
                journal.terms_attestation_expires_at_utc
            ),
            pricing_attestation=journal.pricing_attestation,
            pricing_attestation_sha256=journal.pricing_attestation_sha256,
            pricing_authorization_binding_sha256=(
                journal.pricing_authorization_binding_sha256
            ),
            pricing_expires_at_utc=journal.pricing_expires_at_utc,
            authorization_expires_at_utc=journal.authorization_expires_at_utc,
            checked_at_utc=checked_at,
            completed_at_utc=journal.completed_at_utc,
            state="completed",
            scenarios_completed=scenarios_completed,
            counters=counters,
            usage_based_estimated_cost_cny=(
                _money(counters.usage_based_estimated_cost)
                if counters.usage_complete
                else None
            ),
            outcome_unknown=False,
            error_code=None,
            event_count=len(journal.events),
            chain_head=journal.events[-1]["event_hash"],
        )
        journal.write_receipt(receipt)
        return receipt
    except asyncio.CancelledError:
        _finalize_failure(
            journal,
            counters,
            checked_at=checked_at,
            scenarios_completed=scenarios_completed,
            status="outcome_unknown",
            error_code="kimi_pilot_cancelled",
            outcome_unknown=True,
            now=_clock_now(_now),
        )
        raise
    except TimeoutError:
        return _finalize_failure(
            journal,
            counters,
            checked_at=checked_at,
            scenarios_completed=scenarios_completed,
            status="outcome_unknown",
            error_code="kimi_pilot_run_timeout",
            outcome_unknown=True,
            now=_clock_now(_now),
        )
    except KimiChatTransportError as exc:
        return _finalize_failure(
            journal,
            counters,
            checked_at=checked_at,
            scenarios_completed=scenarios_completed,
            status="outcome_unknown" if exc.outcome_unknown else "failed",
            error_code=exc.code,
            outcome_unknown=exc.outcome_unknown,
            now=_clock_now(_now),
        )
    except KimiControlledPilotError as exc:
        return _finalize_failure(
            journal,
            counters,
            checked_at=checked_at,
            scenarios_completed=scenarios_completed,
            status="outcome_unknown" if exc.outcome_unknown else "failed",
            error_code=exc.code,
            outcome_unknown=(exc.outcome_unknown or journal.outcome_unknown),
            now=_clock_now(_now),
        )
    except Exception:
        return _finalize_failure(
            journal,
            counters,
            checked_at=checked_at,
            scenarios_completed=scenarios_completed,
            status="failed",
            error_code="kimi_pilot_failed",
            outcome_unknown=journal.outcome_unknown,
            now=_clock_now(_now),
        )
    finally:
        api_key = None


async def _run_request(
    request: KimiChatRequest,
    *,
    api_key: str,
    counters: _Counters,
    journal: _Journal,
    scenario_id: str,
    phase: str,
    transport_factory: Callable[[], httpx.AsyncBaseTransport] | None,
    clock: Callable[[], datetime],
    authorization_expires_at_utc: datetime,
    terms_attestation_expires_at_utc: datetime,
    pricing_expires_at_utc: datetime,
) -> KimiChatResponse:
    _require_authorized_instant(
        _clock_now(clock),
        authorization_expires_at_utc=authorization_expires_at_utc,
        terms_attestation_expires_at_utc=terms_attestation_expires_at_utc,
        pricing_expires_at_utc=pricing_expires_at_utc,
    )
    body_size = len(request.to_body_bytes())
    if body_size > REQUEST_BODY_LIMIT_BYTES:
        raise KimiControlledPilotError("kimi_pilot_request_body_limit")
    counters.reserve_request()
    journal.record(
        "request_started",
        state=f"{phase}_in_flight",
        counters=counters,
        scenario_id=scenario_id,
        outcome_unknown=True,
    )
    try:
        response = await run_kimi_chat_completion_v2(
            request,
            api_key=api_key,
            confirm_online=True,
            _transport_factory=transport_factory,
        )
    except KimiChatTransportError as exc:
        try:
            counters.observe_error(exc)
        except KimiControlledPilotError as guard_error:
            journal.record(
                "request_postguard_failed",
                state=f"{phase}_postguard_failed",
                counters=counters,
                scenario_id=scenario_id,
                outcome_unknown=exc.outcome_unknown,
                error_code=guard_error.code,
            )
            raise KimiControlledPilotError(
                guard_error.code,
                outcome_unknown=exc.outcome_unknown,
            ) from guard_error
        journal.record(
            "request_failed",
            state=f"{phase}_failed",
            counters=counters,
            scenario_id=scenario_id,
            outcome_unknown=exc.outcome_unknown,
            error_code=exc.code,
        )
        raise
    try:
        counters.observe_response(response)
    except KimiControlledPilotError as exc:
        journal.record(
            "request_postguard_failed",
            state=f"{phase}_postguard_failed",
            counters=counters,
            scenario_id=scenario_id,
            outcome_unknown=False,
            error_code=exc.code,
        )
        raise
    journal.record(
        "request_completed",
        state=f"{phase}_completed",
        counters=counters,
        scenario_id=scenario_id,
    )
    return response


async def _run_invalid_request_probe(
    *,
    api_key: str,
    counters: _Counters,
    journal: _Journal,
    scenario_id: str,
    transport_factory: Callable[[], httpx.AsyncBaseTransport] | None,
    clock: Callable[[], datetime],
    authorization_expires_at_utc: datetime,
    terms_attestation_expires_at_utc: datetime,
    pricing_expires_at_utc: datetime,
) -> KimiInvalidRequestProbeResult:
    _require_authorized_instant(
        _clock_now(clock),
        authorization_expires_at_utc=authorization_expires_at_utc,
        terms_attestation_expires_at_utc=terms_attestation_expires_at_utc,
        pricing_expires_at_utc=pricing_expires_at_utc,
    )
    counters.reserve_request()
    journal.record(
        "invalid_request_probe_started",
        state="expected_invalid_request_in_flight",
        counters=counters,
        scenario_id=scenario_id,
        outcome_unknown=True,
    )
    try:
        result = await run_kimi_invalid_request_probe(
            api_key=api_key,
            confirm_online=True,
            _transport_factory=transport_factory,
        )
    except KimiChatTransportError as exc:
        counters.observe_error(exc)
        journal.record(
            "invalid_request_probe_failed",
            state="expected_invalid_request_failed",
            counters=counters,
            scenario_id=scenario_id,
            outcome_unknown=exc.outcome_unknown,
            error_code=exc.code,
        )
        raise
    counters.observe_expected_invalid_request(result)
    journal.record(
        "expected_invalid_request_observed",
        state="expected_invalid_request_completed",
        counters=counters,
        scenario_id=scenario_id,
        error_code="kimi_chat_invalid_request",
    )
    return result


def _finalize_failure(
    journal: _Journal,
    counters: _Counters,
    *,
    checked_at: str,
    scenarios_completed: int,
    status: str,
    error_code: str,
    outcome_unknown: bool,
    now: datetime,
) -> dict[str, Any]:
    if journal.events and journal.events[-1].get("event_type") in {
        "run_completed",
        "run_terminal",
    }:
        raise KimiControlledPilotError(
            "kimi_pilot_artifact_io_failed",
            outcome_unknown=outcome_unknown,
        )
    authorization_expires_at_utc = datetime.fromisoformat(
        journal.authorization_expires_at_utc.replace("Z", "+00:00")
    )
    terms_attestation_expires_at_utc = datetime.fromisoformat(
        journal.terms_attestation_expires_at_utc.replace("Z", "+00:00")
    )
    pricing_expires_at_utc = datetime.fromisoformat(
        journal.pricing_expires_at_utc.replace("Z", "+00:00")
    )
    expiry_error = _authorization_instant_error(
        now,
        authorization_expires_at_utc=authorization_expires_at_utc,
        terms_attestation_expires_at_utc=(
            terms_attestation_expires_at_utc
        ),
        pricing_expires_at_utc=pricing_expires_at_utc,
    )
    if expiry_error is not None:
        error_code = expiry_error
        status = "outcome_unknown" if outcome_unknown else "failed"
    journal.scenarios_completed = scenarios_completed
    journal.completed_at_utc = _timestamp(now)
    journal.record(
        "run_terminal",
        state=status,
        counters=counters,
        outcome_unknown=outcome_unknown,
        error_code=error_code,
    )
    receipt = _receipt(
        status=status,
        contract_sha256=journal.contract_sha256,
        candidate_id=journal.candidate_id,
        candidate_commitment_sha256=journal.candidate_commitment_sha256,
        chat_contract_sha256=journal.chat_contract_sha256,
        legacy_successor_tombstone_sha256=(
            journal.legacy_successor_tombstone_sha256
        ),
        authorization_id_sha256=journal.authorization_id_sha256,
        terms_attestation=journal.terms_attestation,
        terms_attestation_sha256=journal.terms_attestation_sha256,
        terms_authorization_binding_sha256=(
            journal.terms_authorization_binding_sha256
        ),
        terms_attestation_expires_at_utc=(
            journal.terms_attestation_expires_at_utc
        ),
        pricing_attestation=journal.pricing_attestation,
        pricing_attestation_sha256=journal.pricing_attestation_sha256,
        pricing_authorization_binding_sha256=(
            journal.pricing_authorization_binding_sha256
        ),
        pricing_expires_at_utc=journal.pricing_expires_at_utc,
        authorization_expires_at_utc=journal.authorization_expires_at_utc,
        checked_at_utc=checked_at,
        completed_at_utc=journal.completed_at_utc,
        state=status,
        scenarios_completed=scenarios_completed,
        counters=counters,
        usage_based_estimated_cost_cny=(
            _money(counters.usage_based_estimated_cost)
            if counters.usage_complete
            else None
        ),
        outcome_unknown=outcome_unknown,
        error_code=error_code,
        event_count=len(journal.events),
        chain_head=journal.events[-1]["event_hash"],
    )
    journal.write_receipt(receipt)
    return receipt


def verify_kimi_controlled_pilot_artifacts(
    project_root: str | Path,
    *,
    authorization_id: str,
    _candidate_validator: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if (
        type(authorization_id) is not str
        or _SAFE_AUTHORIZATION_ID.fullmatch(authorization_id) is None
    ):
        raise KimiControlledPilotError("kimi_pilot_artifact_invalid")
    auth_hash = hashlib.sha256(authorization_id.encode("utf-8")).hexdigest()
    run_directory = _fixed_path(
        project_root, f"{ARTIFACT_SUBDIRECTORY}/{auth_hash}"
    )
    if run_directory.is_symlink() or not run_directory.is_dir():
        raise KimiControlledPilotError("kimi_pilot_artifact_invalid")
    event_path = run_directory / "event_chain.jsonl"
    checkpoint_path = run_directory / "checkpoint.json"
    receipt_path = run_directory / "receipt.json"
    artifact_paths = (event_path, checkpoint_path, receipt_path)
    if any(path.is_symlink() or not path.is_file() for path in artifact_paths):
        raise KimiControlledPilotError("kimi_pilot_artifact_invalid")
    events: list[dict[str, Any]] = []
    previous: str | None = None
    try:
        event_lines = event_path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(event_lines, start=1):
            event = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
            if not isinstance(event, dict) or set(event) != _EVENT_FIELDS:
                raise KimiControlledPilotError("kimi_pilot_artifact_invalid")
            if event.get("schema_version") != EVENT_SCHEMA_VERSION:
                raise KimiControlledPilotError("kimi_pilot_artifact_invalid")
            event_hash = event["event_hash"]
            if (
                type(event_hash) is not str
                or _SHA256.fullmatch(event_hash) is None
                or event.get("sequence") != index
                or event.get("prev_hash") != previous
            ):
                raise KimiControlledPilotError("kimi_pilot_hash_chain_invalid")
            unhashed_event = dict(event)
            del unhashed_event["event_hash"]
            expected = hashlib.sha256(_canonical_bytes(unhashed_event)).hexdigest()
            if event_hash != expected:
                raise KimiControlledPilotError("kimi_pilot_hash_chain_invalid")
            previous = event_hash
            events.append(event)
    except KimiControlledPilotError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise KimiControlledPilotError("kimi_pilot_artifact_invalid") from exc
    checkpoint = _load_artifact_json(checkpoint_path)
    receipt = _load_artifact_json(receipt_path)
    if set(checkpoint) != _CHECKPOINT_FIELDS or set(receipt) != set(RECEIPT_FIELDS):
        raise KimiControlledPilotError("kimi_pilot_artifact_invalid")
    current_contract_sha256 = _current_contract_sha256_for_verification(
        Path(project_root)
    )
    if (
        not events
        or checkpoint["authorization_id_sha256"] != auth_hash
        or receipt["authorization_id_sha256"] != auth_hash
        or checkpoint["event_count"] != len(events)
        or receipt["event_count"] != len(events)
        or checkpoint["event_chain_head_sha256"] != previous
        or receipt["event_chain_head_sha256"] != previous
        or checkpoint["state"] != receipt["state"]
        or checkpoint["contract_sha256"] != current_contract_sha256
        or receipt["contract_sha256"] != current_contract_sha256
    ):
        raise KimiControlledPilotError("kimi_pilot_artifact_invalid")
    _validate_authoritative_candidate(
        Path(project_root).resolve(),
        candidate_id=receipt.get("candidate_id"),
        candidate_commitment_sha256=receipt.get(
            "candidate_commitment_sha256"
        ),
        candidate_validator=_candidate_validator,
    )
    tombstone_path = _validate_legacy_successor_tombstone(
        Path(project_root).resolve(),
        authorization_id_sha256=auth_hash,
        contract_sha256=current_contract_sha256,
        receipt=receipt,
    )
    _validate_artifact_projection(
        events=events,
        authorization_id_sha256=auth_hash,
        contract_sha256=current_contract_sha256,
        checkpoint=checkpoint,
        receipt=receipt,
    )
    _assert_sanitized_artifacts(
        event_path, checkpoint_path, receipt_path, tombstone_path
    )
    return {
        "status": "valid",
        "authorization_id_sha256": auth_hash,
        "event_count": len(events),
        "event_chain_head_sha256": previous,
        "network_calls": 0,
    }


def _validate_legacy_successor_tombstone(
    project_root: Path,
    *,
    authorization_id_sha256: str,
    contract_sha256: str,
    receipt: Mapping[str, Any],
) -> Path:
    path = _fixed_path(
        project_root,
        (
            f"{LEGACY_ARTIFACT_SUBDIRECTORY}/"
            f"{authorization_id_sha256}/{SUCCESSOR_TOMBSTONE_FILENAME}"
        ),
    )
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024:
            raise ValueError("invalid tombstone path")
        raw = path.read_bytes()
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        authorization_expiry = datetime.fromisoformat(
            str(receipt.get("authorization_expires_at_utc")).replace(
                "Z", "+00:00"
            )
        )
        created_at = datetime.fromisoformat(
            str(receipt.get("checked_at_utc")).replace("Z", "+00:00")
        )
        expected = _successor_tombstone_payload(
            authorization_id_sha256=authorization_id_sha256,
            contract_sha256=contract_sha256,
            candidate_id=receipt.get("candidate_id"),
            candidate_commitment_sha256=receipt.get(
                "candidate_commitment_sha256"
            ),
            chat_contract_sha256=receipt.get("chat_contract_sha256"),
            authorization_expires_at_utc=authorization_expiry,
            created_at_utc=created_at,
        )
        expected_raw = _canonical_bytes(expected) + b"\n"
    except Exception as exc:
        raise KimiControlledPilotError(
            "kimi_pilot_legacy_tombstone_invalid"
        ) from exc
    if (
        payload != expected
        or raw != expected_raw
        or hashlib.sha256(raw).hexdigest()
        != receipt.get("legacy_successor_tombstone_sha256")
    ):
        raise KimiControlledPilotError(
            "kimi_pilot_legacy_tombstone_invalid"
        )
    return path


def _current_contract_sha256_for_verification(project_root: Path) -> str:
    try:
        contract_path = _fixed_path(
            project_root, "evals/v2/kimi_controlled_pilot_contract_v2.json"
        )
        chat_contract_path = _fixed_path(
            project_root, "evals/v2/kimi_chat_completions_contract_v2.json"
        )
        scenarios_path = _fixed_path(
            project_root, "evals/v2/kimi_controlled_pilot_scenarios_v1.json"
        )
        terms_path = _fixed_path(
            project_root, "evals/v2/kimi_terms_g2a_provisional_contract.json"
        )
        if (
            _load_json(contract_path) != kimi_controlled_pilot_contract()
            or not chat_contract_path.is_file()
            or chat_contract_path.is_symlink()
            or hashlib.sha256(chat_contract_path.read_bytes()).hexdigest()
            != _CHAT_CONTRACT_SHA256
            or hashlib.sha256(scenarios_path.read_bytes()).hexdigest()
            != _SCENARIOS_SHA256
            or hashlib.sha256(terms_path.read_bytes()).hexdigest()
            != _TERMS_SHA256
        ):
            raise KimiControlledPilotError("kimi_pilot_artifact_invalid")
        return hashlib.sha256(contract_path.read_bytes()).hexdigest()
    except KimiControlledPilotError as exc:
        raise KimiControlledPilotError("kimi_pilot_artifact_invalid") from exc
    except OSError as exc:
        raise KimiControlledPilotError("kimi_pilot_artifact_invalid") from exc


@dataclass(frozen=True, slots=True)
class _EventSpec:
    event_type: str
    state: str
    scenario_id: str | None
    scenarios_completed: int
    delta_kind: str
    outcome_unknown: bool = False
    error_code: str | None = None


_S1: Final = "KIMI-SYNTH-TOOL-001"
_S2: Final = "KIMI-SYNTH-TOOL-002"
_S3: Final = "KIMI-SYNTH-PROVIDER-003"
_SUCCESS_EVENT_TEMPLATE: Final = (
    _EventSpec("run_authorized", "authorized", None, 0, "zero"),
    _EventSpec("request_started", "tool_request_in_flight", _S1, 0, "reserve", True),
    _EventSpec("request_completed", "tool_request_completed", _S1, 0, "usage"),
    _EventSpec("tool_batch_completed", "scenario_1_tool_completed", _S1, 0, "tool"),
    _EventSpec("request_started", "final_response_in_flight", _S1, 0, "reserve", True),
    _EventSpec("request_completed", "final_response_completed", _S1, 0, "usage"),
    _EventSpec("scenario_completed", "scenario_1_completed", _S1, 1, "scenario"),
    _EventSpec("request_started", "tool_request_in_flight", _S2, 1, "reserve", True),
    _EventSpec("request_completed", "tool_request_completed", _S2, 1, "usage"),
    _EventSpec("tool_batch_completed", "scenario_2_tool_completed", _S2, 1, "tool"),
    _EventSpec("request_started", "final_response_in_flight", _S2, 1, "reserve", True),
    _EventSpec("request_completed", "final_response_completed", _S2, 1, "usage"),
    _EventSpec("scenario_completed", "scenario_2_completed", _S2, 2, "scenario"),
    _EventSpec(
        "invalid_request_probe_started",
        "expected_invalid_request_in_flight",
        _S3,
        2,
        "reserve",
        True,
    ),
    _EventSpec(
        "expected_invalid_request_observed",
        "expected_invalid_request_completed",
        _S3,
        2,
        "invalid_observed",
        False,
        "kimi_chat_invalid_request",
    ),
    _EventSpec("scenario_completed", "scenario_3_completed", _S3, 3, "scenario"),
    _EventSpec("run_completed", "completed", None, 3, "terminal_success"),
)
_EVENT_INTEGER_COUNTER_FIELDS: Final = (
    "scenarios_completed",
    "model_request_count",
    "network_attempts",
    "network_calls",
    "requested_tool_call_count",
    "deduplicated_tool_call_count",
    "executed_tool_call_count",
    "expected_invalid_request_count",
    "input_tokens",
    "output_tokens",
    "reserved_input_tokens",
    "reserved_output_tokens",
    "usage_observed_request_count",
)
_MONOTONIC_COUNTER_FIELDS: Final = tuple(
    field for field in _EVENT_INTEGER_COUNTER_FIELDS if field != "scenarios_completed"
)
_TERMINAL_EXPIRY_ERRORS: Final = frozenset(
    {
        "kimi_pilot_terms_expired",
        "kimi_pilot_capability_expired",
        "kimi_pilot_terms_attestation_stale",
        "kimi_pilot_pricing_attestation_stale",
    }
)


def _fsm_invalid() -> None:
    raise KimiControlledPilotError("kimi_pilot_event_fsm_invalid")


def _event_cost_invariant(event: Mapping[str, Any]) -> None:
    if any(
        type(event.get(field)) is not int or event[field] < 0
        for field in _EVENT_INTEGER_COUNTER_FIELDS
    ):
        _fsm_invalid()
    requests = event["model_request_count"]
    if (
        event["network_attempts"] != requests
        or event["reserved_input_tokens"]
        != requests * INPUT_TOKEN_LIMIT_PER_REQUEST
        or event["reserved_output_tokens"]
        != requests * OUTPUT_TOKEN_LIMIT_PER_REQUEST
        or event["reserved_estimated_cost_cny"]
        != _money(
            Decimal(requests)
            * _cost(INPUT_TOKEN_LIMIT_PER_REQUEST, OUTPUT_TOKEN_LIMIT_PER_REQUEST)
        )
        or type(event.get("usage_complete")) is not bool
    ):
        _fsm_invalid()
    expected_usage_cost = _money(
        _cost(event["input_tokens"], event["output_tokens"])
    )
    if event["usage_complete"]:
        if event.get("usage_based_estimated_cost_cny") != expected_usage_cost:
            _fsm_invalid()
    elif event.get("usage_based_estimated_cost_cny") is not None:
        _fsm_invalid()
    if (
        event["network_calls"] > event["network_attempts"]
        or event["usage_observed_request_count"] > event["network_calls"]
        or event["deduplicated_tool_call_count"]
        + event["executed_tool_call_count"]
        > event["requested_tool_call_count"]
    ):
        _fsm_invalid()


def _same_except(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    allowed: frozenset[str],
) -> None:
    fields = set(_MONOTONIC_COUNTER_FIELDS) | {
        "scenarios_completed",
        "usage_complete",
        "usage_based_estimated_cost_cny",
        "reserved_estimated_cost_cny",
    }
    if any(
        previous.get(field) != current.get(field)
        for field in fields - set(allowed)
    ):
        _fsm_invalid()


def _validate_event_delta(
    kind: str,
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> None:
    _event_cost_invariant(current)
    if previous is None:
        if kind != "zero" or any(
            current[field] != 0 for field in _EVENT_INTEGER_COUNTER_FIELDS
        ) or current["reserved_estimated_cost_cny"] != "0.000000":
            _fsm_invalid()
        return
    _event_cost_invariant(previous)
    if any(
        current[field] < previous[field] for field in _MONOTONIC_COUNTER_FIELDS
    ) or (not previous["usage_complete"] and current["usage_complete"]):
        _fsm_invalid()

    if kind == "reserve":
        _same_except(
            previous,
            current,
            frozenset(
                {
                    "model_request_count",
                    "network_attempts",
                    "reserved_input_tokens",
                    "reserved_output_tokens",
                    "reserved_estimated_cost_cny",
                }
            ),
        )
        if (
            current["model_request_count"]
            != previous["model_request_count"] + 1
            or current["network_attempts"]
            != previous["network_attempts"] + 1
        ):
            _fsm_invalid()
    elif kind == "usage":
        _same_except(
            previous,
            current,
            frozenset(
                {
                    "network_calls",
                    "usage_observed_request_count",
                    "input_tokens",
                    "output_tokens",
                    "usage_based_estimated_cost_cny",
                }
            ),
        )
        if (
            current["network_calls"] != previous["network_calls"] + 1
            or current["usage_observed_request_count"]
            != previous["usage_observed_request_count"] + 1
            or current["usage_complete"] is not True
        ):
            _fsm_invalid()
    elif kind == "tool":
        _same_except(
            previous,
            current,
            frozenset(
                {
                    "requested_tool_call_count",
                    "executed_tool_call_count",
                }
            ),
        )
        if (
            current["requested_tool_call_count"]
            != previous["requested_tool_call_count"] + 1
            or current["executed_tool_call_count"]
            != previous["executed_tool_call_count"] + 1
            or current["deduplicated_tool_call_count"]
            != previous["deduplicated_tool_call_count"]
        ):
            _fsm_invalid()
    elif kind == "invalid_observed":
        _same_except(
            previous,
            current,
            frozenset({"network_calls", "expected_invalid_request_count"}),
        )
        if (
            current["network_calls"] != previous["network_calls"] + 1
            or current["expected_invalid_request_count"]
            != previous["expected_invalid_request_count"] + 1
        ):
            _fsm_invalid()
    elif kind == "scenario":
        _same_except(previous, current, frozenset({"scenarios_completed"}))
        if current["scenarios_completed"] != previous["scenarios_completed"] + 1:
            _fsm_invalid()
    elif kind in {"terminal_success", "terminal_failure"}:
        _same_except(previous, current, frozenset())
    elif kind == "request_failed":
        allowed = {
            "network_calls",
            "usage_observed_request_count",
            "input_tokens",
            "output_tokens",
            "usage_complete",
            "usage_based_estimated_cost_cny",
        }
        _same_except(previous, current, frozenset(allowed))
        network_delta = current["network_calls"] - previous["network_calls"]
        usage_delta = (
            current["usage_observed_request_count"]
            - previous["usage_observed_request_count"]
        )
        if network_delta not in {0, 1} or usage_delta not in {0, 1}:
            _fsm_invalid()
        if usage_delta == 1 and network_delta != 1:
            _fsm_invalid()
        if usage_delta == 0 and (
            current["input_tokens"] != previous["input_tokens"]
            or current["output_tokens"] != previous["output_tokens"]
        ):
            _fsm_invalid()
    elif kind == "postguard_failed":
        _validate_event_delta("usage", previous, current)
    else:
        _fsm_invalid()


def _validate_event_state_machine(
    events: list[Mapping[str, Any]],
    *,
    checkpoint: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> None:
    if not events:
        _fsm_invalid()
    immutable_fields = (
        "candidate_id",
        "candidate_commitment_sha256",
        "predecessor_candidate_commitment_sha256",
        "chat_contract_sha256",
        "legacy_successor_tombstone_sha256",
        "terms_attestation_sha256",
        "terms_authorization_binding_sha256",
        "terms_attestation_expires_at_utc",
        "pricing_attestation_sha256",
        "pricing_authorization_binding_sha256",
        "pricing_expires_at_utc",
        "authorization_schema_version",
        "authorization_expires_at_utc",
        "checked_at_utc",
    )
    if any(
        event.get(field) != receipt.get(field)
        for event in events
        for field in immutable_fields
    ):
        _fsm_invalid()

    cursor = 0
    previous: Mapping[str, Any] | None = None
    failure_marker: Mapping[str, Any] | None = None
    terminal_seen = False
    for index, event in enumerate(events):
        if terminal_seen:
            _fsm_invalid()
        event_type = event.get("event_type")
        if failure_marker is not None and event_type != "run_terminal":
            _fsm_invalid()
        if event_type == "run_terminal":
            if index != len(events) - 1 or cursor == 0 or previous is None:
                _fsm_invalid()
            if (
                event.get("state") not in {"failed", "outcome_unknown"}
                or event.get("state") != receipt.get("status")
                or event.get("scenario_id") is not None
                or event.get("outcome_unknown")
                is not (event.get("state") == "outcome_unknown")
                or type(event.get("error_code")) is not str
                or not event.get("error_code")
                or event.get("completed_at_utc") != receipt.get("completed_at_utc")
            ):
                _fsm_invalid()
            if failure_marker is not None and (
                event.get("error_code") != failure_marker.get("error_code")
                and event.get("error_code") not in _TERMINAL_EXPIRY_ERRORS
            ):
                _fsm_invalid()
            _validate_event_delta("terminal_failure", previous, event)
            terminal_seen = True
            previous = event
            continue

        if cursor >= len(_SUCCESS_EVENT_TEMPLATE):
            _fsm_invalid()
        expected = _SUCCESS_EVENT_TEMPLATE[cursor]
        is_exact = (
            event_type == expected.event_type
            and event.get("state") == expected.state
            and event.get("scenario_id") == expected.scenario_id
            and event.get("scenarios_completed") == expected.scenarios_completed
        )
        if is_exact:
            if (
                event.get("outcome_unknown") is not expected.outcome_unknown
                or event.get("error_code") != expected.error_code
                or (
                    expected.event_type != "run_completed"
                    and event.get("completed_at_utc") is not None
                )
                or (
                    expected.event_type == "run_completed"
                    and event.get("completed_at_utc")
                    != receipt.get("completed_at_utc")
                )
            ):
                _fsm_invalid()
            _validate_event_delta(expected.delta_kind, previous, event)
            cursor += 1
            previous = event
            continue

        if previous is None:
            _fsm_invalid()
        if (
            expected.event_type == "request_completed"
            and previous.get("event_type") == "request_started"
            and event_type in {"request_failed", "request_postguard_failed"}
        ):
            suffix = (
                "_postguard_failed"
                if event_type == "request_postguard_failed"
                else "_failed"
            )
            expected_state = str(previous.get("state")).removesuffix(
                "_in_flight"
            ) + suffix
            if (
                event.get("state") != expected_state
                or event.get("scenario_id") != previous.get("scenario_id")
                or event.get("scenarios_completed")
                != previous.get("scenarios_completed")
                or type(event.get("error_code")) is not str
                or not event.get("error_code")
                or event.get("completed_at_utc") is not None
            ):
                _fsm_invalid()
            delta_kind = (
                "postguard_failed"
                if event_type == "request_postguard_failed"
                else "request_failed"
            )
            _validate_event_delta(delta_kind, previous, event)
            failure_marker = event
            previous = event
            continue
        if (
            expected.event_type == "expected_invalid_request_observed"
            and previous.get("event_type") == "invalid_request_probe_started"
            and event_type == "invalid_request_probe_failed"
        ):
            if (
                event.get("state") != "expected_invalid_request_failed"
                or event.get("scenario_id") != _S3
                or event.get("scenarios_completed") != 2
                or type(event.get("error_code")) is not str
                or not event.get("error_code")
                or event.get("completed_at_utc") is not None
            ):
                _fsm_invalid()
            _validate_event_delta("request_failed", previous, event)
            failure_marker = event
            previous = event
            continue
        _fsm_invalid()

    status = receipt.get("status")
    if status == "completed":
        if (
            terminal_seen
            or cursor != len(_SUCCESS_EVENT_TEMPLATE)
            or len(events) != len(_SUCCESS_EVENT_TEMPLATE)
            or events[-1].get("event_type") != "run_completed"
        ):
            _fsm_invalid()
    elif status in {"failed", "outcome_unknown"}:
        if not terminal_seen or events[-1].get("event_type") != "run_terminal":
            _fsm_invalid()
    else:
        _fsm_invalid()

    if checkpoint.get("scenario_index") not in range(0, SCENARIO_COUNT + 1):
        _fsm_invalid()
def _validate_artifact_projection(
    *,
    events: list[Mapping[str, Any]],
    authorization_id_sha256: str,
    contract_sha256: str,
    checkpoint: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> None:
    last_event = events[-1]
    shared_fields = (
        "state",
        "authorization_schema_version",
        "authorization_expires_at_utc",
        "terms_attestation_expires_at_utc",
        "pricing_expires_at_utc",
        "checked_at_utc",
        "completed_at_utc",
        "scenarios_completed",
        "model_request_count",
        "network_attempts",
        "network_calls",
        "requested_tool_call_count",
        "deduplicated_tool_call_count",
        "executed_tool_call_count",
        "expected_invalid_request_count",
        "input_tokens",
        "output_tokens",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "reserved_estimated_cost_cny",
        "usage_observed_request_count",
        "usage_complete",
        "usage_based_estimated_cost_cny",
        "outcome_unknown",
        "error_code",
    )
    if any(
        checkpoint.get(field) != last_event.get(field)
        or receipt.get(field) != checkpoint.get(field)
        for field in shared_fields
    ):
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")

    candidate_id = receipt.get("candidate_id")
    candidate_commitment_sha256 = receipt.get(
        "candidate_commitment_sha256"
    )
    chat_contract_sha256 = receipt.get("chat_contract_sha256")
    try:
        authorization_expires_at_utc = datetime.fromisoformat(
            str(receipt.get("authorization_expires_at_utc")).replace(
                "Z", "+00:00"
            )
        )
        canonical_authorization_expiry = _canonical_authorization_expiry(
            authorization_expires_at_utc
        )
    except (TypeError, ValueError) as exc:
        raise KimiControlledPilotError(
            "kimi_pilot_artifact_projection_invalid"
        ) from exc
    try:
        _validate_successor_binding(
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment_sha256,
            chat_contract_sha256=chat_contract_sha256,
        )
    except ValueError as exc:
        raise KimiControlledPilotError(
            "kimi_pilot_artifact_projection_invalid"
        ) from exc
    successor_fields = (
        "candidate_id",
        "candidate_commitment_sha256",
        "predecessor_candidate_commitment_sha256",
        "chat_contract_sha256",
        "legacy_successor_tombstone_sha256",
    )
    if (
        receipt.get("predecessor_candidate_commitment_sha256")
        != PREDECESSOR_CANDIDATE_COMMITMENT_SHA256
        or receipt.get("authorization_schema_version")
        != AUTHORIZATION_SCHEMA_VERSION
        or receipt.get("authorization_expires_at_utc")
        != canonical_authorization_expiry
        or type(receipt.get("legacy_successor_tombstone_sha256")) is not str
        or _SHA256.fullmatch(
            receipt.get("legacy_successor_tombstone_sha256", "")
        )
        is None
        or any(
            checkpoint.get(field) != receipt.get(field)
            or any(event.get(field) != receipt.get(field) for event in events)
            for field in successor_fields
        )
    ):
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")

    if (
        receipt.get("terms_attestation") != checkpoint.get("terms_attestation")
        or receipt.get("terms_attestation_sha256")
        != checkpoint.get("terms_attestation_sha256")
        or receipt.get("terms_authorization_binding_sha256")
        != checkpoint.get("terms_authorization_binding_sha256")
        or receipt.get("terms_attestation_max_age_seconds")
        != checkpoint.get("terms_attestation_max_age_seconds")
        or receipt.get("pricing_attestation")
        != checkpoint.get("pricing_attestation")
        or receipt.get("pricing_attestation_sha256")
        != checkpoint.get("pricing_attestation_sha256")
        or receipt.get("pricing_authorization_binding_sha256")
        != checkpoint.get("pricing_authorization_binding_sha256")
        or receipt.get("pricing_attestation_max_age_seconds")
        != checkpoint.get("pricing_attestation_max_age_seconds")
    ):
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")
    attestation, attestation_sha256 = _validate_persisted_terms_attestation(
        receipt.get("terms_attestation"),
        authorization_id_sha256=authorization_id_sha256,
        contract_sha256=contract_sha256,
        candidate_id=candidate_id,
        candidate_commitment_sha256=candidate_commitment_sha256,
        chat_contract_sha256=chat_contract_sha256,
        authorization_expires_at_utc=authorization_expires_at_utc,
    )
    binding_sha256 = attestation.authorization_binding_sha256
    pricing_attestation, pricing_attestation_sha256 = (
        _validate_persisted_pricing_attestation(
            receipt.get("pricing_attestation"),
            authorization_id_sha256=authorization_id_sha256,
            contract_sha256=contract_sha256,
            candidate_id=candidate_id,
            candidate_commitment_sha256=candidate_commitment_sha256,
            chat_contract_sha256=chat_contract_sha256,
            authorization_expires_at_utc=authorization_expires_at_utc,
            terms_attestation_sha256=attestation_sha256,
        )
    )
    pricing_binding_sha256 = pricing_attestation.authorization_binding_sha256
    expected_terms_attestation_expires_at_utc = _timestamp(
        attestation.retrieved_at_utc.astimezone(timezone.utc)
        + timedelta(seconds=TERMS_ATTESTATION_MAX_AGE_SECONDS)
    )
    expected_pricing_expires_at_utc = _timestamp(
        pricing_attestation.retrieved_at_utc.astimezone(timezone.utc)
        + timedelta(seconds=PRICING_ATTESTATION_MAX_AGE_SECONDS)
    )
    if (
        receipt.get("terms_attestation_sha256") != attestation_sha256
        or receipt.get("terms_authorization_binding_sha256") != binding_sha256
        or receipt.get("terms_attestation_expires_at_utc")
        != expected_terms_attestation_expires_at_utc
        or any(
            event.get("terms_attestation_sha256") != attestation_sha256
            or event.get("terms_authorization_binding_sha256") != binding_sha256
            or event.get("terms_attestation_expires_at_utc")
            != expected_terms_attestation_expires_at_utc
            for event in events
        )
        or receipt.get("pricing_attestation_sha256")
        != pricing_attestation_sha256
        or receipt.get("pricing_authorization_binding_sha256")
        != pricing_binding_sha256
        or receipt.get("pricing_expires_at_utc")
        != expected_pricing_expires_at_utc
        or any(
            event.get("pricing_attestation_sha256")
            != pricing_attestation_sha256
            or event.get("pricing_authorization_binding_sha256")
            != pricing_binding_sha256
            or event.get("pricing_expires_at_utc")
            != expected_pricing_expires_at_utc
            for event in events
        )
    ):
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")

    _validate_event_state_machine(
        events,
        checkpoint=checkpoint,
        receipt=receipt,
    )

    expected_scenario_ids = (
        "KIMI-SYNTH-TOOL-001",
        "KIMI-SYNTH-TOOL-002",
        "KIMI-SYNTH-PROVIDER-003",
    )
    scenarios_completed = 0
    for event in events:
        if event.get("event_type") == "scenario_completed":
            if (
                scenarios_completed >= SCENARIO_COUNT
                or event.get("scenario_id")
                != expected_scenario_ids[scenarios_completed]
            ):
                raise KimiControlledPilotError(
                    "kimi_pilot_artifact_projection_invalid"
                )
            scenarios_completed += 1
        if event.get("scenarios_completed") != scenarios_completed:
            raise KimiControlledPilotError(
                "kimi_pilot_artifact_projection_invalid"
            )

    if (
        checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
        or checkpoint.get("authorization_id_sha256")
        != authorization_id_sha256
        or checkpoint.get("contract_sha256") != contract_sha256
        or receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or receipt.get("contract_id") != CONTRACT_ID
        or receipt.get("scenario_count") != SCENARIO_COUNT
        or receipt.get("model_request_limit") != MODEL_REQUEST_LIMIT
        or receipt.get("local_estimated_reservation_limit_cny")
        != _money(LOCAL_ESTIMATED_RESERVATION_LIMIT_CNY)
        or receipt.get("actual_billed_cost_cny") is not None
        or receipt.get("all_cache_miss_pricing") is not True
        or receipt.get("local_cost_claim_scope")
        != "local_conservative_hard_stop_only"
        or receipt.get("candidate_result_created") is not False
        or any(
            receipt.get(field) is not False
            for field in RECEIPT_FIELDS
            if field.startswith("authorizes_")
        )
        or checkpoint.get("terms_attestation_max_age_seconds")
        != TERMS_ATTESTATION_MAX_AGE_SECONDS
        or checkpoint.get("pricing_attestation_max_age_seconds")
        != PRICING_ATTESTATION_MAX_AGE_SECONDS
        or events[0].get("event_type") != "run_authorized"
        or receipt.get("scenarios_completed") != scenarios_completed
    ):
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")
    status = receipt.get("status")
    if (
        status not in {"completed", "failed", "outcome_unknown"}
        or receipt.get("state") != status
        or (
            status == "completed"
            and (
                last_event.get("event_type") != "run_completed"
                or receipt.get("scenarios_completed") != SCENARIO_COUNT
                or checkpoint.get("scenario_index") != SCENARIO_COUNT
                or receipt.get("error_code") is not None
                or receipt.get("outcome_unknown") is not False
                or receipt.get("expected_invalid_request_count") != 1
                or sum(
                    event.get("event_type")
                    == "expected_invalid_request_observed"
                    and event.get("scenario_id") == "KIMI-SYNTH-PROVIDER-003"
                    and event.get("error_code") == "kimi_chat_invalid_request"
                    for event in events
                )
                != 1
            )
        )
        or (
            status != "completed"
            and (
                last_event.get("event_type") != "run_terminal"
                or type(receipt.get("error_code")) is not str
                or not receipt.get("error_code")
                or receipt.get("outcome_unknown")
                is not (status == "outcome_unknown")
            )
        )
        or any(
            event.get("event_type") in {"run_completed", "run_terminal"}
            for event in events[:-1]
        )
    ):
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")
    integer_fields = (
        "scenarios_completed",
        "model_request_count",
        "network_attempts",
        "network_calls",
        "requested_tool_call_count",
        "deduplicated_tool_call_count",
        "executed_tool_call_count",
        "expected_invalid_request_count",
        "input_tokens",
        "output_tokens",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "usage_observed_request_count",
    )
    if any(
        type(receipt.get(field)) is not int or receipt[field] < 0
        for field in integer_fields
    ):
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")
    observed_limit_breach = (
        status in {"failed", "outcome_unknown"}
        and receipt.get("error_code") == "kimi_pilot_usage_limit_exceeded"
        and any(
            event.get("event_type") == "request_postguard_failed"
            and event.get("error_code") == "kimi_pilot_usage_limit_exceeded"
            for event in events
        )
    )
    if (
        receipt["network_attempts"] != receipt["model_request_count"]
        or receipt["scenarios_completed"] > SCENARIO_COUNT
        or receipt["model_request_count"] > MODEL_REQUEST_LIMIT
        or receipt["network_calls"] > receipt["network_attempts"]
        or receipt["usage_observed_request_count"] > receipt["network_attempts"]
        or receipt["expected_invalid_request_count"] > 1
        or receipt["usage_observed_request_count"]
        + receipt["expected_invalid_request_count"]
        > receipt["network_attempts"]
        or receipt["executed_tool_call_count"] > TOOL_EXECUTION_LIMIT
        or receipt["deduplicated_tool_call_count"]
        + receipt["executed_tool_call_count"]
        > receipt["requested_tool_call_count"]
        or receipt["reserved_input_tokens"]
        != receipt["model_request_count"] * INPUT_TOKEN_LIMIT_PER_REQUEST
        or receipt["reserved_output_tokens"]
        != receipt["model_request_count"] * OUTPUT_TOKEN_LIMIT_PER_REQUEST
        or receipt["reserved_input_tokens"] > INPUT_TOKEN_LIMIT_TOTAL
        or receipt["reserved_output_tokens"] > OUTPUT_TOKEN_LIMIT_TOTAL
        or (
            (
                receipt["input_tokens"] > INPUT_TOKEN_LIMIT_TOTAL
                or receipt["output_tokens"] > OUTPUT_TOKEN_LIMIT_TOTAL
            )
            and not observed_limit_breach
        )
        or type(checkpoint.get("scenario_index")) is not int
        or checkpoint["scenario_index"] < receipt["scenarios_completed"]
        or checkpoint["scenario_index"]
        > min(receipt["scenarios_completed"] + 1, SCENARIO_COUNT)
    ):
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")
    expected_reservation = _money(
        Decimal(receipt["model_request_count"])
        * _cost(INPUT_TOKEN_LIMIT_PER_REQUEST, OUTPUT_TOKEN_LIMIT_PER_REQUEST)
    )
    if receipt.get("reserved_estimated_cost_cny") != expected_reservation:
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")
    if Decimal(expected_reservation) > LOCAL_ESTIMATED_RESERVATION_LIMIT_CNY:
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")
    usage_estimate = receipt.get("usage_based_estimated_cost_cny")
    if receipt.get("usage_complete") is True:
        if usage_estimate != _money(
            _cost(receipt["input_tokens"], receipt["output_tokens"])
        ):
            raise KimiControlledPilotError(
                "kimi_pilot_artifact_projection_invalid"
            )
        if (
            Decimal(str(usage_estimate))
            > LOCAL_ESTIMATED_RESERVATION_LIMIT_CNY
            and not observed_limit_breach
        ):
            raise KimiControlledPilotError(
                "kimi_pilot_artifact_projection_invalid"
            )
    elif usage_estimate is not None:
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")
    try:
        checked = datetime.fromisoformat(
            str(receipt["checked_at_utc"]).replace("Z", "+00:00")
        )
        completed = datetime.fromisoformat(
            str(receipt["completed_at_utc"]).replace("Z", "+00:00")
        )
        authorization_expires = datetime.fromisoformat(
            str(receipt["authorization_expires_at_utc"]).replace(
                "Z", "+00:00"
            )
        )
        terms_attestation_expires = datetime.fromisoformat(
            str(receipt["terms_attestation_expires_at_utc"]).replace(
                "Z", "+00:00"
            )
        )
        pricing_expires = datetime.fromisoformat(
            str(receipt["pricing_expires_at_utc"]).replace("Z", "+00:00")
        )
        attested = attestation.retrieved_at_utc
        pricing_attested = pricing_attestation.retrieved_at_utc
        if (
            checked.tzinfo is None
            or checked.utcoffset() is None
            or completed.tzinfo is None
            or completed.utcoffset() is None
            or authorization_expires.tzinfo is None
            or authorization_expires.utcoffset() is None
            or terms_attestation_expires.tzinfo is None
            or terms_attestation_expires.utcoffset() is None
            or pricing_expires.tzinfo is None
            or pricing_expires.utcoffset() is None
        ):
            raise ValueError("naive artifact timestamp")
    except (TypeError, ValueError) as exc:
        raise KimiControlledPilotError(
            "kimi_pilot_artifact_projection_invalid"
        ) from exc
    age = (checked - attested).total_seconds()
    pricing_age = (checked - pricing_attested).total_seconds()
    if (
        completed < checked
        or age < 0
        or age > TERMS_ATTESTATION_MAX_AGE_SECONDS
        or terms_attestation_expires
        != attested.astimezone(timezone.utc)
        + timedelta(seconds=TERMS_ATTESTATION_MAX_AGE_SECONDS)
        or pricing_age < 0
        or pricing_age > PRICING_ATTESTATION_MAX_AGE_SECONDS
        or pricing_expires
        != pricing_attested.astimezone(timezone.utc)
        + timedelta(seconds=PRICING_ATTESTATION_MAX_AGE_SECONDS)
        or checked + timedelta(seconds=RUN_TIMEOUT_SECONDS)
        > min(
            _terms_expiry_utc(),
            authorization_expires,
            terms_attestation_expires,
            pricing_expires,
        )
        or completed >= _terms_expiry_utc()
        or completed >= authorization_expires
        or completed >= terms_attestation_expires
        or completed >= pricing_expires
    ):
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")


def _validate_persisted_terms_attestation(
    raw: Any,
    *,
    authorization_id_sha256: str,
    contract_sha256: str,
    candidate_id: str,
    candidate_commitment_sha256: str,
    chat_contract_sha256: str,
    authorization_expires_at_utc: datetime,
) -> tuple[KimiTermsAttestation, str]:
    expected_fields = {
        "schema_version",
        "retrieved_at_utc",
        "source_urls",
        "displayed_updated_dates",
        "displayed_effective_dates",
        "canonical_text_sha256",
        "canonicalization_version",
        "material_delta_review_status",
        "authorization_binding_sha256",
    }
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")
    if (
        raw.get("schema_version") != "kimi-terms-attestation/1.0"
        or raw.get("source_urls") != _TERMS_SOURCE_URLS
        or raw.get("displayed_updated_dates")
        != {
            "service_agreement": TERMS_SERVICE_AGREEMENT_UPDATED_DATE,
            "privacy_policy": TERMS_PRIVACY_POLICY_UPDATED_DATE,
            "payment_agreement": TERMS_PAYMENT_AGREEMENT_UPDATED_DATE,
        }
        or raw.get("displayed_effective_dates")
        != {
            source_id: TERMS_DISPLAYED_EFFECTIVE_DATE
            for source_id in _TERMS_SOURCE_IDS
        }
    ):
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")
    try:
        retrieved = datetime.fromisoformat(
            str(raw["retrieved_at_utc"]).replace("Z", "+00:00")
        )
        attestation = KimiTermsAttestation(
            retrieved_at_utc=retrieved,
            canonical_text_sha256=raw["canonical_text_sha256"],
            canonicalization_version=raw["canonicalization_version"],
            material_delta_review_status=raw["material_delta_review_status"],
            authorization_binding_sha256=raw["authorization_binding_sha256"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise KimiControlledPilotError(
            "kimi_pilot_artifact_projection_invalid"
        ) from exc
    attestation_sha256 = attestation.attestation_sha256
    expected_binding = _authorization_binding_sha256(
        authorization_id_sha256=authorization_id_sha256,
        contract_sha256=contract_sha256,
        candidate_id=candidate_id,
        candidate_commitment_sha256=candidate_commitment_sha256,
        chat_contract_sha256=chat_contract_sha256,
        authorization_expires_at_utc=authorization_expires_at_utc,
        attestation_sha256=attestation_sha256,
    )
    if (
        attestation.to_dict() != raw
        or attestation.authorization_binding_sha256 != expected_binding
    ):
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")
    return attestation, attestation_sha256


def _validate_persisted_pricing_attestation(
    raw: Any,
    *,
    authorization_id_sha256: str,
    contract_sha256: str,
    candidate_id: str,
    candidate_commitment_sha256: str,
    chat_contract_sha256: str,
    authorization_expires_at_utc: datetime,
    terms_attestation_sha256: str,
) -> tuple[KimiPricingAttestation, str]:
    expected_fields = {
        "schema_version",
        "retrieved_at_utc",
        "source_url",
        "canonical_source_sha256",
        "canonical_source_bytes",
        "canonicalization_version",
        "material_delta_review_status",
        "model_id",
        "currency",
        "billing_unit_tokens",
        "cached_input_cny_per_million",
        "uncached_input_cny_per_million",
        "output_cny_per_million",
        "cache_discount_used_for_local_hard_stop",
        "actual_billed_cost_known",
        "claim_scope",
        "authorization_binding_sha256",
    }
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")
    try:
        retrieved = datetime.fromisoformat(
            str(raw["retrieved_at_utc"]).replace("Z", "+00:00")
        )
        attestation = KimiPricingAttestation(
            retrieved_at_utc=retrieved,
            canonical_source_sha256=raw["canonical_source_sha256"],
            canonical_source_bytes=raw["canonical_source_bytes"],
            canonicalization_version=raw["canonicalization_version"],
            material_delta_review_status=raw["material_delta_review_status"],
            authorization_binding_sha256=raw["authorization_binding_sha256"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise KimiControlledPilotError(
            "kimi_pilot_artifact_projection_invalid"
        ) from exc
    attestation_sha256 = attestation.attestation_sha256
    expected_binding = _pricing_authorization_binding_sha256(
        authorization_id_sha256=authorization_id_sha256,
        contract_sha256=contract_sha256,
        candidate_id=candidate_id,
        candidate_commitment_sha256=candidate_commitment_sha256,
        chat_contract_sha256=chat_contract_sha256,
        authorization_expires_at_utc=authorization_expires_at_utc,
        terms_attestation_sha256=terms_attestation_sha256,
        pricing_attestation_sha256=attestation_sha256,
    )
    if (
        attestation.to_dict() != raw
        or attestation.authorization_binding_sha256 != expected_binding
    ):
        raise KimiControlledPilotError("kimi_pilot_artifact_projection_invalid")
    return attestation, attestation_sha256


def _load_artifact_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except Exception as exc:
        raise KimiControlledPilotError("kimi_pilot_artifact_invalid") from exc
    if not isinstance(value, dict):
        raise KimiControlledPilotError("kimi_pilot_artifact_invalid")
    return value


def _assert_sanitized_artifacts(*paths: Path) -> None:
    forbidden = (
        "MOONSHOT_API_KEY",
        "authorization:",
        "@",
        "reasoning_content",
        "tool_call_id",
        "kimi_synth_success_v1",
        "kimi_synth_missing_v1",
        "effect_size",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if any(value.lower() in text.lower() for value in forbidden):
            raise KimiControlledPilotError("kimi_pilot_artifact_not_sanitized")


# Version-explicit aliases keep CLI/freezer imports impossible to confuse with
# the immutable v1 implementation while retaining compact internal names in
# this mechanical frozen fork.
KimiPilotV2Capability = KimiPilotCapability
build_kimi_pricing_attestation_v2 = build_kimi_pricing_attestation
build_kimi_terms_attestation_v2 = build_kimi_terms_attestation
controlled_pilot_v2_contract_sha256 = controlled_pilot_contract_sha256
create_kimi_pilot_v2_capability = create_kimi_pilot_capability
kimi_controlled_pilot_v2_contract = kimi_controlled_pilot_contract
load_kimi_pricing_attestation_v2 = load_kimi_pricing_attestation
load_kimi_terms_attestation_v2 = load_kimi_terms_attestation
run_kimi_controlled_pilot_v2 = run_kimi_controlled_pilot
verify_kimi_controlled_pilot_v2_artifacts = (
    verify_kimi_controlled_pilot_artifacts
)


__all__ = [
    "ARTIFACT_SUBDIRECTORY",
    "CONTRACT_ID",
    "EXPECTED_CANDIDATE_ID",
    "KimiControlledPilotError",
    "KimiPilotCapability",
    "KimiPilotV2Capability",
    "KimiPricingAttestation",
    "KimiTermsAttestation",
    "PREDECESSOR_CANDIDATE_COMMITMENT_SHA256",
    "RECEIPT_FIELDS",
    "RECEIPT_SCHEMA_VERSION",
    "build_kimi_pricing_attestation",
    "build_kimi_pricing_attestation_v2",
    "build_kimi_terms_attestation",
    "build_kimi_terms_attestation_v2",
    "controlled_pilot_contract_sha256",
    "controlled_pilot_v2_contract_sha256",
    "create_kimi_pilot_capability",
    "create_kimi_pilot_v2_capability",
    "kimi_controlled_pilot_contract",
    "kimi_controlled_pilot_v2_contract",
    "load_kimi_pricing_attestation",
    "load_kimi_pricing_attestation_v2",
    "load_kimi_terms_attestation",
    "load_kimi_terms_attestation_v2",
    "run_kimi_controlled_pilot",
    "run_kimi_controlled_pilot_v2",
    "verify_kimi_controlled_pilot_artifacts",
    "verify_kimi_controlled_pilot_v2_artifacts",
]
