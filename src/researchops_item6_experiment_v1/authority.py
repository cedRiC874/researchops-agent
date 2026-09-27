"""Fresh internal claim ownership. Receipts and caller-created objects grant nothing."""
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
import weakref

from researchops_completion_timing import local_claim
from researchops_completion_timing.clock import _TimingClock
from . import contract as c

_prepared = weakref.WeakKeyDictionary()
_owners = weakref.WeakKeyDictionary()
_lock = threading.RLock()


class Prepared:
    __slots__ = ("__weakref__",)
    def __init__(self, *a, **kw): raise TypeError("prepared requires validation")
    def __copy__(self): raise TypeError("prepared is not transferable")
    def __deepcopy__(self, memo): raise TypeError("prepared is not transferable")
    def __reduce_ex__(self, protocol): raise TypeError("prepared is not serializable")


def _test_location():
    """Mode barrier before even opening a store; tests replace only the OS locator."""
    path = local_claim._windows_local_app_data()
    temp = Path(tempfile.gettempdir()).resolve()
    c.require(type(path) is type(Path()) and path.is_absolute() and path.parent.resolve() == temp
              and path.name.startswith("item6-test-store-") and path.resolve() == path
              and not path.is_symlink() and not path.is_junction(), "offline_store_isolation")
    return path


def validate_experiment(*, freeze_bytes, approval_bytes, approved_digest, expected_mode):
    c.require(expected_mode in {"offline_test", "live"}, "entry_mode")
    freeze = c.validate_freeze(c.decode(freeze_bytes))
    c.require(freeze["mode"] == expected_mode, "entry_mode_mismatch")
    approval = c.validate_approval(c.decode(approval_bytes), freeze, approved_digest)
    c.require(c.raw(freeze) == freeze_bytes and c.raw(approval) == approval_bytes, "canonical_documents_required")
    c.check_source(freeze)
    c.check_execution(freeze)
    if expected_mode == "offline_test": _test_location()
    # This status read is reached only in the explicitly isolated test backend or live admission.
    status = local_claim.local_claim_store_status()
    c.require(status["status"] == "ready" and status["execution_environment_id"] == freeze["environment_id"], "store_identity")
    output = c.ROOT / c.OUTPUT / c.digest(approval["candidate"]["authorization_id"].encode())
    c.require(not output.exists() and not output.is_symlink(), "output_exists")
    value = object.__new__(Prepared)
    with _lock:
        _prepared[value] = dict(freeze=freeze_bytes, approval=approval_bytes, digest=approved_digest,
                                pid=os.getpid(), consumed=False, output=output)
    return value


class Owner:
    __slots__ = ("__weakref__",)
    def __init__(self, *a, **kw): raise TypeError("owner requires successful fresh writer")
    def __copy__(self): raise TypeError("owner cannot be copied")
    def __deepcopy__(self, memo): raise TypeError("owner cannot be copied")
    def __reduce_ex__(self, protocol): raise TypeError("owner cannot be serialized")

    def _state(self):
        with _lock:
            c.require(type(self) is Owner and self in _owners, "owner_required")
            state = _owners[self]
            c.require(state["pid"] == os.getpid(), "owner_process")
            return state

    @property
    def mode(self): return c.decode(self._state()["freeze"])["mode"]
    @property
    def freeze(self): return c.decode(self._state()["freeze"])
    @property
    def clock(self): return self._state()["clock"]
    @property
    def run_id(self): return self._state()["run_id"]
    @property
    def clock_domain(self): return self._state()["clock_domain"]
    @property
    def output(self): return self._state()["output"]
    @property
    def stopped(self): return self._state()["stopped"]

    def stop(self, code):
        state = self._state()
        state["stopped"] = state["stopped"] or code
        self.clock.abort_new_work()

    def check(self, *, source_check=False):
        state = self._state()
        c.require(not state["stopped"] and not state["closed"], "owner_stopped")
        freeze = self.freeze
        c.validate_approval(c.decode(state["approval"]), freeze, state["digest"])
        instant = c.now()
        c.require(instant >= state["last_utc"], "utc_rollback")
        state["last_utc"] = instant
        if self.mode == "offline_test": _test_location()
        local_claim.read_reserved_local_claim(state["request"], expected_receipt_sha256=c.digest(state["receipt"]))
        if source_check: c.check_source(freeze)
        self.dispatch_checkpoint()
        return freeze

    def dispatch_checkpoint(self):
        state = self._state()
        c.require(not state["stopped"] and not state["closed"], "owner_stopped")
        candidate = c.decode(state["approval"])["candidate"]
        instant = c.now()
        c.require(instant >= state["last_utc"] and instant < c.utc(candidate["expires_at_utc"]), "dispatch_expired")
        state["last_utc"] = instant
        c.require(self.clock.remaining_phase_ns() > 0, "phase_expired")

    def take_factory(self, factory):
        self.check(source_check=True)
        with _lock:
            state = self._state()
            c.require(not state["factory_taken"], "factory_reused")
            state["factory_taken"] = True
            state["factory"] = factory

    def assert_factory(self, factory):
        state = self._state()
        c.require(state["factory_taken"] and state["factory"] is factory, "factory_identity")
        c.require(factory.freeze == self.freeze, "factory_freeze_drift")
        if hasattr(factory, "budget"):
            c.require(factory.budget.limits == self.freeze["budget"] and factory.budget.pricing == self.freeze["pricing"]
                      and factory.budget.mode == self.mode, "budget_policy_drift")

    def open_business_case(self, factory, case):
        self.assert_factory(factory)
        state = self._state()
        c.require(state["case"] is None and state["case_index"] < 32, "owner_case_order")
        expected = self.freeze["business_plan"][state["case_index"]]
        c.require(expected == {"task_id": case.task["task_id"], "path_kind": case.path}, "owner_case_order")
        original = next(t for t in c.decode(c.read(c.TASKS))["tasks"] if t["task_id"] == case.task["task_id"])
        c.require(case.task == original, "task_memory_drift")
        state["case"] = case; state["case_index"] += 1
        state["case_identity"] = c.digest(c.raw({"task": case.task, "path": case.path, "run_id": case.record["run_id"]}))

    def assert_case(self, factory, case):
        self.assert_factory(factory)
        c.require(self._state()["case"] is case and case is not None, "owner_case_identity")
        c.require(self._state()["case_identity"] == c.digest(c.raw({"task": case.task, "path": case.path, "run_id": case.record["run_id"]})), "case_memory_drift")

    def close_business_case(self, factory, case):
        self.assert_case(factory, case)
        self._state()["case"] = None

    def _consume_mapping(self):
        self.check(source_check=True)
        with _lock:
            state = self._state()
            c.require(state["factory_taken"] and not state["mapping_taken"], "mapping_owner_required")
            state["mapping_taken"] = True
            return self.mode

    def _bind_mapping(self, mapping):
        state = self._state()
        c.require(state["mapping_taken"] and state["mapping"] is None, "mapping_reused")
        state["mapping"] = mapping

    def _assert_mapping(self, mapping, mode):
        state = self._state()
        # Identity proof remains usable to record/validate a failure terminal, never to send new work.
        c.require(state["mapping"] is mapping and state["mapping_taken"] and self.mode == mode, "mapping_identity")

    def snapshots(self):
        state = self._state()
        return {"freeze": self.freeze, "approval": c.decode(state["approval"]),
                "claim_request": c.decode(state["request"]), "claim_receipt": c.decode(state["receipt"]),
                "approved_digest": state["digest"], "mode": self.mode, "run_id": self.run_id,
                "clock_domain_id": self.clock_domain, "stopped": self.stopped}

    def close(self): self._state()["closed"] = True


def _claim_experiment(prepared):
    with _lock:
        c.require(type(prepared) is Prepared and prepared in _prepared, "prepared_required")
        value = _prepared[prepared]
        c.require(value["pid"] == os.getpid() and not value["consumed"], "prepared_consumed")
        value["consumed"] = True  # uncertain claims cannot be attempted again
    freeze = c.validate_freeze(c.decode(value["freeze"]))
    approval = c.validate_approval(c.decode(value["approval"]), freeze, value["digest"])
    c.check_source(freeze); c.check_execution(freeze)
    if freeze["mode"] == "offline_test": _test_location()
    domain = "PCECLOCK-" + uuid.uuid4().hex.upper()
    intent = dict(scope=c.SCOPE, mode=freeze["mode"], freeze_sha256=c.commit("freeze", freeze),
                  approval_sha256=c.digest(value["approval"]), clock_domain_id=domain)
    request = dict(schema_version="provider-completion-local-claim-request/1.0", execution_environment_id=freeze["environment_id"],
                   authorization_id_sha256=c.commit("authorization-id", {"mode": freeze["mode"], "id": approval["candidate"]["authorization_id"]}),
                   authorization_grant_sha256=c.digest(value["approval"]), consumption_entry_sha256=c.commit("claim-intent", intent),
                   timing_plan_commitment_sha256=c.commit("timing-plan", freeze), execution_commit=freeze["execution_commit"], clock_domain_id=domain)
    winner = local_claim._reserve_with_ownership(c.raw(request))
    try:
        receipt = winner._take()
        owner = object.__new__(Owner)
        clock = _TimingClock(request_timeout_ns=freeze["budget"]["request_seconds"] * 10**9,
                             phase_timeout_ns=freeze["budget"]["batch_seconds"] * 10**9)
        remaining = c.utc(approval["candidate"]["expires_at_utc"]) - c.now()
        clock._tighten_phase_deadline(time.monotonic_ns() + int(remaining.total_seconds() * 10**9))
        with _lock:
            _owners[owner] = dict(**{k: value[k] for k in ("freeze", "approval", "digest", "pid", "output")},
                request=c.raw(request), receipt=receipt, clock=clock, clock_domain=domain,
                run_id="ITEM6-" + request["authorization_id_sha256"][:32].upper(), last_utc=c.now(),
                factory_taken=False, factory=None, case=None, case_identity=None, case_index=0,
                mapping_taken=False, mapping=None, stopped=None, closed=False)
        owner.check(source_check=True)
        return owner
    except BaseException as error:
        winner._invalidate()
        error.claim_may_exist = True
        raise
