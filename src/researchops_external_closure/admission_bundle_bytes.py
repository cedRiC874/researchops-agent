"""Pure byte/commitment checks. File integrity is not first-live success."""

from __future__ import annotations

import hashlib
from types import MappingProxyType

from .admission_contract import FrozenAdmissionLinkContract
from .errors import ExternalClosurePrimitiveError
from .io import scan_public_artifact_bytes
from .primitives import canonical_json_bytes


_DOMAINS = {
    "completion_telemetry_first_live_evidence_bundle": "evidence_bundle",
    "completion_telemetry_runtime_registry_admission_bundle": "registry_bundle",
}


def admission_bundle_commitment(contract: FrozenAdmissionLinkContract, document: dict) -> str:
    if type(contract) is not FrozenAdmissionLinkContract or type(document) is not dict:
        raise ExternalClosurePrimitiveError("admission_bundle_input_invalid")
    kind = document.get("document_type")
    if type(kind) is not str or kind not in _DOMAINS:
        raise ExternalClosurePrimitiveError("admission_bundle_type_invalid")
    body = dict(document)
    body.pop("commitment_sha256", None)
    domain = contract.document()["byte_protocol"]["domain_values"][_DOMAINS[kind]]
    return hashlib.sha256(domain.encode() + b"\0" + canonical_json_bytes(body)).hexdigest()


def validate_first_live_bundle_envelope(
    contract: FrozenAdmissionLinkContract, bundle_bytes: bytes, *,
    expected_commitment_sha256: str, expected_authorization_binding_sha256: str,
    expected_subject: dict,
) -> dict:
    """Check caller-held expectations before any artifact-directory access."""
    document = contract.validate("first_live_evidence_bundle_v1.schema.json", bundle_bytes,
                                 max_bytes=contract.document()["limits"]["evidence_bundle_bytes"])
    if (
        type(expected_commitment_sha256) is not str
        or document["commitment_sha256"] != expected_commitment_sha256
        or admission_bundle_commitment(contract, document) != expected_commitment_sha256
        or type(expected_authorization_binding_sha256) is not str
        or document["external_authorization_binding_sha256"] != expected_authorization_binding_sha256
        or type(expected_subject) is not dict
        or canonical_json_bytes(document["subject"]) != canonical_json_bytes(expected_subject)
    ):
        raise ExternalClosurePrimitiveError("admission_bundle_external_binding_mismatch")
    scan_public_artifact_bytes((bundle_bytes, canonical_json_bytes(document)))
    return document


def verify_first_live_bundle_bytes(
    contract: FrozenAdmissionLinkContract, bundle_bytes: bytes, files: dict[str, bytes], *,
    expected_commitment_sha256: str, expected_authorization_binding_sha256: str,
    expected_subject: dict,
) -> dict:
    """Return a defensive manifest projection, never an execution/admission verdict."""
    document=validate_first_live_bundle_envelope(contract,bundle_bytes,
        expected_commitment_sha256=expected_commitment_sha256,
        expected_authorization_binding_sha256=expected_authorization_binding_sha256,
        expected_subject=expected_subject)
    names = contract.document()["evidence_file_order"]
    if type(files) not in (dict, MappingProxyType) or set(files) != set(names):
        raise ExternalClosurePrimitiveError("admission_bundle_file_set_invalid")
    files = dict(files)
    total = 0
    for row in document["artifact_files"]:
        raw = files[row["name"]]
        if type(raw) is not bytes:
            raise ExternalClosurePrimitiveError("admission_bundle_bytes_required")
        total += len(raw)
        if total > contract.document()["limits"]["artifact_total_bytes"]:
            raise ExternalClosurePrimitiveError("admission_bundle_size_limit")
        if type(row["bytes"]) is not int or row["bytes"] != len(raw) or row["sha256"] != hashlib.sha256(raw).hexdigest():
            raise ExternalClosurePrimitiveError("admission_bundle_file_hash_mismatch")
    scan_public_artifact_bytes((bundle_bytes, canonical_json_bytes(document), *files.values()))
    return document


__all__ = ["admission_bundle_commitment", "validate_first_live_bundle_envelope", "verify_first_live_bundle_bytes"]
