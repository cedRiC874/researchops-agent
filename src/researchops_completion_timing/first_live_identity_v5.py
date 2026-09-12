"""V5 protocol and v11 historical source identity; no runtime admission.

The existing v4/v9 verifier is unchanged. This entry point reads raw Git objects,
never imports historical source, and never generates missing source profiles.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from researchops_external_closure.execution_binding_v4 import (
    HistoricalTimedProfileComponents, verify_historical_timed_profile,
)
from researchops_external_closure.git_objects_v2 import read_git_object_snapshot
from researchops_external_closure.io import read_regular_file_no_follow
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object
from . import contract as core


IMPLEMENTATION_PATH = 'evals/provider_completion_first_live_validation_v1/deepseek_responses_adapter_validation_implementation_v5.json'
IMPLEMENTATION_BYTES = 4773
IMPLEMENTATION_SHA256 = '09fab825fef91286576ef486f2860b47a9ff01efeba9c5d93dff8f0c3145d315'
IMPLEMENTATION_COMMITMENT_SHA256 = 'b07da916da4f444b55bd499ab436d85e35ed28c88dc821bc3817508252e1bc09'
_DOMAIN = 'researchops-provider-completion-first-live-implementation-v5'


def _fail(code):
    core.fail('timed_first_live_v5_' + code)


def _document(payload):
    if type(payload) is not bytes or len(payload) != IMPLEMENTATION_BYTES or core.digest(payload) != IMPLEMENTATION_SHA256:
        _fail('contract_invalid')
    document = decode_strict_json_object(payload, max_bytes=8192)
    if (document['commitment']['domain'] != _DOMAIN
            or core.digest(_DOMAIN.encode() + b'\0' + raw(document)) != IMPLEMENTATION_COMMITMENT_SHA256):
        _fail('commitment_invalid')
    return document


def verify_timed_implementation_files(files):
    """Verify a fixed protocol graph, not a source snapshot or executable proof."""
    if not isinstance(files, Mapping):
        _fail('files_invalid')
    document = _document(files.get(IMPLEMENTATION_PATH))
    expected = {IMPLEMENTATION_PATH} | {item['path'] for item in document['fixed_bindings']}
    if len(files) != len(expected) or set(files) != expected:
        _fail('files_invalid')
    for item in document['fixed_bindings']:
        payload = files[item['path']]
        if type(payload) is not bytes or len(payload) != item['bytes'] or core.digest(payload) != item['sha256']:
            _fail('dependency_drift')
    return {'status': 'timed_first_live_v5_protocol_identity_verified',
            'implementation_contract_sha256': IMPLEMENTATION_SHA256,
            'implementation_commitment_sha256': IMPLEMENTATION_COMMITMENT_SHA256,
            'runnable_implementation_verified': False, 'source_profile_verified': False,
            'first_live_success_verified': False, 'runtime_authority_granted': False,
            'provider_calls': 0, 'real_key_loads': 0}


def load_timed_implementation_contract(root):
    if not isinstance(root, Path):
        _fail('path_invalid')
    payload = read_regular_file_no_follow(root / IMPLEMENTATION_PATH, max_bytes=8192)
    document = _document(payload)
    files = {IMPLEMENTATION_PATH: payload}
    for item in document['fixed_bindings']:
        files[item['path']] = read_regular_file_no_follow(root / item['path'], max_bytes=32768)
    return verify_timed_implementation_files(files)


@dataclass(frozen=True, slots=True)
class TimedFirstLiveHistoricalIdentity:
    historical: HistoricalTimedProfileComponents
    control_protocol_commitment_sha256: str
    source_manifest_commitment_sha256: str
    subject: Mapping[str, str]
    source_recipe_version: int = field(default=4, init=False)
    implementation_version: int = field(default=5, init=False)
    control_protocol_identity_verified: bool = field(default=True, init=False)
    source_components_verified: bool = field(default=True, init=False)
    runnable_implementation_verified: bool = field(default=False, init=False)
    artifact_binding_verified: bool = field(default=False, init=False)
    review_verified: bool = field(default=False, init=False)
    registry_admission_verified: bool = field(default=False, init=False)
    runtime_authority_granted: bool = field(default=False, init=False)
    first_live_success_verified: bool = field(default=False, init=False)


def verify_historical_timed_implementation(project_root, *, commit, tree):
    """Derive review subject from the same fixed Git tree's v5/v11 bytes."""
    if not isinstance(project_root, Path):
        _fail('path_invalid')
    historical = verify_historical_timed_profile(project_root, commit=commit, tree=tree, profile='first_live')
    first = read_git_object_snapshot(project_root, commit, (IMPLEMENTATION_PATH,), expected_tree_oid=tree)
    document = _document(first.blobs[0].payload)
    paths = tuple(sorted({IMPLEMENTATION_PATH} | {item['path'] for item in document['fixed_bindings']}))
    snapshot = read_git_object_snapshot(project_root, commit, paths, expected_tree_oid=tree)
    if snapshot.entries != first.entries:
        _fail('tree_changed')
    verify_timed_implementation_files({blob.path: blob.payload for blob in snapshot.blobs})
    profile = document['execution_profile']
    hashes = historical.components.component_hashes
    subject = {name: profile[name] for name in
               ('provider_id', 'model_id', 'api_origin', 'api_surface', 'transport_id', 'adapter_version')}
    subject.update(
        first_live_execution_commit=historical.commit, first_live_execution_tree=historical.tree,
        first_live_source_integrity_commitment_sha256=historical.components.source_integrity_commitment_sha256,
        first_live_implementation_commitment_sha256=IMPLEMENTATION_COMMITMENT_SHA256,
        source_byte_inventory_sha256=hashes['source_byte_inventory_sha256'],
        dependency_lock_sha256=hashes['dependency_lock_sha256'], pyproject_sha256=hashes['pyproject_sha256'],
        selected_mapping_sha256=hashes['mapping_sha256'],
    )
    return TimedFirstLiveHistoricalIdentity(historical, IMPLEMENTATION_COMMITMENT_SHA256,
        historical.components.implementation_commitment_sha256, MappingProxyType(subject))
