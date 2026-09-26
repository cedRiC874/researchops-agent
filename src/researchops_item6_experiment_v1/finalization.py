"""Bounded completion evidence after scoring, export and sealing; no sending authority."""
import time
from . import contract as c


class FinalizationDeadline:
    def __init__(self, owner):
        self.owner = owner
        self.started_ns = time.monotonic_ns()
        self.remaining_ns = owner.clock.remaining_phase_ns()
        self.deadline_ns = self.started_ns + self.remaining_ns
        candidate = owner.snapshots()["approval"]["candidate"]
        self.expires = c.utc(candidate["expires_at_utc"])
        self.not_before = c.utc(candidate["not_before_utc"])
        self.last_utc = owner._state()["last_utc"]
        self.stage = "start"
        self.check("start")
        self.started_utc = self.last_utc

    def check(self, stage):
        self.stage = stage
        instant = c.now()
        c.require(self.last_utc <= instant and self.not_before <= instant < self.expires
                  and time.monotonic_ns() < self.deadline_ns, "finalization_deadline")
        self.last_utc = instant

    def finish(self, seal, status):
        self.check("terminal_before_write")
        record = dict(schema_version="item6-finalization/1.0", run_id=self.owner.run_id,
            mode=self.owner.mode, status=status, seal_sha256=seal,
            started_at_utc=self.started_utc.isoformat().replace("+00:00", "Z"),
            sealed_at_utc=self.last_utc.isoformat().replace("+00:00", "Z"),
            elapsed_ns=time.monotonic_ns()-self.started_ns, remaining_at_start_ns=self.remaining_ns,
            clock_source="local_monotonic_finalization", phase_scope="request_and_cleanup_before_archive")
        with (self.owner.output / "finalization.json").open("xb") as stream:
            stream.write(c.raw(record))
        self.check("terminal_after_write")
        return c.digest(c.raw(record))


def record_failure(owner, error, deadline):
    """Failure diagnostics may be written after expiry, never as successful completion."""
    phase = owner.clock.snapshot()
    value = dict(schema_version="item6-finalization-failure/1.0", status="failed", mode=owner.mode,
        run_id=owner.run_id, code=c.safe_error(error), stage=deadline.stage if deadline else "start",
        phase_closed=phase["closed"], phase_halted=phase["halted"],
        phase_terminal_ns=phase["phase"]["phase_terminal_ns"],
        observed_at_utc=c.now().isoformat().replace("+00:00", "Z"),
        seal_present=(owner.output / "sealed.json").exists(), runtime_authority_granted=False)
    try:
        with (owner.output / "finalization-failure.json").open("xb") as stream: stream.write(c.raw(value))
        error.finalization_failure_recorded = True
    except OSError:
        error.finalization_failure_recorded = False
