"""Readonly signed timing preregistration/consumption proof, never a permit.

The as-of time and OOB observations are explicit external inputs. A future
runtime gate must obtain its own current clock, verify source/review/first-live
admission and make a durable one-shot claim before releasing tasks or loading Key.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from researchops.provider_completion_external_contract import load_frozen_external_preregistration_contract
from researchops_external_closure import documents as inherited
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.io import read_regular_file_no_follow
from researchops_external_closure.primitives import canonical_json_bytes, decode_strict_json_object
from researchops_external_closure.types import PreReceiptDocumentBytes
from .contract import CONTRACT_SHA256, TimingContractError, _decode, commitment, digest, fail, load_contract, runtime_plan_core_commitment


PROFILE_PATH = Path("evals/provider_completion_pre_execution_v1")
PROFILE_SHA256 = "29730b198c2519894341266d7e1ea3f1a6f2ed341235766f70458c572c972cc7"
_TOKEN = object()


def load_pre_execution_contract(root):
    raw = read_regular_file_no_follow(root / PROFILE_PATH / "contract_v1.json", max_bytes=8192)
    if len(raw) != 3513 or digest(raw) != PROFILE_SHA256:
        fail("timing_pre_execution_contract_invalid")
    profile = decode_strict_json_object(raw, max_bytes=8192)
    schemas = {}
    for item in profile["schemas"]:
        raw = read_regular_file_no_follow(root / PROFILE_PATH / item["path"], max_bytes=65536)
        if len(raw) != item["bytes"] or digest(raw) != item["sha256"]:
            fail("timing_pre_execution_contract_invalid")
        schemas[item["path"]] = decode_strict_json_object(raw, max_bytes=65536)
    return profile, schemas


@dataclass(frozen=True, slots=True, init=False)
class VerifiedTimedPreExecutionDocuments:
    trust_manifest_sha256: str
    preregistration_envelope_sha256: str
    authorization_grant_sha256: str
    consumption_receipt_sha256: str
    consumption_ledger_entry_sha256: str
    consumption_ledger_head_sha256: str
    external_consumption_anchor_sha256: str
    pre_execution_observation_sha256: str
    timing_plan_commitment_sha256: str
    runtime_plan_core_sha256: str
    runtime_plan_commitment_sha256: str
    timing_execution_binding_sha256: str
    verified_as_of_utc: str
    grant_expires_at_utc: str

    def __init__(self, *args, **kwargs):
        raise TypeError("VerifiedTimedPreExecutionDocuments requires verification")

    @classmethod
    def _create(cls, token, **values):
        if token is not _TOKEN or set(values) != set(cls.__dataclass_fields__) or any(type(value) is not str for value in values.values()):
            raise TypeError("invalid pre-execution proof construction")
        result = object.__new__(cls)
        for key, value in values.items():
            object.__setattr__(result, key, value)
        return result

    def summary(self):
        return dict(asdict(self), scope="signed_timing_graph_at_supplied_time_only",
                    current_source_review_first_live_admission_verified=False,
                    independent_current_time_proved=False, one_shot_execution_claimed=False,
                    runtime_authority_granted=False, current_closure_claim_allowed=False,
                    provider_calls=0, real_key_loads=0)


def _verify(root, documents, *, external_observation_bundle, timing_plan_bytes, verification_time_utc, _version=2):
    if type(documents) is not PreReceiptDocumentBytes:
        fail("timing_pre_execution_documents_invalid")
    _profile, additions = load_pre_execution_contract(root)
    old = load_frozen_external_preregistration_contract(root)
    schemas = dict(old["schemas"])
    # Only this new entrypoint selects the v2 schema. No old schema file or
    # postrun schema map is mutated, relaxed or given synthetic manifest fields.
    if type(_version) is not int or _version not in (2, 3):
        fail("timing_pre_execution_version_invalid")
    if _version == 2:
        envelope_schema = additions["preregistration_v2.schema.json"]
    else:
        from .execution_scope import load_execution_scope_contract
        _scope_profile, envelope_schema = load_execution_scope_contract(root)
    schemas["external_preregistration_envelope_v1.schema.json"] = envelope_schema
    decoded = {name: inherited._decode_schema_document(getattr(documents, name), schemas=schemas,
        schema_name=schema_name, schema_error="external_closure_document_schema_invalid")
        for name, schema_name in inherited._SCHEMA_BY_DOCUMENT.items()}
    observation = inherited._decode_schema_document(external_observation_bundle,
        schemas=additions, schema_name="observation_v1.schema.json", schema_error="timing_pre_execution_observation_invalid")
    inherited._verify_signed_preregistration_graph(decoded, observation)
    times = inherited._verify_consumption_timeline(decoded, observation)
    as_of = inherited._timestamp(verification_time_utc)
    if not times["consumed_anchor"] <= as_of < min(times["grant_expires"], times["envelope_valid"], times["trust_expires"]):
        fail("timing_pre_execution_as_of_invalid")
    frozen, timing_schemas = load_contract(root)
    plan = _decode(timing_plan_bytes, timing_schemas["completion_timing_plan_v1.schema.json"], frozen["limits"]["max_plan_bytes"], ())
    envelope, grant = decoded["preregistration_envelope"], decoded["authorization_grant"]
    runtime, execution = envelope["runtime_plan"], envelope["execution_binding"]
    if (plan["timing_contract_sha256"] != CONTRACT_SHA256
        or plan["plan_commitment_sha256"] != commitment("plan", plan)
        or plan["plan_commitment_sha256"] != runtime["timing_plan_commitment_sha256"]):
        fail("timing_pre_execution_plan_mismatch")
    denominator, limits = runtime["denominator_plan"], runtime["transport_limits"]
    expected = {key: execution[key] for key in ("execution_commit", "execution_tree", "source_integrity_commitment_sha256")}
    expected.update({key: denominator[key] for key in ("provider_id", "api_surface", "transport_id", "adapter_version")})
    expected.update(campaign_id=runtime["campaign_topology"]["campaign_id"], runtime_plan_core_sha256=runtime_plan_core_commitment(runtime))
    if plan["binding"] != expected:
        fail("timing_pre_execution_binding_mismatch")
    if any(type(limits[name]) is not int for name in ("request_timeout_seconds", "total_timeout_seconds")):
        fail("timing_pre_execution_limits_invalid")
    if (plan["planned_case_handles"] != denominator["case_ids"]
        or plan["max_attempts"] != denominator["total_model_request_cap"]
        or plan["max_attempts_per_case"] != denominator["max_turns_per_case"]
        or plan["request_timeout_ns"] != limits["request_timeout_seconds"] * 1_000_000_000
        or plan["phase_timeout_ns"] != limits["total_timeout_seconds"] * 1_000_000_000):
        fail("timing_pre_execution_limits_invalid")
    anchor_hash = digest(canonical_json_bytes(observation["authorization_consumed_anchor"]))
    binding = dict(expected, authorization_grant_sha256=grant["document_sha256"], external_consumption_anchor_sha256=anchor_hash,
                   runtime_plan_commitment_sha256=runtime["external_plan_binding_sha256"])
    return VerifiedTimedPreExecutionDocuments._create(_TOKEN,
        trust_manifest_sha256=decoded["trust_manifest"]["document_sha256"], preregistration_envelope_sha256=envelope["document_sha256"],
        authorization_grant_sha256=grant["document_sha256"], consumption_receipt_sha256=decoded["consumption_receipt"]["document_sha256"],
        consumption_ledger_entry_sha256=decoded["authorization_consumed_ledger_entry"]["entry_sha256"],
        consumption_ledger_head_sha256=decoded["authorization_consumed_ledger_entry"]["resulting_head_sha256"],
        external_consumption_anchor_sha256=anchor_hash, pre_execution_observation_sha256=digest(external_observation_bundle),
        timing_plan_commitment_sha256=plan["plan_commitment_sha256"], runtime_plan_core_sha256=expected["runtime_plan_core_sha256"],
        runtime_plan_commitment_sha256=runtime["external_plan_binding_sha256"], timing_execution_binding_sha256=commitment("execution_binding", binding),
        verified_as_of_utc=as_of.isoformat(), grant_expires_at_utc=grant["expires_at_utc"])


def verify_timed_pre_execution(root, documents, *, external_observation_bundle, timing_plan_bytes, verification_time_utc):
    """Verify signatures at a supplied as-of time without clocks, Key or claims."""
    try:
        return _verify(root, documents, external_observation_bundle=external_observation_bundle,
                       timing_plan_bytes=timing_plan_bytes, verification_time_utc=verification_time_utc)
    except (TimingContractError, ExternalClosurePrimitiveError):
        raise
    except Exception:
        fail("timing_pre_execution_invalid")
