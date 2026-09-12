"""Real A-layer negatives with rehashed synthetic artifacts; no verdict stubs."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops_completion_timing import contract as timing
from researchops_external_closure import timed_evaluator as evaluator
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests.test_timed_evaluator_full import TimedEvaluatorFullFixture


class TimedErrorAttributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = TimedEvaluatorFullFixture()
        cls.addClassCleanup(cls.fixture.close)

    def exercise(self, mutation, expected_category):
        fixture = self.fixture
        original = fixture.full_arguments['artifact_directory']
        before = {path.name: path.read_bytes() for path in original.iterdir()}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            files = dict(before)
            name = 'completion_timing_evidence.json'
            evidence = json.loads(files[name])
            if mutation == 'heads':
                evidence['audit_chain_heads_sha256'] = 'f' * 64
                evidence['evidence_commitment_sha256'] = timing.commitment('evidence', evidence)
                files[name] = raw(evidence)
            else:
                # Still legal JSON with exactly the same value, but wrong bytes.
                files[name] += b'\n'
            manifest = json.loads(files['completion_closure_manifest.json'])
            manifest['files'][name] = dict(bytes=len(files[name]), sha256=timing.digest(files[name]))
            files['completion_closure_manifest.json'] = raw(manifest)
            publication = json.loads(files['completion_timing_publication.json'])
            publication['bundle_manifest_file_sha256'] = timing.digest(files['completion_closure_manifest.json'])
            publication['publication_commitment_sha256'] = timing.commitment('publication', publication)
            files['completion_timing_publication.json'] = raw(publication)
            for filename, payload in files.items():
                (directory / filename).write_bytes(payload)
            arguments = dict(fixture.full_arguments, artifact_directory=directory)
            observed = json.loads(arguments['postrun_observation'])
            observed['manifest_observation']['manifest_sha256'] = timing.digest(files['completion_closure_manifest.json'])
            arguments['postrun_observation'] = raw(observed)
            with patch('socket.socket', side_effect=AssertionError('no Provider or network')), \
                 patch.object(evaluator, '_verify_admission', wraps=evaluator._verify_admission) as admission:
                result = evaluator.evaluate_timed_closure_bundle(fixture.repository.root, fixture.full_documents, **arguments)
            self.assertEqual(admission.call_count, 1)
            self.assertIs(type(result), evaluator.TimedReceiptProjectionReady, getattr(result, 'error_code', None))
            body = json.loads(result.unsigned_receipt_canonical_json)
            self.assertEqual(body['timing']['status'], 'invalid')
            self.assertEqual(body['timing']['error_code'], expected_category)
            self.assertEqual(body['evidence_error_code'], 'closure_outer_projection_invalid')
            self.assertFalse(body['evidence_valid'])
            self.assertFalse(body['timing']['gate_complete'])
            self.assertFalse(body['pre_anchor_closure_eligible'])
            self.assertFalse(result.closure_claim_allowed)
        self.assertEqual(before, {path.name: path.read_bytes() for path in original.iterdir()})

    def test_rehashed_wrong_chain_heads_are_projection_not_hash_error(self):
        self.exercise('heads', 'timing_projection_mismatch')

    def test_correct_file_hashes_do_not_make_noncanonical_json_a_hash_error(self):
        self.exercise('noncanonical', 'timing_artifact_invalid')


if __name__ == '__main__':
    unittest.main(verbosity=2)
