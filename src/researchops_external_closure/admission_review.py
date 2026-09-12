"""Pure review-signature linkage. A valid review is not valid live evidence.

All observations and expectations are supplied out of band by the caller.
This module has no file/Git/clock/Provider access, signer or runtime factory.
The outer verifier must derive expected_subject from real evidence and Git,
not use a subject copied from this review as its own expectation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .admission_contract import FrozenAdmissionLinkContract
from .documents import _assert_role_time, _role_map, _verify_signatures
from .errors import ExternalClosurePrimitiveError
from .io import scan_public_artifact_bytes
from .primitives import canonical_json_bytes, parse_utc_timestamp, verify_document_sha256, verify_ed25519_signature


_HASH = re.compile(r"(?!0{64}\Z)[0-9a-f]{64}\Z")
_KEY = re.compile(r"PCEKEY-[A-F0-9]{32}\Z")
_TYPE = "completion_telemetry_first_live_review"


def _fail(code: str):
    raise ExternalClosurePrimitiveError("admission_review_" + code) from None


def _hash(value: object) -> str:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        _fail("external_binding_invalid")
    return value


def review_document_sha256(contract: FrozenAdmissionLinkContract, document: dict) -> str:
    if type(contract) is not FrozenAdmissionLinkContract or type(document) is not dict:
        _fail("input_invalid")
    if document.get("document_type") != _TYPE:
        _fail("input_invalid")
    body = dict(document)
    body.pop("document_sha256", None)
    body.pop("signatures", None)
    domain = contract.document()["byte_protocol"]["domain_values"]["review"]
    return hashlib.sha256(domain.encode() + b"\0" + canonical_json_bytes(body)).hexdigest()


def review_signature_message(contract: FrozenAdmissionLinkContract, key_id: str, document_sha256: str) -> bytes:
    if type(contract) is not FrozenAdmissionLinkContract or type(key_id) is not str or _KEY.fullmatch(key_id) is None:
        _fail("input_invalid")
    domain = contract.document()["byte_protocol"]["domain_values"]["review_signature"]
    return b"\0".join((domain.encode(), _TYPE.encode(), b"freeze_authority", key_id.encode(), bytes.fromhex(_hash(document_sha256))))


@dataclass(frozen=True, slots=True)
class ReviewLinkResult:
    review_document_sha256: str
    first_live_evidence_commitment_sha256: str
    decision: str
    review_signature_verified: bool = True
    subject_matches_supplied_expectations: bool = True
    # These MUST NOT be inferred from a signature or a caller-supplied subject.
    first_live_artifact_integrity_verified: bool = False
    git_source_completeness_verified: bool = False
    registry_admission_verified: bool = False
    runtime_authority_granted: bool = False
    closure_claim_allowed: bool = False


def verify_review_link(
    contract: FrozenAdmissionLinkContract, *, review_bytes: bytes, trust_bytes: bytes,
    observation_bytes: bytes, expected_review_document_sha256: str,
    expected_trust_manifest_sha256: str, expected_candidate_freeze_receipt_sha256: str,
    expected_candidate_frozen_at_utc: str, expected_subject: dict,
    expected_first_live_evidence_commitment_sha256: str,
    expected_evidence_commit: str, expected_evidence_tree: str,
) -> ReviewLinkResult:
    if type(contract) is not FrozenAdmissionLinkContract or type(expected_subject) is not dict:
        _fail("input_invalid")
    limits = contract.document()["limits"]
    review = contract.validate("first_live_review_record_v1.schema.json", review_bytes,
                               max_bytes=limits["review_document_bytes"])
    trust = contract.validate("external_trust_manifest_v1.schema.json", trust_bytes,
                              max_bytes=limits["trust_manifest_bytes"])
    observed = contract.validate("external_review_observation_v1.schema.json", observation_bytes,
                                 max_bytes=limits["review_observation_bytes"])
    # Scan decoded strings too: JSON Unicode escapes must not conceal a Key.
    scan_public_artifact_bytes((review_bytes, trust_bytes, observation_bytes,
                                canonical_json_bytes(review), canonical_json_bytes(trust),
                                canonical_json_bytes(observed)))
    review_hash = review_document_sha256(contract, review)
    if review["document_sha256"] != review_hash:
        _fail("hash_mismatch")
    verify_document_sha256(trust)
    if (
        _hash(expected_review_document_sha256) != review_hash
        or observed["review_document_sha256"] != review_hash
        or _hash(expected_trust_manifest_sha256) != trust["document_sha256"]
        or observed["trust_manifest_sha256"] != trust["document_sha256"]
        or observed["candidate_freeze_receipt_sha256"] != _hash(expected_candidate_freeze_receipt_sha256)
        or observed["candidate_frozen_at_utc"] != expected_candidate_frozen_at_utc
    ):
        _fail("external_binding_mismatch")
    if (
        canonical_json_bytes(review["subject"]) != canonical_json_bytes(expected_subject)
        or review["first_live_evidence_commitment_sha256"] != _hash(expected_first_live_evidence_commitment_sha256)
        or type(expected_evidence_commit) is not str or review["evidence_commit"] != expected_evidence_commit
        or type(expected_evidence_tree) is not str or review["evidence_tree"] != expected_evidence_tree
    ):
        _fail("subject_mismatch")

    roles = _role_map(trust)
    created = parse_utc_timestamp(trust["created_at_utc"])
    valid_from = parse_utc_timestamp(trust["valid_from_utc"])
    expires = parse_utc_timestamp(trust["expires_at_utc"])
    reviewed = parse_utc_timestamp(review["reviewed_at_utc"])
    if not (
        created <= parse_utc_timestamp(observed["trust_observed_at_utc"]) <= reviewed
        and parse_utc_timestamp(review["first_live_completed_at_utc"])
        <= parse_utc_timestamp(review["evidence_observed_at_utc"])
        <= reviewed <= parse_utc_timestamp(observed["review_observed_at_utc"])
        < parse_utc_timestamp(expected_candidate_frozen_at_utc)
    ):
        _fail("timeline_invalid")
    _verify_signatures(trust, expected_roles=("freeze_authority", "task_custodian", "ledger_witness"),
                       roles=roles, action_time=created, trust_valid_from=valid_from, trust_expires=expires)
    role = roles["freeze_authority"]
    _assert_role_time(role, action_time=reviewed, trust_valid_from=valid_from, trust_expires=expires)
    signature = review["signatures"][0]
    if signature["key_id"] != role["key_id"] or signature["signed_sha256"] != review_hash:
        _fail("signature_binding_invalid")
    verify_ed25519_signature(public_key_b64=role["public_key_b64"], signature_b64=signature["signature_b64"],
                             message=review_signature_message(contract, role["key_id"], review_hash))
    return ReviewLinkResult(review_hash, review["first_live_evidence_commitment_sha256"], review["decision"])


__all__ = ["ReviewLinkResult", "review_document_sha256", "review_signature_message", "verify_review_link"]
