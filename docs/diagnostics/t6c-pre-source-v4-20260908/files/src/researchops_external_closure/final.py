"""Pure layer-B verification for externally anchored closure receipts.

The verifier consumes caller-supplied bytes, loads only the repository-frozen
schema contract, and returns a small immutable terminal value. It has no clock,
signer, Provider, runtime authority, or network capability. A normal publication
is re-evaluated by layer A exactly once. Quarantined publication is deliberately
receipt-only and never passes its artifact path to layer A.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

import jsonschema

from .documents import verify_pre_receipt_documents
from .errors import ExternalClosurePrimitiveError
from .primitives import (
    build_signature_message,
    canonical_json_bytes,
    decode_strict_json_object,
    parse_utc_timestamp,
    verify_document_sha256,
    verify_ed25519_signature,
    verify_ledger_hashes,
)
from .types import FinalClosureResult, FinalDocumentBytes, ReceiptProjectionReady
from .schema_guard import LOCAL_ONLY_SCHEMA_REGISTRY, only_local_schema_references


_MAX_DOCUMENT_BYTES = 2_000_000
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_REASONS_DOMAIN = "researchops-provider-completion-closure-receipt-v1"

_RECEIPT_SCHEMA_ERROR = "closure_receipt_schema_invalid"
_RECEIPT_SIGNATURE_ERROR = "closure_receipt_signature_invalid"
_RECEIPT_PROJECTION_ERROR = "closure_receipt_projection_mismatch"
_EXTERNAL_ANCHOR_ERROR = "closure_external_anchor_mismatch"


class _FinalVerificationFailure(Exception):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _reject(code: str) -> NoReturn:
    raise _FinalVerificationFailure(code)


def _invalid(code: str) -> FinalClosureResult:
    return FinalClosureResult(
        "unclosed_invalid", False, False, code, None, None
    )


def _default_rerun_evaluator(*args: object, **kwargs: object) -> object:
    """Resolve layer A only when a normal publication actually needs it."""

    from .evaluator import evaluate_closure_bundle

    return evaluate_closure_bundle(*args, **kwargs)


def _load_schemas(project_root: Path) -> Mapping[str, Mapping[str, Any]]:
    if not isinstance(project_root, Path):
        _reject(_RECEIPT_SCHEMA_ERROR)
    try:
        from researchops.provider_completion_external_contract import (
            ExternalPreregistrationContractError,
            load_frozen_external_preregistration_contract,
        )

        try:
            contract = load_frozen_external_preregistration_contract(project_root)
        except ExternalPreregistrationContractError:
            _reject(_RECEIPT_SCHEMA_ERROR)
        schemas = contract.get("schemas")
        if not isinstance(schemas, Mapping):
            _reject(_RECEIPT_SCHEMA_ERROR)
        return schemas
    except _FinalVerificationFailure:
        raise
    except Exception:
        _reject(_RECEIPT_SCHEMA_ERROR)


def _decode_schema_document(
    payload: bytes,
    *,
    schemas: Mapping[str, Mapping[str, Any]],
    schema_name: str,
    error_code: str,
) -> dict[str, Any]:
    try:
        value = decode_strict_json_object(payload, max_bytes=_MAX_DOCUMENT_BYTES)
        schema = schemas.get(schema_name)
        if not isinstance(schema, Mapping) or not only_local_schema_references(schema):
            _reject(error_code)
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
            registry=LOCAL_ONLY_SCHEMA_REGISTRY,
        ).validate(value)
        return value
    except _FinalVerificationFailure:
        raise
    except Exception:
        _reject(error_code)


def _decode_verified_pre_document(payload: bytes) -> dict[str, Any]:
    try:
        return decode_strict_json_object(payload, max_bytes=_MAX_DOCUMENT_BYTES)
    except ExternalClosurePrimitiveError:
        _reject(_EXTERNAL_ANCHOR_ERROR)


def _timestamp(value: object, *, error_code: str):
    if type(value) is not str:
        _reject(error_code)
    try:
        return parse_utc_timestamp(value)
    except ExternalClosurePrimitiveError:
        _reject(error_code)


def _role_record(trust: Mapping[str, Any], role: str) -> Mapping[str, Any]:
    records = trust.get("roles")
    if type(records) is not list:
        _reject(_EXTERNAL_ANCHOR_ERROR)
    for record in records:
        if isinstance(record, Mapping) and record.get("role") == role:
            return record
    _reject(_EXTERNAL_ANCHOR_ERROR)


def _assert_signature_time(
    *,
    trust: Mapping[str, Any],
    role_record: Mapping[str, Any],
    action_time: object,
    error_code: str,
) -> None:
    action = _timestamp(action_time, error_code=error_code)
    valid_from = _timestamp(trust.get("valid_from_utc"), error_code=error_code)
    trust_expires = _timestamp(trust.get("expires_at_utc"), error_code=error_code)
    key_not_before = _timestamp(
        role_record.get("not_before_utc"), error_code=error_code
    )
    key_expires = _timestamp(
        role_record.get("expires_at_utc"), error_code=error_code
    )
    if not (
        valid_from <= action < trust_expires
        and key_not_before <= action < key_expires
    ):
        _reject(error_code)


def _verify_exact_signature(
    document: Mapping[str, Any],
    *,
    role: str,
    role_record: Mapping[str, Any],
    trust: Mapping[str, Any],
    action_time: object,
    subject_field: str,
    error_code: str,
) -> None:
    document_type = document.get("document_type")
    subject = document.get(subject_field)
    signatures = document.get("signatures")
    key_id = role_record.get("key_id")
    public_key = role_record.get("public_key_b64")
    if (
        type(document_type) is not str
        or type(subject) is not str
        or type(signatures) is not list
        or len(signatures) != 1
        or type(key_id) is not str
        or type(public_key) is not str
        or not isinstance(signatures[0], Mapping)
    ):
        _reject(error_code)
    signature = signatures[0]
    if (
        signature.get("role") != role
        or signature.get("key_id") != key_id
        or signature.get("algorithm") != "ed25519"
        or signature.get("signed_sha256") != subject
        or type(signature.get("signature_b64")) is not str
    ):
        _reject(error_code)
    _assert_signature_time(
        trust=trust,
        role_record=role_record,
        action_time=action_time,
        error_code=error_code,
    )
    try:
        message = build_signature_message(
            document_type=document_type,
            role=role,
            key_id=key_id,
            signed_sha256=subject,
        )
        verify_ed25519_signature(
            public_key_b64=public_key,
            signature_b64=signature["signature_b64"],
            message=message,
        )
    except ExternalClosurePrimitiveError:
        _reject(error_code)


def _pre_mode_observation(observation: Mapping[str, Any]) -> bytes:
    projection = dict(observation)
    projection["mode"] = "pre_receipt"
    projection["closure_receipt_observation"] = None
    projection["closure_evidence_anchor"] = None
    try:
        return canonical_json_bytes(projection)
    except ExternalClosurePrimitiveError:
        _reject(_EXTERNAL_ANCHOR_ERROR)


def _verify_pre_history(
    documents: FinalDocumentBytes,
    *,
    schemas: Mapping[str, Mapping[str, Any]],
    pre_observation: bytes,
) -> object:
    try:
        return verify_pre_receipt_documents(
            documents.pre_receipt,
            schemas=schemas,
            external_observation_bundle=pre_observation,
        )
    except ExternalClosurePrimitiveError:
        _reject(_EXTERNAL_ANCHOR_ERROR)


def _verify_receipt_hash_and_signature(
    receipt: Mapping[str, Any], *, trust: Mapping[str, Any]
) -> None:
    try:
        verify_document_sha256(dict(receipt))
    except ExternalClosurePrimitiveError:
        _reject(_RECEIPT_SIGNATURE_ERROR)
    custodian = _role_record(trust, "task_custodian")
    _verify_exact_signature(
        receipt,
        role="task_custodian",
        role_record=custodian,
        trust=trust,
        action_time=receipt.get("receipt_issued_at_utc"),
        subject_field="document_sha256",
        error_code=_RECEIPT_SIGNATURE_ERROR,
    )


def _reason_commitment(reasons: object) -> str:
    try:
        payload = canonical_json_bytes(reasons)
    except ExternalClosurePrimitiveError:
        _reject(_RECEIPT_PROJECTION_ERROR)
    return hashlib.sha256(
        _REASONS_DOMAIN.encode("utf-8")
        + b"\x00closure_reasons\x00"
        + payload
    ).hexdigest()


def _verify_manifest_observation_projection(
    receipt: Mapping[str, Any], observation: Mapping[str, Any]
) -> None:
    manifest = observation.get("manifest_observation")
    if not isinstance(manifest, Mapping):
        _reject(_RECEIPT_PROJECTION_ERROR)
    disposition = receipt.get("artifact_publication_disposition")
    stage = receipt.get("last_completed_artifact_write_stage")
    if manifest.get("last_completed_artifact_write_stage") != stage:
        _reject(_RECEIPT_PROJECTION_ERROR)
    if disposition == "normal":
        expected_state = "present" if stage == "manifest_written" else "absent"
        if manifest.get("state") != expected_state:
            _reject(_RECEIPT_PROJECTION_ERROR)
        if expected_state == "present":
            expected_hash = receipt.get("closure_bundle_manifest_sha256")
            if receipt.get("bundle_status") == "manifest_present_invalid":
                partial = receipt.get("partial_artifacts")
                commitment = (
                    partial.get("completion_closure_manifest.json")
                    if isinstance(partial, Mapping)
                    else None
                )
                expected_hash = (
                    commitment.get("sha256")
                    if isinstance(commitment, Mapping)
                    else None
                )
            if expected_hash is None or manifest.get("manifest_sha256") != expected_hash:
                _reject(_RECEIPT_PROJECTION_ERROR)
        return
    quarantine_reason = {
        "sensitive_quarantined": "sensitive_detected",
        "privacy_unverified_quarantined": "privacy_unverified",
    }.get(disposition)
    if (
        quarantine_reason is None
        or manifest.get("state") != "withheld_quarantined"
        or manifest.get("manifest_sha256") is not None
        or manifest.get("quarantine_reason") != quarantine_reason
    ):
        _reject(_RECEIPT_PROJECTION_ERROR)


def _verify_receipt_projection(
    receipt: Mapping[str, Any],
    *,
    pre_summary: object,
    pre_documents: Mapping[str, Mapping[str, Any]],
    observation: Mapping[str, Any],
) -> None:
    envelope = pre_documents["preregistration_envelope"]
    candidate = pre_documents["candidate_freeze_receipt"]
    preregistration_entry = pre_documents["preregistration_frozen_ledger_entry"]
    grant = pre_documents["authorization_grant"]
    consumption = pre_documents["consumption_receipt"]
    consumed_entry = pre_documents["authorization_consumed_ledger_entry"]
    try:
        runtime_plan = envelope["runtime_plan"]
        campaign = runtime_plan["campaign_topology"]["campaign_id"]
        custody = envelope["task_custody"]
    except (KeyError, TypeError):
        _reject(_EXTERNAL_ANCHOR_ERROR)

    expected_pairs = (
        (receipt.get("campaign_id"), campaign),
        (
            receipt.get("preregistration_envelope_sha256"),
            getattr(pre_summary, "preregistration_envelope_sha256", None),
        ),
        (
            receipt.get("preregistration_freeze_entry_sha256"),
            getattr(pre_summary, "preregistration_frozen_ledger_entry_sha256", None),
        ),
        (
            receipt.get("authorization_grant_sha256"),
            getattr(pre_summary, "authorization_grant_sha256", None),
        ),
        (
            receipt.get("consumption_receipt_sha256"),
            getattr(pre_summary, "consumption_receipt_sha256", None),
        ),
        (
            receipt.get("authorization_consumption_entry_sha256"),
            getattr(
                pre_summary,
                "authorization_consumed_ledger_entry_sha256",
                None,
            ),
        ),
        (receipt.get("planned_case_count"), custody.get("planned_case_count")),
    )
    if any(left != right for left, right in expected_pairs):
        _reject(_RECEIPT_PROJECTION_ERROR)

    release_pairs = (
        (
            receipt.get("released_task_bundle_commitment_sha256"),
            custody.get("task_bundle_commitment_sha256"),
        ),
        (
            receipt.get("released_case_handle_order_commitment_sha256"),
            custody.get("case_order_commitment_sha256"),
        ),
        (
            receipt.get("released_execution_input_order_commitment_sha256"),
            custody.get("execution_input_order_commitment_sha256"),
        ),
    )
    if receipt.get("task_released") is True and any(
        left != right for left, right in release_pairs
    ):
        _reject(_RECEIPT_PROJECTION_ERROR)

    if receipt.get("closure_reasons_commitment_sha256") != _reason_commitment(
        receipt.get("closure_reasons")
    ):
        _reject(_RECEIPT_PROJECTION_ERROR)
    _verify_manifest_observation_projection(receipt, observation)

    completed = _timestamp(
        receipt.get("completed_at_utc"), error_code=_RECEIPT_PROJECTION_ERROR
    )
    issued = _timestamp(
        receipt.get("receipt_issued_at_utc"),
        error_code=_RECEIPT_PROJECTION_ERROR,
    )
    consumed = _timestamp(
        consumed_entry.get("occurred_at_utc"), error_code=_EXTERNAL_ANCHOR_ERROR
    )
    grant_expires = _timestamp(
        grant.get("expires_at_utc"), error_code=_EXTERNAL_ANCHOR_ERROR
    )
    manifest_value = observation.get("manifest_observation")
    if not isinstance(manifest_value, Mapping):
        _reject(_RECEIPT_PROJECTION_ERROR)
    manifest_observed = _timestamp(
        manifest_value.get("observed_at_utc"),
        error_code=_RECEIPT_PROJECTION_ERROR,
    )
    if not (
        consumed < completed <= manifest_observed < issued
        and completed < grant_expires
    ):
        _reject(_RECEIPT_PROJECTION_ERROR)

    consumed_anchor = observation.get("authorization_consumed_anchor")
    if not isinstance(consumed_anchor, Mapping):
        _reject(_EXTERNAL_ANCHOR_ERROR)
    consumed_observed = _timestamp(
        consumed_anchor.get("observed_at_utc"), error_code=_EXTERNAL_ANCHOR_ERROR
    )
    for field in ("task_released_at_utc", "provider_key_loaded_at_utc"):
        raw = receipt.get(field)
        if raw is not None:
            action = _timestamp(raw, error_code=_RECEIPT_PROJECTION_ERROR)
            if not consumed_observed < action <= completed:
                _reject(_RECEIPT_PROJECTION_ERROR)

    # The receipt must bind computed prior documents, never artifact values.
    if (
        candidate.get("document_sha256")
        != getattr(pre_summary, "candidate_freeze_receipt_sha256", None)
        or preregistration_entry.get("entry_sha256")
        != getattr(
            pre_summary, "preregistration_frozen_ledger_entry_sha256", None
        )
        or grant.get("document_sha256")
        != getattr(pre_summary, "authorization_grant_sha256", None)
        or consumption.get("document_sha256")
        != getattr(pre_summary, "consumption_receipt_sha256", None)
    ):
        _reject(_EXTERNAL_ANCHOR_ERROR)


def _verify_final_entry_and_observation(
    final_entry: Mapping[str, Any],
    *,
    receipt: Mapping[str, Any],
    pre_summary: object,
    pre_documents: Mapping[str, Mapping[str, Any]],
    observation: Mapping[str, Any],
) -> None:
    trust = pre_documents["trust_manifest"]
    envelope = pre_documents["preregistration_envelope"]
    consumed_entry = pre_documents["authorization_consumed_ledger_entry"]
    try:
        verify_ledger_hashes(dict(final_entry))
    except ExternalClosurePrimitiveError:
        _reject(_EXTERNAL_ANCHOR_ERROR)

    witness = _role_record(trust, "ledger_witness")
    _verify_exact_signature(
        final_entry,
        role="ledger_witness",
        role_record=witness,
        trust=trust,
        action_time=final_entry.get("occurred_at_utc"),
        subject_field="entry_sha256",
        error_code=_EXTERNAL_ANCHOR_ERROR,
    )

    consumed_sequence = consumed_entry.get("sequence")
    if (
        type(consumed_sequence) is not int
        or consumed_sequence >= _MAX_SAFE_INTEGER
        or final_entry.get("entry_type") != "closure_evidence_anchored"
        or final_entry.get("ledger_id") != consumed_entry.get("ledger_id")
        or final_entry.get("sequence") != consumed_sequence + 1
        or final_entry.get("previous_head_sha256")
        != consumed_entry.get("resulting_head_sha256")
    ):
        _reject(_EXTERNAL_ANCHOR_ERROR)

    expected_hashes = {
        "candidate_freeze_receipt_sha256": getattr(
            pre_summary, "candidate_freeze_receipt_sha256", None
        ),
        "preregistration_envelope_sha256": getattr(
            pre_summary, "preregistration_envelope_sha256", None
        ),
        "authorization_grant_sha256": getattr(
            pre_summary, "authorization_grant_sha256", None
        ),
        "consumption_receipt_sha256": getattr(
            pre_summary, "consumption_receipt_sha256", None
        ),
        "closure_receipt_sha256": receipt.get("document_sha256"),
    }
    if any(final_entry.get(name) != value for name, value in expected_hashes.items()):
        _reject(_EXTERNAL_ANCHOR_ERROR)

    receipt_observation = observation.get("closure_receipt_observation")
    final_anchor = observation.get("closure_evidence_anchor")
    if not isinstance(receipt_observation, Mapping) or not isinstance(
        final_anchor, Mapping
    ):
        _reject(_EXTERNAL_ANCHOR_ERROR)
    if receipt_observation.get("document_sha256") != receipt.get(
        "document_sha256"
    ):
        _reject(_EXTERNAL_ANCHOR_ERROR)
    anchor_expected = {
        "entry_sha256": final_entry.get("entry_sha256"),
        "ledger_id": final_entry.get("ledger_id"),
        "entry_type": "closure_evidence_anchored",
        "sequence": final_entry.get("sequence"),
        "previous_head_sha256": final_entry.get("previous_head_sha256"),
        "resulting_head_sha256": final_entry.get("resulting_head_sha256"),
        "entry_occurred_at_utc": final_entry.get("occurred_at_utc"),
    }
    if any(final_anchor.get(name) != value for name, value in anchor_expected.items()):
        _reject(_EXTERNAL_ANCHOR_ERROR)

    issued = _timestamp(
        receipt.get("receipt_issued_at_utc"),
        error_code=_RECEIPT_PROJECTION_ERROR,
    )
    receipt_observed = _timestamp(
        receipt_observation.get("observed_at_utc"),
        error_code=_EXTERNAL_ANCHOR_ERROR,
    )
    entry_time = _timestamp(
        final_entry.get("occurred_at_utc"), error_code=_EXTERNAL_ANCHOR_ERROR
    )
    anchor_observed = _timestamp(
        final_anchor.get("observed_at_utc"), error_code=_EXTERNAL_ANCHOR_ERROR
    )
    try:
        valid_until_raw = envelope["valid_until_utc"]
    except (KeyError, TypeError):
        _reject(_EXTERNAL_ANCHOR_ERROR)
    valid_until = _timestamp(valid_until_raw, error_code=_EXTERNAL_ANCHOR_ERROR)
    if not (
        issued <= receipt_observed <= entry_time
        and issued < entry_time <= anchor_observed <= valid_until
    ):
        _reject(_EXTERNAL_ANCHOR_ERROR)


def _receipt_unsigned_body(receipt: Mapping[str, Any]) -> bytes:
    body = dict(receipt)
    body.pop("document_sha256", None)
    body.pop("signatures", None)
    try:
        return canonical_json_bytes(body)
    except ExternalClosurePrimitiveError:
        _reject(_RECEIPT_PROJECTION_ERROR)


def _rerun_normal_projection(
    *,
    project_root: Path,
    documents: FinalDocumentBytes,
    pre_observation: bytes,
    artifact_directory: Path | None,
    postrun_attested_facts: bytes,
    receipt: Mapping[str, Any],
    admission_evidence: object | None = None,
) -> ReceiptProjectionReady:
    try:
        extra = {} if admission_evidence is None else {"admission_evidence": admission_evidence}
        result = _default_rerun_evaluator(
            project_root,
            documents.pre_receipt,
            external_observation_bundle=pre_observation,
            artifact_directory=artifact_directory,
            postrun_attested_facts=postrun_attested_facts,
            **extra,
        )
    except Exception:
        _reject(_RECEIPT_PROJECTION_ERROR)
    if type(result) is not ReceiptProjectionReady:
        _reject(_RECEIPT_PROJECTION_ERROR)
    if (
        result.unsigned_receipt_canonical_json != _receipt_unsigned_body(receipt)
        or result.receipt_document_sha256 != receipt.get("document_sha256")
        or result.evidence_valid != receipt.get("evidence_valid")
        or result.pre_anchor_closure_eligible
        != receipt.get("pre_anchor_closure_eligible")
        or result.evidence_error_code != receipt.get("evidence_error_code")
        or result.closure_reasons != tuple(receipt.get("closure_reasons", ()))
    ):
        _reject(_RECEIPT_PROJECTION_ERROR)
    return result


def verify_closed_campaign(
    project_root: Path,
    documents: FinalDocumentBytes,
    *,
    external_observation_bundle: bytes,
    artifact_directory: Path | None,
    postrun_attested_facts: bytes,
    admission_evidence: object | None = None,
) -> FinalClosureResult:
    """Verify a signed closure receipt and its independently witnessed anchor.

    Verification failures are returned as ``unclosed_invalid`` with one of the
    four error codes frozen by T6-C. Normal publication invokes the fixed
    layer-A evaluator exactly once and requires an exact unsigned projection.
    Quarantine never invokes it or inspects artifact/post-run-facts inputs.
    """

    try:
        if type(documents) is not FinalDocumentBytes:
            _reject(_RECEIPT_SCHEMA_ERROR)
        schemas = _load_schemas(project_root)
        receipt = _decode_schema_document(
            documents.closure_receipt,
            schemas=schemas,
            schema_name="closure_receipt_v1.schema.json",
            error_code=_RECEIPT_SCHEMA_ERROR,
        )
        try:
            verify_document_sha256(receipt)
        except ExternalClosurePrimitiveError:
            _reject(_RECEIPT_SIGNATURE_ERROR)

        observation = _decode_schema_document(
            external_observation_bundle,
            schemas=schemas,
            schema_name="external_observation_bundle_v1.schema.json",
            error_code=_EXTERNAL_ANCHOR_ERROR,
        )
        if observation.get("mode") != "final":
            _reject(_EXTERNAL_ANCHOR_ERROR)
        final_entry = _decode_schema_document(
            documents.closure_evidence_anchored_ledger_entry,
            schemas=schemas,
            schema_name="external_ledger_entry_v1.schema.json",
            error_code=_EXTERNAL_ANCHOR_ERROR,
        )
        pre_observation = _pre_mode_observation(observation)
        pre_summary = _verify_pre_history(
            documents, schemas=schemas, pre_observation=pre_observation
        )
        pre_documents = {
            name: _decode_verified_pre_document(getattr(documents.pre_receipt, name))
            for name in (
                "trust_manifest",
                "candidate_freeze_receipt",
                "preregistration_envelope",
                "preregistration_frozen_ledger_entry",
                "authorization_grant",
                "consumption_receipt",
                "authorization_consumed_ledger_entry",
            )
        }
        _verify_receipt_hash_and_signature(
            receipt, trust=pre_documents["trust_manifest"]
        )
        _verify_receipt_projection(
            receipt,
            pre_summary=pre_summary,
            pre_documents=pre_documents,
            observation=observation,
        )
        _verify_final_entry_and_observation(
            final_entry,
            receipt=receipt,
            pre_summary=pre_summary,
            pre_documents=pre_documents,
            observation=observation,
        )

        receipt_hash = receipt["document_sha256"]
        final_hash = final_entry["entry_sha256"]
        if receipt.get("artifact_publication_disposition") != "normal":
            return FinalClosureResult(
                "closed_failure_attested",
                False,
                False,
                None,
                receipt_hash,
                final_hash,
            )

        rerun = _rerun_normal_projection(
            project_root=project_root,
            documents=documents,
            pre_observation=pre_observation,
            artifact_directory=artifact_directory,
            postrun_attested_facts=postrun_attested_facts,
            receipt=receipt,
            admission_evidence=admission_evidence,
        )
        if not rerun.evidence_valid:
            return FinalClosureResult(
                "closed_failure_attested",
                False,
                False,
                None,
                receipt_hash,
                final_hash,
            )
        return FinalClosureResult(
            "closed_evidence_verified",
            True,
            rerun.pre_anchor_closure_eligible,
            None,
            receipt_hash,
            final_hash,
        )
    except _FinalVerificationFailure as failure:
        return _invalid(failure.code)


__all__ = ["verify_closed_campaign"]
