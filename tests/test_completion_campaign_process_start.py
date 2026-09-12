"""Temporary process/source/store and synthetic opening; no real tasks/Key/Provider."""
from dataclasses import fields
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from researchops_completion_timing import local_claim
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests.timed_campaign_fixture import TimedCampaignFixture


@unittest.skipUnless(os.name == 'nt', 'designated Windows execution environment')
class ProcessBoundCampaignTests(unittest.TestCase):
    def run_process(self, *, with_opening=False, with_runtime=False):
        fixture = TimedCampaignFixture(); self.addCleanup(fixture.close)
        root = fixture.repository.root
        for name, payload in fixture.campaign_files.items():
            path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
        fixture.repository.git('update-ref', 'HEAD', fixture.campaign_commit)
        known = fixture.root / 'campaign-known-folder'; known.mkdir()
        with patch.object(local_claim, '_windows_local_app_data', return_value=known):
            environment_id = local_claim.provision_local_claim_store()['execution_environment_id']
        fixture.set_execution_environment(environment_id)
        opening_path = None
        if with_opening:
            opening_path = fixture.root / 'custodian-opening.json'
            opening_path.write_bytes(fixture.custodian_opening_package())
        inputs = fixture.campaign_admission_inputs
        encoded = {}
        for field in fields(inputs):
            value = getattr(inputs, field.name)
            encoded[field.name] = value.decode() if type(value) is bytes else str(value) if isinstance(value, Path) else value
        packet = dict(documents={field.name: getattr(fixture.campaign_documents, field.name).decode() for field in fields(fixture.campaign_documents)},
            observation=fixture.campaign_observation.decode(), plan=fixture.campaign_timing_plan.decode(),
            admission=encoded, as_of=fixture.campaign_as_of)
        if opening_path is not None: packet['opening_path'] = str(opening_path)
        if with_runtime:
            from tests.test_deepseek_completion_first_live_validation import _response_body
            packet['mock_response'] = _response_body('completed', 512)
        packet_path = fixture.root / 'campaign-packet.json'; packet_path.write_bytes(raw(packet))
        code = '\n'.join((
            'import asyncio, json, socket, sys', 'from pathlib import Path', 'from datetime import datetime',
            'loop = asyncio.new_event_loop()',
            "def blocked(*args, **kwargs): raise AssertionError('Provider network forbidden')",
            'socket.socket.connect = blocked', 'socket.socket.connect_ex = blocked', 'socket.getaddrinfo = blocked',
            'from researchops_completion_timing import campaign_start as start, local_claim',
            'from researchops_completion_timing.admission import TimedAdmissionInputs',
            'from researchops_external_closure.types import PreReceiptDocumentBytes',
            "packet = json.loads(Path(sys.argv[2]).read_bytes())",
            # Historical, signed synthetic documents: mock local UTC only. The
            # monotonic clock, graph/source/environment verification and all
            # local claim writes remain real. No claim of production freshness.
            "start._utc_now = lambda: packet['as_of']",
            'class FixtureDateTime:',
            '    @staticmethod',
            "    def now(tz): return datetime.fromisoformat(packet['as_of'].replace('Z', '+00:00'))",
            'local_claim.datetime = FixtureDateTime',
            'local_claim._windows_local_app_data = lambda: Path(sys.argv[3])',
            "documents = PreReceiptDocumentBytes(**{name: value.encode() for name, value in packet['documents'].items()})",
            "values = packet['admission']; values['artifact_directory'] = Path(values['artifact_directory'])",
            "for name in ('bundle_bytes', 'review_bytes', 'trust_bytes', 'observation_bytes'): values[name] = values[name].encode()",
            "prepared = start._prepare_process_bound_campaign_start(Path(sys.argv[1]), documents, external_observation_bundle=packet['observation'].encode(), timing_plan_bytes=packet['plan'].encode(), admission_inputs=TimedAdmissionInputs(**values))",
            'summary = prepared.summary()', 'phase = prepared.clock.snapshot()',
            "assert phase['phase']['task_released_ns'] is None and phase['phase']['key_loaded_ns'] is None and phase['attempts'] == []",
            "if 'opening_path' in packet:",
            '    from researchops_completion_timing.campaign_tasks import _CampaignTaskOwner',
            '    owner = _CampaignTaskOwner(prepared)',
            "    summary['opening'] = owner.open_tasks(Path(packet['opening_path']))",
            "    assert prepared.clock.snapshot()['phase']['task_released_ns'] is not None",
            "    assert prepared.clock.snapshot()['phase']['key_loaded_ns'] is None",
            "    assert prepared.clock.snapshot()['attempts'] == []",
            "    if 'mock_response' in packet:",
            '        import httpx2',
            '        from researchops.audit import AuditLedger',
            '        from researchops_completion_timing import campaign_runtime as runtime',
            '        calls = []',
            '        def handler(request):',
            "            calls.append(json.loads(request.content)['max_output_tokens'])",
            "            return httpx2.Response(200, json=packet['mock_response'])",
            '        runtime._native_transport = lambda: httpx2.MockTransport(handler)',
            "        ledger = AuditLedger(Path(sys.argv[1]) / 'campaign-audit.sqlite3', clock=lambda: FixtureDateTime.now(None))",
            '        factory = runtime._CampaignModelFactory(owner, ledger)',
            '        factory.before_key_load(); factory.key_loaded()',
            "        for index in range(2): loop.run_until_complete(factory.run_case(index, api_key='FIXTURE-NOT-A-REAL-KEY'))",
            "        summary['runtime'] = {'calls':calls, 'budget':factory._budget.summary(), 'runs':[]}",
            '        for run in factory._runs:',
            '            exported = ledger.export_run(run)',
            "            assert owner._opened._canary_b64 not in json.dumps(exported)",
            "            assert 'FIXTURE-NOT-A-REAL-KEY' not in json.dumps(exported)",
            "            summary['runtime']['runs'].append({'chain_valid':ledger.verify_chain(run).valid,'event_types':[row['event_type'] for row in exported['events']]})",
            'else: prepared._take_for_runtime()',
            'try: prepared._take_for_runtime()',
            "except start.CampaignStartError: summary['duplicate_handoff_blocked'] = True",
            "else: raise AssertionError('second handoff allowed')",
            'try: local_claim._reserve_with_ownership(prepared.request_bytes)',
            "except local_claim.LocalClaimError: summary['duplicate_claim_blocked'] = True",
            "else: raise AssertionError('claim replay allowed')",
            "summary['phase_cap_ns'] = prepared.clock._phase_cap",
            'print(json.dumps(summary, sort_keys=True))',
            'loop.close()',
        ))
        environment = {name: os.environ[name] for name in ('SystemRoot', 'WINDIR', 'PATH', 'TEMP', 'TMP') if name in os.environ}
        environment['PYTHONPATH'] = str(root / 'src')
        cache = fixture.root / 'mpl-cache'; cache.mkdir()
        environment['MPLCONFIGDIR'] = str(cache); environment['MPLBACKEND'] = 'Agg'
        result = subprocess.run([sys.executable, '-c', code, str(root), str(packet_path), str(known)],
            cwd=root, env=environment, capture_output=True, text=True, timeout=900, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        for name in ('claim_consumed', 'source_and_head_matched', 'loaded_project_origins_matched',
                     'locked_distribution_versions_matched', 'duplicate_handoff_blocked', 'duplicate_claim_blocked'):
            self.assertTrue(summary[name], name)
        for name in ('task_released', 'key_loaded', 'runtime_authority_granted', 'closure_claim_allowed', 'retry_authorized'):
            self.assertFalse(summary[name], name)
        self.assertEqual(summary['phase_cap_ns'], 600_000_000_000)
        self.assertEqual(summary['provider_calls'], 0)
        return summary

    def test_actual_process_source_versions_and_winning_claim_precede_task_and_key(self):
        self.run_process()

    def test_actual_claimed_process_opens_only_matching_private_package_without_key(self):
        summary = self.run_process(with_opening=True)
        self.assertTrue(summary['opening']['opening_verified'])
        self.assertTrue(summary['opening']['private_read_attempted'])
        self.assertEqual(summary['opening']['case_count'], 2)
        self.assertFalse(summary['opening']['key_loaded'])
        self.assertFalse(summary['opening']['runtime_authority_granted'])

    def test_actual_claimed_owner_runs_two_sdk_cases_with_budget_and_timed_ledger(self):
        summary = self.run_process(with_opening=True, with_runtime=True)
        runtime = summary['runtime']
        self.assertEqual(runtime['calls'], [512, 512])
        self.assertEqual(runtime['budget']['reserved_request_count'], 2)
        self.assertEqual(runtime['budget']['completed_case_count'], 2)
        for run in runtime['runs']:
            self.assertTrue(run['chain_valid'])
            self.assertEqual(run['event_types'], ['run_started', 'model_request_started', 'provider_transport_request_sent',
                                                  'model_response_telemetry_recorded', 'model_call_recorded',
                                                  'model_run_usage_recorded', 'run_status_changed'])


if __name__ == '__main__': unittest.main()
