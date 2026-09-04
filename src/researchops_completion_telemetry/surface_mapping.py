from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from .mapping import CompletionMappingResult, map_completion


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REGISTRY_SCHEMA_VERSION = "provider-completion-surface-registry/2.0"
_SELECTION_SCHEMA_VERSION = "provider-completion-mapping/2.0"
_MANIFEST_RELATIVE_PATH = Path(
    "evals/provider_completion_telemetry_v2/fixture_manifest_v2.json"
)
_V1_MANIFEST_RELATIVE_PATH = Path(
    "evals/provider_completion_telemetry_v1/fixture_manifest.json"
)
_EXPECTED_PROBE_RECEIPT_BYTES = 9215
_EXPECTED_PROBE_RECEIPT_SHA256 = (
    "cb3417b0f3c56eca7fd6d05dda68c4717315004ba0d6e5d6da408d52d990131d"
)
_MAX_SAFE_TREE_DEPTH = 16
_MAX_USAGE_BYTES = 4096
_MAX_USAGE_DEPTH = 4
_MAX_USAGE_LEAVES = 64
_MAX_USAGE_PATH_BYTES = 255
_SAFE_COUNTER_SEGMENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_SAFE_SCHEMA_VALUE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
_SECRET_TEXT = re.compile(
    r"(?:\bsk[-_][A-Za-z0-9_-]{8,}\b|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token)\s*[:=]\s*\S+|"
    r"\b(?:proxy-)?authorization\s*:\s*(?:bearer|basic)\s+\S+|"
    r"\b(?:set-)?cookie\s*:\s*\S+)",
    re.IGNORECASE,
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:[A-Z]:[\\/]|\\\\)[^\r\n\t\"']+")
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![\w/])/(?:[A-Za-z0-9._~-]+/)*[A-Za-z0-9._~-]+"
)
_TRACEBACK = re.compile(
    r"Traceback\s*\(most recent call last\)|"
    r"\bFile\s+[\"'][^\"']+[\"']\s*,\s*line\s+\d+|"
    r"\b__traceback__\b",
    re.IGNORECASE,
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "documented_on",
        "adapter_changes_included",
        "online_calls_performed",
        "registry_key_fields",
        "surface_mismatch_behavior",
        "nearest_surface_fallback_allowed",
        "predecessor_mapping",
        "entries",
    }
)
_PREDECESSOR_FIELDS = frozenset(
    {
        "relative_path",
        "bytes",
        "sha256",
        "canonical_json_sha256",
        "schema_version",
        "immutable",
    }
)
_ENTRY_FIELDS = frozenset(
    {
        "provider_id",
        "api_surface",
        "transport_id",
        "adapter_version",
        "output_counter_comparability",
        "output_counter_path",
        "mapping_source",
        "mapping_version",
        "provenance_tier",
        "unverified_shape",
        "first_live_validation_required",
        "provenance_promotion_allowed",
        "offline_selection_allowed",
        "runtime_binding_allowed",
        "runtime_binding_blocker",
        "source",
    }
)
_MANIFEST_FIELDS = frozenset(
    {"schema_version", "status", "registry", "fixtures", "summary"}
)
_FILE_COMMITMENT_FIELDS = frozenset({"file", "bytes", "sha256"})
_FIXTURE_COMMITMENT_FIELDS = frozenset(
    {"fixture_id", "file", "bytes", "sha256", "fixture_kind"}
)
_SUMMARY_FIELDS = frozenset(
    {
        "fixture_count",
        "live_capture_fixture_count",
        "synthetic_mutation_fixture_count",
        "online_calls_performed_for_this_successor",
        "adapter_changes_included",
    }
)
_ALLOWED_CONDITION_PATHS = {
    "responses": frozenset(
        {"status", "incomplete_details", "incomplete_details.reason"}
    ),
    "messages": frozenset({"stop_reason", "stop_sequence"}),
    "openai_compatible_chat_completions": frozenset({"finish_reason"}),
}
_EXPECTED_TRIPLES = frozenset(
    {
        ("deepseek", "responses", "openai_compatible_responses"),
        ("openai", "responses", "openai_responses"),
        ("anthropic", "messages", "litellm_anthropic_chat_completions"),
        (
            "moonshot_kimi",
            "openai_compatible_chat_completions",
            "moonshot_direct_chat_completions_sse_v3",
        ),
    }
)
_EXPECTED_RUNTIME_BINDING_METADATA = {
    ("deepseek", "responses", "openai_compatible_responses"): (
        "deepseek-responses-adapter/1.0",
        "output_tokens",
    ),
    ("openai", "responses", "openai_responses"): (
        "openai-responses-adapter/1.0",
        "output_tokens",
    ),
    ("anthropic", "messages", "litellm_anthropic_chat_completions"): (
        "anthropic-litellm-adapter/1.0",
        "output_tokens",
    ),
    (
        "moonshot_kimi",
        "openai_compatible_chat_completions",
        "moonshot_direct_chat_completions_sse_v3",
    ): ("moonshot-kimi-sse-v3-adapter/1.0", "completion_tokens"),
}
_INLINE_SOURCE_FIELDS = frozenset(
    {
        "kind",
        "relative_path",
        "bytes",
        "sha256",
        "probe_id",
        "api_origin",
        "path",
        "model",
        "adapter_kwargs_equivalence_claimed",
        "observed_distinct_shape_count",
        "observed_native_values",
        "known_but_unobserved_values_remain_unmapped",
        "provenance_limitations",
    }
)
_ALIAS_SOURCE_FIELDS = frozenset({"kind", "attribution_scope"})
_PROVENANCE_LIMITATION_FIELDS = frozenset(
    {
        "capture_time",
        "authorization_linkage",
        "probe_script_linkage",
        "predecessor_receipt_bytes",
        "message_output_item_observed_scope",
        "normal_probe_message_completed_observed",
    }
)
_EXPECTED_V2_FIXTURES = {
    "deepseek_responses_completed_20260903": {
        "scenario": "completed",
        "fixture_kind": "unmodified_live_projection",
        "source_probe_label": "responses_normal_completion",
    },
    "deepseek_responses_length_capped_20260903": {
        "scenario": "length_capped",
        "fixture_kind": "unmodified_live_projection",
        "source_probe_label": "responses_output_cap_attempt",
    },
    "deepseek_responses_missing_fields_20260903": {
        "scenario": "missing_fields",
        "fixture_kind": "synthetic_mutation",
        "base_fixture_id": "deepseek_responses_completed_20260903",
    },
    "deepseek_responses_unknown_value_20260903": {
        "scenario": "unknown_value",
        "fixture_kind": "synthetic_mutation",
        "base_fixture_id": "deepseek_responses_completed_20260903",
    },
}
_V1_MANIFEST_FIELDS = frozenset(
    {
        "claim_boundary",
        "fixtures",
        "generated_on",
        "mapping",
        "rule_coverage",
        "privacy_review",
        "schema_version",
        "status",
        "summary",
    }
)
_V1_FIXTURE_COMMITMENT_FIELDS = frozenset(
    {
        "bytes",
        "derivation_kind",
        "file",
        "fixture_id",
        "provenance_tier",
        "provider_id",
        "scenario",
        "sha256",
        "unverified_shape",
    }
)
_V1_FIXTURE_FIELDS = frozenset(
    {
        "fixture_schema_version",
        "fixture_id",
        "provider_id",
        "api_surface",
        "scenario",
        "provenance",
        "derivation",
        "response_projection",
        "capture_boundary",
    }
)
_V1_PROVENANCE_FIELDS = frozenset(
    {"tier", "as_of_date", "unverified_shape", "live_validation", "source"}
)
_V1_CAPTURE_BOUNDARY = {
    "telemetry_contract_development_only": True,
    "evaluation_task": False,
    "run_ledger_written": False,
    "task_pass_computed": False,
    "model_quality_claim_allowed": False,
    "content_omitted": True,
    "reasoning_content_omitted": True,
    "tool_call_arguments_omitted": True,
    "system_prompt_omitted": True,
    "raw_response_persisted": False,
    "api_key_persisted": False,
    "private_or_non_synthetic_evaluation_data": False,
}
_V1_SOURCE_FIELDS = {
    "live_response": frozenset(
        {
            "kind",
            "capture_profile",
            "model",
            "origin",
            "stream",
            "thinking",
            "prompt",
            "network_attempts",
            "client_retries",
            "max_tokens",
            "captured_at_utc",
        }
    ),
    "sdk_type_definition": frozenset(
        {"kind", "artifact", "version", "symbols", "files", "official_reference"}
    ),
    "sdk_type_definition_generated_from_openapi": frozenset(
        {"kind", "artifact", "commit", "symbols", "files", "official_reference"}
    ),
    "doc_prose": frozenset(
        {
            "kind",
            "official_reference",
            "model",
            "documented_finish_reasons",
            "documented_usage_fields",
            "successful_handshake_observed",
            "existing_status",
            "openai_style_compatibility_complete",
        }
    ),
}
_PROBE_RECEIPT_FIELDS = frozenset(
    {
        "probe_id",
        "status",
        "boundary",
        "transport",
        "limitations",
        "probes",
        "post_request_cleanup_succeeded",
    }
)
_PROBE_RECORD_FIELDS = frozenset(
    {
        "probe_label",
        "requested_max_output_tokens",
        "http_status",
        "provider_request_id_sha256",
        "provider_request_id_header_present",
        "provider_request_id_hash_withheld",
        "top_level_keys_observed",
        "top_level_presence",
        "shape_only_top_level_observations",
        "incomplete_details_reason_presence",
        "incomplete_details_keys_observed",
        "usage_keys_observed",
        "response_projection",
        "output_container",
        "output_item_shapes",
        "raw_response_cleanup_state",
    }
)
_PROBE_BOUNDARY = {
    "is_evaluation_task": False,
    "enters_run_ledger": False,
    "produces_task_pass": False,
    "authorizes_prompt_or_scorer_change": False,
    "supports_model_quality_claim": False,
    "authorizes_provider_registration": False,
    "response_body_persisted": False,
    "raw_nested_objects_persisted": False,
    "input_content_persisted": False,
    "provider_side_retention_unverified": True,
    "diagnostic_key_names_and_item_shapes_persisted": True,
    "shape_only_top_level_values_persisted": False,
    "shape_only_top_level_presence_type_and_count_persisted": True,
    "raw_response_cleanup_state_persisted_when_record_retained": True,
}
_PROBE_TRANSPORT = {
    "base_url": "https://api.deepseek.com",
    "path": "/responses",
    "model": "deepseek-v4-flash",
    "openai_sdk_version": "3.1.0",
    "stream": False,
    "max_retries": 0,
    "concurrency": 1,
    "resume": False,
    "fallback": False,
    "network_attempts_max": 3,
    "model_requests_max": 3,
    "request_timeout_seconds": 120.0,
    "request_phase_total_timeout_seconds": 300.0,
    "request_phase_total_timeout_preempts_per_request_timeouts": True,
    "cleanup_timeout_seconds_per_resource": 5.0,
    "maximum_raw_response_cleanup_resources": 3,
    "raw_response_cleanup_included_in_request_phase_total_timeout": True,
    "maximum_post_request_cleanup_resources": 2,
    "request_phase_plus_post_request_cleanup_timeout_upper_bound_seconds": 310.0,
    "setup_time_bounded": False,
    "whole_process_wall_timeout_claimed": False,
    "trust_environment": False,
    "follow_redirects": False,
    "kwargs_source": "minimal_direct_probe_not_sdk_built",
    "requested_max_output_tokens_sum_max": 368,
}
_PROBE_LIMITATION_FIELDS = frozenset(
    {
        "forced_cap_minimum_output_tokens",
        "forced_cap_attempt_completed",
        "forced_cap_attempt_retained_status",
        "forced_cap_not_observed_interpretation_scope",
        "forced_cap_not_observed_interpretation",
        "adapter_kwargs_equivalence_claimed",
        "sdk_built_kwargs_offline_diff_performed",
        "known_but_unobserved_values_remain_unmapped",
        "supersedes_probe_receipt_sha256",
        "superseded_field",
        "superseded_field_reason",
        "message_stage_probe_max_output_tokens",
        "message_stage_probe_cap_is_fixed_non_adaptive",
        "message_stage_entry_guaranteed",
        "message_stage_requested_word_repetitions",
        "message_stage_final_planned_attempt",
        "no_further_message_stage_probe_if_target_not_observed",
        "input_token_hard_cap_claimed",
        "message_stage_cap_not_observed_interpretation_scope",
        "message_stage_cap_not_observed_interpretation",
        "observed_response_count",
        "observed_response_count_definition",
        "distinct_top_level_shape_count",
        "distinct_top_level_shape_eligible_response_count",
        "distinct_top_level_shape_unavailable_response_count",
        "distinct_shape_definition",
        "message_stage_cap_attempt_completed",
        "message_output_item_observed",
        "message_output_item_with_incomplete_status_observed",
        "top_level_incomplete_max_output_tokens_observed",
        "message_stage_cap_target_observed",
    }
)
_PROBE_TOP_LEVEL_KEYS = [
    "background",
    "completed_at",
    "content_filters",
    "created_at",
    "error",
    "frequency_penalty",
    "id",
    "incomplete_details",
    "instructions",
    "max_output_tokens",
    "max_tool_calls",
    "metadata",
    "model",
    "moderation",
    "object",
    "output",
    "parallel_tool_calls",
    "presence_penalty",
    "previous_response_id",
    "prompt_cache_key",
    "prompt_cache_retention",
    "reasoning",
    "safety_identifier",
    "service_tier",
    "status",
    "store",
    "temperature",
    "text",
    "tool_choice",
    "tools",
    "top_logprobs",
    "top_p",
    "truncation",
    "usage",
    "user",
]
_PROBE_USAGE_KEYS = [
    "input_tokens",
    "input_tokens_details",
    "output_tokens",
    "output_tokens_details",
    "total_tokens",
]
_PROBE_SHAPE_ONLY_OBSERVATIONS = {
    "content_filters": {
        "presence": "present_null",
        "json_type": "null",
        "direct_child_count": None,
    },
    "error": {
        "presence": "present_null",
        "json_type": "null",
        "direct_child_count": None,
    },
    "truncation": {
        "presence": "present_non_null",
        "json_type": "string",
        "direct_child_count": None,
    },
}
_PROBE_EXPECTED_COMPLETION_SHAPES = {
    "responses_normal_completion": {
        "status": "completed",
        "incomplete_details": None,
        "presence": {
            "status": "present_non_null",
            "incomplete_details": "present_null",
            "stop_sequence": "missing",
        },
        "reason_presence": "parent_null",
        "detail_keys": None,
        "output_shapes": [
            {
                "index": 0,
                "type": "reasoning",
                "status_key_present": True,
                "status": "completed",
            },
            {
                "index": 1,
                "type": "message",
                "status_key_present": True,
                "status": "completed",
            },
        ],
    },
    "responses_output_cap_attempt": {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "presence": {
            "status": "present_non_null",
            "incomplete_details": "present_non_null",
            "stop_sequence": "missing",
        },
        "reason_presence": "reason_present_non_null",
        "detail_keys": ["reason"],
        "output_shapes": [
            {
                "index": 0,
                "type": "reasoning",
                "status_key_present": True,
                "status": "incomplete",
            }
        ],
    },
    "responses_message_stage_cap_attempt": {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "presence": {
            "status": "present_non_null",
            "incomplete_details": "present_non_null",
            "stop_sequence": "missing",
        },
        "reason_presence": "reason_present_non_null",
        "detail_keys": ["reason"],
        "output_shapes": [
            {
                "index": 0,
                "type": "reasoning",
                "status_key_present": True,
                "status": "incomplete",
            }
        ],
    },
}
_VERIFIED_TOKEN = object()
_SELECTION_TOKEN = object()
_RUNTIME_BINDING_TOKEN = object()


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(child) for key, child in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(child) for child in value)
    return value


def _thaw_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_thaw_json(child) for child in value]
    return value


class VerifiedRuntimeCompletionBinding:
    """Opaque live-write authority derived only from a runtime-enabled selection."""

    __slots__ = (
        "_telemetry_schema_sha256",
        "_adapter_version",
        "_mapping_schema_version",
        "_mapping_version",
        "_mapping_sha256",
        "_provider_id",
        "_api_surface",
        "_transport_id",
        "_output_counter_comparability",
        "_output_counter_path",
        "_authority_scope",
        "_mapping",
        "_authority_token",
        "_locked",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError(
            "VerifiedRuntimeCompletionBinding requires runtime authority"
        )

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("VerifiedRuntimeCompletionBinding is immutable")
        object.__setattr__(self, name, value)

    @classmethod
    def _create(
        cls,
        token: object,
        selection: VerifiedSurfaceSelection,
    ) -> VerifiedRuntimeCompletionBinding:
        if token is not _RUNTIME_BINDING_TOKEN:
            raise TypeError("invalid runtime binding construction token")
        if (
            type(selection) is not VerifiedSurfaceSelection
            or getattr(selection, "_authority_token", None) is not _SELECTION_TOKEN
            or selection.purpose not in {"runtime_binding", "first_live_validation"}
            or selection.runtime_binding_allowed is not True
        ):
            raise _error(
                "surface_runtime_authority_missing",
                "offline selection cannot authorize a live completion record",
            )
        instance = object.__new__(cls)
        for name in (
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
        ):
            object.__setattr__(instance, f"_{name}", getattr(selection, name))
        object.__setattr__(instance, "_mapping", selection._mapping)
        object.__setattr__(
            instance,
            "_authority_scope",
            (
                "campaign_runtime"
                if selection.purpose == "runtime_binding"
                else "first_live_validation"
            ),
        )
        object.__setattr__(instance, "_authority_token", _RUNTIME_BINDING_TOKEN)
        object.__setattr__(instance, "_locked", True)
        return instance

    @property
    def telemetry_schema_sha256(self) -> str:
        return self._telemetry_schema_sha256

    @property
    def adapter_version(self) -> str:
        return self._adapter_version

    @property
    def mapping_schema_version(self) -> str:
        return self._mapping_schema_version

    @property
    def mapping_version(self) -> str:
        return self._mapping_version

    @property
    def mapping_sha256(self) -> str:
        return self._mapping_sha256

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
    def output_counter_comparability(self) -> str:
        return self._output_counter_comparability

    @property
    def output_counter_path(self) -> str:
        return self._output_counter_path

    @property
    def authority_scope(self) -> str:
        return self._authority_scope

    def resolve_mapping(self, projection: Mapping[str, Any]) -> CompletionMappingResult:
        self.assert_runtime_authority()
        return map_completion(
            projection,
            self._provider_id,
            _thaw_json(self._mapping),
        )

    def assert_runtime_authority(self, *, expected_scope: str | None = None) -> None:
        if (
            type(self) is not VerifiedRuntimeCompletionBinding
            or getattr(self, "_authority_token", None) is not _RUNTIME_BINDING_TOKEN
            or getattr(self, "_authority_scope", None)
            not in {"campaign_runtime", "first_live_validation"}
            or (
                expected_scope is not None
                and self._authority_scope != expected_scope
            )
        ):
            raise _error(
                "surface_runtime_authority_missing",
                "runtime completion binding authority is invalid",
            )

    def runtime_snapshot(self) -> dict[str, str]:
        self.assert_runtime_authority()
        return {
            "telemetry_schema_sha256": self._telemetry_schema_sha256,
            "adapter_version": self._adapter_version,
            "mapping_schema_version": self._mapping_schema_version,
            "mapping_version": self._mapping_version,
            "mapping_sha256": self._mapping_sha256,
            "provider_id": self._provider_id,
            "api_surface": self._api_surface,
            "transport_id": self._transport_id,
            "output_counter_comparability": self._output_counter_comparability,
            "output_counter_path": self._output_counter_path,
        }


class VerifiedSurfaceSelection:
    """Immutable offline/runtime mapping selection with content-derived hashes."""

    __slots__ = (
        "_purpose",
        "_telemetry_schema_sha256",
        "_adapter_version",
        "_mapping_schema_version",
        "_mapping_version",
        "_mapping_sha256",
        "_provider_id",
        "_api_surface",
        "_transport_id",
        "_output_counter_comparability",
        "_output_counter_path",
        "_runtime_binding_allowed",
        "_mapping",
        "_authority_token",
        "_locked",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("VerifiedSurfaceSelection requires verified artifacts")

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("VerifiedSurfaceSelection is immutable")
        object.__setattr__(self, name, value)

    @classmethod
    def _create(
        cls,
        token: object,
        *,
        purpose: str,
        telemetry_schema_sha256: str,
        mapping: Mapping[str, Any],
        entry: Mapping[str, Any],
    ) -> VerifiedSurfaceSelection:
        if token is not _SELECTION_TOKEN:
            raise TypeError("invalid selection construction token")
        surface = _object(mapping.get("surface_selection"), label="surface selection")
        mapping_bytes = json.dumps(
            mapping,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        instance = object.__new__(cls)
        values = {
            "purpose": purpose,
            "telemetry_schema_sha256": telemetry_schema_sha256,
            "adapter_version": entry["adapter_version"],
            "mapping_schema_version": mapping["schema_version"],
            "mapping_version": entry["mapping_version"],
            "mapping_sha256": hashlib.sha256(mapping_bytes).hexdigest(),
            "provider_id": surface["provider_id"],
            "api_surface": surface["api_surface"],
            "transport_id": surface["transport_id"],
            "output_counter_comparability": entry[
                "output_counter_comparability"
            ],
            "output_counter_path": entry["output_counter_path"],
            "runtime_binding_allowed": entry["runtime_binding_allowed"],
        }
        for name, value in values.items():
            object.__setattr__(instance, f"_{name}", value)
        object.__setattr__(instance, "_mapping", _freeze_json(mapping))
        object.__setattr__(instance, "_authority_token", _SELECTION_TOKEN)
        object.__setattr__(instance, "_locked", True)
        return instance

    @property
    def purpose(self) -> str:
        return self._purpose

    @property
    def telemetry_schema_sha256(self) -> str:
        return self._telemetry_schema_sha256

    @property
    def adapter_version(self) -> str:
        return self._adapter_version

    @property
    def mapping_schema_version(self) -> str:
        return self._mapping_schema_version

    @property
    def mapping_version(self) -> str:
        return self._mapping_version

    @property
    def mapping_sha256(self) -> str:
        return self._mapping_sha256

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
    def output_counter_comparability(self) -> str:
        return self._output_counter_comparability

    @property
    def output_counter_path(self) -> str:
        return self._output_counter_path

    @property
    def runtime_binding_allowed(self) -> bool:
        return self._runtime_binding_allowed

    def mapping_snapshot(self) -> dict[str, Any]:
        if getattr(self, "_authority_token", None) is not _SELECTION_TOKEN:
            raise _error(
                "surface_selection_not_verified", "selection authority is invalid"
            )
        return _thaw_json(self._mapping)

    def resolve_mapping(self, projection: Mapping[str, Any]) -> CompletionMappingResult:
        return map_completion(projection, self._provider_id, self.mapping_snapshot())

    def create_runtime_binding(self) -> VerifiedRuntimeCompletionBinding:
        return VerifiedRuntimeCompletionBinding._create(
            _RUNTIME_BINDING_TOKEN, self
        )


class SurfaceMappingError(ValueError):
    """Stable fail-closed error for artifact loading and surface selection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


class VerifiedSurfaceRegistry:
    """Opaque result of full byte, path, schema, and cross-file verification."""

    __slots__ = (
        "_repository_root",
        "_registry",
        "_predecessor",
        "_fixtures",
        "_entries",
        "_provider_mappings",
        "_telemetry_schema_sha256",
        "_authority_token",
        "_locked",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("VerifiedSurfaceRegistry must be created by its strict loader")

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("VerifiedSurfaceRegistry is immutable")
        object.__setattr__(self, name, value)

    @classmethod
    def _create(
        cls,
        token: object,
        *,
        repository_root: Path,
        registry: Mapping[str, Any],
        predecessor: Mapping[str, Any],
        fixtures: Mapping[str, Mapping[str, Any]],
        entries: Mapping[tuple[str, str, str], Mapping[str, Any]],
        provider_mappings: Mapping[tuple[str, str, str], Mapping[str, Any]],
        telemetry_schema_sha256: str,
    ) -> VerifiedSurfaceRegistry:
        if token is not _VERIFIED_TOKEN:
            raise TypeError("invalid VerifiedSurfaceRegistry construction token")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_repository_root", repository_root)
        object.__setattr__(instance, "_registry", _freeze_json(registry))
        object.__setattr__(instance, "_predecessor", _freeze_json(predecessor))
        object.__setattr__(
            instance,
            "_fixtures",
            MappingProxyType(
                {key: _freeze_json(value) for key, value in fixtures.items()}
            ),
        )
        object.__setattr__(
            instance,
            "_entries",
            MappingProxyType(
                {key: _freeze_json(value) for key, value in entries.items()}
            ),
        )
        object.__setattr__(
            instance,
            "_provider_mappings",
            MappingProxyType(
                {
                    key: _freeze_json(value)
                    for key, value in provider_mappings.items()
                }
            ),
        )
        object.__setattr__(
            instance, "_telemetry_schema_sha256", telemetry_schema_sha256
        )
        object.__setattr__(instance, "_authority_token", _VERIFIED_TOKEN)
        object.__setattr__(instance, "_locked", True)
        return instance

    @property
    def repository_root(self) -> Path:
        return self._repository_root

    def fixture_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._fixtures))

    def fixture_snapshot(self, fixture_id: str) -> dict[str, Any]:
        identifier = _identifier(fixture_id, label="fixture_id")
        fixture = self._fixtures.get(identifier)
        if fixture is None:
            raise _error("surface_fixture_unknown", "fixture is not verified")
        return _thaw_json(fixture)

    def registry_snapshot(self) -> dict[str, Any]:
        return _thaw_json(self._registry)


def _error(code: str, message: str) -> SurfaceMappingError:
    return SurfaceMappingError(code, message)


def _object(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error("surface_registry_invalid", f"{label} must be an object")
    return value


def _array(value: object, *, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _error("surface_registry_invalid", f"{label} must be an array")
    return value


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise _error("surface_registry_invalid", f"{label} is not a safe identifier")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise _error("surface_artifact_json_duplicate_key", "JSON key is duplicated")
        output[key] = value
    return output


def _reject_nonfinite(value: str) -> object:
    del value
    raise _error(
        "surface_artifact_json_nonfinite", "JSON contains a non-finite number"
    )


def _decode_json(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except SurfaceMappingError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _error(
            "surface_artifact_json_invalid", f"{label} is invalid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise _error(
            "surface_artifact_json_invalid", f"{label} must contain an object"
        )
    return value


def _scan_sensitive_tree(value: object, *, label: str, depth: int = 0) -> None:
    """Reject sensitive strings recursively; exact object schemas are checked separately."""

    if depth > _MAX_SAFE_TREE_DEPTH:
        raise _error("surface_artifact_privacy_invalid", f"{label} is too deeply nested")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        public_url = value.startswith(("https://", "http://"))
        public_path = value == "/responses" or value.startswith(
            "/response_projection/"
        )
        if (
            _SECRET_TEXT.search(value)
            or _EMAIL.search(value)
            or (not public_url and _WINDOWS_ABSOLUTE_PATH.search(value))
            or (
                not public_url
                and not public_path
                and _POSIX_ABSOLUTE_PATH.search(value)
            )
            or _TRACEBACK.search(value)
        ):
            raise _error(
                "surface_artifact_sensitive_value",
                f"{label} contains a forbidden sensitive value",
            )
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise _error(
                    "surface_artifact_privacy_invalid",
                    f"{label} contains a non-string key",
                )
            _scan_sensitive_tree(child, label=label, depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _scan_sensitive_tree(child, label=label, depth=depth + 1)
        return
    raise _error(
        "surface_artifact_privacy_invalid",
        f"{label} contains a non-JSON value",
    )


def _validate_usage_tree(value: object, *, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise _error("surface_fixture_usage_invalid", f"{label} must be an object or null")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _error("surface_fixture_usage_invalid", f"{label} is not canonical JSON") from exc
    if len(encoded) > _MAX_USAGE_BYTES:
        raise _error("surface_fixture_usage_invalid", f"{label} exceeds its byte limit")

    leaves = 0

    def visit(node: Mapping[str, Any], path: tuple[str, ...]) -> None:
        nonlocal leaves
        for key, child in node.items():
            if not isinstance(key, str) or not _SAFE_COUNTER_SEGMENT.fullmatch(key):
                raise _error(
                    "surface_fixture_usage_invalid",
                    f"{label} contains an unsafe counter key",
                )
            child_path = (*path, key)
            if len(".".join(child_path).encode("utf-8")) > _MAX_USAGE_PATH_BYTES:
                raise _error(
                    "surface_fixture_usage_invalid",
                    f"{label} contains an overlong counter path",
                )
            if isinstance(child, Mapping):
                if len(child_path) >= _MAX_USAGE_DEPTH:
                    raise _error(
                        "surface_fixture_usage_invalid",
                        f"{label} exceeds its depth limit",
                    )
                visit(child, child_path)
            elif child is None or (
                isinstance(child, int) and not isinstance(child, bool) and child >= 0
            ):
                leaves += 1
                if leaves > _MAX_USAGE_LEAVES:
                    raise _error(
                        "surface_fixture_usage_invalid",
                        f"{label} exceeds its numeric/null leaf limit",
                    )
            else:
                raise _error(
                    "surface_fixture_usage_invalid",
                    f"{label} contains a non-numeric counter value",
                )

    visit(value, ())


def _validate_response_projection(
    value: object,
    *,
    api_surface: str,
    label: str,
    require_http_status: bool = True,
) -> Mapping[str, Any]:
    projection = _object(value, label=label)
    surface_fields = {
        "responses": {"status", "incomplete_details"},
        "messages": {"stop_reason", "stop_sequence"},
        "openai_compatible_chat_completions": {"finish_reason"},
    }.get(api_surface)
    if surface_fields is None:
        raise _error("surface_fixture_shape_invalid", f"{label} has an unknown surface")
    allowed = surface_fields | {
        "usage",
        "provider_request_id_sha256",
        "http_status",
    }
    required = {"usage", "http_status"} if require_http_status else {"usage"}
    if set(projection) - allowed or not required <= set(projection):
        raise _error(
            "surface_fixture_shape_invalid",
            f"{label} has forbidden or missing fields",
        )
    for field in ("status", "finish_reason", "stop_reason"):
        if field not in projection:
            continue
        child = projection[field]
        if child is not None and (
            not isinstance(child, str)
            or not _SAFE_SCHEMA_VALUE.fullmatch(child)
        ):
            raise _error(
                "surface_fixture_shape_invalid",
                f"{label}.{field} is not a bounded schema value",
            )
    if "stop_sequence" in projection:
        child = projection["stop_sequence"]
        if child is not None and not (
            isinstance(child, int)
            and not isinstance(child, bool)
            and 0 <= child <= 2_147_483_647
        ) and not (
            isinstance(child, str) and len(child.encode("utf-8")) <= 64
        ):
            raise _error(
                "surface_fixture_shape_invalid",
                f"{label}.stop_sequence is not a bounded fixture value",
            )
    if "incomplete_details" in projection:
        details = projection["incomplete_details"]
        if isinstance(details, Mapping):
            if set(details) - {"reason"}:
                raise _error(
                    "surface_fixture_shape_invalid",
                    f"{label}.incomplete_details has extra fields",
                )
            reason = details.get("reason")
            if reason is not None and (
                not isinstance(reason, str)
                or not _SAFE_SCHEMA_VALUE.fullmatch(reason)
            ):
                raise _error(
                    "surface_fixture_shape_invalid",
                    f"{label}.incomplete_details.reason is unsafe",
                )
        elif details is not None and not (
            isinstance(details, str) and len(details.encode("utf-8")) <= 64
        ):
            raise _error(
                "surface_fixture_shape_invalid",
                f"{label}.incomplete_details is an unsafe fixture value",
            )
    _validate_usage_tree(projection["usage"], label=f"{label}.usage")
    request_hash = projection.get("provider_request_id_sha256")
    if request_hash is not None and (
        not isinstance(request_hash, str) or not _SHA256.fullmatch(request_hash)
    ):
        raise _error(
            "surface_fixture_shape_invalid",
            f"{label}.provider_request_id_sha256 is invalid",
        )
    if "http_status" in projection:
        http_status = projection["http_status"]
        if (
            isinstance(http_status, bool)
            or not isinstance(http_status, int)
            or not 100 <= http_status <= 599
        ):
            raise _error(
                "surface_fixture_shape_invalid", f"{label}.http_status is invalid"
            )
    _scan_sensitive_tree(projection, label=label)
    return projection


def _validate_v1_source(value: object, *, label: str) -> None:
    source = _object(value, label=label)
    kind = source.get("kind")
    expected = _V1_SOURCE_FIELDS.get(str(kind))
    if expected is None or set(source) != expected:
        raise _error("surface_v1_fixture_invalid", f"{label} has invalid fields")
    if kind == "live_response":
        if (
            source.get("capture_profile") != "minimal_completion_probe_v1"
            or source.get("origin") != "https://api.deepseek.com"
            or source.get("stream") is not False
            or source.get("thinking") != "disabled"
            or source.get("prompt") != "fixed_trivial_input_content_not_persisted"
            or source.get("network_attempts") != 1
            or source.get("client_retries") != 0
            or type(source.get("max_tokens")) is not int
            or source["max_tokens"] < 1
            or not isinstance(source.get("captured_at_utc"), str)
        ):
            raise _error("surface_v1_fixture_invalid", f"{label} live source is invalid")
    elif kind in {"sdk_type_definition", "sdk_type_definition_generated_from_openapi"}:
        files = _array(source.get("files"), label=f"{label}.files")
        symbols = _array(source.get("symbols"), label=f"{label}.symbols")
        if not files or not symbols or any(not isinstance(item, str) for item in symbols):
            raise _error("surface_v1_fixture_invalid", f"{label} SDK source is invalid")
        for item in files:
            file_info = _object(item, label=f"{label}.files[]")
            path = file_info.get("path")
            if (
                set(file_info) != {"path", "bytes", "sha256"}
                or not isinstance(path, str)
                or not path
                or path.startswith(("/", "\\"))
                or "\\" in path
                or ".." in PurePosixPath(path).parts
                or type(file_info.get("bytes")) is not int
                or file_info["bytes"] < 1
                or not isinstance(file_info.get("sha256"), str)
                or not _SHA256.fullmatch(file_info["sha256"])
            ):
                raise _error("surface_v1_fixture_invalid", f"{label} file source is invalid")
    else:
        if (
            source.get("successful_handshake_observed") is not False
            or source.get("openai_style_compatibility_complete") is not False
            or source.get("existing_status") != "no_successful_handshake"
        ):
            raise _error("surface_v1_fixture_invalid", f"{label} prose source is invalid")
    _scan_sensitive_tree(source, label=label)


def _apply_v1_derivation(
    fixture: Mapping[str, Any],
    fixtures: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    derivation = _object(fixture.get("derivation"), label="v1 fixture derivation")
    if set(derivation) != {"kind", "base_fixture_id", "operations"} or derivation.get(
        "kind"
    ) != "synthetic_mutation":
        raise _error("surface_v1_fixture_invalid", "v1 fixture derivation is invalid")
    base_id = _identifier(derivation.get("base_fixture_id"), label="base_fixture_id")
    base = fixtures.get(base_id)
    if (
        base is None
        or base.get("provider_id") != fixture.get("provider_id")
        or base.get("api_surface") != fixture.get("api_surface")
    ):
        raise _error("surface_v1_fixture_invalid", "v1 fixture base is invalid")
    output = copy.deepcopy(
        dict(_object(base.get("response_projection"), label="v1 base projection"))
    )
    for raw_operation in _array(derivation.get("operations"), label="v1 operations"):
        operation = _object(raw_operation, label="v1 operation")
        op = operation.get("op")
        raw_paths = operation.get("paths") if op == "remove" and "paths" in operation else None
        paths = list(_array(raw_paths, label="v1 remove paths")) if raw_paths is not None else [operation.get("path")]
        expected_fields = {"op", "paths"} if raw_paths is not None else (
            {"op", "path", "value"} if op == "replace" else
            {"op", "path", "sentinel"} if op == "replace_with_unknown_sentinel" else
            {"op", "path"}
        )
        if set(operation) != expected_fields or not paths:
            raise _error("surface_v1_fixture_invalid", "v1 mutation fields are invalid")
        for path in paths:
            if (
                not isinstance(path, str)
                or not path.startswith("/response_projection/")
                or "/" in path[len("/response_projection/") :]
            ):
                raise _error("surface_v1_fixture_invalid", "v1 mutation path is invalid")
            field = path[len("/response_projection/") :]
            if field not in output:
                raise _error("surface_v1_fixture_invalid", "v1 mutation target is absent")
            if op == "remove":
                del output[field]
            elif op == "replace":
                output[field] = copy.deepcopy(operation.get("value"))
            elif op == "replace_with_unknown_sentinel":
                sentinel = operation.get("sentinel")
                if (
                    not isinstance(sentinel, str)
                    or not sentinel.startswith("__fixture_unknown_native_value_")
                    or len(sentinel.encode("utf-8")) > 64
                ):
                    raise _error("surface_v1_fixture_invalid", "v1 unknown sentinel is invalid")
                output[field] = sentinel
            else:
                raise _error("surface_v1_fixture_invalid", "v1 mutation op is invalid")
    return output


def _validate_v1_fixture_package(
    root: Path,
    *,
    predecessor_path: Path,
    predecessor_raw: bytes,
) -> None:
    predecessor = _decode_json(predecessor_raw, label="v1 mapping")
    manifest_path = (root / _V1_MANIFEST_RELATIVE_PATH).resolve()
    if not manifest_path.is_relative_to(root):
        raise _error("surface_artifact_path_invalid", "v1 manifest escapes repository")
    try:
        manifest = _decode_json(manifest_path.read_bytes(), label="v1 fixture manifest")
    except OSError as exc:
        raise _error("surface_artifact_read_failed", "v1 fixture manifest cannot be read") from exc
    if (
        set(manifest) != _V1_MANIFEST_FIELDS
        or manifest.get("schema_version") != "provider-completion-fixture-manifest/1.0"
        or manifest.get("status") != "valid"
        or manifest.get("generated_on") != "2026-09-02"
    ):
        raise _error("surface_v1_manifest_invalid", "v1 fixture manifest is invalid")
    claim_boundary = _object(
        manifest.get("claim_boundary"), label="v1 manifest.claim_boundary"
    )
    if claim_boundary != {
        "evaluation_run": False,
        "mapping_is_adapter_implementation": False,
        "model_quality_claim_allowed": False,
        "provider_registration_allowed": False,
        "run_ledger_written": False,
        "task_pass_computed": False,
    }:
        raise _error("surface_v1_manifest_invalid", "v1 claim boundary is invalid")
    v1_root = manifest_path.parent
    mapping_commitment = _object(manifest.get("mapping"), label="v1 manifest.mapping")
    coverage_commitment = _object(
        manifest.get("rule_coverage"), label="v1 manifest.rule_coverage"
    )
    if (
        set(mapping_commitment) != _FILE_COMMITMENT_FIELDS
        or set(coverage_commitment) != _FILE_COMMITMENT_FIELDS
        or mapping_commitment.get("file") != predecessor_path.name
        or mapping_commitment.get("bytes") != len(predecessor_raw)
        or mapping_commitment.get("sha256")
        != hashlib.sha256(predecessor_raw).hexdigest()
        or coverage_commitment.get("file") != "rule_coverage_v1.json"
    ):
        raise _error("surface_v1_manifest_invalid", "v1 mapping commitments are invalid")
    coverage_path = _safe_relative_path(
        v1_root, coverage_commitment.get("file"), allowed_root=v1_root
    )
    _decode_json(
        _read_committed_file(
            coverage_path, coverage_commitment, label="v1 rule coverage"
        ),
        label="v1 rule coverage",
    )

    fixture_commitments = _array(manifest.get("fixtures"), label="v1 fixtures")
    if len(fixture_commitments) != 29:
        raise _error("surface_v1_manifest_invalid", "v1 fixture count is not 29")
    fixtures: dict[str, Mapping[str, Any]] = {}
    fixture_paths: set[Path] = set()
    entries: dict[str, Mapping[str, Any]] = {}
    for raw_commitment in fixture_commitments:
        commitment = _object(raw_commitment, label="v1 fixture commitment")
        if set(commitment) != _V1_FIXTURE_COMMITMENT_FIELDS:
            raise _error("surface_v1_manifest_invalid", "v1 fixture commitment is invalid")
        fixture_id = _identifier(commitment.get("fixture_id"), label="v1 fixture_id")
        if fixture_id in fixtures:
            raise _error("surface_v1_manifest_invalid", "v1 fixture ID repeats")
        fixture_path = _safe_relative_path(
            v1_root / "fixtures",
            commitment.get("file"),
            allowed_root=v1_root / "fixtures",
        )
        if fixture_path in fixture_paths:
            raise _error("surface_v1_manifest_invalid", "v1 fixture path repeats")
        fixture_paths.add(fixture_path)
        fixture = _decode_json(
            _read_committed_file(
                fixture_path, commitment, label=f"v1 fixture {fixture_id}"
            ),
            label=f"v1 fixture {fixture_id}",
        )
        if set(fixture) != _V1_FIXTURE_FIELDS:
            raise _error("surface_v1_fixture_invalid", "v1 fixture fields are invalid")
        provenance = _object(fixture.get("provenance"), label="v1 provenance")
        expected_surface = {
            "deepseek": "openai_compatible_chat_completions",
            "openai": "responses",
            "anthropic": "messages",
            "moonshot_kimi": "openai_compatible_chat_completions",
        }.get(str(fixture.get("provider_id")))
        source_kind = (
            provenance.get("source", {}).get("kind")
            if isinstance(provenance.get("source"), Mapping)
            else None
        )
        expected_tier = {
            "live_response": "live_capture",
            "sdk_type_definition": "official_schema",
            "sdk_type_definition_generated_from_openapi": "official_schema",
            "doc_prose": "doc_prose",
        }.get(str(source_kind))
        derived = fixture.get("derivation") is not None
        if source_kind == "live_response":
            expected_unverified = derived
            expected_live_validation = (
                "synthetic_mutation_from_validated_base" if derived else "validated"
            )
        elif source_kind in {
            "sdk_type_definition",
            "sdk_type_definition_generated_from_openapi",
        }:
            expected_unverified = True
            expected_live_validation = "pending_first_call"
        else:
            expected_unverified = True
            expected_live_validation = "no_successful_handshake"
        if (
            fixture.get("fixture_schema_version") != "provider-completion-fixture/1.0"
            or fixture.get("fixture_id") != fixture_id
            or fixture.get("provider_id") != commitment.get("provider_id")
            or fixture.get("scenario") != commitment.get("scenario")
            or set(provenance) != _V1_PROVENANCE_FIELDS
            or provenance.get("tier") != commitment.get("provenance_tier")
            or provenance.get("tier") != expected_tier
            or provenance.get("as_of_date") != "2026-09-02"
            or provenance.get("live_validation") != expected_live_validation
            or type(provenance.get("unverified_shape")) is not bool
            or provenance.get("unverified_shape") is not expected_unverified
            or provenance.get("unverified_shape") is not commitment.get("unverified_shape")
            or fixture.get("api_surface") != expected_surface
            or not isinstance(fixture.get("scenario"), str)
            or not _SAFE_ID.fullmatch(fixture["scenario"])
            or fixture.get("capture_boundary") != _V1_CAPTURE_BOUNDARY
            or commitment.get("derivation_kind")
            != (None if fixture.get("derivation") is None else "synthetic_mutation")
        ):
            raise _error("surface_v1_fixture_invalid", "v1 fixture metadata is invalid")
        api_surface = fixture.get("api_surface")
        if not isinstance(api_surface, str):
            raise _error("surface_v1_fixture_invalid", "v1 fixture surface is invalid")
        _validate_v1_source(provenance.get("source"), label="v1 provenance.source")
        _validate_response_projection(
            fixture.get("response_projection"),
            api_surface=api_surface,
            label=f"v1 fixture {fixture_id}.response_projection",
        )
        _scan_sensitive_tree(fixture, label=f"v1 fixture {fixture_id}")
        fixtures[fixture_id] = fixture
        entries[fixture_id] = commitment

    actual_paths = {path.resolve() for path in (v1_root / "fixtures").glob("*.json")}
    if actual_paths != fixture_paths:
        raise _error("surface_v1_manifest_invalid", "v1 fixture directory is not closed")
    for fixture_id, fixture in fixtures.items():
        if fixture.get("derivation") is not None:
            derivation = _object(
                fixture.get("derivation"), label="v1 fixture derivation"
            )
            base = _object(
                fixtures.get(str(derivation.get("base_fixture_id"))),
                label="v1 fixture base",
            )
            if (
                _apply_v1_derivation(fixture, fixtures)
                != fixture.get("response_projection")
                or _object(
                    fixture.get("provenance"), label="v1 fixture provenance"
                ).get("source")
                != _object(base.get("provenance"), label="v1 base provenance").get(
                    "source"
                )
            ):
                raise _error(
                    "surface_v1_fixture_invalid",
                    "v1 fixture derivation or source differs",
                )

    fixture_expectations = _object(
        predecessor.get("fixture_expectations"),
        label="v1 fixture expectations",
    )
    if set(fixture_expectations) != set(fixtures):
        raise _error(
            "surface_v1_fixture_invalid",
            "v1 fixture set differs from mapping expectations",
        )
    for fixture_id, fixture in fixtures.items():
        expectation = _object(
            fixture_expectations.get(fixture_id), label="v1 fixture expectation"
        )
        if set(expectation) != {
            "normalized_completion_state",
            "truncation_signal_source",
            "preserved_native_value",
            "matched_rule_id",
        }:
            raise _error(
                "surface_v1_fixture_invalid", "v1 fixture expectation is invalid"
            )
        actual = map_completion(
            _object(
                fixture.get("response_projection"), label="v1 response projection"
            ),
            str(fixture.get("provider_id")),
            predecessor,
        )
        expected = (
            expectation["normalized_completion_state"],
            expectation["truncation_signal_source"],
            expectation["preserved_native_value"],
            expectation["matched_rule_id"],
        )
        if actual != expected:
            raise _error(
                "surface_v1_fixture_invalid",
                f"v1 fixture {fixture_id} differs from its mapping expectation",
            )

    summary = _object(manifest.get("summary"), label="v1 manifest.summary")
    provenance_counts: dict[str, int] = {}
    for fixture in fixtures.values():
        tier = str(fixture["provenance"]["tier"])
        provenance_counts[tier] = provenance_counts.get(tier, 0) + 1
    if (
        set(summary)
        != {
            "adapter_changes",
            "deepseek_live_network_calls",
            "fixture_count",
            "live_attribution_fixture_count",
            "mapper_implementation_included",
            "other_provider_live_network_calls",
            "provenance_counts",
            "provider_count",
            "unverified_shape_count",
        }
        or summary.get("adapter_changes") != 0
        or summary.get("deepseek_live_network_calls") != 2
        or summary.get("fixture_count") != len(fixtures)
        or summary.get("live_attribution_fixture_count") != 2
        or summary.get("mapper_implementation_included") is not True
        or summary.get("other_provider_live_network_calls") != 0
        or summary.get("provenance_counts") != provenance_counts
        or summary.get("provider_count")
        != len({fixture["provider_id"] for fixture in fixtures.values()})
        or summary.get("unverified_shape_count")
        != sum(bool(fixture["provenance"]["unverified_shape"]) for fixture in fixtures.values())
    ):
        raise _error("surface_v1_manifest_invalid", "v1 summary is invalid")
    privacy = _object(manifest.get("privacy_review"), label="v1 privacy review")
    if set(privacy) != {
        "absolute_paths_absent",
        "api_keys_absent",
        "content_fields_absent",
        "duplicate_keys_rejected",
        "emails_absent",
        "manual_review_completed",
        "manual_review_finding_count",
        "manual_review_required_before_commit",
        "manual_review_scope",
        "org_account_identifiers_absent",
        "raw_request_ids_absent",
        "strict_json_parsed",
        "system_prompt_absent",
        "tool_arguments_absent",
    } or any(
        privacy.get(field) is not True
        for field in privacy
        if field not in {"manual_review_finding_count", "manual_review_scope"}
    ) or privacy.get("manual_review_finding_count") != 0 or privacy.get(
        "manual_review_scope"
    ) != "all_29_fixtures":
        raise _error("surface_v1_manifest_invalid", "v1 privacy review is invalid")


def _validate_probe_receipt(receipt: Mapping[str, Any]) -> None:
    if (
        set(receipt) != _PROBE_RECEIPT_FIELDS
        or receipt.get("probe_id") != "deepseek_responses_completion_shape_v3"
        or receipt.get("status") != "completed"
        or receipt.get("post_request_cleanup_succeeded") is not True
        or receipt.get("boundary") != _PROBE_BOUNDARY
        or receipt.get("transport") != _PROBE_TRANSPORT
    ):
        raise _error("surface_source_receipt_shape_invalid", "probe receipt fields are invalid")
    limitations = _object(receipt.get("limitations"), label="probe limitations")
    if set(limitations) != _PROBE_LIMITATION_FIELDS:
        raise _error("surface_source_receipt_shape_invalid", "probe limitations fields are invalid")
    expected_limitations = {
        "forced_cap_minimum_output_tokens": 16,
        "forced_cap_attempt_completed": True,
        "forced_cap_attempt_retained_status": "incomplete",
        "forced_cap_not_observed_interpretation_scope": (
            "applies_only_when_no_truncation_was_observed"
        ),
        "forced_cap_not_observed_interpretation": (
            "not_triggered_not_evidence_of_nonexistence"
        ),
        "adapter_kwargs_equivalence_claimed": False,
        "sdk_built_kwargs_offline_diff_performed": False,
        "known_but_unobserved_values_remain_unmapped": True,
        "supersedes_probe_receipt_sha256": "d124a07f40b1031247a832409bbf13f9c352e42862f3d4c64da6690eb89709ad",
        "superseded_field": "observed_shape_count_max",
        "superseded_field_reason": "counted_requests_not_distinct_shapes",
        "message_stage_probe_max_output_tokens": 96,
        "message_stage_probe_cap_is_fixed_non_adaptive": True,
        "message_stage_entry_guaranteed": False,
        "message_stage_requested_word_repetitions": 500,
        "message_stage_final_planned_attempt": True,
        "no_further_message_stage_probe_if_target_not_observed": True,
        "input_token_hard_cap_claimed": False,
        "message_stage_cap_not_observed_interpretation_scope": (
            "applies_only_when_message_stage_cap_target_observed_is_false"
        ),
        "message_stage_cap_not_observed_interpretation": (
            "not_triggered_not_evidence_of_nonexistence"
        ),
        "observed_response_count": 3,
        "observed_response_count_definition": (
            "sanitized_response_shape_records_present_in_this_receipt"
        ),
        "distinct_top_level_shape_count": 2,
        "distinct_top_level_shape_eligible_response_count": 3,
        "distinct_top_level_shape_unavailable_response_count": 0,
        "distinct_shape_definition": (
            "(status_key_present,status_value); "
            "incomplete_details_reason_presence; ordered output_item_shapes "
            "((type_key_present,type_value),(status_key_present,status_value)); "
            "sorted top_level_presence key/value pairs"
        ),
        "message_stage_cap_attempt_completed": True,
        "message_output_item_observed": False,
        "message_output_item_with_incomplete_status_observed": False,
        "top_level_incomplete_max_output_tokens_observed": True,
        "message_stage_cap_target_observed": False,
    }
    if any(limitations.get(key) != value for key, value in expected_limitations.items()):
        raise _error("surface_source_receipt_shape_invalid", "probe limitations values are invalid")
    probes = _array(receipt.get("probes"), label="probe records")
    labels_and_caps = (
        ("responses_normal_completion", 256),
        ("responses_output_cap_attempt", 16),
        ("responses_message_stage_cap_attempt", 96),
    )
    if len(probes) != len(labels_and_caps):
        raise _error("surface_source_receipt_shape_invalid", "probe record count is invalid")
    by_label: dict[str, Mapping[str, Any]] = {}
    for raw_probe, (expected_label, expected_cap) in zip(probes, labels_and_caps):
        probe = _object(raw_probe, label="probe record")
        if (
            set(probe) != _PROBE_RECORD_FIELDS
            or probe.get("probe_label") != expected_label
            or probe.get("requested_max_output_tokens") != expected_cap
            or probe.get("http_status") != 200
            or probe.get("raw_response_cleanup_state") != "succeeded"
        ):
            raise _error("surface_source_receipt_shape_invalid", "probe record fields are invalid")
        request_hash = probe.get("provider_request_id_sha256")
        if request_hash is not None and (
            not isinstance(request_hash, str) or not _SHA256.fullmatch(request_hash)
        ):
            raise _error("surface_source_receipt_shape_invalid", "probe request hash is invalid")
        if not isinstance(probe.get("provider_request_id_header_present"), bool) or not isinstance(
            probe.get("provider_request_id_hash_withheld"), bool
        ):
            raise _error("surface_source_receipt_shape_invalid", "probe request ID state is invalid")
        if (
            probe["provider_request_id_header_present"] is False
            and (request_hash is not None or probe["provider_request_id_hash_withheld"] is True)
        ):
            raise _error("surface_source_receipt_shape_invalid", "probe request ID state conflicts")
        for list_field in (
            "top_level_keys_observed",
            "incomplete_details_keys_observed",
            "usage_keys_observed",
        ):
            value = probe.get(list_field)
            if value is not None and (
                not isinstance(value, list)
                or len(value) > 128
                or any(not isinstance(item, str) or not _SAFE_SCHEMA_VALUE.fullmatch(item) for item in value)
                or value != sorted(set(value))
            ):
                raise _error("surface_source_receipt_shape_invalid", f"probe {list_field} is invalid")
        presence = _object(probe.get("top_level_presence"), label="probe presence")
        if set(presence) != {"status", "incomplete_details", "stop_sequence"} or any(
            value not in {"missing", "present_null", "present_non_null"}
            for value in presence.values()
        ):
            raise _error("surface_source_receipt_shape_invalid", "probe presence is invalid")
        if probe.get("incomplete_details_reason_presence") not in {
            "parent_missing",
            "parent_null",
            "parent_non_object",
            "reason_missing",
            "reason_present_null",
            "reason_present_non_null",
        }:
            raise _error(
                "surface_source_receipt_shape_invalid",
                "probe incomplete-details reason presence is invalid",
            )
        shape_only = _object(
            probe.get("shape_only_top_level_observations"), label="shape-only observations"
        )
        if set(shape_only) != {"content_filters", "error", "truncation"}:
            raise _error("surface_source_receipt_shape_invalid", "shape-only keys are invalid")
        for diagnostic in shape_only.values():
            item = _object(diagnostic, label="shape-only diagnostic")
            if (
                set(item) != {"presence", "json_type", "direct_child_count"}
                or item.get("presence")
                not in {"missing", "present_null", "present_non_null"}
                or item.get("json_type")
                not in {None, "null", "boolean", "string", "number", "array", "object"}
                or (
                    item.get("direct_child_count") is not None
                    and (
                        type(item["direct_child_count"]) is not int
                        or item["direct_child_count"] < 0
                    )
                )
            ):
                raise _error("surface_source_receipt_shape_invalid", "shape-only diagnostic is invalid")
        output_container = _object(probe.get("output_container"), label="output container")
        if (
            set(output_container)
            != {"presence", "json_type", "item_count", "shapes_truncated"}
            or output_container.get("presence")
            not in {"missing", "present_null", "present_non_null"}
            or output_container.get("json_type")
            not in {None, "null", "boolean", "string", "number", "array", "object"}
            or type(output_container.get("shapes_truncated")) is not bool
            or (
                output_container.get("item_count") is not None
                and (
                    type(output_container["item_count"]) is not int
                    or output_container["item_count"] < 0
                )
            )
        ):
            raise _error("surface_source_receipt_shape_invalid", "output container is invalid")
        shapes = _array(probe.get("output_item_shapes"), label="output item shapes")
        if len(shapes) > 128:
            raise _error("surface_source_receipt_shape_invalid", "too many output item shapes")
        for index, raw_shape in enumerate(shapes):
            shape = _object(raw_shape, label="output item shape")
            if shape.get("index") != index or set(shape) not in {
                frozenset({"index", "non_object"}),
                frozenset({"index", "status_key_present"}),
                frozenset({"index", "status_key_present", "type"}),
                frozenset({"index", "status_key_present", "status"}),
                frozenset({"index", "status_key_present", "type", "status"}),
            }:
                raise _error("surface_source_receipt_shape_invalid", "output item shape is invalid")
            if "non_object" in shape and shape["non_object"] is not True:
                raise _error("surface_source_receipt_shape_invalid", "non-object shape marker is invalid")
            if "status_key_present" in shape and type(shape["status_key_present"]) is not bool:
                raise _error("surface_source_receipt_shape_invalid", "shape status presence is invalid")
            for field in ("type", "status"):
                if field in shape and (
                    not isinstance(shape[field], str) or not _SAFE_SCHEMA_VALUE.fullmatch(shape[field])
                ):
                    raise _error("surface_source_receipt_shape_invalid", "output shape value is invalid")
        projection = _validate_response_projection(
            probe.get("response_projection"),
            api_surface="responses",
            label=f"probe {expected_label}.response_projection",
            require_http_status=False,
        )
        expected_shape = _PROBE_EXPECTED_COMPLETION_SHAPES[expected_label]
        if (
            set(projection) != {"status", "incomplete_details", "usage"}
            or projection.get("status") != expected_shape["status"]
            or projection.get("incomplete_details")
            != expected_shape["incomplete_details"]
            or probe.get("provider_request_id_sha256") is not None
            or probe.get("provider_request_id_header_present") is not False
            or probe.get("provider_request_id_hash_withheld") is not False
            or probe.get("top_level_keys_observed") != _PROBE_TOP_LEVEL_KEYS
            or probe.get("top_level_presence") != expected_shape["presence"]
            or probe.get("shape_only_top_level_observations")
            != _PROBE_SHAPE_ONLY_OBSERVATIONS
            or probe.get("incomplete_details_reason_presence")
            != expected_shape["reason_presence"]
            or probe.get("incomplete_details_keys_observed")
            != expected_shape["detail_keys"]
            or probe.get("usage_keys_observed") != _PROBE_USAGE_KEYS
            or probe.get("output_container")
            != {
                "presence": "present_non_null",
                "json_type": "array",
                "item_count": len(expected_shape["output_shapes"]),
                "shapes_truncated": False,
            }
            or probe.get("output_item_shapes") != expected_shape["output_shapes"]
        ):
            raise _error(
                "surface_source_receipt_shape_invalid",
                f"probe {expected_label} differs from its committed shape",
            )
        by_label[expected_label] = probe

    distinct_shape_keys: set[tuple[object, ...]] = set()
    for probe in by_label.values():
        projection = _object(
            probe.get("response_projection"), label="probe response projection"
        )
        item_sequence = tuple(
            (
                ("type" in shape, shape.get("type")),
                ("status" in shape, shape.get("status")),
            )
            for shape in _array(
                probe.get("output_item_shapes"), label="probe output item shapes"
            )
        )
        presence = _object(
            probe.get("top_level_presence"), label="probe top-level presence"
        )
        distinct_shape_keys.add(
            (
                ("status" in projection, projection.get("status")),
                probe.get("incomplete_details_reason_presence"),
                item_sequence,
                tuple(sorted(presence.items())),
            )
        )
    if (
        len(distinct_shape_keys)
        != limitations.get("distinct_top_level_shape_count")
        or len(by_label)
        != limitations.get("distinct_top_level_shape_eligible_response_count")
        or limitations.get("distinct_top_level_shape_unavailable_response_count") != 0
    ):
        raise _error(
            "surface_source_receipt_shape_invalid",
            "probe distinct-shape counts differ from their published definition",
        )
    normal_shapes = _array(
        by_label["responses_normal_completion"].get("output_item_shapes"),
        label="normal output shapes",
    )
    message_cap_shapes = _array(
        by_label["responses_message_stage_cap_attempt"].get("output_item_shapes"),
        label="message-stage output shapes",
    )
    if not any(
        isinstance(item, Mapping)
        and item.get("type") == "message"
        and item.get("status") == "completed"
        for item in normal_shapes
    ) or any(
        isinstance(item, Mapping) and item.get("type") == "message"
        for item in message_cap_shapes
    ):
        raise _error(
            "surface_source_receipt_scope_invalid",
            "message observation scope differs from committed probes",
        )
    _scan_sensitive_tree(receipt, label="probe receipt")


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _error(
            "surface_predecessor_mapping_invalid",
            "predecessor mapping is not canonical JSON",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _safe_relative_path(base: Path, value: object, *, allowed_root: Path) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise _error(
            "surface_artifact_path_invalid",
            "artifact path is not canonical relative POSIX",
        )
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise _error(
            "surface_artifact_path_invalid", "artifact path escapes its fixed root"
        )
    target = (base / Path(*relative.parts)).resolve()
    if not target.is_relative_to(allowed_root.resolve()):
        raise _error(
            "surface_artifact_path_invalid", "artifact path escapes its fixed root"
        )
    return target


def _read_committed_file(
    path: Path,
    commitment: Mapping[str, Any],
    *,
    label: str,
) -> bytes:
    size = commitment.get("bytes")
    digest = commitment.get("sha256")
    if (
        type(size) is not int
        or size < 1
        or not isinstance(digest, str)
        or not _SHA256.fullmatch(digest)
    ):
        raise _error(
            "surface_manifest_invalid", f"{label} commitment is invalid"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _error(
            "surface_artifact_read_failed", f"{label} cannot be read"
        ) from exc
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise _error(
            "surface_artifact_commitment_mismatch",
            f"{label} bytes or SHA-256 differ",
        )
    return raw


def _collect_rules(value: object) -> tuple[Mapping[str, Any], ...]:
    rules: list[Mapping[str, Any]] = []

    def visit(child: object) -> None:
        if isinstance(child, Mapping):
            if {
                "rule_id",
                "precedence_stage",
                "condition",
                "normalized_completion_state",
                "truncation_signal_source",
            }.issubset(child):
                rules.append(child)
                return
            for nested in child.values():
                visit(nested)
        elif isinstance(child, Sequence) and not isinstance(
            child, (str, bytes, bytearray)
        ):
            for nested in child:
                visit(nested)

    visit(value)
    return tuple(rules)


def _condition_paths(value: object) -> tuple[str, ...]:
    condition = _object(value, label="mapping rule condition")
    operator = condition.get("op")
    paths: list[str] = []
    if operator == "all":
        for child in _array(
            condition.get("conditions"), label="condition.conditions"
        ):
            paths.extend(_condition_paths(child))
    elif operator == "not":
        paths.extend(_condition_paths(condition.get("condition")))
    elif operator == "all_missing":
        fields = _array(condition.get("fields"), label="condition.fields")
        if not all(isinstance(field, str) for field in fields):
            raise _error(
                "surface_registry_invalid", "condition fields are invalid"
            )
        paths.extend(fields)
    elif operator in {
        "equals",
        "is_null",
        "is_present_non_null",
        "json_type_is",
        "default_unknown",
    }:
        field = condition.get("field")
        if not isinstance(field, str):
            raise _error("surface_registry_invalid", "condition field is invalid")
        paths.append(field)
    else:
        raise _error(
            "surface_registry_invalid", "condition operator is unsupported"
        )
    return tuple(paths)


def _validate_mapping_paths(
    provider_mapping: Mapping[str, Any], api_surface: str
) -> None:
    allowed = _ALLOWED_CONDITION_PATHS.get(api_surface)
    if allowed is None:
        raise _error(
            "surface_registry_invalid", "API surface has no condition-field allowlist"
        )
    rules = _collect_rules(provider_mapping)
    if not rules:
        raise _error(
            "surface_registry_invalid", "provider mapping has no executable rules"
        )
    for rule in rules:
        rule_id = rule.get("rule_id")
        for field in _condition_paths(rule.get("condition")):
            if field not in allowed:
                raise _error(
                    "surface_mapping_condition_field_forbidden",
                    f"rule {rule_id!r} reads forbidden field {field!r}",
                )
        preserved_field = rule.get("preserved_native_value_field")
        if preserved_field is not None and preserved_field not in allowed:
            raise _error(
                "surface_mapping_condition_field_forbidden",
                f"rule {rule_id!r} preserves forbidden field {preserved_field!r}",
            )


def _predecessor_provenance_tier(
    predecessor: Mapping[str, Any], provider_id: str
) -> str:
    tiers = _object(
        predecessor.get("provenance_tiers"), label="predecessor provenance_tiers"
    )
    matches: list[str] = []
    for tier, raw_policy in tiers.items():
        policy = _object(raw_policy, label=f"provenance_tiers.{tier}")
        applies_to = _array(
            policy.get("applies_to"), label=f"provenance_tiers.{tier}.applies_to"
        )
        if provider_id in applies_to:
            matches.append(str(tier))
    if len(matches) != 1:
        raise _error(
            "surface_registry_invalid", "predecessor provenance is ambiguous"
        )
    return matches[0]


def _canonical_key(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _error(
            "surface_cross_file_semantics_invalid",
            "cross-file value is not canonical JSON",
        ) from exc


def _native_shape(projection: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(projection[key])
        for key in ("status", "incomplete_details")
        if key in projection
    }


def _replay_synthetic_projection(
    fixture: Mapping[str, Any], fixtures: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    derivation = _object(fixture.get("derivation"), label="synthetic derivation")
    if set(derivation) != {"kind", "base_fixture_id", "operations"} or derivation.get(
        "kind"
    ) != "synthetic_mutation":
        raise _error(
            "surface_fixture_derivation_invalid",
            "synthetic derivation has an invalid field set",
        )
    base_id = _identifier(
        derivation.get("base_fixture_id"), label="derivation.base_fixture_id"
    )
    base = fixtures.get(base_id)
    if base is None:
        raise _error(
            "surface_fixture_derivation_invalid", "synthetic base fixture is absent"
        )
    base_provenance = _object(base.get("provenance"), label="base provenance")
    if base_provenance.get("fixture_kind") != "unmodified_live_projection":
        raise _error(
            "surface_fixture_derivation_invalid",
            "synthetic base must be an unmodified live projection",
        )
    output = copy.deepcopy(
        dict(_object(base.get("response_projection"), label="base projection"))
    )
    for raw_operation in _array(
        derivation.get("operations"), label="derivation.operations"
    ):
        operation = _object(raw_operation, label="derivation operation")
        op = operation.get("op")
        path = operation.get("path")
        if (
            not isinstance(path, str)
            or not path.startswith("/response_projection/")
            or "/" in path[len("/response_projection/") :]
        ):
            raise _error(
                "surface_fixture_derivation_invalid", "mutation path is invalid"
            )
        field = path[len("/response_projection/") :]
        if op == "remove":
            if set(operation) != {"op", "path"} or field not in output:
                raise _error(
                    "surface_fixture_derivation_invalid", "remove mutation is invalid"
                )
            del output[field]
        elif op == "replace_with_unknown_sentinel":
            if set(operation) != {"op", "path", "sentinel"} or field not in output:
                raise _error(
                    "surface_fixture_derivation_invalid", "replace mutation is invalid"
                )
            sentinel = operation.get("sentinel")
            if (
                not isinstance(sentinel, str)
                or not sentinel.startswith("__fixture_unknown_native_value_")
                or len(sentinel.encode("utf-8")) > 64
            ):
                raise _error(
                    "surface_fixture_derivation_invalid", "unknown sentinel is invalid"
                )
            output[field] = sentinel
        else:
            raise _error(
                "surface_fixture_derivation_invalid", "mutation operation is unsupported"
            )
    return output


def _validate_registry(
    registry: Mapping[str, Any], predecessor: Mapping[str, Any]
) -> tuple[
    dict[tuple[str, str, str], Mapping[str, Any]],
    dict[tuple[str, str, str], Mapping[str, Any]],
]:
    if set(registry) != _TOP_LEVEL_FIELDS:
        raise _error(
            "surface_registry_invalid", "registry has an unexpected field set"
        )
    if registry.get("schema_version") != _REGISTRY_SCHEMA_VERSION:
        raise _error(
            "surface_registry_invalid", "registry schema version is unsupported"
        )
    if (
        registry.get("status")
        != "executable_offline_surface_registry_zero_adapter_changes"
        or registry.get("documented_on") != "2026-09-03"
        or
        registry.get("adapter_changes_included") is not False
        or registry.get("online_calls_performed") is not False
        or registry.get("registry_key_fields")
        != ["provider_id", "api_surface", "transport_id"]
        or registry.get("surface_mismatch_behavior") != "fail_closed"
        or registry.get("nearest_surface_fallback_allowed") is not False
    ):
        raise _error(
            "surface_registry_invalid", "registry safety boundary is invalid"
        )

    predecessor_commitment = _object(
        registry.get("predecessor_mapping"), label="registry.predecessor_mapping"
    )
    if (
        set(predecessor_commitment) != _PREDECESSOR_FIELDS
        or predecessor_commitment.get("immutable") is not True
        or predecessor_commitment.get("schema_version")
        != predecessor.get("schema_version")
        or not isinstance(
            predecessor_commitment.get("canonical_json_sha256"), str
        )
        or not _SHA256.fullmatch(
            predecessor_commitment["canonical_json_sha256"]
        )
        or _canonical_sha256(predecessor)
        != predecessor_commitment["canonical_json_sha256"]
        or predecessor_commitment.get("relative_path")
        != "evals/provider_completion_telemetry_v1/provider_completion_mapping_v1.json"
        or predecessor_commitment.get("bytes") != 48071
        or predecessor_commitment.get("sha256")
        != "2a3e4c6a81fd76d9e20542091fbffdd4c6137d2e730b626117446109658b946d"
    ):
        raise _error(
            "surface_predecessor_mapping_mismatch", "predecessor content differs"
        )

    predecessor_providers = _object(
        predecessor.get("providers"), label="predecessor.providers"
    )
    entries: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    provider_mappings: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for index, raw_entry in enumerate(
        _array(registry.get("entries"), label="registry.entries")
    ):
        entry = _object(raw_entry, label=f"registry.entries[{index}]")
        source_kind = entry.get("mapping_source")
        expected_fields = set(_ENTRY_FIELDS)
        if source_kind == "v1_provider_alias":
            expected_fields.add("predecessor_provider_id")
        elif source_kind == "inline_successor":
            expected_fields.update({"provider_mapping", "fixture_expectations"})
        else:
            raise _error(
                "surface_registry_invalid", "mapping source is unsupported"
            )
        if set(entry) != expected_fields:
            raise _error(
                "surface_registry_invalid", f"entry {index} has unexpected fields"
            )
        provider_id = _identifier(
            entry.get("provider_id"), label="entry.provider_id"
        )
        api_surface = _identifier(
            entry.get("api_surface"), label="entry.api_surface"
        )
        transport_id = _identifier(
            entry.get("transport_id"), label="entry.transport_id"
        )
        adapter_version = _identifier(
            entry.get("adapter_version"), label="entry.adapter_version"
        )
        output_counter_path = _identifier(
            entry.get("output_counter_path"), label="entry.output_counter_path"
        )
        mapping_version = _identifier(
            entry.get("mapping_version"), label="entry.mapping_version"
        )
        if (
            entry.get("provenance_tier")
            not in {"live_capture", "official_schema", "doc_prose"}
            or type(entry.get("unverified_shape")) is not bool
            or type(entry.get("first_live_validation_required")) is not bool
            or entry.get("provenance_promotion_allowed") is not False
            or entry.get("offline_selection_allowed") is not True
            or type(entry.get("runtime_binding_allowed")) is not bool
            or entry.get("output_counter_comparability") != "comparable"
            or not isinstance(entry.get("source"), Mapping)
        ):
            raise _error(
                "surface_registry_invalid", f"entry {index} provenance is invalid"
            )
        blocker = entry.get("runtime_binding_blocker")
        if not isinstance(blocker, str) or not blocker:
            raise _error(
                "surface_registry_invalid",
                f"entry {index} runtime blocker is invalid",
            )
        if (
            entry["runtime_binding_allowed"] is not False
            or entry["first_live_validation_required"] is not True
        ):
            raise _error(
                "surface_registry_v2_runtime_promotion_forbidden",
                "v2 entries must retain first-live validation and runtime=false",
            )

        key = (provider_id, api_surface, transport_id)
        if key in entries:
            raise _error(
                "surface_registry_duplicate_key",
                "provider/surface/transport repeats",
            )
        expected_adapter, expected_counter_path = _EXPECTED_RUNTIME_BINDING_METADATA.get(
            key, (None, None)
        )
        if (
            adapter_version != expected_adapter
            or output_counter_path != expected_counter_path
        ):
            raise _error(
                "surface_registry_invalid",
                "Adapter version or output counter path differs from the fixed triple",
            )

        if source_kind == "v1_provider_alias":
            predecessor_provider = _identifier(
                entry.get("predecessor_provider_id"),
                label="entry.predecessor_provider_id",
            )
            if predecessor_provider != provider_id:
                raise _error(
                    "surface_registry_invalid", "v1 alias changes provider ID"
                )
            source = _object(entry.get("source"), label="entry.source")
            if (
                set(source) != _ALIAS_SOURCE_FIELDS
                or source.get("kind") != "immutable_v1_provider_mapping_alias"
            ):
                raise _error(
                    "surface_registry_invalid", "v1 alias source metadata is invalid"
                )
            provider_mapping = _object(
                predecessor_providers.get(provider_id),
                label=f"predecessor.providers.{provider_id}",
            )
            expected_tier = _predecessor_provenance_tier(
                predecessor, provider_id
            )
            if (
                provider_mapping.get("api_surface") != api_surface
                or provider_mapping.get("active_mapping_version")
                != mapping_version
                or entry.get("provenance_tier") != expected_tier
                or entry.get("unverified_shape")
                is not bool(provider_mapping.get("unverified_shape", False))
                or entry.get("first_live_validation_required")
                is not bool(
                    provider_mapping.get("first_live_validation_required", False)
                )
            ):
                raise _error(
                    "surface_alias_metadata_mismatch",
                    "v1 alias metadata is not derived from its predecessor",
                )
        else:
            source = _object(entry.get("source"), label="entry.source")
            provenance_limitations = _object(
                source.get("provenance_limitations"),
                label="entry.source.provenance_limitations",
            )
            if (
                set(source) != _INLINE_SOURCE_FIELDS
                or source.get("kind") != "sanitized_live_probe_receipt"
                or source.get("relative_path") != "probe_out_v3.json"
                or source.get("bytes") != _EXPECTED_PROBE_RECEIPT_BYTES
                or source.get("sha256")
                != _EXPECTED_PROBE_RECEIPT_SHA256
                or source.get("path") != "/responses"
                or source.get("adapter_kwargs_equivalence_claimed") is not False
                or source.get("known_but_unobserved_values_remain_unmapped") is not True
                or set(provenance_limitations) != _PROVENANCE_LIMITATION_FIELDS
                or provenance_limitations
                != {
                    "capture_time": "not_persisted",
                    "authorization_linkage": "external_not_machine_bound",
                    "probe_script_linkage": "external_not_machine_bound",
                    "predecessor_receipt_bytes": "v1_v2_excluded_unavailable",
                    "message_output_item_observed_scope": (
                        "responses_message_stage_cap_attempt_only"
                    ),
                    "normal_probe_message_completed_observed": True,
                }
            ):
                raise _error(
                    "surface_registry_invalid", "inline source metadata is invalid"
                )
            provider_mapping = _object(
                entry.get("provider_mapping"), label="entry.provider_mapping"
            )
            if (
                provider_mapping.get("api_surface") != api_surface
                or provider_mapping.get("active_mapping_version")
                != mapping_version
                or provider_mapping.get("provenance_tier")
                != entry.get("provenance_tier")
                or provider_mapping.get("unverified_shape")
                is not entry.get("unverified_shape")
                or provider_mapping.get("first_live_validation_required")
                is not entry.get("first_live_validation_required")
            ):
                raise _error(
                    "surface_registry_invalid", "inline mapping metadata differs"
                )

        _validate_mapping_paths(provider_mapping, api_surface)
        entries[key] = entry
        provider_mappings[key] = provider_mapping

    if set(entries) != _EXPECTED_TRIPLES:
        raise _error(
            "surface_registry_invalid", "registry triple set is not the fixed v2 set"
        )
    return entries, provider_mappings


def load_verified_surface_registry(
    repository_root: str | Path,
) -> VerifiedSurfaceRegistry:
    """Load the fixed manifest and verify every dependency before selection."""

    root = Path(repository_root).resolve()
    manifest_path = (root / _MANIFEST_RELATIVE_PATH).resolve()
    if not manifest_path.is_relative_to(root):
        raise _error(
            "surface_artifact_path_invalid", "fixed manifest path escapes repository"
        )
    try:
        manifest_raw = manifest_path.read_bytes()
    except OSError as exc:
        raise _error(
            "surface_artifact_read_failed", "fixed manifest cannot be read"
        ) from exc
    manifest = _decode_json(manifest_raw, label="surface fixture manifest")
    if set(manifest) != _MANIFEST_FIELDS:
        raise _error(
            "surface_manifest_invalid", "manifest has an unexpected field set"
        )
    if (
        manifest.get("schema_version")
        != "provider-completion-surface-fixture-manifest/2.0"
        or manifest.get("status") != "offline_only_zero_adapter_changes"
    ):
        raise _error(
            "surface_manifest_invalid", "manifest schema version is unsupported"
        )
    v2_root = manifest_path.parent.resolve()

    registry_commitment = _object(
        manifest.get("registry"), label="manifest.registry"
    )
    if set(registry_commitment) != _FILE_COMMITMENT_FIELDS:
        raise _error(
            "surface_manifest_invalid", "registry commitment fields are invalid"
        )
    registry_path = _safe_relative_path(
        v2_root, registry_commitment.get("file"), allowed_root=v2_root
    )
    registry_raw = _read_committed_file(
        registry_path, registry_commitment, label="surface registry"
    )
    registry = _decode_json(registry_raw, label="surface registry")

    predecessor_commitment = _object(
        registry.get("predecessor_mapping"), label="registry.predecessor_mapping"
    )
    if set(predecessor_commitment) != _PREDECESSOR_FIELDS:
        raise _error(
            "surface_registry_invalid", "predecessor commitment fields are invalid"
        )
    predecessor_path = _safe_relative_path(
        root, predecessor_commitment.get("relative_path"), allowed_root=root
    )
    predecessor_raw = _read_committed_file(
        predecessor_path, predecessor_commitment, label="v1 predecessor mapping"
    )
    predecessor = _decode_json(predecessor_raw, label="v1 predecessor mapping")
    entries, provider_mappings = _validate_registry(registry, predecessor)
    _validate_v1_fixture_package(
        root,
        predecessor_path=predecessor_path,
        predecessor_raw=predecessor_raw,
    )

    fixture_commitments = _array(
        manifest.get("fixtures"), label="manifest.fixtures"
    )
    fixtures: dict[str, Mapping[str, Any]] = {}
    fixture_paths: set[Path] = set()
    for index, raw_commitment in enumerate(fixture_commitments):
        commitment = _object(
            raw_commitment, label=f"manifest.fixtures[{index}]"
        )
        if set(commitment) != _FIXTURE_COMMITMENT_FIELDS:
            raise _error(
                "surface_manifest_invalid", "fixture commitment fields are invalid"
            )
        fixture_id = _identifier(
            commitment.get("fixture_id"), label="fixture_id"
        )
        if commitment.get("fixture_kind") not in {
            "unmodified_live_projection",
            "synthetic_mutation",
        }:
            raise _error(
                "surface_manifest_invalid", "fixture kind is invalid"
            )
        if fixture_id in fixtures:
            raise _error(
                "surface_manifest_duplicate_fixture_id", "fixture ID repeats"
            )
        fixture_path = _safe_relative_path(
            v2_root, commitment.get("file"), allowed_root=v2_root
        )
        if fixture_path in fixture_paths:
            raise _error(
                "surface_manifest_duplicate_fixture_path", "fixture path repeats"
            )
        fixture_paths.add(fixture_path)
        fixture_raw = _read_committed_file(
            fixture_path, commitment, label=f"fixture {fixture_id}"
        )
        fixture = _decode_json(fixture_raw, label=f"fixture {fixture_id}")
        if fixture.get("fixture_id") != fixture_id:
            raise _error(
                "surface_fixture_identity_mismatch",
                "fixture ID differs from manifest",
            )
        provenance = fixture.get("provenance")
        capture_boundary = fixture.get("capture_boundary")
        expected_top_fields = {
            "fixture_schema_version",
            "fixture_id",
            "provider_id",
            "api_surface",
            "scenario",
            "provenance",
            "response_projection",
            "capture_boundary",
        }
        if commitment["fixture_kind"] == "synthetic_mutation":
            expected_top_fields.add("derivation")
            expected_provenance_fields = {
                "source_tier",
                "fixture_kind",
                "unverified_shape",
                "live_validation",
                "source_receipt_sha256",
                "base_fixture_id",
            }
        else:
            expected_provenance_fields = {
                "source_tier",
                "fixture_kind",
                "unverified_shape",
                "source_receipt_sha256",
                "source_probe_label",
                "capture_scope",
            }
        expected_fixture = _EXPECTED_V2_FIXTURES.get(fixture_id)
        if (
            expected_fixture is None
            or commitment.get("fixture_kind")
            != expected_fixture.get("fixture_kind")
            or set(fixture) != expected_top_fields
            or fixture.get("fixture_schema_version")
            != "provider-completion-fixture/2.0"
            or fixture.get("provider_id") != "deepseek"
            or fixture.get("api_surface") != "responses"
            or fixture.get("scenario") != expected_fixture.get("scenario")
            or not isinstance(provenance, Mapping)
            or set(provenance) != expected_provenance_fields
            or provenance.get("source_tier") != "live_capture"
            or provenance.get("fixture_kind") != commitment["fixture_kind"]
            or not isinstance(fixture.get("response_projection"), Mapping)
            or set(fixture["response_projection"])
            - {
                "status",
                "incomplete_details",
                "usage",
                "provider_request_id_sha256",
                "http_status",
            }
            or not isinstance(capture_boundary, Mapping)
            or capture_boundary
            != {
                "evaluation_task": False,
                "run_ledger_written": False,
                "raw_response_persisted": False,
                "content_omitted": True,
                "api_key_persisted": False,
                "model_quality_claim_allowed": False,
            }
        ):
            raise _error(
                "surface_fixture_shape_invalid", "fixture metadata is invalid"
            )
        if commitment["fixture_kind"] == "synthetic_mutation":
            if (
                provenance.get("unverified_shape") is not True
                or provenance.get("live_validation")
                != "synthetic_mutation_from_validated_base"
                or provenance.get("base_fixture_id")
                != expected_fixture.get("base_fixture_id")
            ):
                raise _error(
                    "surface_fixture_shape_invalid",
                    "synthetic fixture provenance is invalid",
                )
        elif (
            provenance.get("unverified_shape") is not False
            or provenance.get("capture_scope")
            != "minimal_direct_probe_not_sdk_built"
            or provenance.get("source_probe_label")
            != expected_fixture.get("source_probe_label")
        ):
            raise _error(
                "surface_fixture_shape_invalid",
                "live fixture provenance is invalid",
            )
        _validate_response_projection(
            fixture.get("response_projection"),
            api_surface="responses",
            label=f"fixture {fixture_id}.response_projection",
        )
        _scan_sensitive_tree(fixture, label=f"fixture {fixture_id}")
        fixtures[fixture_id] = fixture

    summary = _object(manifest.get("summary"), label="manifest.summary")
    if (
        set(summary) != _SUMMARY_FIELDS
        or summary.get("fixture_count") != len(fixtures)
        or summary.get("live_capture_fixture_count") != 2
        or summary.get("synthetic_mutation_fixture_count") != 2
        or summary.get("online_calls_performed_for_this_successor") is not False
        or summary.get("adapter_changes_included") is not False
    ):
        raise _error(
            "surface_manifest_invalid", "manifest summary is invalid"
        )

    inline_entries = [
        entry
        for entry in entries.values()
        if entry["mapping_source"] == "inline_successor"
    ]
    expected_fixture_ids: set[str] = set()
    for entry in inline_entries:
        expectations = _object(
            entry.get("fixture_expectations"), label="fixture expectations"
        )
        if expected_fixture_ids & set(expectations):
            raise _error(
                "surface_registry_duplicate_fixture_id",
                "fixture expectation repeats",
            )
        expected_fixture_ids.update(expectations)
        source = _object(entry.get("source"), label="inline source")
        if source.get("kind") != "sanitized_live_probe_receipt":
            raise _error(
                "surface_registry_invalid", "inline source kind is invalid"
            )
        receipt_path = _safe_relative_path(
            root, source.get("relative_path"), allowed_root=root
        )
        receipt_raw = _read_committed_file(
            receipt_path, source, label="source probe receipt"
        )
        receipt = _decode_json(receipt_raw, label="source probe receipt")
        _validate_probe_receipt(receipt)
        transport = receipt.get("transport")
        limitations = receipt.get("limitations")
        probes = receipt.get("probes")
        if (
            receipt.get("probe_id") != source.get("probe_id")
            or receipt.get("status") != "completed"
            or not isinstance(transport, Mapping)
            or transport.get("base_url") != source.get("api_origin")
            or transport.get("model") != source.get("model")
            or transport.get("path") != source.get("path")
            or not isinstance(limitations, Mapping)
            or limitations.get("distinct_top_level_shape_count")
            != source.get("observed_distinct_shape_count")
            or not isinstance(probes, list)
        ):
            raise _error(
                "surface_source_receipt_mismatch", "probe receipt identity differs"
            )
        probe_by_label: dict[str, Mapping[str, Any]] = {}
        distinct_shapes: list[dict[str, Any]] = []
        distinct_keys: set[str] = set()
        for raw_probe in probes:
            probe = _object(raw_probe, label="source probe")
            label = _identifier(probe.get("probe_label"), label="probe label")
            if label in probe_by_label:
                raise _error(
                    "surface_source_receipt_mismatch", "probe label repeats"
                )
            projection = _object(
                probe.get("response_projection"), label="probe response projection"
            )
            shape = _native_shape(projection)
            shape_key = _canonical_key(shape)
            if shape_key not in distinct_keys:
                distinct_keys.add(shape_key)
                distinct_shapes.append(shape)
            probe_by_label[label] = probe
        if (
            len(distinct_shapes) != source.get("observed_distinct_shape_count")
            or distinct_shapes != source.get("observed_native_values")
        ):
            raise _error(
                "surface_source_receipt_mismatch",
                "source native-value summary differs from the probe",
            )

        for fixture in fixtures.values():
            provenance = _object(fixture.get("provenance"), label="fixture provenance")
            if provenance.get("fixture_kind") != "unmodified_live_projection":
                continue
            label = provenance.get("source_probe_label")
            probe = probe_by_label.get(str(label))
            if probe is None:
                raise _error(
                    "surface_fixture_live_projection_mismatch",
                    "live fixture references an unknown probe label",
                )
            expected_projection = copy.deepcopy(
                dict(
                    _object(
                        probe.get("response_projection"),
                        label="probe response projection",
                    )
                )
            )
            expected_projection["provider_request_id_sha256"] = probe.get(
                "provider_request_id_sha256"
            )
            expected_projection["http_status"] = probe.get("http_status")
            if (
                fixture.get("response_projection") != expected_projection
                or provenance.get("source_receipt_sha256") != source.get("sha256")
            ):
                raise _error(
                    "surface_fixture_live_projection_mismatch",
                    "live fixture differs from its probe projection",
                )
    if (
        set(fixtures) != expected_fixture_ids
        or set(fixtures) != set(_EXPECTED_V2_FIXTURES)
    ):
        raise _error(
            "surface_fixture_set_mismatch", "fixture set differs from registry"
        )

    for fixture in fixtures.values():
        provenance = _object(fixture.get("provenance"), label="fixture provenance")
        if provenance.get("fixture_kind") != "synthetic_mutation":
            continue
        derivation = _object(fixture.get("derivation"), label="fixture derivation")
        if provenance.get("base_fixture_id") != derivation.get("base_fixture_id"):
            raise _error(
                "surface_fixture_derivation_invalid",
                "provenance and derivation name different base fixtures",
            )
        base = _object(
            fixtures.get(str(derivation.get("base_fixture_id"))),
            label="synthetic base fixture",
        )
        base_provenance = _object(
            base.get("provenance"), label="synthetic base provenance"
        )
        if provenance.get("source_receipt_sha256") != base_provenance.get(
            "source_receipt_sha256"
        ):
            raise _error(
                "surface_fixture_derivation_invalid",
                "synthetic fixture source receipt differs from its live base",
            )
        replayed = _replay_synthetic_projection(fixture, fixtures)
        if replayed != fixture.get("response_projection"):
            raise _error(
                "surface_fixture_derivation_mismatch",
                "synthetic fixture differs from its declared mutation replay",
            )

    schema_path = (
        root
        / "evals/provider_completion_telemetry_v1/schemas/provider_completion_record_v1.schema.json"
    ).resolve()
    if not schema_path.is_relative_to(root):
        raise _error(
            "surface_artifact_path_invalid", "record schema path escapes repository"
        )
    try:
        schema_raw = schema_path.read_bytes()
    except OSError as exc:
        raise _error(
            "surface_artifact_read_failed", "record schema cannot be read"
        ) from exc
    schema = _decode_json(schema_raw, label="provider completion record schema")
    if (
        schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
        or schema.get("title") != "Provider completion telemetry persisted record v1"
    ):
        raise _error(
            "surface_record_schema_invalid", "record schema identity is invalid"
        )
    telemetry_schema_sha256 = hashlib.sha256(schema_raw).hexdigest()

    verified = VerifiedSurfaceRegistry._create(
        _VERIFIED_TOKEN,
        repository_root=root,
        registry=registry,
        predecessor=predecessor,
        fixtures=fixtures,
        entries=entries,
        provider_mappings=provider_mappings,
        telemetry_schema_sha256=telemetry_schema_sha256,
    )
    _validate_selected_fixture_expectations(verified)
    return verified


def _selection_mapping(
    verified: VerifiedSurfaceRegistry,
    *,
    key: tuple[str, str, str],
    entry: Mapping[str, Any],
    provider_mapping: Mapping[str, Any],
) -> dict[str, Any]:
    provider_id, api_surface, transport_id = key
    predecessor = _thaw_json(verified._predecessor)
    predecessor_commitment = verified._registry["predecessor_mapping"]
    selected = predecessor
    selected["schema_version"] = _SELECTION_SCHEMA_VERSION
    selected["status"] = "surface_selected_offline_zero_adapter_changes"
    selected["documented_on"] = "2026-09-03"
    selected["adapter_changes_included"] = False
    selected["providers"] = {
        provider_id: _thaw_json(provider_mapping)
    }
    selected["fixture_expectations"] = _thaw_json(
        entry.get("fixture_expectations", {})
    )
    selected["surface_selection"] = {
        "provider_id": provider_id,
        "api_surface": api_surface,
        "transport_id": transport_id,
        "adapter_version": entry["adapter_version"],
        "output_counter_comparability": entry[
            "output_counter_comparability"
        ],
        "output_counter_path": entry["output_counter_path"],
        "mapping_version": entry["mapping_version"],
        "mapping_source": entry["mapping_source"],
        "provenance_tier": entry["provenance_tier"],
        "unverified_shape": entry["unverified_shape"],
        "first_live_validation_required": entry[
            "first_live_validation_required"
        ],
        "provenance_promotion_allowed": entry[
            "provenance_promotion_allowed"
        ],
        "offline_selection_allowed": entry["offline_selection_allowed"],
        "runtime_binding_allowed": entry["runtime_binding_allowed"],
        "runtime_binding_blocker": entry["runtime_binding_blocker"],
        "predecessor_mapping_sha256": predecessor_commitment["sha256"],
        "predecessor_mapping_canonical_json_sha256": predecessor_commitment[
            "canonical_json_sha256"
        ],
    }

    rules = _collect_rules(provider_mapping)
    stage_counts = {
        stage: 0 for stage in selected.get("mapping_precedence", ())
    }
    rule_ids: set[str] = set()
    for rule in rules:
        stage = rule.get("precedence_stage")
        rule_id = rule.get("rule_id")
        if (
            stage not in stage_counts
            or not isinstance(rule_id, str)
            or rule_id in rule_ids
        ):
            raise _error(
                "surface_registry_invalid",
                "selected mapping rule metadata is invalid",
            )
        rule_ids.add(rule_id)
        stage_counts[stage] += 1
    stage_contract = copy.deepcopy(
        dict(
            _object(
                selected.get("precedence_stage_contract"),
                label="precedence stage contract",
            )
        )
    )
    for stage, count in stage_counts.items():
        policy = stage_contract.get(stage)
        if not isinstance(policy, dict):
            raise _error(
                "surface_registry_invalid", "precedence stage policy is invalid"
            )
        policy["materialized_rule_count"] = count
    selected["precedence_stage_contract"] = stage_contract
    selected["materialized_provider_rule_count"] = len(rule_ids)
    return selected


def _validate_selected_fixture_expectations(
    verified: VerifiedSurfaceRegistry,
) -> None:
    for key, entry in verified._entries.items():
        if entry.get("mapping_source") != "inline_successor":
            continue
        expectations = _object(
            entry.get("fixture_expectations"), label="fixture expectations"
        )
        selected = _selection_mapping(
            verified,
            key=key,
            entry=entry,
            provider_mapping=verified._provider_mappings[key],
        )
        for fixture_id, raw_expectation in expectations.items():
            fixture = _object(
                verified._fixtures.get(fixture_id), label="expected fixture"
            )
            expectation = _object(
                raw_expectation, label="fixture mapping expectation"
            )
            if set(expectation) != {
                "normalized_completion_state",
                "truncation_signal_source",
                "preserved_native_value",
                "matched_rule_id",
            }:
                raise _error(
                    "surface_fixture_expectation_invalid",
                    "fixture mapping expectation has invalid fields",
                )
            actual = map_completion(
                _object(
                    fixture.get("response_projection"),
                    label="fixture response projection",
                ),
                key[0],
                selected,
            )
            expected = (
                expectation["normalized_completion_state"],
                expectation["truncation_signal_source"],
                expectation["preserved_native_value"],
                expectation["matched_rule_id"],
            )
            if actual != expected:
                raise _error(
                    "surface_fixture_expectation_invalid",
                    f"fixture {fixture_id} differs from its mapping expectation",
                )


def select_surface_mapping(
    provider_id: str,
    api_surface: str,
    transport_id: str,
    verified_registry: VerifiedSurfaceRegistry,
    *,
    purpose: str,
) -> VerifiedSurfaceSelection:
    """Select one exact verified triple for explicit offline or runtime use."""

    if (
        type(verified_registry) is not VerifiedSurfaceRegistry
        or getattr(verified_registry, "_authority_token", None) is not _VERIFIED_TOKEN
    ):
        raise _error(
            "surface_registry_not_verified",
            "selection requires load_verified_surface_registry output",
        )
    provider = _identifier(provider_id, label="provider_id")
    surface = _identifier(api_surface, label="api_surface")
    transport = _identifier(transport_id, label="transport_id")
    entries = verified_registry._entries
    providers = {key[0] for key in entries}
    provider_surfaces = {(key[0], key[1]) for key in entries}
    if provider not in providers:
        raise _error(
            "surface_mapping_provider_unknown", "provider is not registered"
        )
    if (provider, surface) not in provider_surfaces:
        raise _error(
            "surface_mapping_surface_mismatch", "API surface is not registered"
        )
    key = (provider, surface, transport)
    entry = entries.get(key)
    if entry is None:
        raise _error(
            "surface_mapping_transport_mismatch", "transport is not registered"
        )
    if purpose == "offline_validation":
        allowed = entry["offline_selection_allowed"]
    elif purpose == "runtime_binding":
        allowed = entry["runtime_binding_allowed"]
    else:
        raise _error(
            "surface_mapping_purpose_invalid", "selection purpose is invalid"
        )
    if allowed is not True:
        raise _error(
            "surface_mapping_runtime_binding_blocked",
            str(entry["runtime_binding_blocker"]),
        )
    mapping = _selection_mapping(
        verified_registry,
        key=key,
        entry=entry,
        provider_mapping=verified_registry._provider_mappings[key],
    )
    return VerifiedSurfaceSelection._create(
        _SELECTION_TOKEN,
        purpose=purpose,
        telemetry_schema_sha256=verified_registry._telemetry_schema_sha256,
        mapping=mapping,
        entry=entry,
    )


def load_and_select_surface_mapping(
    repository_root: str | Path,
    provider_id: str,
    api_surface: str,
    transport_id: str,
    *,
    purpose: str,
) -> VerifiedSurfaceSelection:
    verified = load_verified_surface_registry(repository_root)
    return select_surface_mapping(
        provider_id,
        api_surface,
        transport_id,
        verified,
        purpose=purpose,
    )


def create_runtime_completion_binding(
    selection: VerifiedSurfaceSelection,
) -> VerifiedRuntimeCompletionBinding:
    if (
        type(selection) is not VerifiedSurfaceSelection
        or selection.purpose != "runtime_binding"
    ):
        raise _error(
            "surface_runtime_authority_missing",
            "runtime binding requires a verified runtime selection",
        )
    return selection.create_runtime_binding()


def _create_first_live_validation_binding(
    selection: VerifiedSurfaceSelection,
) -> VerifiedRuntimeCompletionBinding:
    """Private bootstrap used only after a one-shot validation grant is consumed."""

    if (
        type(selection) is not VerifiedSurfaceSelection
        or selection.purpose != "first_live_validation"
    ):
        raise _error(
            "surface_first_live_authority_missing",
            "first-live binding requires a verified validation-only selection",
        )
    return selection.create_runtime_binding()


__all__ = [
    "SurfaceMappingError",
    "VerifiedRuntimeCompletionBinding",
    "VerifiedSurfaceSelection",
    "VerifiedSurfaceRegistry",
    "create_runtime_completion_binding",
    "load_and_select_surface_mapping",
    "load_verified_surface_registry",
    "select_surface_mapping",
]
