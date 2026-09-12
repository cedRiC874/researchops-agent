"""Real synthetic signatures across observation versions; no artifacts/Provider."""
import copy
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from researchops_external_closure import timed_history as history, timed_contract as wire
from researchops_external_closure.primitives import canonical_json_bytes as raw
from researchops_completion_timing import local_claim
from tests import test_completion_execution_scope as scope_fixture
from tests import test_completion_timing_pre_execution as pre_fixture
from tests import test_external_closure_evaluator as facts_fixture
from tests.test_external_closure_documents import SyntheticPreReceipt


class TimedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture, self.plan = scope_fixture.scoped_fixture()
        self.after = copy.deepcopy(SyntheticPreReceipt().observation)
        self.after.update(copy.deepcopy(self.fixture.observation))
        self.after.update(schema_version='provider-completion-timed-closure-observation/2.0', mode='pre_receipt')
        self.facts = facts_fixture._postrun(self.fixture)
        self.facts.update(schema_version='provider-completion-postrun-attested-facts/2.0',
                          timed_last_completed_write_stage='timing_publication_written')
        profile, _ = wire.load_contract()
        self.facts['receipt_id'] = wire.receipt_id(dict(campaign_id=self.facts['campaign_id'],
            authorization_grant_sha256=self.fixture.documents['authorization_grant']['document_sha256'],
            authorization_consumption_entry_sha256=self.fixture.documents['authorization_consumed_ledger_entry']['entry_sha256']), profile)

    def check(self, **changes):
        arguments = dict(pre_execution_observation=raw(self.fixture.observation), postrun_observation=raw(self.after),
            timing_plan_bytes=raw(self.plan), postrun_attested_facts=raw(self.facts))
        arguments.update(changes)
        return history.verify_timed_pre_receipt_inputs(wire.ROOT, pre_fixture.documents(self.fixture), **arguments)

    def test_v3_graph_v2_observation_and_facts_link_without_clock_store_or_artifacts(self):
        before = raw(self.fixture.documents), raw(self.fixture.observation), raw(self.after), raw(self.facts)
        with patch('socket.socket', side_effect=AssertionError('no network')), \
             patch('subprocess.Popen', side_effect=AssertionError('no process')), \
             patch('time.monotonic_ns', side_effect=AssertionError('no clock')), \
             patch.object(local_claim, 'reserve_local_claim', side_effect=AssertionError('no claim')), \
             patch.object(local_claim, 'local_claim_store_status', side_effect=AssertionError('no store')):
            checked = self.check()
        self.assertEqual(checked.history.scope.timing.verified_as_of_utc,
                         self.fixture.observation['authorization_consumed_anchor']['observed_at_utc'])
        self.assertEqual(checked.history.authorization_grant_sha256, self.fixture.documents['authorization_grant']['document_sha256'])
        self.assertEqual(checked.facts.timed_last_completed_write_stage, 'timing_publication_written')
        self.assertFalse(checked.closure_claim_allowed)
        for name in ('final_witness_verified', 'oob_origin_independently_proved', 'artifact_evidence_verified', 'runtime_authority_granted'):
            self.assertFalse(getattr(checked.history, name))
        self.assertEqual(before, (raw(self.fixture.documents), raw(self.fixture.observation), raw(self.after), raw(self.facts)))
        with self.assertRaises(FrozenInstanceError): checked.history.observation_mode = 'final'
        with self.assertRaises(TypeError): checked.facts._values['receipt_id'] = 'changed'

    def test_changed_independent_history_is_not_adopted_from_postrun(self):
        self.after['authorization_consumed_anchor']['observation_source_commitment_sha256'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'observation_history_mismatch'): self.check()

    def test_unsigned_envelope_change_cannot_pass(self):
        self.fixture.documents['preregistration_envelope']['valid_until_utc'] = '2026-09-04T02:00:00Z'
        with self.assertRaises(ValueError): self.check()

    def test_signed_unscoped_v2_is_not_upgraded_to_v3(self):
        fixture, plan = pre_fixture.signed_fixture()
        after = copy.deepcopy(self.after); after.update(copy.deepcopy(fixture.observation))
        after.update(schema_version='provider-completion-timed-closure-observation/2.0', mode='pre_receipt')
        with self.assertRaises(ValueError):
            history.verify_timed_history(wire.ROOT, pre_fixture.documents(fixture),
                pre_execution_observation=raw(fixture.observation), postrun_observation=raw(after), timing_plan_bytes=raw(plan))

    def test_valid_looking_wrong_receipt_id_is_rejected(self):
        self.facts['receipt_id'] = 'PCECLOSE-' + 'F' * 32
        with self.assertRaisesRegex(ValueError, 'receipt_id_mismatch'): self.check()

    def test_task_release_cannot_precede_the_observed_consumption_anchor(self):
        self.facts['task_released_at_utc'] = self.fixture.observation['authorization_consumed_anchor']['observed_at_utc']
        with self.assertRaisesRegex(ValueError, 'postrun_facts_timeline_invalid'): self.check()

    def test_manifest_completed_stage_must_match_facts(self):
        self.facts.update(timed_last_completed_write_stage='timing_plan_written',
            last_completed_artifact_write_stage='completion_telemetry_written', artifact_error_code='pce_artifact_timing_evidence_failed')
        self.after['manifest_observation'].update(state='absent', manifest_sha256=None,
            last_completed_artifact_write_stage='audit_index_written')
        with self.assertRaisesRegex(ValueError, 'manifest_stage_mismatch'): self.check()

    def test_final_observation_shape_does_not_verify_a_final_witness(self):
        self.after['mode'] = 'final'
        self.after['closure_receipt_observation'] = dict(self.after['trust_manifest_observation'],
            document_sha256='e' * 64, observed_at_utc='2026-09-04T00:00:22Z')
        consumed = self.fixture.documents['authorization_consumed_ledger_entry']
        self.after['closure_evidence_anchor'] = dict(self.after['authorization_consumed_anchor'],
            entry_type='closure_evidence_anchored', sequence=consumed['sequence'] + 1,
            previous_head_sha256=consumed['resulting_head_sha256'], entry_sha256='d' * 64,
            resulting_head_sha256='c' * 64, observed_at_utc='2026-09-04T00:00:23Z')
        checked = self.check(observation_mode='final')
        self.assertFalse(checked.history.final_witness_verified)
        self.assertFalse(checked.closure_claim_allowed)
        with self.assertRaisesRegex(ValueError, 'observation_mode_invalid'): self.check()


if __name__ == '__main__': unittest.main()
