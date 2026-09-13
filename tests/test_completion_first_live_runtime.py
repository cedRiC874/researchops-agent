"""Actual claimed/process-bound factory to SDK/MockTransport/SQLite in a child."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import first_live, first_live_control as control, local_claim
from researchops_completion_timing.first_live_runtime import _FirstLiveModelFactory
from researchops_completion_timing import first_live_runtime as runtime
from researchops import model_providers
from researchops_completion_timing.first_live_start import FirstLiveStartError
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests.timed_first_live_evidence_fixture import TimedEvidenceFixture
from tests.test_deepseek_completion_first_live_validation import _response_body


class FirstLiveRuntimeBoundaryTests(unittest.TestCase):
    def test_native_transport_explicitly_disables_environment_and_retries(self):
        with patch.object(runtime.httpx2, "AsyncHTTPTransport") as constructor:
            runtime._native_transport()
        constructor.assert_called_once_with(retries=0, trust_env=False)

    def test_unchecked_preparation_cannot_enter_factory(self):
        with self.assertRaisesRegex(FirstLiveStartError, "first_live_factory_preparation_required"):
            _FirstLiveModelFactory(object(), object())

    def run_factory_process(self, mode, *, implementation_version=4):
        if type(implementation_version) is not int or implementation_version not in (4, 5):
            raise ValueError('synthetic runtime version invalid')
        fixture = TimedEvidenceFixture(implementation_version=implementation_version, current_startup_runtime=True); self.addCleanup(fixture.close)
        root = fixture.repository.root
        fixture.repository.git("update-ref", "HEAD", fixture.source_commit)
        known = fixture.root / "runtime-known-folder"; known.mkdir()
        with patch.object(local_claim, "_windows_local_app_data", return_value=known):
            environment_id = local_claim.provision_local_claim_store()["execution_environment_id"]
        plan = fixture.archive.data["plan"]
        plan["binding"]["execution_environment_id"] = environment_id
        plan["binding"]["validation_run_id"] = first_live.validation_run_id(plan["binding"])
        plan["plan_commitment_sha256"] = first_live.commitment("plan", plan)
        auth = fixture.archive.data["auth"]
        now = datetime.now(timezone.utc) - timedelta(seconds=1)
        stamp = lambda value: value.isoformat().replace("+00:00", "Z")
        auth.update(execution_environment_id=environment_id, timing_plan_commitment_sha256=plan["plan_commitment_sha256"],
            authorized_at_utc=stamp(now), expires_at_utc=stamp(now + timedelta(seconds=900)),
            pricing_snapshot_date=now.date().isoformat(), pricing_retrieved_at_utc=stamp(now))
        auth["authorization_binding_sha256"] = control.authorization_binding(auth)
        plan_file, auth_file = fixture.root / "runtime-plan.json", fixture.root / "runtime-auth.json"
        plan_file.write_bytes(json.dumps(plan, indent=2).encode() if mode == "publish" else raw(plan))
        auth_file.write_bytes(raw(auth))
        response_file = fixture.root / "responses.json"
        response_file.write_bytes(raw({"responses": [_response_body("completed", 256), _response_body("incomplete", 16)]}))
        code = "\n".join((
            "import asyncio, json, socket, sys", "from pathlib import Path", "loop = asyncio.new_event_loop()",
            "def blocked(*args, **kwargs): raise AssertionError('real Provider network forbidden')",
            "socket.socket.connect = blocked", "socket.socket.connect_ex = blocked", "socket.getaddrinfo = blocked",
            "import httpx2", "from researchops.audit import AuditLedger",
            "from researchops_completion_timing import local_claim, first_live_runtime as runtime",
            "from researchops_completion_timing.first_live_start import _prepare_process_bound_first_live_start, FirstLiveStartError",
            "from researchops_completion_timing.first_live_publish import _publish_completed_first_live",
            "from researchops_completion_timing.first_live_failure import build_first_live_failure_receipt",
            "local_claim._windows_local_app_data = lambda: Path(sys.argv[4])",
            "auth = Path(sys.argv[3]).read_bytes()",
            "prepared = _prepare_process_bound_first_live_start(Path(sys.argv[1]), plan_bytes=Path(sys.argv[2]).read_bytes(), authorization_bytes=auth, expected_authorization_binding_sha256=json.loads(auth)['authorization_binding_sha256'])",
            "responses = json.loads(Path(sys.argv[5]).read_bytes())['responses']; calls = []",
            "def handler(request):",
            "    body = json.loads(request.content); calls.append(body['max_output_tokens'])",
            "    if sys.argv[7] == 'http_error': return httpx2.Response(403, json={'error':{'message':'synthetic forbidden'}})",
            "    if sys.argv[7] == 'source_change':",
            "        path = Path(sys.argv[1]) / 'src/researchops/__init__.py'; path.write_bytes(path.read_bytes()+b'\\n# synthetic source change after send\\n')",
            "    if not responses: raise AssertionError('third model request')",
            "    return httpx2.Response(200, json=responses.pop(0), headers={'x-request-id': 'synthetic-request'})",
            "runtime._native_transport = lambda: httpx2.MockTransport(handler)",
            "ledger = AuditLedger(Path(sys.argv[6]))", "factory = runtime._FirstLiveModelFactory(prepared, ledger)",
            "factory.before_key_load(); factory.key_loaded()",  # Fake key only; no real loader called.
            "async def run():",
            "    results = []; failures = []; failure_receipts = []; publication = None; fake_key = 'FIXTURE-NOT-A-REAL-KEY'",
            "    for _ in range(2):",
            "        try: results.append(await factory.run_next(api_key=fake_key))",
            "        except Exception as error:",
            "            failures.append(type(error).__name__)",
            "            failure_receipts.append(json.loads(build_first_live_failure_receipt(Path(sys.argv[1]), error, factory=factory)))",
            "    fake_key = None",
            "    duplicate_blocked = False",
            "    if sys.argv[7] == 'publish':",
            "        factory.complete_phase_after_key_release()",
            "        value = _publish_completed_first_live(factory, Path(sys.argv[8]), authorization_bytes=auth)",
            "        publication = {'status':value['status'], 'file_count':len(value['created_files']), 'bundle':json.loads(value['bundle_bytes']), 'authority':value['runtime_authority_granted']}",
            "    else:",
            "        try: await factory.run_next(api_key='FIXTURE-NOT-A-REAL-KEY')",
            "        except FirstLiveStartError: duplicate_blocked = True",
            "    exported = ledger.export_run(factory._run)",
            "    assert 'FIXTURE-NOT-A-REAL-KEY' not in json.dumps(exported)",
            "    terminals = [row for row in exported['events'] if row['event_type']=='model_response_telemetry_recorded']",
            "    kinds = [r['safe_payload']['terminal_kind'] for r in exported['events'] if 'terminal_kind' in r['safe_payload']]",
            "    print(json.dumps({'calls':calls, 'states':[r['completion_state'] for r in results], 'failures':failures, 'failure_receipts':failure_receipts, 'terminal_kinds':kinds, 'duplicate_blocked':duplicate_blocked, 'terminal_count':len(terminals), 'timed_versions':[r['safe_payload']['schema_version'] for r in terminals], 'chain_valid':ledger.verify_chain(factory._run).valid, 'factory_halted':factory._failed, 'publication':publication}))",
            "try: loop.run_until_complete(run())", "finally: loop.close()",
        ))
        if mode.startswith("public_"):
            code = "\n".join((
                "import asyncio, json, socket, sys", "from pathlib import Path", "loop = asyncio.new_event_loop()",
                "def blocked(*args, **kwargs): raise AssertionError('real Provider network forbidden')",
                "socket.socket.connect = blocked", "socket.socket.connect_ex = blocked", "socket.getaddrinfo = blocked",
                "import httpx2",
                "from researchops_completion_timing import local_claim, first_live_runtime as runtime, first_live_run as runner",
                "from researchops_completion_timing.first_live_failure import validate_failure_receipt",
                "from researchops_completion_timing.first_live_failure_artifacts import verify_failure_artifacts",
                "local_claim._windows_local_app_data = lambda: Path(sys.argv[4])",
                "calls = []; key_loads = []; responses = json.loads(Path(sys.argv[5]).read_bytes())['responses']",
                "def load_key():",
                "    key_loads.append(True)",
                "    if sys.argv[7] == 'public_missing_key': raise RuntimeError('FAKE-SECRET-ERROR')",
                "    return 'FIXTURE-NOT-A-REAL-KEY'",
                "runner._load_configured_key = load_key",
                "def handler(request):",
                "    body = json.loads(request.content); calls.append(body['max_output_tokens'])",
                "    if sys.argv[7] == 'public_http_error': return httpx2.Response(403, json={'error':{'message':'FAKE-SECRET-ERROR'}})",
                "    if not responses: raise AssertionError('third model request')",
                "    return httpx2.Response(200, json=responses.pop(0), headers={'x-request-id':'synthetic-request'})",
                "runtime._native_transport = lambda: httpx2.MockTransport(handler)",
                "auth = Path(sys.argv[3]).read_bytes(); plan = Path(sys.argv[2]).read_bytes()",
                "async def run():",
                "    payload = await runner.run_timed_first_live(plan_bytes=plan, authorization_bytes=auth, expected_authorization_binding_sha256=json.loads(auth)['authorization_binding_sha256'], confirm_online=True)",
                "    result = json.loads(payload); archive_count = None; failure_artifact = None",
                "    if result['status'] == 'completed':",
                "        directory = Path(sys.argv[1]) / result['artifact_directory']; archive_count = len(list(directory.iterdir()))",
                "        assert (Path(sys.argv[1]) / result['bundle_path']).is_file()",
                "    else:",
                "        validate_failure_receipt(Path(sys.argv[1]), payload)",
                "        work = Path(sys.argv[1]) / 'output/first-live-v4' / (json.loads(plan)['binding']['authorization_id_sha256'] + '.work')",
                "        manifest = json.loads((work / 'failure_manifest.json').read_bytes())",
                "        assert (work / 'failure_receipt.json').read_bytes() == payload",
                "        failure_artifact = verify_failure_artifacts(Path(sys.argv[1]), work, expected_manifest_commitment_sha256=manifest['manifest_commitment_sha256'])",
                "        failure_artifact['event_count'] = manifest['audit_snapshot']['event_count']",
                "    for path in (Path(sys.argv[1]) / 'output').rglob('*'):",
                "        if path.is_file():",
                "            saved = path.read_bytes(); assert b'FIXTURE-NOT-A-REAL-KEY' not in saved and b'FAKE-SECRET-ERROR' not in saved",
                "    assert b'FAKE-SECRET-ERROR' not in payload",
                "    print(json.dumps({'result':result,'calls':calls,'key_load_count':len(key_loads),'archive_count':archive_count,'failure_artifact':failure_artifact}))",
                "try: loop.run_until_complete(run())", "finally: loop.close()",
            ))
        if implementation_version == 5:
            code = code.replace('_prepare_process_bound_first_live_start', '_prepare_process_bound_first_live_start_v5')
            code = code.replace('runtime._FirstLiveModelFactory(', 'runtime._FirstLiveModelFactoryV5(')
            code = code.replace('_publish_completed_first_live', '_publish_completed_first_live_v5')
            code = code.replace('runner.run_timed_first_live(', 'runner.run_timed_first_live_v5(')
        environment = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP") if name in os.environ}
        environment["PYTHONPATH"] = str(root / "src")
        cache = fixture.root / "mpl-cache"; cache.mkdir()
        environment["MPLCONFIGDIR"] = str(cache)
        environment["MPLBACKEND"] = "Agg"
        result = subprocess.run([sys.executable, "-c", code, str(root), str(plan_file), str(auth_file), str(known),
            str(response_file), str(fixture.root / "runtime-audit.sqlite3"), mode, str(fixture.root / "published")], cwd=root, env=environment,
            capture_output=True, text=True, timeout=360, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @unittest.skipUnless(os.name == "nt", "fixed Windows store")
    def test_real_checked_process_factory_runs_only_two_frozen_mock_requests(self):
        observed = self.run_factory_process("two")
        self.assertEqual(observed["calls"], [256, 16])
        self.assertEqual(observed["states"], ["completed", "incomplete_length"])
        self.assertEqual(observed["timed_versions"], ["provider-completion-ledger-event/1.2"] * 2)
        self.assertTrue(observed["duplicate_blocked"])
        self.assertTrue(observed["chain_valid"])
        self.assertTrue(observed["factory_halted"])

    @unittest.skipUnless(os.name == "nt", "fixed Windows store")
    def test_http_error_records_once_and_never_retries_or_starts_second_request(self):
        observed = self.run_factory_process("http_error")
        self.assertEqual(observed["calls"], [256])
        self.assertEqual(observed["states"], [])
        self.assertEqual(observed["terminal_kinds"], ["http_error"])
        self.assertEqual(observed["failure_receipts"][0]["dispatch_attempt_count"], 1)
        self.assertEqual(observed["failure_receipts"][0]["status"], "failed")
        self.assertIsNone(observed["failure_receipts"][0]["token_usage"])
        self.assertTrue(observed["factory_halted"])
        self.assertTrue(observed["chain_valid"])

    @unittest.skipUnless(os.name == "nt", "fixed Windows store")
    def test_source_change_after_first_send_blocks_second_provider_request(self):
        observed = self.run_factory_process("source_change")
        self.assertEqual(observed["calls"], [256])
        self.assertEqual(observed["states"], ["completed"])
        self.assertEqual(observed["terminal_kinds"], ["response_accepted"])
        self.assertEqual(observed["failure_receipts"][0]["dispatch_attempt_count"], 1)
        self.assertEqual(observed["failure_receipts"][0]["validated_response_count"], 1)
        self.assertTrue(observed["factory_halted"])
        self.assertTrue(observed["chain_valid"])

    @unittest.skipUnless(os.name == "nt", "fixed Windows store")
    def test_completed_factory_writes_and_verifies_the_actual_eight_file_archive(self):
        observed = self.run_factory_process("publish")
        self.assertEqual(observed["calls"], [256, 16])
        self.assertFalse(observed["factory_halted"])
        self.assertEqual(observed["publication"]["status"], "first_live_archive_written_and_verified")
        self.assertEqual(observed["publication"]["file_count"], 8)
        self.assertFalse(observed["publication"]["authority"])

    @unittest.skipUnless(os.name == "nt", "fixed Windows store")
    def test_public_invocation_runs_checked_factory_and_publishes_archive(self):
        observed = self.run_factory_process("public_success")
        self.assertEqual(observed["result"]["status"], "completed")
        self.assertEqual(observed["calls"], [256, 16])
        self.assertEqual(observed["key_load_count"], 1)
        self.assertEqual(observed["archive_count"], 8)
        self.assertFalse(observed["result"]["closure_claim_allowed"])

    @unittest.skipUnless(os.name == "nt", "fixed Windows store")
    def test_public_http_failure_stops_after_one_call_with_sanitized_receipt(self):
        observed = self.run_factory_process("public_http_error")
        self.assertEqual(observed["calls"], [256])
        self.assertEqual(observed["result"]["status"], "failed")
        self.assertEqual(observed["result"]["dispatch_attempt_count"], 1)
        self.assertTrue(observed["result"]["claim_consumed"])
        self.assertIsNone(observed["archive_count"])
        self.assertTrue(observed['failure_artifact']['audit_chain_bound'])
        self.assertEqual(observed['failure_artifact']['event_count'], 4)
        self.assertFalse(observed['failure_artifact']['receipt_counts_independently_verified'])

    @unittest.skipUnless(os.name == "nt", "fixed Windows store")
    def test_public_key_load_failure_consumes_claim_but_sends_nothing(self):
        observed = self.run_factory_process("public_missing_key")
        self.assertEqual(observed["calls"], [])
        self.assertEqual(observed["key_load_count"], 1)
        self.assertEqual(observed["result"]["status"], "failed_before_dispatch")
        self.assertEqual(observed["result"]["dispatch_attempt_count"], 0)
        self.assertTrue(observed["result"]["claim_consumed"])
        self.assertTrue(observed['failure_artifact']['audit_chain_bound'])
        self.assertEqual(observed['failure_artifact']['event_count'], 2)


class TimedTransportConstructionTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_client_constructor_failure_closes_the_owned_transport(self):
        closed = []
        class Transport:
            async def aclose(self): closed.append(True)
        class Session:
            def _create_first_live_timed_transport(self): return Transport()
        class FailingClient:
            def __init__(self, **kwargs): raise RuntimeError("synthetic constructor failure")
        # This is a resource-failure unit test only. It does not claim session
        # admission: the separate real-process tests exercise that entire gate.
        with patch.object(model_providers, "_validate_completion_session"), \
             patch.object(model_providers, "_load_responses_transport", return_value=(object, object, FailingClient)):
            with self.assertRaisesRegex(RuntimeError, "synthetic constructor failure"):
                async with model_providers.DeepSeekProvider().open_model(model_id="deepseek-v4-flash",
                        api_key="FIXTURE-NOT-A-REAL-KEY", completion_telemetry_session=Session()):
                    self.fail("client creation unexpectedly succeeded")
        self.assertEqual(closed, [True])


if __name__ == "__main__":
    unittest.main()
