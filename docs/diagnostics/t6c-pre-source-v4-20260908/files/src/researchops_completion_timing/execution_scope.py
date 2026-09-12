"""Signed single-host scope verification and claim-request derivation, no permit."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from researchops_external_closure.io import read_regular_file_no_follow
from researchops_external_closure.primitives import canonical_json_bytes, decode_strict_json_object
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from . import local_claim
from .contract import TimingContractError, digest, fail
from .pre_execution import PROFILE_SHA256 as PRE_EXECUTION_SHA256, VerifiedTimedPreExecutionDocuments, _verify
from researchops_external_closure.primitives import parse_utc_timestamp


PROFILE_PATH = Path("evals/provider_completion_execution_scope_v1")
PROFILE_SHA256 = "f200c361792fe87772c4314a6ce99d7c7340c61913c7440ee39a6e74f3b328cb"
_TOKEN = object()


def load_execution_scope_contract(root):
    raw = read_regular_file_no_follow(root / PROFILE_PATH / "contract_v1.json", max_bytes=4096)
    if len(raw) != 2042 or digest(raw) != PROFILE_SHA256:
        fail("execution_scope_contract_invalid")
    profile = decode_strict_json_object(raw, max_bytes=4096)
    if (profile["pre_execution_contract_sha256"] != PRE_EXECUTION_SHA256
        or profile["local_claim_contract_sha256"] != local_claim.CONTRACT_SHA256):
        fail("execution_scope_contract_invalid")
    local_claim._profile()  # Read only the frozen contract, never the real store.
    parent = read_regular_file_no_follow(root / "evals/provider_completion_local_claim_v1/contract_v1.json", max_bytes=8192)
    if digest(parent) != profile["local_claim_contract_sha256"]:
        fail("execution_scope_contract_invalid")
    item = profile["schema"]
    raw = read_regular_file_no_follow(root / PROFILE_PATH / item["path"], max_bytes=65536)
    if len(raw) != item["bytes"] or digest(raw) != item["sha256"]:
        fail("execution_scope_contract_invalid")
    return profile, decode_strict_json_object(raw, max_bytes=65536)


@dataclass(frozen=True, slots=True, init=False)
class VerifiedSingleHostScope:
    timing: VerifiedTimedPreExecutionDocuments
    execution_environment_id: str
    execution_scope_sha256: str
    authorization_id_sha256: str
    execution_commit: str

    def __init__(self, *args, **kwargs):
        raise TypeError("VerifiedSingleHostScope requires signed v3 verification")

    @classmethod
    def _create(cls, token, *, timing, execution_environment_id, execution_scope_sha256, authorization_id_sha256, execution_commit):
        if token is not _TOKEN or type(timing) is not VerifiedTimedPreExecutionDocuments:
            raise TypeError("invalid execution-scope proof")
        value = object.__new__(cls)
        for key, field in dict(timing=timing, execution_environment_id=execution_environment_id,
            execution_scope_sha256=execution_scope_sha256, authorization_id_sha256=authorization_id_sha256,
            execution_commit=execution_commit).items():
            object.__setattr__(value, key, field)
        return value

    def summary(self):
        return dict(self.timing.summary(), scope="signed_single_host_scope_at_supplied_time_only",
                    execution_environment_id=self.execution_environment_id, execution_scope_sha256=self.execution_scope_sha256,
                    local_store_identity_verified=False, one_shot_execution_claimed=False, runtime_authority_granted=False)


def verify_single_host_pre_execution(root, documents, *, external_observation_bundle, timing_plan_bytes, verification_time_utc):
    """Only v3 qualifies; no downgrade to a valid-but-unscoped v2 grant."""
    try:
        timing = _verify(root, documents, external_observation_bundle=external_observation_bundle,
                         timing_plan_bytes=timing_plan_bytes, verification_time_utc=verification_time_utc, _version=3)
        envelope = decode_strict_json_object(documents.preregistration_envelope, max_bytes=2_000_000)
        grant = decode_strict_json_object(documents.authorization_grant, max_bytes=2_000_000)
        scope = envelope["runtime_plan"]["execution_scope"]
        return VerifiedSingleHostScope._create(_TOKEN, timing=timing, execution_environment_id=scope["execution_environment_id"],
            execution_scope_sha256=digest(canonical_json_bytes(scope)),
            authorization_id_sha256=grant["explicit_user_authorization_id_sha256"], execution_commit=envelope["execution_binding"]["execution_commit"])
    except (TimingContractError, ExternalClosurePrimitiveError):
        raise
    except Exception:
        fail("execution_scope_invalid")


def build_scoped_claim_request(proof, *, clock_domain_id):
    """Derive metadata bytes only. A runtime factory must own the fresh domain."""
    if type(proof) is not VerifiedSingleHostScope or type(clock_domain_id) is not str or re.fullmatch(r"PCECLOCK-[A-F0-9]{32}", clock_domain_id) is None:
        fail("execution_scope_proof_invalid")
    return canonical_json_bytes(dict(schema_version="provider-completion-local-claim-request/1.0",
        execution_environment_id=proof.execution_environment_id, authorization_id_sha256=proof.authorization_id_sha256,
        authorization_grant_sha256=proof.timing.authorization_grant_sha256,
        consumption_entry_sha256=proof.timing.consumption_ledger_entry_sha256,
        timing_plan_commitment_sha256=proof.timing.timing_plan_commitment_sha256,
        execution_commit=proof.execution_commit, clock_domain_id=clock_domain_id))


def verify_scoped_store_identity(proof):
    """Readonly store match; neither provisioning nor a claim occurs here."""
    if type(proof) is not VerifiedSingleHostScope:
        fail("execution_scope_proof_invalid")
    state = local_claim.local_claim_store_status()
    if state["status"] != "ready" or state["execution_environment_id"] != proof.execution_environment_id:
        fail("execution_scope_store_mismatch")
    return {"status": "scope_store_matched", "execution_environment_id": proof.execution_environment_id,
            "one_shot_execution_claimed": False, "runtime_authority_granted": False, "current_closure_claim_allowed": False}


def _current_utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def verify_current_single_host_scope(root, documents, *, external_observation_bundle, timing_plan_bytes):
    """Fresh local-clock check; no caller as-of/clock argument or side effects.

    Still not a runtime permit: the eventual gateway must recheck immediately
    before irreversible claim/Key use, and enforce source/review/first-live gates.
    """
    try:
        started = _current_utc()
        proof = verify_single_host_pre_execution(root, documents, external_observation_bundle=external_observation_bundle,
            timing_plan_bytes=timing_plan_bytes, verification_time_utc=started)
        finished = _current_utc()
        first, last = parse_utc_timestamp(started), parse_utc_timestamp(finished)
        if last < first:
            fail("execution_scope_clock_reversed")
        envelope = decode_strict_json_object(documents.preregistration_envelope, max_bytes=2_000_000)
        trust = decode_strict_json_object(documents.trust_manifest, max_bytes=2_000_000)
        expiry = min(parse_utc_timestamp(proof.timing.grant_expires_at_utc),
                     parse_utc_timestamp(envelope["valid_until_utc"]), parse_utc_timestamp(trust["expires_at_utc"]))
        if last >= expiry:
            fail("execution_scope_expired_during_verification")
        return proof
    except (TimingContractError, ExternalClosurePrimitiveError):
        raise
    except Exception:
        fail("execution_scope_clock_unavailable")
