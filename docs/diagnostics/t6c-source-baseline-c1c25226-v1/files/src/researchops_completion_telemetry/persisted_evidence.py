"""Read-only validation of already-persisted live completion evidence.

This module deliberately cannot create captures, runtime plans, ledger sessions,
Provider clients, or runtime bindings.  It copies a verified offline mapping
selection and an optional signed denominator-plan projection into an opaque
validation context, then applies the same pure validators to evidence already
persisted by the live writer.  Its summary API returns only frozen ordinals,
counts, normalized usage, and predecessor closure reasons.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .mapping import map_completion
from .sanitization import (
    CompletionTelemetryError,
    ValidatedRuntimeAttemptSummary as PersistedRuntimeAttemptSummary,
    ValidatedRuntimeCaseSummary as PersistedRuntimeCaseSummary,
    ValidatedRuntimeDenominatorSummary as PersistedRuntimeDenominatorSummary,
    ValidatedRuntimeResponseSummary as PersistedRuntimeResponseSummary,
    ValidatedRuntimeUsageIndexSummary as PersistedRuntimeUsageIndexSummary,
    _canonical_json_bytes,
    _validate_binding_snapshot,
    _validate_completion_record,
    _validate_runtime_denominator_artifact_core,
    _validate_runtime_denominator_plan_snapshot,
)


_CONTEXT_TOKEN = object()
_EXPECTED_BINDING_FIELDS = frozenset(
    {
        "telemetry_schema_sha256",
        "adapter_version",
        "mapping_schema_version",
        "mapping_version",
        "mapping_sha256",
        "provider_id",
        "api_surface",
        "transport_id",
        "output_counter_comparability",
        "output_counter_path",
    }
)


def _error(code: str) -> CompletionTelemetryError:
    return CompletionTelemetryError(code)


class PersistedLiveEvidenceValidationContext:
    """Opaque, immutable, validation-only copy of one verified mapping."""

    __slots__ = (
        "_binding_bytes",
        "_mapping_bytes",
        "_plan_bytes",
        "_plan_commitment",
        "_token",
        "_locked",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError(
            "PersistedLiveEvidenceValidationContext requires a verified offline selection"
        )

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("PersistedLiveEvidenceValidationContext is immutable")
        object.__setattr__(self, name, value)

    @classmethod
    def _create(
        cls,
        token: object,
        *,
        binding: Mapping[str, Any],
        mapping: Mapping[str, Any],
        denominator_plan: Mapping[str, Any] | None,
        denominator_plan_commitment_sha256: str | None,
    ) -> PersistedLiveEvidenceValidationContext:
        if token is not _CONTEXT_TOKEN:
            raise TypeError("invalid persisted-evidence context token")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_binding_bytes", _canonical_json_bytes(binding))
        object.__setattr__(instance, "_mapping_bytes", _canonical_json_bytes(mapping))
        object.__setattr__(
            instance,
            "_plan_bytes",
            (
                None
                if denominator_plan is None
                else _canonical_json_bytes(denominator_plan)
            ),
        )
        object.__setattr__(
            instance,
            "_plan_commitment",
            denominator_plan_commitment_sha256,
        )
        object.__setattr__(instance, "_token", _CONTEXT_TOKEN)
        object.__setattr__(instance, "_locked", True)
        return instance

    def _binding(self) -> dict[str, Any]:
        if getattr(self, "_token", None) is not _CONTEXT_TOKEN:
            raise _error("completion_persisted_evidence_context_invalid")
        return json.loads(self._binding_bytes.decode("utf-8"))

    def _mapping(self) -> dict[str, Any]:
        if getattr(self, "_token", None) is not _CONTEXT_TOKEN:
            raise _error("completion_persisted_evidence_context_invalid")
        return json.loads(self._mapping_bytes.decode("utf-8"))

    def _denominator_plan(self) -> tuple[dict[str, Any], str]:
        if getattr(self, "_token", None) is not _CONTEXT_TOKEN:
            raise _error("completion_persisted_evidence_context_invalid")
        plan_bytes = self._plan_bytes
        commitment = self._plan_commitment
        if type(plan_bytes) is not bytes or type(commitment) is not str:
            raise _error("completion_persisted_evidence_plan_required")
        plan = json.loads(plan_bytes.decode("utf-8"))
        if not isinstance(plan, dict):  # pragma: no cover - constructor invariant
            raise _error("completion_persisted_evidence_context_invalid")
        return plan, commitment


def create_persisted_live_evidence_validation_context(
    selection: object,
    *,
    expected_binding: Mapping[str, Any],
    expected_denominator_plan: Mapping[str, Any] | None = None,
    expected_denominator_plan_commitment_sha256: str | None = None,
) -> PersistedLiveEvidenceValidationContext:
    """Copy one verified offline surface selection into a read-only context.

    ``expected_binding`` and the optional denominator plan/commitment are exact
    projections signed by the external preregistration envelope.  They are
    copied only after validation and are never treated as runtime authority.
    """

    # Import only while constructing a context.  Importing this module remains
    # free of the registry/runtime surface and cannot mint a runtime binding.
    from .surface_mapping import VerifiedSurfaceSelection

    if (
        type(selection) is not VerifiedSurfaceSelection
        or selection.purpose != "offline_validation"
    ):
        raise _error("completion_persisted_evidence_selection_invalid")
    if (
        not isinstance(expected_binding, Mapping)
        or set(expected_binding) != _EXPECTED_BINDING_FIELDS
        or any(not isinstance(key, str) for key in expected_binding)
    ):
        raise _error("completion_persisted_evidence_binding_invalid")
    observed = {
        "telemetry_schema_sha256": selection.telemetry_schema_sha256,
        "adapter_version": selection.adapter_version,
        "mapping_schema_version": selection.mapping_schema_version,
        "mapping_version": selection.mapping_version,
        "mapping_sha256": selection.mapping_sha256,
        "provider_id": selection.provider_id,
        "api_surface": selection.api_surface,
        "transport_id": selection.transport_id,
        "output_counter_comparability": selection.output_counter_comparability,
        "output_counter_path": selection.output_counter_path,
    }
    validated = _validate_binding_snapshot(dict(expected_binding))
    if validated != observed:
        raise _error("completion_persisted_evidence_binding_mismatch")
    if (expected_denominator_plan is None) != (
        expected_denominator_plan_commitment_sha256 is None
    ):
        raise _error("completion_persisted_evidence_plan_invalid")
    validated_plan: Mapping[str, Any] | None = None
    if expected_denominator_plan is not None:
        if not isinstance(expected_denominator_plan, Mapping):
            raise _error("completion_persisted_evidence_plan_invalid")
        validated_plan = _validate_runtime_denominator_plan_snapshot(
            expected_denominator_plan,
            binding_snapshot=validated,
            preregistration_commitment=(
                expected_denominator_plan_commitment_sha256
            ),
        )
    mapping = selection.mapping_snapshot()
    return PersistedLiveEvidenceValidationContext._create(
        _CONTEXT_TOKEN,
        binding=validated,
        mapping=mapping,
        denominator_plan=validated_plan,
        denominator_plan_commitment_sha256=(
            expected_denominator_plan_commitment_sha256
        ),
    )


def validate_persisted_live_completion_record(
    record: Mapping[str, Any],
    *,
    context: PersistedLiveEvidenceValidationContext,
    sensitive_canaries: Iterable[str] = (),
) -> None:
    """Validate one persisted ``live_adapter_write`` record without mutation."""

    if type(context) is not PersistedLiveEvidenceValidationContext:
        raise _error("completion_persisted_evidence_context_invalid")
    binding = context._binding()
    mapping = context._mapping()

    def resolver(projection: Mapping[str, Any]):
        return map_completion(projection, binding["provider_id"], mapping)

    _validate_completion_record(
        record,
        binding=binding,
        mapping_resolver=resolver,
        allowed_provenances=frozenset({"live_adapter_write"}),
        sensitive_canaries=sensitive_canaries,
    )


def validate_persisted_live_completion_records(
    records: Sequence[Mapping[str, Any]],
    *,
    context: PersistedLiveEvidenceValidationContext,
    sensitive_canaries: Iterable[str] = (),
) -> int:
    """Validate an ordered record sequence and return only its count."""

    if not isinstance(records, Sequence) or isinstance(
        records, (str, bytes, bytearray)
    ):
        raise _error("completion_persisted_evidence_records_invalid")
    canaries = tuple(sensitive_canaries)
    for record in records:
        validate_persisted_live_completion_record(
            record,
            context=context,
            sensitive_canaries=canaries,
        )
    return len(records)


def validate_persisted_live_runtime_denominator_artifact(
    artifact: Mapping[str, Any],
    *,
    context: PersistedLiveEvidenceValidationContext,
    sensitive_canaries: Iterable[str] = (),
) -> None:
    """Validate a persisted live denominator without minting runtime authority."""

    summarize_persisted_live_runtime_denominator_artifact(
        artifact,
        context=context,
        sensitive_canaries=sensitive_canaries,
    )


def summarize_persisted_live_runtime_denominator_artifact(
    artifact: Mapping[str, Any],
    *,
    context: PersistedLiveEvidenceValidationContext,
    sensitive_canaries: Iterable[str] = (),
) -> PersistedRuntimeDenominatorSummary:
    """Return only frozen ordinal/count/usage facts from validated evidence."""

    if type(context) is not PersistedLiveEvidenceValidationContext:
        raise _error("completion_persisted_evidence_context_invalid")
    binding = context._binding()
    mapping = context._mapping()
    plan, commitment = context._denominator_plan()

    def resolver(projection: Mapping[str, Any]):
        return map_completion(projection, binding["provider_id"], mapping)

    return _validate_runtime_denominator_artifact_core(
        artifact,
        plan_snapshot=plan,
        binding_snapshot=binding,
        preregistration_commitment=commitment,
        mapping_resolver=resolver,
        sensitive_canaries=sensitive_canaries,
    )


__all__ = [
    "PersistedRuntimeAttemptSummary",
    "PersistedRuntimeCaseSummary",
    "PersistedRuntimeDenominatorSummary",
    "PersistedRuntimeResponseSummary",
    "PersistedRuntimeUsageIndexSummary",
    "PersistedLiveEvidenceValidationContext",
    "create_persisted_live_evidence_validation_context",
    "summarize_persisted_live_runtime_denominator_artifact",
    "validate_persisted_live_completion_record",
    "validate_persisted_live_completion_records",
    "validate_persisted_live_runtime_denominator_artifact",
]
