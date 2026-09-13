"""Adversarial full-entrypoint tests on real synthetic Git/signature/SQLite graphs."""
import base64
import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

from researchops.audit import AuditLedger
from researchops_completion_timing import contract as timing
from researchops_external_closure import timed_evaluator as evaluator, timed_final as final, timed_contract as wire
from researchops_external_closure import semantics
from researchops_external_closure.artifact_inputs import _read_database_rows
from researchops_external_closure.primitives import canonical_json_bytes as raw
from researchops_external_closure.types import FinalDocumentBytes
from tests.test_timed_evaluator_full import TimedEvaluatorFullFixture
from tests import test_external_closure_evaluator as legacy_fixture
from tests import test_external_closure_semantics as content_fixture


def signed_final(fixture, body):
    profile, schemas = wire.load_contract(fixture.repository.root)
    receipt = copy.deepcopy(body)
    def sign(kind, value):
        role = 'task_custodian' if kind == 'receipt' else 'ledger_witness'
        key = fixture.signers.key_ids[role]
        digest = value['document_sha256' if kind == 'receipt' else 'entry_sha256']
        message = wire.signature_message(kind, key_id=key, signed_sha256=digest, contract=profile, schemas=schemas)
        value['signatures'] = [dict(role=role, key_id=key, algorithm='ed25519', signed_sha256=digest,
            signature_b64=base64.b64encode(fixture.signers.keys[role].sign(message)).decode())]
    receipt['document_sha256'] = wire.receipt_sha256(receipt, profile); sign('receipt', receipt)
    docs = fixture.signers.documents; consumed = docs['authorization_consumed_ledger_entry']
    entry = dict(schema_version='provider-completion-timed-closure-ledger-entry/2.0',
        document_type='completion_telemetry_timed_closure_ledger_entry', timed_closure_contract_sha256=wire.CONTRACT_SHA256,
        entry_type='closure_evidence_anchored', ledger_id=consumed['ledger_id'], sequence=consumed['sequence'] + 1,
        previous_head_sha256=consumed['resulting_head_sha256'], occurred_at_utc='2026-09-06T01:03:04Z',
        candidate_freeze_receipt_sha256=docs['candidate_freeze_receipt']['document_sha256'],
        preregistration_envelope_sha256=docs['preregistration_envelope']['document_sha256'],
        authorization_grant_sha256=docs['authorization_grant']['document_sha256'],
        consumption_receipt_sha256=docs['consumption_receipt']['document_sha256'], closure_receipt_sha256=receipt['document_sha256'])
    entry['entry_sha256'] = wire.entry_sha256(entry, profile)
    entry['resulting_head_sha256'] = wire.resulting_head_sha256(entry['previous_head_sha256'], entry['entry_sha256'], profile)
    sign('ledger', entry)
    observed = legacy_fixture._final_observation_bytes(fixture.full_arguments['postrun_observation'],
        receipt_sha256=receipt['document_sha256'], receipt_observed_at_utc='2026-09-06T01:03:03Z',
        entry=entry, entry_observed_at_utc='2026-09-06T01:03:05Z')
    return FinalDocumentBytes(fixture.full_documents, raw(receipt), raw(entry)), observed


def overlapping_artifacts(fixture):
    """New test-owned DB/files: no UPDATE, trigger disabling or old-file overwrite."""
    original = fixture.full_arguments['artifact_directory']
    destination = fixture.root / 'forged-overlap'; destination.mkdir()
    files = {path.name: path.read_bytes() for path in original.iterdir()}
    runs, events, models, tools, attempts, approvals = _read_database_rows(
        original / 'phase6_audit.sqlite3', expected_payload=files['phase6_audit.sqlite3'])
    if tools or attempts or approvals: raise AssertionError('unexpected baseline tools')
    index = json.loads(files['phase6_audit_index.json'])
    evidence = json.loads(files['completion_timing_evidence.json'])
    evidence['segments'][1]['attempt_started_ns'] = evidence['segments'][0]['lifetime_ended_ns'] - 1
    for segment in evidence['segments']:
        segment['segment_commitment_sha256'] = timing.commitment('segment', segment)
    by_attempt = {segment['attempt_index']: segment for segment in evidence['segments']}
    changed_events = [dict(row) for row in events]
    for row in changed_events:
        if row['event_type'] not in semantics._TERMINAL_EVENT_TYPES: continue
        payload = json.loads(row['safe_payload_json']); segment = by_attempt[payload['attempt_index']]
        payload['timing'] = dict(timing_contract_sha256=timing.CONTRACT_SHA256,
            **{name: segment[name] for name in ('plan_commitment_sha256', 'execution_binding_sha256', 'clock_domain_id', 'segment_commitment_sha256')})
        row['safe_payload_json'] = raw(payload).decode()
    runs_by_id = {row['run_id']: row for row in runs}
    ordered_runs = [runs_by_id[item['run_id']] for item in index['runs']]
    value = dict(run_rows=ordered_runs, event_rows=changed_events, audit_index=index)
    helper = content_fixture.ExternalClosureSemanticTests()
    for ordinal in range(len(ordered_runs)): helper._rehash_run(value, ordinal)
    heads = [dict(run_id=item['run_id'], final_chain_head_sha256=item['chain_verification']['chain_head']) for item in index['runs']]
    evidence['audit_chain_heads_sha256'] = semantics._domain_hash(semantics._AUDIT_RUN_SET_DOMAIN, b'final_chain_heads', raw(heads))
    evidence['evidence_commitment_sha256'] = timing.commitment('evidence', evidence)
    database = destination / 'phase6_audit.sqlite3'; AuditLedger(database)
    connection = sqlite3.connect(database)
    try:
        # Column names come only from the fixed SELECT lists in the test reader.
        for table, rows in (('runs', ordered_runs), ('audit_events', changed_events), ('model_calls', models)):
            columns = tuple(rows[0])
            connection.executemany(f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join(':'+name for name in columns)})", rows)
        connection.commit(); connection.execute('PRAGMA wal_checkpoint(TRUNCATE)'); connection.execute('PRAGMA journal_mode=DELETE')
    finally:
        connection.close()
    files.update({'phase6_audit.sqlite3': database.read_bytes(), 'phase6_audit_index.json': raw(index),
                  'completion_timing_evidence.json': raw(evidence)})
    manifest = json.loads(files['completion_closure_manifest.json'])
    manifest['files'] = {name: {'bytes': len(files[name]), 'sha256': timing.digest(files[name])} for name in manifest['files']}
    files['completion_closure_manifest.json'] = raw(manifest)
    publication = json.loads(files['completion_timing_publication.json'])
    publication.update(timing_evidence_file_sha256=timing.digest(files['completion_timing_evidence.json']),
        bundle_manifest_file_sha256=timing.digest(files['completion_closure_manifest.json']))
    publication['publication_commitment_sha256'] = timing.commitment('publication', publication)
    files['completion_timing_publication.json'] = raw(publication)
    for name, payload in files.items():
        if name != 'phase6_audit.sqlite3': (destination / name).write_bytes(payload)
    observation = json.loads(fixture.full_arguments['postrun_observation'])
    observation['manifest_observation']['manifest_sha256'] = timing.digest(files['completion_closure_manifest.json'])
    return fixture.full_arguments | dict(artifact_directory=destination, postrun_observation=raw(observation))


class TimedAdversarialFullTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = TimedEvaluatorFullFixture(); cls.addClassCleanup(cls.fixture.close)
        cls.baseline = evaluator.evaluate_timed_closure_bundle(cls.fixture.repository.root,
            cls.fixture.full_documents, **cls.fixture.full_arguments)
        if type(cls.baseline) is not evaluator.TimedReceiptProjectionReady or not cls.baseline.pre_anchor_closure_eligible:
            raise AssertionError(('baseline invalid', getattr(cls.baseline, 'error_code', None), getattr(cls.baseline, 'evidence_error_code', None)))

    def test_fully_rehashed_overlap_rejected_by_actual_A(self):
        arguments = overlapping_artifacts(self.fixture)
        with patch('socket.socket', side_effect=AssertionError('no Provider/network')):
            result = evaluator.evaluate_timed_closure_bundle(self.fixture.repository.root, self.fixture.full_documents, **arguments)
        self.assertIs(type(result), evaluator.TimedReceiptProjectionReady, getattr(result, 'error_code', None))
        self.assertFalse(result.evidence_valid)
        self.assertEqual(result.evidence_error_code, 'closure_timeline_invalid')
        body = json.loads(result.unsigned_receipt_canonical_json)
        self.assertEqual(body['timing']['error_code'], 'timing_concurrency_overlap')
        self.assertFalse(body['timing']['gate_complete'])
        self.assertFalse(result.pre_anchor_closure_eligible)

    def test_resigned_phase_duration_is_rejected_by_actual_B_A_rerun(self):
        body = json.loads(self.baseline.unsigned_receipt_canonical_json)
        body['timing']['phase_duration_ns'] += 1
        documents, observation = signed_final(self.fixture, body)
        actual = evaluator.evaluate_timed_closure_bundle; reruns = []
        def traced(*args, **kwargs):
            result = actual(*args, **kwargs); reruns.append(result); return result
        with patch.object(evaluator, 'evaluate_timed_closure_bundle', side_effect=traced), \
             patch('socket.socket', side_effect=AssertionError('no Provider/network')):
            result = final.verify_closed_timed_campaign(self.fixture.repository.root, documents,
                final_observation=observation, **self.fixture.full_arguments)
        self.assertEqual(len(reruns), 1)
        self.assertIs(type(reruns[0]), evaluator.TimedReceiptProjectionReady)
        self.assertTrue(reruns[0].pre_anchor_closure_eligible, reruns[0].evidence_error_code)
        self.assertEqual(reruns[0].unsigned_receipt_canonical_json, self.baseline.unsigned_receipt_canonical_json)
        self.assertEqual(result.status, 'unclosed_invalid')
        self.assertEqual(result.error_code, 'closure_receipt_projection_mismatch')
        self.assertFalse(result.closure_claim_allowed)


if __name__ == '__main__': unittest.main()
