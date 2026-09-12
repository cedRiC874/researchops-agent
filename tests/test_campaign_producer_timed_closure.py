"""Public v5 producer -> untouched seven files -> actual timed A and B.

All grants, signatures, tasks and observations are synthetic. Provider IO uses
the existing isolated MockTransport child. No production admission/verifier is
stubbed; external postrun attestations are test authoring, not live evidence.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import traceback
import unittest
from unittest.mock import patch

from researchops_external_closure import timed_evaluator as evaluator, timed_final as final, timed_contract as wire
from researchops_external_closure.primitives import canonical_json_bytes as raw
from researchops_external_closure.types import FinalDocumentBytes
from tests import test_completion_campaign_public_process as producer_fixture
from tests.test_external_closure_documents import SyntheticPreReceipt
from tests import test_external_closure_evaluator as legacy


@unittest.skipUnless(os.name == 'nt', 'designated Windows synthetic environment')
class CampaignProducerClosureTests(unittest.TestCase):
    def test_unmodified_public_producer_archive_passes_actual_A_and_B(self):
        producer = producer_fixture.PublicCampaignProcessTests(); self.addCleanup(producer.doCleanups)
        summary = producer.run_process('api', implementation_version=5)
        fixture = producer.produced_fixture
        root = fixture.repository.root
        directory = root / summary['archive_relative_path']
        original = {path.name: path.read_bytes() for path in directory.iterdir()}
        self.assertEqual(len(original), 7)
        manifest_sha = hashlib.sha256(original['completion_closure_manifest.json']).hexdigest()
        self.assertEqual(manifest_sha, summary['manifest_file_sha256'])
        self.assertEqual(original['completion_timing_plan.json'], fixture.campaign_timing_plan)
        self.assertFalse(summary['closure_claim_allowed'])

        # Only the independent synthetic observation/attestation is authored
        # here. The DB, index, denominator, timing and manifest remain original.
        after = copy.deepcopy(SyntheticPreReceipt().observation)
        after.update(json.loads(fixture.campaign_observation))
        after.update(schema_version='provider-completion-timed-closure-observation/2.0', mode='pre_receipt')
        after['manifest_observation'].update(state='present', manifest_sha256=manifest_sha,
            observed_at_utc='2026-09-06T01:03:01Z', last_completed_artifact_write_stage='manifest_written')
        facts = legacy._postrun(fixture.signers)
        facts.update(schema_version='provider-completion-postrun-attested-facts/2.0',
            timed_last_completed_write_stage='timing_publication_written',
            task_released_at_utc=fixture.campaign_as_of, provider_key_loaded_at_utc=fixture.campaign_as_of,
            completed_at_utc='2026-09-06T01:03:00Z', receipt_issued_at_utc='2026-09-06T01:03:02Z',
            network_attempt_count_observed=summary['native_dispatch_count'],
            model_request_count_observed=summary['native_dispatch_count'])
        docs = fixture.signers.documents
        profile, schemas = wire.load_contract(root)
        facts['receipt_id'] = wire.receipt_id(dict(campaign_id=facts['campaign_id'],
            authorization_grant_sha256=docs['authorization_grant']['document_sha256'],
            authorization_consumption_entry_sha256=docs['authorization_consumed_ledger_entry']['entry_sha256']), profile)
        arguments = dict(pre_execution_observation=fixture.campaign_observation,
            postrun_observation=raw(after), timing_plan_bytes=fixture.campaign_timing_plan,
            artifact_directory=directory, postrun_attested_facts=raw(facts),
            admission_inputs=fixture.campaign_admission_inputs)
        actual_admission = evaluator._verify_admission
        admission_errors = []
        def trace_admission(*args, **kwargs):
            try:
                return actual_admission(*args, **kwargs)
            except Exception as error:
                # Test-only diagnostics: no exception body, locals or path.
                admission_errors.append(dict(kind=type(error).__name__, code=vars(error).get('code'),
                    frames=[(Path(frame.filename).name, frame.name, frame.lineno)
                            for frame in traceback.extract_tb(error.__traceback__)]))
                raise
        with patch.object(evaluator, '_verify_admission', side_effect=trace_admission), \
             patch('socket.socket', side_effect=AssertionError('A has no network')):
            projected = evaluator.evaluate_timed_closure_bundle(root, fixture.campaign_documents, **arguments)
        self.assertIs(type(projected), evaluator.TimedReceiptProjectionReady, getattr(projected, 'error_code', None))
        self.assertTrue(projected.pre_anchor_closure_eligible,
            (projected.evidence_error_code, admission_errors, projected.unsigned_receipt_canonical_json.decode()))
        self.assertFalse(projected.closure_claim_allowed)

        def sign(kind, value):
            role = 'task_custodian' if kind == 'receipt' else 'ledger_witness'
            key = fixture.signers.key_ids[role]
            digest = value['document_sha256' if kind == 'receipt' else 'entry_sha256']
            message = wire.signature_message(kind, key_id=key, signed_sha256=digest, contract=profile, schemas=schemas)
            value['signatures'] = [dict(role=role, key_id=key, algorithm='ed25519', signed_sha256=digest,
                signature_b64=base64.b64encode(fixture.signers.keys[role].sign(message)).decode())]
        receipt = json.loads(projected.unsigned_receipt_canonical_json)
        receipt['document_sha256'] = projected.receipt_document_sha256; sign('receipt', receipt)
        consumed = docs['authorization_consumed_ledger_entry']
        entry = dict(schema_version='provider-completion-timed-closure-ledger-entry/2.0',
            document_type='completion_telemetry_timed_closure_ledger_entry', timed_closure_contract_sha256=wire.CONTRACT_SHA256,
            entry_type='closure_evidence_anchored', ledger_id=consumed['ledger_id'], sequence=consumed['sequence'] + 1,
            previous_head_sha256=consumed['resulting_head_sha256'], occurred_at_utc='2026-09-06T01:03:04Z',
            candidate_freeze_receipt_sha256=docs['candidate_freeze_receipt']['document_sha256'],
            preregistration_envelope_sha256=docs['preregistration_envelope']['document_sha256'],
            authorization_grant_sha256=docs['authorization_grant']['document_sha256'],
            consumption_receipt_sha256=docs['consumption_receipt']['document_sha256'],
            closure_receipt_sha256=receipt['document_sha256'])
        entry['entry_sha256'] = wire.entry_sha256(entry, profile)
        entry['resulting_head_sha256'] = wire.resulting_head_sha256(entry['previous_head_sha256'], entry['entry_sha256'], profile)
        sign('ledger', entry)
        observation = legacy._final_observation_bytes(raw(after), receipt_sha256=receipt['document_sha256'],
            receipt_observed_at_utc='2026-09-06T01:03:03Z', entry=entry, entry_observed_at_utc='2026-09-06T01:03:05Z')
        actual = evaluator.evaluate_timed_closure_bundle; reruns = []
        def traced(*args, **kwargs):
            result = actual(*args, **kwargs); reruns.append(result); return result
        with patch.object(evaluator, 'evaluate_timed_closure_bundle', side_effect=traced), \
             patch('socket.socket', side_effect=AssertionError('B has no network')):
            closed = final.verify_closed_timed_campaign(root,
                FinalDocumentBytes(fixture.campaign_documents, raw(receipt), raw(entry)),
                final_observation=observation, **arguments)
        self.assertEqual(len(reruns), 1)
        self.assertEqual(reruns[0].unsigned_receipt_canonical_json, projected.unsigned_receipt_canonical_json)
        self.assertEqual(closed.status, 'closed_evidence_verified', closed.error_code)
        self.assertTrue(closed.evidence_valid); self.assertTrue(closed.closure_claim_allowed)
        self.assertFalse(closed.runtime_authority_granted)
        self.assertEqual(closed.verification_provider_calls, 0)
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, original)


if __name__ == '__main__': unittest.main()
