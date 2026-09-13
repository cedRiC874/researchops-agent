"""Timed implementation-protocol identity from fixed bytes, not runnable proof."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from collections.abc import Mapping

from researchops_external_closure.execution_binding_v3 import HistoricalTimedProfileComponents, verify_historical_timed_profile
from researchops_external_closure.git_objects import read_git_object_snapshot
from researchops_external_closure.io import read_regular_file_no_follow
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object
from . import contract as core


IMPLEMENTATION_PATH = "evals/provider_completion_first_live_validation_v1/deepseek_responses_adapter_validation_implementation_v4.json"
IMPLEMENTATION_SHA256 = "25e849a057121c56e162b618c2c9b5be117b889df61387279ac423a423dbca52"
IMPLEMENTATION_COMMITMENT_SHA256 = "86b7b38a2f07828365fba9e0e0488de731e1cf4a07dac193c33622b2a66557b7"
_DOMAIN = "researchops-provider-completion-first-live-implementation-v4"


def _document(payload):
    if type(payload) is not bytes or len(payload) != 4524 or core.digest(payload) != IMPLEMENTATION_SHA256:
        core.fail("timed_first_live_implementation_contract_invalid")
    document = decode_strict_json_object(payload, max_bytes=8192)
    if (document["commitment"]["domain"] != _DOMAIN
        or core.digest(_DOMAIN.encode() + b"\0" + raw(document)) != IMPLEMENTATION_COMMITMENT_SHA256):
        core.fail("timed_first_live_implementation_commitment_invalid")
    return document


def verify_timed_implementation_files(files):
    """Pure pinned protocol graph. A valid graph is not a working implementation."""
    if not isinstance(files, Mapping):
        core.fail("timed_first_live_implementation_files_invalid")
    document = _document(files.get(IMPLEMENTATION_PATH))
    expected = {IMPLEMENTATION_PATH} | {item["path"] for item in document["fixed_bindings"]}
    if len(files) != len(expected) or set(files) != expected:
        core.fail("timed_first_live_implementation_files_invalid")
    for item in document["fixed_bindings"]:
        payload = files[item["path"]]
        if type(payload) is not bytes or len(payload) != item["bytes"] or core.digest(payload) != item["sha256"]:
            core.fail("timed_first_live_implementation_dependency_drift")
    return {"status": "timed_first_live_protocol_identity_verified", "implementation_contract_sha256": IMPLEMENTATION_SHA256,
        "implementation_commitment_sha256": IMPLEMENTATION_COMMITMENT_SHA256,
        "runnable_implementation_verified": False, "source_profile_verified": False,
        "first_live_success_verified": False, "runtime_authority_granted": False, "provider_calls": 0, "real_key_loads": 0}


def load_timed_implementation_contract(root):
    """Read fixed public protocol files only; no source snapshot is generated."""
    if not isinstance(root, Path):
        core.fail("timed_first_live_implementation_path_invalid")
    payload = read_regular_file_no_follow(root / IMPLEMENTATION_PATH, max_bytes=8192)
    document = _document(payload)
    files = {IMPLEMENTATION_PATH: payload}
    for item in document["fixed_bindings"]:
        files[item["path"]] = read_regular_file_no_follow(root / item["path"], max_bytes=32768)
    return verify_timed_implementation_files(files)


@dataclass(frozen=True, slots=True)
class TimedFirstLiveHistoricalIdentity:
    historical: HistoricalTimedProfileComponents
    control_protocol_commitment_sha256: str
    source_manifest_commitment_sha256: str
    subject: Mapping[str, str]
    control_protocol_identity_verified: bool = True
    source_components_verified: bool = True
    runnable_implementation_verified: bool = False
    artifact_binding_verified: bool = False
    review_verified: bool = False
    registry_admission_verified: bool = False
    runtime_authority_granted: bool = False
    first_live_success_verified: bool = False


def verify_historical_timed_implementation(project_root, *, commit, tree):
    """Derive subject from actual Git/profile/protocol, never from a review."""
    if not isinstance(project_root, Path):
        core.fail("timed_first_live_implementation_path_invalid")
    historical = verify_historical_timed_profile(project_root, commit=commit, tree=tree, profile="first_live")
    first = read_git_object_snapshot(project_root, commit, (IMPLEMENTATION_PATH,), expected_tree_oid=tree)
    document = _document(first.blobs[0].payload)
    paths = tuple(sorted({IMPLEMENTATION_PATH} | {item["path"] for item in document["fixed_bindings"]}))
    snapshot = read_git_object_snapshot(project_root, commit, paths, expected_tree_oid=tree)
    if snapshot.entries != first.entries:
        core.fail("timed_first_live_implementation_tree_changed")
    verify_timed_implementation_files({blob.path: blob.payload for blob in snapshot.blobs})
    profile = document["execution_profile"]
    hashes = historical.components.component_hashes
    subject = {name: profile[name] for name in ("provider_id", "model_id", "api_origin", "api_surface", "transport_id", "adapter_version")}
    subject.update(first_live_execution_commit=historical.commit, first_live_execution_tree=historical.tree,
        first_live_source_integrity_commitment_sha256=historical.components.source_integrity_commitment_sha256,
        first_live_implementation_commitment_sha256=IMPLEMENTATION_COMMITMENT_SHA256,
        source_byte_inventory_sha256=hashes["source_byte_inventory_sha256"], dependency_lock_sha256=hashes["dependency_lock_sha256"],
        pyproject_sha256=hashes["pyproject_sha256"], selected_mapping_sha256=hashes["mapping_sha256"])
    return TimedFirstLiveHistoricalIdentity(historical, IMPLEMENTATION_COMMITMENT_SHA256,
        historical.components.implementation_commitment_sha256, MappingProxyType(subject))
