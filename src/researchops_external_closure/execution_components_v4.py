"""Pure 320-file source successor; old engines and their limits are unchanged."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from types import MappingProxyType

from . import execution_components as v1, execution_components_v2 as v2, execution_components_v3 as v3
from .errors import ExternalClosurePrimitiveError
from .primitives import canonical_json_bytes as raw, decode_strict_json_object

RECIPE_PATH='evals/provider_completion_execution_binding_v4/execution_component_recipe_v4.json'
RECIPE_SHA256='d07be82febc00009b03a63541c3a86d3dea81f9bd00983c157e41f28eeaded08'
RECIPE_BYTES=18216
MAX_SELECTED_FILES=320
PROFILE_PATHS={
    'first_live':('evals/provider_completion_execution_binding_v4/implementation_manifest_first_live_v3.json','evals/phase6_deepseek_depth60_plan_v11.json'),
    'campaign':('evals/provider_completion_execution_binding_v4/implementation_manifest_campaign_v3.json','evals/phase6_deepseek_depth60_plan_v12.json'),
}


def _fail(code): raise ExternalClosurePrimitiveError('execution_v4_'+code) from None
def _sha(payload): return hashlib.sha256(payload).hexdigest()
def _hash(domain,value): return _sha(domain.encode()+b'\0'+raw(value))


def decode_recipe(payload):
    if type(payload) is not bytes or len(payload)!=RECIPE_BYTES or _sha(payload)!=RECIPE_SHA256:
        _fail('recipe_invalid')
    return decode_strict_json_object(payload,max_bytes=32768)


def _recipes(recipe,files):
    values=[]
    for spec in recipe['predecessor_recipes']:
        payload=files.get(spec['path'])
        if type(payload) is not bytes or _sha(payload)!=spec['sha256']: _fail('predecessor_recipe_invalid')
        values.append(decode_strict_json_object(payload,max_bytes=2_000_000))
    # The pinned predecessor decoders additionally enforce their own identities.
    v1._recipe(files[v1.RECIPE_PATH]); v2.decode_recipe(files[v2.RECIPE_PATH]); v3.decode_recipe(files[v3.RECIPE_PATH])
    return values


def select_profile_paths(available_paths,recipe_bytes,*,predecessor_recipe_bytes,profile):
    recipe=decode_recipe(recipe_bytes)
    if type(profile) is not str or profile not in PROFILE_PATHS: _fail('profile_invalid')
    base,second,third=_recipes(recipe,predecessor_recipe_bytes)
    paths=v1._path_inventory(available_paths); available=set(paths)
    # Reuse path/group semantics only, never a predecessor's bounded selector.
    groups=v1._groups(paths,base)
    selected=set(base['mandatory_files']) | set(base['raw_components'].values())
    selected.update(path for group in groups.values() for path in group)
    selected.update(spec['path'] for spec in base['predecessors'].values())
    selection=recipe['selection']
    prefix=selection['contract_directory_prefix']; suffix=selection['contract_suffix']
    selected.update(path for path in paths if path.startswith(prefix) and '/' in path[len(prefix):] and path.endswith(suffix))
    selected.update(path for path in paths if path.startswith(selection['source_prefix']) and path.endswith(selection['source_suffix']))
    excluded=set(selection['all_profiles_excluded_paths'])
    if profile=='first_live': excluded.update(selection['first_live_excluded_paths'])
    selected.difference_update(excluded)
    required=set(base['mandatory_files']) | set(second['extensions']['mandatory_paths']) | set(third['mandatory_paths']) | set(selection['mandatory_paths'])
    if profile=='campaign': required.update(selection['campaign_required_paths'])
    if not required.issubset(available): _fail('required_file_missing')
    selected.update(required)
    if set(PROFILE_PATHS[profile]).intersection(selected) or len(selected)>recipe['limits']['selected_files']:
        _fail('coverage_invalid')
    return tuple(sorted(selected))


def _frozen_lineage(snapshot,base,second,third):
    v1._verify_frozen_graph(snapshot,base)
    v2._validate_old_v6(snapshot,second,base)
    for item in second['fixed_bindings']:
        payload=snapshot.get(item['path'])
        if type(payload) is not bytes or len(payload)!=item['bytes'] or _sha(payload)!=item['sha256']:
            _fail('fixed_binding_drift')
    admission=decode_strict_json_object(snapshot['evals/provider_completion_admission_link_v1/admission_link_contract_v1.json'],max_bytes=8192)
    for item in admission['schema_bindings']:
        payload=snapshot.get('evals/provider_completion_admission_link_v1/schemas/'+item['name'])
        if type(payload) is not bytes or len(payload)!=item['bytes'] or _sha(payload)!=item['sha256']:
            _fail('admission_schema_drift')
    return v3._validate_predecessor(snapshot,third),admission


@dataclass(frozen=True,slots=True)
class ProfileDocuments:
    manifest: bytes
    plan: bytes


@dataclass(frozen=True,slots=True)
class VerifiedTimedProfileComponents:
    profile: str
    implementation_commitment_sha256: str
    source_integrity_commitment_sha256: str
    component_hashes: Mapping[str,str]
    selected_file_count: int
    source_integrity_only: bool=True
    runtime_admission_verified: bool=False
    online_execution_authorized: bool=False
    historical_result_revalidated: bool=False


def build_profile_documents(files,*,available_paths,profile):
    if not isinstance(files,Mapping) or len(files)>MAX_SELECTED_FILES: _fail('file_set_limit')
    recipe_bytes=files.get(RECIPE_PATH); recipe=decode_recipe(recipe_bytes)
    predecessors={spec['path']:files.get(spec['path']) for spec in recipe['predecessor_recipes']}
    base,second,third=_recipes(recipe,predecessors)
    paths=v1._path_inventory(available_paths)
    selected=select_profile_paths(paths,recipe_bytes,predecessor_recipe_bytes=predecessors,profile=profile)
    if len(files)!=len(selected) or set(files)!=set(selected): _fail('file_set_mismatch')
    snapshot={}; total=0
    for path in selected:
        payload=files[path]
        if type(payload) is not bytes or len(payload)>recipe['limits']['file_bytes']: _fail('file_bytes_invalid')
        total+=len(payload)
        if total>recipe['limits']['total_bytes']: _fail('total_bytes_limit')
        snapshot[path]=payload
    lineage,admission=_frozen_lineage(snapshot,base,second,third)
    config=decode_strict_json_object(snapshot[v1.CONFIG_PATH],max_bytes=2_000_000)
    if raw(config)!=raw(base['runner_config_values']): _fail('runner_config_mismatch')
    if profile=='campaign':
        first_paths=select_profile_paths(paths,recipe_bytes,predecessor_recipe_bytes=predecessors,profile='first_live')
        first=build_profile_documents({path:snapshot[path] for path in first_paths},available_paths=paths,profile='first_live')
        first_manifest,first_plan=PROFILE_PATHS['first_live']
        if snapshot[first_manifest]!=first.manifest or snapshot[first_plan]!=first.plan: _fail('first_live_snapshot_drift')
        first_value=decode_strict_json_object(first.plan,max_bytes=2_000_000)
        lineage=dict(plan_id=recipe['profiles']['first_live']['plan_id'],plan_commitment_sha256=first_value['plan_commitment_sha256'],
                     plan_sha256=_sha(first.plan),manifest_sha256=_sha(first.manifest))
    inventory=[dict(path=path,bytes=len(snapshot[path]),sha256=_sha(snapshot[path])) for path in selected]
    source=[row for row in inventory if row['path'].startswith('src/') and row['path'].endswith('.py')]
    groups={'all_selected_src_python':source,
            'selected_telemetry_and_timing_python':[row for row in source if row['path'].startswith(('src/researchops_completion_telemetry/','src/researchops_completion_timing/'))],
            'all_selected_json':[row for row in inventory if row['path'].endswith('.json')]}
    components={name:_hash(spec['domain'],groups[spec['selection']]) for name,spec in recipe['component_groups'].items()}
    components.update({name:_sha(snapshot[path]) for name,path in base['raw_components'].items()})
    components.update(runner_config_commitment_sha256=_hash(base['domains']['runner_config'],config),
        telemetry_schema_sha256=_sha(snapshot[base['schema_binding_path']]),mapping_sha256=v1._mapping_sha256(snapshot,base),
        source_byte_inventory_sha256=_hash(admission['byte_protocol']['domain_values']['source_inventory'],source),
        admission_contract_sha256=_sha(snapshot['evals/provider_completion_admission_link_v1/admission_link_contract_v1.json']))
    spec=recipe['profiles'][profile]
    manifest=dict(schema_version='provider-completion-execution-implementation-manifest/4.0',profile=profile,status='locked_source_integrity_only',
        recipe_sha256=RECIPE_SHA256,component_hashes=components,file_inventory=inventory,source_integrity_only=True,
        runtime_admission_verified=False,online_execution_authorized=False,historical_result_revalidated=False)
    manifest['commitment_sha256']=_hash(spec['manifest_domain'],manifest)
    manifest_bytes=raw(manifest)+b'\n'
    plan=dict(schema_version=spec['plan_schema_version'],plan_id=spec['plan_id'],profile=profile,status='locked_offline_not_run',
        recipe_sha256=RECIPE_SHA256,predecessor=lineage,component_hashes=components,implementation_commitment_sha256=manifest['commitment_sha256'],
        implementation_manifest_sha256=_sha(manifest_bytes),source_integrity_only=True,online_execution_authorized=False,
        runtime_admission_verified=False,historical_result_revalidated=False,network_calls=0,model_calls=0)
    plan['plan_commitment_sha256']=_hash(spec['plan_domain'],plan)
    return ProfileDocuments(manifest_bytes,raw(plan)+b'\n')


def verify_profile_documents(files,*,available_paths,profile,manifest,plan):
    if type(manifest) is not bytes or type(plan) is not bytes or max(len(manifest),len(plan))>2_000_000: _fail('artifact_bytes_invalid')
    expected=build_profile_documents(files,available_paths=available_paths,profile=profile)
    if manifest!=expected.manifest or plan!=expected.plan: _fail('component_drift')
    m,p=(decode_strict_json_object(value,max_bytes=2_000_000) for value in (manifest,plan))
    return VerifiedTimedProfileComponents(profile,m['commitment_sha256'],p['plan_commitment_sha256'],MappingProxyType(m['component_hashes']),len(m['file_inventory']))
