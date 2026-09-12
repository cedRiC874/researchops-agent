"""Explicit first-live invocation; no callbacks, Root override or CLI Key value."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from researchops.audit import AuditLedger
from researchops_external_closure.io import read_regular_file_no_follow
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object
from .first_live_start import _prepare_process_bound_first_live_start_impl, FirstLiveStartError, _StartBudget
from .first_live_runtime import _FirstLiveModelFactory, _FirstLiveModelFactoryV5
from .first_live_publish import _publish_completed_first_live, _publish_completed_first_live_v5, _write_exclusive, FirstLivePublishError
from .first_live_failure import build_first_live_failure_receipt
from .local_claim import _locked_chain
from .first_live_workspace import _owned_work_paths
from .first_live_summary import load_summary_schema, validate_invocation_summary
from .first_live_failure_artifacts import load_failure_artifact_schema, _persist_owned_failure


ROOT = Path(__file__).resolve().parents[2]


def _load_configured_key():
    """Only called after checked preparation/claim and the pre-Key boundary."""
    value = os.environ.get("DEEPSEEK_API_KEY")
    if type(value) is not str or not value.strip():
        raise FirstLiveStartError("first_live_key_unavailable", claim_consumed=True)
    return value.strip()


def _failure(error, factory=None):
    try:
        return build_first_live_failure_receipt(ROOT, error, factory=factory)
    except BaseException:
        # A damaged schema must not make the CLI print an exception/Key body.
        return raw(dict(schema_version="provider-completion-first-live-failure/1.0", status="outcome_unknown",
            error_code="first_live_failure_unknown", observation_scope="insufficient_observation", claim_consumed=None,
            observed_send_markers=None, dispatch_attempt_count=None, validated_response_count=None, inflight=None,
            token_usage=None, provider_bill=None, exception_text_recorded=False, model_content_recorded=False,
            api_key_recorded=False, retry_authorized=False, resume_authorized=False, fallback_authorized=False,
            model_quality_claim_allowed=False, runtime_authority_granted=False, closure_claim_allowed=False))


def _sealing_checkpoint(prepared, started):
    try:
        prepared._budget.artifact_checkpoint(prepared._expires)
        now = prepared.clock._sealing_offset()
        if type(started) is not int or not 0 <= started <= now or now - started > 30_000_000_000:
            raise ValueError("first_live_publish_timeout")
    except Exception:
        raise FirstLivePublishError("first_live_publish_timeout", directory_created=True, created_files=()) from None


def _publish_envelope(prepared, path, publication):
    started = publication['sealing_started_ns']
    _sealing_checkpoint(prepared, started)
    with _locked_chain(path.parent):
        _write_exclusive(path, publication['bundle_bytes'], [])
        _sealing_checkpoint(prepared, started)
    _sealing_checkpoint(prepared, started)


async def _owned_key_phase(factory):
    key = None
    try:
        factory.before_key_load()
        key = _load_configured_key()
        factory.key_loaded()
        for _ in range(2):
            await factory.run_next(api_key=key)
        return None
    except BaseException as error:
        factory._failed = True
        factory._prepared.abort()
        receipt = _failure(error, factory)
        # Do not retain SDK exception chains containing client/request objects.
        error.__traceback__ = error.__cause__ = error.__context__ = None
        return receipt
    finally:
        key = None  # Owned reference only; not an environment/physical RAM wipe.


def _close_failed_phase(factory):
    try:
        snapshot = factory._prepared.clock.snapshot()
        if snapshot["active_attempt"] is None and not snapshot["closed"]:
            if not factory._prepared.clock._post_recorded:
                factory._prepared.clock.post_cleanup_terminal(status="unknown")
            if not factory._prepared.clock._key_recorded:
                factory._prepared.clock.key_reference_released(status="released")
            factory._prepared.clock.phase_terminal()
        factory._ledger.set_run_status(factory._run, "failed", terminal_error_code="first_live_failed")
    except BaseException:
        pass  # Preserve partial evidence; never repair or turn it into success.


def _try_persist_failure(factory, receipt):
    try:
        _persist_owned_failure(factory, receipt)
    except BaseException:
        # Expiry, cancellation, partial writes or damaged state cannot justify
        # retry or an invented archive-success flag in the stdout receipt.
        pass


async def run_timed_first_live(*, plan_bytes, authorization_bytes, expected_authorization_binding_sha256, confirm_online=False):
    """One explicit invocation; the operator must supply a fresh authorized G3.

    This function is never invoked against a real Provider by offline tests.
    No key, transport, clock, artifact-directory or verifier parameter is exposed.
    """
    if confirm_online is not True:
        return _failure(FirstLiveStartError("first_live_confirmation_required", claim_consumed=False, origin="preparation"))
    try:
        budget = _StartBudget()
    except BaseException as error:
        return _failure(error)
    return await _run_with_budget(plan_bytes=plan_bytes, authorization_bytes=authorization_bytes,
        expected_authorization_binding_sha256=expected_authorization_binding_sha256, budget=budget)


async def run_timed_first_live_v5(*, plan_bytes, authorization_bytes, expected_authorization_binding_sha256, confirm_online=False):
    """Explicit v5 implementation, unchanged wire formats and fixed probe limits."""
    if confirm_online is not True:
        return _failure(FirstLiveStartError('first_live_confirmation_required', claim_consumed=False, origin='preparation'))
    try:
        budget = _StartBudget()
    except BaseException as error:
        return _failure(error)
    return await _run_with_budget(plan_bytes=plan_bytes, authorization_bytes=authorization_bytes,
        expected_authorization_binding_sha256=expected_authorization_binding_sha256, budget=budget, _implementation_version=5)


async def _run_with_budget(*, plan_bytes, authorization_bytes, expected_authorization_binding_sha256, budget, _implementation_version=4):
    prepared = factory = publication = None
    try:
        if type(_implementation_version) is not int or _implementation_version not in (4, 5):
            raise FirstLiveStartError('first_live_start_version_invalid', claim_consumed=False, origin='preparation')
        load_summary_schema(ROOT)  # Output contract failure must precede claim/Key.
        load_failure_artifact_schema(ROOT)
        extra = {} if _implementation_version == 4 else {'_implementation_version': 5}
        prepared = _prepare_process_bound_first_live_start_impl(ROOT, plan_bytes=plan_bytes, authorization_bytes=authorization_bytes,
            expected_authorization_binding_sha256=expected_authorization_binding_sha256, _budget=budget, **extra)
        plan = decode_strict_json_object(plan_bytes, max_bytes=131072)
        identifier = plan["binding"]["authorization_id_sha256"]
        with _owned_work_paths(ROOT, identifier) as (database, directory, envelope_path):
            try:
                factory_type = _FirstLiveModelFactory if _implementation_version == 4 else _FirstLiveModelFactoryV5
                factory = factory_type(prepared, AuditLedger(database))
                failure = await _owned_key_phase(factory)
                if failure is not None:
                    _close_failed_phase(factory)
                    _try_persist_failure(factory, failure)
                    return failure
                factory.complete_phase_after_key_release()
                publisher = _publish_completed_first_live if _implementation_version == 4 else _publish_completed_first_live_v5
                publication = publisher(factory, directory, authorization_bytes=authorization_bytes)
                _publish_envelope(prepared, envelope_path, publication)
                bundle = decode_strict_json_object(publication["bundle_bytes"], max_bytes=16384)
                result = raw(dict(schema_version="provider-completion-first-live-run/4.0", status="completed", summary_only=True,
                    authorization_id_sha256=identifier, artifact_directory=directory.relative_to(ROOT).as_posix(),
                    bundle_path=envelope_path.relative_to(ROOT).as_posix(), bundle_commitment_sha256=bundle["bundle_commitment_sha256"],
                    dispatch_attempt_count=2, model_quality_claim_allowed=False, provider_registration_authorized=False,
                    runtime_authority_granted=False, closure_claim_allowed=False, retry_authorized=False, resume_authorized=False))
                validate_invocation_summary(ROOT, result)
            except BaseException as error:
                if factory is not None:
                    factory._failed = True
                    prepared.abort()
                    _close_failed_phase(factory)  # While owned paths are still held.
                    if factory._phase_finished and type(error) is not FirstLivePublishError:
                        error = FirstLivePublishError('first_live_publish_failed', directory_created=True, created_files=())
                    receipt = _failure(error, factory)
                    _try_persist_failure(factory, receipt)
                    return receipt
                raise
        _sealing_checkpoint(prepared, publication['sealing_started_ns'])  # Includes work-handle flush/close.
        return result
    except BaseException as error:
        if prepared is not None:
            prepared.abort()
        if factory is not None and factory._phase_finished and type(error) is not FirstLivePublishError:
            error = FirstLivePublishError("first_live_publish_failed", directory_created=True, created_files=())
        elif factory is None and prepared is not None:
            error = FirstLiveStartError("first_live_pre_model_failed", claim_consumed=True, origin="preparation")
        return _failure(error, factory)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("first_live_cli_arguments_invalid")


def main(argv=None):
    return _main(argv, implementation_version=4)


def main_v5(argv=None):
    return _main(argv, implementation_version=5)


def _main(argv, *, implementation_version):
    if type(implementation_version) is not int or implementation_version not in (4, 5):
        raise ValueError('first_live_cli_version_invalid')
    parser = _Parser(description="Run one explicitly authorized timed first-live validation.", allow_abbrev=implementation_version == 4)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--authorization", required=True)
    parser.add_argument("--expected-authorization-binding", required=True)
    parser.add_argument("--confirm-online", action="store_true")
    try:
        args = parser.parse_args(argv)
        if not args.confirm_online:
            result = _failure(FirstLiveStartError("first_live_confirmation_required", claim_consumed=False, origin="preparation"))
        else:
            budget = _StartBudget()  # Includes input-file reads; never reset by preparation.
            plan = read_regular_file_no_follow(Path(args.plan), max_bytes=131072)
            auth = read_regular_file_no_follow(Path(args.authorization), max_bytes=8192)
            extra = {} if implementation_version == 4 else {'_implementation_version': 5}
            result = asyncio.run(_run_with_budget(plan_bytes=plan, authorization_bytes=auth,
                expected_authorization_binding_sha256=args.expected_authorization_binding, budget=budget, **extra))
    except SystemExit as error:
        if error.code == 0:
            return 0  # argparse's static help contains no caller-supplied values.
        result = _failure(error)
    except BaseException as error:
        result = _failure(error)
    print(result.decode("utf-8"))
    return 0 if json.loads(result).get("status") == "completed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
