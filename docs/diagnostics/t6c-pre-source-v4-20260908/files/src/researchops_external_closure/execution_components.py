"""Pure component commitments for the local T6-C implementation candidate.

This module interprets the versioned recipe over caller-supplied immutable file
bytes. The historical wrapper must derive the complete file list from verified
Git trees; a caller-supplied manifest never chooses its own coverage. No source
code is executed and no filesystem, clock, Provider or authority is accessed.
Component identity is deliberately distinct from first-live/runtime admission.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, NoReturn

from .errors import ExternalClosurePrimitiveError
from .primitives import canonical_json_bytes, decode_strict_json_object


RECIPE_PATH = "evals/provider_completion_execution_binding_v1/execution_component_recipe_v1.json"
RECIPE_SHA256 = "4f86881db9c65012d51dbb87c52fe76a7fb3b0020c54c8a0ca8c341433eaa61b"
CONFIG_PATH = "evals/provider_completion_execution_binding_v1/runner_config_v1.json"
MANIFEST_PATH = "evals/provider_completion_execution_binding_v1/implementation_manifest_v1.json"
V6_PLAN_PATH = "evals/phase6_deepseek_depth60_plan_v6.json"
_MAX_FILES = 256
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_SUMMARY_TOKEN = object()


def _fail(suffix: str) -> NoReturn:
    raise ExternalClosurePrimitiveError("external_closure_components_" + suffix) from None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _domain_hash(domain: str, value: object) -> str:
    return _sha256(domain.encode("utf-8") + b"\0" + canonical_json_bytes(value))


def _safe_path(value: object) -> str:
    if type(value) is not str or not value:
        _fail("path_invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail("path_invalid")
    if size > 1024:
        _fail("path_invalid")
    if (
        "\\" in value
        or ":" in value
        or any(ord(character) < 32 for character in value)
        or unicodedata.normalize("NFC", value) != value
        or any(part in {"", ".", "..", ".git"} for part in value.split("/"))
    ):
        _fail("path_invalid")
    return value


def _recipe(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or _sha256(raw) != RECIPE_SHA256:
        _fail("recipe_mismatch")
    return decode_strict_json_object(raw, max_bytes=2_000_000)


def _path_inventory(available_paths: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(available_paths, Sequence) or isinstance(available_paths, (str, bytes)):
        _fail("inventory_invalid")
    if len(available_paths) > 16_384:
        _fail("inventory_limit")
    values = tuple(_safe_path(path) for path in available_paths)
    if len({value.casefold() for value in values}) != len(values):
        _fail("inventory_collision")
    return tuple(sorted(values))


def _groups(available_paths: Sequence[str], recipe: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    paths = _path_inventory(available_paths)
    available = set(paths)
    if not set(recipe["mandatory_files"]).issubset(available):
        _fail("mandatory_file_missing")
    result: dict[str, tuple[str, ...]] = {}
    for name, group in recipe["file_groups"].items():
        selected = {
            path for path in paths
            if path.endswith(group["suffix"])
            and any(path.startswith(prefix) for prefix in group["prefixes"])
        }
        for path in group["files"]:
            if path not in available:
                _fail("mandatory_file_missing")
            selected.add(path)
        selected.difference_update(group["exclude"])
        if not selected:
            _fail("group_empty")
        result[name] = tuple(sorted(selected))
    return result


def select_execution_component_paths(
    available_paths: Sequence[str], recipe_bytes: bytes
) -> tuple[str, ...]:
    """Derive complete coverage from tree inventory, never from a manifest."""

    recipe = _recipe(recipe_bytes)
    groups = _groups(available_paths, recipe)
    selected = set(recipe["mandatory_files"])
    selected.update(recipe["raw_components"].values())
    selected.update(path for names in groups.values() for path in names)
    selected.update(item["path"] for item in recipe["predecessors"].values())
    if len(selected) > _MAX_FILES or {MANIFEST_PATH, V6_PLAN_PATH}.intersection(selected):
        _fail("coverage_invalid")
    return tuple(sorted(selected))


def _snapshot_files(files: Mapping[str, bytes], selected: tuple[str, ...]) -> dict[str, bytes]:
    if not isinstance(files, Mapping) or len(files) > _MAX_FILES or set(files) != set(selected):
        _fail("file_set_mismatch")
    result: dict[str, bytes] = {}
    total = 0
    for path in selected:
        raw = files[path]
        if type(raw) is not bytes or len(raw) > _MAX_FILE_BYTES:
            _fail("file_bytes_invalid")
        total += len(raw)
        if total > _MAX_TOTAL_BYTES:
            _fail("total_bytes_limit")
        result[path] = raw
    return result


def _required_bytes(files: Mapping[str, bytes], path: str) -> bytes:
    raw = files.get(_safe_path(path))
    if type(raw) is not bytes:
        _fail("bound_file_missing")
    return raw


def _verify_frozen_graph(files: Mapping[str, bytes], recipe: Mapping[str, Any]) -> None:
    def visit(value: object) -> None:
        if isinstance(value, dict):
            path = value.get("relative_path")
            digest = value.get("file_sha256", value.get("sha256"))
            if type(path) is str and type(value.get("bytes")) is int and type(digest) is str:
                payload = _required_bytes(files, path)
                if len(payload) != value["bytes"] or _sha256(payload) != digest:
                    _fail("frozen_binding_mismatch")
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for predecessor in recipe["predecessors"].values():
        raw = _required_bytes(files, predecessor["path"])
        if len(raw) != predecessor["bytes"] or _sha256(raw) != predecessor["sha256"]:
            _fail("predecessor_mismatch")
        value = decode_strict_json_object(raw, max_bytes=2_000_000)
        if "contract_commitment" in value:
            if value["contract_commitment"]["sha256"] != predecessor["semantic_commitment"]:
                _fail("predecessor_mismatch")
        elif value.get("plan_commitment_sha256") != predecessor["semantic_commitment"]:
            _fail("predecessor_mismatch")
        visit(value)
    for historical in recipe["historical_plan_chain"]:
        raw = _required_bytes(files, historical["path"])
        if len(raw) != historical["bytes"] or _sha256(raw) != historical["sha256"]:
            _fail("historical_plan_mismatch")
        value = decode_strict_json_object(raw, max_bytes=2_000_000)
        recorded = value.pop("plan_commitment_sha256", None)
        if (
            recorded != historical["semantic_commitment"]
            or _domain_hash(historical["domain"], value) != recorded
        ):
            _fail("historical_plan_mismatch")
    for package in recipe["frozen_fixture_packages"]:
        raw = _required_bytes(files, package["manifest_path"])
        if len(raw) != package["bytes"] or _sha256(raw) != package["sha256"]:
            _fail("fixture_manifest_mismatch")
        manifest = decode_strict_json_object(raw, max_bytes=2_000_000)
        bindings = [
            (package["fixture_base"], item) for item in manifest["fixtures"]
        ] + [(package["base"], manifest[key]) for key in package["bound_top_fields"]]
        for base, item in bindings:
            target = _required_bytes(files, base + _safe_path(item["file"]))
            if len(target) != item["bytes"] or _sha256(target) != item["sha256"]:
                _fail("fixture_binding_mismatch")
    probe = recipe["frozen_probe_source"]
    raw = _required_bytes(files, probe["path"])
    if len(raw) != probe["bytes"] or _sha256(raw) != probe["sha256"]:
        _fail("probe_source_mismatch")


def _mapping_sha256(files: Mapping[str, bytes], recipe: Mapping[str, Any]) -> str:
    # This is the same pure projection used by the runtime's offline selector;
    # no VerifiedSurfaceSelection, runtime binding, tracker or client is made.
    from researchops_completion_telemetry.surface_mapping import (
        _offline_mapping_projection_from_documents,
    )

    spec = recipe["mapping_projection"]
    registry = decode_strict_json_object(_required_bytes(files, spec["registry_path"]), max_bytes=2_000_000)
    predecessor_raw = _required_bytes(files, spec["predecessor_path"])
    predecessor = decode_strict_json_object(predecessor_raw, max_bytes=2_000_000)
    bound = registry.get("predecessor_mapping")
    if (
        type(bound) is not dict
        or bound.get("bytes") != len(predecessor_raw)
        or bound.get("sha256") != _sha256(predecessor_raw)
        or bound.get("canonical_json_sha256") != _sha256(canonical_json_bytes(predecessor))
    ):
        _fail("mapping_binding_mismatch")
    config = recipe["runner_config_values"]
    try:
        projected = _offline_mapping_projection_from_documents(
            registry, predecessor,
            key=(config["provider_id"], config["api_surface"], config["transport_id"]),
        )
    except Exception:
        _fail("mapping_binding_mismatch")
    return _sha256(canonical_json_bytes(projected))


@dataclass(frozen=True, slots=True)
class ExecutionComponentDocuments:
    """Canonical candidate artifacts, not an authorization or execution proof."""

    implementation_manifest: bytes
    source_integrity_v6_plan: bytes


@dataclass(frozen=True, slots=True, init=False)
class VerifiedExecutionComponents:
    implementation_commitment_sha256: str
    source_integrity_commitment_sha256: str
    component_hashes: Mapping[str, str]
    file_count: int
    admission_verified: bool
    runtime_authority_granted: bool

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("VerifiedExecutionComponents requires successful recomputation")

    @classmethod
    def _create(cls, token: object, manifest: Mapping[str, Any], plan: Mapping[str, Any]):
        if token is not _SUMMARY_TOKEN:
            raise TypeError("execution_component_summary_construction_forbidden")
        value = object.__new__(cls)
        object.__setattr__(value, "implementation_commitment_sha256", manifest["commitment_sha256"])
        object.__setattr__(value, "source_integrity_commitment_sha256", plan["plan_commitment_sha256"])
        object.__setattr__(value, "component_hashes", MappingProxyType(dict(manifest["component_hashes"])))
        object.__setattr__(value, "file_count", len(manifest["file_inventory"]))
        object.__setattr__(value, "admission_verified", False)
        object.__setattr__(value, "runtime_authority_granted", False)
        return value


def build_execution_component_documents(
    files: Mapping[str, bytes], *, available_paths: Sequence[str]
) -> ExecutionComponentDocuments:
    """Build deterministic implementation/v6 identities from a complete snapshot."""

    recipe_raw = _required_bytes(files, RECIPE_PATH)
    recipe = _recipe(recipe_raw)
    paths = _path_inventory(available_paths)
    selected = select_execution_component_paths(paths, recipe_raw)
    snapshot = _snapshot_files(files, selected)
    _recipe(snapshot[RECIPE_PATH])
    groups = _groups(paths, recipe)
    _verify_frozen_graph(snapshot, recipe)
    config = decode_strict_json_object(snapshot[CONFIG_PATH], max_bytes=2_000_000)
    if canonical_json_bytes(config) != canonical_json_bytes(recipe["runner_config_values"]):
        _fail("runner_config_mismatch")

    inventory = [{"path": path, "bytes": len(snapshot[path]), "sha256": _sha256(snapshot[path])} for path in selected]
    by_path = {entry["path"]: entry for entry in inventory}
    components = {
        name: _domain_hash(recipe["file_groups"][name]["domain"], [by_path[path] for path in names])
        for name, names in groups.items()
    }
    components.update({name: _sha256(snapshot[path]) for name, path in recipe["raw_components"].items()})
    components["runner_config_commitment_sha256"] = _domain_hash(recipe["domains"]["runner_config"], config)
    components["telemetry_schema_sha256"] = _sha256(snapshot[recipe["schema_binding_path"]])
    components["mapping_sha256"] = _mapping_sha256(snapshot, recipe)
    manifest = {
        "schema_version": "provider-completion-execution-implementation-manifest/1.0",
        "status": "locked_source_integrity_only",
        "recipe_sha256": RECIPE_SHA256,
        "component_hashes": components,
        "file_inventory": inventory,
        "file_groups": {name: list(names) for name, names in groups.items()},
        "source_integrity_only": True,
        "runtime_authority_granted": False,
        "installed_environment_verified": False,
        "admission_verified": False,
    }
    manifest["commitment_sha256"] = _domain_hash(recipe["domains"]["implementation_manifest"], manifest)
    if set(manifest) != set(recipe["implementation_manifest_fields"]):
        _fail("manifest_fields_invalid")
    manifest_bytes = canonical_json_bytes(manifest) + b"\n"
    plan = {
        "schema_version": "6.0",
        "plan_id": "phase6-deepseek-depth60-v6",
        "status": "locked_offline_not_run",
        "recipe_sha256": RECIPE_SHA256,
        "predecessor": dict(recipe["predecessors"]["source_integrity_v5"]),
        "component_hashes": components,
        "implementation_commitment_sha256": manifest["commitment_sha256"],
        "implementation_manifest_sha256": _sha256(manifest_bytes),
        "online_execution_authorized": False,
        "network_calls": 0,
        "model_calls": 0,
        "historical_result_revalidation": False,
    }
    plan["plan_commitment_sha256"] = _domain_hash(recipe["domains"]["source_integrity_v6"], plan)
    if set(plan) != set(recipe["source_integrity_v6_fields"]):
        _fail("plan_fields_invalid")
    return ExecutionComponentDocuments(manifest_bytes, canonical_json_bytes(plan) + b"\n")


def verify_execution_component_documents(
    files: Mapping[str, bytes], *, available_paths: Sequence[str],
    implementation_manifest: bytes, source_integrity_v6_plan: bytes,
) -> VerifiedExecutionComponents:
    """Recompute every field and file; a self-consistent edited manifest is not enough."""

    if type(implementation_manifest) is not bytes or type(source_integrity_v6_plan) is not bytes:
        _fail("artifact_bytes_invalid")
    expected = build_execution_component_documents(files, available_paths=available_paths)
    if implementation_manifest != expected.implementation_manifest:
        _fail("implementation_drift")
    if source_integrity_v6_plan != expected.source_integrity_v6_plan:
        _fail("v6_drift")
    manifest = decode_strict_json_object(expected.implementation_manifest, max_bytes=2_000_000)
    plan = decode_strict_json_object(expected.source_integrity_v6_plan, max_bytes=2_000_000)
    return VerifiedExecutionComponents._create(_SUMMARY_TOKEN, manifest, plan)


__all__ = [
    "CONFIG_PATH", "MANIFEST_PATH", "RECIPE_PATH", "RECIPE_SHA256", "V6_PLAN_PATH",
    "ExecutionComponentDocuments", "VerifiedExecutionComponents",
    "build_execution_component_documents", "select_execution_component_paths",
    "verify_execution_component_documents",
]
