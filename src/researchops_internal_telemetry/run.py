"""Versioned Internal entrypoint. CLI execution requires explicit fresh approval."""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import time

from researchops.audit import AuditLedger
from . import admission, artifacts, source
from .contract import ROOT, InternalError, decode, digest, now, raw, read, require, safe_error_code, utc
from .runtime import _InternalFactory


def _load_key(owner):
    # This sole value read is reached only after prepare + winning ownership.
    require(type(owner) is admission._ClaimedInternal and owner._taken and owner._mapping_taken, "key_owner_required")
    owner.check(source_check=True)
    require(owner.clock.snapshot()["phase"]["task_released_ns"] is not None, "key_task_boundary")
    value = os.environ.get("DEEPSEEK_API_KEY")
    require(type(value) is str and bool(value.strip()), "key_unavailable")
    return value.strip()


async def _key_phase(factory):
    key = None
    try:
        factory.check()
        key = _load_key(factory.owner)
        factory.owner.clock.key_loaded()
        for _ in range(30):
            await factory.run_case(key)
        return None
    except BaseException as error:
        code = safe_error_code(error)
        error.__traceback__ = error.__context__ = error.__cause__ = None
        factory._failed = True
        factory.owner.abort()
        return code
    finally:
        key = None


async def run_internal_v1(*, freeze_bytes, authorization_bytes, approved_digest, confirm_online=False):
    """No root, transport, clock, Key loader, store path or bypass parameters."""
    owner = factory = None
    claim_may_exist = False
    artifact_status = "unavailable"
    error_code = None
    try:
        require(confirm_online is True, "confirmation_required")
        from agents import set_tracing_disabled
        set_tracing_disabled(True)
        owner = admission.prepare(freeze_bytes=freeze_bytes, authorization_bytes=authorization_bytes, approved_digest=approved_digest)
        with artifacts.owned_paths(owner) as (database, archive, bundle):
            ledger = AuditLedger(database, timestamp_format="utc_z")
            factory = _InternalFactory(owner, ledger)
            error_code = await _key_phase(factory)
            snapshot = owner.clock.snapshot()
            if error_code is None:
                owner.check(source_check=True)
                source.verify_execution(ROOT, factory.freeze["execution_commit"], source.verify_source(ROOT))
            if snapshot["active_attempt"] is None and not snapshot["closed"]:
                owner.clock.post_cleanup_terminal(status="completed" if error_code is None else "unknown")
                owner.clock.key_reference_released(status="released")
                owner.clock.phase_terminal()
            status = "completed" if error_code is None else (
                "outcome_unknown" if any(item["terminal_kind"] == "outcome_unknown" for item in factory.terminals) else "failed")
            ledger.set_run_status(factory.run_id, "completed" if status == "completed" else "failed", terminal_error_code=error_code)
            commitment, sealing_started = artifacts.seal(factory, database, archive, bundle, run_status=status)
            artifact_status = "sealed"
            from .verify import verify_archive
            checked = verify_archive(archive, bundle_bytes=read(bundle, 32768), expected_bundle_commitment=commitment,
                expected_approved_digest=approved_digest)
            require(0 <= time.monotonic_ns()-sealing_started <= 120_000_000_000
                and now() < utc(decode(owner.authorization_bytes)["candidate"]["expires_at_utc"]), "sealing_timeout")
            if error_code is None:
                require(checked["eligible_before_exit_observation"], "archive_not_eligible")
                return dict(schema_version="provider-completion-internal-invocation/1.0", status="completed",
                    claim_consumed=True, artifact_status=artifact_status, bundle_commitment_sha256=commitment,
                    case_pass_count=30, model_requests=factory.model_requests, network_attempts=factory.network_attempts,
                    transport_mode=factory.transport_mode,
                    process_exit_observed=False, internal_acceptance_passed=False,
                    acceptance_pending="independent_exit_and_archive_readback", external_validation_completed=False,
                    status_closure_allowed=False, provider_bill=None, retry_authorized=False, exit_code=0)
    except BaseException as error:
        claim_may_exist = getattr(error, "claim_may_exist", False)
        if error_code is None:
            error_code = safe_error_code(error)
        error.__traceback__ = error.__context__ = error.__cause__ = None
        if owner is not None:
            owner.abort()
        if factory is not None and artifact_status == "unavailable":
            artifact_status = "partial"
    return dict(schema_version="provider-completion-internal-invocation/1.0",
        status="outcome_unknown" if claim_may_exist or (factory is not None and any(item["terminal_kind"] == "outcome_unknown" for item in factory.terminals))
            else "failed_before_dispatch" if factory is None or factory.network_attempts == 0 else "failed",
        claim_consumed=True if owner is not None else None if claim_may_exist else False,
        artifact_status=artifact_status, error_code=error_code,
        model_requests=factory.model_requests if factory is not None else None if claim_may_exist else 0,
        network_attempts=factory.network_attempts if factory is not None else None if claim_may_exist else 0,
        transport_mode=factory.transport_mode if factory is not None else None,
        internal_acceptance_passed=False, external_validation_completed=False, status_closure_allowed=False,
        provider_bill=None, retry_authorized=False, exit_code=4)


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise InternalError("internal_cli_arguments_invalid")


def main(argv=None):
    parser = _SafeParser(prog="researchops-internal-v1",description="Explicit Internal v1; no external admission or resume.")
    parser.add_argument("--freeze", required=True)
    parser.add_argument("--authorization", required=True)
    parser.add_argument("--approved-digest", required=True)
    parser.add_argument("--confirm-online", action="store_true")
    # Not an auto-generated authorization; the operator must provide approval.
    try:
        args = parser.parse_args(argv)
        result = asyncio.run(run_internal_v1(freeze_bytes=read(args.freeze), authorization_bytes=read(args.authorization,16384),
            approved_digest=args.approved_digest, confirm_online=args.confirm_online))
    except SystemExit as error:
        if error.code == 0:return 0
        raise
    except BaseException:
        result = dict(schema_version="provider-completion-internal-invocation/1.0", status="failed_before_dispatch",
            error_code="internal_input_read_failed", claim_consumed=False, network_attempts=0,
            internal_acceptance_passed=False, status_closure_allowed=False, exit_code=4)
    print(raw(result).decode("utf-8"))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
