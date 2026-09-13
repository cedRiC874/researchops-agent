"""Public owner in copied, committed synthetic source with actual admission gates."""
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
from tests.test_completion_timing_pre_execution import resign, documents


@unittest.skipUnless(os.name=='nt','designated Windows execution environment')
class PublicCampaignProcessTests(unittest.TestCase):
    def run_process(self,mode,*,implementation_version=4):
        if type(implementation_version) is not int or implementation_version not in (4,5):
            raise ValueError('synthetic campaign version invalid')
        fixture=TimedCampaignFixture(implementation_version=implementation_version,current_campaign_runtime=True); self.addCleanup(fixture.close)
        # The authoring template uses 100 ns to test integer boundaries. Real
        # filesystem/source checks need a deliberately signed synthetic budget.
        timing_plan=json.loads(fixture.campaign_timing_plan)
        self.assertEqual(timing_plan['artifact_sealing_timeout_ns'],100)
        timing_plan['artifact_sealing_timeout_ns']=30_000_000_000
        resign(fixture.signers,timing_plan,envelope_version=3)
        fixture.campaign_documents=documents(fixture.signers)
        fixture.campaign_observation=raw(fixture.signers.observation)
        fixture.campaign_timing_plan=raw(timing_plan)
        root=fixture.repository.root
        for name,payload in fixture.campaign_files.items():
            path=root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(payload)
        fixture.repository.git('update-ref','HEAD',fixture.campaign_commit)
        known=fixture.root/'public-campaign-known-folder'; known.mkdir()
        with patch.object(local_claim,'_windows_local_app_data',return_value=known):
            environment_id=local_claim.provision_local_claim_store()['execution_environment_id']
        fixture.set_execution_environment(environment_id)
        opening=fixture.root/'public-custodian-opening.json'
        opening.write_bytes(fixture.custodian_opening_package())
        inputs=fixture.campaign_admission_inputs
        admission={field.name:(getattr(inputs,field.name).decode() if type(getattr(inputs,field.name)) is bytes
                              else str(getattr(inputs,field.name)) if isinstance(getattr(inputs,field.name),Path)
                              else getattr(inputs,field.name)) for field in fields(inputs)}
        packet=dict(schema_version='provider-completion-campaign-invocation-input/1.0',
            documents={field.name:getattr(fixture.campaign_documents,field.name).decode() for field in fields(fixture.campaign_documents)},
            external_observation_bundle=fixture.campaign_observation.decode(),timing_plan=fixture.campaign_timing_plan.decode(),
            admission=admission,task_package_path=str(opening))
        packet_path=fixture.root/'public-invocation-packet.json'; packet_path.write_bytes(raw(packet))
        from tests.test_deepseek_completion_first_live_validation import _response_body
        metadata=fixture.root/'public-process-fixture.json'
        metadata.write_bytes(raw(dict(as_of=fixture.campaign_as_of,mock_response=_response_body('completed',512))))
        code='\n'.join((
            'import asyncio, io, json, socket, sys', 'from pathlib import Path', 'from datetime import datetime',
            # Construct loop before blocking Windows socketpair's local setup.
            'loop=asyncio.new_event_loop()',
            'original_connect=socket.socket.connect',
            'def local_socketpair(*args,**kwargs):',
            '    listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM)',
            '    client=None; reader=None',
            '    try:',
            "        listener.bind(('127.0.0.1',0)); listener.listen(1)",
            '        client=socket.socket(socket.AF_INET,socket.SOCK_STREAM)',
            '        original_connect(client,listener.getsockname())',
            '        reader,_=listener.accept()',
            '        return reader,client',
            '    except BaseException:',
            '        if reader is not None: reader.close()',
            '        if client is not None: client.close()',
            '        raise',
            '    finally: listener.close()',
            "def blocked(*args,**kwargs): raise AssertionError('external network forbidden')",
            'socket.socket.connect=blocked', 'socket.socket.connect_ex=blocked', 'socket.getaddrinfo=blocked',
            # Only the loop's local self-pipe has a private connected-pair path;
            # normal socket connect/DNS stay blocked, including during setup.
            'socket.socketpair=local_socketpair',
            'import httpx2', 'from researchops.audit import AuditLedger',
            'from researchops import audit as audit_module',
            'from researchops_completion_timing import campaign_run as run, campaign_runtime as runtime, campaign_phase as phase, campaign_start as start, local_claim',
            'from researchops_completion_timing.admission import TimedAdmissionInputs',
            'from researchops_external_closure.types import PreReceiptDocumentBytes',
            'metadata=json.loads(Path(sys.argv[3]).read_bytes())',
            # Synthetic historical UTC/locator/Key/transport only. No source,
            # signature, claim, opening, publication or verifier is stubbed.
            "start._utc_now=lambda:metadata['as_of']",
            'class FixtureDateTime(datetime):',
            '    @staticmethod',
            "    def now(tz): return datetime.fromisoformat(metadata['as_of'].replace('Z','+00:00'))",
            'local_claim.datetime=FixtureDateTime',
            'local_claim._windows_local_app_data=lambda:Path(sys.argv[2])',
            # Replace the clock, NOT _now: exercise the real versioned formatter.
            'audit_module.datetime=FixtureDateTime',
            'loads=[]; calls=[]',
            'def load_fake():',
            "    loads.append(True); return 'FIXTURE-NOT-A-REAL-KEY'",
            'phase._load_configured_key=load_fake',
            'def response(request):',
            "    calls.append(json.loads(request.content)['max_output_tokens'])",
            "    return httpx2.Response(200,json=metadata['mock_response'])",
            'runtime._native_transport=lambda:httpx2.MockTransport(response)',
            "if sys.argv[4]=='api':",
            '    packet=json.loads(Path(sys.argv[1]).read_bytes())',
            "    documents=PreReceiptDocumentBytes(**{name:value.encode() for name,value in packet['documents'].items()})",
            "    values=packet['admission']; values['artifact_directory']=Path(values['artifact_directory'])",
            "    for name in ('bundle_bytes','review_bytes','trust_bytes','observation_bytes'): values[name]=values[name].encode()",
            "    result=loop.run_until_complete(run.run_timed_campaign(documents=documents,external_observation_bundle=packet['external_observation_bundle'].encode(),timing_plan_bytes=packet['timing_plan'].encode(),admission_inputs=TimedAdmissionInputs(**values),task_package_path=Path(packet['task_package_path']),confirm_online=True))",
            '    summary=json.loads(result)',
            'else:',
            '    import contextlib',
            '    output=io.StringIO()',
            '    with contextlib.redirect_stdout(output):',
            "        exit_code=run.main(['--packet',sys.argv[1],'--confirm-online'])",
            "    summary=json.loads(output.getvalue()); summary['fixture_cli_exit_code']=exit_code",
            "assert summary['status']=='completed', summary",
            "assert summary['claim_consumed'] is True and summary['phase_completed'] is True",
            "assert summary['final_workspace_close_observed'] is True",
            "assert summary['run_count']==2 and summary['native_dispatch_count']==2",
            'assert calls==[512,512] and len(loads)==1',
            "archive=run.ROOT/summary['archive_relative_path']",
            "saved=archive.parent/(summary['authorization_id_sha256']+'.invocation.json')",
            'disk=json.loads(saved.read_bytes())',
            "assert disk['status']=='pre_return_archive_verified' and disk['final_workspace_close_observed'] is False",
            "assert disk['receipt_write_status']=='excluded_self_write'",
            "assert disk['manifest_file_sha256']==summary['manifest_file_sha256']",
            "assert len(list(archive.iterdir()))==7",
            "for path in archive.iterdir(): assert b'FIXTURE-NOT-A-REAL-KEY' not in path.read_bytes()",
            'print(json.dumps(summary,sort_keys=True))',
            'loop.run_until_complete(loop.shutdown_asyncgens())',
            'loop.run_until_complete(loop.shutdown_default_executor())',
            'loop.close()',
        ))
        if implementation_version==5:
            code=code.replace('run.run_timed_campaign(', 'run.run_timed_campaign_v5(')
            code=code.replace('run.main(', 'run.main_v5(')
        environment={name:os.environ[name] for name in ('SystemRoot','WINDIR','PATH','TEMP','TMP') if name in os.environ}
        environment['PYTHONPATH']=str(root/'src')
        cache=fixture.root/'public-mpl-cache'; cache.mkdir()
        environment['MPLCONFIGDIR']=str(cache); environment['MPLBACKEND']='Agg'
        result=subprocess.run([sys.executable,'-c',code,str(packet_path),str(known),str(metadata),mode],
            cwd=root,env=environment,capture_output=True,text=True,timeout=900,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0,result.stderr)
        summary=json.loads(result.stdout)
        self.assertEqual(summary['status'],'completed')
        self.assertFalse(summary['closure_claim_allowed']); self.assertFalse(summary['runtime_authority_granted'])
        self.assertFalse(summary['provider_calls_independently_verified'])
        # Test-only handoff of the actual producer directory while this helper's
        # registered cleanup still owns it. Never rewrite/reseal its contents.
        self.produced_fixture = fixture
        return summary

    def test_public_api_with_actual_signed_source_claim_and_private_opening(self):
        self.run_process('api')

    def test_packet_cli_with_actual_signed_source_claim_and_private_opening(self):
        self.assertEqual(self.run_process('cli')['fixture_cli_exit_code'],0)


if __name__=='__main__': unittest.main()
