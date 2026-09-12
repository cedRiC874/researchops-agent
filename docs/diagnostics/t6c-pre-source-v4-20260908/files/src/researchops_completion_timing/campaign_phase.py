"""Private consumed-owner phase lifecycle; no public CLI or archive publication.

Only an actual, fresh campaign factory may enter. The private loader is patched
with a fake in offline tests; this module does not itself grant Key/network use.
Returned diagnostics are not persisted evidence or a closure verdict.
"""
from __future__ import annotations

import asyncio
import os

from researchops_completion_telemetry.capture import evaluate_runtime_denominator_closure
from .campaign_runtime import _CampaignModelFactory, CampaignRuntimeError


def _load_configured_key():
    value = os.environ.get('DEEPSEEK_API_KEY')
    if type(value) is not str or not value.strip():
        raise CampaignRuntimeError('campaign_phase_key_unavailable')
    return value.strip()


def _complete_after_owned_key_release(factory):
    """Private caller has unwound its Key-owning frame before entering here."""
    factory._check_current()
    count = len(factory._opening._cases)
    budget = factory._budget.summary()
    if (factory._busy or factory._active is not None or factory._reservation is not None
        or factory._send_pending is not None or factory._phase_finished
        or budget['halted'] or budget['inflight'] or not budget['usage_complete']
        or budget['completed_case_count'] != count or len(factory._runs) != count
        or len(factory._case_reconciliations) != count):
        factory._stop('campaign_phase_cases_incomplete')
    # Sealing is irreversible; failure cannot reopen a denominator for retry.
    denominator = factory._tracker.seal_runtime()
    if not evaluate_runtime_denominator_closure(denominator)['claim_allowed']:
        factory._stop('campaign_phase_denominator_incomplete')
    clock = factory._prepared.clock
    snapshot = clock.snapshot()
    if (snapshot['halted'] or snapshot['active_attempt'] is not None or snapshot['closed']
        or len(snapshot['attempts']) != len(denominator.attempts)
        or factory._network_count != len(denominator.attempts)):
        factory._stop('campaign_phase_attempts_incomplete')
    for run in factory._runs:
        if factory._ledger.get_run(run)['status'] != 'completed' or not factory._ledger.verify_chain(run).valid:
            factory._stop('campaign_phase_audit_invalid')
    clock.post_cleanup_terminal(status='completed')
    clock.key_reference_released(status='released')
    clock.phase_terminal()
    snapshot = clock.snapshot()
    if snapshot['halted'] or snapshot['clock_failed'] or not snapshot['closed']:
        factory._stop('campaign_phase_deadline_exceeded')
    factory._sealed_denominator = denominator
    factory._phase_finished = True


def _finish_failed_phase(factory):
    """Preserve partial observations; do not guess an unfinished attempt's end."""
    try:
        clock = factory._prepared.clock
        state = clock.snapshot()
        if state['active_attempt'] is not None or state['closed']:
            return
        if not clock._post_recorded:
            clock.post_cleanup_terminal(status='unknown')
        if not clock._key_recorded:
            clock.key_reference_released(status='released')
        clock.phase_terminal()
    except Exception:
        # Broken clocks/IO cannot be repaired with fabricated successful offsets.
        return


async def _owned_key_cases(factory):
    key = None
    failure = None
    try:
        factory.before_key_load()
        key = _load_configured_key()
        factory.key_loaded()
        for index in range(len(factory._opening._cases)):
            remaining = factory._prepared.clock.remaining_phase_ns()
            if remaining <= 0:
                raise CampaignRuntimeError('campaign_phase_deadline_exceeded')
            async with asyncio.timeout(remaining / 1_000_000_000):
                await factory.run_case(index, api_key=key)
    except BaseException as error:
        failure = ('campaign_phase_cancelled' if isinstance(error, asyncio.CancelledError)
                   else 'campaign_phase_failed')
        factory._abort()
        # Never return SDK exception bodies/tracebacks containing request/Key refs.
        error.__traceback__ = error.__cause__ = error.__context__ = None
    finally:
        key = None  # Owned reference, not environment deletion or physical erasure.
    return failure


async def _run_owned_campaign_phase(factory):
    """One private invocation; no caller Key, clock, loader, prompt or retry input."""
    if type(factory) is not _CampaignModelFactory:
        raise CampaignRuntimeError('campaign_phase_factory_required')
    state = factory._prepared.clock.snapshot()
    if (factory._phase_driver_taken or factory._busy or factory._runs
        or factory._phase_finished or factory._failed or state['closed']
        or state['halted'] or state['active_attempt'] is not None
        or state['phase']['key_loaded_ns'] is not None):
        factory._stop('campaign_phase_already_started')
    factory._phase_driver_taken = True
    # _owned_key_cases returns only after its finally and exception frame unwind.
    failure = await _owned_key_cases(factory)
    if failure is None:
        try:
            _complete_after_owned_key_release(factory)
        except BaseException as error:
            failure = 'campaign_phase_finalization_failed'
            factory._abort()
            error.__traceback__ = error.__cause__ = error.__context__ = None
    if failure is not None:
        _finish_failed_phase(factory)
    return dict(status='campaign_phase_completed' if failure is None else 'campaign_phase_failed',
                error_code=failure, observation_scope='owned_local_phase_only',
                phase_completed=factory._phase_finished, artifact_published=False,
                closure_claim_allowed=False, provider_calls_independently_verified=False,
                provider_bill_cny=None, retry_authorized=False)
