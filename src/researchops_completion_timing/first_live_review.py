"""Timed first-live review v2: signature linkage, not source/runtime admission."""
from __future__ import annotations

import re

from researchops_external_closure.admission_contract import CONTRACT_SHA256 as ADMISSION_SHA256, load_admission_link_contract
from researchops_external_closure.documents import _assert_role_time, _role_map, _verify_signatures
from researchops_external_closure.io import read_regular_file_no_follow, scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object, parse_utc_timestamp, verify_document_sha256, verify_ed25519_signature
from . import contract as core
from . import first_live_artifacts as archive
from .artifacts import _sha


PROFILE_SHA256 = "240dbe57b868eaebbda40a586728b0de66ad399070537b8886ce1566c06cde93"
_DIRECTORY = "evals/provider_completion_first_live_review_v2"
_TYPE = "completion_telemetry_timed_first_live_review"
_REVIEW_DOMAIN = "researchops-provider-completion-timed-first-live-review-v2"
_SIGNATURE_DOMAIN = "researchops-provider-completion-timed-first-live-review-signature-v2"


def load_first_live_review_contract(root):
    parent = load_admission_link_contract(root)
    archive.load_first_live_artifact_contract(root)
    payload = read_regular_file_no_follow(root / _DIRECTORY / "contract_v2.json", max_bytes=4096)
    if len(payload) != 2060 or core.digest(payload) != PROFILE_SHA256:
        core.fail("first_live_review_contract_invalid")
    profile = decode_strict_json_object(payload, max_bytes=4096)
    if (profile["predecessor_admission_contract_sha256"] != ADMISSION_SHA256
        or profile["first_live_artifact_contract_sha256"] != archive.PROFILE_SHA256
        or profile["document_type"] != _TYPE or profile["review_domain"] != _REVIEW_DOMAIN
        or profile["signature_domain"] != _SIGNATURE_DOMAIN):
        core.fail("first_live_review_contract_invalid")
    spec = profile["review_schema"]
    payload = read_regular_file_no_follow(root / _DIRECTORY / spec["name"], max_bytes=8192)
    if len(payload) != spec["bytes"] or core.digest(payload) != spec["sha256"]:
        core.fail("first_live_review_contract_invalid")
    return parent, decode_strict_json_object(payload, max_bytes=8192)


def review_document_sha256(document):
    if type(document) is not dict or document.get("document_type") != _TYPE:
        core.fail("first_live_review_document_invalid")
    body = dict(document); body.pop("document_sha256", None); body.pop("signatures", None)
    return core.digest(_REVIEW_DOMAIN.encode() + b"\0" + raw(body))


def review_signature_message(key_id, document_sha256):
    _sha(document_sha256)
    if type(key_id) is not str or re.fullmatch(r"PCEKEY-[A-F0-9]{32}", key_id) is None:
        core.fail("first_live_review_signature_binding_invalid")
    return b"\0".join((_SIGNATURE_DOMAIN.encode(), _TYPE.encode(), b"freeze_authority", key_id.encode(), bytes.fromhex(document_sha256)))


def verify_timed_first_live_review(root, *, review_bytes, trust_bytes, observation_bytes,
        expected_review_document_sha256, expected_trust_manifest_sha256,
        expected_candidate_freeze_receipt_sha256, expected_candidate_frozen_at_utc,
        expected_subject, expected_first_live_evidence_commitment_sha256,
        expected_evidence_commit, expected_evidence_tree, expected_first_live_completed_at_utc,
        sensitive_canaries=()):
    """Verify supplied independent expectations; do not mint them from the review."""
    parent, schema = load_first_live_review_contract(root)
    canaries = tuple(sensitive_canaries)
    review = core._decode(review_bytes, schema, 32768, canaries)
    trust = parent.validate("external_trust_manifest_v1.schema.json", trust_bytes, max_bytes=65536)
    observed = parent.validate("external_review_observation_v1.schema.json", observation_bytes, max_bytes=32768)
    scan_public_artifact_bytes((review_bytes, trust_bytes, observation_bytes, raw(review), raw(trust), raw(observed)), sensitive_canaries=canaries)
    if type(expected_subject) is not dict:
        core.fail("first_live_review_subject_mismatch")
    for value in (expected_review_document_sha256, expected_trust_manifest_sha256,
                  expected_candidate_freeze_receipt_sha256, expected_first_live_evidence_commitment_sha256):
        _sha(value)
    review_hash = review_document_sha256(review)
    if review["document_sha256"] != review_hash:
        core.fail("first_live_review_hash_mismatch")
    verify_document_sha256(trust)
    if (expected_review_document_sha256 != review_hash or observed["review_document_sha256"] != review_hash
        or expected_trust_manifest_sha256 != trust["document_sha256"] or observed["trust_manifest_sha256"] != trust["document_sha256"]
        or observed["candidate_freeze_receipt_sha256"] != expected_candidate_freeze_receipt_sha256
        or observed["candidate_frozen_at_utc"] != expected_candidate_frozen_at_utc):
        core.fail("first_live_review_external_binding_mismatch")
    if (raw(review["subject"]) != raw(expected_subject)
        or review["first_live_evidence_commitment_sha256"] != expected_first_live_evidence_commitment_sha256
        or type(expected_evidence_commit) is not str or review["evidence_commit"] != expected_evidence_commit
        or type(expected_evidence_tree) is not str or review["evidence_tree"] != expected_evidence_tree
        or type(expected_first_live_completed_at_utc) is not str or review["first_live_completed_at_utc"] != expected_first_live_completed_at_utc):
        core.fail("first_live_review_subject_mismatch")
    roles = _role_map(trust)
    created, valid_from, expires = (parse_utc_timestamp(trust[name]) for name in ("created_at_utc", "valid_from_utc", "expires_at_utc"))
    reviewed = parse_utc_timestamp(review["reviewed_at_utc"])
    if not (created <= parse_utc_timestamp(observed["trust_observed_at_utc"]) <= reviewed
        and parse_utc_timestamp(expected_first_live_completed_at_utc) <= parse_utc_timestamp(review["evidence_observed_at_utc"])
        <= reviewed <= parse_utc_timestamp(observed["review_observed_at_utc"]) < parse_utc_timestamp(expected_candidate_frozen_at_utc)):
        core.fail("first_live_review_timeline_invalid")
    _verify_signatures(trust, expected_roles=("freeze_authority", "task_custodian", "ledger_witness"),
        roles=roles, action_time=created, trust_valid_from=valid_from, trust_expires=expires)
    role = roles["freeze_authority"]
    _assert_role_time(role, action_time=reviewed, trust_valid_from=valid_from, trust_expires=expires)
    signature = review["signatures"][0]
    if signature["key_id"] != role["key_id"] or signature["signed_sha256"] != review_hash:
        core.fail("first_live_review_signature_binding_invalid")
    verify_ed25519_signature(public_key_b64=role["public_key_b64"], signature_b64=signature["signature_b64"],
        message=review_signature_message(role["key_id"], review_hash))
    return dict(status="timed_first_live_review_link_verified", review_document_sha256=review_hash,
        first_live_evidence_commitment_sha256=expected_first_live_evidence_commitment_sha256, decision=review["decision"],
        review_signature_verified=True, external_review_digest_matched=True, subject_matches_supplied_expectations=True,
        artifact_integrity_verified=False, historical_source_equality_verified=False, registry_admission_verified=False,
        actual_live_execution_verified=False, runtime_authority_granted=False, first_live_success_verified=False,
        current_closure_claim_allowed=False, provider_calls=0, real_key_loads=0)
