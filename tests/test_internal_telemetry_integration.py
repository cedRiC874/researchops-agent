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

from researchops_internal_telemetry import contract as c, source


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
