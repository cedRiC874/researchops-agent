"""Independent frozen-formula fixtures; no actual unseen tasks or release permit."""
import base64
import copy
import hashlib
import json
import pickle
import unittest
from pathlib import Path

from researchops_completion_timing.campaign_opening import _verify_private_task_opening
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests.test_external_closure_documents import SyntheticPreReceipt

ROOT = Path(__file__).resolve().parents[1]


class CampaignOpeningTests(unittest.TestCase):
    def setUp(self):
        self.envelope = copy.deepcopy(SyntheticPreReceipt().documents['preregistration_envelope'])
        self.salt = bytes(range(32, 64))
        self.seen = (hashlib.sha256(b'other synthetic task').hexdigest(),)
        handles = self.envelope['runtime_plan']['denominator_plan']['case_ids']
        self.bundle = dict(schema_version='completion-telemetry-unseen-case-bundle/1.0', bundle_id='PCEBUNDLE-' + 'A' * 32,
            synthetic_only=True, candidate_freeze_receipt_sha256=self.envelope['execution_binding']['candidate_freeze_receipt_sha256'],
            seen_case_exclusion_manifest_sha256=self.envelope['task_custody']['seen_task_exclusion_manifest_sha256'],
            leak_canary_b64=base64.b64encode(bytes(range(32))).decode(), leak_canary_injected_into_every_request=True,
            cases=[dict(case_handle=handle, task_schema_version='completion-telemetry-synthetic-task/1.0',
                user_input='synthetic opening fixture ' + str(index), max_turns_per_case=2, tools=[], golden=None, scorer=None)
                for index, handle in enumerate(handles)], participant_derived_data_present=False,
            private_or_non_synthetic_data_present=False, direct_or_quasi_identifier_data_present=False)
        self.reseal()

    def hash(self, domain, value, salted=False):
        prefix = domain.encode() + b'\0'
        if salted: prefix += len(self.salt).to_bytes(4, 'big') + self.salt
        return hashlib.sha256(prefix + raw(value)).hexdigest()

    def reseal(self):
        custody = self.envelope['task_custody']
        custody['task_bundle_commitment_sha256'] = self.hash('researchops-provider-completion-external-unseen-case-bundle-v1', self.bundle, True)
        custody['seen_task_exclusion_set_count'] = len(set(self.seen))
        custody['seen_task_exclusion_set_commitment_sha256'] = self.hash('researchops-provider-completion-seen-case-exclusion-set-v1', sorted(set(self.seen)))
        self.expected = []
        for case in self.bundle['cases']:
            value = {name: case[name] for name in ('case_handle', 'task_schema_version', 'user_input', 'max_turns_per_case', 'tools')}
            value.update(leak_canary_b64=self.bundle['leak_canary_b64'], runner_config_commitment_sha256=self.envelope['execution_binding']['runner_config_commitment_sha256'])
            self.expected.append(self.hash('researchops-provider-completion-execution-input-v1', value, True))
        custody['execution_input_order_commitment_sha256'] = self.hash('researchops-provider-completion-execution-input-order-v1', self.expected)

    def verify(self, **changes):
        arguments = dict(bundle_bytes=raw(self.bundle), secret_salt=self.salt, seen_task_digests=self.seen, envelope_bytes=raw(self.envelope))
        arguments.update(changes)
        return _verify_private_task_opening(ROOT, **arguments)

    def test_all_frozen_salted_formulas_match_independent_construction(self):
        opened = self.verify()
        self.assertEqual(opened.execution_input_commitments, tuple(self.expected))
        summary = opened.summary()
        self.assertEqual(summary['bundle_commitment_sha256'], self.envelope['task_custody']['task_bundle_commitment_sha256'])
        self.assertEqual(summary['exact_overlap_count'], 0)
        for name in ('authorization_verified', 'task_release_order_verified', 'seen_inventory_completeness_verified', 'semantic_overlap_excluded', 'runtime_authority_granted'):
            self.assertFalse(summary[name])

    def test_whitespace_does_not_change_canonical_bundle_commitment(self):
        self.assertEqual(self.verify().summary(), self.verify(bundle_bytes=json.dumps(self.bundle, indent=2).encode()).summary())

    def test_wrong_or_short_salt_rejects(self):
        for salt in (b'short', bytes(range(33, 65))):
            with self.subTest(length=len(salt)), self.assertRaises(ValueError): self.verify(secret_salt=salt)

    def test_task_mutation_cannot_reuse_old_commitment(self):
        self.bundle['cases'][0]['user_input'] += ' changed'
        with self.assertRaisesRegex(ValueError, 'bundle_commitment_mismatch'): self.verify()

    def test_float_turn_limit_is_not_accepted_as_an_integer(self):
        self.bundle['cases'][0]['max_turns_per_case'] = 2.0
        self.reseal()
        with self.assertRaises(ValueError): self.verify()

    def test_trailing_newline_case_handle_is_rejected(self):
        handle = self.bundle['cases'][0]['case_handle'] + '\n'
        self.bundle['cases'][0]['case_handle'] = handle
        self.envelope['runtime_plan']['denominator_plan']['case_ids'][0] = handle
        self.reseal()
        with self.assertRaises(ValueError): self.verify()

    def test_recommitted_wrong_case_order_or_turns_still_rejects(self):
        original = copy.deepcopy(self.bundle)
        for change in ('order', 'turns'):
            self.bundle = copy.deepcopy(original)
            if change == 'order': self.bundle['cases'].reverse()
            else: self.bundle['cases'][0]['max_turns_per_case'] = 1
            self.reseal()
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'case_order_invalid'): self.verify()

    def test_provided_seen_set_overlap_is_rejected(self):
        case = self.bundle['cases'][0]
        value = {name: case[name] for name in ('task_schema_version', 'user_input', 'tools')}
        self.seen = (self.hash('researchops-provider-completion-canonical-case-digest-v1', value),)
        self.reseal()
        with self.assertRaisesRegex(ValueError, 'seen_overlap'): self.verify()

    def test_changed_or_duplicate_seen_set_cannot_pass(self):
        for seen in ((hashlib.sha256(b'different').hexdigest(),), self.seen * 2):
            with self.subTest(count=len(seen)), self.assertRaises(ValueError): self.verify(seen_task_digests=seen)

    def test_execution_order_commitment_is_checked_separately(self):
        self.envelope['task_custody']['execution_input_order_commitment_sha256'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'execution_order_mismatch'): self.verify()

    def test_golden_non_synthetic_and_extra_fields_reject(self):
        for key, value in (('synthetic_only', False), ('private_or_non_synthetic_data_present', True), ('extra', 'rejected')):
            original = copy.deepcopy(self.bundle); self.bundle[key] = value; self.reseal()
            with self.subTest(key=key), self.assertRaises(ValueError): self.verify()
            self.bundle = original
        self.bundle['cases'][0]['golden'] = 'not allowed'; self.reseal()
        with self.assertRaises(ValueError): self.verify()

    def test_secret_like_input_is_rejected_without_echo(self):
        marker = 'sk-FAKE_OPENING_SECRET_123456'
        self.bundle['cases'][0]['user_input'] = marker; self.reseal()
        with self.assertRaises(ValueError) as caught: self.verify()
        self.assertNotIn(marker, str(caught.exception))

    def test_repr_copy_pickle_and_mutation_do_not_export_opening(self):
        opened = self.verify()
        self.assertNotIn(self.bundle['cases'][0]['user_input'], repr(opened))
        self.assertNotIn(self.bundle['leak_canary_b64'], repr(opened))
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with self.assertRaises(TypeError): operation(opened)
        with self.assertRaises(TypeError): opened._cases[0]['user_input'] = 'changed'
        self.assertEqual(opened._cases[0]['tools'], ())


if __name__ == '__main__': unittest.main()
