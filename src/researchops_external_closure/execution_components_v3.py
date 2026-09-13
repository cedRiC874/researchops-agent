"""Pure timed first-live v9 / campaign v10 source profiles; no snapshot writer."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from . import execution_components as base
from . import execution_components_v2 as predecessor
from .errors import ExternalClosurePrimitiveError
from .primitives import canonical_json_bytes as raw, decode_strict_json_object


RECIPE_PATH = "evals/provider_completion_execution_binding_v3/execution_component_recipe_v3.json"
RECIPE_SHA256 = "26eefc30007379e0d3b4276b18b4fd586b2503948a0142c9c6041873aef836b2"
PROFILE_PATHS = {
    "first_live": ("evals/provider_completion_execution_binding_v3/implementation_manifest_first_live_v2.json", "evals/phase6_deepseek_depth60_plan_v9.json"),
    "campaign": ("evals/provider_completion_execution_binding_v3/implementation_manifest_campaign_v2.json", "evals/phase6_deepseek_depth60_plan_v10.json"),
}


def _fail(code):
    raise ExternalClosurePrimitiveError("execution_v3_" + code) from None


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _hash(domain, value):
    return _sha(domain.encode() + b"\0" + raw(value))


def decode_recipe(payload):
    if type(payload) is not bytes or len(payload) != 7325 or _sha(payload) != RECIPE_SHA256:
        _fail("recipe_invalid")
    recipe = decode_strict_json_object(payload, max_bytes=8192)
    if recipe["base_recipe"]["sha256"] != predecessor.RECIPE_SHA256:
        _fail("recipe_invalid")
    return recipe


def _bytes(files, path):
    if not isinstance(files, Mapping) or type(files.get(path)) is not bytes:
        _fail("file_missing")
    return files[path]


def select_profile_paths(available_paths, recipe_bytes, v2_recipe_bytes, base_recipe_bytes, *, profile):
    recipe = decode_recipe(recipe_bytes)
    if type(profile) is not str or profile not in PROFILE_PATHS:
        _fail("profile_invalid")
    paths = base._path_inventory(available_paths)
    selected = set(predecessor.select_profile_paths(paths, v2_recipe_bytes, base_recipe_bytes, profile="first_live"))
    excluded = set(recipe["all_profiles_excluded_paths"])
    if profile == "first_live":
        excluded.update(recipe["first_live_excluded_paths"])
    prefix = recipe["contract_selection"]["directory_prefix"]
    suffix = recipe["contract_selection"]["suffix"]
    selected.update(path for path in paths if path.startswith(prefix) and "/" in path[len(prefix):]
                    and path.endswith(suffix) and path not in excluded)
    required = set(recipe["mandatory_paths"])
    if profile == "campaign":
        required.update(recipe["campaign_required_paths"])
    if not required.issubset(paths):
        _fail("required_file_missing")
    selected.update(required)
    if set(PROFILE_PATHS[profile]).intersection(selected) or len(selected) > recipe["limits"]["selected_files"]:
        _fail("coverage_invalid")
    return tuple(sorted(selected))


def _validate_predecessor(files, recipe):
    frozen = recipe["frozen_predecessor_v7"]
    domains = predecessor.decode_recipe(_bytes(files, predecessor.RECIPE_PATH))["profiles"]["first_live"]
    values = {}
    for kind, field in (("plan", "plan_commitment_sha256"), ("manifest", "commitment_sha256")):
        payload = _bytes(files, frozen[kind + "_path"])
        if len(payload) != frozen[kind + "_bytes"] or _sha(payload) != frozen[kind + "_sha256"]:
            _fail("v7_predecessor_drift")
        value = decode_strict_json_object(payload, max_bytes=2_000_000)
        recorded = value.pop(field, None)
        if recorded != frozen[kind + "_commitment_sha256"] or _hash(domains[kind + "_domain"], value) != recorded:
            _fail("v7_predecessor_drift")
        values[kind] = value
    plan, manifest = values["plan"], values["manifest"]
    if (plan["implementation_manifest_sha256"] != frozen["manifest_sha256"]
        or plan["implementation_commitment_sha256"] != frozen["manifest_commitment_sha256"]
        or plan["component_hashes"] != manifest["component_hashes"]):
        _fail("v7_predecessor_drift")
    return {"plan_id": "phase6-deepseek-depth60-v7", "plan_commitment_sha256": frozen["plan_commitment_sha256"],
            "plan_sha256": frozen["plan_sha256"], "manifest_sha256": frozen["manifest_sha256"]}


@dataclass(frozen=True, slots=True)
class ProfileDocuments:
    manifest: bytes
    plan: bytes


@dataclass(frozen=True, slots=True)
class VerifiedTimedProfileComponents:
    profile: str
    implementation_commitment_sha256: str
    source_integrity_commitment_sha256: str
    component_hashes: Mapping[str, str]
    selected_file_count: int
    source_integrity_only: bool = True
    runtime_admission_verified: bool = False
    online_execution_authorized: bool = False
    historical_result_revalidated: bool = False


def build_profile_documents(files, *, available_paths, profile):
    recipe_bytes = _bytes(files, RECIPE_PATH)
    recipe = decode_recipe(recipe_bytes)
    v2_bytes, base_bytes = _bytes(files, predecessor.RECIPE_PATH), _bytes(files, base.RECIPE_PATH)
    paths = base._path_inventory(available_paths)
    selected = select_profile_paths(paths, recipe_bytes, v2_bytes, base_bytes, profile=profile)
    # selected is already bounded. Reject a larger supplied mapping before
    # enumerating its keys, not after allocating an unbounded set.
    if not isinstance(files, Mapping) or len(files) != len(selected) or set(files) != set(selected):
        _fail("file_set_mismatch")
    snapshot = {name: _bytes(files, name) for name in selected}
    limits = recipe["limits"]
    if any(len(value) > limits["file_bytes"] for value in snapshot.values()) or sum(map(len, snapshot.values())) > limits["total_bytes"]:
        _fail("byte_limit")
    lineage = _validate_predecessor(snapshot, recipe)
    base_paths = predecessor.select_profile_paths(paths, v2_bytes, base_bytes, profile="first_live")
    derived = predecessor.build_profile_documents({name: snapshot[name] for name in base_paths}, available_paths=paths, profile="first_live")
    components_manifest = decode_strict_json_object(derived.manifest, max_bytes=2_000_000)
    if profile == "campaign":
        first_paths = select_profile_paths(paths, recipe_bytes, v2_bytes, base_bytes, profile="first_live")
        first = build_profile_documents({name: snapshot[name] for name in first_paths}, available_paths=paths, profile="first_live")
        manifest_path, plan_path = PROFILE_PATHS["first_live"]
        if snapshot[manifest_path] != first.manifest or snapshot[plan_path] != first.plan:
            _fail("first_live_snapshot_drift")
        first_plan = decode_strict_json_object(first.plan, max_bytes=2_000_000)
        lineage = {"plan_id": "phase6-deepseek-depth60-v9", "plan_commitment_sha256": first_plan["plan_commitment_sha256"],
                   "plan_sha256": _sha(first.plan), "manifest_sha256": _sha(first.manifest)}
    inventory = [{"path": name, "bytes": len(snapshot[name]), "sha256": _sha(snapshot[name])} for name in selected]
    components = dict(components_manifest["component_hashes"])
    components["completion_telemetry_contract_bundle_sha256"] = _hash(recipe["extension_domain"],
        {"base_contract_bundle_sha256": components["completion_telemetry_contract_bundle_sha256"],
         "extension_inventory": [row for row in inventory if row["path"] not in base_paths]})
    spec = recipe["profiles"][profile]
    manifest = dict(schema_version="provider-completion-execution-implementation-manifest/3.0", profile=profile,
        status="locked_source_integrity_only", recipe_sha256=RECIPE_SHA256,
        base_derivation_commitment_sha256=components_manifest["commitment_sha256"], component_hashes=components,
        file_inventory=inventory, source_integrity_only=True, runtime_admission_verified=False,
        online_execution_authorized=False, historical_result_revalidated=False)
    manifest["commitment_sha256"] = _hash(spec["manifest_domain"], manifest)
    manifest_bytes = raw(manifest) + b"\n"
    plan = dict(schema_version=spec["plan_schema_version"], plan_id=spec["plan_id"], profile=profile,
        status="locked_offline_not_run", recipe_sha256=RECIPE_SHA256, predecessor=lineage, component_hashes=components,
        implementation_commitment_sha256=manifest["commitment_sha256"], implementation_manifest_sha256=_sha(manifest_bytes),
        source_integrity_only=True, online_execution_authorized=False, runtime_admission_verified=False,
        historical_result_revalidated=False, network_calls=0, model_calls=0)
    plan["plan_commitment_sha256"] = _hash(spec["plan_domain"], plan)
    return ProfileDocuments(manifest_bytes, raw(plan) + b"\n")


def verify_profile_documents(files, *, available_paths, profile, manifest, plan):
    if type(manifest) is not bytes or type(plan) is not bytes:
        _fail("artifact_bytes_invalid")
    expected = build_profile_documents(files, available_paths=available_paths, profile=profile)
    if manifest != expected.manifest or plan != expected.plan:
        _fail("component_drift")
    m, p = (decode_strict_json_object(value, max_bytes=2_000_000) for value in (manifest, plan))
    return VerifiedTimedProfileComponents(profile, m["commitment_sha256"], p["plan_commitment_sha256"],
        MappingProxyType(m["component_hashes"]), len(m["file_inventory"]))
