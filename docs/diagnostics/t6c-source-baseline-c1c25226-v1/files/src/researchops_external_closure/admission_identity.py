"""Bind first-live artifact semantics to actual historical component and evidence blobs."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

from .admission_bundle_bytes import validate_first_live_bundle_envelope
from .admission_contract import load_admission_link_contract
from .admission_first_live import FirstLiveArtifactSemanticsResult, verify_first_live_success_bundle
from .errors import ExternalClosurePrimitiveError
from .execution_binding_v2 import HistoricalProfileComponents, verify_historical_profile
from .git_objects import read_git_object_snapshot
from .primitives import canonical_json_bytes, decode_strict_json_object


CONTROL_PATH="evals/provider_completion_first_live_validation_v1/deepseek_responses_adapter_validation_implementation_v3.json"
CONTROL_SHA256="6e11120ad2463ae4aaeed076b4e490e98e977fb21c8e71fce91e9bb4bc9118eb"
CONTROL_COMMITMENT="b320f096ac7afdfd68516e3d2a5be526b99cb99e6a557ee414d8f390900bd6a7"
_DESIGN_PATH="evals/provider_completion_first_live_validation_v1/deepseek_responses_adapter_validation_contract_v1.json"
_DESIGN_SHA256="477addbc41987d11c6ac7ede3fa6f94d74322efe09c541ea20f3a7937eb96496"
_DESIGN_COMMITMENT="ddff10f30031faf77d6417dd695dd61dae4c6a45334efae7388ab4f2adc4a5bc"
_ENTRYPOINT_PATH="src/researchops/deepseek_completion_first_live_successor.py"


def _fail(code):
    raise ExternalClosurePrimitiveError("admission_identity_"+code) from None


@dataclass(frozen=True,slots=True)
class FirstLiveHistoricalIdentity:
    historical: HistoricalProfileComponents
    artifacts: FirstLiveArtifactSemanticsResult
    control_contract_commitment_sha256: str
    source_manifest_commitment_sha256: str
    reviewed_evidence_commit: str
    reviewed_evidence_tree: str
    reviewed_bundle_git_verified: bool = True
    component_identity_verified: bool = True
    entrypoint_names_present: bool = True
    implementation_behavior_proved_by_source_hash: bool = False
    source_equivalence_verified: bool = False
    review_signature_verified: bool = False
    registry_admission_verified: bool = False
    runtime_authority_granted: bool = False


def verify_first_live_historical_identity(
    project_root: Path,*,artifact_directory: Path,bundle_bytes: bytes,
    expected_evidence_commitment_sha256: str,expected_authorization_binding_sha256: str,
    expected_subject: dict,reviewed_evidence_commit: str,reviewed_evidence_tree: str,
) -> FirstLiveHistoricalIdentity:
    contract=load_admission_link_contract(project_root)
    bundle=validate_first_live_bundle_envelope(contract,bundle_bytes,
        expected_commitment_sha256=expected_evidence_commitment_sha256,
        expected_authorization_binding_sha256=expected_authorization_binding_sha256,expected_subject=expected_subject)
    subject=bundle["subject"]
    historical=verify_historical_profile(project_root,commit=subject["first_live_execution_commit"],
                                          tree=subject["first_live_execution_tree"],profile="first_live")
    components=historical.components
    if components.source_integrity_commitment_sha256!=subject["first_live_source_integrity_commitment_sha256"]:
        _fail("source_plan_mismatch")
    projection={"source_byte_inventory_sha256":"source_byte_inventory_sha256","dependency_lock_sha256":"dependency_lock_sha256",
                "pyproject_sha256":"pyproject_sha256","selected_mapping_sha256":"mapping_sha256"}
    if any(subject[name]!=components.component_hashes[field] for name,field in projection.items()):
        _fail("component_mismatch")
    # Control-plane implementation commitment != source-manifest commitment.
    # Read and rehash the actual control contract instead of conflating them.
    source=read_git_object_snapshot(project_root,historical.commit,(CONTROL_PATH,_DESIGN_PATH,_ENTRYPOINT_PATH),expected_tree_oid=historical.tree)
    raw={blob.path:blob.payload for blob in source.blobs}
    if hashlib.sha256(raw[CONTROL_PATH]).hexdigest()!=CONTROL_SHA256 or hashlib.sha256(raw[_DESIGN_PATH]).hexdigest()!=_DESIGN_SHA256:
        _fail("control_contract_invalid")
    control=decode_strict_json_object(raw[CONTROL_PATH],max_bytes=8192)
    digest=hashlib.sha256(b"researchops-provider-completion-first-live-implementation-v3\0"+canonical_json_bytes(control)).hexdigest()
    if (digest!=CONTROL_COMMITMENT or subject["first_live_implementation_commitment_sha256"]!=digest
        or bundle["first_live_implementation_contract_sha256"]!=CONTROL_SHA256
        or bundle["first_live_design_commitment_sha256"]!=_DESIGN_COMMITMENT):
        _fail("control_contract_mismatch")
    syntax=ast.parse(raw[_ENTRYPOINT_PATH].decode("utf-8"))
    names={node.name for node in syntax.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))}
    required=control["required_entrypoints"]
    if required["module_path"]!=_ENTRYPOINT_PATH or not {required[key] for key in ("contract_validator","authorization_binding","run_gate")}.issubset(names):
        _fail("entrypoint_missing")
    # No snapshot code is imported. Name presence is not a behavior/review claim.
    path="docs/evidence/provider-completion-first-live-v1/"+bundle["authorization_id_sha256"]+"/bundle.json"
    reviewed=read_git_object_snapshot(project_root,reviewed_evidence_commit,(path,),expected_tree_oid=reviewed_evidence_tree)
    if reviewed.blobs[0].payload!=bundle_bytes:
        _fail("reviewed_bundle_mismatch")
    artifacts=verify_first_live_success_bundle(project_root,artifact_directory=artifact_directory,bundle_bytes=bundle_bytes,
        expected_evidence_commitment_sha256=expected_evidence_commitment_sha256,
        expected_authorization_binding_sha256=expected_authorization_binding_sha256,expected_subject=expected_subject)
    if artifacts.source_plan_id!="phase6-deepseek-depth60-v7":
        _fail("source_plan_mismatch")
    return FirstLiveHistoricalIdentity(historical,artifacts,digest,components.implementation_commitment_sha256,
                                       reviewed.commit_oid,reviewed.tree_oid)


__all__=["CONTROL_PATH","CONTROL_SHA256","CONTROL_COMMITMENT","FirstLiveHistoricalIdentity","verify_first_live_historical_identity"]
