"""Direct first-live authorization/claim linkage, not runtime or live proof."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import re

from researchops_external_closure.io import read_regular_file_no_follow, scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes, decode_strict_json_object, parse_utc_timestamp
from . import contract as core
from . import first_live
from . import local_claim


PROFILE_SHA256 = "ad7915042f33da62f608827489b921a31a87ebb601f26b0efcae3704cd224245"
_DIRECTORY = "evals/provider_completion_first_live_control_v1"
_DOMAIN = "researchops-provider-completion-first-live-direct-authorization-v1"
_TOKEN = object()


def load_control_contract(root):
    first_live.load_first_live_timing_contract(root)
    raw = read_regular_file_no_follow(root / _DIRECTORY / "contract_v1.json", max_bytes=4096)
    if len(raw) != 2474 or core.digest(raw) != PROFILE_SHA256:
        core.fail("first_live_control_contract_invalid")
    profile = decode_strict_json_object(raw, max_bytes=4096)
    if (profile["first_live_timing_profile_sha256"] != first_live.PROFILE_SHA256
        or profile["local_claim_contract_sha256"] != local_claim.CONTRACT_SHA256
        or profile["authorization_binding_domain"] != _DOMAIN):
        core.fail("first_live_control_contract_invalid")
    schemas = {}
    for item in profile["schemas"]:
        raw = read_regular_file_no_follow(root / _DIRECTORY / item["name"], max_bytes=8192)
        if len(raw) != item["bytes"] or core.digest(raw) != item["sha256"]:
            core.fail("first_live_control_contract_invalid")
        schemas[item["name"]] = decode_strict_json_object(raw, max_bytes=8192)
    return profile, schemas


def authorization_binding(document):
    body = dict(document); body.pop("authorization_binding_sha256", None)
    return core.digest(_DOMAIN.encode() + b"\0" + canonical_json_bytes(body))


def _duration(start, end):
    difference = end.whole_second_utc - start.whole_second_utc
    def fractional(value):
        return Fraction(int(value.fraction_digits or "0"), 10 ** len(value.fraction_digits))
    return Fraction(difference.days * 86400 + difference.seconds) + fractional(end) - fractional(start)


@dataclass(frozen=True, slots=True, init=False)
class VerifiedFirstLiveAuthorization:
    authorization_binding_sha256: str
    authorization_document_sha256: str
    authorization_id_sha256: str
    execution_environment_id: str
    timing_plan_commitment_sha256: str
    execution_commit: str
    authorized_at_utc: str
    expires_at_utc: str

    def __init__(self, *args, **kwargs):
        raise TypeError("VerifiedFirstLiveAuthorization requires independent binding verification")

    @classmethod
    def _create(cls, token, **values):
        if token is not _TOKEN or set(values) != set(cls.__dataclass_fields__):
            raise TypeError("invalid first-live control proof")
        result = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(result, name, value)
        return result


def verify_first_live_authorization(root, *, plan_bytes, authorization_bytes, expected_authorization_binding_sha256, verification_time_utc):
    """Verify at a supplied time; computing the expected digest is not consent."""
    if type(expected_authorization_binding_sha256) is not str or re.fullmatch(r"(?!0{64}$)[0-9a-f]{64}", expected_authorization_binding_sha256) is None:
        core.fail("first_live_authorization_binding_mismatch")
    profile, schemas = load_control_contract(root)
    _, parent, timing_schemas = first_live.load_first_live_timing_contract(root)
    plan = core._decode(plan_bytes, timing_schemas["completion_timing_plan_v1.schema.json"], parent["limits"]["max_plan_bytes"], ())
    auth = core._decode(authorization_bytes, schemas["authorization_v1.schema.json"], 8192, ())
    if (canonical_json_bytes(auth) != authorization_bytes or auth["authorization_binding_sha256"] != authorization_binding(auth)
        or auth["authorization_binding_sha256"] != expected_authorization_binding_sha256):
        core.fail("first_live_authorization_binding_mismatch")
    if (plan["first_live_profile_sha256"] != first_live.PROFILE_SHA256
        or plan["timing_contract_sha256"] != core.CONTRACT_SHA256
        or plan["plan_commitment_sha256"] != first_live.commitment("plan", plan)
        or plan["binding"]["validation_run_id"] != first_live.validation_run_id(plan["binding"])
        or auth["timing_plan_commitment_sha256"] != plan["plan_commitment_sha256"]
        or auth["execution_environment_id"] != plan["binding"]["execution_environment_id"]
        or auth["authorization_id_sha256"] != plan["binding"]["authorization_id_sha256"]):
        core.fail("first_live_authorization_plan_mismatch")
    start, end, now = (parse_utc_timestamp(value) for value in (auth["authorized_at_utc"], auth["expires_at_utc"], verification_time_utc))
    if not 330 <= _duration(start, end) <= 86400 or not start <= now < end:
        core.fail("first_live_authorization_time_invalid")
    retrieved = parse_utc_timestamp(auth["pricing_retrieved_at_utc"])
    if (retrieved > start or auth["pricing_snapshot_date"] != auth["authorized_at_utc"][:10]
        or auth["pricing_snapshot_date"] != auth["pricing_retrieved_at_utc"][:10]):
        core.fail("first_live_authorization_pricing_invalid")
    input_price = Fraction(Decimal(auth["input_price_per_million_cny"]))
    output_price = Fraction(Decimal(auth["output_price_per_million_cny"]))
    if input_price <= 0 or output_price <= 0 or (1024 * input_price + 272 * output_price) / 1_000_000 > 1:
        core.fail("first_live_authorization_cost_reservation_invalid")
    return VerifiedFirstLiveAuthorization._create(_TOKEN,
        authorization_binding_sha256=auth["authorization_binding_sha256"], authorization_document_sha256=core.digest(authorization_bytes),
        authorization_id_sha256=auth["authorization_id_sha256"], execution_environment_id=auth["execution_environment_id"],
        timing_plan_commitment_sha256=plan["plan_commitment_sha256"], execution_commit=plan["binding"]["execution_commit"],
        authorized_at_utc=auth["authorized_at_utc"], expires_at_utc=auth["expires_at_utc"])


def build_first_live_claim_intent(root, proof, *, clock_domain_id, intended_at_utc):
    if type(proof) is not VerifiedFirstLiveAuthorization:
        core.fail("first_live_authorization_proof_invalid")
    _, schemas = load_control_contract(root)
    intent = dict(schema_version="provider-completion-first-live-claim-intent/1.0", status="intent_only_not_reserved",
        authorization_id_sha256=proof.authorization_id_sha256, execution_environment_id=proof.execution_environment_id,
        authorization_binding_sha256=proof.authorization_binding_sha256, authorization_document_sha256=proof.authorization_document_sha256,
        timing_plan_commitment_sha256=proof.timing_plan_commitment_sha256, clock_domain_id=clock_domain_id,
        intended_at_utc=intended_at_utc, task_or_key_use_authorized=False)
    raw = canonical_json_bytes(intent)
    core._decode(raw, schemas["claim_intent_v1.schema.json"], 4096, ())
    if not parse_utc_timestamp(proof.authorized_at_utc) <= parse_utc_timestamp(intended_at_utc) < parse_utc_timestamp(proof.expires_at_utc):
        core.fail("first_live_claim_intent_time_invalid")
    request = dict(schema_version="provider-completion-local-claim-request/1.0", execution_environment_id=proof.execution_environment_id,
        authorization_id_sha256=proof.authorization_id_sha256, authorization_grant_sha256=proof.authorization_document_sha256,
        consumption_entry_sha256=core.digest(raw), timing_plan_commitment_sha256=proof.timing_plan_commitment_sha256,
        execution_commit=proof.execution_commit, clock_domain_id=clock_domain_id)
    return raw, canonical_json_bytes(request)


def verify_first_live_claim_link(root, proof, *, intent_bytes, claim_receipt_bytes, expected_claim_receipt_sha256):
    """Verify supplied receipt linkage, not the protected store or runtime authority."""
    if type(proof) is not VerifiedFirstLiveAuthorization:
        core.fail("first_live_authorization_proof_invalid")
    if type(expected_claim_receipt_sha256) is not str or re.fullmatch(r"(?!0{64}$)[0-9a-f]{64}", expected_claim_receipt_sha256) is None:
        core.fail("first_live_claim_link_mismatch")
    _, schemas = load_control_contract(root)
    intent = core._decode(intent_bytes, schemas["claim_intent_v1.schema.json"], 4096, ())
    expected_intent, expected_request = build_first_live_claim_intent(root, proof,
        clock_domain_id=intent["clock_domain_id"], intended_at_utc=intent["intended_at_utc"])
    if intent_bytes != expected_intent or core.digest(claim_receipt_bytes) != expected_claim_receipt_sha256:
        core.fail("first_live_claim_link_mismatch")
    receipt = decode_strict_json_object(claim_receipt_bytes, max_bytes=4096)
    scan_public_artifact_bytes((claim_receipt_bytes, canonical_json_bytes(receipt)))
    if canonical_json_bytes(receipt) != claim_receipt_bytes:
        core.fail("first_live_claim_link_mismatch")
    request = decode_strict_json_object(expected_request, max_bytes=4096)
    expected = dict(request, schema_version="provider-completion-local-claim/1.0", status="reserved",
        claimed_at_utc=receipt.get("claimed_at_utc"), claim_contract_sha256=local_claim.CONTRACT_SHA256,
        store_scope="single_host_fixed_store", provider_actions_authorized=False)
    if canonical_json_bytes(receipt) != canonical_json_bytes(expected):
        core.fail("first_live_claim_link_mismatch")
    claimed = parse_utc_timestamp(receipt["claimed_at_utc"])
    if not parse_utc_timestamp(intent["intended_at_utc"]) <= claimed < parse_utc_timestamp(proof.expires_at_utc):
        core.fail("first_live_claim_time_invalid")
    return {"status": "first_live_control_link_verified", "authorization_binding_sha256": proof.authorization_binding_sha256,
            "claim_receipt_sha256": core.digest(claim_receipt_bytes), "clock_domain_id": intent["clock_domain_id"],
            "local_store_entry_independently_verified": False, "source_admission_verified": False,
            "runtime_authority_granted": False, "first_live_success_verified": False, "provider_calls": 0, "real_key_loads": 0}


def verify_first_live_stored_claim_link(root, proof, *, intent_bytes, expected_claim_receipt_sha256):
    """Read the fixed store, not a caller receipt; evidence only, not recovery."""
    if type(proof) is not VerifiedFirstLiveAuthorization:
        core.fail("first_live_authorization_proof_invalid")
    _, schemas = load_control_contract(root)
    intent = core._decode(intent_bytes, schemas["claim_intent_v1.schema.json"], 4096, ())
    expected_intent, request = build_first_live_claim_intent(root, proof,
        clock_domain_id=intent["clock_domain_id"], intended_at_utc=intent["intended_at_utc"])
    if intent_bytes != expected_intent:
        core.fail("first_live_claim_link_mismatch")
    receipt = local_claim.read_reserved_local_claim(request, expected_receipt_sha256=expected_claim_receipt_sha256)
    checked = verify_first_live_claim_link(root, proof, intent_bytes=intent_bytes,
        claim_receipt_bytes=receipt, expected_claim_receipt_sha256=expected_claim_receipt_sha256)
    return dict(checked, status="first_live_fixed_store_claim_link_verified",
        local_store_entry_independently_verified=True, store_verification_scope="readonly_snapshot_only",
        successful_reservation_return_proved=False, retry_or_resume_authorized=False)


def verify_first_live_controlled_timing(root, *, plan_bytes, authorization_bytes,
        expected_authorization_binding_sha256, verification_time_utc, intent_bytes,
        claim_receipt_bytes, expected_claim_receipt_sha256, evidence_bytes,
        record_bytes, terminal_projection_bytes, publication_bytes=None,
        expected_manifest_file_sha256=None, sensitive_canaries=()):
    """Join control, record and timing data; supplied projections are not a ledger.

    This is an offline conformance check, not a fresh-time execution gate. A
    matching claim receipt does not prove protected-store persistence, and the
    caller's terminal projection cannot establish actual execution provenance.
    """
    # Snapshot the caller's canary iterable once and apply it to *all* documents,
    # including control metadata previously checked only against generic rules.
    canaries = tuple(sensitive_canaries)
    controls = (plan_bytes, authorization_bytes, intent_bytes, claim_receipt_bytes)
    for payload in controls:
        value = decode_strict_json_object(payload, max_bytes=131072)
        scan_public_artifact_bytes((payload, canonical_json_bytes(value)), sensitive_canaries=canaries)
    proof = verify_first_live_authorization(root, plan_bytes=plan_bytes,
        authorization_bytes=authorization_bytes,
        expected_authorization_binding_sha256=expected_authorization_binding_sha256,
        verification_time_utc=verification_time_utc)
    claim = verify_first_live_claim_link(root, proof, intent_bytes=intent_bytes,
        claim_receipt_bytes=claim_receipt_bytes, expected_claim_receipt_sha256=expected_claim_receipt_sha256)
    timing = first_live.validate_first_live_timing_documents(root, plan_bytes=plan_bytes,
        evidence_bytes=evidence_bytes, terminal_projection_bytes=terminal_projection_bytes,
        publication_bytes=publication_bytes, expected_manifest_file_sha256=expected_manifest_file_sha256,
        sensitive_canaries=canaries)
    shapes = first_live.verify_first_live_record_shapes(root, record_bytes, sensitive_canaries=canaries)
    evidence = decode_strict_json_object(evidence_bytes, max_bytes=4194304)
    records = decode_strict_json_object(record_bytes, max_bytes=262144)["records"]
    if (evidence["binding"]["authorization_binding_sha256"] != proof.authorization_binding_sha256
        or evidence["binding"]["local_claim_receipt_sha256"] != claim["claim_receipt_sha256"]
        or evidence["clock_domain_id"] != claim["clock_domain_id"]):
        core.fail("first_live_control_timing_binding_mismatch")
    segments = evidence["segments"]
    # The shape-success path requires the full pair. Do not silently zip away a
    # missing/extra response, or treat a failure receipt as an accepted record.
    if len(segments) != len(records):
        core.fail("first_live_control_record_coverage_mismatch")
    for segment, record in zip(segments, records):
        if (segment["terminal_kind"] != "response_accepted"
            or segment["request_index"] != record["request_index"]
            or segment["response_index"] != record["response_index"]
            or segment["completion_record_sha256"] != core.digest(canonical_json_bytes(record))
            or segment["usage_sha256"] != core.digest(canonical_json_bytes(record["usage"]))):
            core.fail("first_live_control_record_binding_mismatch")
    return {"status": "first_live_controlled_timing_consistent",
        "scope": "supplied_control_record_and_timing_conformance_only",
        "authorization_binding_sha256": proof.authorization_binding_sha256,
        "plan_commitment_sha256": proof.timing_plan_commitment_sha256,
        "claim_receipt_sha256": claim["claim_receipt_sha256"],
        "timing_evidence_sha256": core.digest(evidence_bytes), "record_count": shapes["record_count"],
        "timing_gate_complete": timing["timing_gate_complete"], "incomplete_reasons": timing["incomplete_reasons"],
        "local_store_entry_independently_verified": False, "authoritative_ledger_verified": False,
        "actual_clock_capture_proved": False, "fresh_execution_time_verified": False,
        "source_admission_verified": False, "runtime_authority_granted": False,
        "first_live_success_verified": False, "current_closure_claim_allowed": False,
        "provider_calls": 0, "real_key_loads": 0}
