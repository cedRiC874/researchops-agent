"""Private checked first-live preparation; no Key/task release or Provider API."""
from __future__ import annotations

import re
import os
import secrets
import threading
import time
from datetime import datetime, timezone

from researchops_external_closure.execution_binding import _current_file
from researchops_external_closure.execution_components_v3 import PROFILE_PATHS
from researchops_external_closure.execution_current_v3 import verify_current_timed_profile
from researchops_external_closure.execution_local_v3 import verify_local_timed_execution_identity, _head
from researchops_external_closure.primitives import decode_strict_json_object, parse_utc_timestamp
from . import first_live_control as control
from . import local_claim
from .clock import _TimingClock
from .contract import digest
from .first_live_identity import load_timed_implementation_contract
from .process_environment import verify_process_environment
from pathlib import Path


_TOKEN = object()
_WHOLE_NS = 330_000_000_000
_SEAL_NS = 30_000_000_000


class FirstLiveStartError(ValueError):
    def __init__(self, code, *, claim_consumed, origin="unknown"):
        self.code, self.claim_consumed = code, claim_consumed
        self.origin = origin
        self.retry_authorized = False
        super().__init__(code)


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _monotonic_ns():
    value = time.monotonic_ns()
    if type(value) is not int or value < 0:
        raise ValueError("first_live_start_clock_invalid")
    return value


class _StartBudget:
    def __init__(self):
        self.start_ns = self.last_ns = _monotonic_ns()
        self.last_utc = None
        self.deadline_ns = self.start_ns + _WHOLE_NS
        self._artifact_started_ns = None

    def artifact_checkpoint(self, expires_at_utc):
        """One shared sealing interval, including any later failure persistence."""
        result = self.checkpoint(expires_at_utc, reserve_ns=0)
        if self._artifact_started_ns is None:
            self._artifact_started_ns = self.last_ns
        if self.last_ns - self._artifact_started_ns > _SEAL_NS:
            raise ValueError('first_live_start_artifact_budget_exhausted')
        return result

    def checkpoint(self, expires_at_utc=None, *, reserve_ns=_SEAL_NS):
        if type(reserve_ns) is not int or reserve_ns not in (0, _SEAL_NS):
            raise ValueError("first_live_start_budget_invalid")
        # Sampling monotonic before UTC makes the translated UTC deadline
        # conservative by any time spent obtaining the UTC observation.
        now_ns = _monotonic_ns()
        stamp = _utc_now()
        utc = parse_utc_timestamp(stamp)
        if now_ns < self.last_ns or (self.last_utc is not None and utc < self.last_utc):
            raise ValueError("first_live_start_clock_reversed")
        self.last_ns, self.last_utc = now_ns, utc
        deadline = self.deadline_ns
        if expires_at_utc is not None:
            remaining = control._duration(utc, parse_utc_timestamp(expires_at_utc)) * 1_000_000_000
            if remaining <= 0:
                raise ValueError("first_live_start_authorization_expired")
            deadline = min(deadline, now_ns + remaining.numerator // remaining.denominator)
        if now_ns + reserve_ns >= deadline:
            raise ValueError("first_live_start_budget_exhausted")
        return stamp, deadline - reserve_ns


class _ClaimedFirstLiveStart:
    __slots__ = ("_clock", "clock_domain_id", "receipt_bytes", "intent_bytes", "source_identity", "plan_bytes", "_budget", "_pid", "_taken", "_lock", "_expires", "_environment")

    def __init__(self, token, *, clock, clock_domain_id, receipt_bytes, intent_bytes, source_identity, plan_bytes, budget, expires):
        if token is not _TOKEN:
            raise TypeError("first-live start requires the checked fresh-claim path")
        self._clock, self.clock_domain_id = clock, clock_domain_id
        self.receipt_bytes, self.intent_bytes = receipt_bytes, intent_bytes
        self.source_identity, self.plan_bytes, self._budget = source_identity, plan_bytes, budget
        self._pid, self._taken, self._lock, self._expires = os.getpid(), False, threading.Lock(), expires
        self._environment = None

    def _assert_process(self):
        if os.getpid() != self._pid:
            raise FirstLiveStartError("first_live_start_wrong_process", claim_consumed=True)

    @property
    def clock(self):
        self._assert_process()
        return self._clock

    def _take_for_runtime(self):
        self._assert_process()
        with self._lock:
            if self._taken:
                raise FirstLiveStartError("first_live_start_already_taken", claim_consumed=True)
            self._taken = True
            try:
                _now, deadline = self._budget.checkpoint(self._expires)
                self._clock._tighten_phase_deadline(deadline)
            except Exception:
                self._clock.abort_new_work()
                raise FirstLiveStartError("first_live_start_handoff_unavailable", claim_consumed=True) from None
            return self

    def summary(self):
        self._assert_process()
        return {"status": "source_checked_claimed_clock_prepared", "scope": "preparation_checkpoint_only", "claim_consumed": True,
            "claim_receipt_sha256": digest(self.receipt_bytes), "clock_domain_id": self.clock_domain_id,
            "task_released": False, "key_loaded": False, "provider_calls": 0,
            "loaded_project_origins_matched": self._environment is not None,
            "locked_distribution_versions_matched": self._environment is not None,
            "loaded_process_source_verified": False, "installed_environment_verified": False,
            "runtime_authority_granted": False, "first_live_success_verified": False, "retry_authorized": False}

    def abort(self):
        self._assert_process()
        with self._lock:
            self._taken = True
            self._clock.abort_new_work()

    def __copy__(self): raise TypeError("first-live start cannot be copied")
    def __deepcopy__(self, memo): raise TypeError("first-live start cannot be copied")
    def __reduce_ex__(self, protocol): raise TypeError("first-live start cannot be serialized")


def _prepare_claimed_first_live_start_impl(root, *, plan_bytes, authorization_bytes, expected_authorization_binding_sha256, _budget):
    """Private preparation only. No public dispatcher or Provider accepts this yet."""
    consumed = False
    clock = None
    winner = None
    try:
        if _budget is not None and type(_budget) is not _StartBudget:
            raise ValueError("first_live_start_budget_invalid")
        budget = _StartBudget() if _budget is None else _budget
        began, _ = budget.checkpoint()
        load_timed_implementation_contract(root)
        proof = control.verify_first_live_authorization(root, plan_bytes=plan_bytes, authorization_bytes=authorization_bytes,
            expected_authorization_binding_sha256=expected_authorization_binding_sha256, verification_time_utc=began)
        state = local_claim.local_claim_store_status()
        if state["status"] != "ready" or state["execution_environment_id"] != proof.execution_environment_id:
            raise ValueError("first_live_start_store_mismatch")
        plan = decode_strict_json_object(plan_bytes, max_bytes=131072)
        source_plan = decode_strict_json_object(_current_file(root, PROFILE_PATHS["first_live"][1]), max_bytes=2_000_000)
        identity = verify_local_timed_execution_identity(root, profile="first_live", expected_commit=proof.execution_commit,
            expected_tree=plan["binding"]["execution_tree"],
            expected_source_integrity_commitment_sha256=plan["binding"]["source_integrity_commitment_sha256"],
            expected_source_manifest_commitment_sha256=source_plan["implementation_commitment_sha256"])
        budget.checkpoint(proof.expires_at_utc)
        intended, _ = budget.checkpoint(proof.expires_at_utc)
        domain = "PCECLOCK-" + secrets.token_hex(16).upper()
        intent, request = control.build_first_live_claim_intent(root, proof, clock_domain_id=domain, intended_at_utc=intended)
        winner = local_claim._reserve_with_ownership(request)
        consumed = True
        receipt = winner._take()
        control.verify_first_live_claim_link(root, proof, intent_bytes=intent, claim_receipt_bytes=receipt,
            expected_claim_receipt_sha256=digest(receipt))
        now, phase_deadline = budget.checkpoint(proof.expires_at_utc)
        if parse_utc_timestamp(now) < parse_utc_timestamp(decode_strict_json_object(receipt, max_bytes=4096)["claimed_at_utc"]):
            raise ValueError("first_live_start_clock_reversed")
        clock = _TimingClock(request_timeout_ns=120_000_000_000, phase_timeout_ns=300_000_000_000)
        clock._tighten_phase_deadline(phase_deadline)
        # Claim IO is not permission to use source that changed during the claim.
        current = verify_current_timed_profile(root, profile="first_live")
        if (current.source_integrity_commitment_sha256 != identity.source_integrity_commitment_sha256
            or current.implementation_commitment_sha256 != identity.source_manifest_commitment_sha256
            or _head(root) != identity.execution_commit):
            raise ValueError("first_live_start_post_claim_source_mismatch")
        _now, phase_deadline = budget.checkpoint(proof.expires_at_utc)
        clock._tighten_phase_deadline(phase_deadline)
        return _ClaimedFirstLiveStart(_TOKEN, clock=clock, clock_domain_id=domain, receipt_bytes=receipt,
            intent_bytes=intent, source_identity=identity, plan_bytes=plan_bytes, budget=budget, expires=proof.expires_at_utc)
    except BaseException as error:
        consumed = consumed or bool(getattr(error, "claim_may_exist", False))
        if winner is not None:
            winner._invalidate()
        if clock is not None:
            clock.abort_new_work()
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        code = getattr(error, "code", None)
        if code is None and type(error) is ValueError and re.fullmatch(r"first_live_start_[a-z_]+", str(error)):
            code = str(error)
        if type(code) is not str or re.fullmatch(r"[a-z0-9_]{1,128}", code) is None:
            code = "first_live_start_failed"
        raise FirstLiveStartError(code, claim_consumed=consumed, origin="preparation") from None


def _prepare_claimed_first_live_start(root, *, plan_bytes, authorization_bytes, expected_authorization_binding_sha256):
    return _prepare_claimed_first_live_start_impl(root, plan_bytes=plan_bytes, authorization_bytes=authorization_bytes,
        expected_authorization_binding_sha256=expected_authorization_binding_sha256, _budget=None)


def _prepare_process_bound_first_live_start(root, *, plan_bytes, authorization_bytes, expected_authorization_binding_sha256):
    return _prepare_process_bound_first_live_start_impl(root, plan_bytes=plan_bytes, authorization_bytes=authorization_bytes,
        expected_authorization_binding_sha256=expected_authorization_binding_sha256, _budget=_StartBudget())


def _prepare_process_bound_first_live_start_impl(root, *, plan_bytes, authorization_bytes, expected_authorization_binding_sha256, _budget):
    """Require this loaded package's worktree; still no Provider/task/Key release."""
    prepared = None
    try:
        if type(_budget) is not _StartBudget:
            raise ValueError("first_live_start_budget_invalid")
        budget = _budget
        budget.checkpoint()
        if not isinstance(root, Path) or root.resolve(strict=True) != Path(__file__).resolve().parents[2]:
            raise ValueError("first_live_start_process_root_mismatch")
        source_plan = decode_strict_json_object(_current_file(root, PROFILE_PATHS["first_live"][1]), max_bytes=2_000_000)
        hashes = source_plan["component_hashes"]
        expected = dict(expected_dependency_lock_sha256=hashes["dependency_lock_sha256"], expected_pyproject_sha256=hashes["pyproject_sha256"])
        verify_process_environment(root, **expected)
        # The inner source/G3 checks must authenticate this source plan before
        # any claim; preliminary metadata equality is not source authority.
        prepared = _prepare_claimed_first_live_start_impl(root, plan_bytes=plan_bytes, authorization_bytes=authorization_bytes,
            expected_authorization_binding_sha256=expected_authorization_binding_sha256, _budget=budget)
        prepared._environment = verify_process_environment(root, **expected)
        _now, deadline = budget.checkpoint(prepared._expires)
        prepared.clock._tighten_phase_deadline(deadline)
        return prepared
    except BaseException as error:
        if prepared is not None:
            prepared.abort()
        if isinstance(error, (KeyboardInterrupt, SystemExit, FirstLiveStartError)):
            raise
        code = getattr(error, "code", None)
        if code is None and type(error) is ValueError and re.fullmatch(r"first_live_start_[a-z_]+", str(error)):
            code = str(error)
        if type(code) is not str or re.fullmatch(r"[a-z0-9_]{1,128}", code) is None:
            code = "first_live_start_environment_unavailable"
        raise FirstLiveStartError(code, claim_consumed=prepared is not None, origin="preparation") from None
