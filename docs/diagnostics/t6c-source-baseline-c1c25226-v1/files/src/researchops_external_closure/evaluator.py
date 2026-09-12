"""Deterministic layer-A evaluation of externally attested closure evidence.

The public entrypoint consumes only repository-public frozen contracts, caller
supplied immutable document bytes, an optional public artifact directory, and
strict operational post-run facts.  It has no signer, clock, Provider, network,
task-opening, key-loading, or runtime-authority capability.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, NoReturn

import jsonschema

from .artifact_inputs import extract_artifact_evaluation_inputs
from .artifacts import ArtifactBundleSummary, VerifiedPostrunFacts, validate_postrun_attested_facts
from .documents import VerifiedPreReceiptDocuments, verify_pre_receipt_documents
from .errors import ExternalClosurePrimitiveError
from .io import (
    decode_canonical_json_file,
    read_exact_artifact_directory,
    read_regular_file_no_follow,
    scan_public_artifact_bytes,
)
from .primitives import (
    canonical_json_bytes,
    compute_document_sha256,
    decode_strict_json_object,
    parse_utc_timestamp,
)
from .schema_guard import LOCAL_ONLY_SCHEMA_REGISTRY, only_local_schema_references
from .types import (
    PreReceiptDocumentBytes,
    PreReceiptRejected,
    PreReceiptResult,
    ReceiptProjectionReady,
)


_MAX_DOCUMENT_BYTES = 2_000_000
_MAX_ARTIFACT_FILE_BYTES = 100_000_000
_MAX_ARTIFACT_TOTAL_BYTES = 100_000_000
_REASON_COMMITMENT_DOMAIN = "researchops-provider-completion-closure-receipt-v1"
_CLOSURE_EVIDENCE_COMMITMENT_SHA256 = (
    "a96b39a1f4bafa3bcf512fd88a5e7b0ca3987ca2bc9804642a1d1259cbf8cad0"
)
_AUDIT_SCHEMA_COMMITMENT_SHA256 = (
    "c0df1f54a96fe3c4f5ee196e654203a62e5e775a3e21fd5f47ace9e349169385"
)
_WRITER_ORDER = (
    "phase6_audit.sqlite3",
    "phase6_audit_index.json",
    "phase6_completion_telemetry.json",
    "completion_closure_manifest.json",
)
_EXPECTED_NAMES_BY_STAGE = {
    "not_started": (),
    "audit_database_written": _WRITER_ORDER[:1],
    "audit_index_written": _WRITER_ORDER[:2],
    "completion_telemetry_written": _WRITER_ORDER[:3],
    "manifest_written": _WRITER_ORDER,
}
_EVIDENCE_ERROR_PRIORITY = (
    "closure_sensitive_content_detected",
    "closure_sensitive_scan_unavailable",
    "closure_database_origin_untrusted",
    "closure_bundle_manifest_invalid",
    "closure_bundle_hash_mismatch",
    "closure_audit_schema_invalid",
    "closure_audit_chain_invalid",
    "closure_audit_run_set_invalid",
    "closure_event_projection_invalid",
    "closure_denominator_invalid",
    "closure_outer_projection_invalid",
    "closure_budget_invalid",
    "closure_timeline_invalid",
)
_CLOSURE_REASON_ORDER = (
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
_UNSIGNED_RECEIPT_FIELDS = (
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


@dataclass(frozen=True, slots=True)
class _ArtifactProjection:
    bundle_status: str
    publication_disposition: str
    public_scan_status: str
    manifest_sha256: str | None
    partial_artifacts: dict[str, dict[str, object]]


def _reject(code: str) -> PreReceiptRejected:
    return PreReceiptRejected(code)


def _raise(code: str) -> NoReturn:
    raise ExternalClosurePrimitiveError(code)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _thaw_json(value: object) -> object:
    """Make a private JSON-compatible copy of the loader's recursive freeze."""

    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _load_schemas(project_root: Path) -> Mapping[str, Mapping[str, Any]]:
    try:
        from researchops.provider_completion_external_contract import (
            ExternalPreregistrationContractError,
            EXPECTED_CLOSURE_REASON_ORDER,
            EXPECTED_CLOSURE_RECEIPT_UNSIGNED_FIELDS,
            load_frozen_external_preregistration_contract,
        )

        try:
            contract = load_frozen_external_preregistration_contract(project_root)
        except ExternalPreregistrationContractError:
            _raise("external_closure_frozen_contract_invalid")
        if (
            tuple(EXPECTED_CLOSURE_REASON_ORDER) != _CLOSURE_REASON_ORDER
            or tuple(EXPECTED_CLOSURE_RECEIPT_UNSIGNED_FIELDS)
            != _UNSIGNED_RECEIPT_FIELDS
        ):
            _raise("external_closure_frozen_contract_invalid")
        schemas = contract.get("schemas")
        if not isinstance(schemas, Mapping):
            _raise("external_closure_frozen_contract_invalid")
        thawed = _thaw_json(schemas)
        if not isinstance(thawed, dict) or any(
            type(name) is not str or not isinstance(schema, dict)
            for name, schema in thawed.items()
        ):
            _raise("external_closure_frozen_contract_invalid")
        return thawed
    except ExternalClosurePrimitiveError:
        raise
    except Exception:
        _raise("external_closure_frozen_contract_invalid")


def _decode_verified(payload: bytes) -> dict[str, Any]:
    return decode_strict_json_object(payload, max_bytes=_MAX_DOCUMENT_BYTES)


def _nested_mapping(value: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    current: object = value
    for part in path:
        if not isinstance(current, Mapping):
            _raise("external_closure_postrun_facts_projection_invalid")
        current = current.get(part)
    if not isinstance(current, Mapping):
        _raise("external_closure_postrun_facts_projection_invalid")
    return current


def _postrun_pre_projection_error(
    facts: VerifiedPostrunFacts,
    *,
    pre: VerifiedPreReceiptDocuments,
    envelope: Mapping[str, Any],
    consumed_entry: Mapping[str, Any],
    observation: Mapping[str, Any],
    trust: Mapping[str, Any],
) -> str | None:
    """Check fact fields whose mismatch could never yield an anchorable receipt."""

    try:
        runtime_plan = _nested_mapping(envelope, "runtime_plan")
        campaign = _nested_mapping(runtime_plan, "campaign_topology").get(
            "campaign_id"
        )
        custody = _nested_mapping(envelope, "task_custody")
        if facts.campaign_id != campaign:
            return "external_closure_postrun_facts_projection_invalid"
        if facts.task_released:
            expected_release = (
                custody.get("task_bundle_commitment_sha256"),
                custody.get("case_order_commitment_sha256"),
                custody.get("execution_input_order_commitment_sha256"),
            )
            observed_release = (
                facts.released_task_bundle_commitment_sha256,
                facts.released_case_handle_order_commitment_sha256,
                facts.released_execution_input_order_commitment_sha256,
            )
            if observed_release != expected_release:
                return "external_closure_postrun_facts_projection_invalid"

        completed = parse_utc_timestamp(facts.completed_at_utc)
        issued = parse_utc_timestamp(facts.receipt_issued_at_utc)
        consumed = parse_utc_timestamp(str(consumed_entry.get("occurred_at_utc")))
        consumed_observed = parse_utc_timestamp(
            pre.authorization_consumed_observed_at_utc
        )
        grant_expires = parse_utc_timestamp(pre.grant_expires_at_utc)
        envelope_valid = parse_utc_timestamp(pre.envelope_valid_until_utc)
        manifest_observation = _nested_mapping(observation, "manifest_observation")
        manifest_observed = parse_utc_timestamp(
            str(manifest_observation.get("observed_at_utc"))
        )
        if not (
            consumed < completed <= manifest_observed < issued < envelope_valid
            and completed < grant_expires
        ):
            return "external_closure_postrun_facts_timeline_invalid"
        for raw in (facts.task_released_at_utc, facts.provider_key_loaded_at_utc):
            if raw is not None:
                action = parse_utc_timestamp(raw)
                if not consumed_observed < action <= completed:
                    return "external_closure_postrun_facts_timeline_invalid"

        trust_valid = parse_utc_timestamp(str(trust.get("valid_from_utc")))
        trust_expires = parse_utc_timestamp(str(trust.get("expires_at_utc")))
        roles = trust.get("roles")
        if type(roles) is not list:
            return "external_closure_postrun_facts_projection_invalid"
        custodian = next(
            (
                item
                for item in roles
                if isinstance(item, Mapping)
                and item.get("role") == "task_custodian"
            ),
            None,
        )
        if not isinstance(custodian, Mapping):
            return "external_closure_postrun_facts_projection_invalid"
        key_valid = parse_utc_timestamp(str(custodian.get("not_before_utc")))
        key_expires = parse_utc_timestamp(str(custodian.get("expires_at_utc")))
        if not trust_valid <= issued < trust_expires or not key_valid <= issued < key_expires:
            return "external_closure_postrun_facts_timeline_invalid"
    except ExternalClosurePrimitiveError:
        return "external_closure_postrun_facts_timeline_invalid"
    except Exception:
        return "external_closure_postrun_facts_projection_invalid"
    return None


def _privacy_error(facts: VerifiedPostrunFacts, public_scan_status: str) -> str | None:
    if (
        facts.custodian_private_leak_canary_scan_status == "leak_detected"
        or public_scan_status == "sensitive_detected"
    ):
        return "closure_sensitive_content_detected"
    if (
        facts.custodian_private_leak_canary_scan_status == "unavailable"
        or public_scan_status == "unavailable"
    ):
        return "closure_sensitive_scan_unavailable"
    if (
        facts.database_origin_status != "fresh_path_confirmed"
        or facts.database_mutation_status != "normative_only"
    ):
        return "closure_database_origin_untrusted"
    return None


def _summary_projection(summary: ArtifactBundleSummary) -> _ArtifactProjection:
    partial: dict[str, dict[str, object]] = {}
    if (
        summary.artifact_publication_disposition == "normal"
        and summary.bundle_status != "complete"
    ):
        partial = {
            item.name: {"bytes": item.byte_count, "sha256": item.sha256}
            for item in summary.file_commitments
        }
    return _ArtifactProjection(
        bundle_status=summary.bundle_status,
        publication_disposition=summary.artifact_publication_disposition,
        public_scan_status=summary.public_generic_privacy_scan_status,
        manifest_sha256=summary.manifest_sha256,
        partial_artifacts=partial,
    )


def _schema_validate(
    value: object,
    *,
    schemas: Mapping[str, Mapping[str, Any]],
    schema_name: str,
) -> bool:
    schema = schemas.get(schema_name)
    if not isinstance(schema, Mapping) or not only_local_schema_references(schema):
        return False
    try:
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
            registry=LOCAL_ONLY_SCHEMA_REGISTRY,
        ).validate(value)
    except Exception:
        return False
    return True


def _manifest_base_valid(
    payload: bytes,
    *,
    facts: VerifiedPostrunFacts,
    schemas: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, object] | None, bool]:
    try:
        manifest = decode_canonical_json_file(
            payload, max_bytes=_MAX_ARTIFACT_FILE_BYTES
        )
    except ExternalClosurePrimitiveError:
        return None, False
    # A canonical, schema-valid manifest with a mismatched commitment is a
    # complete bundle with invalid evidence, not an invalid manifest document.
    valid = _schema_validate(
        manifest,
        schemas=schemas,
        schema_name="closure_bundle_manifest_v1.schema.json",
    )
    return (manifest if valid else None), valid


def _map_artifact_error(code: str) -> str:
    if code == "external_closure_sensitive_content_detected":
        return "closure_sensitive_content_detected"
    if code == "external_closure_bundle_manifest_invalid" or code.startswith(
        "external_closure_artifact_json_"
    ):
        return "closure_bundle_manifest_invalid"
    if code == "external_closure_audit_schema_invalid":
        return "closure_audit_schema_invalid"
    if code == "external_closure_audit_index_invalid":
        return "closure_audit_run_set_invalid"
    if code == "external_closure_audit_rows_invalid":
        return "closure_event_projection_invalid"
    if code == "external_closure_completion_telemetry_invalid":
        return "closure_outer_projection_invalid"
    if code in _EVIDENCE_ERROR_PRIORITY:
        return code
    return "closure_bundle_hash_mismatch"


def _recover_artifact_projection(
    artifact_directory: object,
    *,
    facts: VerifiedPostrunFacts,
    schemas: Mapping[str, Mapping[str, Any]],
    original_error: str,
) -> tuple[_ArtifactProjection, dict[str, object] | None, tuple[str, ...]]:
    """Recover a safe receipt projection after a normal-container failure.

    Recovery publishes commitments only after a fresh exact-set read and the
    public generic scan.  If that cannot be proven, it degrades to a hash-free
    privacy-unverified quarantine instead of guessing file metadata.
    """

    stage = facts.last_completed_artifact_write_stage
    expected_names = _EXPECTED_NAMES_BY_STAGE[stage]
    bundle_status = "complete" if stage == "manifest_written" else "manifestless_failure"
    mapped_error = _map_artifact_error(original_error)
    attested_privacy_error = _privacy_error(
        facts, facts.public_generic_privacy_scan_status
    )
    if attested_privacy_error is not None:
        return (
            _ArtifactProjection(
                bundle_status=bundle_status,
                publication_disposition=(
                    "sensitive_quarantined"
                    if attested_privacy_error == "closure_sensitive_content_detected"
                    else "privacy_unverified_quarantined"
                ),
                public_scan_status=facts.public_generic_privacy_scan_status,
                manifest_sha256=None,
                partial_artifacts={},
            ),
            None,
            (mapped_error, attested_privacy_error),
        )
    if not isinstance(artifact_directory, Path):
        return (
            _ArtifactProjection(
                bundle_status=bundle_status,
                publication_disposition="privacy_unverified_quarantined",
                public_scan_status="unavailable",
                manifest_sha256=None,
                partial_artifacts={},
            ),
            None,
            (mapped_error, "closure_sensitive_scan_unavailable"),
        )
    try:
        payloads = read_exact_artifact_directory(
            artifact_directory,
            expected_names=expected_names,
            max_file_bytes=_MAX_ARTIFACT_FILE_BYTES,
            max_total_bytes=_MAX_ARTIFACT_TOTAL_BYTES,
        )
        scanned = scan_public_artifact_bytes(tuple(payloads[name] for name in expected_names))
        if scanned != len(expected_names):
            _raise("external_closure_sensitive_scan_unavailable")
    except ExternalClosurePrimitiveError as error:
        if error.code == "external_closure_sensitive_content_detected":
            return (
                _ArtifactProjection(
                    bundle_status=bundle_status,
                    publication_disposition="sensitive_quarantined",
                    public_scan_status="sensitive_detected",
                    manifest_sha256=None,
                    partial_artifacts={},
                ),
                None,
                (mapped_error, "closure_sensitive_content_detected"),
            )
        return (
            _ArtifactProjection(
                bundle_status=bundle_status,
                publication_disposition="privacy_unverified_quarantined",
                public_scan_status="unavailable",
                manifest_sha256=None,
                partial_artifacts={},
            ),
            None,
            (mapped_error, "closure_sensitive_scan_unavailable"),
        )
    except Exception:
        return (
            _ArtifactProjection(
                bundle_status=bundle_status,
                publication_disposition="privacy_unverified_quarantined",
                public_scan_status="unavailable",
                manifest_sha256=None,
                partial_artifacts={},
            ),
            None,
            (mapped_error, "closure_sensitive_scan_unavailable"),
        )

    commitments = {
        name: {"bytes": len(payloads[name]), "sha256": _sha256(payloads[name])}
        for name in expected_names
    }
    manifest: dict[str, object] | None = None
    manifest_sha256: str | None = None
    errors = [mapped_error]
    if stage == "manifest_written":
        manifest, manifest_valid = _manifest_base_valid(
            payloads["completion_closure_manifest.json"],
            facts=facts,
            schemas=schemas,
        )
        if not manifest_valid:
            bundle_status = "manifest_present_invalid"
            errors.append("closure_bundle_manifest_invalid")
        else:
            manifest_sha256 = commitments["completion_closure_manifest.json"][
                "sha256"
            ]  # type: ignore[assignment]
            # Recovery is a second bounded read. Never reuse assumptions about
            # file commitments made by the failed first container evaluation.
            manifest_files = manifest.get("files") if manifest is not None else None
            if not isinstance(manifest_files, Mapping) or any(
                manifest_files.get(name) != commitments[name]
                for name in _WRITER_ORDER[:-1]
            ):
                errors.append("closure_bundle_hash_mismatch")
    else:
        errors.append("closure_bundle_manifest_invalid")
    partial = commitments if bundle_status != "complete" else {}
    return (
        _ArtifactProjection(
            bundle_status=bundle_status,
            publication_disposition="normal",
            public_scan_status="passed",
            manifest_sha256=manifest_sha256,
            partial_artifacts=partial,
        ),
        manifest,
        tuple(errors),
    )


def _load_committed_manifest(
    artifact_directory: Path,
    *,
    summary: ArtifactBundleSummary,
    facts: VerifiedPostrunFacts,
    schemas: Mapping[str, Mapping[str, Any]],
) -> dict[str, object]:
    commitment = next(
        (
            item
            for item in summary.file_commitments
            if item.name == "completion_closure_manifest.json"
        ),
        None,
    )
    if commitment is None:
        _raise("closure_bundle_hash_mismatch")
    try:
        payload = read_regular_file_no_follow(
            artifact_directory / "completion_closure_manifest.json",
            max_bytes=_MAX_ARTIFACT_FILE_BYTES,
        )
    except ExternalClosurePrimitiveError:
        _raise("closure_bundle_hash_mismatch")
    if len(payload) != commitment.byte_count or _sha256(payload) != commitment.sha256:
        _raise("closure_bundle_hash_mismatch")
    manifest, valid = _manifest_base_valid(
        payload, facts=facts, schemas=schemas
    )
    if not valid or manifest is None:
        _raise("closure_bundle_hash_mismatch")
    return manifest


def _manifest_cross_projection_error(
    manifest: Mapping[str, object],
    *,
    pre: VerifiedPreReceiptDocuments,
    envelope: Mapping[str, Any],
) -> str | None:
    runtime = _nested_mapping(envelope, "runtime_plan")
    expected = {
        "campaign_id": _nested_mapping(runtime, "campaign_topology").get(
            "campaign_id"
        ),
        "preregistration_envelope_sha256": pre.preregistration_envelope_sha256,
        "preregistration_freeze_entry_sha256": (
            pre.preregistration_frozen_ledger_entry_sha256
        ),
        "authorization_grant_sha256": pre.authorization_grant_sha256,
        "consumption_receipt_sha256": pre.consumption_receipt_sha256,
        "closure_evidence_contract_commitment_sha256": (
            _CLOSURE_EVIDENCE_COMMITMENT_SHA256
        ),
        "denominator_plan_commitment_sha256": runtime.get(
            "denominator_plan_commitment_sha256"
        ),
        "external_plan_binding_sha256": runtime.get("external_plan_binding_sha256"),
        "audit_database_schema_commitment_sha256": (
            _AUDIT_SCHEMA_COMMITMENT_SHA256
        ),
    }
    if any(manifest.get(name) != value for name, value in expected.items()):
        return "closure_bundle_hash_mismatch"
    return None


def _manifest_observation_error(
    observation: Mapping[str, Any], projection: _ArtifactProjection
) -> str | None:
    value = observation.get("manifest_observation")
    if not isinstance(value, Mapping):
        return "external_closure_manifest_observation_mismatch"
    disposition = projection.publication_disposition
    if disposition == "normal":
        if projection.bundle_status == "manifestless_failure":
            valid = value.get("state") == "absent" and value.get("manifest_sha256") is None
        else:
            valid = value.get("state") == "present"
            expected_hash = projection.manifest_sha256
            if expected_hash is None:
                manifest_commitment = projection.partial_artifacts.get(
                    "completion_closure_manifest.json"
                )
                if isinstance(manifest_commitment, Mapping):
                    expected_hash = manifest_commitment.get("sha256")  # type: ignore[assignment]
            if valid and expected_hash is not None:
                valid = value.get("manifest_sha256") == expected_hash
        return None if valid else "external_closure_manifest_observation_mismatch"
    expected_reason = {
        "sensitive_quarantined": "sensitive_detected",
        "privacy_unverified_quarantined": "privacy_unverified",
    }.get(disposition)
    valid = (
        expected_reason is not None
        and value.get("state") == "withheld_quarantined"
        and value.get("manifest_sha256") is None
        and value.get("quarantine_reason") == expected_reason
    )
    return None if valid else "external_closure_manifest_observation_mismatch"


def _first_evidence_error(candidates: tuple[str, ...] | list[str]) -> str | None:
    present = frozenset(candidates)
    return next((code for code in _EVIDENCE_ERROR_PRIORITY if code in present), None)


def _ordered_reasons(*groups: tuple[str, ...]) -> tuple[str, ...]:
    present: set[str] = set()
    for group in groups:
        present.update(group)
    if not present.issubset(_CLOSURE_REASON_ORDER):
        _raise("closure_outer_projection_invalid")
    return tuple(reason for reason in _CLOSURE_REASON_ORDER if reason in present)


def _external_gate_reasons(facts: VerifiedPostrunFacts) -> tuple[str, ...]:
    reasons: list[str] = []
    if facts.local_terminal_outcome != "succeeded":
        reasons.append("local_terminal_outcome_not_succeeded")
    if not facts.task_released:
        reasons.append("task_not_released")
    if not facts.provider_key_loaded:
        reasons.append("provider_key_not_loaded")
    if not facts.task_bundle_opening_verified_in_memory:
        reasons.append("task_bundle_opening_unverified")
    if not facts.canary_injection_verified_for_every_send:
        reasons.append("leak_canary_injection_unverified")
    if facts.raw_response_cleanup_status in {"failed", "timed_out", "cancelled"}:
        reasons.append("raw_response_cleanup_incomplete")
    if facts.post_request_cleanup_status in {"failed", "timed_out", "cancelled"}:
        reasons.append("post_request_cleanup_incomplete")
    if facts.provider_key_reference_release_status in {
        "failed",
        "timed_out",
        "cancelled",
    }:
        reasons.append("provider_key_reference_not_released")
    return tuple(reasons)


def _reason_commitment(reasons: tuple[str, ...]) -> str:
    return _sha256(
        _REASON_COMMITMENT_DOMAIN.encode("utf-8")
        + b"\x00closure_reasons\x00"
        + canonical_json_bytes(list(reasons))
    )


def _verify_semantics(
    inputs: object,
    *,
    project_root: Path,
    envelope: Mapping[str, Any],
    pre: VerifiedPreReceiptDocuments,
    facts: VerifiedPostrunFacts,
    admission_evidence: object | None = None,
    candidate_frozen_at_utc: str | None = None,
) -> tuple[object | None, str | None]:
    runtime = _nested_mapping(envelope, "runtime_plan")
    denominator = _nested_mapping(runtime, "denominator_plan")
    interpretation = _nested_mapping(runtime, "telemetry_interpretation")
    execution = _nested_mapping(envelope, "execution_binding")
    custody = _nested_mapping(envelope, "task_custody")
    expected_binding = {
        "telemetry_schema_sha256": denominator.get("telemetry_schema_sha256"),
        "adapter_version": denominator.get("adapter_version"),
        "mapping_schema_version": denominator.get("mapping_schema_version"),
        "mapping_version": denominator.get("mapping_version"),
        "mapping_sha256": denominator.get("mapping_sha256"),
        "provider_id": denominator.get("provider_id"),
        "api_surface": denominator.get("api_surface"),
        "transport_id": denominator.get("transport_id"),
        "output_counter_comparability": interpretation.get(
            "output_counter_comparability"
        ),
        "output_counter_path": interpretation.get("output_counter_path"),
    }
    try:
        from researchops_completion_telemetry.persisted_evidence import (
            create_persisted_live_evidence_validation_context,
        )
        from researchops_completion_telemetry.surface_mapping import (
            load_and_select_surface_mapping,
        )

        selection = load_and_select_surface_mapping(
            project_root,
            str(denominator.get("provider_id")),
            str(denominator.get("api_surface")),
            str(denominator.get("transport_id")),
            purpose="offline_validation",
        )
        # v2 supplies immutable mapping bytes only, never runtime eligibility.
        # A separate, fully checked v3 evidence carrier is required below.
        context = create_persisted_live_evidence_validation_context(
            selection,
            expected_binding=expected_binding,
            expected_denominator_plan=denominator,
            expected_denominator_plan_commitment_sha256=str(
                runtime.get("denominator_plan_commitment_sha256")
            ),
        )
    except Exception:
        return None, "closure_denominator_invalid"

    admission_error = "closure_denominator_invalid"
    try:
        from .admission_evidence import AdmissionEvidenceInputs, VerifiedAdmissionLinks, verify_admission_evidence_links

        if (
            type(admission_evidence) is AdmissionEvidenceInputs
            and admission_evidence.expected_trust_manifest_sha256 == pre.trust_manifest_sha256
            and admission_evidence.expected_candidate_freeze_receipt_sha256 == pre.candidate_freeze_receipt_sha256
            and admission_evidence.expected_candidate_frozen_at_utc == candidate_frozen_at_utc
        ):
            links = verify_admission_evidence_links(project_root, execution_binding=dict(execution), evidence=admission_evidence)
            if (
                type(links) is VerifiedAdmissionLinks
                and links.component_bindings_verified is True
                and links.first_live_artifact_semantics_verified is True
                and links.source_equivalence_verified is True
                and links.review_signature_and_observation_verified is True
                and links.registry_links_verified is True
                and links.runtime_authority_granted is False
                and links.closure_claim_allowed is False
            ):
                admission_error = None
    except Exception:
        admission_error = "closure_denominator_invalid"

    def failure(code: str) -> tuple[None, str]:
        candidates = [code]
        if admission_error is not None:
            candidates.append(admission_error)
        return None, str(_first_evidence_error(candidates))

    try:
        from .semantics import (
            VerifiedArtifactContentSemantics,
            verify_artifact_content_semantics,
        )

        semantic_inputs = getattr(inputs, "_semantic_inputs")()
        if type(semantic_inputs) is not tuple or len(semantic_inputs) != 5:
            return failure("closure_outer_projection_invalid")
        telemetry, audit_index, runs, events, model_calls = semantic_inputs
        if not isinstance(telemetry, Mapping) or not isinstance(audit_index, Mapping):
            return failure("closure_outer_projection_invalid")
        result = verify_artifact_content_semantics(
            telemetry,
            audit_index,
            run_rows=runs,
            event_rows=events,
            model_call_rows=model_calls,
            persisted_evidence_context=context,
            envelope_id=str(envelope.get("envelope_id")),
            campaign_id=facts.campaign_id,
            expected_runtime_plan=runtime,
            authorization_grant_sha256=pre.authorization_grant_sha256,
            execution_input_order_commitment_sha256=str(
                custody.get("execution_input_order_commitment_sha256")
            ),
            api_origin=str(execution.get("api_origin")),
            api_path="/responses",
            model_id=str(execution.get("model_id")),
            postrun_completed_at_utc=facts.completed_at_utc,
            authorization_consumed_at_utc=(
                pre.authorization_consumed_observed_at_utc
            ),
            task_released_at_utc=facts.task_released_at_utc,
            provider_key_loaded_at_utc=facts.provider_key_loaded_at_utc,
            tool_call_count=getattr(inputs, "tool_call_count"),
            tool_attempt_count=getattr(inputs, "tool_attempt_count"),
            approval_decision_count=getattr(inputs, "approval_decision_count"),
        )
        if type(result) is not VerifiedArtifactContentSemantics:
            return failure("closure_outer_projection_invalid")
        postrun_error = _semantic_postrun_error(
            result,
            facts=facts,
            planned_case_count=custody.get("planned_case_count"),
        )
        if postrun_error is not None:
            return failure(postrun_error)
        if admission_error is not None:
            return failure(admission_error)
        return result, None
    except ExternalClosurePrimitiveError as error:
        if error.code in _EVIDENCE_ERROR_PRIORITY:
            return failure(error.code)
        return failure("closure_outer_projection_invalid")
    except Exception:
        return failure("closure_outer_projection_invalid")


def _semantic_postrun_error(
    semantic: object,
    *,
    facts: VerifiedPostrunFacts,
    planned_case_count: object,
) -> str | None:
    if (
        getattr(semantic, "planned_case_count", None) != planned_case_count
        or getattr(semantic, "network_attempt_count_observed", None)
        != facts.network_attempt_count_observed
        or getattr(semantic, "model_request_count_observed", None)
        != facts.model_request_count_observed
    ):
        return "closure_event_projection_invalid"
    last_error = getattr(semantic, "last_observed_run_terminal_error_code", None)
    if facts.terminal_stage == "none" and last_error is not None:
        return "closure_event_projection_invalid"
    if facts.terminal_stage in {"transport", "response", "raw_response_cleanup"} and (
        last_error != facts.terminal_error_code
    ):
        return "closure_event_projection_invalid"
    return None


def _receipt_body(
    *,
    pre: VerifiedPreReceiptDocuments,
    facts: VerifiedPostrunFacts,
    projection: _ArtifactProjection,
    planned_case_count: int,
    semantic: object | None,
    evidence_error: str | None,
) -> dict[str, object]:
    evidence_valid = evidence_error is None and semantic is not None
    if evidence_valid:
        semantic_reasons = tuple(
            getattr(semantic, "semantic_closure_reasons", ())
        )
        prefix_reasons = (
            ()
            if bool(getattr(semantic, "full_plan_observed", False))
            else ("planned_cases_not_finalized",)
        )
        reasons = _ordered_reasons(
            prefix_reasons, semantic_reasons, _external_gate_reasons(facts)
        )
        eligible = bool(
            getattr(semantic, "semantic_closure_claim_allowed", False)
            and getattr(semantic, "full_plan_observed", False)
            and not reasons
        )
        observed_run_set = getattr(
            semantic, "observed_audit_run_set_commitment_sha256"
        )
        ordered_heads = getattr(
            semantic, "ordered_final_chain_heads_commitment_sha256"
        )
        attempt_count = getattr(semantic, "attempt_count")
        accepted_count = getattr(semantic, "accepted_response_count")
        network_count = getattr(semantic, "network_attempt_count_observed")
        model_request_count = getattr(semantic, "model_request_count_observed")
        usage_complete = getattr(semantic, "usage_complete")
        observed_input = getattr(semantic, "observed_input_tokens")
        observed_output = getattr(semantic, "observed_output_tokens")
        observed_cost = getattr(semantic, "observed_cost_cny")
    else:
        reasons = ()
        eligible = False
        observed_run_set = None
        ordered_heads = None
        attempt_count = None
        accepted_count = None
        network_count = facts.network_attempt_count_observed
        model_request_count = facts.model_request_count_observed
        usage_complete = None
        observed_input = None
        observed_output = None
        observed_cost = None

    body: dict[str, object] = {
        "schema_version": "provider-completion-closure-receipt/1.0",
        "document_type": "completion_telemetry_closure_receipt",
        "receipt_id": facts.receipt_id,
        "campaign_id": facts.campaign_id,
        "completed_at_utc": facts.completed_at_utc,
        "receipt_issued_at_utc": facts.receipt_issued_at_utc,
        "bundle_status": projection.bundle_status,
        "artifact_publication_disposition": projection.publication_disposition,
        "terminal_stage": facts.terminal_stage,
        "local_terminal_outcome": facts.local_terminal_outcome,
        "terminal_provider_outcome_state": facts.terminal_provider_outcome_state,
        "terminal_error_code": facts.terminal_error_code,
        "last_completed_artifact_write_stage": (
            facts.last_completed_artifact_write_stage
        ),
        "artifact_error_code": facts.artifact_error_code,
        "preregistration_envelope_sha256": pre.preregistration_envelope_sha256,
        "preregistration_freeze_entry_sha256": (
            pre.preregistration_frozen_ledger_entry_sha256
        ),
        "authorization_grant_sha256": pre.authorization_grant_sha256,
        "consumption_receipt_sha256": pre.consumption_receipt_sha256,
        "authorization_consumption_entry_sha256": (
            pre.authorization_consumed_ledger_entry_sha256
        ),
        "task_released": facts.task_released,
        "task_released_at_utc": facts.task_released_at_utc,
        "provider_key_loaded": facts.provider_key_loaded,
        "provider_key_loaded_at_utc": facts.provider_key_loaded_at_utc,
        "released_task_bundle_commitment_sha256": (
            facts.released_task_bundle_commitment_sha256
        ),
        "released_case_handle_order_commitment_sha256": (
            facts.released_case_handle_order_commitment_sha256
        ),
        "task_bundle_opening_verified_in_memory": (
            facts.task_bundle_opening_verified_in_memory
        ),
        "task_bundle_opening_persisted": facts.task_bundle_opening_persisted,
        "custodian_private_leak_canary_scan_status": (
            facts.custodian_private_leak_canary_scan_status
        ),
        "public_generic_privacy_scan_status": projection.public_scan_status,
        "canary_injection_verified_for_every_send": (
            facts.canary_injection_verified_for_every_send
        ),
        "database_origin_status": facts.database_origin_status,
        "database_mutation_status": facts.database_mutation_status,
        "released_execution_input_order_commitment_sha256": (
            facts.released_execution_input_order_commitment_sha256
        ),
        "closure_bundle_manifest_sha256": projection.manifest_sha256,
        "partial_artifacts": projection.partial_artifacts,
        "observed_audit_run_set_commitment_sha256": observed_run_set,
        "ordered_final_chain_heads_commitment_sha256": ordered_heads,
        "planned_case_count": planned_case_count,
        "attempt_count": attempt_count,
        "accepted_response_count": accepted_count,
        "network_attempt_count_observed": network_count,
        "model_request_count_observed": model_request_count,
        "usage_complete": usage_complete,
        "observed_input_tokens": observed_input,
        "observed_output_tokens": observed_output,
        "observed_cost_cny": observed_cost,
        "evidence_valid": evidence_valid,
        "evidence_error_code": evidence_error,
        "pre_anchor_closure_eligible": eligible,
        "closure_reasons": list(reasons),
        "closure_reasons_commitment_sha256": _reason_commitment(reasons),
        "task_content_included_in_receipt": False,
        "case_ids_included_in_receipt": False,
        "records_included_in_receipt": False,
        "provider_content_included_in_receipt": False,
        "raw_response_cleanup_status": facts.raw_response_cleanup_status,
        "post_request_cleanup_status": facts.post_request_cleanup_status,
        "provider_key_reference_release_status": (
            facts.provider_key_reference_release_status
        ),
    }
    return body


def _validate_unsigned_receipt(
    body: dict[str, object], *, schemas: Mapping[str, Mapping[str, Any]]
) -> str:
    if len(body) != len(_UNSIGNED_RECEIPT_FIELDS) or set(body) != set(
        _UNSIGNED_RECEIPT_FIELDS
    ):
        _raise("external_closure_receipt_projection_invalid")
    document_hash = compute_document_sha256(body)
    schema_document = {
        **body,
        "document_sha256": document_hash,
        "signatures": [
            {
                "role": "task_custodian",
                "key_id": "PCEKEY-00000000000000000000000000000000",
                "algorithm": "ed25519",
                "signed_sha256": document_hash,
                "signature_b64": "A" * 86 + "==",
            }
        ],
    }
    if not _schema_validate(
        schema_document,
        schemas=schemas,
        schema_name="closure_receipt_v1.schema.json",
    ):
        _raise("external_closure_receipt_projection_invalid")
    return document_hash


def evaluate_closure_bundle(
    project_root: Path,
    documents: PreReceiptDocumentBytes,
    *,
    external_observation_bundle: bytes,
    artifact_directory: Path | None,
    postrun_attested_facts: bytes,
    admission_evidence: object | None = None,
) -> PreReceiptResult:
    """Return an unsigned, content-free closure receipt projection.

    Invalid frozen/pre-receipt documents and invalid post-run fact bytes are
    rejected.  Artifact and persisted-evidence failures are instead projected
    into a schema-valid, externally signable failure receipt whenever the safe
    public facts needed for such a receipt remain available.
    """

    if not isinstance(project_root, Path):
        return _reject("external_closure_project_root_invalid")
    if type(documents) is not PreReceiptDocumentBytes:
        return _reject("external_closure_pre_receipt_documents_invalid")
    if type(external_observation_bundle) is not bytes:
        return _reject("external_closure_observation_schema_invalid")
    if type(postrun_attested_facts) is not bytes:
        return _reject("external_closure_postrun_facts_invalid")

    try:
        schemas = _load_schemas(project_root)
        pre = verify_pre_receipt_documents(
            documents,
            schemas=schemas,
            external_observation_bundle=external_observation_bundle,
        )
    except ExternalClosurePrimitiveError as error:
        return _reject(error.code)
    except Exception:
        return _reject("external_closure_pre_receipt_documents_invalid")

    try:
        facts = validate_postrun_attested_facts(
            postrun_attested_facts, schemas=schemas
        )
    except ExternalClosurePrimitiveError as error:
        return _reject(error.code)
    except Exception:
        return _reject("external_closure_postrun_facts_invalid")

    try:
        envelope = _decode_verified(documents.preregistration_envelope)
        consumed_entry = _decode_verified(
            documents.authorization_consumed_ledger_entry
        )
        observation = _decode_verified(external_observation_bundle)
        trust = _decode_verified(documents.trust_manifest)
        projection_error = _postrun_pre_projection_error(
            facts,
            pre=pre,
            envelope=envelope,
            consumed_entry=consumed_entry,
            observation=observation,
            trust=trust,
        )
        if projection_error is not None:
            return _reject(projection_error)
        custody = _nested_mapping(envelope, "task_custody")
        planned_case_count = custody.get("planned_case_count")
        if type(planned_case_count) is not int:
            return _reject("external_closure_postrun_facts_projection_invalid")
    except ExternalClosurePrimitiveError as error:
        return _reject(error.code)
    except Exception:
        return _reject("external_closure_postrun_facts_projection_invalid")

    errors: list[str] = []
    semantic: object | None = None
    manifest: Mapping[str, object] | None = None
    inputs: object | None = None
    try:
        # Passing ``None`` on an attested quarantine makes the zero-read boundary
        # structural even when the caller supplies a hostile path-like object.
        privacy = _privacy_error(
            facts, facts.public_generic_privacy_scan_status
        )
        inputs = extract_artifact_evaluation_inputs(
            None if privacy is not None else artifact_directory,
            postrun_facts=facts,
            schemas=schemas,
        )
        summary = getattr(inputs, "summary")
        if type(summary) is not ArtifactBundleSummary:
            _raise("external_closure_artifact_snapshot_invalid")
        artifact_projection = _summary_projection(summary)
        privacy = _privacy_error(facts, artifact_projection.public_scan_status)
        if privacy is not None:
            errors.append(privacy)
        if artifact_projection.bundle_status != "complete":
            errors.append("closure_bundle_manifest_invalid")
        if (
            artifact_projection.bundle_status == "complete"
            and artifact_projection.publication_disposition == "normal"
        ):
            if not isinstance(artifact_directory, Path):
                _raise("external_closure_artifact_directory_required")
            manifest = _load_committed_manifest(
                artifact_directory,
                summary=summary,
                facts=facts,
                schemas=schemas,
            )
    except ExternalClosurePrimitiveError as error:
        artifact_projection, manifest, recovered_errors = _recover_artifact_projection(
            artifact_directory,
            facts=facts,
            schemas=schemas,
            original_error=error.code,
        )
        errors.extend(recovered_errors)
        privacy = _privacy_error(facts, artifact_projection.public_scan_status)
        if privacy is not None:
            errors.append(privacy)
    except Exception:
        artifact_projection, manifest, recovered_errors = _recover_artifact_projection(
            artifact_directory,
            facts=facts,
            schemas=schemas,
            original_error="external_closure_artifact_snapshot_invalid",
        )
        errors.extend(recovered_errors)

    observation_error = _manifest_observation_error(observation, artifact_projection)
    manifest_observation = observation.get("manifest_observation")
    if (
        observation_error is None
        and isinstance(manifest_observation, Mapping)
        and manifest_observation.get("last_completed_artifact_write_stage")
        != facts.last_completed_artifact_write_stage
    ):
        observation_error = "external_closure_manifest_observation_mismatch"
    if observation_error is not None:
        return _reject(observation_error)

    if manifest is not None:
        try:
            cross_error = _manifest_cross_projection_error(
                manifest, pre=pre, envelope=envelope
            )
        except ExternalClosurePrimitiveError:
            cross_error = "closure_bundle_hash_mismatch"
        if cross_error is not None:
            errors.append(cross_error)

    evidence_error = _first_evidence_error(errors)
    if evidence_error is None:
        if inputs is None:
            evidence_error = "closure_outer_projection_invalid"
        else:
            semantic, semantic_error = _verify_semantics(
                inputs,
                project_root=project_root,
                envelope=envelope,
                pre=pre,
                facts=facts,
                admission_evidence=admission_evidence,
                candidate_frozen_at_utc=_decode_verified(documents.candidate_freeze_receipt).get("frozen_at_utc"),
            )
            if semantic_error is not None:
                evidence_error = semantic_error
            elif semantic is not None:
                evidence_error = _semantic_postrun_error(
                    semantic,
                    facts=facts,
                    planned_case_count=planned_case_count,
                )

    try:
        body = _receipt_body(
            pre=pre,
            facts=facts,
            projection=artifact_projection,
            planned_case_count=planned_case_count,
            semantic=semantic,
            evidence_error=evidence_error,
        )
        receipt_hash = _validate_unsigned_receipt(body, schemas=schemas)
        unsigned = canonical_json_bytes(body)
        reasons = tuple(body["closure_reasons"])
        return ReceiptProjectionReady(
            unsigned_receipt_canonical_json=unsigned,
            receipt_document_sha256=receipt_hash,
            evidence_valid=bool(body["evidence_valid"]),
            pre_anchor_closure_eligible=bool(
                body["pre_anchor_closure_eligible"]
            ),
            evidence_error_code=(
                body["evidence_error_code"]
                if isinstance(body["evidence_error_code"], str)
                else None
            ),
            closure_reasons=reasons,
        )
    except ExternalClosurePrimitiveError as error:
        return _reject(error.code)
    except Exception:
        return _reject("external_closure_receipt_projection_invalid")


__all__ = ["evaluate_closure_bundle"]
