"""In-memory source successor conformance; no worktree plans or model calls."""
import copy
import hashlib
import json
import unittest
from collections.abc import Mapping
from pathlib import Path

from researchops_external_closure import execution_components as v1, execution_components_v2 as v2
from researchops_external_closure import execution_components_v3 as v3, execution_components_v4 as v4

ROOT=Path(__file__).resolve().parents[1]
BASELINE=ROOT/'docs/diagnostics/t6c-pre-source-v4-20260908'


class ExecutionProfileV4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        manifest=json.loads((BASELINE/'manifest.json').read_bytes())
        cls.old={row['path']:(BASELINE/'files'/row['path']).read_bytes() for row in manifest['files']}
        cls.baseline_manifest=manifest
        cls.files=dict(cls.old)
        for path in (v4.RECIPE_PATH,'src/researchops_external_closure/execution_components_v4.py'):
            cls.files[path]=(ROOT/path).read_bytes()
        cls.recipes={module.RECIPE_PATH:cls.files[module.RECIPE_PATH] for module in (v1,v2,v3)}

    def select(self,files,profile='first_live'):
        return v4.select_profile_paths(tuple(sorted(files)),self.files[v4.RECIPE_PATH],predecessor_recipe_bytes=self.recipes,profile=profile)

    def build(self,files=None,profile='first_live'):
        files=self.files if files is None else files
        selected=self.select(files,profile)
        return v4.build_profile_documents({path:files[path] for path in selected},available_paths=tuple(sorted(files)),profile=profile)

    def test_archived_v3_inventory_bytes_and_old_limits_are_preserved(self):
        self.assertEqual(self.baseline_manifest['selected_file_count'],252)
        for row in self.baseline_manifest['files']:
            payload=self.old[row['path']]
            self.assertEqual((len(payload),hashlib.sha256(payload).hexdigest()),(row['bytes'],row['sha256']))
        selected=v3.select_profile_paths(tuple(sorted(self.old)),self.old[v3.RECIPE_PATH],self.old[v2.RECIPE_PATH],self.old[v1.RECIPE_PATH],profile='first_live')
        self.assertEqual(set(selected),set(self.old))
        self.assertEqual(v1._MAX_FILES,256)
        self.assertEqual(v3.decode_recipe(self.old[v3.RECIPE_PATH])['limits']['selected_files'],256)
        first=v3.build_profile_documents(self.old,available_paths=tuple(sorted(self.old)),profile='first_live')
        files=dict(self.old)
        manifest_path,plan_path=v3.PROFILE_PATHS['first_live']
        files.update({manifest_path:first.manifest,plan_path:first.plan,
            'evals/provider_completion_runtime_registry_v3/runtime_registry_v3.json':b'{}',
            'evals/provider_completion_runtime_registry_v3/registry_admission_bundle_v1.json':b'{}'})
        second=v3.build_profile_documents(files,available_paths=tuple(sorted(files)),profile='campaign')
        self.assertEqual(len(json.loads(second.manifest)['file_inventory']),256)

    def test_v11_source_only_documents_recompute_under_new_domains(self):
        result=self.build()
        checked=v4.verify_profile_documents(self.files,available_paths=tuple(sorted(self.files)),profile='first_live',manifest=result.manifest,plan=result.plan)
        plan=json.loads(result.plan); manifest=json.loads(result.manifest)
        self.assertEqual(plan['plan_id'],'phase6-deepseek-depth60-v11')
        self.assertEqual(manifest['schema_version'],'provider-completion-execution-implementation-manifest/4.0')
        self.assertTrue(checked.source_integrity_only); self.assertFalse(checked.online_execution_authorized)
        self.assertFalse(checked.runtime_admission_verified)
        self.assertEqual(plan['predecessor']['plan_commitment_sha256'],'c1c25226ef9186432d64b297def584da2d875afb0f923014975f9c9b481fb362')

    def test_320_files_work_without_hidden_legacy_256_cap_and_321_rejects(self):
        files=dict(self.files)
        for index in range(320-len(self.select(files))): files[f'src/future_{index:03d}.py']=b'# synthetic source\n'
        self.assertEqual(len(self.select(files)),320)
        self.assertEqual(len(json.loads(self.build(files).manifest)['file_inventory']),320)
        with self.assertRaisesRegex(ValueError,'coverage_invalid'):
            v1.select_execution_component_paths(tuple(sorted(files)),self.recipes[v1.RECIPE_PATH])
        files['src/one_too_many.py']=b'# synthetic\n'
        with self.assertRaisesRegex(ValueError,'execution_v4_coverage_invalid'): self.select(files)

    def test_oversized_mapping_rejects_before_key_or_value_iteration(self):
        class Oversized(Mapping):
            def __len__(self): return 321
            def __iter__(self): raise AssertionError('must not iterate')
            def __getitem__(self,key): raise AssertionError('must not fetch')
        with self.assertRaisesRegex(ValueError,'execution_v4_file_set_limit'):
            v4.build_profile_documents(Oversized(),available_paths=(),profile='first_live')

    def test_unknown_matching_contract_is_selected_but_unknown_profile_rejects(self):
        files=dict(self.files); files['evals/provider_completion_future/spec.json']=b'{}'
        self.assertIn('evals/provider_completion_future/spec.json',self.select(files))
        with self.assertRaisesRegex(ValueError,'profile_invalid'): self.select(files,'other')

    def test_missing_known_contract_is_not_silently_dropped(self):
        files=dict(self.files); files.pop('evals/provider_completion_campaign_run_v1/contract_v1.json')
        with self.assertRaisesRegex(ValueError,'required_file_missing'): self.select(files)

    def test_byte_bounds_are_unchanged(self):
        files=dict(self.files); files['src/large.py']=b'x'*(8*1024*1024+1)
        with self.assertRaisesRegex(ValueError,'file_bytes_invalid'): self.build(files)
        files=dict(self.files)
        for index in range(4): files[f'src/large_{index}.py']=b'x'*(8*1024*1024)
        with self.assertRaisesRegex(ValueError,'total_bytes_limit'): self.build(files)

    def test_timing_code_changes_move_both_conservative_component_hashes(self):
        first=json.loads(self.build().manifest)
        files=dict(self.files); files['src/researchops_completion_timing/clock.py']+=b'\n# synthetic difference\n'
        second=json.loads(self.build(files).manifest)
        for name in ('source_bundle_sha256','closure_verifier_sha256','sanitizer_sha256','completion_telemetry_runtime_bundle_sha256'):
            self.assertNotEqual(first['component_hashes'][name],second['component_hashes'][name])

    def test_historical_v7_bytes_cannot_be_relabelled(self):
        files=dict(self.files); path='evals/phase6_deepseek_depth60_plan_v7.json'
        value=json.loads(files[path]); value['network_calls']=1
        files[path]=json.dumps(value).encode()
        with self.assertRaisesRegex(ValueError,'v7_predecessor_drift'): self.build(files)

    def test_campaign_recomputes_first_live_source_and_rejects_changed_static_bytes(self):
        first=self.build(); files=dict(self.files)
        manifest_path,plan_path=v4.PROFILE_PATHS['first_live']
        files.update({manifest_path:first.manifest,plan_path:first.plan,
            'evals/provider_completion_runtime_registry_v3/runtime_registry_v3.json':b'{}',
            'evals/provider_completion_runtime_registry_v3/registry_admission_bundle_v1.json':b'{}'})
        second=self.build(files,'campaign')
        self.assertEqual(json.loads(second.plan)['plan_id'],'phase6-deepseek-depth60-v12')
        self.assertEqual(json.loads(second.manifest)['component_hashes']['source_byte_inventory_sha256'],
                         json.loads(first.manifest)['component_hashes']['source_byte_inventory_sha256'])
        files['src/researchops_completion_timing/clock.py']+=b'\n# synthetic drift\n'
        with self.assertRaisesRegex(ValueError,'first_live_snapshot_drift'): self.build(files,'campaign')


if __name__=='__main__': unittest.main()
