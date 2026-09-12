"""Compose timed evidence, signed review, source equality and registry links.

This verifies offline prerequisites, not current-source/fresh-time/one-shot
campaign permission, installed dependencies, actual Provider execution or T7.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from researchops_external_closure.admission_evidence import _binding
from researchops_external_closure.admission_registry import validate_registry_carrier
from researchops_external_closure.admission_source_git import verify_historical_source_equality
from researchops_external_closure.execution_binding_v3 import verify_historical_timed_profile
from researchops_external_closure.execution_components_v3 import PROFILE_PATHS as TIMED_PROFILE_PATHS
from researchops_external_closure import execution_binding_v4, execution_components_v4
from researchops_external_closure.git_objects import read_git_object_snapshot
from . import contract as core
from . import first_live_review as review
from . import first_live_artifacts as archive
from .first_live_evidence import verify_timed_first_live_evidence, verify_timed_first_live_evidence_v5
from .artifacts import _sha


@dataclass(frozen=True, slots=True)
class TimedAdmissionInputs:
    artifact_directory: Path
    bundle_bytes: bytes
    review_bytes: bytes
    trust_bytes: bytes
    observation_bytes: bytes
    expected_review_document_sha256: str
    expected_trust_manifest_sha256: str
    expected_authorization_binding_sha256: str
    expected_candidate_freeze_receipt_sha256: str
    expected_candidate_frozen_at_utc: str
    expected_first_live_completed_at_utc: str
    verification_time_utc: str

    def __post_init__(self):
        if not isinstance(self.artifact_directory, Path):
            raise TypeError("timed_admission_artifact_path_invalid")
        for name in ("bundle_bytes", "review_bytes", "trust_bytes", "observation_bytes"):
            if type(getattr(self, name)) is not bytes:
                raise TypeError("timed_admission_document_bytes_required")


@dataclass(frozen=True, slots=True)
class VerifiedTimedAdmissionLinks:
    candidate_commit: str
    candidate_tree: str
    first_live_execution_commit: str
    first_live_execution_tree: str
    bundle_commitment_sha256: str
    review_document_sha256: str
    runtime_registry_commitment_sha256: str
    source_byte_inventory_sha256: str
    artifact_source_binding_verified: bool = True
    review_signature_and_external_bindings_verified: bool = True
    source_byte_equality_verified: bool = True
    first_live_snapshot_link_verified: bool = True
    registry_links_verified: bool = True
    outer_preregistration_and_campaign_authorization_verified: bool = False
    current_source_and_fresh_time_verified: bool = False
    winning_local_claim_verified: bool = False
    installed_environment_verified: bool = False
    actual_network_execution_independently_proved: bool = False
    runtime_authority_granted: bool = False
    closure_claim_allowed: bool = False
    verification_provider_calls: int = 0
    verification_key_loads: int = 0


@dataclass(frozen=True, slots=True)
class VerifiedTimedAdmissionLinksV5(VerifiedTimedAdmissionLinks):
    # Distinct exact type: legacy runtime ownership gates must not implicitly
    # accept a new-version diagnostic as their old checked admission object.
    source_recipe_version: int = field(default=4, init=False)
    first_live_implementation_version: int = field(default=5, init=False)


def verify_timed_admission_links(project_root, *, execution_binding, inputs, sensitive_canaries=()):
    """Legacy v4 first-live / v3 source composition, not runtime permission."""
    return _verify_timed_admission_links(project_root, execution_binding=execution_binding,
        inputs=inputs, sensitive_canaries=sensitive_canaries, implementation_version=4)


def verify_timed_admission_links_v5(project_root, *, execution_binding, inputs, sensitive_canaries=()):
    """Explicit v5 first-live / v4 source prerequisites, with no online authority."""
    return _verify_timed_admission_links(project_root, execution_binding=execution_binding,
        inputs=inputs, sensitive_canaries=sensitive_canaries, implementation_version=5)


def _verify_timed_admission_links(project_root, *, execution_binding, inputs, sensitive_canaries, implementation_version):
    if type(implementation_version) is not int or implementation_version not in (4, 5):
        core.fail('timed_admission_version_invalid')
    if not isinstance(project_root, Path) or type(inputs) is not TimedAdmissionInputs:
        core.fail("timed_admission_input_invalid")
    binding = _binding(project_root, execution_binding)
    if binding["candidate_freeze_receipt_sha256"] != inputs.expected_candidate_freeze_receipt_sha256:
        core.fail("timed_admission_candidate_freeze_mismatch")
    canaries = tuple(sensitive_canaries)
    parent, schema = review.load_first_live_review_contract(project_root)
    claimed = core._decode(inputs.review_bytes, schema, 32768, canaries)
    _sha(inputs.expected_review_document_sha256)
    if (claimed["document_sha256"] != review.review_document_sha256(claimed)
        or claimed["document_sha256"] != inputs.expected_review_document_sha256):
        core.fail("timed_admission_review_digest_mismatch")
    # This is only an early digest check. Do not echo the claimed subject as an
    # expectation: actual archive/Git derivation below supplies that expectation.
    evidence_verifier = verify_timed_first_live_evidence if implementation_version == 4 else verify_timed_first_live_evidence_v5
    evidence = evidence_verifier(project_root, artifact_directory=inputs.artifact_directory,
        bundle_bytes=inputs.bundle_bytes, expected_bundle_commitment_sha256=binding["first_live_evidence_commitment_sha256"],
        expected_authorization_binding_sha256=inputs.expected_authorization_binding_sha256,
        verification_time_utc=inputs.verification_time_utc, reviewed_evidence_commit=claimed["evidence_commit"],
        reviewed_evidence_tree=claimed["evidence_tree"], sensitive_canaries=canaries)
    subject = dict(evidence.subject)
    checked_review = review.verify_timed_first_live_review(project_root, review_bytes=inputs.review_bytes,
        trust_bytes=inputs.trust_bytes, observation_bytes=inputs.observation_bytes,
        expected_review_document_sha256=inputs.expected_review_document_sha256,
        expected_trust_manifest_sha256=inputs.expected_trust_manifest_sha256,
        expected_candidate_freeze_receipt_sha256=inputs.expected_candidate_freeze_receipt_sha256,
        expected_candidate_frozen_at_utc=inputs.expected_candidate_frozen_at_utc, expected_subject=subject,
        expected_first_live_evidence_commitment_sha256=evidence.bundle_commitment_sha256,
        expected_evidence_commit=evidence.reviewed_evidence_commit, expected_evidence_tree=evidence.reviewed_evidence_tree,
        expected_first_live_completed_at_utc=inputs.expected_first_live_completed_at_utc, sensitive_canaries=canaries)
    if checked_review["decision"] != "approved":
        core.fail("timed_admission_review_not_approved")
    if implementation_version == 4:
        candidate = verify_historical_timed_profile(project_root, commit=binding["execution_commit"], tree=binding["execution_tree"], profile="campaign")
    else:
        candidate = execution_binding_v4.verify_historical_timed_profile(
            project_root, commit=binding['execution_commit'], tree=binding['execution_tree'], profile='campaign')
    components = candidate.components
    expected = {"source_integrity_commitment_sha256": components.source_integrity_commitment_sha256,
                "implementation_commitment_sha256": components.implementation_commitment_sha256,
                "first_live_validation_status": "reviewed_success", "runtime_registry_binding_allowed": True}
    for name in ("runner_config_commitment_sha256", "dependency_lock_sha256", "sanitizer_sha256",
                 "closure_verifier_sha256", "telemetry_schema_sha256", "mapping_sha256"):
        expected[name] = components.component_hashes[name]
    expected.update({name: subject[name] for name in ("provider_id", "model_id", "api_origin", "api_surface", "transport_id", "adapter_version")})
    if any(binding[name] != value for name, value in expected.items()):
        core.fail("timed_admission_candidate_components_mismatch")
    registry_path = "evals/provider_completion_runtime_registry_v3/runtime_registry_v3.json"
    carrier_path = "evals/provider_completion_runtime_registry_v3/registry_admission_bundle_v1.json"
    profile_paths = TIMED_PROFILE_PATHS if implementation_version == 4 else execution_components_v4.PROFILE_PATHS
    first_manifest, first_plan = profile_paths['first_live']
    candidate_snapshot = read_git_object_snapshot(project_root, candidate.commit,
        (registry_path, carrier_path, first_manifest, first_plan), expected_tree_oid=candidate.tree)
    payloads = {blob.path: blob.payload for blob in candidate_snapshot.blobs}
    observed_first = read_git_object_snapshot(project_root, subject["first_live_execution_commit"],
        (first_manifest, first_plan), expected_tree_oid=subject["first_live_execution_tree"])
    # Candidate-local consistency (v9/v10 or v11/v12) is insufficient: its stored first-live profile could
    # be a different, self-consistently regenerated snapshot with equal Python
    # bytes but changed static contracts. Tie it to the actually evidenced run.
    if any(payloads[blob.path] != blob.payload for blob in observed_first.blobs):
        core.fail("timed_admission_first_live_snapshot_mismatch")
    # Preserve the explicit byte-comparison requirement. Equal version labels
    # or inventory hashes alone do not replace the historical material compare.
    equality = verify_historical_source_equality(project_root, parent,
        first_live_commit=subject["first_live_execution_commit"], first_live_tree=subject["first_live_execution_tree"],
        campaign_commit=candidate.commit, campaign_tree=candidate.tree)
    if (equality.equality.source_byte_inventory_sha256 != subject["source_byte_inventory_sha256"]
        or equality.equality.source_byte_inventory_sha256 != components.component_hashes["source_byte_inventory_sha256"]
        or equality.equality.dependency_lock_sha256 != binding["dependency_lock_sha256"]
        or equality.equality.pyproject_sha256 != components.component_hashes["pyproject_sha256"]
        or subject["selected_mapping_sha256"] != binding["mapping_sha256"]):
        core.fail("timed_admission_source_equivalence_mismatch")
    # Registry v3 remains a non-authorizing carrier. This new entry point binds
    # it to explicit timed evidence/review; the legacy entry point is unchanged.
    validate_registry_carrier(project_root, parent, registry_bytes=payloads[registry_path], admission_bundle_bytes=payloads[carrier_path],
        expected_registry_commitment_sha256=binding["runtime_registry_commitment_sha256"], expected_subject=subject,
        expected_first_live_evidence_commitment_sha256=evidence.bundle_commitment_sha256,
        expected_review_document_sha256=inputs.expected_review_document_sha256,
        expected_reviewed_evidence_commit=evidence.reviewed_evidence_commit, expected_reviewed_evidence_tree=evidence.reviewed_evidence_tree)
    final_archive = archive.verify_first_live_artifact_bundle(project_root, inputs.artifact_directory,
        bundle_bytes=inputs.bundle_bytes, expected_bundle_commitment_sha256=evidence.bundle_commitment_sha256,
        expected_authorization_binding_sha256=inputs.expected_authorization_binding_sha256,
        verification_time_utc=inputs.verification_time_utc, sensitive_canaries=canaries)
    if not final_archive["timing_gate_complete"]:
        core.fail("timed_admission_final_timing_incomplete")
    result_type = VerifiedTimedAdmissionLinks if implementation_version == 4 else VerifiedTimedAdmissionLinksV5
    return result_type(candidate.commit, candidate.tree, subject["first_live_execution_commit"],
        subject["first_live_execution_tree"], evidence.bundle_commitment_sha256, inputs.expected_review_document_sha256,
        binding["runtime_registry_commitment_sha256"], equality.equality.source_byte_inventory_sha256)
