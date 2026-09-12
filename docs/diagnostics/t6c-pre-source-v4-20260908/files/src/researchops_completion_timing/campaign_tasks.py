"""One consumed-owner private package read; no Key loader or Provider factory."""
from __future__ import annotations

import base64
import re
import threading
from pathlib import Path

from researchops_external_closure.execution_current_v3 import verify_current_timed_profile
from researchops_external_closure.execution_local_v3 import _head
from researchops_external_closure.io import read_regular_file_no_follow
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object
from .campaign_start import ROOT, _ClaimedCampaignStart
from .campaign_opening import _verify_private_task_opening
from .contract import digest
from .process_environment import verify_process_environment
from . import local_claim

PROFILE_PATH = 'evals/provider_completion_task_release_v1/contract_v1.json'
PROFILE_SHA256 = 'd580b165eee9809cf88899457ea7476dd61d2be074149f1f8789c2a3724c9eb4'


class CampaignTaskReleaseError(ValueError):
    def __init__(self, code):
        self.code = code
        self.retry_authorized = False
        super().__init__(code)


def _profile():
    payload = read_regular_file_no_follow(ROOT / PROFILE_PATH, max_bytes=8192)
    if digest(payload) != PROFILE_SHA256:
        raise CampaignTaskReleaseError('campaign_task_release_profile_invalid')
    return decode_strict_json_object(payload, max_bytes=8192)


class _CampaignTaskOwner:
    def __init__(self, prepared):
        if type(prepared) is not _ClaimedCampaignStart:
            raise CampaignTaskReleaseError('campaign_task_release_preparation_required')
        self._prepared = prepared
        self._lock = threading.Lock()
        self._attempted = self._failed = False
        self._runner_taken = False
        self._opened = None
        try:
            prepared._take_for_runtime()
            self._profile = _profile()
            self._check_current()
        except BaseException:
            self._failed = True; prepared.abort(); raise

    def _check_current(self):
        prepared = self._prepared
        prepared._assert_process()
        prepared._verified_envelope()
        if self._failed:
            raise CampaignTaskReleaseError('campaign_task_release_halted')
        _stamp, deadline = prepared._validity.checkpoint(reserve_ns=prepared._sealing_reserve_ns)
        prepared.clock._tighten_phase_deadline(deadline)
        prepared.clock.remaining_phase_ns()
        current = verify_current_timed_profile(ROOT, profile='campaign')
        expected = prepared.source_identity
        if (current.source_integrity_commitment_sha256 != expected.source_integrity_commitment_sha256
            or current.implementation_commitment_sha256 != expected.source_manifest_commitment_sha256
            or _head(ROOT) != expected.execution_commit):
            raise CampaignTaskReleaseError('campaign_task_release_source_changed')
        verify_process_environment(ROOT, expected_dependency_lock_sha256=current.component_hashes['dependency_lock_sha256'],
            expected_pyproject_sha256=current.component_hashes['pyproject_sha256'])
        if local_claim.read_reserved_local_claim(prepared.request_bytes, expected_receipt_sha256=digest(prepared.receipt_bytes)) != prepared.receipt_bytes:
            raise CampaignTaskReleaseError('campaign_task_release_claim_changed')
        _stamp, deadline = prepared._validity.checkpoint(reserve_ns=prepared._sealing_reserve_ns)
        prepared.clock._tighten_phase_deadline(deadline)
        prepared.clock.remaining_phase_ns()

    def open_tasks(self, package_path):
        """Read custodian-owned files only after consumed ownership; never write."""
        with self._lock:
            payload = package = salt = None
            try:
                if self._attempted:
                    raise CampaignTaskReleaseError('campaign_task_release_already_attempted')
                self._attempted = True
                self._check_current()
                if (type(package_path) is not type(Path()) or not package_path.is_absolute() or '..' in package_path.parts
                    or re.fullmatch(r'[A-Za-z]:', package_path.drive) is None
                    or any(':' in component for component in package_path.parts[1:])
                    or local_claim._kernel().GetDriveTypeW(package_path.anchor) != 3):
                    raise CampaignTaskReleaseError('campaign_task_release_path_invalid')
                with local_claim._locked_chain(package_path.parent):
                    # Earliest possible exposure boundary, not a claim that a
                    # failed file read actually produced valid task contents.
                    self._prepared.clock.task_released()
                    payload = read_regular_file_no_follow(package_path, max_bytes=self._profile['maximum_package_bytes'])
                    package = decode_strict_json_object(payload, max_bytes=self._profile['maximum_package_bytes'])
                    if (set(package) != set(self._profile['private_package_fields'])
                        or package['schema_version'] != self._profile['private_package_schema_version']
                        or type(package['bundle']) is not dict or type(package['secret_salt_b64']) is not str
                        or len(package['secret_salt_b64']) > 4 * ((self._profile['maximum_salt_bytes'] + 2) // 3)
                        or type(package['seen_task_digests']) is not list
                        or not 1 <= len(package['seen_task_digests']) <= self._profile['maximum_seen_task_digests']):
                        raise CampaignTaskReleaseError('campaign_task_release_package_invalid')
                    salt = base64.b64decode(package['secret_salt_b64'], validate=True)
                    if (not self._profile['minimum_salt_bytes'] <= len(salt) <= self._profile['maximum_salt_bytes']
                        or base64.b64encode(salt).decode('ascii') != package['secret_salt_b64']):
                        raise CampaignTaskReleaseError('campaign_task_release_salt_invalid')
                    opened = _verify_private_task_opening(ROOT, bundle_bytes=raw(package['bundle']), secret_salt=salt,
                        seen_task_digests=tuple(package['seen_task_digests']), envelope_bytes=self._prepared.envelope_bytes)
                    self._check_current()
                    if read_regular_file_no_follow(package_path, max_bytes=self._profile['maximum_package_bytes']) != payload:
                        raise CampaignTaskReleaseError('campaign_task_release_package_changed')
                    self._opened = opened
                self._check_current()
                return self.summary()
            except BaseException as error:
                self._opened = None; self._failed = True; self._prepared.abort()
                if isinstance(error, (KeyboardInterrupt, SystemExit)): raise
                code = vars(error).get('code')
                if type(code) is not str or re.fullmatch(r'campaign_(?:opening|task_release)_[a-z_]+', code) is None:
                    code = 'campaign_task_release_failed'
                raise CampaignTaskReleaseError(code) from None
            finally:
                payload = package = salt = None  # Owned references only, not a RAM wipe.

    def summary(self):
        self._prepared._assert_process()
        return dict(status='campaign_task_opening_verified' if self._opened is not None else 'campaign_task_opening_not_verified',
            private_read_attempted=self._attempted, opening_verified=self._opened is not None, failed=self._failed,
            case_count=None if self._opened is None else len(self._opened._cases), key_loaded=False, provider_calls=0,
            runtime_authority_granted=False, closure_claim_allowed=False, retry_authorized=False)

    def abort(self):
        with self._lock:
            self._failed = True; self._opened = None; self._prepared.abort()

    def _take_for_runner(self):
        with self._lock:
            try:
                if self._runner_taken or self._failed or self._opened is None:
                    raise CampaignTaskReleaseError('campaign_task_release_runner_unavailable')
                self._runner_taken = True
                self._check_current()
                return self._opened
            except BaseException:
                self._failed = True; self._opened = None; self._prepared.abort()
                raise

    def __repr__(self): return '<campaign task owner; private contents withheld>'
    def __copy__(self): raise TypeError('task owner cannot be copied')
    def __deepcopy__(self, memo): raise TypeError('task owner cannot be copied')
    def __reduce_ex__(self, protocol): raise TypeError('task owner cannot be serialized')
