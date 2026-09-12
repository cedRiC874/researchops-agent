from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from jsonschema import Draft202012Validator


DESIGN_CONTRACT_RELATIVE_PATH = PurePosixPath(
    "evals/provider_completion_external_preregistration_v1/"
    "external_preregistration_design_contract_v1.json"
)
DESIGN_CONTRACT_BYTES = 65_751
DESIGN_CONTRACT_FILE_SHA256 = (
    "3b16e6d65ec7030fb1f271b57cbde369a55fbf4f966ad57cb73e591d5333d61d"
)
DESIGN_CONTRACT_COMMITMENT_SHA256 = (
    "480a8511d35d295d8a99ba7561a19f742b189094dec589bbd8317899c1f833c8"
)
DESIGN_CONTRACT_COMMITMENT_DOMAIN = (
    "researchops-provider-completion-external-preregistration-design-v1"
)

SUPPORTING_CONTRACT_RELATIVE_PATH = PurePosixPath(
    "evals/provider_completion_external_preregistration_v1/"
    "closure_evidence_contract_v1.json"
)
SUPPORTING_CONTRACT_BYTES = 39_575
SUPPORTING_CONTRACT_FILE_SHA256 = (
    "2c904737b2776ac1df2358df86e320e6421c6eed17b48ab97d49f7512d37e439"
)
SUPPORTING_CONTRACT_COMMITMENT_SHA256 = (
    "a96b39a1f4bafa3bcf512fd88a5e7b0ca3987ca2bc9804642a1d1259cbf8cad0"
)
SUPPORTING_CONTRACT_COMMITMENT_DOMAIN = (
    "researchops-provider-completion-closure-evidence-contract-v1"
)

SCHEMA_ROOT_RELATIVE_PATH = PurePosixPath(
    "evals/provider_completion_external_preregistration_v1/schemas"
)
MAX_CONTRACT_FILE_BYTES = 2_000_000
EXPECTED_SCHEMA_COUNT = 14

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_DIRECT_SCHEMA_FILES = MappingProxyType(
    {
        "authorization_grant": "authorization_grant_v1.schema.json",
        "candidate_freeze_receipt": "candidate_freeze_receipt_v1.schema.json",
        "closure_receipt": "closure_receipt_v1.schema.json",
        "consumption_receipt": "consumption_receipt_v1.schema.json",
        "external_ledger_entry": "external_ledger_entry_v1.schema.json",
        "external_preregistration_envelope": (
            "external_preregistration_envelope_v1.schema.json"
        ),
        "external_trust_manifest": "external_trust_manifest_v1.schema.json",
        "seen_task_exclusion_manifest": (
            "seen_task_exclusion_manifest_v1.schema.json"
        ),
        "unseen_case_bundle": "unseen_case_bundle_v1.schema.json",
    }
)

_SUPPORTING_SCHEMA_PATHS = MappingProxyType(
    {
        "manifest_schema": "closure_bundle_manifest_v1.schema.json",
        "transport_send_payload_schema": "transport_send_payload_v1.schema.json",
        "non_completion_event_projection_schema": (
            "closure_run_event_projection_v1.schema.json"
        ),
        "postrun_attested_facts_schema": "postrun_attested_facts_v1.schema.json",
        "external_observation_bundle_schema": (
            "external_observation_bundle_v1.schema.json"
        ),
    }
)

_PREDECESSOR_PATHS = MappingProxyType(
    {
        "first_live_design": PurePosixPath(
            "evals/provider_completion_first_live_validation_v1/"
            "deepseek_responses_adapter_validation_contract_v1.json"
        ),
        "first_live_implementation": PurePosixPath(
            "evals/provider_completion_first_live_validation_v1/"
            "deepseek_responses_adapter_validation_implementation_v2.json"
        ),
        "depth60_source_integrity_v5": PurePosixPath(
            "evals/phase6_deepseek_depth60_plan_v5.json"
        ),
        "runtime_hardening_v2": PurePosixPath(
            "evals/provider_completion_telemetry_v1/"
            "provider_completion_runtime_hardening_contract_v2.json"
        ),
        "surface_registry_v2": PurePosixPath(
            "evals/provider_completion_telemetry_v2/"
            "provider_completion_surface_registry_v2.json"
        ),
    }
)

_SUPPORTING_PREDECESSOR_PATHS = MappingProxyType(
    {
        "record_contract": PurePosixPath(
            "evals/provider_completion_telemetry_v1/"
            "provider_completion_record_contract_v1.json"
        ),
        "runtime_hardening": PurePosixPath(
            "evals/provider_completion_telemetry_v1/"
            "provider_completion_runtime_hardening_contract_v2.json"
        ),
    }
)

EXPECTED_CLOSURE_REASON_ORDER = (
    "planned_cases_not_finalized",
    "no_model_attempts_observed",
    "no_provider_responses_observed",
    "local_terminal_outcome_not_succeeded",
    "task_not_released",
    "provider_key_not_loaded",
    "task_bundle_opening_unverified",
    "leak_canary_injection_unverified",
    "raw_response_cleanup_incomplete",
    "post_request_cleanup_incomplete",
    "provider_key_reference_not_released",
    "planned_case_without_accepted_response",
    "response_telemetry_rejected",
    "http_error_response_observed",
    "request_failed_without_response",
    "model_request_cancelled",
    "model_request_outcome_unknown",
    "sdk_raw_response_count_mismatched",
    "sdk_raw_response_count_unavailable",
    "sdk_usage_request_count_mismatched",
    "sdk_usage_request_count_unavailable",
    "completion_state_projection_mismatch",
    "completion_record_denominator_mismatch",
    "completion_state_unmapped",
    "completion_state_not_provided",
    "completion_state_not_persisted",
    "completion_state_unrecognized",
    "truncation_signal_token_cap_fallback",
    "truncation_signal_none",
    "truncation_signal_unrecognized",
    "audit_chain_or_export_invalid",
    "completion_telemetry_ledger_write_failed",
    "model_request_started_count_mismatch",
    "model_request_terminal_count_mismatch",
    "transport_send_count_mismatch",
    "transport_retry_or_cap_violation",
    "accepted_response_event_count_mismatch",
    "rejected_response_event_count_mismatch",
    "legacy_response_usage_event_present",
    "ledger_event_commitment_missing",
    "ledger_event_commitment_mismatch",
    "ledger_event_payload_mismatch",
    "record_usage_incomplete",
    "record_usage_index_coverage_mismatch",
    "observed_input_token_cap_exceeded",
    "observed_output_token_cap_exceeded",
    "observed_cost_stop_exceeded",
)

EXPECTED_CLOSURE_RECEIPT_UNSIGNED_FIELDS = (
    "schema_version",
    "document_type",
    "receipt_id",
    "campaign_id",
    "completed_at_utc",
    "receipt_issued_at_utc",
    "bundle_status",
    "artifact_publication_disposition",
    "terminal_stage",
    "local_terminal_outcome",
    "terminal_provider_outcome_state",
    "terminal_error_code",
    "last_completed_artifact_write_stage",
    "artifact_error_code",
    "preregistration_envelope_sha256",
    "preregistration_freeze_entry_sha256",
    "authorization_grant_sha256",
    "consumption_receipt_sha256",
    "authorization_consumption_entry_sha256",
    "task_released",
    "task_released_at_utc",
    "provider_key_loaded",
    "provider_key_loaded_at_utc",
    "released_task_bundle_commitment_sha256",
    "released_case_handle_order_commitment_sha256",
    "task_bundle_opening_verified_in_memory",
    "task_bundle_opening_persisted",
    "custodian_private_leak_canary_scan_status",
    "public_generic_privacy_scan_status",
    "canary_injection_verified_for_every_send",
    "database_origin_status",
    "database_mutation_status",
    "released_execution_input_order_commitment_sha256",
    "closure_bundle_manifest_sha256",
    "partial_artifacts",
    "observed_audit_run_set_commitment_sha256",
    "ordered_final_chain_heads_commitment_sha256",
    "planned_case_count",
    "attempt_count",
    "accepted_response_count",
    "network_attempt_count_observed",
    "model_request_count_observed",
    "usage_complete",
    "observed_input_tokens",
    "observed_output_tokens",
    "observed_cost_cny",
    "evidence_valid",
    "evidence_error_code",
    "pre_anchor_closure_eligible",
    "closure_reasons",
    "closure_reasons_commitment_sha256",
    "task_content_included_in_receipt",
    "case_ids_included_in_receipt",
    "records_included_in_receipt",
    "provider_content_included_in_receipt",
    "raw_response_cleanup_status",
    "post_request_cleanup_status",
    "provider_key_reference_release_status",
)

EXPECTED_FINAL_VERIFIER_ERROR_CODES = (
    "closure_receipt_schema_invalid",
    "closure_receipt_signature_invalid",
    "closure_receipt_projection_mismatch",
    "closure_external_anchor_mismatch",
)


class ExternalPreregistrationContractError(RuntimeError):
    """Stable, payload-free error raised by the frozen contract loader."""

    def __init__(self, code: str) -> None:
        super().__init__(
            "Provider completion external preregistration contract is invalid."
        )
        self.code = code


def _error(code: str) -> ExternalPreregistrationContractError:
    return ExternalPreregistrationContractError(code)


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_repository_path(root: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or any(
        part in {"", ".", ".."} or ":" in part or "\\" in part
        for part in relative.parts
    ):
        raise _error("external_contract_path_invalid")
    current = root
    for part in relative.parts:
        current = current / part
        if _is_link_like(current):
            raise _error("external_contract_path_invalid")
    try:
        resolved = current.resolve(strict=True)
    except OSError:
        raise _error("external_contract_file_invalid") from None
    if not resolved.is_relative_to(root):
        raise _error("external_contract_path_invalid")
    return resolved


def _read_regular_file_no_follow(
    path: Path,
    *,
    maximum_bytes: int = MAX_CONTRACT_FILE_BYTES,
) -> bytes:
    try:
        before = path.lstat()
        if (
            _is_link_like(path)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size < 1
            or before.st_size > maximum_bytes
            or before.st_nlink != 1
        ):
            raise _error("external_contract_file_invalid")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size != before.st_size
                or opened.st_nlink != 1
            ):
                raise _error("external_contract_file_identity_changed")
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        finally:
            os.close(descriptor)
        after = path.lstat()
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
            or len(payload) != before.st_size
            or len(payload) > maximum_bytes
        ):
            raise _error("external_contract_file_identity_changed")
        return payload
    except ExternalPreregistrationContractError:
        raise
    except OSError:
        raise _error("external_contract_file_invalid") from None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _error("external_contract_json_duplicate_key")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> None:
    raise _error("external_contract_json_nonfinite")


def _parse_json_object(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except ExternalPreregistrationContractError:
        raise
    except (UnicodeError, json.JSONDecodeError):
        raise _error("external_contract_json_invalid") from None
    if not isinstance(value, dict):
        raise _error("external_contract_json_shape_invalid")
    return value


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise _error("external_contract_canonicalization_invalid") from None


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_fixed_json(
    root: Path,
    relative: PurePosixPath,
    *,
    expected_bytes: int,
    expected_sha256: str,
) -> tuple[dict[str, Any], bytes]:
    path = _safe_repository_path(root, relative)
    payload = _read_regular_file_no_follow(path)
    if len(payload) != expected_bytes or _sha256(payload) != expected_sha256:
        raise _error("external_contract_file_commitment_mismatch")
    return _parse_json_object(payload), payload


def _verify_semantic_commitment(
    document: Mapping[str, Any],
    *,
    expected_domain: str,
    expected_sha256: str,
) -> None:
    body = copy.deepcopy(document)
    commitment = body.get("contract_commitment")
    if not isinstance(commitment, dict) or set(commitment) != {
        "algorithm",
        "domain",
        "payload",
        "sha256",
    }:
        raise _error("external_contract_semantic_commitment_invalid")
    declared = commitment.pop("sha256")
    if (
        commitment != {
            "algorithm": "sha256-domain-separated-canonical-json-v1",
            "domain": expected_domain,
            "payload": "whole_contract_with_contract_commitment.sha256_omitted",
        }
        or declared != expected_sha256
        or _SHA256.fullmatch(str(declared)) is None
    ):
        raise _error("external_contract_semantic_commitment_invalid")
    actual = _sha256(
        expected_domain.encode("ascii") + b"\0" + _canonical_json_bytes(body)
    )
    if actual != expected_sha256:
        raise _error("external_contract_semantic_commitment_mismatch")


def _binding_path(binding: Mapping[str, Any]) -> PurePosixPath:
    value = binding.get("relative_path")
    if not isinstance(value, str) or "\\" in value:
        raise _error("external_contract_binding_invalid")
    relative = PurePosixPath(value)
    if type(binding.get("bytes")) is not int or _SHA256.fullmatch(
        str(binding.get("file_sha256"))
    ) is None:
        raise _error("external_contract_binding_invalid")
    return relative


def _verify_bound_file(
    root: Path,
    binding: Mapping[str, Any],
    *,
    expected_relative: PurePosixPath,
) -> dict[str, Any]:
    relative = _binding_path(binding)
    if relative != expected_relative:
        raise _error("external_contract_binding_invalid")
    payload = _read_regular_file_no_follow(_safe_repository_path(root, relative))
    if len(payload) != binding["bytes"] or _sha256(payload) != binding["file_sha256"]:
        raise _error("external_contract_binding_mismatch")
    return _parse_json_object(payload)


def _supporting_schema_bindings(
    supporting: Mapping[str, Any],
) -> Mapping[str, Mapping[str, Any]]:
    try:
        return {
            "manifest_schema": supporting["manifest_schema"],
            "transport_send_payload_schema": supporting["completion_event_contract"][
                "transport_send_payload_schema"
            ],
            "non_completion_event_projection_schema": supporting[
                "completion_event_contract"
            ]["non_completion_event_projection_schema"],
            "postrun_attested_facts_schema": supporting["postrun_facts_contract"][
                "schema"
            ],
            "external_observation_bundle_schema": supporting[
                "offline_verifier_boundary"
            ]["external_observation_bundle_schema"],
        }
    except (KeyError, TypeError):
        raise _error("external_contract_supporting_shape_invalid") from None


def _verify_schemas(
    root: Path,
    design: Mapping[str, Any],
    supporting: Mapping[str, Any],
) -> Mapping[str, Mapping[str, Any]]:
    direct = design.get("schema_bindings")
    if not isinstance(direct, dict) or set(direct) != {
        *_DIRECT_SCHEMA_FILES,
        "json_schema_draft",
        "additional_properties_allowed",
    }:
        raise _error("external_contract_schema_binding_invalid")
    if (
        direct.get("json_schema_draft") != "2020-12"
        or direct.get("additional_properties_allowed") is not False
    ):
        raise _error("external_contract_schema_binding_invalid")
    supporting_bindings = _supporting_schema_bindings(supporting)
    documents: dict[str, Mapping[str, Any]] = {}
    observed_names: set[str] = set()
    for name, filename in _DIRECT_SCHEMA_FILES.items():
        binding = direct.get(name)
        if not isinstance(binding, Mapping):
            raise _error("external_contract_schema_binding_invalid")
        schema = _verify_bound_file(
            root,
            binding,
            expected_relative=SCHEMA_ROOT_RELATIVE_PATH / filename,
        )
        documents[filename] = schema
        observed_names.add(filename)
    for name, filename in _SUPPORTING_SCHEMA_PATHS.items():
        binding = supporting_bindings.get(name)
        if not isinstance(binding, Mapping):
            raise _error("external_contract_schema_binding_invalid")
        schema = _verify_bound_file(
            root,
            binding,
            expected_relative=SCHEMA_ROOT_RELATIVE_PATH / filename,
        )
        if filename in observed_names:
            raise _error("external_contract_schema_binding_overlap")
        documents[filename] = schema
        observed_names.add(filename)
    expected_names = set(_DIRECT_SCHEMA_FILES.values()) | set(
        _SUPPORTING_SCHEMA_PATHS.values()
    )
    schema_root = _safe_repository_path(root, SCHEMA_ROOT_RELATIVE_PATH)
    if not schema_root.is_dir() or _is_link_like(schema_root):
        raise _error("external_contract_schema_root_invalid")
    actual_names: set[str] = set()
    try:
        # Stream the directory: reject an extra/duplicate name before inspecting
        # it, rather than materializing an unbounded directory with Path.iterdir.
        with os.scandir(schema_root) as entries:
            for entry in entries:
                name = entry.name
                if (
                    len(actual_names) >= EXPECTED_SCHEMA_COUNT
                    or name not in expected_names
                    or name in actual_names
                ):
                    raise _error("external_contract_schema_file_set_invalid")
                path = schema_root / name
                if _is_link_like(path) or not path.is_file() or not name.endswith(
                    ".schema.json"
                ):
                    raise _error("external_contract_schema_file_set_invalid")
                actual_names.add(name)
    except OSError:
        raise _error("external_contract_schema_file_set_invalid") from None
    if (
        len(observed_names) != EXPECTED_SCHEMA_COUNT
        or observed_names != expected_names
        or actual_names != expected_names
    ):
        raise _error("external_contract_schema_file_set_invalid")
    for schema in documents.values():
        try:
            Draft202012Validator.check_schema(schema)
        except Exception:
            raise _error("external_contract_schema_meta_invalid") from None
    return MappingProxyType(documents)


def _verify_predecessors(
    root: Path,
    design: Mapping[str, Any],
    supporting: Mapping[str, Any],
) -> None:
    predecessors = design.get("predecessors")
    if not isinstance(predecessors, dict) or set(predecessors) != set(
        _PREDECESSOR_PATHS
    ):
        raise _error("external_contract_predecessor_invalid")
    documents: dict[str, Mapping[str, Any]] = {}
    for name, expected_relative in _PREDECESSOR_PATHS.items():
        binding = predecessors.get(name)
        if not isinstance(binding, Mapping) or binding.get("immutable") is not True:
            raise _error("external_contract_predecessor_invalid")
        documents[name] = _verify_bound_file(
            root,
            binding,
            expected_relative=expected_relative,
        )
    semantic_fields = {
        "first_live_design": ("contract_commitment",),
        "first_live_implementation": ("implementation_commitment",),
        "depth60_source_integrity_v5": ("plan_commitment_sha256",),
    }
    for name, path in semantic_fields.items():
        declared = predecessors[name].get("semantic_commitment_sha256")
        document: Any = documents[name]
        for part in path:
            document = document.get(part) if isinstance(document, Mapping) else None
        if isinstance(document, Mapping):
            document = document.get("sha256")
        if declared != document or _SHA256.fullmatch(str(declared)) is None:
            raise _error("external_contract_predecessor_commitment_mismatch")

    supporting_predecessors = supporting.get("predecessors")
    if not isinstance(supporting_predecessors, dict) or set(
        supporting_predecessors
    ) != set(_SUPPORTING_PREDECESSOR_PATHS):
        raise _error("external_contract_predecessor_invalid")
    for name, expected_relative in _SUPPORTING_PREDECESSOR_PATHS.items():
        binding = supporting_predecessors.get(name)
        if not isinstance(binding, Mapping):
            raise _error("external_contract_predecessor_invalid")
        _verify_bound_file(
            root,
            binding,
            expected_relative=expected_relative,
        )


def _verify_exact_contract_lists(
    design: Mapping[str, Any],
    supporting: Mapping[str, Any],
    schemas: Mapping[str, Mapping[str, Any]],
) -> None:
    try:
        design_closure = design["closure_evidence_contract"]
        support_closure = supporting["closure_contract"]
        support_boundary = supporting["offline_verifier_boundary"]
        receipt_schema = schemas["closure_receipt_v1.schema.json"]
        defs_key = next(key for key in receipt_schema if key.endswith("defs"))
        schema_reasons = receipt_schema[defs_key]["closure_reason"]["enum"]
        unsigned_required = tuple(
            field
            for field in receipt_schema["required"]
            if field not in {"document_sha256", "signatures"}
        )
    except (KeyError, StopIteration, TypeError):
        raise _error("external_contract_exact_list_invalid") from None
    if (
        tuple(support_closure.get("reason_order", ()))
        != EXPECTED_CLOSURE_REASON_ORDER
        or tuple(schema_reasons) != EXPECTED_CLOSURE_REASON_ORDER
        or tuple(support_closure.get("closure_receipt_unsigned_body_exact_fields", ()))
        != EXPECTED_CLOSURE_RECEIPT_UNSIGNED_FIELDS
        or tuple(design_closure.get("closure_receipt_unsigned_body_exact_fields", ()))
        != EXPECTED_CLOSURE_RECEIPT_UNSIGNED_FIELDS
        or unsigned_required != EXPECTED_CLOSURE_RECEIPT_UNSIGNED_FIELDS
        or tuple(support_boundary.get("final_verifier_error_codes", ()))
        != EXPECTED_FINAL_VERIFIER_ERROR_CODES
        or tuple(design_closure.get("final_verifier_error_codes", ()))
        != EXPECTED_FINAL_VERIFIER_ERROR_CODES
    ):
        raise _error("external_contract_exact_list_invalid")


def _verify_frozen_boundaries(
    design: Mapping[str, Any], supporting: Mapping[str, Any]
) -> None:
    try:
        valid = (
            design.get("schema_version")
            == "provider-completion-external-preregistration-design/1.0"
            and design.get("contract_id")
            == "provider-completion-external-preregistration-design-v1"
            and design.get("status") == "design_only_not_implemented_not_authorized"
            and design["implementation_state"]["field_contract_frozen"] is True
            and design["authorization_boundary"]["online_execution_authorized"]
            is False
            and design["authorization_boundary"]["provider_key_use_authorized"]
            is False
            and design["scope"]["online_calls_performed"] == 0
            and design["scope"]["provider_key_loaded"] is False
            and design["scope"]["runtime_authority_granted"] is False
            and supporting.get("schema_version")
            == "provider-completion-closure-evidence-contract/1.0"
            and supporting.get("contract_id")
            == "provider-completion-closure-evidence-v1"
            and supporting.get("status")
            == "design_only_not_implemented_not_authorized"
            and supporting["implementation_state"]["semantic_contract_frozen"]
            is True
            and supporting["implementation_state"]["online_calls_performed"] == 0
            and supporting["implementation_state"]["provider_key_loaded"] is False
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise _error("external_contract_frozen_boundary_invalid")


def _freeze(value: Any) -> Any:
    """Recursively freeze a JSON-compatible value.

    A shallow mapping proxy is insufficient for schemas: callers could otherwise
    mutate a nested ``properties`` mapping or ``required`` array after the frozen
    files had been verified.  Tuples remain consumable by jsonschema's iterable
    keyword validators while preventing list mutation.
    """

    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def load_frozen_external_preregistration_contract(
    project_root: str | Path,
) -> Mapping[str, Any]:
    """Validate the frozen T6-C design graph and return a read-only summary.

    The loader performs no network, Provider, database, runtime-binding, or
    filesystem write operation. It validates only repository-public frozen
    contracts, schemas, and predecessor commitments.
    """

    try:
        lexical_root = Path(project_root).absolute()
        if _is_link_like(lexical_root):
            raise _error("external_contract_project_root_invalid")
        root = lexical_root.resolve(strict=True)
    except ExternalPreregistrationContractError:
        raise
    except OSError:
        raise _error("external_contract_project_root_invalid") from None
    if not root.is_dir():
        raise _error("external_contract_project_root_invalid")

    design, _ = _load_fixed_json(
        root,
        DESIGN_CONTRACT_RELATIVE_PATH,
        expected_bytes=DESIGN_CONTRACT_BYTES,
        expected_sha256=DESIGN_CONTRACT_FILE_SHA256,
    )
    supporting, supporting_payload = _load_fixed_json(
        root,
        SUPPORTING_CONTRACT_RELATIVE_PATH,
        expected_bytes=SUPPORTING_CONTRACT_BYTES,
        expected_sha256=SUPPORTING_CONTRACT_FILE_SHA256,
    )
    _verify_semantic_commitment(
        design,
        expected_domain=DESIGN_CONTRACT_COMMITMENT_DOMAIN,
        expected_sha256=DESIGN_CONTRACT_COMMITMENT_SHA256,
    )
    _verify_semantic_commitment(
        supporting,
        expected_domain=SUPPORTING_CONTRACT_COMMITMENT_DOMAIN,
        expected_sha256=SUPPORTING_CONTRACT_COMMITMENT_SHA256,
    )
    supporting_contracts = design.get("supporting_contracts")
    if not isinstance(supporting_contracts, dict) or set(supporting_contracts) != {
        "closure_evidence"
    }:
        raise _error("external_contract_supporting_binding_invalid")
    supporting_binding = supporting_contracts["closure_evidence"]
    if (
        not isinstance(supporting_binding, Mapping)
        or _binding_path(supporting_binding) != SUPPORTING_CONTRACT_RELATIVE_PATH
        or supporting_binding.get("bytes") != len(supporting_payload)
        or supporting_binding.get("file_sha256") != _sha256(supporting_payload)
        or supporting_binding.get("semantic_commitment_sha256")
        != SUPPORTING_CONTRACT_COMMITMENT_SHA256
        or supporting_binding.get("immutable_after_freeze") is not True
    ):
        raise _error("external_contract_supporting_binding_invalid")

    schemas = _verify_schemas(root, design, supporting)
    _verify_predecessors(root, design, supporting)
    _verify_exact_contract_lists(design, supporting, schemas)
    _verify_frozen_boundaries(design, supporting)

    return _freeze(
        {
            "status": "valid_frozen_external_preregistration_design",
            "contract_id": design["contract_id"],
            "design_contract": _freeze(
                {
                    "bytes": DESIGN_CONTRACT_BYTES,
                    "file_sha256": DESIGN_CONTRACT_FILE_SHA256,
                    "semantic_commitment_sha256": (
                        DESIGN_CONTRACT_COMMITMENT_SHA256
                    ),
                }
            ),
            "supporting_contract": _freeze(
                {
                    "bytes": SUPPORTING_CONTRACT_BYTES,
                    "file_sha256": SUPPORTING_CONTRACT_FILE_SHA256,
                    "semantic_commitment_sha256": (
                        SUPPORTING_CONTRACT_COMMITMENT_SHA256
                    ),
                }
            ),
            "schema_count": EXPECTED_SCHEMA_COUNT,
            "schema_files": tuple(sorted(schemas)),
            "schemas": _freeze(schemas),
            "predecessor_count": len(_PREDECESSOR_PATHS),
            "supporting_predecessor_count": len(_SUPPORTING_PREDECESSOR_PATHS),
            "field_contract_frozen": True,
            "semantic_contract_frozen": True,
            "online_execution_authorized": False,
            "network_calls": 0,
            "model_calls": 0,
            "provider_key_loaded": False,
        }
    )


__all__ = [
    "DESIGN_CONTRACT_COMMITMENT_SHA256",
    "DESIGN_CONTRACT_FILE_SHA256",
    "EXPECTED_CLOSURE_REASON_ORDER",
    "EXPECTED_CLOSURE_RECEIPT_UNSIGNED_FIELDS",
    "EXPECTED_FINAL_VERIFIER_ERROR_CODES",
    "ExternalPreregistrationContractError",
    "SUPPORTING_CONTRACT_COMMITMENT_SHA256",
    "SUPPORTING_CONTRACT_FILE_SHA256",
    "load_frozen_external_preregistration_contract",
]
