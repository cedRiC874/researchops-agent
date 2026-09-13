"""V5 campaign ownership/entry boundaries; no Provider permission from tests."""
import inspect
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from researchops_completion_timing import campaign_start as start, campaign_tasks as tasks
from researchops_completion_timing import campaign_runtime as runtime, campaign_run as run
from researchops_completion_timing import campaign_publish as publication
from researchops_external_closure import execution_components_v4, execution_local_v4, execution_current_v4


class CampaignV5BoundaryTests(unittest.TestCase):
    def test_source_routing_selects_fixed_v5_functions(self):
        paths, verifier, local, current = start._source_functions(5)
        self.assertIs(paths, execution_components_v4.PROFILE_PATHS)
        self.assertIs(verifier, start.verify_campaign_prerequisites_v5)
        self.assertIs(local, execution_local_v4.verify_local_timed_execution_identity)
        self.assertIs(current, execution_current_v4.verify_current_timed_profile)
        self.assertEqual(start._source_functions(4)[0], start.PROFILE_PATHS)

    def test_invalid_version_reads_no_clock_or_store(self):
        with patch.object(start, '_CampaignValidity', side_effect=AssertionError('no clock')) as clock:
            for version in (True, 4.0, '5', None, 6):
                with self.subTest(version=version), self.assertRaisesRegex(start.CampaignStartError, 'version_invalid') as caught:
                    start._prepare_process_bound_campaign_start_impl(None, None, external_observation_bundle=None,
                        timing_plan_bytes=None, admission_inputs=None, implementation_version=version)
                self.assertFalse(caught.exception.claim_consumed)
            self.assertEqual(clock.call_count, 0)

    def test_task_owner_versions_never_take_each_others_preparation(self):
        for owner, prepared_type in ((tasks._CampaignTaskOwnerV5, start._ClaimedCampaignStart),
                                     (tasks._CampaignTaskOwner, start._ClaimedCampaignStartV5)):
            with self.subTest(owner=owner.__name__), patch.object(prepared_type, '_take_for_runtime', side_effect=AssertionError('no take')) as take:
                with self.assertRaisesRegex(tasks.CampaignTaskReleaseError, 'preparation_required'):
                    owner(object.__new__(prepared_type))
                self.assertEqual(take.call_count, 0)

    def test_unknown_factories_cannot_enter_model_runtime(self):
        class Unknown(runtime._CampaignModelFactoryV5): pass
        for factory in (runtime._CampaignModelFactoryV5, Unknown):
            with self.subTest(factory=factory.__name__), self.assertRaises(runtime.CampaignRuntimeError):
                factory(object(), object())

    def test_plan_change_invalidates_v5_owner_even_after_restoring_bytes(self):
        clock = SimpleNamespace(abort_new_work=Mock())
        prepared = start._ClaimedCampaignStartV5(start._TOKEN, clock=clock, validity=None, proof=None, identity=None,
            environment=None, domain='PCECLOCK-' + 'A' * 32, request=b'{}', receipt=b'{}', plan_bytes=b'original',
            envelope_bytes=b'{}', sealing=1)
        prepared.plan_bytes = b'changed'
        with self.assertRaisesRegex(start.CampaignStartError, 'plan_changed'): prepared._assert_process()
        self.assertTrue(prepared._taken)
        prepared.abort(); prepared.plan_bytes = b'original'
        with self.assertRaisesRegex(start.CampaignStartError, 'already_taken'): prepared._take_for_runtime()

    def test_publication_identity_must_still_match_original_envelope(self):
        factory = object.__new__(runtime._CampaignModelFactoryV5)
        identity = SimpleNamespace(source_recipe_version=4, profile='campaign', execution_commit='a'*40,
            execution_tree='b'*40, source_integrity_commitment_sha256='c'*64, source_manifest_commitment_sha256='d'*64)
        envelope = {'execution_binding': dict(execution_commit='f'*40, execution_tree='b'*40,
            source_integrity_commitment_sha256='c'*64, implementation_commitment_sha256='d'*64)}
        factory._prepared = SimpleNamespace(_assert_process=Mock(), _verified_envelope=lambda: envelope, source_identity=identity)
        with patch.object(publication, '_publication_checkpoint'), \
             patch.object(execution_current_v4, 'verify_current_timed_profile', side_effect=AssertionError('no source read')) as source:
            with self.assertRaisesRegex(publication.CampaignPublishError, 'source_changed'):
                publication._check_publication_identity(factory)
            self.assertEqual(source.call_count, 0)


class CampaignV5InvocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_without_confirmation_reads_no_inputs(self):
        names = ('documents', 'external_observation_bundle', 'timing_plan_bytes', 'admission_inputs', 'task_package_path')
        with patch.object(run, 'load_invocation_contract', side_effect=AssertionError('no reads')) as read:
            for confirmation in (False, None, 1, 'true'):
                result = json.loads(await run.run_timed_campaign_v5(**{name: object() for name in names}, confirm_online=confirmation))
                self.assertEqual(result['status'], 'not_confirmed')
            self.assertEqual(read.call_count, 0)

    async def test_public_v5_signature_has_no_callback_or_version_override(self):
        self.assertEqual(tuple(inspect.signature(run.run_timed_campaign_v5).parameters),
            tuple(inspect.signature(run.run_timed_campaign).parameters))


if __name__ == '__main__': unittest.main()
