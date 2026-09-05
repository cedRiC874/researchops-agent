from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from collections.abc import Mapping, Sequence
from typing import Any

from .sanitization import (
    OfflineCompletionRecordBinding,
    SanitizedCompletionCapture,
    build_completion_record,
    build_offline_completion_record,
)
from .surface_mapping import (
    VerifiedRuntimeCompletionBinding,
    VerifiedSurfaceSelection,
)


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
_SAFE_REJECTION_CODE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_EXPECTED_RESPONSES = 2_147_483_647
_COLLECTOR_TOKEN = object()
_PLAN_TOKEN = object()
_DYNAMIC_PLAN_TOKEN = object()
_ATTEMPT_TOKEN = object()
_DENOMINATOR_ALGORITHM = "transport-response-finalization-v1"
RUNTIME_CLOSURE_RECOGNIZED_STATES = frozenset(
    {
        "completed",
        "incomplete_length",
        "incomplete_content_filter",
        "incomplete_other",
        "error",
    }
)
RUNTIME_CLOSURE_REQUIRED_SIGNAL_SOURCE = "native_status"


class CompletionCaptureError(ValueError):
    """Stable error raised before an invalid capture slot can be recorded."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


def _error(code: str, message: str) -> CompletionCaptureError:
    return CompletionCaptureError(code, message)


def _safe_identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise _error(
            "completion_capture_identity_invalid", f"{field_name} is invalid"
        )
    return value


class VerifiedCapturePlanBinding:
    """Opaque binding of a runtime authority to one committed response denominator."""

    __slots__ = (
        "_runtime_binding",
        "_expected_response_count",
        "_preregistration_commitment",
        "_authority_token",
        "_locked",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("VerifiedCapturePlanBinding requires a verified runtime plan")

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("VerifiedCapturePlanBinding is immutable")
        object.__setattr__(self, name, value)

    @classmethod
    def _create(
        cls,
        token: object,
        *,
        runtime_binding: VerifiedRuntimeCompletionBinding,
        expected_response_count: int,
        preregistration_commitment: str,
    ) -> VerifiedCapturePlanBinding:
        if token is not _PLAN_TOKEN:
            raise TypeError("invalid capture plan construction token")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_runtime_binding", runtime_binding)
        object.__setattr__(
            instance, "_expected_response_count", expected_response_count
        )
        object.__setattr__(
            instance, "_preregistration_commitment", preregistration_commitment
        )
        object.__setattr__(instance, "_authority_token", _PLAN_TOKEN)
        object.__setattr__(instance, "_locked", True)
        return instance

    @property
    def expected_response_count(self) -> int:
        return self._expected_response_count

    @property
    def preregistration_commitment(self) -> str:
        return self._preregistration_commitment

    def assert_plan_authority(self) -> None:
        if (
            type(self) is not VerifiedCapturePlanBinding
            or getattr(self, "_authority_token", None) is not _PLAN_TOKEN
        ):
            raise _error(
                "completion_capture_plan_authority_missing",
                "runtime capture plan authority is invalid",
            )
        try:
            self._runtime_binding.assert_runtime_authority()
        except Exception:
            raise _error(
                "completion_capture_plan_authority_missing",
                "runtime capture plan authority is invalid",
            ) from None

    def runtime_binding(self) -> VerifiedRuntimeCompletionBinding:
        self.assert_plan_authority()
        return self._runtime_binding


def verify_runtime_capture_plan(
    binding: VerifiedRuntimeCompletionBinding,
    plan: Mapping[str, Any],
    *,
    preregistration_commitment: str,
) -> VerifiedCapturePlanBinding:
    """Verify an exact runtime plan and its externally supplied commitment."""

    if type(binding) is not VerifiedRuntimeCompletionBinding:
        raise _error(
            "completion_capture_runtime_binding_required",
            "runtime plan requires verified runtime authority",
        )
    try:
        binding.assert_runtime_authority()
        runtime = binding.runtime_snapshot()
    except Exception:
        raise _error(
            "completion_capture_runtime_binding_required",
            "runtime plan requires verified runtime authority",
        ) from None
    expected_fields = {
        "schema_version",
        "provider_id",
        "api_surface",
        "transport_id",
        "adapter_version",
        "telemetry_schema_sha256",
        "mapping_schema_version",
        "mapping_version",
        "mapping_sha256",
        "expected_response_count",
    }
    if not isinstance(plan, Mapping) or set(plan) != expected_fields:
        raise _error(
            "completion_capture_plan_invalid", "runtime plan has an invalid field set"
        )
    expected_response_count = plan.get("expected_response_count")
    if (
        plan.get("schema_version") != "provider-completion-capture-plan/1.0"
        or type(expected_response_count) is not int
        or not 1 <= expected_response_count <= _MAX_EXPECTED_RESPONSES
        or any(plan.get(field) != runtime[field] for field in expected_fields - {
            "schema_version",
            "expected_response_count",
        })
    ):
        raise _error(
            "completion_capture_plan_invalid",
            "runtime plan differs from its verified completion binding",
        )
    if (
        not isinstance(preregistration_commitment, str)
        or not _SHA256.fullmatch(preregistration_commitment)
    ):
        raise _error(
            "completion_capture_preregistration_invalid",
            "preregistration commitment must be a lowercase SHA-256",
        )
    try:
        canonical = json.dumps(
            plan,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _error(
            "completion_capture_plan_invalid", "runtime plan is not canonical JSON"
        ) from exc
    if hashlib.sha256(canonical).hexdigest() != preregistration_commitment:
        raise _error(
            "completion_capture_plan_commitment_mismatch",
            "runtime plan differs from its preregistration commitment",
        )
    return VerifiedCapturePlanBinding._create(
        _PLAN_TOKEN,
        runtime_binding=binding,
        expected_response_count=expected_response_count,
        preregistration_commitment=preregistration_commitment,
    )


@dataclass(frozen=True, slots=True)
class CompletionCaptureSlot:
    """One denominator-preserving accepted or rejected capture position."""

    provider_id: str
    api_surface: str
    transport_id: str
    adapter_version: str
    response_index: int
    request_index: int
    capture_status: str
    _capture: SanitizedCompletionCapture | None = field(repr=False)
    rejection_code: str | None = None

    @property
    def capture(self) -> SanitizedCompletionCapture | None:
        return (
            self._capture._collector_snapshot()
            if self._capture is not None
            else None
        )

    def mapping_projection(self) -> dict[str, object] | None:
        return (
            self._capture.mapping_projection()
            if self._capture is not None
            else None
        )

    def record_components(self) -> dict[str, object] | None:
        return (
            self._capture.record_components()
            if self._capture is not None
            else None
        )


@dataclass(frozen=True, slots=True)
class CompletionCaptureArtifact:
    """Frozen denominator/plan binding produced by a completed collector."""

    provider_id: str
    api_surface: str
    transport_id: str
    adapter_version: str
    expected_response_count: int
    preregistration_commitment: str
    collection_purpose: str
    slots: tuple[CompletionCaptureSlot, ...]
    offline_selection: VerifiedSurfaceSelection | None = field(
        default=None, repr=False
    )
    runtime_plan_binding: VerifiedCapturePlanBinding | None = field(
        default=None, repr=False
    )

    def _validate_structure(self) -> None:
        if (
            type(self.expected_response_count) is not int
            or self.expected_response_count < 1
            or not isinstance(self.preregistration_commitment, str)
            or not _SHA256.fullmatch(self.preregistration_commitment)
            or len(self.slots) != self.expected_response_count
        ):
            raise _error(
                "completion_capture_artifact_invalid",
                "capture artifact denominator or commitment is invalid",
            )
        expected_indices = list(range(self.expected_response_count))
        if [slot.response_index for slot in self.slots] != expected_indices or [
            slot.request_index for slot in self.slots
        ] != expected_indices:
            raise _error(
                "completion_capture_artifact_invalid",
                "capture artifact indices do not cover the denominator",
            )
        for slot in self.slots:
            if (
                slot.provider_id != self.provider_id
                or slot.api_surface != self.api_surface
                or slot.transport_id != self.transport_id
                or slot.adapter_version != self.adapter_version
                or slot.capture_status not in {"accepted", "rejected"}
                or (
                    slot.capture_status == "accepted"
                    and (slot._capture is None or slot.rejection_code is not None)
                )
                or (
                    slot.capture_status == "rejected"
                    and (slot._capture is not None or slot.rejection_code is None)
                )
            ):
                raise _error(
                    "completion_capture_artifact_invalid",
                    "capture artifact slot identity or state is invalid",
                )
        if self.collection_purpose == "offline_validation":
            selection = self.offline_selection
            if (
                type(selection) is not VerifiedSurfaceSelection
                or selection.purpose != "offline_validation"
                or self.runtime_plan_binding is not None
                or selection.provider_id != self.provider_id
                or selection.api_surface != self.api_surface
                or selection.transport_id != self.transport_id
                or selection.adapter_version != self.adapter_version
            ):
                raise _error(
                    "completion_capture_offline_selection_required",
                    "offline artifact authority or identity is invalid",
                )
            selection.mapping_snapshot()
        elif self.collection_purpose == "runtime_binding":
            plan = self.runtime_plan_binding
            if type(plan) is not VerifiedCapturePlanBinding or self.offline_selection is not None:
                raise _error(
                    "completion_capture_plan_authority_missing",
                    "runtime artifact requires verified plan authority",
                )
            plan.assert_plan_authority()
            binding = plan.runtime_binding()
            runtime = binding.runtime_snapshot()
            if (
                plan.expected_response_count != self.expected_response_count
                or plan.preregistration_commitment != self.preregistration_commitment
                or runtime["provider_id"] != self.provider_id
                or runtime["api_surface"] != self.api_surface
                or runtime["transport_id"] != self.transport_id
                or runtime["adapter_version"] != self.adapter_version
            ):
                raise _error(
                    "completion_capture_artifact_invalid",
                    "runtime artifact differs from its plan binding",
                )
        else:
            raise _error(
                "completion_capture_purpose_invalid", "artifact purpose is invalid"
            )

    @property
    def has_rejections(self) -> bool:
        return any(slot.capture_status == "rejected" for slot in self.slots)

    def denominator_binding(self) -> dict[str, object]:
        self._validate_structure()
        return {
            "provider_id": self.provider_id,
            "api_surface": self.api_surface,
            "transport_id": self.transport_id,
            "adapter_version": self.adapter_version,
            "expected_response_count": self.expected_response_count,
            "preregistration_commitment": self.preregistration_commitment,
            "collection_purpose": self.collection_purpose,
            "response_indices": [slot.response_index for slot in self.slots],
            "request_indices": [slot.request_index for slot in self.slots],
            "rejected_response_indices": [
                slot.response_index
                for slot in self.slots
                if slot.capture_status == "rejected"
            ],
        }

    def to_record_artifact(self) -> dict[str, object]:
        """Build exact T2 records from the frozen captures, never caller records."""

        self._validate_structure()
        if self.has_rejections:
            raise _error(
                "completion_capture_rejected_slot_present",
                "a rejected capture slot cannot become a complete record artifact",
            )
        records: list[dict[str, Any]] = []
        if self.collection_purpose == "runtime_binding":
            plan = self.runtime_plan_binding
            if type(plan) is not VerifiedCapturePlanBinding:  # validated above
                raise AssertionError("runtime artifact lost plan authority")
            binding = plan.runtime_binding()
            try:
                binding.assert_runtime_authority()
            except Exception:
                raise _error(
                    "completion_capture_runtime_binding_required",
                    "runtime artifact lost its verified authority",
                ) from None
            for slot in self.slots:
                if slot.capture is None:  # pragma: no cover - has_rejections guard
                    raise AssertionError("accepted runtime slot lost capture")
                records.append(
                    build_completion_record(
                        slot.capture,
                        binding=binding,
                        response_index=slot.response_index,
                        request_index=slot.request_index,
                    )
                )
        elif self.collection_purpose == "offline_validation":
            selection = self.offline_selection
            if (
                type(selection) is not VerifiedSurfaceSelection
                or selection.purpose != "offline_validation"
            ):
                raise _error(
                    "completion_capture_offline_selection_required",
                    "offline artifact lost its verified selection",
                )
            selection.mapping_snapshot()
            binding = OfflineCompletionRecordBinding(
                telemetry_schema_sha256=selection.telemetry_schema_sha256,
                adapter_version=selection.adapter_version,
                mapping_schema_version=selection.mapping_schema_version,
                mapping_version=selection.mapping_version,
                mapping_sha256=selection.mapping_sha256,
                provider_id=selection.provider_id,
                api_surface=selection.api_surface,
                transport_id=selection.transport_id,
            )

            def resolver(
                projection: Mapping[str, Any],
                provider_id: str,
                api_surface: str,
                transport_id: str,
            ) -> object:
                if (
                    provider_id != selection.provider_id
                    or api_surface != selection.api_surface
                    or transport_id != selection.transport_id
                ):
                    raise _error(
                        "completion_capture_record_identity_mismatch",
                        "offline record binding differs from its selection",
                    )
                return selection.resolve_mapping(projection)

            for slot in self.slots:
                if slot.capture is None:  # pragma: no cover - has_rejections guard
                    raise AssertionError("accepted offline slot lost capture")
                records.append(
                    build_offline_completion_record(
                        slot.capture,
                        binding=binding,
                        response_index=slot.response_index,
                        request_index=slot.request_index,
                        mapping_resolver=resolver,
                    )
                )
        else:  # pragma: no cover - collector construction invariant
            raise _error(
                "completion_capture_purpose_invalid", "artifact purpose is invalid"
            )
        return {
            "schema_version": "provider-completion-telemetry-artifact/1.0",
            "expected_response_count": self.expected_response_count,
            "preregistration_commitment": self.preregistration_commitment,
            "records": records,
        }


class CompletionTelemetryCollector:
    """Per-context collector with a preregistered, gap-free denominator.

    ``append`` accepts only the opaque result of
    ``sanitize_completion_capture``. If sanitization or record construction is
    rejected, the caller must use ``append_rejection`` for the same request
    index so a failed write cannot disappear through response renumbering.
    """

    __slots__ = (
        "_provider_id",
        "_api_surface",
        "_transport_id",
        "_adapter_version",
        "_expected_response_count",
        "_preregistration_commitment",
        "_slots",
        "_sealed",
        "_collection_purpose",
        "_offline_selection",
        "_runtime_plan_binding",
    )

    def __init__(
        self,
        *,
        _token: object,
        provider_id: str,
        api_surface: str,
        transport_id: str,
        adapter_version: str,
        collection_purpose: str,
        offline_selection: VerifiedSurfaceSelection | None,
        runtime_plan_binding: VerifiedCapturePlanBinding | None,
        expected_response_count: int,
        preregistration_commitment: str,
    ) -> None:
        if _token is not _COLLECTOR_TOKEN:
            raise _error(
                "completion_capture_construction_forbidden",
                "collector requires a verified surface or runtime binding",
            )
        self._provider_id = _safe_identifier(provider_id, field_name="provider_id")
        self._api_surface = _safe_identifier(
            api_surface, field_name="api_surface"
        )
        self._transport_id = _safe_identifier(
            transport_id, field_name="transport_id"
        )
        self._adapter_version = _safe_identifier(
            adapter_version, field_name="adapter_version"
        )
        if collection_purpose not in {"offline_validation", "runtime_binding"}:
            raise _error(
                "completion_capture_purpose_invalid",
                "collector purpose is invalid",
            )
        self._collection_purpose = collection_purpose
        if collection_purpose == "offline_validation":
            if (
                type(offline_selection) is not VerifiedSurfaceSelection
                or offline_selection.purpose != "offline_validation"
                or runtime_plan_binding is not None
            ):
                raise _error(
                    "completion_capture_offline_selection_required",
                    "offline collector requires exactly one offline selection",
                )
            offline_selection.mapping_snapshot()
        elif (
            type(runtime_plan_binding) is not VerifiedCapturePlanBinding
            or offline_selection is not None
        ):
            raise _error(
                "completion_capture_runtime_binding_required",
                "runtime collector requires exactly one runtime binding",
            )
        self._offline_selection = offline_selection
        self._runtime_plan_binding = runtime_plan_binding
        if (
            type(expected_response_count) is not int
            or not 1 <= expected_response_count <= _MAX_EXPECTED_RESPONSES
        ):
            raise _error(
                "completion_capture_expected_count_invalid",
                "expected response count must be a positive bounded integer",
            )
        self._expected_response_count = expected_response_count
        if (
            not isinstance(preregistration_commitment, str)
            or not _SHA256.fullmatch(preregistration_commitment)
        ):
            raise _error(
                "completion_capture_preregistration_invalid",
                "preregistration commitment must be a lowercase SHA-256",
            )
        self._preregistration_commitment = preregistration_commitment
        self._slots: list[CompletionCaptureSlot] = []
        self._sealed = False

    @classmethod
    def for_offline_validation(
        cls,
        selection: VerifiedSurfaceSelection,
        *,
        expected_response_count: int,
        preregistration_commitment: str,
    ) -> CompletionTelemetryCollector:
        if (
            type(selection) is not VerifiedSurfaceSelection
            or selection.purpose != "offline_validation"
        ):
            raise _error(
                "completion_capture_offline_selection_required",
                "offline collector requires a verified offline selection",
            )
        # This also verifies the selection's private authority token.
        selection.mapping_snapshot()
        return cls(
            _token=_COLLECTOR_TOKEN,
            provider_id=selection.provider_id,
            api_surface=selection.api_surface,
            transport_id=selection.transport_id,
            adapter_version=selection.adapter_version,
            collection_purpose="offline_validation",
            offline_selection=selection,
            runtime_plan_binding=None,
            expected_response_count=expected_response_count,
            preregistration_commitment=preregistration_commitment,
        )

    @classmethod
    def for_runtime(
        cls,
        plan_binding: VerifiedRuntimeDenominatorPlanBinding,
    ) -> RuntimeDenominatorTracker:
        """Create the dynamic runtime tracker.

        Runtime response counts are derived from finalized transport attempts;
        they are never supplied as an exact pre-run denominator.
        """

        del cls
        return RuntimeDenominatorTracker(plan_binding)

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def api_surface(self) -> str:
        return self._api_surface

    @property
    def transport_id(self) -> str:
        return self._transport_id

    @property
    def adapter_version(self) -> str:
        return self._adapter_version

    @property
    def expected_response_count(self) -> int:
        return self._expected_response_count

    @property
    def observed_slot_count(self) -> int:
        return len(self._slots)

    @property
    def preregistration_commitment(self) -> str:
        return self._preregistration_commitment

    @property
    def collection_purpose(self) -> str:
        return self._collection_purpose

    @property
    def sealed(self) -> bool:
        return self._sealed

    def _next_response_index(self, request_index: int) -> int:
        if self._sealed:
            raise _error(
                "completion_capture_sealed", "cannot append after snapshot"
            )
        if type(request_index) is not int or request_index < 0:
            raise _error(
                "completion_capture_request_index_invalid",
                "request index must be a non-negative integer",
            )
        expected_index = len(self._slots)
        if expected_index >= self._expected_response_count:
            raise _error(
                "completion_capture_count_exceeded",
                "capture count exceeds the preregistered denominator",
            )
        if request_index < expected_index:
            raise _error(
                "completion_capture_request_index_duplicate_or_reordered",
                "request index is duplicated or reordered",
            )
        if request_index > expected_index:
            raise _error(
                "completion_capture_request_index_gap",
                "request indices must be contiguous",
            )
        return expected_index

    def append(
        self,
        request_index: int,
        capture: SanitizedCompletionCapture,
    ) -> CompletionCaptureSlot:
        response_index = self._next_response_index(request_index)
        if type(capture) is not SanitizedCompletionCapture:
            raise _error(
                "completion_capture_sanitized_type_required",
                "append requires sanitize_completion_capture output",
            )
        if capture.historical:
            raise _error(
                "completion_capture_historical_forbidden",
                "a live Provider context cannot collect a historical projection",
            )
        # Retain a separately owned canonical snapshot. Even a caller that
        # deliberately bypasses the capture's normal attribute lock cannot
        # mutate the collector's copy through its original reference.
        owned_capture = capture._collector_snapshot()
        slot = CompletionCaptureSlot(
            provider_id=self._provider_id,
            api_surface=self._api_surface,
            transport_id=self._transport_id,
            adapter_version=self._adapter_version,
            response_index=response_index,
            request_index=request_index,
            capture_status="accepted",
            _capture=owned_capture,
        )
        self._slots.append(slot)
        return slot

    def append_rejection(
        self,
        request_index: int,
        rejection_code: str,
    ) -> CompletionCaptureSlot:
        response_index = self._next_response_index(request_index)
        if (
            not isinstance(rejection_code, str)
            or not _SAFE_REJECTION_CODE.fullmatch(rejection_code)
        ):
            raise _error(
                "completion_capture_rejection_code_invalid",
                "rejection code must be a stable safe identifier",
            )
        slot = CompletionCaptureSlot(
            provider_id=self._provider_id,
            api_surface=self._api_surface,
            transport_id=self._transport_id,
            adapter_version=self._adapter_version,
            response_index=response_index,
            request_index=request_index,
            capture_status="rejected",
            _capture=None,
            rejection_code=rejection_code,
        )
        self._slots.append(slot)
        return slot

    def snapshot(self) -> CompletionCaptureArtifact:
        if len(self._slots) != self._expected_response_count:
            raise _error(
                "completion_capture_denominator_incomplete",
                "snapshot requires every preregistered response slot",
            )
        self._sealed = True
        return CompletionCaptureArtifact(
            provider_id=self._provider_id,
            api_surface=self._api_surface,
            transport_id=self._transport_id,
            adapter_version=self._adapter_version,
            expected_response_count=self._expected_response_count,
            preregistration_commitment=self._preregistration_commitment,
            collection_purpose=self._collection_purpose,
            slots=tuple(self._slots),
            offline_selection=self._offline_selection,
            runtime_plan_binding=self._runtime_plan_binding,
        )


class VerifiedRuntimeDenominatorPlanBinding:
    """Opaque runtime authority with a preregistered derivation rule, not an exact N."""

    __slots__ = (
        "_runtime_binding",
        "_case_ids",
        "_case_ids_sha256",
        "_max_turns_per_case",
        "_total_model_request_cap",
        "_preregistration_commitment",
        "_authority_token",
        "_locked",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError(
            "VerifiedRuntimeDenominatorPlanBinding requires a verified runtime plan"
        )

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("VerifiedRuntimeDenominatorPlanBinding is immutable")
        object.__setattr__(self, name, value)

    @classmethod
    def _create(
        cls,
        token: object,
        *,
        runtime_binding: VerifiedRuntimeCompletionBinding,
        case_ids: tuple[str, ...],
        case_ids_sha256: str,
        max_turns_per_case: int,
        total_model_request_cap: int,
        preregistration_commitment: str,
    ) -> VerifiedRuntimeDenominatorPlanBinding:
        if token is not _DYNAMIC_PLAN_TOKEN:
            raise TypeError("invalid runtime denominator plan construction token")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_runtime_binding", runtime_binding)
        object.__setattr__(instance, "_case_ids", case_ids)
        object.__setattr__(instance, "_case_ids_sha256", case_ids_sha256)
        object.__setattr__(instance, "_max_turns_per_case", max_turns_per_case)
        object.__setattr__(
            instance, "_total_model_request_cap", total_model_request_cap
        )
        object.__setattr__(
            instance, "_preregistration_commitment", preregistration_commitment
        )
        object.__setattr__(instance, "_authority_token", _DYNAMIC_PLAN_TOKEN)
        object.__setattr__(instance, "_locked", True)
        return instance

    def assert_plan_authority(self) -> None:
        if (
            type(self) is not VerifiedRuntimeDenominatorPlanBinding
            or getattr(self, "_authority_token", None) is not _DYNAMIC_PLAN_TOKEN
        ):
            raise _error(
                "completion_capture_plan_authority_missing",
                "runtime denominator plan authority is invalid",
            )
        try:
            self._runtime_binding.assert_runtime_authority()
        except Exception:
            raise _error(
                "completion_capture_plan_authority_missing",
                "runtime denominator plan authority is invalid",
            ) from None

    @property
    def case_ids(self) -> tuple[str, ...]:
        return self._case_ids

    @property
    def case_ids_sha256(self) -> str:
        return self._case_ids_sha256

    @property
    def max_turns_per_case(self) -> int:
        return self._max_turns_per_case

    @property
    def total_model_request_cap(self) -> int:
        return self._total_model_request_cap

    @property
    def preregistration_commitment(self) -> str:
        return self._preregistration_commitment

    @property
    def denominator_algorithm(self) -> str:
        return _DENOMINATOR_ALGORITHM

    @property
    def exact_response_count_preregistered(self) -> bool:
        return False

    def runtime_binding(self) -> VerifiedRuntimeCompletionBinding:
        self.assert_plan_authority()
        return self._runtime_binding


def _canonical_sha256(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _error(
            "completion_capture_plan_invalid", "runtime plan is not canonical JSON"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def verify_runtime_denominator_plan(
    binding: VerifiedRuntimeCompletionBinding,
    plan: Mapping[str, Any],
    *,
    preregistration_commitment: str,
) -> VerifiedRuntimeDenominatorPlanBinding:
    """Bind a runtime authority to a pre-run case set, limits, and derivation rule."""

    if type(binding) is not VerifiedRuntimeCompletionBinding:
        raise _error(
            "completion_capture_runtime_binding_required",
            "runtime denominator plan requires verified runtime authority",
        )
    try:
        binding.assert_runtime_authority()
        runtime = binding.runtime_snapshot()
    except Exception:
        raise _error(
            "completion_capture_runtime_binding_required",
            "runtime denominator plan requires verified runtime authority",
        ) from None
    expected_fields = {
        "schema_version",
        "provider_id",
        "api_surface",
        "transport_id",
        "adapter_version",
        "telemetry_schema_sha256",
        "mapping_schema_version",
        "mapping_version",
        "mapping_sha256",
        "case_ids",
        "case_ids_sha256",
        "max_turns_per_case",
        "total_model_request_cap",
        "agents_sdk_retries",
        "http_client_retries",
        "denominator_algorithm",
        "exact_response_count_preregistered",
    }
    if not isinstance(plan, Mapping) or set(plan) != expected_fields:
        raise _error(
            "completion_capture_plan_invalid", "runtime plan has an invalid field set"
        )
    raw_case_ids = plan.get("case_ids")
    if (
        not isinstance(raw_case_ids, Sequence)
        or isinstance(raw_case_ids, (str, bytes, bytearray))
        or not raw_case_ids
    ):
        raise _error("completion_capture_plan_invalid", "case IDs are invalid")
    case_ids: list[str] = []
    for raw_case_id in raw_case_ids:
        case_ids.append(_safe_identifier(raw_case_id, field_name="case_id"))
    if len(case_ids) != len(set(case_ids)):
        raise _error("completion_capture_plan_invalid", "case IDs repeat")
    case_ids_sha256 = _canonical_sha256(case_ids)
    max_turns = plan.get("max_turns_per_case")
    request_cap = plan.get("total_model_request_cap")
    if (
        plan.get("schema_version")
        != "provider-completion-runtime-denominator-plan/1.0"
        or plan.get("denominator_algorithm") != _DENOMINATOR_ALGORITHM
        or plan.get("exact_response_count_preregistered") is not False
        or type(plan.get("agents_sdk_retries")) is not int
        or plan.get("agents_sdk_retries") != 0
        or type(plan.get("http_client_retries")) is not int
        or plan.get("http_client_retries") != 0
        or type(max_turns) is not int
        or not 1 <= max_turns <= _MAX_EXPECTED_RESPONSES
        or type(request_cap) is not int
        or not 1 <= request_cap <= _MAX_EXPECTED_RESPONSES
        or request_cap > len(case_ids) * max_turns
        or plan.get("case_ids_sha256") != case_ids_sha256
        or any(
            plan.get(field) != runtime[field]
            for field in {
                "provider_id",
                "api_surface",
                "transport_id",
                "adapter_version",
                "telemetry_schema_sha256",
                "mapping_schema_version",
                "mapping_version",
                "mapping_sha256",
            }
        )
    ):
        raise _error(
            "completion_capture_plan_invalid",
            "runtime denominator plan differs from its verified binding or limits",
        )
    if (
        not isinstance(preregistration_commitment, str)
        or not _SHA256.fullmatch(preregistration_commitment)
        or _canonical_sha256(plan) != preregistration_commitment
    ):
        raise _error(
            "completion_capture_plan_commitment_mismatch",
            "runtime denominator plan differs from its preregistration commitment",
        )
    return VerifiedRuntimeDenominatorPlanBinding._create(
        _DYNAMIC_PLAN_TOKEN,
        runtime_binding=binding,
        case_ids=tuple(case_ids),
        case_ids_sha256=case_ids_sha256,
        max_turns_per_case=max_turns,
        total_model_request_cap=request_cap,
        preregistration_commitment=preregistration_commitment,
    )


@dataclass(frozen=True, slots=True)
class RuntimeAttemptHandle:
    """Opaque handle allocated before one outbound model request."""

    case_id: str
    attempt_index: int
    case_attempt_index: int
    _tracker_token: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class RuntimeAttemptTerminal:
    case_id: str
    attempt_index: int
    case_attempt_index: int
    terminal_kind: str
    response_index: int | None
    _capture: SanitizedCompletionCapture | None = field(repr=False, compare=False)
    error_code: str | None = None

    @property
    def provider_response_observed(self) -> bool:
        return self.terminal_kind in {"response_accepted", "response_rejected"}


@dataclass(frozen=True, slots=True)
class RuntimeCaseReconciliation:
    case_id: str
    attempts_started: int
    attempts_terminal: int
    observed_response_count: int
    accepted_response_count: int
    rejected_response_count: int
    sdk_raw_response_count: int | None
    sdk_raw_response_reconciliation: str
    sdk_usage_request_count: int | None
    sdk_usage_request_reconciliation: str
    sdk_request_usage_indices_by_response: tuple[
        tuple[int, int, tuple[int, ...]], ...
    ]
    closure_eligible: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "attempts_started": self.attempts_started,
            "attempts_terminal": self.attempts_terminal,
            "observed_response_count": self.observed_response_count,
            "accepted_response_count": self.accepted_response_count,
            "rejected_response_count": self.rejected_response_count,
            "sdk_raw_response_count": self.sdk_raw_response_count,
            "sdk_raw_response_reconciliation": self.sdk_raw_response_reconciliation,
            "sdk_usage_request_count": self.sdk_usage_request_count,
            "sdk_usage_request_reconciliation": self.sdk_usage_request_reconciliation,
            "sdk_request_usage_indices_by_response": [
                {
                    "response_index": response_index,
                    "sdk_raw_response_index": sdk_response_index,
                    "sdk_request_usage_indices": list(indices),
                }
                for response_index, sdk_response_index, indices in self.sdk_request_usage_indices_by_response
            ],
            "closure_eligible": self.closure_eligible,
        }


@dataclass(frozen=True, slots=True)
class RuntimeDenominatorArtifact:
    plan_binding: VerifiedRuntimeDenominatorPlanBinding = field(repr=False)
    attempts: tuple[RuntimeAttemptTerminal, ...]
    cases: tuple[RuntimeCaseReconciliation, ...]
    planned_case_ids: tuple[str, ...]
    not_finalized_case_ids: tuple[str, ...]
    observed_response_count: int
    accepted_response_count: int
    rejected_response_count: int

    def _validate_structure(self) -> None:
        self.plan_binding.assert_plan_authority()
        if self.planned_case_ids != self.plan_binding.case_ids:
            raise _error(
                "completion_capture_artifact_invalid",
                "runtime artifact case set differs from its plan",
            )
        if [item.attempt_index for item in self.attempts] != list(
            range(len(self.attempts))
        ):
            raise _error(
                "completion_capture_artifact_invalid",
                "runtime attempt indices are not contiguous",
            )
        observed = tuple(
            item for item in self.attempts if item.provider_response_observed
        )
        if [item.response_index for item in observed] != list(range(len(observed))):
            raise _error(
                "completion_capture_artifact_invalid",
                "runtime response indices are not derived contiguously",
            )
        if any(
            item.response_index is not None
            for item in self.attempts
            if not item.provider_response_observed
        ):
            raise _error(
                "completion_capture_artifact_invalid",
                "a no-response terminal consumes a response index",
            )
        allowed_terminal_kinds = {
            "response_accepted",
            "response_rejected",
            "http_error",
            "no_response",
            "cancelled",
            "outcome_unknown",
        }
        for item in self.attempts:
            if item.terminal_kind not in allowed_terminal_kinds:
                raise _error(
                    "completion_capture_artifact_invalid",
                    "runtime artifact contains an unknown terminal kind",
                )
            if item.terminal_kind == "response_accepted":
                valid_terminal = item._capture is not None and item.error_code is None
            elif item.terminal_kind == "cancelled":
                valid_terminal = item._capture is None and item.error_code is None
            else:
                valid_terminal = (
                    item._capture is None
                    and isinstance(item.error_code, str)
                    and bool(_SAFE_REJECTION_CODE.fullmatch(item.error_code))
                )
            if not valid_terminal:
                raise _error(
                    "completion_capture_artifact_invalid",
                    "runtime artifact terminal payload is inconsistent",
                )
        accepted = sum(item.terminal_kind == "response_accepted" for item in observed)
        rejected = sum(item.terminal_kind == "response_rejected" for item in observed)
        if (
            len(self.attempts) > self.plan_binding.total_model_request_cap
            or len(observed) != self.observed_response_count
            or accepted != self.accepted_response_count
            or rejected != self.rejected_response_count
            or set(self.not_finalized_case_ids)
            != set(self.planned_case_ids) - {item.case_id for item in self.cases}
            or len({item.case_id for item in self.cases}) != len(self.cases)
            or any(item.case_id not in self.planned_case_ids for item in self.attempts)
        ):
            raise _error(
                "completion_capture_artifact_invalid",
                "runtime artifact counts or case reconciliation are inconsistent",
            )
        for case_id in self.planned_case_ids:
            case_attempts = tuple(
                item for item in self.attempts if item.case_id == case_id
            )
            if len(case_attempts) > self.plan_binding.max_turns_per_case or [
                item.case_attempt_index for item in case_attempts
            ] != list(range(len(case_attempts))):
                raise _error(
                    "completion_capture_artifact_invalid",
                    "runtime artifact case-attempt indices or cap are invalid",
                )
        for case in self.cases:
            case_attempts = tuple(
                item for item in self.attempts if item.case_id == case.case_id
            )
            case_observed = tuple(
                item for item in case_attempts if item.provider_response_observed
            )
            case_accepted = sum(
                item.terminal_kind == "response_accepted" for item in case_observed
            )
            case_rejected = sum(
                item.terminal_kind == "response_rejected" for item in case_observed
            )
            expected_raw_reconciliation = (
                "unavailable"
                if case.sdk_raw_response_count is None
                else "matched"
                if case.sdk_raw_response_count == len(case_observed)
                else "mismatched"
            )
            expected_usage_reconciliation = (
                "unavailable"
                if case.sdk_usage_request_count is None
                else "matched"
                if case.sdk_usage_request_count == len(case_attempts)
                else "mismatched"
            )
            expected_closure = (
                bool(case_observed)
                and expected_raw_reconciliation == "matched"
                and expected_usage_reconciliation == "matched"
                and case_rejected == 0
                and all(
                    item.terminal_kind == "response_accepted"
                    for item in case_attempts
                )
            )
            if (
                case.attempts_started != len(case_attempts)
                or case.attempts_terminal != len(case_attempts)
                or case.observed_response_count != len(case_observed)
                or case.accepted_response_count != case_accepted
                or case.rejected_response_count != case_rejected
                or case.sdk_raw_response_reconciliation
                != expected_raw_reconciliation
                or case.sdk_usage_request_reconciliation
                != expected_usage_reconciliation
                or case.closure_eligible is not expected_closure
            ):
                raise _error(
                    "completion_capture_artifact_invalid",
                    "runtime case reconciliation differs from attempt evidence",
                )

    def response_records(self) -> tuple[dict[str, Any], ...]:
        """Build records only from accepted, collector-owned Provider responses."""

        self._validate_structure()
        binding = self.plan_binding.runtime_binding()
        records: list[dict[str, Any]] = []
        for attempt in self.attempts:
            if attempt.terminal_kind != "response_accepted":
                continue
            if attempt.response_index is None or attempt._capture is None:
                raise _error(
                    "completion_capture_artifact_invalid",
                    "accepted response lost its index or sanitized capture",
                )
            records.append(
                build_completion_record(
                    attempt._capture._collector_snapshot(),
                    binding=binding,
                    response_index=attempt.response_index,
                    request_index=attempt.attempt_index,
                )
            )
        return tuple(records)

    def to_dict(self) -> dict[str, Any]:
        self._validate_structure()
        terminal_counts = {
            kind: sum(item.terminal_kind == kind for item in self.attempts)
            for kind in (
                "response_accepted",
                "response_rejected",
                "http_error",
                "no_response",
                "cancelled",
                "outcome_unknown",
            )
        }
        return {
            "schema_version": "provider-completion-runtime-denominator-artifact/1.0",
            "denominator_algorithm": _DENOMINATOR_ALGORITHM,
            "exact_response_count_preregistered": False,
            "derived_after_run": True,
            "preregistration_commitment": self.plan_binding.preregistration_commitment,
            "planned_case_ids": list(self.planned_case_ids),
            "max_turns_per_case": self.plan_binding.max_turns_per_case,
            "total_model_request_cap": self.plan_binding.total_model_request_cap,
            "attempts_started": len(self.attempts),
            "attempts_terminal": len(self.attempts),
            "observed_response_count": self.observed_response_count,
            "accepted_response_count": self.accepted_response_count,
            "rejected_response_count": self.rejected_response_count,
            "terminal_kind_counts": terminal_counts,
            "attempts": [
                {
                    "case_id": item.case_id,
                    "attempt_index": item.attempt_index,
                    "case_attempt_index": item.case_attempt_index,
                    "terminal_kind": item.terminal_kind,
                    "response_index": item.response_index,
                    "error_code": item.error_code,
                }
                for item in self.attempts
            ],
            "cases": [item.to_dict() for item in self.cases],
            "not_finalized_case_ids": list(self.not_finalized_case_ids),
            "records": list(self.response_records()),
        }


class RuntimeCaseTelemetrySession:
    """Adapter-facing view bound to one case; the Provider never supplies case IDs."""

    __slots__ = ("_tracker", "_case_id")

    def __init__(self, tracker: RuntimeDenominatorTracker, case_id: str) -> None:
        self._tracker = tracker
        self._case_id = case_id

    @property
    def case_id(self) -> str:
        return self._case_id

    @property
    def provider_id(self) -> str:
        return self._tracker.binding_snapshot()["provider_id"]

    @property
    def api_surface(self) -> str:
        return self._tracker.binding_snapshot()["api_surface"]

    @property
    def transport_id(self) -> str:
        return self._tracker.binding_snapshot()["transport_id"]

    @property
    def adapter_version(self) -> str:
        return self._tracker.binding_snapshot()["adapter_version"]

    def binding_snapshot(self) -> dict[str, str]:
        """Return a detached exact identity for Adapter pre-network checks."""

        return self._tracker.binding_snapshot()

    def begin_attempt(self) -> RuntimeAttemptHandle:
        return self._tracker.begin_attempt(self._case_id)

    def _bound_handle(self, handle: RuntimeAttemptHandle) -> RuntimeAttemptHandle:
        if not isinstance(handle, RuntimeAttemptHandle) or handle.case_id != self._case_id:
            raise _error(
                "completion_capture_attempt_handle_invalid",
                "attempt handle belongs to a different case session",
            )
        return handle

    def finalize_response_accepted(
        self, handle: RuntimeAttemptHandle, capture: SanitizedCompletionCapture
    ) -> RuntimeAttemptTerminal:
        return self._tracker.finalize_response_accepted(
            self._bound_handle(handle), capture
        )

    def finalize_response_rejected(
        self, handle: RuntimeAttemptHandle, error_code: str
    ) -> RuntimeAttemptTerminal:
        return self._tracker.finalize_response_rejected(
            self._bound_handle(handle), error_code
        )

    def finalize_http_error(
        self, handle: RuntimeAttemptHandle, error_code: str
    ) -> RuntimeAttemptTerminal:
        return self._tracker.finalize_http_error(
            self._bound_handle(handle), error_code
        )

    def finalize_no_response(
        self, handle: RuntimeAttemptHandle, error_code: str
    ) -> RuntimeAttemptTerminal:
        return self._tracker.finalize_no_response(
            self._bound_handle(handle), error_code
        )

    def finalize_cancelled(
        self, handle: RuntimeAttemptHandle
    ) -> RuntimeAttemptTerminal:
        return self._tracker.finalize_cancelled(self._bound_handle(handle))

    def finalize_outcome_unknown(
        self, handle: RuntimeAttemptHandle, error_code: str
    ) -> RuntimeAttemptTerminal:
        return self._tracker.finalize_outcome_unknown(
            self._bound_handle(handle), error_code
        )


class RuntimeDenominatorTracker:
    """Dynamic campaign tracker whose response denominator is derived after finalization."""

    __slots__ = (
        "_plan",
        "_tracker_token",
        "_pending",
        "_terminals",
        "_case_attempt_counts",
        "_case_reconciliations",
        "_response_count",
        "_sealed",
    )

    def __init__(self, plan_binding: VerifiedRuntimeDenominatorPlanBinding) -> None:
        if type(plan_binding) is not VerifiedRuntimeDenominatorPlanBinding:
            raise _error(
                "completion_capture_plan_authority_missing",
                "runtime tracker requires a verified dynamic plan",
            )
        plan_binding.assert_plan_authority()
        self._plan = plan_binding
        self._tracker_token = object()
        self._pending: dict[int, RuntimeAttemptHandle] = {}
        self._terminals: dict[int, RuntimeAttemptTerminal] = {}
        self._case_attempt_counts = {case_id: 0 for case_id in plan_binding.case_ids}
        self._case_reconciliations: dict[str, RuntimeCaseReconciliation] = {}
        self._response_count = 0
        self._sealed = False

    def binding_snapshot(self) -> dict[str, str]:
        self._plan.assert_plan_authority()
        return self._plan.runtime_binding().runtime_snapshot()

    def runtime_binding(self) -> VerifiedRuntimeCompletionBinding:
        """Return the immutable authority already verified by this tracker."""

        self._plan.assert_plan_authority()
        return self._plan.runtime_binding()

    def plan_binding(self) -> VerifiedRuntimeDenominatorPlanBinding:
        """Return the immutable dynamic plan authority owned by this tracker."""

        self._plan.assert_plan_authority()
        return self._plan

    def plan_snapshot(self) -> dict[str, Any]:
        """Return a detached plan/binding projection for pre-network runner checks."""

        self._plan.assert_plan_authority()
        return {
            "case_ids": list(self._plan.case_ids),
            "case_ids_sha256": self._plan.case_ids_sha256,
            "max_turns_per_case": self._plan.max_turns_per_case,
            "total_model_request_cap": self._plan.total_model_request_cap,
            "preregistration_commitment": self._plan.preregistration_commitment,
            "denominator_algorithm": self._plan.denominator_algorithm,
            "exact_response_count_preregistered": (
                self._plan.exact_response_count_preregistered
            ),
            "binding": self.binding_snapshot(),
        }

    def bind_case(self, case_id: str) -> RuntimeCaseTelemetrySession:
        resolved = _safe_identifier(case_id, field_name="case_id")
        if resolved not in self._case_attempt_counts:
            raise _error("completion_capture_case_unknown", "case is not preregistered")
        return RuntimeCaseTelemetrySession(self, resolved)

    def begin_attempt(self, case_id: str) -> RuntimeAttemptHandle:
        if self._sealed:
            raise _error("completion_capture_sealed", "runtime tracker is sealed")
        resolved = _safe_identifier(case_id, field_name="case_id")
        if resolved not in self._case_attempt_counts:
            raise _error("completion_capture_case_unknown", "case is not preregistered")
        if resolved in self._case_reconciliations:
            raise _error("completion_capture_case_sealed", "case is already reconciled")
        case_attempt_index = self._case_attempt_counts[resolved]
        if case_attempt_index >= self._plan.max_turns_per_case:
            raise _error(
                "completion_capture_case_attempt_cap_exceeded",
                "case model-attempt cap is exhausted",
            )
        attempt_index = len(self._pending) + len(self._terminals)
        if attempt_index >= self._plan.total_model_request_cap:
            raise _error(
                "completion_capture_campaign_attempt_cap_exceeded",
                "campaign model-request cap is exhausted",
            )
        handle = RuntimeAttemptHandle(
            case_id=resolved,
            attempt_index=attempt_index,
            case_attempt_index=case_attempt_index,
            _tracker_token=self._tracker_token,
        )
        self._case_attempt_counts[resolved] = case_attempt_index + 1
        self._pending[attempt_index] = handle
        return handle

    def _terminal(
        self,
        handle: RuntimeAttemptHandle,
        terminal_kind: str,
        *,
        capture: SanitizedCompletionCapture | None = None,
        error_code: str | None = None,
    ) -> RuntimeAttemptTerminal:
        if self._sealed:
            raise _error("completion_capture_sealed", "runtime tracker is sealed")
        if (
            type(handle) is not RuntimeAttemptHandle
            or handle._tracker_token is not self._tracker_token
            or self._pending.get(handle.attempt_index) != handle
        ):
            if isinstance(handle, RuntimeAttemptHandle) and handle.attempt_index in self._terminals:
                raise _error(
                    "completion_capture_attempt_already_terminal",
                    "attempt already has a terminal outcome",
                )
            raise _error(
                "completion_capture_attempt_handle_invalid", "attempt handle is invalid"
            )
        observed = terminal_kind in {"response_accepted", "response_rejected"}
        if terminal_kind == "response_accepted":
            if type(capture) is not SanitizedCompletionCapture or capture.historical:
                raise _error(
                    "completion_capture_sanitized_type_required",
                    "accepted response requires a current sanitized capture",
                )
            owned_capture = capture._collector_snapshot()
            if error_code is not None:
                raise _error(
                    "completion_capture_attempt_terminal_invalid",
                    "accepted response cannot carry an error code",
                )
        else:
            owned_capture = None
            if capture is not None:
                raise _error(
                    "completion_capture_attempt_terminal_invalid",
                    "non-accepted terminal cannot retain a capture",
                )
            if terminal_kind == "cancelled":
                if error_code is not None:
                    raise _error(
                        "completion_capture_attempt_terminal_invalid",
                        "cancelled terminal has a fixed safe meaning",
                    )
            elif (
                not isinstance(error_code, str)
                or not _SAFE_REJECTION_CODE.fullmatch(error_code)
            ):
                raise _error(
                    "completion_capture_rejection_code_invalid",
                    "terminal error code must be a stable safe identifier",
                )
        response_index = self._response_count if observed else None
        if observed:
            self._response_count += 1
        terminal = RuntimeAttemptTerminal(
            case_id=handle.case_id,
            attempt_index=handle.attempt_index,
            case_attempt_index=handle.case_attempt_index,
            terminal_kind=terminal_kind,
            response_index=response_index,
            _capture=owned_capture,
            error_code=error_code,
        )
        del self._pending[handle.attempt_index]
        self._terminals[handle.attempt_index] = terminal
        return terminal

    def finalize_response_accepted(
        self, handle: RuntimeAttemptHandle, capture: SanitizedCompletionCapture
    ) -> RuntimeAttemptTerminal:
        return self._terminal(handle, "response_accepted", capture=capture)

    def finalize_response_rejected(
        self, handle: RuntimeAttemptHandle, error_code: str
    ) -> RuntimeAttemptTerminal:
        return self._terminal(handle, "response_rejected", error_code=error_code)

    def finalize_http_error(
        self, handle: RuntimeAttemptHandle, error_code: str
    ) -> RuntimeAttemptTerminal:
        return self._terminal(handle, "http_error", error_code=error_code)

    def finalize_no_response(
        self, handle: RuntimeAttemptHandle, error_code: str
    ) -> RuntimeAttemptTerminal:
        return self._terminal(handle, "no_response", error_code=error_code)

    def finalize_cancelled(
        self, handle: RuntimeAttemptHandle
    ) -> RuntimeAttemptTerminal:
        return self._terminal(handle, "cancelled")

    def finalize_outcome_unknown(
        self, handle: RuntimeAttemptHandle, error_code: str
    ) -> RuntimeAttemptTerminal:
        return self._terminal(handle, "outcome_unknown", error_code=error_code)

    def seal_case(
        self,
        case_id: str,
        *,
        sdk_raw_response_count: int | None,
        sdk_usage_request_count: int | None,
        sdk_request_usage_indices_by_response: Mapping[int, Sequence[int]] | None = None,
    ) -> RuntimeCaseReconciliation:
        if self._sealed:
            raise _error("completion_capture_sealed", "runtime tracker is sealed")
        resolved = _safe_identifier(case_id, field_name="case_id")
        if resolved not in self._case_attempt_counts:
            raise _error("completion_capture_case_unknown", "case is not preregistered")
        if resolved in self._case_reconciliations:
            raise _error("completion_capture_case_sealed", "case is already reconciled")
        if any(handle.case_id == resolved for handle in self._pending.values()):
            raise _error(
                "completion_capture_attempt_pending",
                "case cannot be sealed with an unfinished attempt",
            )
        case_terminals = tuple(
            item for item in self._terminals.values() if item.case_id == resolved
        )
        observed = tuple(item for item in case_terminals if item.provider_response_observed)
        accepted = tuple(
            item for item in observed if item.terminal_kind == "response_accepted"
        )
        rejected = tuple(
            item for item in observed if item.terminal_kind == "response_rejected"
        )
        for value, code in (
            (sdk_raw_response_count, "completion_capture_sdk_response_count_invalid"),
            (sdk_usage_request_count, "completion_capture_sdk_usage_count_invalid"),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise _error(code, "SDK count must be a non-negative integer or null")
        raw_reconciliation = (
            "unavailable"
            if sdk_raw_response_count is None
            else "matched"
            if sdk_raw_response_count == len(observed)
            else "mismatched"
        )
        usage_reconciliation = (
            "unavailable"
            if sdk_usage_request_count is None
            else "matched"
            if sdk_usage_request_count == len(case_terminals)
            else "mismatched"
        )
        nested: list[tuple[int, int, tuple[int, ...]]] = []
        if sdk_request_usage_indices_by_response is not None:
            if not isinstance(sdk_request_usage_indices_by_response, Mapping):
                raise _error(
                    "completion_capture_sdk_usage_indices_invalid",
                    "SDK usage indices must be a response-index mapping",
                )
            for sdk_response_index, raw_indices in sorted(
                sdk_request_usage_indices_by_response.items()
            ):
                if (
                    type(sdk_response_index) is not int
                    or not 0 <= sdk_response_index < len(observed)
                    or not isinstance(raw_indices, Sequence)
                    or isinstance(raw_indices, (str, bytes, bytearray))
                    or any(type(item) is not int or item < 0 for item in raw_indices)
                    or list(raw_indices) != list(range(len(raw_indices)))
                ):
                    raise _error(
                        "completion_capture_sdk_usage_indices_invalid",
                        "SDK request-usage indices are not nested zero-based indices",
                    )
                response_index = observed[sdk_response_index].response_index
                if response_index is None:  # pragma: no cover - observed invariant
                    raise AssertionError("observed response lost response index")
                nested.append(
                    (response_index, sdk_response_index, tuple(raw_indices))
                )
        terminal_kinds = {item.terminal_kind for item in case_terminals}
        closure_eligible = (
            bool(observed)
            and raw_reconciliation == "matched"
            and usage_reconciliation == "matched"
            and not rejected
            and terminal_kinds <= {"response_accepted"}
        )
        result = RuntimeCaseReconciliation(
            case_id=resolved,
            attempts_started=self._case_attempt_counts[resolved],
            attempts_terminal=len(case_terminals),
            observed_response_count=len(observed),
            accepted_response_count=len(accepted),
            rejected_response_count=len(rejected),
            sdk_raw_response_count=sdk_raw_response_count,
            sdk_raw_response_reconciliation=raw_reconciliation,
            sdk_usage_request_count=sdk_usage_request_count,
            sdk_usage_request_reconciliation=usage_reconciliation,
            sdk_request_usage_indices_by_response=tuple(nested),
            closure_eligible=closure_eligible,
        )
        self._case_reconciliations[resolved] = result
        return result

    def seal_runtime(self) -> RuntimeDenominatorArtifact:
        if self._sealed:
            raise _error("completion_capture_sealed", "runtime tracker is sealed")
        if self._pending:
            raise _error(
                "completion_capture_attempt_pending",
                "runtime tracker cannot seal with unfinished attempts",
            )
        self._sealed = True
        attempts = tuple(self._terminals[index] for index in sorted(self._terminals))
        cases = tuple(
            self._case_reconciliations[case_id]
            for case_id in self._plan.case_ids
            if case_id in self._case_reconciliations
        )
        observed = tuple(item for item in attempts if item.provider_response_observed)
        accepted = tuple(
            item for item in observed if item.terminal_kind == "response_accepted"
        )
        rejected = tuple(
            item for item in observed if item.terminal_kind == "response_rejected"
        )
        return RuntimeDenominatorArtifact(
            plan_binding=self._plan,
            attempts=attempts,
            cases=cases,
            planned_case_ids=self._plan.case_ids,
            not_finalized_case_ids=tuple(
                case_id
                for case_id in self._plan.case_ids
                if case_id not in self._case_reconciliations
            ),
            observed_response_count=len(observed),
            accepted_response_count=len(accepted),
            rejected_response_count=len(rejected),
        )


def evaluate_runtime_denominator_closure(
    artifact: RuntimeDenominatorArtifact,
    states_and_sources: Sequence[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Evaluate the denominator/reconciliation half of the future closing gate."""

    if not isinstance(artifact, RuntimeDenominatorArtifact):
        raise _error(
            "completion_capture_artifact_invalid", "runtime artifact is invalid"
        )
    artifact._validate_structure()
    reasons: list[str] = []

    def add_reason(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if artifact.not_finalized_case_ids:
        add_reason("planned_cases_not_finalized")
    if not artifact.attempts:
        add_reason("no_model_attempts_observed")
    if artifact.observed_response_count < 1:
        add_reason("no_provider_responses_observed")
    accepted_case_ids = {
        attempt.case_id
        for attempt in artifact.attempts
        if attempt.terminal_kind == "response_accepted"
    }
    if any(
        case_id not in accepted_case_ids for case_id in artifact.planned_case_ids
    ):
        add_reason("planned_case_without_accepted_response")
    terminal_reasons = {
        "response_rejected": "response_telemetry_rejected",
        "http_error": "http_error_response_observed",
        "no_response": "request_failed_without_response",
        "cancelled": "model_request_cancelled",
        "outcome_unknown": "model_request_outcome_unknown",
    }
    for attempt in artifact.attempts:
        reason = terminal_reasons.get(attempt.terminal_kind)
        if reason is not None:
            add_reason(reason)
    for case in artifact.cases:
        if case.sdk_raw_response_reconciliation == "mismatched":
            add_reason("sdk_raw_response_count_mismatched")
        elif case.sdk_raw_response_reconciliation == "unavailable":
            add_reason("sdk_raw_response_count_unavailable")
        if case.sdk_usage_request_reconciliation == "mismatched":
            add_reason("sdk_usage_request_count_mismatched")
        elif case.sdk_usage_request_reconciliation == "unavailable":
            add_reason("sdk_usage_request_count_unavailable")
    records = artifact.response_records()
    derived_states_and_sources = tuple(
        (
            str(record["normalized_completion_state"]),
            str(record["truncation_signal_source"]),
        )
        for record in records
    )
    if states_and_sources is not None and tuple(states_and_sources) != derived_states_and_sources:
        add_reason("completion_state_projection_mismatch")
    if len(records) != artifact.observed_response_count:
        add_reason("completion_record_denominator_mismatch")
    for state, source in derived_states_and_sources:
        if state == "unmapped":
            add_reason("completion_state_unmapped")
        elif state == "not_provided":
            add_reason("completion_state_not_provided")
        elif state == "not_persisted":
            add_reason("completion_state_not_persisted")
        elif state not in RUNTIME_CLOSURE_RECOGNIZED_STATES:
            add_reason("completion_state_unrecognized")
        if source == "token_cap_fallback":
            add_reason("truncation_signal_token_cap_fallback")
        elif source == "none":
            add_reason("truncation_signal_none")
        elif source != RUNTIME_CLOSURE_REQUIRED_SIGNAL_SOURCE:
            add_reason("truncation_signal_unrecognized")
    return {
        "claim_allowed": not reasons,
        "claim_scope": "derived_observed_provider_responses_in_this_run_only",
        "denominator_algorithm": _DENOMINATOR_ALGORITHM,
        "exact_response_count_preregistered": False,
        "derived_after_run": True,
        "observed_response_count": artifact.observed_response_count,
        "reasons": reasons,
    }


# Compatibility names now resolve to the dynamic runtime plan. The old fixed-N
# implementation above remains reachable only by already-instantiated objects;
# no public factory can create it for runtime use.
VerifiedCapturePlanBinding = VerifiedRuntimeDenominatorPlanBinding
verify_runtime_capture_plan = verify_runtime_denominator_plan


__all__ = [
    "CompletionCaptureError",
    "CompletionCaptureArtifact",
    "CompletionCaptureSlot",
    "CompletionTelemetryCollector",
    "RuntimeAttemptHandle",
    "RuntimeAttemptTerminal",
    "RuntimeCaseReconciliation",
    "RuntimeCaseTelemetrySession",
    "RuntimeDenominatorArtifact",
    "RuntimeDenominatorTracker",
    "RUNTIME_CLOSURE_RECOGNIZED_STATES",
    "RUNTIME_CLOSURE_REQUIRED_SIGNAL_SOURCE",
    "VerifiedCapturePlanBinding",
    "VerifiedRuntimeDenominatorPlanBinding",
    "evaluate_runtime_denominator_closure",
    "verify_runtime_capture_plan",
    "verify_runtime_denominator_plan",
]
