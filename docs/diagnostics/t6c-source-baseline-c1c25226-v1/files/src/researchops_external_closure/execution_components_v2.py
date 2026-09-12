"""Pure first-live v7 / campaign v8 source snapshots, with unchanged v6 history.

Both profiles use the existing v6 byte-derivation engine without publishing a
new v6 artifact. v8 additionally revalidates the exact v7 snapshot and binds the
two later registry data files. No code or mapping change is waived by this split.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from . import execution_components as base
from .errors import ExternalClosurePrimitiveError
from .primitives import canonical_json_bytes, decode_strict_json_object


RECIPE_PATH="evals/provider_completion_execution_binding_v2/execution_component_recipe_v2.json"
RECIPE_SHA256="a474542348d71d6becced84c327cfcca6353ffe33033be4426370c8ef8d58c6e"
PROFILE_PATHS={
    "first_live":("evals/provider_completion_execution_binding_v2/implementation_manifest_first_live_v1.json","evals/phase6_deepseek_depth60_plan_v7.json"),
    "campaign":("evals/provider_completion_execution_binding_v2/implementation_manifest_campaign_v1.json","evals/phase6_deepseek_depth60_plan_v8.json"),
}


def _fail(code):
    raise ExternalClosurePrimitiveError("execution_v2_"+code) from None


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _hash(domain,value):
    return _sha(domain.encode()+b"\0"+canonical_json_bytes(value))


def decode_recipe(raw: bytes) -> dict:
    if type(raw) is not bytes or len(raw)!=7076 or _sha(raw)!=RECIPE_SHA256:
        _fail("recipe_invalid")
    return decode_strict_json_object(raw,max_bytes=8192)


def _raw(files,path):
    if not isinstance(files,Mapping):
        _fail("file_set_mismatch")
    value=files.get(path)
    if type(value) is not bytes:
        _fail("file_missing")
    return value


def select_profile_paths(available_paths,recipe_bytes: bytes,base_recipe_bytes: bytes,*,profile: str) -> tuple[str,...]:
    recipe=decode_recipe(recipe_bytes)
    if type(profile) is not str or profile not in PROFILE_PATHS:
        _fail("profile_invalid")
    if type(base_recipe_bytes) is not bytes or _sha(base_recipe_bytes)!=recipe["base_recipe"]["sha256"]:
        _fail("base_recipe_invalid")
    paths=base._path_inventory(available_paths)
    selected=set(base.select_execution_component_paths(paths,base_recipe_bytes))
    extension=recipe["extensions"]
    excluded=set(extension["all_profiles_excluded_paths"])
    if profile=="first_live":
        excluded.update(extension["first_live_excluded_paths"])
    selected.update(path for path in paths if path.endswith(".json") and any(path.startswith(prefix) for prefix in extension["contract_json_roots"]) and path not in excluded)
    required=set(extension["mandatory_paths"])
    if profile=="campaign":
        required.update(extension["campaign_required_paths"])
    if not required.issubset(paths):
        _fail("required_file_missing")
    selected.update(required)
    if set(PROFILE_PATHS[profile]).intersection(selected) or len(selected)>256:
        _fail("coverage_invalid")
    return tuple(sorted(selected))


@dataclass(frozen=True,slots=True)
class ProfileDocuments:
    manifest: bytes
    plan: bytes


@dataclass(frozen=True,slots=True)
class VerifiedProfileComponents:
    profile: str
    implementation_commitment_sha256: str
    source_integrity_commitment_sha256: str
    component_hashes: Mapping[str,str]
    selected_file_count: int
    source_integrity_only: bool = True
    runtime_admission_verified: bool = False
    online_execution_authorized: bool = False
    historical_result_revalidated: bool = False


def _validate_old_v6(files,recipe,base_recipe):
    predecessor=recipe["frozen_predecessor_v6"]
    values={}
    for kind in ("plan","manifest"):
        raw=_raw(files,predecessor[kind+"_path"])
        if len(raw)!=predecessor[kind+"_bytes"] or _sha(raw)!=predecessor[kind+"_sha256"]:
            _fail("v6_predecessor_drift")
        value=decode_strict_json_object(raw,max_bytes=2_000_000)
        key="plan_commitment_sha256" if kind=="plan" else "commitment_sha256"
        recorded=value.pop(key,None)
        domain=base_recipe["domains"]["source_integrity_v6" if kind=="plan" else "implementation_manifest"]
        if recorded!=predecessor[kind+"_commitment_sha256"] or _hash(domain,value)!=recorded:
            _fail("v6_predecessor_drift")
        values[kind]=value
    if (values["plan"]["implementation_manifest_sha256"]!=predecessor["manifest_sha256"]
        or values["plan"]["implementation_commitment_sha256"]!=predecessor["manifest_commitment_sha256"]
        or values["plan"]["component_hashes"]!=values["manifest"]["component_hashes"]):
        _fail("v6_predecessor_drift")


def build_profile_documents(files: Mapping[str,bytes],*,available_paths,profile: str) -> ProfileDocuments:
    recipe_raw=_raw(files,RECIPE_PATH)
    recipe=decode_recipe(recipe_raw)
    base_raw=_raw(files,recipe["base_recipe"]["path"])
    paths=base._path_inventory(available_paths)
    selected=select_profile_paths(paths,recipe_raw,base_raw,profile=profile)
    if not isinstance(files,Mapping) or set(files)!=set(selected):
        _fail("file_set_mismatch")
    snapshot={name:_raw(files,name) for name in selected}
    if any(len(raw)>8*1024*1024 for raw in snapshot.values()) or sum(map(len,snapshot.values()))>32*1024*1024:
        _fail("byte_limit")
    base_paths=base.select_execution_component_paths(paths,base_raw)
    derived=base.build_execution_component_documents({name:snapshot[name] for name in base_paths},available_paths=paths)
    base_manifest=decode_strict_json_object(derived.implementation_manifest,max_bytes=2_000_000)
    base_recipe=decode_strict_json_object(base_raw,max_bytes=2_000_000)
    _validate_old_v6(snapshot,recipe,base_recipe)
    for item in recipe["fixed_bindings"]:
        raw=_raw(snapshot,item["path"])
        if len(raw)!=item["bytes"] or _sha(raw)!=item["sha256"]:
            _fail("fixed_binding_drift")
    admission=decode_strict_json_object(snapshot["evals/provider_completion_admission_link_v1/admission_link_contract_v1.json"],max_bytes=8192)
    for item in admission["schema_bindings"]:
        raw=_raw(snapshot,"evals/provider_completion_admission_link_v1/schemas/"+item["name"])
        if len(raw)!=item["bytes"] or _sha(raw)!=item["sha256"]:
            _fail("admission_schema_drift")
    predecessor={"plan_id":"phase6-deepseek-depth60-v6","plan_commitment_sha256":recipe["frozen_predecessor_v6"]["plan_commitment_sha256"],
                 "plan_sha256":recipe["frozen_predecessor_v6"]["plan_sha256"],"manifest_sha256":recipe["frozen_predecessor_v6"]["manifest_sha256"]}
    if profile=="campaign":
        first_paths=select_profile_paths(paths,recipe_raw,base_raw,profile="first_live")
        first=build_profile_documents({name:snapshot[name] for name in first_paths},available_paths=paths,profile="first_live")
        manifest_path,plan_path=PROFILE_PATHS["first_live"]
        if snapshot[manifest_path]!=first.manifest or snapshot[plan_path]!=first.plan:
            _fail("first_live_snapshot_drift")
        plan=decode_strict_json_object(first.plan,max_bytes=2_000_000)
        predecessor={"plan_id":"phase6-deepseek-depth60-v7","plan_commitment_sha256":plan["plan_commitment_sha256"],
                     "plan_sha256":_sha(first.plan),"manifest_sha256":_sha(first.manifest)}
    inventory=[{"path":name,"bytes":len(snapshot[name]),"sha256":_sha(snapshot[name])} for name in selected]
    extension_inventory=[row for row in inventory if row["path"] not in base_paths]
    components=dict(base_manifest["component_hashes"])
    components["completion_telemetry_contract_bundle_sha256"]=_hash(recipe["extensions"]["domain"],
        {"base_contract_bundle_sha256":components["completion_telemetry_contract_bundle_sha256"],"extension_inventory":extension_inventory})
    source_rows=[row for row in inventory if row["path"].startswith("src/") and row["path"].endswith(".py")]
    components["source_byte_inventory_sha256"]=_hash(admission["byte_protocol"]["domain_values"]["source_inventory"],source_rows)
    components["admission_contract_sha256"]=_sha(snapshot["evals/provider_completion_admission_link_v1/admission_link_contract_v1.json"])
    spec=recipe["profiles"][profile]
    manifest={"schema_version":"provider-completion-execution-implementation-manifest/2.0","profile":profile,"status":"locked_source_integrity_only",
              "recipe_sha256":RECIPE_SHA256,"base_derivation_commitment_sha256":base_manifest["commitment_sha256"],
              "component_hashes":components,"file_inventory":inventory,"source_integrity_only":True,"runtime_admission_verified":False,
              "online_execution_authorized":False,"historical_result_revalidated":False}
    manifest["commitment_sha256"]=_hash(spec["manifest_domain"],manifest)
    manifest_raw=canonical_json_bytes(manifest)+b"\n"
    plan={"schema_version":spec["plan_schema_version"],"plan_id":spec["plan_id"],"profile":profile,"status":"locked_offline_not_run",
          "recipe_sha256":RECIPE_SHA256,"predecessor":predecessor,"component_hashes":components,
          "implementation_commitment_sha256":manifest["commitment_sha256"],"implementation_manifest_sha256":_sha(manifest_raw),
          "source_integrity_only":True,"online_execution_authorized":False,"runtime_admission_verified":False,
          "historical_result_revalidated":False,"network_calls":0,"model_calls":0}
    plan["plan_commitment_sha256"]=_hash(spec["plan_domain"],plan)
    return ProfileDocuments(manifest_raw,canonical_json_bytes(plan)+b"\n")


def verify_profile_documents(files,*,available_paths,profile: str,manifest: bytes,plan: bytes) -> VerifiedProfileComponents:
    if type(manifest) is not bytes or type(plan) is not bytes:
        _fail("artifact_bytes_invalid")
    expected=build_profile_documents(files,available_paths=available_paths,profile=profile)
    if manifest!=expected.manifest or plan!=expected.plan:
        _fail("component_drift")
    m=decode_strict_json_object(manifest,max_bytes=2_000_000)
    p=decode_strict_json_object(plan,max_bytes=2_000_000)
    return VerifiedProfileComponents(profile,m["commitment_sha256"],p["plan_commitment_sha256"],MappingProxyType(m["component_hashes"]),len(m["file_inventory"]))


__all__=["RECIPE_PATH","RECIPE_SHA256","PROFILE_PATHS","ProfileDocuments","VerifiedProfileComponents",
         "decode_recipe","select_profile_paths","build_profile_documents","verify_profile_documents"]
