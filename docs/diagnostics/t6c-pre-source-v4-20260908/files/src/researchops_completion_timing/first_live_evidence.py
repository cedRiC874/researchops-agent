"""Bind timed archive to actual execution/source and reviewed Git bundle bytes."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from collections.abc import Mapping

from researchops_completion_telemetry.surface_mapping import load_and_select_surface_mapping
from researchops_external_closure.git_objects import read_git_object_snapshot
from researchops_external_closure.io import read_exact_artifact_directory
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object
from . import contract as core
from . import first_live_artifacts as archive
from .first_live_identity import TimedFirstLiveHistoricalIdentity, verify_historical_timed_implementation
from .artifacts import _sha


@dataclass(frozen=True, slots=True)
class TimedFirstLiveEvidenceIdentity:
    historical: TimedFirstLiveHistoricalIdentity
    subject: Mapping[str, str]
    bundle_commitment_sha256: str
    reviewed_evidence_commit: str
    reviewed_evidence_tree: str
    archive_manifest_sha256: str
    archive_publication_sha256: str
    audit_chain_head_sha256: str
    artifact_linkage_verified: bool = True
    reviewed_bundle_git_verified: bool = True
    execution_source_mapping_binding_verified: bool = True
    timing_gate_complete: bool = True
    source_equivalence_verified: bool = False
    review_signature_verified: bool = False
    registry_admission_verified: bool = False
    actual_live_execution_verified: bool = False
    runtime_authority_granted: bool = False
    first_live_success_verified: bool = False


def _assert_plan_subject(binding, subject):
    expected = {name: subject[name] for name in ("provider_id", "api_surface", "transport_id", "adapter_version")}
    expected.update(execution_commit=subject["first_live_execution_commit"], execution_tree=subject["first_live_execution_tree"],
        source_integrity_commitment_sha256=subject["first_live_source_integrity_commitment_sha256"])
    if any(binding[name] != value for name, value in expected.items()):
        core.fail("timed_first_live_evidence_source_mismatch")


def verify_timed_first_live_evidence(project_root, *, artifact_directory, bundle_bytes,
        expected_bundle_commitment_sha256, expected_authorization_binding_sha256,
        verification_time_utc, reviewed_evidence_commit, reviewed_evidence_tree, sensitive_canaries=()):
    """Derive subject and verify the reviewed anchor; no review-supplied subject.

    The repository path for bundle.json remains the existing first-live evidence
    namespace, keyed by the fresh authorization ID hash. V4 does not reinterpret
    an old seven-file bundle: the explicit eight-file schema/domain must verify.
    """
    if not isinstance(project_root, Path) or not isinstance(artifact_directory, Path):
        core.fail("timed_first_live_evidence_path_invalid")
    canaries = tuple(sensitive_canaries)
    checked = archive.verify_first_live_artifact_bundle(project_root, artifact_directory, bundle_bytes=bundle_bytes,
        expected_bundle_commitment_sha256=expected_bundle_commitment_sha256,
        expected_authorization_binding_sha256=expected_authorization_binding_sha256,
        verification_time_utc=verification_time_utc, sensitive_canaries=canaries)
    if not checked["timing_gate_complete"]:
        core.fail("timed_first_live_evidence_timing_incomplete")
    profile, _ = archive.load_first_live_artifact_contract(project_root)

    def snapshot():
        return read_exact_artifact_directory(artifact_directory, expected_names=tuple(profile["artifact_files"]),
            max_file_bytes=profile["max_file_bytes"], max_total_bytes=profile["max_total_bytes"])

    files = snapshot()
    bundle = decode_strict_json_object(bundle_bytes, max_bytes=profile["max_envelope_bytes"])
    actual = {name: {"bytes": len(payload), "sha256": core.digest(payload)} for name, payload in files.items()}
    if raw(actual) != raw(bundle["files"]):
        core.fail("timed_first_live_evidence_snapshot_changed")
    plan = decode_strict_json_object(files["completion_timing_plan.json"], max_bytes=131072)
    binding = plan["binding"]
    identifier = _sha(binding["authorization_id_sha256"])
    path = "docs/evidence/provider-completion-first-live-v1/" + identifier + "/bundle.json"
    reviewed = read_git_object_snapshot(project_root, reviewed_evidence_commit, (path,), expected_tree_oid=reviewed_evidence_tree)
    if reviewed.blobs[0].payload != bundle_bytes:
        core.fail("timed_first_live_reviewed_bundle_mismatch")
    historical = verify_historical_timed_implementation(project_root, commit=binding["execution_commit"], tree=binding["execution_tree"])
    subject = dict(historical.subject)
    _assert_plan_subject(binding, subject)
    selection = load_and_select_surface_mapping(project_root, "deepseek", "responses", "openai_compatible_responses", purpose="offline_validation")
    if selection.mapping_sha256 != subject["selected_mapping_sha256"]:
        core.fail("timed_first_live_evidence_mapping_mismatch")
    if snapshot() != files:
        core.fail("timed_first_live_evidence_snapshot_changed")
    return TimedFirstLiveEvidenceIdentity(historical, MappingProxyType(subject), expected_bundle_commitment_sha256,
        reviewed.commit_oid, reviewed.tree_oid, checked["manifest_sha256"], checked["publication_sha256"], checked["audit_chain_head_sha256"])
