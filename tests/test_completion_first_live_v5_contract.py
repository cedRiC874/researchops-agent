"""V5 source-binding declaration only; no runtime or online behavior proof."""
import hashlib
import json
from pathlib import Path
import unittest

from researchops_completion_timing import first_live_identity as old
from researchops_external_closure.primitives import canonical_json_bytes as raw

ROOT=Path(__file__).resolve().parents[1]
PATH='evals/provider_completion_first_live_validation_v1/deepseek_responses_adapter_validation_implementation_v5.json'
SHA256='09fab825fef91286576ef486f2860b47a9ff01efeba9c5d93dff8f0c3145d315'


class FirstLiveV5ContractTests(unittest.TestCase):
    def test_only_identity_and_source_bindings_change(self):
        previous=json.loads((ROOT/old.IMPLEMENTATION_PATH).read_bytes())
        payload=(ROOT/PATH).read_bytes(); value=json.loads(payload)
        self.assertEqual((len(payload),hashlib.sha256(payload).hexdigest()),(4773,SHA256))
        self.assertEqual(value['requirements'],previous['requirements'])
        self.assertEqual(value['boundaries'],previous['boundaries'])
        self.assertEqual(value['execution_profile']['source_plan_id'],'phase6-deepseek-depth60-v11')
        for name in ('provider_id','model_id','api_origin','api_surface','transport_id','adapter_version'):
            self.assertEqual(value['execution_profile'][name],previous['execution_profile'][name])

    def test_every_fixed_dependency_matches_its_declared_bytes(self):
        value=json.loads((ROOT/PATH).read_bytes())
        for item in value['fixed_bindings']:
            payload=(ROOT/item['path']).read_bytes()
            self.assertEqual((len(payload),hashlib.sha256(payload).hexdigest()),(item['bytes'],item['sha256']))
        self.assertIn(old.IMPLEMENTATION_PATH,[item['path'] for item in value['fixed_bindings']])

    def test_new_domain_is_distinct_and_old_loader_does_not_accept_v5(self):
        payload=(ROOT/PATH).read_bytes(); value=json.loads(payload)
        self.assertEqual(value['commitment']['domain'],'researchops-provider-completion-first-live-implementation-v5')
        commitment=hashlib.sha256(value['commitment']['domain'].encode()+b'\0'+raw(value)).hexdigest()
        self.assertNotEqual(commitment,old.IMPLEMENTATION_COMMITMENT_SHA256)
        with self.assertRaisesRegex(ValueError,'implementation_contract_invalid'): old._document(payload)


if __name__=='__main__': unittest.main()
