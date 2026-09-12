"""Pure verification of T6-C pre-receipt documents and external observations.

The verifier consumes caller-supplied bytes and an already-verified schema map.
It deliberately has no filesystem, clock, environment, database, network,
Provider, runtime-binding, or signing capability.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from fractions import Fraction
from typing import Any, Mapping

import jsonschema

from .errors import ExternalClosurePrimitiveError
from .primitives import (
    build_signature_message,
    canonical_json_bytes,
    decode_strict_json_object,
    derive_ed25519_key_id,
    parse_utc_timestamp,
    utc_interval_is_positive_and_at_most,
    verify_document_sha256,
    verify_ed25519_signature,
    verify_ledger_hashes,
)
from .types import PreReceiptDocumentBytes, UtcTimestamp
from .schema_guard import LOCAL_ONLY_SCHEMA_REGISTRY, only_local_schema_references


_MAX_DOCUMENT_BYTES = 2_000_000
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_ZERO_SHA256 = "0" * 64

_USER_AUTHORIZATION_BINDING_DOMAIN = (
    "researchops-provider-completion-user-authorization-binding-v1"
)
_SEEN_SET_DOMAIN = "researchops-provider-completion-seen-case-exclusion-set-v1"
_CASE_HANDLE_SET_DOMAIN = "researchops-provider-completion-case-handle-set-v1"
_CASE_HANDLE_ORDER_DOMAIN = "researchops-provider-completion-case-handle-order-v1"
_CAMPAIGN_ID_DOMAIN = "researchops-provider-completion-campaign-id-v1"
_AUDIT_RUN_ID_DOMAIN = "researchops-provider-completion-audit-run-id-v1"
_AUDIT_RUN_SET_DOMAIN = "researchops-provider-completion-audit-run-set-v1"
_RUNTIME_PLAN_DOMAIN = "researchops-provider-completion-external-runtime-plan-v1"

_SCHEMA_BY_DOCUMENT = {
    "trust_manifest": "external_trust_manifest_v1.schema.json",
    "candidate_freeze_receipt": "candidate_freeze_receipt_v1.schema.json",
    "candidate_frozen_ledger_entry": "external_ledger_entry_v1.schema.json",
    "seen_case_exclusion_manifest": (
        "seen_task_exclusion_manifest_v1.schema.json"
    ),
    "preregistration_envelope": (
        "external_preregistration_envelope_v1.schema.json"
    ),
    "preregistration_frozen_ledger_entry": (
        "external_ledger_entry_v1.schema.json"
    ),
    "authorization_grant": "authorization_grant_v1.schema.json",
    "consumption_receipt": "consumption_receipt_v1.schema.json",
    "authorization_consumed_ledger_entry": (
        "external_ledger_entry_v1.schema.json"
    ),
}
_OBSERVATION_SCHEMA = "external_observation_bundle_v1.schema.json"

_TRUST_ROLES = ("freeze_authority", "task_custodian", "ledger_witness")
_SIGNED_DOCUMENT_ROLES = {
    "completion_telemetry_external_trust_manifest": _TRUST_ROLES,
    "completion_telemetry_candidate_freeze": ("freeze_authority",),
    "completion_telemetry_seen_task_exclusion_manifest": (
        "freeze_authority",
        "task_custodian",
    ),
    "completion_telemetry_closure_preregistration": (
        "freeze_authority",
        "task_custodian",
    ),
    "completion_telemetry_closure_authorization_grant": (
        "freeze_authority",
        "task_custodian",
    ),
    "completion_telemetry_external_ledger_entry": ("ledger_witness",),
}
_VERIFIED_SUMMARY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedPreReceiptDocuments:
    """Hash/timestamp-only proof that every layer-A document gate passed."""

    trust_manifest_sha256: str
    candidate_freeze_receipt_sha256: str
    candidate_frozen_ledger_entry_sha256: str
    seen_case_exclusion_manifest_sha256: str
    preregistration_envelope_sha256: str
    preregistration_frozen_ledger_entry_sha256: str
    authorization_grant_sha256: str
    consumption_receipt_sha256: str
    authorization_consumed_ledger_entry_sha256: str
    authorization_consumed_ledger_head_sha256: str
    trust_observed_at_utc: str
    envelope_observed_at_utc: str
    authorization_observed_at_utc: str
    authorization_consumed_observed_at_utc: str
    manifest_observed_at_utc: str
    grant_expires_at_utc: str
    envelope_valid_until_utc: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError(
            "VerifiedPreReceiptDocuments requires successful document verification"
        )

    @classmethod
    def _create(
        cls, token: object, **values: str
    ) -> VerifiedPreReceiptDocuments:
        if token is not _VERIFIED_SUMMARY_TOKEN or set(values) != set(
            cls.__dataclass_fields__
        ):
            raise TypeError("verified_pre_receipt_summary_construction_forbidden")
        instance = object.__new__(cls)
        for name in cls.__dataclass_fields__:
            value = values[name]
            if type(value) is not str or not value:
                raise TypeError("verified_pre_receipt_summary_value_invalid")
            object.__setattr__(instance, name, value)
        return instance


def _fail(code: str) -> None:
    raise ExternalClosurePrimitiveError(code)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _domain_hash(domain: str, *parts: bytes) -> str:
    return _sha256(domain.encode("utf-8") + b"\x00" + b"\x00".join(parts))


def _schema(
    schemas: Mapping[str, Mapping[str, Any]], name: str
) -> Mapping[str, Any]:
    if not isinstance(schemas, Mapping):
        _fail("external_closure_schema_mapping_invalid")
    candidate = schemas.get(name)
    if not isinstance(candidate, Mapping) or not only_local_schema_references(candidate):
        _fail("external_closure_schema_mapping_invalid")
    return candidate


def _decode_schema_document(
    payload: bytes,
    *,
    schemas: Mapping[str, Mapping[str, Any]],
    schema_name: str,
    schema_error: str,
) -> dict[str, Any]:
    value = decode_strict_json_object(payload, max_bytes=_MAX_DOCUMENT_BYTES)
    try:
        jsonschema.Draft202012Validator(
            _schema(schemas, schema_name),
            format_checker=jsonschema.FormatChecker(),
            registry=LOCAL_ONLY_SCHEMA_REGISTRY,
        ).validate(value)
    except ExternalClosurePrimitiveError:
        raise
    except Exception:
        _fail(schema_error)
    return value


def _timestamp(value: object) -> UtcTimestamp:
    if type(value) is not str:
        _fail("external_closure_timeline_invalid")
    try:
        return parse_utc_timestamp(value)
    except ExternalClosurePrimitiveError:
        _fail("external_closure_timeline_invalid")


def _nested(value: Mapping[str, Any], *path: str) -> Any:
    current: Any = value
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            _fail("external_closure_cross_projection_invalid")
        current = current[part]
    return current


def _document_hash(document: Mapping[str, Any]) -> str:
    value = document.get("document_sha256")
    if type(value) is not str:
        _fail("external_closure_cross_projection_invalid")
    return value


def _entry_hash(entry: Mapping[str, Any]) -> str:
    value = entry.get("entry_sha256")
    if type(value) is not str:
        _fail("external_closure_ledger_projection_invalid")
    return value


def _role_map(trust: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw_roles = trust.get("roles")
    if type(raw_roles) is not list or len(raw_roles) != len(_TRUST_ROLES):
        _fail("external_closure_trust_invalid")
    roles: dict[str, Mapping[str, Any]] = {}
    for expected_role, record in zip(_TRUST_ROLES, raw_roles):
        if not isinstance(record, Mapping) or record.get("role") != expected_role:
            _fail("external_closure_trust_invalid")
        public_key = record.get("public_key_b64")
        key_id = record.get("key_id")
        if type(public_key) is not str or type(key_id) is not str:
            _fail("external_closure_trust_invalid")
        if derive_ed25519_key_id(public_key) != key_id:
            _fail("external_closure_trust_invalid")
        not_before = _timestamp(record.get("not_before_utc"))
        expires = _timestamp(record.get("expires_at_utc"))
        if not not_before < expires:
            _fail("external_closure_trust_invalid")
        roles[expected_role] = record
    if (
        len({record["key_id"] for record in roles.values()}) != 3
        or len({record["public_key_b64"] for record in roles.values()}) != 3
        or len(
            {record["organization_commitment_sha256"] for record in roles.values()}
        )
        != 3
    ):
        _fail("external_closure_trust_role_separation_invalid")
    return roles


def _assert_role_time(
    role_record: Mapping[str, Any],
    *,
    action_time: UtcTimestamp,
    trust_valid_from: UtcTimestamp,
    trust_expires: UtcTimestamp,
) -> None:
    role_not_before = _timestamp(role_record.get("not_before_utc"))
    role_expires = _timestamp(role_record.get("expires_at_utc"))
    if not (
        trust_valid_from <= action_time < trust_expires
        and role_not_before <= action_time < role_expires
    ):
        _fail("external_closure_signature_time_invalid")


def _verify_signatures(
    document: Mapping[str, Any],
    *,
    expected_roles: tuple[str, ...],
    roles: Mapping[str, Mapping[str, Any]],
    action_time: UtcTimestamp,
    trust_valid_from: UtcTimestamp,
    trust_expires: UtcTimestamp,
) -> None:
    document_type = document.get("document_type")
    document_hash = (
        document.get("entry_sha256")
        if document_type == "completion_telemetry_external_ledger_entry"
        else document.get("document_sha256")
    )
    signatures = document.get("signatures")
    if (
        type(document_type) is not str
        or _SIGNED_DOCUMENT_ROLES.get(document_type) != expected_roles
        or type(document_hash) is not str
        or type(signatures) is not list
        or len(signatures) != len(expected_roles)
    ):
        _fail("external_closure_signature_projection_invalid")
    for expected_role, signature in zip(expected_roles, signatures):
        role_record = roles.get(expected_role)
        if not isinstance(signature, Mapping) or not isinstance(
            role_record, Mapping
        ):
            _fail("external_closure_signature_projection_invalid")
        key_id = role_record.get("key_id")
        if (
            signature.get("role") != expected_role
            or signature.get("key_id") != key_id
            or signature.get("algorithm") != "ed25519"
            or signature.get("signed_sha256") != document_hash
            or type(key_id) is not str
            or type(role_record.get("public_key_b64")) is not str
            or type(signature.get("signature_b64")) is not str
        ):
            _fail("external_closure_signature_projection_invalid")
        _assert_role_time(
            role_record,
            action_time=action_time,
            trust_valid_from=trust_valid_from,
            trust_expires=trust_expires,
        )
        message = build_signature_message(
            document_type=document_type,
            role=expected_role,
            key_id=key_id,
            signed_sha256=document_hash,
        )
        verify_ed25519_signature(
            public_key_b64=role_record["public_key_b64"],
            signature_b64=signature["signature_b64"],
            message=message,
        )


def _verify_document_hashes(documents: Mapping[str, Mapping[str, Any]]) -> None:
    for name in (
        "trust_manifest",
        "candidate_freeze_receipt",
        "seen_case_exclusion_manifest",
        "preregistration_envelope",
        "authorization_grant",
        "consumption_receipt",
    ):
        document = documents[name]
        if type(document) is not dict:
            _fail("external_closure_document_schema_invalid")
        verify_document_sha256(document)
    for name in (
        "candidate_frozen_ledger_entry",
        "preregistration_frozen_ledger_entry",
        "authorization_consumed_ledger_entry",
    ):
        entry = documents[name]
        if type(entry) is not dict:
            _fail("external_closure_document_schema_invalid")
        verify_ledger_hashes(entry)


def _verify_envelope_commitments(
    envelope: Mapping[str, Any], seen: Mapping[str, Any]
) -> None:
    runtime_plan = _nested(envelope, "runtime_plan")
    denominator = _nested(runtime_plan, "denominator_plan")
    topology = _nested(runtime_plan, "campaign_topology")
    limits = _nested(runtime_plan, "transport_limits")
    budget = _nested(runtime_plan, "budget_policy")
    custody = _nested(envelope, "task_custody")
    case_ids = denominator.get("case_ids")
    if type(case_ids) is not list or any(type(item) is not str for item in case_ids):
        _fail("external_closure_plan_commitment_invalid")

    case_ids_sha256 = _sha256(canonical_json_bytes(case_ids))
    case_set_sha256 = _domain_hash(
        _CASE_HANDLE_SET_DOMAIN, canonical_json_bytes(sorted(case_ids))
    )
    case_order_sha256 = _domain_hash(
        _CASE_HANDLE_ORDER_DOMAIN, canonical_json_bytes(case_ids)
    )
    denominator_sha256 = _sha256(canonical_json_bytes(denominator))

    plan_body = dict(runtime_plan)
    plan_body.pop("external_plan_binding_sha256", None)
    external_plan_sha256 = _domain_hash(
        _RUNTIME_PLAN_DOMAIN, canonical_json_bytes(plan_body)
    )
    budget_body = dict(budget)
    budget_body.pop("budget_policy_commitment_sha256", None)
    budget_sha256 = _domain_hash(
        _RUNTIME_PLAN_DOMAIN,
        b"budget_policy",
        canonical_json_bytes(budget_body),
    )

    task_bundle_hash = custody.get("task_bundle_commitment_sha256")
    envelope_id = envelope.get("envelope_id")
    if type(task_bundle_hash) is not str or type(envelope_id) is not str:
        _fail("external_closure_plan_commitment_invalid")
    try:
        campaign_digest = hashlib.sha256(
            _CAMPAIGN_ID_DOMAIN.encode("utf-8")
            + b"\x00"
            + envelope_id.encode("ascii")
            + b"\x00"
            + bytes.fromhex(task_bundle_hash)
        ).hexdigest()
        run_ids = [
            "PCERUN-"
            + hashlib.sha256(
                _AUDIT_RUN_ID_DOMAIN.encode("utf-8")
                + b"\x00"
                + envelope_id.encode("ascii")
                + b"\x00"
                + case_id.encode("ascii")
            ).hexdigest()[:32].upper()
            for case_id in case_ids
        ]
    except (UnicodeEncodeError, ValueError):
        _fail("external_closure_plan_commitment_invalid")
    campaign_id = "PCECAMP-" + campaign_digest[:32].upper()
    run_set_sha256 = _domain_hash(
        _AUDIT_RUN_SET_DOMAIN,
        b"planned",
        canonical_json_bytes(run_ids),
    )

    if (
        denominator.get("case_ids_sha256") != case_ids_sha256
        or runtime_plan.get("denominator_plan_commitment_sha256")
        != denominator_sha256
        or runtime_plan.get("external_plan_binding_sha256")
        != external_plan_sha256
        or budget.get("budget_policy_commitment_sha256") != budget_sha256
        or topology.get("campaign_id") != campaign_id
        or topology.get("case_handle_set_commitment_sha256") != case_set_sha256
        or topology.get("case_handle_order_commitment_sha256")
        != case_order_sha256
        or topology.get("audit_run_set_commitment_sha256") != run_set_sha256
        or len(run_ids) != len(set(run_ids))
        or custody.get("planned_case_count") != len(case_ids)
        or custody.get("case_handle_set_commitment_sha256") != case_set_sha256
        or custody.get("case_order_commitment_sha256") != case_order_sha256
    ):
        _fail("external_closure_plan_commitment_invalid")

    request_cap = denominator.get("total_model_request_cap")
    max_turns = denominator.get("max_turns_per_case")
    output_per_request = budget.get("output_token_limit_per_request")
    output_total = budget.get("output_token_limit_total")
    if (
        type(request_cap) is not int
        or type(max_turns) is not int
        or type(output_per_request) is not int
        or type(output_total) is not int
        or not len(case_ids) <= request_cap <= len(case_ids) * max_turns
        or limits.get("network_attempt_cap") != request_cap
        or output_total > output_per_request * request_cap
    ):
        _fail("external_closure_plan_limit_invalid")

    try:
        input_price = Decimal(budget["input_price_per_million_cny"])
        output_price = Decimal(budget["output_price_per_million_cny"])
        cost_stop = Decimal(budget["local_observed_cost_stop_cny"])
        # Decimal construction from the schema-validated strings is exact,
        # but Decimal arithmetic inherits caller precision/traps/exponents.
        # Compare exact rational amounts instead; no rounding, ambient context
        # mutation, or binary-float conversion may alter budget admission.
        worst_cost = (
            budget["input_token_limit_total"] * Fraction(input_price)
            + output_total * Fraction(output_price)
        ) / 1_000_000
        exact_stop = Fraction(cost_stop)
    except (DecimalException, KeyError, TypeError, ValueError, OverflowError):
        _fail("external_closure_budget_invalid")
    if (
        input_price <= 0
        or output_price <= 0
        or cost_stop <= 0
        or worst_cost > exact_stop
    ):
        _fail("external_closure_budget_invalid")

    inventory = seen.get("source_inventory")
    if type(inventory) is not list:
        _fail("external_closure_seen_commitment_invalid")
    source_inventory_sha256 = _domain_hash(
        _SEEN_SET_DOMAIN,
        b"source_inventory",
        canonical_json_bytes(inventory),
    )
    if seen.get("source_inventory_commitment_sha256") != source_inventory_sha256:
        _fail("external_closure_seen_commitment_invalid")


def _verify_cross_projections(
    documents: Mapping[str, Mapping[str, Any]],
    observation: Mapping[str, Any],
) -> None:
    trust = documents["trust_manifest"]
    candidate = documents["candidate_freeze_receipt"]
    candidate_entry = documents["candidate_frozen_ledger_entry"]
    seen = documents["seen_case_exclusion_manifest"]
    envelope = documents["preregistration_envelope"]
    prereg_entry = documents["preregistration_frozen_ledger_entry"]
    grant = documents["authorization_grant"]
    consumption = documents["consumption_receipt"]
    consumed_entry = documents["authorization_consumed_ledger_entry"]

    trust_hash = _document_hash(trust)
    candidate_hash = _document_hash(candidate)
    seen_hash = _document_hash(seen)
    envelope_hash = _document_hash(envelope)
    grant_hash = _document_hash(grant)
    consumption_hash = _document_hash(consumption)
    candidate_entry_hash = _entry_hash(candidate_entry)
    prereg_entry_hash = _entry_hash(prereg_entry)

    execution = _nested(envelope, "execution_binding")
    custody = _nested(envelope, "task_custody")
    runtime = _nested(envelope, "runtime_plan")
    denominator = _nested(runtime, "denominator_plan")
    budget = _nested(runtime, "budget_policy")
    external_trust = _nested(envelope, "external_trust")
    closure_evidence = _nested(envelope, "closure_evidence")

    candidate_pairs = (
        (candidate.get("repository"), execution.get("repository")),
        (candidate_hash, execution.get("candidate_freeze_receipt_sha256")),
        (candidate_entry_hash, execution.get("candidate_freeze_ledger_entry_sha256")),
        (candidate.get("candidate_commit"), execution.get("execution_commit")),
        (candidate.get("candidate_tree"), execution.get("execution_tree")),
        (
            candidate.get("source_integrity_commitment_sha256"),
            execution.get("source_integrity_commitment_sha256"),
        ),
        (
            candidate.get("runtime_registry_commitment_sha256"),
            execution.get("runtime_registry_commitment_sha256"),
        ),
        (
            candidate.get("implementation_commitment_sha256"),
            execution.get("implementation_commitment_sha256"),
        ),
        (
            candidate.get("runner_config_commitment_sha256"),
            execution.get("runner_config_commitment_sha256"),
        ),
        (
            candidate.get("closure_evidence_contract_commitment_sha256"),
            execution.get("closure_evidence_contract_commitment_sha256"),
            closure_evidence.get("closure_evidence_contract_commitment_sha256"),
        ),
        (
            candidate.get("dependency_lock_sha256"),
            execution.get("dependency_lock_sha256"),
        ),
    )
    if any(any(item != pair[0] for item in pair[1:]) for pair in candidate_pairs):
        _fail("external_closure_candidate_projection_invalid")

    runtime_pairs = (
        (execution.get("provider_id"), denominator.get("provider_id")),
        (execution.get("api_surface"), denominator.get("api_surface")),
        (execution.get("transport_id"), denominator.get("transport_id")),
        (execution.get("adapter_version"), denominator.get("adapter_version")),
        (
            execution.get("telemetry_schema_sha256"),
            denominator.get("telemetry_schema_sha256"),
        ),
        (execution.get("mapping_sha256"), denominator.get("mapping_sha256")),
    )
    if any(left != right for left, right in runtime_pairs):
        _fail("external_closure_runtime_projection_invalid")

    if (
        seen.get("candidate_freeze_receipt_sha256") != candidate_hash
        or custody.get("seen_task_exclusion_manifest_sha256") != seen_hash
        or custody.get("seen_task_exclusion_set_commitment_sha256")
        != seen.get("exclusion_digest_set_commitment_sha256")
        or custody.get("seen_task_exclusion_set_count")
        != seen.get("exclusion_digest_set_count")
        or custody.get("seen_source_inventory_commitment_sha256")
        != seen.get("source_inventory_commitment_sha256")
    ):
        _fail("external_closure_seen_projection_invalid")

    roles = _role_map(trust)
    if (
        external_trust.get("trust_manifest_sha256") != trust_hash
        or external_trust.get("freeze_authority_key_id")
        != roles["freeze_authority"].get("key_id")
        or external_trust.get("task_custodian_key_id")
        != roles["task_custodian"].get("key_id")
        or external_trust.get("ledger_witness_key_id")
        != roles["ledger_witness"].get("key_id")
        or external_trust.get("freeze_authority_organization_commitment_sha256")
        != roles["freeze_authority"].get("organization_commitment_sha256")
        or external_trust.get("task_custodian_organization_commitment_sha256")
        != roles["task_custodian"].get("organization_commitment_sha256")
        or external_trust.get("ledger_witness_organization_commitment_sha256")
        != roles["ledger_witness"].get("organization_commitment_sha256")
    ):
        _fail("external_closure_trust_projection_invalid")

    ledger_id = external_trust.get("ledger_id")
    if any(
        entry.get("ledger_id") != ledger_id
        for entry in (candidate_entry, prereg_entry, consumed_entry)
    ):
        _fail("external_closure_ledger_projection_invalid")
    base_sequence = external_trust.get("ledger_base_sequence")
    base_head = external_trust.get("ledger_base_head_sha256")
    if (
        type(base_sequence) is not int
        or (base_sequence == 0) != (base_head == _ZERO_SHA256)
        or base_sequence != candidate_entry.get("sequence")
        or base_head != candidate_entry.get("resulting_head_sha256")
    ):
        _fail("external_closure_ledger_projection_invalid")
    if (
        base_sequence >= _MAX_SAFE_INTEGER
        or prereg_entry.get("sequence") != base_sequence + 1
        or prereg_entry.get("previous_head_sha256") != base_head
        or grant.get("ledger_sequence") != prereg_entry.get("sequence")
        or grant.get("ledger_head_sha256")
        != prereg_entry.get("resulting_head_sha256")
        or grant.get("ledger_sequence") >= _MAX_SAFE_INTEGER
        or consumed_entry.get("sequence") != grant.get("ledger_sequence") + 1
        or consumed_entry.get("previous_head_sha256")
        != grant.get("ledger_head_sha256")
    ):
        _fail("external_closure_ledger_projection_invalid")

    expected_entry_hashes = (
        (
            candidate_entry,
            "candidate_frozen",
            candidate_hash,
            None,
            None,
            None,
        ),
        (
            prereg_entry,
            "preregistration_frozen",
            candidate_hash,
            envelope_hash,
            None,
            None,
        ),
        (
            consumed_entry,
            "authorization_consumed",
            candidate_hash,
            envelope_hash,
            grant_hash,
            consumption_hash,
        ),
    )
    for entry, kind, frozen_hash, prereg_hash, authorized_hash, consumed_hash in (
        expected_entry_hashes
    ):
        if (
            entry.get("entry_type") != kind
            or entry.get("candidate_freeze_receipt_sha256") != frozen_hash
            or entry.get("preregistration_envelope_sha256") != prereg_hash
            or entry.get("authorization_grant_sha256") != authorized_hash
            or entry.get("consumption_receipt_sha256") != consumed_hash
            or entry.get("closure_receipt_sha256") is not None
        ):
            _fail("external_closure_ledger_document_projection_invalid")

    _verify_envelope_commitments(envelope, seen)

    plan_commitment = runtime.get("denominator_plan_commitment_sha256")
    external_plan_binding = runtime.get("external_plan_binding_sha256")
    grant_projection = (
        (grant.get("preregistration_envelope_sha256"), envelope_hash),
        (grant.get("preregistration_freeze_entry_sha256"), prereg_entry_hash),
        (grant.get("candidate_commit"), execution.get("execution_commit")),
        (grant.get("candidate_tree"), execution.get("execution_tree")),
        (
            grant.get("source_integrity_commitment_sha256"),
            execution.get("source_integrity_commitment_sha256"),
        ),
        (
            grant.get("runtime_registry_commitment_sha256"),
            execution.get("runtime_registry_commitment_sha256"),
        ),
        (grant.get("denominator_plan_commitment_sha256"), plan_commitment),
        (grant.get("external_plan_binding_sha256"), external_plan_binding),
        (
            grant.get("budget_policy_commitment_sha256"),
            budget.get("budget_policy_commitment_sha256"),
        ),
        (grant.get("pricing_snapshot_date"), budget.get("pricing_snapshot_date")),
        (
            grant.get("pricing_source_snapshot_sha256"),
            budget.get("pricing_source_snapshot_sha256"),
        ),
        (
            grant.get("pricing_retrieved_at_utc"),
            budget.get("pricing_retrieved_at_utc"),
        ),
        (
            grant.get("official_pricing_attestation_current"),
            budget.get("official_pricing_attestation_current"),
        ),
        (grant.get("provider_id"), execution.get("provider_id")),
        (grant.get("model_id"), execution.get("model_id")),
        (grant.get("api_origin"), execution.get("api_origin")),
        (grant.get("api_surface"), execution.get("api_surface")),
        (grant.get("transport_id"), execution.get("transport_id")),
        (grant.get("agents_sdk_retries"), denominator.get("agents_sdk_retries")),
        (grant.get("http_client_retries"), denominator.get("http_client_retries")),
        (grant.get("resume"), _nested(runtime, "transport_limits").get("resume")),
        (
            grant.get("fallback"),
            _nested(runtime, "transport_limits").get("fallback"),
        ),
    )
    if any(left != right for left, right in grant_projection):
        _fail("external_closure_grant_projection_invalid")

    grant_body = dict(grant)
    grant_body.pop("explicit_user_authorization_binding_sha256", None)
    grant_body.pop("document_sha256", None)
    grant_body.pop("signatures", None)
    user_binding = _domain_hash(
        _USER_AUTHORIZATION_BINDING_DOMAIN, canonical_json_bytes(grant_body)
    )
    user_observation = observation.get("user_authorization_observation")
    if not isinstance(user_observation, Mapping) or (
        grant.get("explicit_user_authorization_id_sha256")
        != user_observation.get("authorization_id_sha256")
        or grant.get("explicit_user_authorization_binding_sha256") != user_binding
        or user_observation.get("authorization_binding_sha256") != user_binding
    ):
        _fail("external_closure_user_authorization_projection_invalid")

    consumption_projection = (
        (consumption.get("preregistration_envelope_sha256"), envelope_hash),
        (consumption.get("preregistration_freeze_entry_sha256"), prereg_entry_hash),
        (consumption.get("authorization_grant_sha256"), grant_hash),
        (
            consumption.get("explicit_user_authorization_id_sha256"),
            grant.get("explicit_user_authorization_id_sha256"),
        ),
        (consumption.get("candidate_commit"), grant.get("candidate_commit")),
        (
            consumption.get("denominator_plan_commitment_sha256"),
            plan_commitment,
        ),
        (
            consumption.get("external_plan_binding_sha256"),
            external_plan_binding,
        ),
    )
    if any(left != right for left, right in consumption_projection):
        _fail("external_closure_consumption_projection_invalid")

    trust_observation = observation.get("trust_manifest_observation")
    envelope_observation = observation.get("preregistration_envelope_observation")
    if (
        not isinstance(trust_observation, Mapping)
        or trust_observation.get("document_sha256") != trust_hash
        or not isinstance(envelope_observation, Mapping)
        or envelope_observation.get("document_sha256") != envelope_hash
    ):
        _fail("external_closure_observation_mismatch")


def _verify_anchor(
    anchor: object, entry: Mapping[str, Any], *, expected_type: str
) -> None:
    if not isinstance(anchor, Mapping):
        _fail("external_closure_observation_mismatch")
    expected = {
        "entry_sha256": entry.get("entry_sha256"),
        "ledger_id": entry.get("ledger_id"),
        "entry_type": expected_type,
        "sequence": entry.get("sequence"),
        "previous_head_sha256": entry.get("previous_head_sha256"),
        "resulting_head_sha256": entry.get("resulting_head_sha256"),
        "entry_occurred_at_utc": entry.get("occurred_at_utc"),
    }
    if any(anchor.get(name) != value for name, value in expected.items()):
        _fail("external_closure_observation_mismatch")


def _verify_timeline(
    documents: Mapping[str, Mapping[str, Any]],
    observation: Mapping[str, Any],
) -> None:
    trust = documents["trust_manifest"]
    candidate = documents["candidate_freeze_receipt"]
    candidate_entry = documents["candidate_frozen_ledger_entry"]
    seen = documents["seen_case_exclusion_manifest"]
    envelope = documents["preregistration_envelope"]
    prereg_entry = documents["preregistration_frozen_ledger_entry"]
    grant = documents["authorization_grant"]
    consumption = documents["consumption_receipt"]
    consumed_entry = documents["authorization_consumed_ledger_entry"]

    trust_observation = _nested(observation, "trust_manifest_observation")
    envelope_observation = _nested(
        observation, "preregistration_envelope_observation"
    )
    user_observation = _nested(observation, "user_authorization_observation")
    candidate_anchor = _nested(observation, "candidate_freeze_anchor")
    prereg_anchor = _nested(observation, "preregistration_freeze_anchor")
    consumed_anchor = _nested(observation, "authorization_consumed_anchor")
    manifest_observation = _nested(observation, "manifest_observation")

    _verify_anchor(candidate_anchor, candidate_entry, expected_type="candidate_frozen")
    _verify_anchor(
        prereg_anchor, prereg_entry, expected_type="preregistration_frozen"
    )
    _verify_anchor(
        consumed_anchor, consumed_entry, expected_type="authorization_consumed"
    )

    times = {
        "trust_valid_from": _timestamp(trust.get("valid_from_utc")),
        "trust_created": _timestamp(trust.get("created_at_utc")),
        "trust_expires": _timestamp(trust.get("expires_at_utc")),
        "trust_observed": _timestamp(trust_observation.get("observed_at_utc")),
        "candidate_frozen": _timestamp(candidate.get("frozen_at_utc")),
        "candidate_entry": _timestamp(candidate_entry.get("occurred_at_utc")),
        "candidate_anchor": _timestamp(candidate_anchor.get("observed_at_utc")),
        "seen_created": _timestamp(seen.get("created_at_utc")),
        "task_committed": _timestamp(envelope.get("task_bundle_committed_at_utc")),
        "envelope_frozen": _timestamp(envelope.get("envelope_frozen_at_utc")),
        "envelope_observed": _timestamp(
            envelope_observation.get("observed_at_utc")
        ),
        "envelope_valid": _timestamp(envelope.get("valid_until_utc")),
        "prereg_entry": _timestamp(prereg_entry.get("occurred_at_utc")),
        "prereg_anchor": _timestamp(prereg_anchor.get("observed_at_utc")),
        "authorization_observed": _timestamp(user_observation.get("observed_at_utc")),
        "authorized": _timestamp(grant.get("authorized_at_utc")),
        "not_before": _timestamp(grant.get("not_before_utc")),
        "grant_expires": _timestamp(grant.get("expires_at_utc")),
        "pricing_retrieved": _timestamp(grant.get("pricing_retrieved_at_utc")),
        "consumed": _timestamp(consumption.get("consumed_at_utc")),
        "consumed_entry": _timestamp(consumed_entry.get("occurred_at_utc")),
        "consumed_anchor": _timestamp(consumed_anchor.get("observed_at_utc")),
        "manifest_observed": _timestamp(manifest_observation.get("observed_at_utc")),
    }
    if envelope.get("candidate_frozen_at_utc") != candidate.get("frozen_at_utc"):
        _fail("external_closure_timeline_invalid")
    if not (
        times["trust_valid_from"]
        <= times["trust_created"]
        <= times["trust_observed"]
        <= times["candidate_frozen"]
        < times["candidate_entry"]
        <= times["candidate_anchor"]
        < times["seen_created"]
        < times["task_committed"]
        < times["envelope_frozen"]
        < times["prereg_entry"]
        <= times["prereg_anchor"]
        < times["authorization_observed"]
        <= times["authorized"]
        <= times["not_before"]
        <= times["consumed"]
        < times["consumed_entry"]
        <= times["consumed_anchor"]
        < times["manifest_observed"]
        < times["envelope_valid"]
    ):
        _fail("external_closure_timeline_invalid")
    if not (
        times["candidate_frozen"] < times["trust_expires"]
        and times["envelope_frozen"]
        <= times["envelope_observed"]
        <= times["prereg_entry"]
        and times["pricing_retrieved"] <= times["authorized"]
        and times["not_before"] <= times["consumed"] < times["grant_expires"]
        and times["grant_expires"] <= times["envelope_valid"]
        and utc_interval_is_positive_and_at_most(
            start=times["envelope_frozen"],
            end=times["envelope_valid"],
            maximum_seconds=86_400,
        )
    ):
        _fail("external_closure_timeline_invalid")
    pricing_date = grant.get("pricing_snapshot_date")
    authorized_raw = grant.get("authorized_at_utc")
    pricing_raw = grant.get("pricing_retrieved_at_utc")
    if (
        type(pricing_date) is not str
        or type(authorized_raw) is not str
        or type(pricing_raw) is not str
        or pricing_date != authorized_raw[:10]
        or pricing_date != pricing_raw[:10]
    ):
        _fail("external_closure_timeline_invalid")


def verify_pre_receipt_documents(
    documents: PreReceiptDocumentBytes,
    *,
    schemas: Mapping[str, Mapping[str, Any]],
    external_observation_bundle: bytes,
) -> VerifiedPreReceiptDocuments:
    """Verify all layer-A signed documents, ledger links, and OOB observations.

    No decoded document, case handle, payload, public key, or runtime-capable
    object crosses the return boundary.
    """

    if type(documents) is not PreReceiptDocumentBytes:
        _fail("external_closure_pre_receipt_documents_invalid")
    decoded: dict[str, Mapping[str, Any]] = {}
    for name, schema_name in _SCHEMA_BY_DOCUMENT.items():
        decoded[name] = _decode_schema_document(
            getattr(documents, name),
            schemas=schemas,
            schema_name=schema_name,
            schema_error="external_closure_document_schema_invalid",
        )
    observation = _decode_schema_document(
        external_observation_bundle,
        schemas=schemas,
        schema_name=_OBSERVATION_SCHEMA,
        schema_error="external_closure_observation_schema_invalid",
    )
    if observation.get("mode") != "pre_receipt":
        _fail("external_closure_observation_mode_invalid")

    _verify_document_hashes(decoded)
    trust = decoded["trust_manifest"]
    roles = _role_map(trust)
    trust_valid_from = _timestamp(trust.get("valid_from_utc"))
    trust_created = _timestamp(trust.get("created_at_utc"))
    trust_expires = _timestamp(trust.get("expires_at_utc"))
    if not trust_valid_from <= trust_created < trust_expires:
        _fail("external_closure_trust_invalid")

    signed_actions = (
        (trust, _TRUST_ROLES, trust_created),
        (
            decoded["candidate_freeze_receipt"],
            ("freeze_authority",),
            _timestamp(decoded["candidate_freeze_receipt"].get("frozen_at_utc")),
        ),
        (
            decoded["candidate_frozen_ledger_entry"],
            ("ledger_witness",),
            _timestamp(
                decoded["candidate_frozen_ledger_entry"].get("occurred_at_utc")
            ),
        ),
        (
            decoded["seen_case_exclusion_manifest"],
            ("freeze_authority", "task_custodian"),
            _timestamp(decoded["seen_case_exclusion_manifest"].get("created_at_utc")),
        ),
        (
            decoded["preregistration_envelope"],
            ("freeze_authority", "task_custodian"),
            _timestamp(decoded["preregistration_envelope"].get("envelope_frozen_at_utc")),
        ),
        (
            decoded["preregistration_frozen_ledger_entry"],
            ("ledger_witness",),
            _timestamp(
                decoded["preregistration_frozen_ledger_entry"].get("occurred_at_utc")
            ),
        ),
        (
            decoded["authorization_grant"],
            ("freeze_authority", "task_custodian"),
            _timestamp(decoded["authorization_grant"].get("authorized_at_utc")),
        ),
        (
            decoded["authorization_consumed_ledger_entry"],
            ("ledger_witness",),
            _timestamp(
                decoded["authorization_consumed_ledger_entry"].get("occurred_at_utc")
            ),
        ),
    )
    for document, expected_roles, action_time in signed_actions:
        _verify_signatures(
            document,
            expected_roles=expected_roles,
            roles=roles,
            action_time=action_time,
            trust_valid_from=trust_valid_from,
            trust_expires=trust_expires,
        )

    _verify_cross_projections(decoded, observation)
    _verify_timeline(decoded, observation)

    consumed_entry = decoded["authorization_consumed_ledger_entry"]
    return VerifiedPreReceiptDocuments._create(
        _VERIFIED_SUMMARY_TOKEN,
        trust_manifest_sha256=_document_hash(trust),
        candidate_freeze_receipt_sha256=_document_hash(
            decoded["candidate_freeze_receipt"]
        ),
        candidate_frozen_ledger_entry_sha256=_entry_hash(
            decoded["candidate_frozen_ledger_entry"]
        ),
        seen_case_exclusion_manifest_sha256=_document_hash(
            decoded["seen_case_exclusion_manifest"]
        ),
        preregistration_envelope_sha256=_document_hash(
            decoded["preregistration_envelope"]
        ),
        preregistration_frozen_ledger_entry_sha256=_entry_hash(
            decoded["preregistration_frozen_ledger_entry"]
        ),
        authorization_grant_sha256=_document_hash(decoded["authorization_grant"]),
        consumption_receipt_sha256=_document_hash(decoded["consumption_receipt"]),
        authorization_consumed_ledger_entry_sha256=_entry_hash(consumed_entry),
        authorization_consumed_ledger_head_sha256=str(
            consumed_entry["resulting_head_sha256"]
        ),
        trust_observed_at_utc=str(
            _nested(observation, "trust_manifest_observation")["observed_at_utc"]
        ),
        envelope_observed_at_utc=str(
            _nested(observation, "preregistration_envelope_observation")[
                "observed_at_utc"
            ]
        ),
        authorization_observed_at_utc=str(
            _nested(observation, "user_authorization_observation")["observed_at_utc"]
        ),
        authorization_consumed_observed_at_utc=str(
            _nested(observation, "authorization_consumed_anchor")["observed_at_utc"]
        ),
        manifest_observed_at_utc=str(
            _nested(observation, "manifest_observation")["observed_at_utc"]
        ),
        grant_expires_at_utc=str(decoded["authorization_grant"]["expires_at_utc"]),
        envelope_valid_until_utc=str(
            decoded["preregistration_envelope"]["valid_until_utc"]
        ),
    )


__all__ = ["VerifiedPreReceiptDocuments", "verify_pre_receipt_documents"]
