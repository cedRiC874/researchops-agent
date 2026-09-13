"""Custodian-memory task opening consistency; never consent or task-release IO."""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from types import MappingProxyType

from researchops.provider_completion_external_contract import load_frozen_external_preregistration_contract, DESIGN_CONTRACT_RELATIVE_PATH
from researchops_external_closure.documents import _decode_schema_document
from researchops_external_closure.io import read_regular_file_no_follow, scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object
from .contract import digest, fail


@dataclass(frozen=True, slots=True, repr=False)
class _OpenedCampaignTasks:
    _cases: tuple = field(repr=False)
    _canary: bytes = field(repr=False)
    _canary_b64: str = field(repr=False)
    bundle_commitment_sha256: str
    execution_input_commitments: tuple[str, ...]
    execution_input_order_commitment_sha256: str

    def __repr__(self): return '<private campaign task opening; contents withheld>'
    def __copy__(self): raise TypeError('private opening cannot be copied')
    def __deepcopy__(self, memo): raise TypeError('private opening cannot be copied')
    def __reduce_ex__(self, protocol): raise TypeError('private opening cannot be serialized')

    def summary(self):
        return dict(status='private_opening_consistent', case_count=len(self._cases),
            bundle_commitment_sha256=self.bundle_commitment_sha256,
            execution_input_order_commitment_sha256=self.execution_input_order_commitment_sha256,
            exact_overlap_count=0, seen_inventory_completeness_verified=False, semantic_overlap_excluded=False,
            canary_randomness_independently_verified=False, authorization_verified=False,
            task_release_order_verified=False, runtime_authority_granted=False, closure_claim_allowed=False)


def _verify_private_task_opening(root, *, bundle_bytes, secret_salt, seen_task_digests, envelope_bytes):
    """Caller must enforce consumed ownership BEFORE obtaining these private bytes.

    This checks commitments against supplied envelope bytes, not its signatures.
    It reads only public frozen contracts; no Key, task file, store, ledger, clock,
    network or Provider is opened. No individual unsalted task digest is returned.
    """
    try:
        if type(secret_salt) is not bytes or not 32 <= len(secret_salt) <= 0xFFFFFFFF:
            fail('campaign_opening_salt_invalid')
        if type(seen_task_digests) is not tuple or not 1 <= len(seen_task_digests) <= 1_000_000:
            fail('campaign_opening_seen_set_invalid')
        if any(type(item) is not str or re.fullmatch(r'(?!0{64}$)[0-9a-f]{64}', item) is None for item in seen_task_digests):
            fail('campaign_opening_seen_set_invalid')
        seen = set(seen_task_digests)
        if len(seen) != len(seen_task_digests): fail('campaign_opening_seen_set_invalid')
        frozen = load_frozen_external_preregistration_contract(root)
        design_bytes = read_regular_file_no_follow(root / DESIGN_CONTRACT_RELATIVE_PATH, max_bytes=262144)
        if digest(design_bytes) != frozen['design_contract']['file_sha256']:
            fail('campaign_opening_contract_changed')
        domains = decode_strict_json_object(design_bytes, max_bytes=262144)['canonicalization_and_domains']
        bundle = _decode_schema_document(bundle_bytes, schemas=frozen['schemas'], schema_name='unseen_case_bundle_v1.schema.json',
            schema_error='campaign_opening_bundle_invalid')
        canonical = raw(bundle)
        scan_public_artifact_bytes((canonical,))
        envelope = decode_strict_json_object(envelope_bytes, max_bytes=2_000_000)
        custody, execution = envelope['task_custody'], envelope['execution_binding']
        denominator = envelope['runtime_plan']['denominator_plan']
        def hashed(name, value, *, salted=False):
            state = hashlib.sha256(); state.update(domains[name].encode('utf-8')); state.update(b'\0')
            if salted:
                state.update(len(secret_salt).to_bytes(4, 'big')); state.update(secret_salt)
            state.update(raw(value)); return state.hexdigest()
        for value in (bundle['candidate_freeze_receipt_sha256'], bundle['seen_case_exclusion_manifest_sha256'],
                      execution['runner_config_commitment_sha256']):
            if type(value) is not str or re.fullmatch(r'(?!0{64}$)[0-9a-f]{64}', value) is None:
                fail('campaign_opening_binding_invalid')
        if (re.fullmatch(r'PCEBUNDLE-[A-F0-9]{32}', bundle['bundle_id']) is None
            or bundle['candidate_freeze_receipt_sha256'] != execution['candidate_freeze_receipt_sha256']
            or bundle['seen_case_exclusion_manifest_sha256'] != custody['seen_task_exclusion_manifest_sha256']):
            fail('campaign_opening_binding_invalid')
        bundle_hash = hashed('task_bundle_domain', bundle, salted=True)
        if bundle_hash != custody['task_bundle_commitment_sha256']:
            fail('campaign_opening_bundle_commitment_mismatch')
        if (type(custody['seen_task_exclusion_set_count']) is not int or len(seen) != custody['seen_task_exclusion_set_count']
            or hashed('seen_task_exclusion_set_domain', sorted(seen)) != custody['seen_task_exclusion_set_commitment_sha256']):
            fail('campaign_opening_seen_commitment_mismatch')
        canary = base64.b64decode(bundle['leak_canary_b64'], validate=True)
        if len(canary) != 32 or base64.b64encode(canary).decode('ascii') != bundle['leak_canary_b64']:
            fail('campaign_opening_canary_invalid')
        cases = bundle['cases']; handles = [case['case_handle'] for case in cases]
        if (type(custody['planned_case_count']) is not int or len(cases) != custody['planned_case_count'] or handles != denominator['case_ids']
            or len(set(handles)) != len(handles) or any(re.fullmatch(r'PCECASE-[A-F0-9]{32}', handle) is None for handle in handles)
            or type(denominator['max_turns_per_case']) is not int
            or any(type(case['max_turns_per_case']) is not int or case['max_turns_per_case'] != denominator['max_turns_per_case'] for case in cases)):
            fail('campaign_opening_case_order_invalid')
        commitments = []
        for case in cases:
            case_digest = hashed('canonical_case_digest_domain', {name: case[name] for name in ('task_schema_version', 'user_input', 'tools')})
            if case_digest in seen: fail('campaign_opening_seen_overlap')
            opening = {name: case[name] for name in ('case_handle', 'task_schema_version', 'user_input', 'max_turns_per_case', 'tools')}
            opening.update(leak_canary_b64=bundle['leak_canary_b64'], runner_config_commitment_sha256=execution['runner_config_commitment_sha256'])
            commitments.append(hashed('execution_input_commitment_domain', opening, salted=True))
        order_hash = hashed('execution_input_order_domain', commitments)
        if order_hash != custody['execution_input_order_commitment_sha256']:
            fail('campaign_opening_execution_order_mismatch')
        # Retain only what the future custodian-side runner needs. Do not retain
        # the salt/raw opening or export individual canonical (unsalted) digests.
        return _OpenedCampaignTasks(tuple(MappingProxyType(dict(case, tools=())) for case in cases), canary, bundle['leak_canary_b64'],
                                    bundle_hash, tuple(commitments), order_hash)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)): raise
        # Never propagate decoder/schema exception context containing task text.
        code = vars(error).get('code') if type(error).__module__.startswith(('researchops.', 'researchops_')) else None
        if type(code) is not str or re.fullmatch(r'campaign_opening_[a-z_]+', code) is None:
            code = 'campaign_opening_invalid'
        fail(code)
