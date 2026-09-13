"""Private create-once seven-file publisher, not an external closure receipt."""
from __future__ import annotations

import os
from pathlib import Path

from researchops_external_closure.execution_current_v3 import verify_current_timed_profile
from researchops_external_closure.execution_local_v3 import _head
from researchops_external_closure.io import scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object
from .artifacts import PROFILE_SHA256, load_artifact_contract, verify_timing_artifact_bundle
from .campaign_artifacts import _build_completed_campaign_payloads
from .campaign_runtime import ROOT, _CampaignModelFactory, _CampaignModelFactoryV5
from .contract import CONTRACT_SHA256, commitment, digest
from .first_live_audit import _database_snapshot
from .first_live_publish import _write_exclusive
from .ledger_event import LEDGER_CONTRACT_SHA256
from .local_claim import _locked_chain, read_reserved_local_claim, _kernel
from .process_environment import verify_process_environment


class CampaignPublishError(ValueError):
    def __init__(self, code, *, directory_created=False, created_files=()):
        self.code = code
        self.directory_created = directory_created
        self.created_files = tuple(created_files)
        self.observation_scope = 'this_publish_invocation_only'
        self.retry_authorized = False
        super().__init__(code)


def _publication_checkpoint(factory):
    prepared = factory._prepared
    prepared._assert_process()
    stamp, _deadline = prepared._validity.checkpoint(reserve_ns=0)
    observed = prepared.clock._sealing_offset()
    if (factory._sealing_started_ns is None or factory._sealing_timeout_ns is None
        or observed - factory._sealing_started_ns > factory._sealing_timeout_ns):
        raise CampaignPublishError('campaign_publish_timeout')
    return stamp, observed


def _check_publication_identity(factory):
    """Closed phase cannot use the pre-send checker that tightens an open clock."""
    _publication_checkpoint(factory)
    prepared = factory._prepared
    envelope = prepared._verified_envelope()
    expected = prepared.source_identity
    reader = verify_current_timed_profile
    if type(factory) is _CampaignModelFactoryV5:
        from researchops_external_closure.execution_current_v4 import verify_current_timed_profile as reader
        binding = envelope['execution_binding']
        if (expected.source_recipe_version != 4 or expected.profile != 'campaign'
            or binding['execution_commit'] != expected.execution_commit
            or binding['execution_tree'] != expected.execution_tree
            or binding['source_integrity_commitment_sha256'] != expected.source_integrity_commitment_sha256
            or binding['implementation_commitment_sha256'] != expected.source_manifest_commitment_sha256):
            raise CampaignPublishError('campaign_publish_source_changed')
    current = reader(ROOT, profile='campaign')
    if (current.source_integrity_commitment_sha256 != expected.source_integrity_commitment_sha256
        or current.implementation_commitment_sha256 != expected.source_manifest_commitment_sha256
        or _head(ROOT) != expected.execution_commit):
        raise CampaignPublishError('campaign_publish_source_changed')
    verify_process_environment(ROOT, expected_dependency_lock_sha256=current.component_hashes['dependency_lock_sha256'],
                               expected_pyproject_sha256=current.component_hashes['pyproject_sha256'])
    if read_reserved_local_claim(prepared.request_bytes, expected_receipt_sha256=digest(prepared.receipt_bytes)) != prepared.receipt_bytes:
        raise CampaignPublishError('campaign_publish_claim_changed')
    _publication_checkpoint(factory)


def _publish_completed_campaign(factory, directory):
    return _publish_completed_campaign_impl(factory, directory, implementation_version=4)


def _publish_completed_campaign_v5(factory, directory):
    return _publish_completed_campaign_impl(factory, directory, implementation_version=5)


def _publish_completed_campaign_impl(factory, directory, *, implementation_version):
    """Exact existing profile; partial output is retained and cannot be retried.

    This private destination argument is not a public CLI/path override. A future
    public owner must derive the fixed work/archive names and hold work handles.
    """
    made = False; created = []
    try:
        if type(implementation_version) is not int or implementation_version not in (4, 5):
            raise CampaignPublishError('campaign_publish_factory_required')
        expected = _CampaignModelFactory if implementation_version == 4 else _CampaignModelFactoryV5
        if type(factory) is not expected:
            raise CampaignPublishError('campaign_publish_factory_required')
        if factory._publication_started:
            raise CampaignPublishError('campaign_publish_already_started')
        factory._publication_started = True  # Even preflight failure consumes it.
        prepared = factory._prepared
        state = prepared.clock.snapshot()
        if not factory._phase_finished or factory._failed or not state['closed'] or state['halted']:
            raise CampaignPublishError('campaign_publish_phase_incomplete')
        plan = decode_strict_json_object(prepared.plan_bytes, max_bytes=131072)
        if (plan['plan_commitment_sha256'] != commitment('plan', plan)
            or plan['plan_commitment_sha256'] != prepared.proof.scope.timing.timing_plan_commitment_sha256
            or type(plan['artifact_sealing_timeout_ns']) is not int
            or not 0 < plan['artifact_sealing_timeout_ns'] <= 300_000_000_000):
            raise CampaignPublishError('campaign_publish_plan_invalid')
        factory._sealing_started_ns = prepared.clock._sealing_offset()
        factory._sealing_timeout_ns = plan['artifact_sealing_timeout_ns']
        _publication_checkpoint(factory)
        if (type(directory) is not type(Path()) or not directory.is_absolute() or '..' in directory.parts
            or os.name != 'nt' or len(directory.drive) != 2 or directory.drive[1] != ':'
            or any(':' in part for part in directory.parts[1:]) or _kernel().GetDriveTypeW(directory.anchor) != 3):
            raise CampaignPublishError('campaign_publish_destination_invalid')
        _check_publication_identity(factory)
        profile, _ = load_artifact_contract(ROOT)
        payloads = _build_completed_campaign_payloads(factory)
        evidence = decode_strict_json_object(payloads['completion_timing_evidence.json'], max_bytes=100_000_000)
        manifest = dict(schema_version='provider-completion-timed-bundle-manifest/1.0', status='sealed_payloads',
            campaign_id=plan['binding']['campaign_id'], artifact_contract_sha256=PROFILE_SHA256,
            timing_contract_sha256=CONTRACT_SHA256, timed_ledger_contract_sha256=LEDGER_CONTRACT_SHA256,
            plan_commitment_sha256=plan['plan_commitment_sha256'], execution_binding_sha256=evidence['execution_binding_sha256'],
            files={name:dict(bytes=len(value),sha256=digest(value)) for name,value in payloads.items()},
            manifest_self_hash_included=False, raw_provider_content_persisted=False, task_content_persisted=False,
            api_key_persisted=False, online_execution_authorized=False)
        payloads[profile['manifest_file']] = raw(manifest)
        canaries = (factory._opening._canary_b64.encode(),)
        def bounded():
            if (any(len(value) > profile['max_file_bytes'] for value in payloads.values())
                or sum(map(len,payloads.values())) > profile['max_total_bytes']):
                raise CampaignPublishError('campaign_publish_byte_limit')
            scan_public_artifact_bytes(tuple(payloads.values()), sensitive_canaries=canaries)
        bounded()
        with _locked_chain(directory.parent):
            _publication_checkpoint(factory)
            directory.mkdir(exist_ok=False); made = True
            with _locked_chain(directory):
                for name,value in payloads.items():
                    _publication_checkpoint(factory)
                    _write_exclusive(directory / name,value,created)
                _stamp, ended = _publication_checkpoint(factory)
                publication = dict(schema_version='provider-completion-timing-publication/1.0', document_type='completion_timing_publication',
                    timing_contract_sha256=CONTRACT_SHA256, plan_commitment_sha256=plan['plan_commitment_sha256'],
                    execution_binding_sha256=evidence['execution_binding_sha256'], clock_domain_id=prepared.clock_domain_id,
                    timing_evidence_file_sha256=digest(payloads['completion_timing_evidence.json']),
                    bundle_manifest_file_sha256=digest(payloads[profile['manifest_file']]),
                    sealing_started_ns=factory._sealing_started_ns, sealing_finished_ns=ended, sealing_status='sealed',
                    sealing_scope='bounded_artifact_set_before_publication_receipt', online_execution_authorized=False)
                publication['publication_commitment_sha256'] = commitment('publication',publication)
                payloads[profile['publication_file']] = raw(publication); bounded()
                _publication_checkpoint(factory)
                _write_exclusive(directory / profile['publication_file'],payloads[profile['publication_file']],created)
                _publication_checkpoint(factory)
                envelope = prepared._verified_envelope()
                run_ids = {case:'PCERUN-'+digest(b'researchops-provider-completion-audit-run-id-v1\0'
                    +envelope['envelope_id'].encode()+b'\0'+case.encode())[:32].upper() for case in plan['planned_case_handles']}
                checked = verify_timing_artifact_bundle(ROOT,directory,
                    expected_manifest_sha256=digest(payloads[profile['manifest_file']]),
                    expected_plan_commitment_sha256=prepared.proof.scope.timing.timing_plan_commitment_sha256,
                    expected_execution_binding_sha256=prepared.proof.scope.timing.timing_execution_binding_sha256,
                    expected_denominator_plan_bytes=raw(envelope['runtime_plan']['denominator_plan']),
                    expected_case_run_ids=run_ids,sensitive_canaries=canaries)
                if (not checked['timing_constraints_complete'] or not checked['audit_hash_chains_verified']
                    or _database_snapshot(factory._ledger.database_path,profile['max_file_bytes']) != payloads['phase6_audit.sqlite3']):
                    raise CampaignPublishError('campaign_publish_verification_failed')
                _check_publication_identity(factory)
        _publication_checkpoint(factory)
        factory._publication_finished = True
        return dict(status='campaign_archive_written_and_verified',created_files=tuple(created),
            manifest_file_sha256=digest(payloads[profile['manifest_file']]),
            evidence_file_sha256=digest(payloads['completion_timing_evidence.json']),
            publication_self_write_measured=False, closure_claim_allowed=False,
            runtime_authority_granted=False, provider_bill_cny=None, retry_authorized=False)
    except BaseException as error:
        if type(factory) in (_CampaignModelFactory, _CampaignModelFactoryV5): factory._abort()
        if isinstance(error,(KeyboardInterrupt,SystemExit)): raise
        code = error.code if type(error) is CampaignPublishError else (
            'campaign_publish_destination_exists' if isinstance(error,FileExistsError) else 'campaign_publish_failed')
        raise CampaignPublishError(code,directory_created=made,created_files=created) from None
