"""Join signed campaign scope and historical first-live admission before claim.

Read-only prerequisites, not a runtime capability. No task release, Key, local
store provisioning/reservation, current-source or installed-environment claim.
"""
from __future__ import annotations

from dataclasses import dataclass

from researchops_external_closure.primitives import decode_strict_json_object, parse_utc_timestamp
from researchops_external_closure.types import PreReceiptDocumentBytes
from .admission import TimedAdmissionInputs, VerifiedTimedAdmissionLinks, verify_timed_admission_links
from .execution_scope import VerifiedSingleHostScope, verify_single_host_pre_execution, _current_utc
from .contract import fail

_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedCampaignPrerequisites:
    scope: VerifiedSingleHostScope
    admission: VerifiedTimedAdmissionLinks
    expires_at_utc: str

    def __init__(self, *args, **kwargs):
        raise TypeError('campaign prerequisites require combined verification')

    @classmethod
    def _create(cls, token, scope, admission, expires):
        if token is not _TOKEN or type(scope) is not VerifiedSingleHostScope or type(admission) is not VerifiedTimedAdmissionLinks:
            raise TypeError('campaign prerequisites invalid')
        value = object.__new__(cls)
        for name, item in (('scope', scope), ('admission', admission), ('expires_at_utc', expires)):
            object.__setattr__(value, name, item)
        return value

    def summary(self):
        return dict(status='campaign_prerequisites_verified', same_trust_and_candidate_freeze_verified=True,
            execution_commit=self.admission.candidate_commit, execution_environment_id=self.scope.execution_environment_id,
            expires_at_utc=self.expires_at_utc, current_source_verified=False, installed_environment_verified=False,
            winning_local_claim_verified=False, task_release_authorized=False, provider_key_load_authorized=False,
            runtime_authority_granted=False, closure_claim_allowed=False)


class VerifiedCampaignPrerequisitesV5(VerifiedCampaignPrerequisites):
    """Distinct exact type; legacy starts are not upgraded by this result."""
    __slots__ = ()

    @classmethod
    def _create(cls, token, scope, admission, expires):
        from .admission import VerifiedTimedAdmissionLinksV5
        if (cls is not VerifiedCampaignPrerequisitesV5 or token is not _TOKEN
                or type(scope) is not VerifiedSingleHostScope or type(admission) is not VerifiedTimedAdmissionLinksV5):
            raise TypeError('campaign v5 prerequisites invalid')
        value = object.__new__(cls)
        for name, item in (('scope', scope), ('admission', admission), ('expires_at_utc', expires)):
            object.__setattr__(value, name, item)
        return value

    def summary(self):
        return dict(super().summary(), first_live_implementation_version=5, source_recipe_version=4)


def verify_campaign_prerequisites(root, documents, *, external_observation_bundle, timing_plan_bytes,
                                  admission_inputs, verification_time_utc):
    return _verify_campaign_prerequisites(root, documents, external_observation_bundle=external_observation_bundle,
        timing_plan_bytes=timing_plan_bytes, admission_inputs=admission_inputs,
        verification_time_utc=verification_time_utc, implementation_version=4)


def verify_campaign_prerequisites_v5(root, documents, *, external_observation_bundle, timing_plan_bytes,
                                     admission_inputs, verification_time_utc):
    return _verify_campaign_prerequisites(root, documents, external_observation_bundle=external_observation_bundle,
        timing_plan_bytes=timing_plan_bytes, admission_inputs=admission_inputs,
        verification_time_utc=verification_time_utc, implementation_version=5)


def _verify_campaign_prerequisites(root, documents, *, external_observation_bundle, timing_plan_bytes,
                                   admission_inputs, verification_time_utc, implementation_version):
    if type(implementation_version) is not int or implementation_version not in (4, 5):
        fail('campaign_admission_version_invalid')
    if type(documents) is not PreReceiptDocumentBytes or type(admission_inputs) is not TimedAdmissionInputs:
        fail('campaign_admission_input_invalid')
    scope = verify_single_host_pre_execution(root, documents, external_observation_bundle=external_observation_bundle,
        timing_plan_bytes=timing_plan_bytes, verification_time_utc=verification_time_utc)
    envelope = decode_strict_json_object(documents.preregistration_envelope, max_bytes=2_000_000)
    candidate = decode_strict_json_object(documents.candidate_freeze_receipt, max_bytes=2_000_000)
    trust = decode_strict_json_object(documents.trust_manifest, max_bytes=2_000_000)
    # Do not let two individually valid graphs nominate unrelated trust roots or
    # freezes. Compare independently supplied expectations to VERIFIED documents.
    if (admission_inputs.expected_trust_manifest_sha256 != scope.timing.trust_manifest_sha256
        or admission_inputs.expected_candidate_freeze_receipt_sha256 != candidate['document_sha256']
        or admission_inputs.expected_candidate_frozen_at_utc != candidate['frozen_at_utc']):
        fail('campaign_admission_cross_graph_mismatch')
    verifier, expected_type = verify_timed_admission_links, VerifiedTimedAdmissionLinks
    if implementation_version == 5:
        from .admission import VerifiedTimedAdmissionLinksV5, verify_timed_admission_links_v5
        verifier, expected_type = verify_timed_admission_links_v5, VerifiedTimedAdmissionLinksV5
    links = verifier(root, execution_binding=envelope['execution_binding'], inputs=admission_inputs)
    if (type(links) is not expected_type or links.candidate_commit != scope.execution_commit
        or links.candidate_tree != envelope['execution_binding']['execution_tree']):
        fail('campaign_admission_execution_mismatch')
    expiry = min(parse_utc_timestamp(value) for value in
                 (scope.timing.grant_expires_at_utc, envelope['valid_until_utc'], trust['expires_at_utc']))
    result_type = VerifiedCampaignPrerequisites if implementation_version == 4 else VerifiedCampaignPrerequisitesV5
    return result_type._create(_TOKEN, scope, links, expiry.isoformat())


def verify_current_campaign_prerequisites(root, documents, *, external_observation_bundle, timing_plan_bytes, admission_inputs):
    return _verify_current_campaign_prerequisites(root, documents, external_observation_bundle=external_observation_bundle,
        timing_plan_bytes=timing_plan_bytes, admission_inputs=admission_inputs, implementation_version=4)


def verify_current_campaign_prerequisites_v5(root, documents, *, external_observation_bundle, timing_plan_bytes, admission_inputs):
    return _verify_current_campaign_prerequisites(root, documents, external_observation_bundle=external_observation_bundle,
        timing_plan_bytes=timing_plan_bytes, admission_inputs=admission_inputs, implementation_version=5)


def _verify_current_campaign_prerequisites(root, documents, *, external_observation_bundle, timing_plan_bytes,
                                           admission_inputs, implementation_version):
    """Local UTC before/after the full graph; no caller clock/as-of override.

Historical first-live evidence keeps its own checked historical as-of time; it
must not be re-authorized at the later campaign time. Current grant/envelope/trust
expiry and backward movement are checked again after all archive/Git verification.
"""
    if type(implementation_version) is not int or implementation_version not in (4, 5):
        fail('campaign_admission_version_invalid')
    began = _current_utc()
    verifier = verify_campaign_prerequisites if implementation_version == 4 else verify_campaign_prerequisites_v5
    proof = verifier(root, documents, external_observation_bundle=external_observation_bundle,
        timing_plan_bytes=timing_plan_bytes, admission_inputs=admission_inputs, verification_time_utc=began)
    ended = _current_utc()
    if parse_utc_timestamp(ended) < parse_utc_timestamp(began):
        fail('campaign_admission_clock_reversed')
    if parse_utc_timestamp(ended) >= parse_utc_timestamp(proof.expires_at_utc):
        fail('campaign_admission_expired_during_verification')
    return proof
