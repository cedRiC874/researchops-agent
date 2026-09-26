"""Pinned historical v2 replay plus synthetic campaign data, not current coverage.

The pre-bridge main snapshot supplies both the complete inventory and raw blobs.
No historical Python is executed. Current source coverage belongs to internal v3.
The pinned objects must be available locally (CI already fetches full history).
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from researchops_external_closure import execution_components as base
from researchops_external_closure import execution_components_v2 as v2
from researchops_external_closure.admission_bundle_bytes import admission_bundle_commitment
from researchops_external_closure.admission_contract import CONTRACT_SHA256, load_admission_link_contract
from researchops_external_closure.git_objects import read_git_object_snapshot


ROOT=Path(__file__).resolve().parents[1]
HISTORICAL_COMMIT="5605396e165fc5140eba54340d5a9c93f67540dc"
HISTORICAL_TREE="60a29c8407e55de8ea9b5700fc478f37044d1a49"


def h(label):
    return hashlib.sha256(label.encode()).hexdigest()


@lru_cache(maxsize=1)
def _historical_first_live_snapshot():
    metadata=read_git_object_snapshot(ROOT,HISTORICAL_COMMIT,
        (base.RECIPE_PATH,v2.RECIPE_PATH),expected_tree_oid=HISTORICAL_TREE)
    paths=tuple(sorted(entry.path for entry in metadata.entries if entry.mode in {"100644","100755"}))
    recipes={blob.path:blob.payload for blob in metadata.blobs}
    selected=v2.select_profile_paths(paths,recipes[v2.RECIPE_PATH],recipes[base.RECIPE_PATH],profile="first_live")
    snapshot=read_git_object_snapshot(ROOT,HISTORICAL_COMMIT,selected,expected_tree_oid=HISTORICAL_TREE)
    if snapshot.entries!=metadata.entries:
        raise ValueError("historical_v2_inventory_drift")
    # Cache only immutable values; each caller gets its own mutable fixture map.
    return tuple((blob.path,blob.payload) for blob in snapshot.blobs),paths


def first_live_source_files():
    """Replay the fixed historical source; never substitute current worktree files."""
    items,paths=_historical_first_live_snapshot()
    return dict(items),paths


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
