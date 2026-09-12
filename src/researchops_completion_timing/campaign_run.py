"""Explicit campaign entry with fixed gates and local-only diagnostic summaries."""
from __future__ import annotations

from pathlib import Path
import argparse
import asyncio
import json
import sys

import jsonschema

from researchops_external_closure.io import read_regular_file_no_follow
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object
from researchops_external_closure.schema_guard import only_local_schema_references
from .contract import _decode, digest

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = 'evals/provider_completion_campaign_run_v1/contract_v1.json'
CONTRACT_SHA256 = '9178b3851d2a36a8f73eb83568c1b22564c441c121674130b13b2911cf9fb94a'
CONTRACT_BYTES = 9811


class CampaignInvocationError(ValueError):
    def __init__(self, code):
        self.code = code
        self.retry_authorized = False
        super().__init__(code)


def load_invocation_contract(root=ROOT):
    try:
        payload = read_regular_file_no_follow(root/CONTRACT_PATH,max_bytes=32768)
        if len(payload) != CONTRACT_BYTES or digest(payload) != CONTRACT_SHA256:
            raise ValueError('contract changed')
        value = decode_strict_json_object(payload,max_bytes=32768)
        schema = value['summary_schema']
        if not only_local_schema_references(schema): raise ValueError('external schema')
        jsonschema.Draft202012Validator.check_schema(schema)
        return value
    except Exception:
        raise CampaignInvocationError('campaign_invocation_contract_invalid') from None


def validate_invocation_summary(payload, *, root=ROOT):
    profile = load_invocation_contract(root)
    try:
        value = _decode(payload,profile['summary_schema'],profile['limits']['summary_bytes'],())
        if raw(value) != payload: raise ValueError('noncanonical')
        if value['status'] in {'pre_return_archive_verified','completed'}:
            if (value['archive_relative_path'] != profile['fixed_paths']['parent']+'/'+value['authorization_id_sha256']
                or value['native_dispatch_count'] < value['run_count']):
                raise ValueError('binding mismatch')
        return value
    except Exception:
        raise CampaignInvocationError('campaign_invocation_summary_invalid') from None


def _not_confirmed_summary():
    """No input/contract/task/Key/store read; validate the constant offline."""
    return raw(dict(schema_version='provider-completion-campaign-invocation/1.0',status='not_confirmed',
        observation_scope='local_invocation_only',authorization_id_sha256=None,claim_consumed=False,
        phase_completed=None,archive_status='not_attempted',final_workspace_close_observed=None,
        run_count=None,native_dispatch_count=None,archive_relative_path=None,manifest_file_sha256=None,
        receipt_write_status='not_attempted',error_code='campaign_invocation_not_confirmed',
        retry_authorized=False,closure_claim_allowed=False,runtime_authority_granted=False,
        provider_bill_cny=None,provider_calls_independently_verified=False))


def _bounded_inputs(profile, documents, observation, plan, admission, task_path):
    from .admission import TimedAdmissionInputs
    from researchops_external_closure.types import PreReceiptDocumentBytes
    if type(documents) is not PreReceiptDocumentBytes or type(admission) is not TimedAdmissionInputs:
        raise CampaignInvocationError('campaign_invocation_inputs_invalid')
    limits=profile['limits']; fields=profile['cli']
    payloads=[getattr(documents,name) for name in fields['document_fields']]
    payloads += [observation]+[getattr(admission,name) for name in fields['admission_utf8_document_fields']]
    if (any(type(value) is not bytes or len(value)>limits['public_document_bytes'] for value in payloads)
        or type(plan) is not bytes or len(plan)>limits['timing_plan_bytes']
        or sum(map(len,payloads))+len(plan)>limits['public_document_bytes_total']
        or type(task_path) is not type(Path()) or not task_path.is_absolute()
        or len(str(task_path))>limits['path_characters'] or '\0' in str(task_path)
        or type(admission.artifact_directory) is not type(Path()) or not admission.artifact_directory.is_absolute()
        or len(str(admission.artifact_directory))>limits['path_characters']):
        raise CampaignInvocationError('campaign_invocation_inputs_invalid')
    for name in set(fields['admission_fields'])-set(fields['admission_utf8_document_fields'])-{'artifact_directory'}:
        value=getattr(admission,name)
        if type(value) is not str or len(value)>limits['path_characters']:
            raise CampaignInvocationError('campaign_invocation_inputs_invalid')


def _failure_value(code, *, consumed=None, identifier=None, factory=None, archive='not_attempted', receipt='not_attempted', closed=None):
    value=json.loads(_not_confirmed_summary())
    value.update(status='failed',error_code=code,claim_consumed=consumed,authorization_id_sha256=identifier,
                 archive_status=archive,receipt_write_status=receipt,final_workspace_close_observed=closed)
    if factory is not None:
        value.update(phase_completed=factory._phase_finished,run_count=len(factory._runs),native_dispatch_count=factory._network_count)
        state=factory._prepared.clock.snapshot()
        if state['active_attempt'] is not None or any(item['terminal_kind']=='outcome_unknown' for item in state['attempts']):
            value['status']='outcome_unknown'
    if consumed is None or code=='campaign_invocation_unknown': value['status']='outcome_unknown'
    return value


def _serialize_summary(value):
    try:
        payload=raw(value); validate_invocation_summary(payload)
        return payload
    except Exception:
        # A damaged schema or unavailable observation cannot justify success.
        return raw(_failure_value('campaign_invocation_unknown'))


async def run_timed_campaign(*, documents, external_observation_bundle, timing_plan_bytes, admission_inputs,
                             task_package_path, confirm_online=False):
    return await _run_timed_campaign_impl(documents=documents, external_observation_bundle=external_observation_bundle,
        timing_plan_bytes=timing_plan_bytes, admission_inputs=admission_inputs, task_package_path=task_package_path,
        confirm_online=confirm_online, implementation_version=4)


async def run_timed_campaign_v5(*, documents, external_observation_bundle, timing_plan_bytes, admission_inputs,
                                task_package_path, confirm_online=False):
    return await _run_timed_campaign_impl(documents=documents, external_observation_bundle=external_observation_bundle,
        timing_plan_bytes=timing_plan_bytes, admission_inputs=admission_inputs, task_package_path=task_package_path,
        confirm_online=confirm_online, implementation_version=5)


def _campaign_ledger(database, *, implementation_version):
    """Version-selected write format, not a caller override or a read migration."""
    from researchops.audit import AuditLedger
    if type(implementation_version) is not int or implementation_version not in (4, 5):
        raise CampaignInvocationError('campaign_invocation_inputs_invalid')
    if implementation_version == 5:
        return AuditLedger(database, timestamp_format='utc_z')
    return AuditLedger(database)


async def _run_timed_campaign_impl(*, documents, external_observation_bundle, timing_plan_bytes, admission_inputs,
                                   task_package_path, confirm_online, implementation_version):
    if confirm_online is not True: return _not_confirmed_summary()
    prepared=owner=factory=None; consumed=False; identifier=None
    stage='inputs'; archive_status='not_attempted'; receipt_status='not_attempted'; closed=None
    try:
        if type(implementation_version) is not int or implementation_version not in (4, 5):
            raise CampaignInvocationError('campaign_invocation_inputs_invalid')
        profile=load_invocation_contract()
        _bounded_inputs(profile,documents,external_observation_bundle,timing_plan_bytes,admission_inputs,task_package_path)
        from researchops.audit import AuditLedger
        from .campaign_start import _prepare_process_bound_campaign_start, _prepare_process_bound_campaign_start_v5
        from .campaign_tasks import _CampaignTaskOwner, _CampaignTaskOwnerV5
        from .campaign_runtime import _CampaignModelFactory, _CampaignModelFactoryV5
        from .campaign_phase import _run_owned_campaign_phase
        from .campaign_workspace import _owned_campaign_work_paths
        from .campaign_publish import _publish_completed_campaign, _publish_completed_campaign_v5, _publication_checkpoint
        from .first_live_publish import _write_exclusive
        stage='admission'
        prepare = _prepare_process_bound_campaign_start if implementation_version == 4 else _prepare_process_bound_campaign_start_v5
        prepared=prepare(ROOT,documents,external_observation_bundle=external_observation_bundle,
            timing_plan_bytes=timing_plan_bytes,admission_inputs=admission_inputs)
        consumed=True; identifier=prepared.proof.scope.authorization_id_sha256
        owner_type = _CampaignTaskOwner if implementation_version == 4 else _CampaignTaskOwnerV5
        factory_type = _CampaignModelFactory if implementation_version == 4 else _CampaignModelFactoryV5
        publisher = _publish_completed_campaign if implementation_version == 4 else _publish_completed_campaign_v5
        stage='task_opening'; owner=owner_type(prepared); owner.open_tasks(task_package_path)
        stage='workspace'
        with _owned_campaign_work_paths(ROOT,identifier) as (database,directory,summary_path):
            closed=False
            factory=factory_type(owner,_campaign_ledger(database, implementation_version=implementation_version))
            stage='phase'
            phase=await _run_owned_campaign_phase(factory)
            if not phase['phase_completed']:
                raise CampaignInvocationError('campaign_invocation_phase_failed')
            stage='publication'; archive_status='unknown'
            publication=publisher(factory,directory)
            stage='finalization'; archive_status='unknown'
            value=dict(schema_version='provider-completion-campaign-invocation/1.0',status='pre_return_archive_verified',
                observation_scope='local_invocation_only',authorization_id_sha256=identifier,claim_consumed=True,
                phase_completed=True,archive_status='verified',final_workspace_close_observed=False,
                run_count=len(factory._runs),native_dispatch_count=factory._network_count,
                archive_relative_path=profile['fixed_paths']['parent']+'/'+identifier,
                manifest_file_sha256=publication['manifest_file_sha256'],receipt_write_status='excluded_self_write',
                error_code=None,retry_authorized=False,closure_claim_allowed=False,runtime_authority_granted=False,
                provider_bill_cny=None,provider_calls_independently_verified=False)
            snapshot=raw(value); validate_invocation_summary(snapshot)
            _publication_checkpoint(factory)
            made=[]; receipt_status='unknown'
            try: _write_exclusive(summary_path,snapshot,made)
            except BaseException:
                receipt_status='incomplete' if made else 'unknown'
                raise
            receipt_status='completed'
            _publication_checkpoint(factory)
        closed=True
        _publication_checkpoint(factory)
        value.update(status='completed',final_workspace_close_observed=True,receipt_write_status='completed')
        final=raw(value); validate_invocation_summary(final)
        _publication_checkpoint(factory)
        return final
    except BaseException as error:
        if prepared is None and stage=='admission':
            observed=getattr(error,'claim_consumed',None)
            consumed=observed if type(observed) is bool else None
        if getattr(error,'directory_created',False) is True: archive_status='incomplete'
        for item in (owner,prepared):
            if item is not None:
                try: item.abort()
                except Exception: pass
        code='campaign_invocation_'+stage+'_failed' if stage!='inputs' else 'campaign_invocation_inputs_invalid'
        try:
            value=_failure_value(code,consumed=consumed,identifier=identifier,factory=factory,
                archive=archive_status,receipt=receipt_status,closed=closed)
        except Exception:
            value=_failure_value('campaign_invocation_unknown')
        error.__traceback__=error.__cause__=error.__context__=None
        return _serialize_summary(value)


def _decode_packet(payload,profile):
    from .admission import TimedAdmissionInputs
    from researchops_external_closure.types import PreReceiptDocumentBytes
    value=decode_strict_json_object(payload,max_bytes=profile['limits']['packet_bytes'])
    spec=profile['cli']; limit=profile['limits']
    if (set(value)!=set(spec['packet_fields']) or value['schema_version']!=spec['packet_schema_version']
        or type(value['documents']) is not dict or set(value['documents'])!=set(spec['document_fields'])
        or type(value['admission']) is not dict or set(value['admission'])!=set(spec['admission_fields'])):
        raise CampaignInvocationError('campaign_invocation_inputs_invalid')
    def text_bytes(text,maximum):
        if type(text) is not str or len(text)>maximum:
            raise CampaignInvocationError('campaign_invocation_inputs_invalid')
        result=text.encode('utf-8')
        if len(result)>maximum: raise CampaignInvocationError('campaign_invocation_inputs_invalid')
        return result
    def path(text):
        if type(text) is not str or len(text)>limit['path_characters'] or '\0' in text:
            raise CampaignInvocationError('campaign_invocation_inputs_invalid')
        return Path(text)
    documents=PreReceiptDocumentBytes(**{name:text_bytes(value['documents'][name],limit['public_document_bytes']) for name in spec['document_fields']})
    admission=dict(value['admission']); admission['artifact_directory']=path(admission['artifact_directory'])
    for name in spec['admission_utf8_document_fields']:
        admission[name]=text_bytes(admission[name],limit['public_document_bytes'])
    result=dict(documents=documents,external_observation_bundle=text_bytes(value['external_observation_bundle'],limit['public_document_bytes']),
        timing_plan_bytes=text_bytes(value['timing_plan'],limit['timing_plan_bytes']),admission_inputs=TimedAdmissionInputs(**admission),
        task_package_path=path(value['task_package_path']))
    _bounded_inputs(profile,result['documents'],result['external_observation_bundle'],result['timing_plan_bytes'],result['admission_inputs'],result['task_package_path'])
    return result


class _Parser(argparse.ArgumentParser):
    def error(self,message): raise CampaignInvocationError('campaign_invocation_inputs_invalid')


def main(argv=None):
    return _main(argv, implementation_version=4)


def main_v5(argv=None):
    return _main(argv, implementation_version=5)


def _main(argv, *, implementation_version):
    if type(implementation_version) is not int or implementation_version not in (4, 5):
        raise CampaignInvocationError('campaign_invocation_inputs_invalid')
    args=list(sys.argv[1:] if argv is None else argv)
    if '--confirm-online' not in args:
        result=_not_confirmed_summary()
    else:
        execution_entered=False
        try:
            parser=_Parser(description='Execute one separately authorized campaign; no implicit Key or model permission.',allow_abbrev=False)
            parser.add_argument('--packet',required=True); parser.add_argument('--confirm-online',action='store_true')
            parsed=parser.parse_args(args)
            profile=load_invocation_contract()
            if len(parsed.packet)>profile['limits']['path_characters']:
                raise CampaignInvocationError('campaign_invocation_inputs_invalid')
            packet=read_regular_file_no_follow(Path(parsed.packet),max_bytes=profile['limits']['packet_bytes'])
            arguments=_decode_packet(packet,profile)
            execution_entered=True
            invoke = run_timed_campaign if implementation_version == 4 else run_timed_campaign_v5
            result=asyncio.run(invoke(**arguments,confirm_online=parsed.confirm_online))
        except SystemExit as error:
            if error.code==0 and not execution_entered: return 0
            result=_serialize_summary(_failure_value('campaign_invocation_unknown' if execution_entered else 'campaign_invocation_inputs_invalid',
                                                     consumed=None if execution_entered else False))
        except BaseException as error:
            result=_serialize_summary(_failure_value('campaign_invocation_unknown' if execution_entered else 'campaign_invocation_inputs_invalid',
                                                     consumed=None if execution_entered else False))
            error.__traceback__=error.__cause__=error.__context__=None
    try: sys.stdout.write(result.decode('utf-8'))
    except BaseException: return 5
    return {'completed':0,'not_confirmed':2,'failed':4,'outcome_unknown':5}.get(json.loads(result)['status'],5)


if __name__=='__main__': raise SystemExit(main())
