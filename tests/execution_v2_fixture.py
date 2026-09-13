"""Synthetic source-profile fixtures; no current-tree manifests are written."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from researchops_external_closure import execution_components as base
from researchops_external_closure import execution_components_v2 as v2
from researchops_external_closure.admission_bundle_bytes import admission_bundle_commitment
from researchops_external_closure.admission_contract import CONTRACT_SHA256, load_admission_link_contract
from tests.test_external_closure_execution_components import component_fixture_files


ROOT=Path(__file__).resolve().parents[1]


def h(label):
    return hashlib.sha256(label.encode()).hexdigest()


def first_live_source_files():
    files,paths=component_fixture_files()
    files=dict(files); paths=set(paths)
    recipe_bytes=(ROOT/v2.RECIPE_PATH).read_bytes()
    recipe=json.loads(recipe_bytes)
    paths.update(recipe["extensions"]["mandatory_paths"])
    for prefix in recipe["extensions"]["contract_json_roots"]:
        paths.update(path.relative_to(ROOT).as_posix() for path in (ROOT/prefix).rglob("*.json"))
    # Copy the actual successor source. No synthetic runner stub is inserted.
    controller="src/researchops/deepseek_completion_first_live_successor.py"
    paths.add(controller)
    files[controller]=(ROOT/controller).read_bytes()
    selected=v2.select_profile_paths(tuple(paths),recipe_bytes,files[base.RECIPE_PATH],profile="first_live")
    for name in selected:
        if name not in files:
            files[name]=(ROOT/name).read_bytes()
    return {name:files[name] for name in selected},tuple(sorted(paths))


def registry_documents(subject,evidence_commitment,review_hash,evidence_commit="3"*40,evidence_tree="4"*40):
    contract=load_admission_link_contract(ROOT)
    triples=(("deepseek","responses","openai_compatible_responses"),("openai","responses","openai_responses"),
             ("anthropic","messages","litellm_anthropic_chat_completions"),("moonshot_kimi","openai_compatible_chat_completions","moonshot_direct_chat_completions_sse_v3"))
    entries=[]
    for i,(provider,surface,transport) in enumerate(triples):
        entries.append({"provider_id":provider,"api_surface":surface,"transport_id":transport,
                        "eligibility":"requires_verified_first_live_review" if i==0 else "not_eligible",
                        "mapping_origin":"immutable_v2_offline_projection","selected_mapping_sha256":subject["selected_mapping_sha256"] if i==0 else None,
                        "first_live_evidence_commitment_sha256":evidence_commitment if i==0 else None,
                        "review_document_sha256":review_hash if i==0 else None,"automatic_runtime_authority":False})
    registry={"schema_version":"provider-completion-runtime-registry/3.0","document_type":"completion_telemetry_evidence_gated_runtime_registry",
              "predecessor_registry_sha256":"916f37e6bd8fbc25e1e7f84dce7888cba55422c3982149574bce6706ce04f24a",
              "admission_contract_sha256":CONTRACT_SHA256,"entries":entries,"offline_projection_bytes_unchanged":True,
              "runtime_authority_granted_by_document":False,"online_execution_authorized":False,"provider_registration_authorized":False}
    registry_raw=json.dumps(registry,sort_keys=True,separators=(",",":")).encode()+b"\n"
    bundle={"schema_version":"provider-completion-runtime-registry-admission-bundle/1.0","document_type":"completion_telemetry_runtime_registry_admission_bundle",
            "predecessor_registry_sha256":registry["predecessor_registry_sha256"],"registry_document_sha256":hashlib.sha256(registry_raw).hexdigest(),
            "first_live_evidence_commitment_sha256":evidence_commitment,"review_document_sha256":review_hash,
            "reviewed_evidence_commit":evidence_commit,"reviewed_evidence_tree":evidence_tree,"subject":subject,
            "campaign_selected_mapping_sha256":subject["selected_mapping_sha256"],"admission_contract_sha256":CONTRACT_SHA256,
            "eligibility_requires_full_verification":True,"changes_to_other_provider_entries":False,
            "automatic_registry_promotion_allowed":False,"online_execution_authorized":False,"commitment_sha256":"0"*64}
    bundle["commitment_sha256"]=admission_bundle_commitment(contract,bundle)
    return registry,bundle,registry_raw,json.dumps(bundle,sort_keys=True,separators=(",",":")).encode()+b"\n"


def add_campaign_data(files,paths,first,registry_raw=b"{}",bundle_raw=b"{}"):
    result=dict(files)
    m,p=v2.PROFILE_PATHS["first_live"]
    result.update({m:first.manifest,p:first.plan,
                   "evals/provider_completion_runtime_registry_v3/runtime_registry_v3.json":registry_raw,
                   "evals/provider_completion_runtime_registry_v3/registry_admission_bundle_v1.json":bundle_raw})
    all_paths=tuple(sorted(set(paths)|set(result)))
    selected=v2.select_profile_paths(all_paths,result[v2.RECIPE_PATH],result[base.RECIPE_PATH],profile="campaign")
    return {name:result[name] for name in selected},all_paths
