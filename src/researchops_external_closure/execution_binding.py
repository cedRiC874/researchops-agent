"""Historical Git binding for T6-C source components, without runtime admission.

No worktree content may repair a missing historical object. Both metadata and
source bytes come from the same exact commit/tree. First-live review, registry
promotion, installed environment and authorization are separate, unverified
claims; this component proof never permits closure by itself.
"""

from __future__ import annotations

import re
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .errors import ExternalClosurePrimitiveError
from .execution_components import (
    MANIFEST_PATH,
    RECIPE_PATH,
    V6_PLAN_PATH,
    build_execution_component_documents,
    select_execution_component_paths,
    verify_execution_component_documents,
)
from .git_objects import read_git_object_snapshot
from .io import read_regular_file_no_follow
from .primitives import canonical_json_bytes, decode_strict_json_object


_OID = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"(?!0{64}\Z)[0-9a-f]{64}\Z")
_TOKEN = object()
_COMPONENT_FIELDS = (
    "runner_config_commitment_sha256",
    "dependency_lock_sha256",
    "sanitizer_sha256",
    "closure_verifier_sha256",
    "telemetry_schema_sha256",
    "mapping_sha256",
)
_PROVIDER_FIELDS = (
    "provider_id", "model_id", "api_origin", "api_surface", "transport_id", "adapter_version",
)


def _fail(suffix: str) -> None:
    raise ExternalClosurePrimitiveError("external_closure_execution_" + suffix) from None


@dataclass(frozen=True, slots=True, init=False)
class VerifiedHistoricalExecutionComponents:
    execution_commit: str
    execution_tree: str
    implementation_commitment_sha256: str
    source_integrity_commitment_sha256: str
    component_hashes: Mapping[str, str]
    verified_file_count: int
    first_live_review_verified: bool
    registry_admission_verified: bool
    admission_verified: bool
    runtime_authority_granted: bool

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("historical component proof requires Git and component verification")

    @classmethod
    def _create(cls, token: object, *, commit: str, tree: str, summary: object):
        if token is not _TOKEN:
            raise TypeError("historical_component_proof_construction_forbidden")
        result = object.__new__(cls)
        values = {
            "execution_commit": commit,
            "execution_tree": tree,
            "implementation_commitment_sha256": summary.implementation_commitment_sha256,
            "source_integrity_commitment_sha256": summary.source_integrity_commitment_sha256,
            "component_hashes": MappingProxyType(dict(summary.component_hashes)),
            "verified_file_count": summary.file_count,
            "first_live_review_verified": False,
            "registry_admission_verified": False,
            "admission_verified": False,
            "runtime_authority_granted": False,
        }
        for name, value in values.items():
            object.__setattr__(result, name, value)
        return result


def verify_execution_component_binding(
    project_root: Path, execution_binding: Mapping[str, object]
) -> VerifiedHistoricalExecutionComponents:
    """Verify actual historical code/config hashes instead of self-reported equality.

    This deliberately does not interpret first_live_evidence_commitment_sha256
    or runtime_registry_commitment_sha256 as verified admission: their complete
    evidence/review aggregation protocol is a separate required successor.
    """

    if not isinstance(project_root, Path) or not isinstance(execution_binding, Mapping):
        _fail("binding_invalid")
    try:
        value = decode_strict_json_object(canonical_json_bytes(dict(execution_binding)), max_bytes=32_768)
    except Exception:
        _fail("binding_invalid")
    commit = value.get("execution_commit")
    tree = value.get("execution_tree")
    if type(commit) is not str or _OID.fullmatch(commit) is None:
        _fail("commit_invalid")
    if type(tree) is not str or _OID.fullmatch(tree) is None:
        _fail("tree_invalid")
    for field in (*_COMPONENT_FIELDS, "implementation_commitment_sha256", "source_integrity_commitment_sha256"):
        if type(value.get(field)) is not str or _SHA256.fullmatch(value[field]) is None:
            _fail("binding_invalid")

    metadata = read_git_object_snapshot(
        project_root, commit, (RECIPE_PATH, MANIFEST_PATH, V6_PLAN_PATH),
        expected_tree_oid=tree,
    )
    raw = {blob.path: blob.payload for blob in metadata.blobs}
    paths = tuple(entry.path for entry in metadata.entries if entry.mode in {"100644", "100755"})
    selected = select_execution_component_paths(paths, raw[RECIPE_PATH])
    # A mismatch against the committed declarations can be rejected early;
    # matching declarations never suffice for success. Every source byte is
    # still independently rehashed below before returning a verified result.
    recorded_manifest = decode_strict_json_object(raw[MANIFEST_PATH], max_bytes=2_000_000)
    recorded_plan = decode_strict_json_object(raw[V6_PLAN_PATH], max_bytes=2_000_000)
    recorded_components = recorded_manifest.get("component_hashes")
    if not isinstance(recorded_components, dict):
        _fail("component_metadata_invalid")
    if (
        value["implementation_commitment_sha256"] != recorded_manifest.get("commitment_sha256")
        or value["source_integrity_commitment_sha256"] != recorded_plan.get("plan_commitment_sha256")
        or any(value[field] != recorded_components.get(field) for field in _COMPONENT_FIELDS)
    ):
        _fail("component_binding_mismatch")
    snapshot = read_git_object_snapshot(project_root, commit, selected, expected_tree_oid=tree)
    if metadata.entries != snapshot.entries:
        _fail("tree_changed")
    files = {blob.path: blob.payload for blob in snapshot.blobs}
    summary = verify_execution_component_documents(
        files, available_paths=paths,
        implementation_manifest=raw[MANIFEST_PATH],
        source_integrity_v6_plan=raw[V6_PLAN_PATH],
    )
    expected = {field: summary.component_hashes[field] for field in _COMPONENT_FIELDS}
    expected.update(
        implementation_commitment_sha256=summary.implementation_commitment_sha256,
        source_integrity_commitment_sha256=summary.source_integrity_commitment_sha256,
    )
    recipe = decode_strict_json_object(raw[RECIPE_PATH], max_bytes=2_000_000)
    config = recipe["runner_config_values"]
    expected.update({field: config[field] for field in _PROVIDER_FIELDS})
    if any(value.get(field) != expected_value for field, expected_value in expected.items()):
        _fail("component_binding_mismatch")
    if value.get("closure_evidence_contract_commitment_sha256") != recipe["predecessors"]["closure_evidence"]["semantic_commitment"]:
        _fail("component_binding_mismatch")
    return VerifiedHistoricalExecutionComponents._create(_TOKEN, commit=commit, tree=tree, summary=summary)


def _current_file(root: Path, relative: str) -> bytes:
    parts = relative.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts) or "\\" in relative or ":" in relative:
        _fail("current_path_invalid")
    path = root
    try:
        for part in parts:
            path = path / part
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or int(getattr(metadata, "st_file_attributes", 0)) & 0x400:
                _fail("current_path_invalid")
        return read_regular_file_no_follow(path, max_bytes=8 * 1024 * 1024)
    except ExternalClosurePrimitiveError:
        raise
    except (OSError, ValueError):
        _fail("current_file_unavailable")


def _current_files(project_root: Path):
    """Read a bounded current-tree candidate; never used by the Git verifier."""

    if not isinstance(project_root, Path):
        _fail("current_root_invalid")
    try:
        root = project_root.resolve(strict=True)
        if not root.is_dir():
            _fail("current_root_invalid")
    except (OSError, ValueError):
        _fail("current_root_invalid")
    recipe_raw = _current_file(root, RECIPE_PATH)
    recipe = decode_strict_json_object(recipe_raw, max_bytes=2_000_000)
    # Validate the pinned recipe before interpreting any of its paths.
    from .execution_components import RECIPE_SHA256
    import hashlib
    if hashlib.sha256(recipe_raw).hexdigest() != RECIPE_SHA256:
        _fail("current_recipe_mismatch")
    prefixes = sorted({prefix for group in recipe["file_groups"].values() for prefix in group["prefixes"]})
    minimal: list[str] = []
    for prefix in prefixes:
        if not any(prefix.startswith(parent) for parent in minimal):
            minimal.append(prefix)
    available: set[str] = set()
    directories: dict[Path, tuple[int, int, int]] = {}
    count = 0
    for prefix in minimal:
        start = root / prefix
        # Resolve each prefix component lexically before walking it. scandir's
        # follow_symlinks=False check occurs before any directory is descended.
        parent = root
        try:
            for part in prefix.rstrip("/").split("/"):
                parent = parent / part
                info = parent.lstat()
                if stat.S_ISLNK(info.st_mode) or int(getattr(info, "st_file_attributes", 0)) & 0x400:
                    _fail("current_path_invalid")
            pending = [start]
            while pending:
                directory = pending.pop()
                identity = directory.lstat()
                if not stat.S_ISDIR(identity.st_mode) or int(getattr(identity, "st_file_attributes", 0)) & 0x400:
                    _fail("current_path_invalid")
                directories[directory] = (identity.st_dev, identity.st_ino, identity.st_mtime_ns)
                with os.scandir(directory) as children:
                    for child in children:
                        count += 1
                        if count > 16_384:
                            _fail("current_inventory_limit")
                        info = child.stat(follow_symlinks=False)
                        if child.is_symlink() or int(getattr(info, "st_file_attributes", 0)) & 0x400:
                            _fail("current_path_invalid")
                        path = Path(child.path)
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(path)
                        elif stat.S_ISREG(info.st_mode):
                            available.add(path.relative_to(root).as_posix())
                        else:
                            _fail("current_path_invalid")
        except ExternalClosurePrimitiveError:
            raise
        except OSError:
            _fail("current_file_unavailable")
    available.update(recipe["mandatory_files"])
    available.update(recipe["raw_components"].values())
    available.update(path for group in recipe["file_groups"].values() for path in group["files"])
    available.update(item["path"] for item in recipe["predecessors"].values())
    paths = tuple(sorted(available))
    selected = select_execution_component_paths(paths, recipe_raw)
    files: dict[str, bytes] = {}
    total = 0
    for path in selected:
        raw = _current_file(root, path)
        total += len(raw)
        if total > 32 * 1024 * 1024:
            _fail("current_bytes_limit")
        files[path] = raw
    for path in selected:
        if _current_file(root, path) != files[path]:
            _fail("current_tree_changed")
    try:
        for path, identity in directories.items():
            observed = path.lstat()
            if (
                not stat.S_ISDIR(observed.st_mode)
                or int(getattr(observed, "st_file_attributes", 0)) & 0x400
                or (observed.st_dev, observed.st_ino, observed.st_mtime_ns) != identity
            ):
                _fail("current_tree_changed")
    except OSError:
        _fail("current_tree_changed")
    return root, files, paths


def build_current_execution_component_documents(project_root: Path):
    """Return candidate artifact bytes without writing or overwriting any file."""

    _root, files, paths = _current_files(project_root)
    return build_execution_component_documents(files, available_paths=paths)


def verify_current_execution_components(project_root: Path):
    """Recompute a current-tree source commitment; this does not grant execution."""

    root, files, paths = _current_files(project_root)
    return verify_execution_component_documents(
        files, available_paths=paths,
        implementation_manifest=_current_file(root, MANIFEST_PATH),
        source_integrity_v6_plan=_current_file(root, V6_PLAN_PATH),
    )


__all__ = [
    "VerifiedHistoricalExecutionComponents", "verify_execution_component_binding",
    "build_current_execution_component_documents", "verify_current_execution_components",
]
