"""Isolated v5 preparation process with synthetic authorization and temporary store."""
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from researchops_completion_timing import first_live, first_live_control as control, local_claim
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests.timed_first_live_evidence_fixture import TimedEvidenceFixture


@unittest.skipUnless(os.name == 'nt', 'designated Windows environment')
class FirstLiveV5ProcessTests(unittest.TestCase):
    def test_v5_process_checks_source_origins_lock_and_temp_claim_without_key_or_provider(self):
        fixture = TimedEvidenceFixture(implementation_version=5); self.addCleanup(fixture.close)
        root = fixture.repository.root
        fixture.repository.git('update-ref', 'HEAD', fixture.source_commit)
        known = fixture.root / 'process-test-known-folder'; known.mkdir()
        with patch.object(local_claim, '_windows_local_app_data', return_value=known):
            environment_id = local_claim.provision_local_claim_store()['execution_environment_id']
        plan = fixture.archive.data['plan']
        plan['binding']['execution_environment_id'] = environment_id
        plan['binding']['validation_run_id'] = first_live.validation_run_id(plan['binding'])
        plan['plan_commitment_sha256'] = first_live.commitment('plan', plan)
        auth = fixture.archive.data['auth']; now = datetime.now(timezone.utc) - timedelta(seconds=1)
        stamp = lambda value: value.isoformat().replace('+00:00', 'Z')
        auth.update(execution_environment_id=environment_id, timing_plan_commitment_sha256=plan['plan_commitment_sha256'],
            authorized_at_utc=stamp(now), expires_at_utc=stamp(now + timedelta(seconds=900)),
            pricing_snapshot_date=now.date().isoformat(), pricing_retrieved_at_utc=stamp(now))
        auth['authorization_binding_sha256'] = control.authorization_binding(auth)
        plan_path, auth_path = fixture.root / 'v5-plan.json', fixture.root / 'v5-authorization.json'
        plan_path.write_bytes(raw(plan)); auth_path.write_bytes(raw(auth))
        code = '\n'.join((
            'import json, socket, sys', 'from pathlib import Path',
            "def blocked(*args, **kwargs): raise AssertionError('Provider network forbidden')",
            'socket.socket.connect = blocked', 'socket.socket.connect_ex = blocked',
            'socket.create_connection = blocked', 'socket.getaddrinfo = blocked',
            'from researchops_completion_timing import local_claim',
            'local_claim._windows_local_app_data = lambda: Path(sys.argv[4])',
            'from researchops_completion_timing.first_live_start import _prepare_process_bound_first_live_start_v5',
            'authorization = Path(sys.argv[3]).read_bytes()',
            "prepared = _prepare_process_bound_first_live_start_v5(Path(sys.argv[1]), plan_bytes=Path(sys.argv[2]).read_bytes(), authorization_bytes=authorization, expected_authorization_binding_sha256=json.loads(authorization)['authorization_binding_sha256'])",
            'print(json.dumps(prepared.summary(), sort_keys=True))', 'prepared.abort()',
        ))
        environment = {name: os.environ[name] for name in ('SystemRoot', 'WINDIR', 'PATH', 'TEMP', 'TMP') if name in os.environ}
        environment['PYTHONPATH'] = str(root / 'src')
        result = subprocess.run([sys.executable, '-c', code, str(root), str(plan_path), str(auth_path), str(known)],
            cwd=root, env=environment, capture_output=True, text=True, timeout=360, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual((summary['implementation_version'], summary['source_recipe_version']), (5, 4))
        self.assertTrue(summary['loaded_project_origins_matched'])
        self.assertTrue(summary['locked_distribution_versions_matched'])
        self.assertTrue(summary['claim_consumed'])
        for name in ('task_released', 'key_loaded', 'runtime_authority_granted', 'first_live_success_verified', 'retry_authorized'):
            self.assertFalse(summary[name])


if __name__ == '__main__': unittest.main()
