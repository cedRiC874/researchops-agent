"""Internal freeze + explicit operator approval + fresh fixed-store ownership.

No external signatures or role proofs are manufactured. The approval channel is
the trusted local operator supplying the exact user-approved digest; its identity
is not independently authenticated by a local JSON document.
"""
from __future__ import annotations

import os
import re
import time
import uuid

from researchops_completion_timing import local_claim
from researchops_completion_timing.clock import _TimingClock
from . import source
from .contract import (ROOT, DIRECTORY, ENVIRONMENT_ID, SCOPE, budget_plan, commitment, decode,
                       digest, exact, load_policy, now, raw, read, require, sha, utc,
                       validate_pricing, validate_tasks)

_TOKEN = object()


def build_freeze(*, execution_commit, pricing, review_record, root=ROOT):
    """Offline authoring only. This does not assert that a user has approved it."""
    policy = load_policy(root)
    tasks = validate_tasks(decode(read(root / DIRECTORY / "cases_v1.json")), policy)
    validate_pricing(pricing)
    from researchops_completion_timing.campaign_budget import _CampaignBudget
    _CampaignBudget(budget_plan(policy, tasks, pricing))  # Pure pre-claim policy validation.
    exact(review_record, "kind preparer reviewer same_person developer_known old_task_exclusion_record_sha256")
    require(review_record["kind"] == "internal_review" and review_record["developer_known"] is True
            and type(review_record["same_person"]) is bool, "review_invalid")
    for name in ("preparer", "reviewer"):
        require(type(review_record[name]) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", review_record[name]), "review_identifier")
    sha(review_record["old_task_exclusion_record_sha256"])
    from .task_origin import ORIGIN_PATH, build_origin_record
    origin_bytes = read(root / ORIGIN_PATH)
    require(decode(origin_bytes) == build_origin_record(root)
        and digest(origin_bytes) == review_record["old_task_exclusion_record_sha256"], "task_origin_mismatch")
    require(type(execution_commit) is str and re.fullmatch(r"(?!0{40}$)[0-9a-f]{40}", execution_commit), "execution_commit")
    bound_source = source.verify_source(root)
    return dict(schema_version="provider-completion-internal-freeze/1.0", scope=SCOPE,
        execution_commit=execution_commit, source_commitment_sha256=bound_source["commitment_sha256"],
        protocol=policy, tasks_sha256=digest(read(root / DIRECTORY / "cases_v1.json")), pricing=pricing, review_record=review_record,
        external_validation_completed=False, status_closure_allowed=False)


def validate_freeze(freeze, root=ROOT):
    exact(freeze, "schema_version scope execution_commit source_commitment_sha256 protocol tasks_sha256 pricing review_record external_validation_completed status_closure_allowed")
    expected = build_freeze(execution_commit=freeze["execution_commit"], pricing=freeze["pricing"], review_record=freeze["review_record"], root=root)
    require(freeze == expected, "freeze_mismatch")
    return freeze


def validate_authorization(document, freeze, approved_digest, *, at):
    exact(document, "schema_version candidate approval_observation")
    require(document["schema_version"] == "provider-completion-internal-authorization/1.0", "authorization_schema")
    candidate = document["candidate"]
    exact(candidate, "schema_version scope authorization_id freeze_commitment_sha256 execution_environment_id not_before_utc expires_at_utc output_namespace")
    require(candidate["schema_version"] == "provider-completion-internal-authorization-candidate/1.0"
            and candidate["scope"] == SCOPE and candidate["execution_environment_id"] == ENVIRONMENT_ID
            and candidate["output_namespace"] == "output/internal-telemetry-v1", "authorization_scope")
    require(type(candidate["authorization_id"]) is str and re.fullmatch(r"[a-z][a-z0-9-]{7,95}", candidate["authorization_id"]), "authorization_id")
    require(candidate["freeze_commitment_sha256"] == commitment("freeze", freeze), "authorization_freeze")
    actual = commitment("authorization-candidate", candidate)
    require(sha(approved_digest) == actual, "approval_digest")
    observation = document["approval_observation"]
    exact(observation, "kind approved_digest observed_at_utc message_reference")
    require(observation["kind"] == "explicit_user_approval" and observation["approved_digest"] == actual, "approval_missing")
    require(observation["message_reference"] is None or (type(observation["message_reference"]) is str
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", observation["message_reference"])), "approval_reference")
    start = utc(candidate["not_before_utc"])
    end = utc(candidate["expires_at_utc"])
    approved = utc(observation["observed_at_utc"])
    require(start <= approved <= at < end, "authorization_expired")
    require(freeze["pricing"]["evidence_date_utc"][:10] == candidate["not_before_utc"][:10], "pricing_date")
    return candidate


class _ClaimedInternal:
    def __init__(self, token, *, freeze_bytes, authorization_bytes, receipt_bytes, request_bytes, approved_digest, clock_domain):
        require(token is _TOKEN, "owner_required")
        self.freeze_bytes, self.authorization_bytes = freeze_bytes, authorization_bytes
        self.receipt_bytes, self.request_bytes = receipt_bytes, request_bytes
        self.approved_digest, self.clock_domain = approved_digest, clock_domain
        self._identity = tuple(map(digest, (freeze_bytes, authorization_bytes, receipt_bytes, request_bytes)))
        self._pid, self._last_utc, self._last_ns = os.getpid(), now(), time.monotonic_ns()
        self._halted = self._taken = self._mapping_taken = False
        self.clock = _TimingClock(request_timeout_ns=120_000_000_000, phase_timeout_ns=5_400_000_000_000)

    def __copy__(self): raise TypeError("internal owner cannot be copied")
    def __deepcopy__(self, memo): raise TypeError("internal owner cannot be copied")
    def __reduce_ex__(self, protocol): raise TypeError("internal owner cannot be serialized")

    def check(self, *, source_check=False):
        require(type(self) is _ClaimedInternal and not self._halted and os.getpid() == self._pid, "owner_invalid")
        require(self._identity == tuple(map(digest, (self.freeze_bytes, self.authorization_bytes, self.receipt_bytes, self.request_bytes))), "owner_changed")
        moment, stamp = now(), time.monotonic_ns()
        require(moment >= self._last_utc and stamp >= self._last_ns, "clock_order")
        self._last_utc, self._last_ns = moment, stamp
        freeze, authorization = decode(self.freeze_bytes), decode(self.authorization_bytes)
        validate_authorization(authorization, freeze, self.approved_digest, at=moment)
        local_claim.read_reserved_local_claim(self.request_bytes, expected_receipt_sha256=digest(self.receipt_bytes))
        if source_check:
            source.verify_source(ROOT, expected=freeze["source_commitment_sha256"])
        # IO above can take time. Recheck immediately before giving control back
        # to Key loading or the request owner, not only before that IO started.
        final_moment, final_stamp = now(), time.monotonic_ns()
        require(final_moment >= self._last_utc and final_stamp >= self._last_ns, "clock_order")
        require(final_moment < utc(authorization["candidate"]["expires_at_utc"]), "authorization_expired")
        require(self.clock.remaining_phase_ns() > 0, "phase_expired")
        self._last_utc, self._last_ns = final_moment, final_stamp
        return freeze

    def take(self):
        self.check(source_check=True)
        require(not self._taken, "owner_reused")
        self._taken = True
        return self

    def abort(self):
        self._halted = True
        self.clock.abort_new_work()


def prepare(*, freeze_bytes, authorization_bytes, approved_digest):
    require(os.name == "nt", "execution_environment_unsupported")
    freeze = validate_freeze(decode(freeze_bytes))
    authorization = decode(authorization_bytes, 16384)
    candidate = validate_authorization(authorization, freeze, approved_digest, at=now())
    bound_source = source.verify_source(ROOT, expected=freeze["source_commitment_sha256"])
    source.verify_execution(ROOT, freeze["execution_commit"], bound_source)
    # No environment credential values are read in this preparation path.
    from researchops.deepseek_completion_first_live_validation import _environment_isolated, _network_logging_disabled
    require(_environment_isolated(os.environ) and _network_logging_disabled(), "environment_not_isolated")
    require("DEEPSEEK_API_KEY" in os.environ, "key_variable_absent")
    require(local_claim.local_claim_store_status() == dict(status="ready", execution_environment_id=ENVIRONMENT_ID, runtime_authority_granted=False), "store_not_ready")
    require((utc(candidate["expires_at_utc"]) - now()).total_seconds() >= 5520, "startup_window_short")
    clock_domain = "PCECLOCK-" + uuid.uuid4().hex.upper()
    intent = dict(schema_version="provider-completion-internal-claim-intent/1.0", scope=SCOPE,
        authorization_sha256=digest(authorization_bytes), freeze_commitment_sha256=commitment("freeze", freeze), clock_domain_id=clock_domain)
    request = dict(schema_version="provider-completion-local-claim-request/1.0", execution_environment_id=ENVIRONMENT_ID,
        authorization_id_sha256=digest(candidate["authorization_id"].encode()), authorization_grant_sha256=digest(authorization_bytes),
        consumption_entry_sha256=digest(raw(intent)), timing_plan_commitment_sha256=commitment("timing-plan", freeze),
        execution_commit=freeze["execution_commit"], clock_domain_id=clock_domain)
    winner = local_claim._reserve_with_ownership(raw(request))
    try:
        receipt = winner._take()
        return _ClaimedInternal(_TOKEN, freeze_bytes=freeze_bytes, authorization_bytes=authorization_bytes,
            receipt_bytes=receipt, request_bytes=raw(request), approved_digest=approved_digest, clock_domain=clock_domain)
    except BaseException as error:
        winner._invalidate()
        # A successful claim write cannot become "not consumed" because local
        # ownership construction/clock initialization failed after that write.
        error.claim_may_exist = True
        raise
