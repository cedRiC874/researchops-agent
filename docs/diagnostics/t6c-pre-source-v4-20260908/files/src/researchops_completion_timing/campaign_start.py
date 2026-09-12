"""Process-bound campaign preparation and one-shot ownership; no task/Key IO."""
from __future__ import annotations

import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from researchops_external_closure.execution_binding import _current_file
from researchops_external_closure.execution_components_v3 import PROFILE_PATHS
from researchops_external_closure.execution_current_v3 import verify_current_timed_profile
from researchops_external_closure.execution_local_v3 import verify_local_timed_execution_identity, _head
from researchops_external_closure.primitives import decode_strict_json_object, parse_utc_timestamp
from .campaign_admission import verify_campaign_prerequisites
from .clock import _TimingClock
from .contract import digest
from .execution_scope import build_scoped_claim_request, verify_scoped_store_identity
from .first_live_control import _duration
from .process_environment import verify_process_environment
from . import local_claim

ROOT = Path(__file__).resolve().parents[2]
_TOKEN = object()


class CampaignStartError(ValueError):
    def __init__(self, code, *, claim_consumed):
        self.code, self.claim_consumed = code, claim_consumed
        self.retry_authorized = False
        super().__init__(code)


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class _CampaignValidity:
    """Signed UTC validity, not first-live's fixed 330-second whole budget."""
    def __init__(self):
        self.last_ns = self.last_utc = None
        self.expires = None
        self._expiry_deadline = None
        self.started_at, _ = self.checkpoint()
        self._started_ns = self.last_ns

    def checkpoint(self, *, reserve_ns=0):
        now = time.monotonic_ns()  # Sample before UTC for conservative translation.
        stamp = _utc_now(); utc = parse_utc_timestamp(stamp)
        if (type(now) is not int or now < 0 or type(reserve_ns) is not int or not 0 <= reserve_ns <= 300_000_000_000
            or (self.last_ns is not None and now < self.last_ns) or (self.last_utc is not None and utc < self.last_utc)):
            raise ValueError('campaign_start_clock_invalid')
        self.last_ns, self.last_utc = now, utc
        if self.expires is None:
            return stamp, None
        remaining = _duration(utc, parse_utc_timestamp(self.expires)) * 1_000_000_000
        initial = _duration(parse_utc_timestamp(self.started_at), parse_utc_timestamp(self.expires)) * 1_000_000_000
        # Binding expiry after slow verification must still count all elapsed
        # preparation. A stalled UTC sample cannot grant a fresh validity window.
        translated = min(self._started_ns + initial.numerator // initial.denominator,
                         now + remaining.numerator // remaining.denominator)
        self._expiry_deadline = translated if self._expiry_deadline is None else min(self._expiry_deadline, translated)
        deadline = self._expiry_deadline - reserve_ns
        if deadline <= now:
            raise ValueError('campaign_start_authorization_expired')
        return stamp, deadline


class _ClaimedCampaignStart:
    __slots__ = ('_clock', '_validity', '_pid', '_taken', '_lock', 'proof', 'source_identity', 'environment',
                 'clock_domain_id', 'request_bytes', 'receipt_bytes', 'plan_bytes', 'envelope_bytes', '_envelope_bytes_sha256', '_sealing_reserve_ns')

    def __init__(self, token, *, clock, validity, proof, identity, environment, domain, request, receipt, plan_bytes, envelope_bytes, sealing):
        if token is not _TOKEN:
            raise TypeError('campaign start requires checked preparation')
        self._clock, self._validity = clock, validity
        self._pid, self._taken, self._lock = os.getpid(), False, threading.Lock()
        self.proof, self.source_identity, self.environment = proof, identity, environment
        self.clock_domain_id, self.request_bytes, self.receipt_bytes, self.plan_bytes = domain, request, receipt, plan_bytes
        self.envelope_bytes = envelope_bytes
        self._envelope_bytes_sha256 = digest(envelope_bytes)
        self._sealing_reserve_ns = sealing

    def _assert_process(self):
        if os.getpid() != self._pid:
            raise CampaignStartError('campaign_start_wrong_process', claim_consumed=True)

    def _verified_envelope(self):
        self._assert_process()
        if type(self.envelope_bytes) is not bytes or digest(self.envelope_bytes) != self._envelope_bytes_sha256:
            raise CampaignStartError('campaign_start_envelope_changed', claim_consumed=True)
        return decode_strict_json_object(self.envelope_bytes, max_bytes=2_000_000)

    @property
    def clock(self):
        self._assert_process(); return self._clock

    def _take_for_runtime(self):
        self._assert_process()
        with self._lock:
            if self._taken:
                self._clock.abort_new_work()
                raise CampaignStartError('campaign_start_already_taken', claim_consumed=True)
            self._taken = True
            try:
                self._verified_envelope()
                _stamp, deadline = self._validity.checkpoint(reserve_ns=self._sealing_reserve_ns)
                self._clock._tighten_phase_deadline(deadline)
            except BaseException:
                self._clock.abort_new_work(); raise

    def abort(self):
        self._assert_process()
        with self._lock:
            self._taken = True; self._clock.abort_new_work()

    def summary(self):
        self._assert_process()
        return dict(status='campaign_prepared_before_task_and_key', claim_consumed=True,
            execution_commit=self.source_identity.execution_commit, execution_environment_id=self.proof.scope.execution_environment_id,
            local_claim_receipt_sha256=digest(self.receipt_bytes), source_and_head_matched=True,
            loaded_project_origins_matched=self.environment['loaded_project_origins_matched'],
            locked_distribution_versions_matched=self.environment['locked_distribution_versions_matched'],
            loaded_bytecode_integrity_verified=False, dependency_binary_integrity_verified=False,
            task_released=False, key_loaded=False, provider_calls=0, runtime_authority_granted=False, retry_authorized=False,
            closure_claim_allowed=False)

    def __copy__(self): raise TypeError('campaign start cannot be copied')
    def __deepcopy__(self, memo): raise TypeError('campaign start cannot be copied')
    def __reduce_ex__(self, protocol): raise TypeError('campaign start cannot be serialized')


def _prepare_process_bound_campaign_start(root, documents, *, external_observation_bundle, timing_plan_bytes, admission_inputs):
    consumed = False; winner = clock = None
    try:
        if not isinstance(root, Path) or root.resolve(strict=True) != ROOT:
            raise ValueError('campaign_start_process_root_mismatch')
        validity = _CampaignValidity()
        source_plan = decode_strict_json_object(_current_file(root, PROFILE_PATHS['campaign'][1]), max_bytes=2_000_000)
        hashes = source_plan['component_hashes']
        expected_environment = dict(expected_dependency_lock_sha256=hashes['dependency_lock_sha256'], expected_pyproject_sha256=hashes['pyproject_sha256'])
        verify_process_environment(root, **expected_environment)
        proof = verify_campaign_prerequisites(root, documents, external_observation_bundle=external_observation_bundle,
            timing_plan_bytes=timing_plan_bytes, admission_inputs=admission_inputs, verification_time_utc=validity.started_at)
        validity.expires = proof.expires_at_utc
        envelope = decode_strict_json_object(documents.preregistration_envelope, max_bytes=2_000_000)
        execution = envelope['execution_binding']
        plan = decode_strict_json_object(timing_plan_bytes, max_bytes=131072)
        sealing = plan['artifact_sealing_timeout_ns']
        validity.checkpoint(reserve_ns=sealing)
        identity = verify_local_timed_execution_identity(root, profile='campaign', expected_commit=execution['execution_commit'],
            expected_tree=execution['execution_tree'], expected_source_integrity_commitment_sha256=execution['source_integrity_commitment_sha256'],
            expected_source_manifest_commitment_sha256=execution['implementation_commitment_sha256'])
        environment = verify_process_environment(root, **expected_environment)
        verify_scoped_store_identity(proof.scope)
        validity.checkpoint(reserve_ns=sealing)
        domain = 'PCECLOCK-' + secrets.token_hex(16).upper()
        request = build_scoped_claim_request(proof.scope, clock_domain_id=domain)
        winner = local_claim._reserve_with_ownership(request); consumed = True
        receipt = winner._take()
        if local_claim.read_reserved_local_claim(request, expected_receipt_sha256=digest(receipt)) != receipt:
            raise ValueError('campaign_start_claim_changed')
        claimed = parse_utc_timestamp(decode_strict_json_object(receipt, max_bytes=4096)['claimed_at_utc'])
        now, _ = validity.checkpoint(reserve_ns=sealing)
        if not parse_utc_timestamp(validity.started_at) <= claimed <= parse_utc_timestamp(now):
            raise ValueError('campaign_start_claim_time_invalid')
        # Only now create the campaign origin; no task bytes or Key have opened.
        clock = _TimingClock(request_timeout_ns=plan['request_timeout_ns'], phase_timeout_ns=plan['phase_timeout_ns'])
        _stamp, deadline = validity.checkpoint(reserve_ns=sealing); clock._tighten_phase_deadline(deadline)
        current = verify_current_timed_profile(root, profile='campaign')
        if (current.source_integrity_commitment_sha256 != identity.source_integrity_commitment_sha256
            or current.implementation_commitment_sha256 != identity.source_manifest_commitment_sha256
            or _head(root) != identity.execution_commit):
            raise ValueError('campaign_start_post_claim_source_changed')
        environment = verify_process_environment(root, **expected_environment)
        _stamp, deadline = validity.checkpoint(reserve_ns=sealing); clock._tighten_phase_deadline(deadline)
        return _ClaimedCampaignStart(_TOKEN, clock=clock, validity=validity, proof=proof, identity=identity, environment=environment,
            domain=domain, request=request, receipt=receipt, plan_bytes=timing_plan_bytes,
            envelope_bytes=documents.preregistration_envelope, sealing=sealing)
    except BaseException as error:
        consumed = consumed or bool(getattr(error, 'claim_may_exist', False))
        if winner is not None: winner._invalidate()
        if clock is not None: clock.abort_new_work()
        if isinstance(error, (KeyboardInterrupt, SystemExit)): raise
        code = getattr(error, 'code', None)
        if code is None and type(error) is ValueError and re.fullmatch(r'campaign_start_[a-z_]+', str(error)):
            code = str(error)
        if type(code) is not str or re.fullmatch(r'[a-z0-9_]{1,128}', code) is None:
            code = 'campaign_start_failed'
        raise CampaignStartError(code, claim_consumed=consumed) from None
