"""Compose raw evidence, historical source, review and registry link checks.

This is a read-only prerequisite verifier, not the outer preregistration/witness
verifier or a runtime factory. The outer A/B layers must verify their own signed
documents and complete campaign evidence; this result alone cannot close STATUS.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import jsonschema

from researchops.provider_completion_external_contract import load_frozen_external_preregistration_contract

from .admission_bundle_bytes import validate_first_live_bundle_envelope
from .admission_contract import load_admission_link_contract
from .admission_identity import verify_first_live_historical_identity
from .admission_registry import validate_registry_carrier
from .admission_review import verify_review_link
from .admission_source_git import verify_historical_source_equality
from .errors import ExternalClosurePrimitiveError
from .execution_binding_v2 import verify_historical_profile
from .git_objects import read_git_object_snapshot
from .primitives import canonical_json_bytes, decode_strict_json_object
from .schema_guard import LOCAL_ONLY_SCHEMA_REGISTRY, snapshot_local_schema


@dataclass(frozen=True,slots=True)
class AdmissionEvidenceInputs:
    first_live_artifact_directory: Path
    first_live_bundle: bytes
    first_live_review: bytes
    review_observation: bytes
    trust_manifest: bytes
    expected_review_document_sha256: str
    expected_trust_manifest_sha256: str
    expected_first_live_authorization_binding_sha256: str
    expected_candidate_freeze_receipt_sha256: str
    expected_candidate_frozen_at_utc: str

    def __post_init__(self):
        if not isinstance(self.first_live_artifact_directory,Path):
            raise TypeError("first_live_artifact_directory_must_be_path")
        for name in ("first_live_bundle","first_live_review","review_observation","trust_manifest"):
            if type(getattr(self,name)) is not bytes:
                raise TypeError("admission_evidence_documents_must_be_bytes")


@dataclass(frozen=True,slots=True)
class VerifiedAdmissionLinks:
    candidate_commit: str
    candidate_tree: str
    first_live_execution_commit: str
    first_live_execution_tree: str
    first_live_evidence_commitment_sha256: str
    review_document_sha256: str
    runtime_registry_commitment_sha256: str
    source_byte_inventory_sha256: str
    component_bindings_verified: bool = True
    first_live_artifact_semantics_verified: bool = True
    source_equivalence_verified: bool = True
    review_signature_and_observation_verified: bool = True
    registry_links_verified: bool = True
    outer_preregistration_and_campaign_authorization_verified: bool = False
    actual_network_execution_independently_proved: bool = False
    runtime_authority_granted: bool = False
    closure_claim_allowed: bool = False
    verification_provider_calls: int = 0
    verification_key_loads: int = 0


def _fail(code):
    raise ExternalClosurePrimitiveError("admission_links_"+code) from None


def _binding(root: Path,value: dict) -> dict:
    if type(value) is not dict:
        _fail("binding_invalid")
    raw=canonical_json_bytes(value)
    result=decode_strict_json_object(raw,max_bytes=32768)
    contract=load_frozen_external_preregistration_contract(root)
    envelope=snapshot_local_schema(contract["schemas"]["external_preregistration_envelope_v1.schema.json"])
    schema={**envelope["properties"]["execution_binding"],"$defs":envelope["$defs"]}
    try:
        jsonschema.Draft202012Validator(schema,registry=LOCAL_ONLY_SCHEMA_REGISTRY).validate(result)
    except Exception:
        _fail("binding_invalid")
    return result


def verify_admission_evidence_links(
    project_root: Path,*,execution_binding: dict,evidence: AdmissionEvidenceInputs,
) -> VerifiedAdmissionLinks:
    if not isinstance(project_root,Path) or type(evidence) is not AdmissionEvidenceInputs:
        _fail("input_invalid")
    binding=_binding(project_root,execution_binding)
    if binding["candidate_freeze_receipt_sha256"]!=evidence.expected_candidate_freeze_receipt_sha256:
        _fail("candidate_freeze_mismatch")
    contract=load_admission_link_contract(project_root)
    # This subject is initially only authenticated by the caller-held envelope
    # commitment. Actual Git/control-contract checks below independently prove
    # its identity fields before any successful result can be returned.
    first=contract.validate("first_live_evidence_bundle_v1.schema.json",evidence.first_live_bundle,max_bytes=65536)
    subject=first["subject"]
    validate_first_live_bundle_envelope(contract,evidence.first_live_bundle,
        expected_commitment_sha256=binding["first_live_evidence_commitment_sha256"],
        expected_authorization_binding_sha256=evidence.expected_first_live_authorization_binding_sha256,
        expected_subject=subject)
    review=contract.validate("first_live_review_record_v1.schema.json",evidence.first_live_review,max_bytes=32768)
    checked_review=verify_review_link(contract,review_bytes=evidence.first_live_review,trust_bytes=evidence.trust_manifest,
        observation_bytes=evidence.review_observation,expected_review_document_sha256=evidence.expected_review_document_sha256,
        expected_trust_manifest_sha256=evidence.expected_trust_manifest_sha256,
        expected_candidate_freeze_receipt_sha256=evidence.expected_candidate_freeze_receipt_sha256,
        expected_candidate_frozen_at_utc=evidence.expected_candidate_frozen_at_utc,expected_subject=subject,
        expected_first_live_evidence_commitment_sha256=binding["first_live_evidence_commitment_sha256"],
        expected_evidence_commit=review["evidence_commit"],expected_evidence_tree=review["evidence_tree"])
    if checked_review.decision!="approved":
        _fail("review_not_approved")
    if review["first_live_completed_at_utc"]!=first["first_live_completed_at_utc"]:
        _fail("review_timeline_mismatch")
    candidate=verify_historical_profile(project_root,commit=binding["execution_commit"],tree=binding["execution_tree"],profile="campaign")
    expected={"source_integrity_commitment_sha256":candidate.components.source_integrity_commitment_sha256,
              "implementation_commitment_sha256":candidate.components.implementation_commitment_sha256}
    expected.update({name:candidate.components.component_hashes[name] for name in
                    ("runner_config_commitment_sha256","dependency_lock_sha256","sanitizer_sha256","closure_verifier_sha256","telemetry_schema_sha256","mapping_sha256")})
    if any(binding[name]!=value for name,value in expected.items()):
        _fail("candidate_components_mismatch")
    registry_path="evals/provider_completion_runtime_registry_v3/runtime_registry_v3.json"
    bundle_path="evals/provider_completion_runtime_registry_v3/registry_admission_bundle_v1.json"
    registry_snapshot=read_git_object_snapshot(project_root,candidate.commit,(registry_path,bundle_path),expected_tree_oid=candidate.tree)
    raw={blob.path:blob.payload for blob in registry_snapshot.blobs}
    validate_registry_carrier(project_root,contract,registry_bytes=raw[registry_path],admission_bundle_bytes=raw[bundle_path],
        expected_registry_commitment_sha256=binding["runtime_registry_commitment_sha256"],expected_subject=subject,
        expected_first_live_evidence_commitment_sha256=binding["first_live_evidence_commitment_sha256"],
        expected_review_document_sha256=evidence.expected_review_document_sha256,
        expected_reviewed_evidence_commit=review["evidence_commit"],expected_reviewed_evidence_tree=review["evidence_tree"])
    historical=verify_first_live_historical_identity(project_root,artifact_directory=evidence.first_live_artifact_directory,
        bundle_bytes=evidence.first_live_bundle,expected_evidence_commitment_sha256=binding["first_live_evidence_commitment_sha256"],
        expected_authorization_binding_sha256=evidence.expected_first_live_authorization_binding_sha256,expected_subject=subject,
        reviewed_evidence_commit=review["evidence_commit"],reviewed_evidence_tree=review["evidence_tree"])
    equality=verify_historical_source_equality(project_root,contract,
        first_live_commit=historical.historical.commit,first_live_tree=historical.historical.tree,
        campaign_commit=candidate.commit,campaign_tree=candidate.tree)
    if (equality.equality.source_byte_inventory_sha256!=subject["source_byte_inventory_sha256"]
        or equality.equality.source_byte_inventory_sha256!=candidate.components.component_hashes["source_byte_inventory_sha256"]
        or equality.equality.dependency_lock_sha256!=binding["dependency_lock_sha256"]
        or equality.equality.pyproject_sha256!=candidate.components.component_hashes["pyproject_sha256"]
        or binding["mapping_sha256"]!=subject["selected_mapping_sha256"]):
        _fail("source_equivalence_mismatch")
    return VerifiedAdmissionLinks(candidate.commit,candidate.tree,historical.historical.commit,historical.historical.tree,
        binding["first_live_evidence_commitment_sha256"],evidence.expected_review_document_sha256,
        binding["runtime_registry_commitment_sha256"],equality.equality.source_byte_inventory_sha256)


__all__=["AdmissionEvidenceInputs","VerifiedAdmissionLinks","verify_admission_evidence_links"]
