"""Production-path integration in an isolated local Git fixture, not a live run."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from researchops_internal_telemetry import contract as c, source
from researchops_internal_telemetry import admission, task_origin


@unittest.skipUnless(os.name=="nt","the approved fixed-store runtime is Windows-only")
class InternalIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix="researchops-internal-fixture-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.base=Path(cls.temp.name).resolve(strict=True);cls.root=cls.base/"repo";cls.root.mkdir()
        for name in source.source_files():
            target=cls.root/name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(c.ROOT/name,target)
        # Refresh only this test-owned clone. Its compared eval documents may
        # include newer offline packages; the historical repository files stay fixed.
        cls.copied_origin_bytes=(cls.root/task_origin.ORIGIN_PATH).read_bytes()
        (cls.root/task_origin.ORIGIN_PATH).write_bytes(c.raw(task_origin.build_origin_record(cls.root)))
        (cls.root/source.MANIFEST).write_bytes(c.raw(source.build_manifest(cls.root)))
        cls.environment={name:os.environ[name] for name in ("PATH","SystemRoot","WINDIR","COMSPEC","TEMP","TMP") if name in os.environ}
        cls.environment.update(PYTHONPATH=str(cls.root/"src"),PYTHONDONTWRITEBYTECODE="1",PYTHONUTF8="1",
            OPENAI_AGENTS_DISABLE_TRACING="1",GIT_CONFIG_NOSYSTEM="1",GIT_CONFIG_GLOBAL=os.devnull)
        # Synthetic, throwaway identity only. No project commit or remote exists.
        for args in (("init",),("config","core.autocrlf","false"),("config","user.name","Synthetic Test"),
                     ("config","user.email","fixture@example.invalid"),("add","."),("commit","-m","isolated offline fixture")):
            result=subprocess.run(["git","-C",str(cls.root),*args],env=cls.environment,capture_output=True,timeout=60)
            if result.returncode:raise RuntimeError("synthetic Git fixture failed: "+result.stderr.decode(errors="replace"))
        cls.known=cls.base/"known";cls.known.mkdir()

    def fixture_freeze(self):
        pricing=dict(schema_version="provider-completion-internal-pricing/1.0",status="user_reviewed_official_snapshot",
            provider_id="deepseek",requested_model="deepseek-v4-flash",input_price_per_million_cny="2.000000",
            output_price_per_million_cny="8.000000",cache_discount_assumed=False,provider_invoice_hard_cap=False,
            evidence_date_utc=c.now().isoformat().replace("+00:00","Z"),official_evidence_sha256="1"*64)
        return admission.build_freeze(root=self.root,
            execution_commit=source.git(self.root,"rev-parse","HEAD").decode().strip(),pricing=pricing,
            review_record=dict(kind="internal_review",preparer="synthetic_test",reviewer="synthetic_test",
                same_person=True,developer_known=True,
                old_task_exclusion_record_sha256=c.digest((self.root/task_origin.ORIGIN_PATH).read_bytes())))

    def test_fixture_origin_and_manifest_match_without_changing_history(self):
        current=(self.root/task_origin.ORIGIN_PATH).read_bytes()
        self.assertEqual(c.decode(current),task_origin.build_origin_record(self.root))
        self.assertNotEqual(current,self.copied_origin_bytes)
        self.assertEqual((c.ROOT/task_origin.ORIGIN_PATH).read_bytes(),self.copied_origin_bytes)
        manifest=source.verify_source(self.root)
        row=next(item for item in manifest['files'] if item['path']==task_origin.ORIGIN_PATH)
        self.assertEqual(row['sha256'],c.digest(current))

    def test_fixture_freeze_checks_real_source_without_store_or_runtime(self):
        from researchops_completion_timing import local_claim
        with patch.object(source,'verify_source',wraps=source.verify_source) as verify, \
                patch.object(local_claim,'_windows_local_app_data',side_effect=AssertionError('no real store lookup')) as store:
            freeze=self.fixture_freeze()
        verify.assert_called_once_with(self.root)
        store.assert_not_called()
        self.assertFalse(freeze['external_validation_completed'])
        self.assertFalse(freeze['status_closure_allowed'])
        self.assertEqual(freeze['review_record']['old_task_exclusion_record_sha256'],
                         c.digest((self.root/task_origin.ORIGIN_PATH).read_bytes()))

    def test_stale_fixture_origin_is_rejected_before_source_verification(self):
        path=self.root/task_origin.ORIGIN_PATH
        current=path.read_bytes()
        try:
            path.write_bytes(self.copied_origin_bytes)
            with patch.object(source,'verify_source',wraps=source.verify_source) as verify:
                with self.assertRaisesRegex(c.InternalError,'^internal_task_origin_mismatch$'):
                    self.fixture_freeze()
            verify.assert_not_called()
        finally:
            path.write_bytes(current)
        self.assertEqual((c.ROOT/task_origin.ORIGIN_PATH).read_bytes(),self.copied_origin_bytes)

    def probe(self,mode):
        result=subprocess.run([sys.executable,"-B",str(c.ROOT/"tests/internal_telemetry_probe.py"),mode,str(self.known)],
            cwd=self.root,env=self.environment,capture_output=True,text=True,encoding="utf-8",timeout=240)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        return json.loads(result.stdout)

    def inspect_archive(self,mode,observed,exit_code=0):
        result=subprocess.run([sys.executable,"-B",str(c.ROOT/"tests/internal_telemetry_probe.py"),mode,observed["archive"],
            observed["result"]["bundle_commitment_sha256"],observed["approved_digest"],str(exit_code)],
            cwd=self.root,env=self.environment,capture_output=True,text=True,encoding="utf-8",timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        return json.loads(result.stdout)

    def test_full_entrypoint_30_actual_sdk_requests_and_archive(self):
        observed=self.probe("completed")
        self.assertEqual(len(observed["calls"]),30,observed)
        self.assertEqual(observed["result"]["exit_code"],0,observed)
        self.assertEqual(observed["result"]["artifact_status"],"sealed")
        self.assertFalse(observed["result"]["internal_acceptance_passed"])
        self.assertTrue(all(x==dict(method="POST",url="https://api.deepseek.com/responses",cap=512) for x in observed["calls"]))
        self.assertEqual((Path(observed["archive"])/"cases.json").read_bytes(),(self.root/c.DIRECTORY/"cases_v1.json").read_bytes())
        checked=self.inspect_archive("verify",observed)
        self.assertTrue(checked["offline_engineering_acceptance_passed"])
        self.assertFalse(checked["internal_acceptance_passed"])
        self.assertFalse(checked["status_closure_allowed"])
        self.assertFalse(self.inspect_archive("verify",observed,4)["internal_acceptance_passed"])
        tampered=self.inspect_archive("tamper",observed)
        self.assertTrue(tampered["mutation_applied"])
        self.assertEqual(tampered["code"],"internal_artifact_hash")
        semantic=self.inspect_archive("tamper_semantic",observed)
        self.assertTrue(semantic["mutation_applied"])
        self.assertEqual(semantic["code"],"completion_telemetry_mapping_result_mismatch")

    def test_errors_stop_without_suffix_or_retry(self):
        for mode in ("length","missing","unknown","usage","http","timeout","cancel","privacy"):
            with self.subTest(mode=mode):
                observed=self.probe(mode)
                self.assertEqual(len(observed["calls"]),1,observed)
                self.assertEqual(observed["result"]["exit_code"],4,observed)
                self.assertTrue(observed["result"]["claim_consumed"],observed)
                self.assertFalse(observed["result"]["internal_acceptance_passed"])
                if mode in ("timeout","cancel"):
                    self.assertEqual(observed["result"]["status"],"outcome_unknown",observed)

    def test_expired_authorization_prevents_claim_and_dispatch(self):
        observed=self.probe("expired")
        self.assertEqual(observed["calls"],[],observed)
        self.assertFalse(observed["result"]["claim_consumed"])
        self.assertEqual(observed["result"]["error_code"],"internal_authorization_expired")

    def test_duplicate_claim_never_dispatches_a_second_run(self):
        observed=self.probe("duplicate")
        self.assertEqual(len(observed["calls"]),30,observed)
        self.assertEqual(observed["result"]["exit_code"],0,observed)
        self.assertEqual(observed["result"]["repeated_result"]["exit_code"],4,observed)

    def test_z_source_drift_stops_after_first_actual_send(self):
        observed=self.probe("source_drift")
        self.assertEqual(len(observed["calls"]),1,observed)
        self.assertEqual(observed["result"]["exit_code"],4,observed)
        self.assertEqual(observed["result"]["error_code"],"internal_source_drift",observed)


if __name__=="__main__":unittest.main()
