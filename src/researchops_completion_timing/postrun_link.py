"""Join the signed timing graph to actual artifacts; not full A/B admission.

OOB observations remain caller-supplied independent inputs. This composition
does not establish their real-world origin, current source/review admission,
global one-shot execution, budget/tool semantics or T7 closure.
"""
from __future__ import annotations

from researchops.provider_completion_external_contract import load_frozen_external_preregistration_contract
from researchops_external_closure import documents as inherited
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.primitives import canonical_json_bytes, decode_strict_json_object
from .artifacts import verify_timing_artifact_bundle
from .contract import TimingContractError, fail
from .pre_execution import verify_timed_pre_execution


def _verify_link(root, directory, documents, *, pre_execution_observation,
                 postrun_observation, timing_plan_bytes, verification_time_utc, _version):
    """Derive artifact expectations from the verified signed graph, not a side dict."""
    try:
        scoped = None
        if type(_version) is not int or _version not in (2, 3):
            fail("timing_postrun_link_version_invalid")
        if _version == 2:
            proof = verify_timed_pre_execution(root, documents, external_observation_bundle=pre_execution_observation,
                timing_plan_bytes=timing_plan_bytes, verification_time_utc=verification_time_utc)
        else:
            from .execution_scope import verify_single_host_pre_execution
            scoped = verify_single_host_pre_execution(root, documents, external_observation_bundle=pre_execution_observation,
                timing_plan_bytes=timing_plan_bytes, verification_time_utc=verification_time_utc)
            proof = scoped.timing
        schemas = load_frozen_external_preregistration_contract(root)["schemas"]
        after = inherited._decode_schema_document(postrun_observation, schemas=schemas,
            schema_name="external_observation_bundle_v1.schema.json", schema_error="timing_postrun_observation_invalid")
        if after["mode"] != "pre_receipt":
            fail("timing_postrun_observation_mode_invalid")
        before = decode_strict_json_object(pre_execution_observation, max_bytes=2_000_000)
        common = {key: value for key, value in before.items() if key not in {"schema_version", "mode"}}
        if canonical_json_bytes({key: after.get(key) for key in common}) != canonical_json_bytes(common):
            fail("timing_postrun_observation_history_mismatch")
        manifest = after["manifest_observation"]
        if manifest["state"] != "present":
            fail("timing_postrun_manifest_unavailable")
        observed = inherited._timestamp(manifest["observed_at_utc"])
        anchor = inherited._timestamp(before["authorization_consumed_anchor"]["observed_at_utc"])
        as_of = inherited._timestamp(proof.verified_as_of_utc)
        envelope = decode_strict_json_object(documents.preregistration_envelope, max_bytes=2_000_000)
        if not anchor < observed < inherited._timestamp(envelope["valid_until_utc"]) or as_of > observed:
            fail("timing_postrun_observation_time_invalid")
        runtime = envelope["runtime_plan"]
        case_run_ids = {case: "PCERUN-" + inherited._domain_hash(inherited._AUDIT_RUN_ID_DOMAIN,
            envelope["envelope_id"].encode("ascii"), case.encode("ascii"))[:32].upper()
            for case in runtime["denominator_plan"]["case_ids"]}
        result = verify_timing_artifact_bundle(root, directory,
            expected_manifest_sha256=manifest["manifest_sha256"],
            expected_plan_commitment_sha256=proof.timing_plan_commitment_sha256,
            expected_execution_binding_sha256=proof.timing_execution_binding_sha256,
            expected_denominator_plan_bytes=canonical_json_bytes(runtime["denominator_plan"]),
            expected_case_run_ids=case_run_ids)
        details = {} if scoped is None else {"signed_execution_environment_id": scoped.execution_environment_id, "local_claim_verified": False}
        return dict(result, scope="signed_timing_graph_and_artifact_linkage_against_supplied_oob_only",
                    signed_timing_graph_verified=True, shared_observation_history_matches=True,
                    postrun_manifest_observation_matches=True, oob_origin_independently_proved=False,
                    independent_current_time_proved=False,
                    current_source_review_admission_verified=False, global_one_shot_execution_proved=False,
                    runtime_authority_granted=False, current_closure_claim_allowed=False, **details)
    except (TimingContractError, ExternalClosurePrimitiveError):
        raise
    except Exception:
        fail("timing_postrun_link_invalid")


def verify_signed_timing_artifact_link(root, directory, documents, *, pre_execution_observation,
                                      postrun_observation, timing_plan_bytes, verification_time_utc):
    """Legacy v2 graph linkage; never upgrades an unscoped grant to v3."""
    return _verify_link(root, directory, documents, pre_execution_observation=pre_execution_observation,
        postrun_observation=postrun_observation, timing_plan_bytes=timing_plan_bytes,
        verification_time_utc=verification_time_utc, _version=2)


def verify_scoped_timing_artifact_link(root, directory, documents, *, pre_execution_observation,
                                      postrun_observation, timing_plan_bytes, verification_time_utc):
    """Explicit v3 signed-scope linkage, still not a local claim or runtime permit."""
    return _verify_link(root, directory, documents, pre_execution_observation=pre_execution_observation,
        postrun_observation=postrun_observation, timing_plan_bytes=timing_plan_bytes,
        verification_time_utc=verification_time_utc, _version=3)
