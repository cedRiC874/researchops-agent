from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


AUDIT_SCHEMA_VERSION = "1.0"
ZERO_HASH = "0" * 64
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|token|secret|password|authorization|cookie|credential|connection[_-]?string)",
    re.IGNORECASE,
)
_SAFE_USAGE_KEYS = {
    "cached_tokens",
    "input_tokens",
    "output_tokens",
    "total_tokens",
}
_ROW_CONTAINER_KEY = re.compile(
    r"(?:records?|rows?|sample_values?|dataframe|csv_text|raw_data)", re.IGNORECASE
)
_PARTICIPANT_ID = re.compile(r"\bP\d{3,}\b", re.IGNORECASE)
_API_KEY_VALUE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_WINDOWS_PATH = re.compile(r"(?i)(?:[A-Z]:\\|\\\\)[^\r\n\t\"']+")


class AuditError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict[str, str]:
        return {"status": "error", "error_code": self.code, "message": str(self)}


@dataclass(frozen=True)
class ChainVerification:
    valid: bool
    run_id: str
    event_count: int
    chain_head: str
    error_code: str | None = None
    error_sequence: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "run_id": self.run_id,
            "event_count": self.event_count,
            "chain_head": self.chain_head,
            "error_code": self.error_code,
            "error_sequence": self.error_sequence,
        }


def canonical_json(value: Any) -> str:
    """Return the single canonical JSON representation used for fingerprints."""

    normalized = _normalize_json(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def safe_audit_value(value: Any, *, _depth: int = 0) -> Any:
    """Defensive scrubber; tool-specific allowlists remain the primary control."""

    if _depth > 8:
        return "[MAX_DEPTH_OMITTED]"
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AuditError("audit_non_finite_value", "审计字段不能包含 NaN 或 Infinity。")
        return value
    if isinstance(value, Path):
        return f"[PATH_REDACTED]/{value.name}"
    if isinstance(value, str):
        cleaned = unicodedata.normalize("NFC", value)
        cleaned = _API_KEY_VALUE.sub("[SECRET_REDACTED]", cleaned)
        cleaned = _PARTICIPANT_ID.sub("[ROW_ID_REDACTED]", cleaned)
        cleaned = _WINDOWS_PATH.sub("[ABSOLUTE_PATH_REDACTED]", cleaned)
        if len(cleaned) > 4096:
            cleaned = cleaned[:4096] + "[TRUNCATED]"
        return cleaned
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key in sorted(value, key=lambda item: str(item))[:256]:
            key = unicodedata.normalize("NFC", str(raw_key))
            item = value[raw_key]
            if _SECRET_KEY.search(key):
                is_safe_usage_metric = (
                    key.casefold() in _SAFE_USAGE_KEYS
                    and (
                        item is None
                        or (
                            isinstance(item, int)
                            and not isinstance(item, bool)
                            and item >= 0
                        )
                    )
                )
                result[key] = (
                    safe_audit_value(item, _depth=_depth + 1)
                    if is_safe_usage_metric
                    else "[SECRET_REDACTED]"
                )
            elif _ROW_CONTAINER_KEY.fullmatch(key):
                result[key] = "[ROW_DATA_OMITTED]"
            else:
                result[key] = safe_audit_value(item, _depth=_depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [safe_audit_value(item, _depth=_depth + 1) for item in value[:256]]
    raise AuditError(
        "audit_value_not_serializable",
        f"不允许把 {type(value).__name__} 写入审计账本。",
    )


class AuditLedger:
    """SQLite event ledger with append-only, per-run SHA-256 chains."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    dataset_sha256 TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    terminal_error_code TEXT
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at_utc TEXT NOT NULL,
                    actor_kind TEXT NOT NULL,
                    safe_payload_json TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    UNIQUE(run_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                    call_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    tool_name TEXT NOT NULL,
                    tool_version TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    risk_class TEXT NOT NULL,
                    safe_args_json TEXT NOT NULL,
                    args_hash TEXT NOT NULL,
                    approval_scope_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    safe_result_summary_json TEXT,
                    result_hash TEXT,
                    terminal_error_code TEXT
                );

                CREATE TABLE IF NOT EXISTS tool_attempts (
                    call_id TEXT NOT NULL REFERENCES tool_calls(call_id),
                    attempt_no INTEGER NOT NULL,
                    started_at_utc TEXT NOT NULL,
                    ended_at_utc TEXT,
                    outcome TEXT NOT NULL,
                    duration_ms REAL,
                    retryable INTEGER,
                    error_code TEXT,
                    safe_error_message TEXT,
                    backoff_ms INTEGER,
                    result_hash TEXT,
                    PRIMARY KEY(call_id, attempt_no)
                );

                CREATE TABLE IF NOT EXISTS approval_decisions (
                    approval_id TEXT PRIMARY KEY,
                    call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls(call_id),
                    approval_scope_hash TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK(decision IN ('approve', 'reject')),
                    approver_subject_hash TEXT NOT NULL,
                    requested_at_utc TEXT NOT NULL,
                    decided_at_utc TEXT NOT NULL,
                    expires_at_utc TEXT,
                    safe_reason TEXT
                );

                CREATE TABLE IF NOT EXISTS model_calls (
                    model_call_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    started_at_utc TEXT NOT NULL,
                    latency_ms REAL NOT NULL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cached_tokens INTEGER,
                    cost_usd REAL,
                    outcome TEXT NOT NULL,
                    error_code TEXT
                );

                CREATE TRIGGER IF NOT EXISTS audit_events_no_update
                BEFORE UPDATE ON audit_events BEGIN
                    SELECT RAISE(ABORT, 'audit_events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
                BEFORE DELETE ON audit_events BEGIN
                    SELECT RAISE(ABORT, 'audit_events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS approvals_no_update
                BEFORE UPDATE ON approval_decisions BEGIN
                    SELECT RAISE(ABORT, 'approval decisions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS approvals_no_delete
                BEFORE DELETE ON approval_decisions BEGIN
                    SELECT RAISE(ABORT, 'approval decisions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS tool_identity_no_update
                BEFORE UPDATE OF run_id, tool_name, tool_version, policy_version,
                    safe_args_json, args_hash, approval_scope_hash, idempotency_key
                ON tool_calls BEGIN
                    SELECT RAISE(ABORT, 'tool call identity is immutable');
                END;
                """
            )

    def start_run(
        self,
        *,
        mode: str,
        request_summary: Mapping[str, Any],
        dataset_sha256: str | None = None,
        run_id: str | None = None,
    ) -> str:
        identifier = run_id or f"RUN-AUDIT-{uuid.uuid4().hex[:16].upper()}"
        now = self._now()
        request_hash = sha256_json(request_summary)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO runs(
                    run_id, mode, status, request_sha256, dataset_sha256,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, 'running', ?, ?, ?, ?)""",
                (identifier, mode, request_hash, dataset_sha256, now, now),
            )
            self._append_event_tx(
                connection,
                identifier,
                "run_started",
                {"mode": mode, "request_sha256": request_hash, "dataset_sha256": dataset_sha256},
                actor_kind="system",
                occurred_at=now,
            )
        return identifier

    def set_run_status(
        self,
        run_id: str,
        status: str,
        *,
        terminal_error_code: str | None = None,
    ) -> None:
        allowed = {
            "running": {"waiting_approval", "completed", "failed", "cancelled"},
            "waiting_approval": {"running", "failed", "cancelled"},
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise AuditError("audit_run_not_found", "找不到审计运行。")
            current = str(row["status"])
            if status == current:
                return
            if status not in allowed.get(current, set()):
                raise AuditError(
                    "audit_invalid_run_transition",
                    f"运行状态不能从 {current} 迁移到 {status}。",
                )
            now = self._now()
            connection.execute(
                """UPDATE runs SET status = ?, updated_at_utc = ?,
                    terminal_error_code = ? WHERE run_id = ?""",
                (status, now, terminal_error_code, run_id),
            )
            self._append_event_tx(
                connection,
                run_id,
                "run_status_changed",
                {"from": current, "to": status, "error_code": terminal_error_code},
                actor_kind="system",
                occurred_at=now,
            )

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        actor_kind: str = "system",
    ) -> str:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._append_event_tx(
                connection,
                run_id,
                event_type,
                payload,
                actor_kind=actor_kind,
                occurred_at=self._now(),
            )

    def create_tool_call(
        self,
        *,
        call_id: str,
        run_id: str,
        tool_name: str,
        tool_version: str,
        policy_version: str,
        risk_class: str,
        safe_args: Mapping[str, Any],
        args_hash: str,
        approval_scope_hash: str,
        requires_approval: bool,
    ) -> None:
        now = self._now()
        status = "awaiting_approval" if requires_approval else "ready"
        safe_args_json = canonical_json(safe_audit_value(safe_args))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO tool_calls(
                    call_id, run_id, tool_name, tool_version, policy_version,
                    risk_class, safe_args_json, args_hash, approval_scope_hash,
                    idempotency_key, status, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    call_id,
                    run_id,
                    tool_name,
                    tool_version,
                    policy_version,
                    risk_class,
                    safe_args_json,
                    args_hash,
                    approval_scope_hash,
                    call_id,
                    status,
                    now,
                    now,
                ),
            )
            self._append_event_tx(
                connection,
                run_id,
                "tool_call_proposed",
                {
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "tool_version": tool_version,
                    "policy_version": policy_version,
                    "risk_class": risk_class,
                    "args_hash": args_hash,
                    "approval_scope_hash": approval_scope_hash,
                    "status": status,
                },
                actor_kind="agent",
                occurred_at=now,
            )
            if requires_approval:
                self._append_event_tx(
                    connection,
                    run_id,
                    "tool_approval_requested",
                    {
                        "call_id": call_id,
                        "tool_name": tool_name,
                        "approval_scope_hash": approval_scope_hash,
                    },
                    actor_kind="system",
                    occurred_at=now,
                )

    def record_approval(
        self,
        call_id: str,
        *,
        approval_scope_hash: str,
        decision: str,
        approver: str,
        reason: str | None,
        expires_at_utc: str | None,
    ) -> None:
        if decision not in {"approve", "reject"}:
            raise AuditError("tool_approval_decision_invalid", "审批决定必须是 approve 或 reject。")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tool_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if row is None:
                raise AuditError("tool_call_not_found", "找不到工具调用。")
            if row["status"] != "awaiting_approval":
                raise AuditError("tool_call_state_conflict", "工具调用不处于等待审批状态。")
            if not _secure_equal(str(row["approval_scope_hash"]), approval_scope_hash):
                raise AuditError("tool_approval_mismatch", "审批范围与工具调用不匹配。")
            approval_id = f"APR-{uuid.uuid4().hex[:16].upper()}"
            approver_hash = hashlib.sha256(
                unicodedata.normalize("NFC", approver).encode("utf-8")
            ).hexdigest()
            connection.execute(
                """INSERT INTO approval_decisions(
                    approval_id, call_id, approval_scope_hash, decision,
                    approver_subject_hash, requested_at_utc, decided_at_utc,
                    expires_at_utc, safe_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    approval_id,
                    call_id,
                    approval_scope_hash,
                    decision,
                    approver_hash,
                    row["created_at_utc"],
                    now,
                    expires_at_utc,
                    safe_audit_value(reason) if reason else None,
                ),
            )
            target_status = "approved" if decision == "approve" else "rejected"
            connection.execute(
                "UPDATE tool_calls SET status = ?, updated_at_utc = ? WHERE call_id = ?",
                (target_status, now, call_id),
            )
            self._append_event_tx(
                connection,
                str(row["run_id"]),
                "tool_approval_decided",
                {
                    "call_id": call_id,
                    "decision": decision,
                    "approver_subject_hash": approver_hash,
                    "approval_scope_hash": approval_scope_hash,
                    "expires_at_utc": expires_at_utc,
                },
                actor_kind="human",
                occurred_at=now,
            )

    def expire_tool_call(self, call_id: str) -> None:
        self._terminal_without_attempt(call_id, "expired", "tool_approval_expired")

    def fail_tool_call_without_attempt(self, call_id: str, error_code: str) -> None:
        self._terminal_without_attempt(call_id, "failed", error_code)

    def _terminal_without_attempt(self, call_id: str, status: str, error_code: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT run_id, status FROM tool_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if row is None:
                raise AuditError("tool_call_not_found", "找不到工具调用。")
            if row["status"] in {"succeeded", "failed", "rejected", "expired", "outcome_unknown"}:
                raise AuditError("tool_call_state_conflict", "工具调用已处于终态。")
            now = self._now()
            connection.execute(
                """UPDATE tool_calls SET status = ?, terminal_error_code = ?,
                    updated_at_utc = ? WHERE call_id = ?""",
                (status, error_code, now, call_id),
            )
            self._append_event_tx(
                connection,
                str(row["run_id"]),
                "tool_call_terminal",
                {"call_id": call_id, "status": status, "error_code": error_code},
                actor_kind="system",
                occurred_at=now,
            )

    def start_attempt(self, call_id: str) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT run_id, status, attempt_count FROM tool_calls WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            if row is None:
                raise AuditError("tool_call_not_found", "找不到工具调用。")
            if row["status"] not in {"ready", "approved", "retry_wait"}:
                raise AuditError("tool_call_state_conflict", "当前工具状态不允许开始执行。")
            attempt_no = int(row["attempt_count"]) + 1
            now = self._now()
            updated = connection.execute(
                """UPDATE tool_calls SET status = 'running', attempt_count = ?,
                    updated_at_utc = ? WHERE call_id = ? AND status = ?""",
                (attempt_no, now, call_id, row["status"]),
            )
            if updated.rowcount != 1:
                raise AuditError("tool_call_state_conflict", "工具调用已被另一执行者占用。")
            connection.execute(
                """INSERT INTO tool_attempts(
                    call_id, attempt_no, started_at_utc, outcome
                ) VALUES (?, ?, ?, 'running')""",
                (call_id, attempt_no, now),
            )
            self._append_event_tx(
                connection,
                str(row["run_id"]),
                "tool_attempt_started",
                {"call_id": call_id, "attempt_no": attempt_no},
                actor_kind="tool_runtime",
                occurred_at=now,
            )
        return attempt_no

    def finish_attempt_success(
        self,
        call_id: str,
        attempt_no: int,
        *,
        duration_ms: float,
        safe_result: Mapping[str, Any],
        result_hash: str,
    ) -> None:
        now = self._now()
        safe_result_json = canonical_json(safe_audit_value(safe_result))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_running_attempt(connection, call_id, attempt_no)
            connection.execute(
                """UPDATE tool_attempts SET ended_at_utc = ?, outcome = 'succeeded',
                    duration_ms = ?, retryable = 0, result_hash = ?
                    WHERE call_id = ? AND attempt_no = ?""",
                (now, duration_ms, result_hash, call_id, attempt_no),
            )
            connection.execute(
                """UPDATE tool_calls SET status = 'succeeded', updated_at_utc = ?,
                    safe_result_summary_json = ?, result_hash = ?, terminal_error_code = NULL
                    WHERE call_id = ?""",
                (now, safe_result_json, result_hash, call_id),
            )
            self._append_event_tx(
                connection,
                str(row["run_id"]),
                "tool_attempt_succeeded",
                {
                    "call_id": call_id,
                    "attempt_no": attempt_no,
                    "duration_ms": duration_ms,
                    "result_hash": result_hash,
                },
                actor_kind="tool_runtime",
                occurred_at=now,
            )

    def finish_attempt_failure(
        self,
        call_id: str,
        attempt_no: int,
        *,
        duration_ms: float,
        error_code: str,
        safe_error_message: str,
        retryable: bool,
        will_retry: bool,
        backoff_ms: int | None,
        outcome_unknown: bool = False,
        terminal_error_code: str | None = None,
    ) -> None:
        now = self._now()
        target_status = "retry_wait" if will_retry else (
            "outcome_unknown" if outcome_unknown else "failed"
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_running_attempt(connection, call_id, attempt_no)
            connection.execute(
                """UPDATE tool_attempts SET ended_at_utc = ?, outcome = ?,
                    duration_ms = ?, retryable = ?, error_code = ?,
                    safe_error_message = ?, backoff_ms = ?
                    WHERE call_id = ? AND attempt_no = ?""",
                (
                    now,
                    "failed",
                    duration_ms,
                    int(retryable),
                    error_code,
                    safe_audit_value(safe_error_message),
                    backoff_ms,
                    call_id,
                    attempt_no,
                ),
            )
            connection.execute(
                """UPDATE tool_calls SET status = ?, updated_at_utc = ?,
                    terminal_error_code = ? WHERE call_id = ?""",
                (
                    target_status,
                    now,
                    None if will_retry else (terminal_error_code or error_code),
                    call_id,
                ),
            )
            self._append_event_tx(
                connection,
                str(row["run_id"]),
                "tool_attempt_failed",
                {
                    "call_id": call_id,
                    "attempt_no": attempt_no,
                    "duration_ms": duration_ms,
                    "error_code": error_code,
                    "retryable": retryable,
                    "will_retry": will_retry,
                    "outcome_unknown": outcome_unknown,
                    "terminal_error_code": (
                        None if will_retry else (terminal_error_code or error_code)
                    ),
                },
                actor_kind="tool_runtime",
                occurred_at=now,
            )
            if will_retry:
                self._append_event_tx(
                    connection,
                    str(row["run_id"]),
                    "tool_retry_scheduled",
                    {
                        "call_id": call_id,
                        "after_attempt": attempt_no,
                        "backoff_ms": backoff_ms,
                    },
                    actor_kind="tool_runtime",
                    occurred_at=now,
                )

    def get_tool_call(self, call_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
        if row is None:
            raise AuditError("tool_call_not_found", "找不到工具调用。")
        result = dict(row)
        result["safe_args"] = json.loads(result.pop("safe_args_json"))
        raw_result = result.pop("safe_result_summary_json")
        result["safe_result"] = json.loads(raw_result) if raw_result else None
        return result

    def record_model_call(
        self,
        run_id: str,
        *,
        provider: str,
        model: str,
        started_at_utc: str,
        latency_ms: float,
        input_tokens: int | None,
        output_tokens: int | None,
        cached_tokens: int | None,
        cost_usd: float | None,
        outcome: str,
        error_code: str | None = None,
    ) -> str:
        if not math.isfinite(latency_ms) or latency_ms < 0:
            raise AuditError("model_latency_invalid", "模型延迟必须是有限的非负数。")
        for token_value in (input_tokens, output_tokens, cached_tokens):
            if token_value is not None and token_value < 0:
                raise AuditError("model_usage_invalid", "模型 token 计数不能为负数。")
        if cost_usd is not None and (not math.isfinite(cost_usd) or cost_usd < 0):
            raise AuditError("model_cost_invalid", "模型成本必须是有限的非负数。")
        model_call_id = f"MODEL-{uuid.uuid4().hex[:16].upper()}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO model_calls(
                    model_call_id, run_id, provider, model, started_at_utc,
                    latency_ms, input_tokens, output_tokens, cached_tokens,
                    cost_usd, outcome, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    model_call_id,
                    run_id,
                    provider,
                    model,
                    started_at_utc,
                    latency_ms,
                    input_tokens,
                    output_tokens,
                    cached_tokens,
                    cost_usd,
                    outcome,
                    error_code,
                ),
            )
            self._append_event_tx(
                connection,
                run_id,
                "model_call_recorded",
                {
                    "model_call_id": model_call_id,
                    "provider": provider,
                    "model": model,
                    "latency_ms": latency_ms,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cached_tokens": cached_tokens,
                    "cost_usd": cost_usd,
                    "outcome": outcome,
                    "error_code": error_code,
                },
                actor_kind="agent_sdk",
                occurred_at=self._now(),
            )
        return model_call_id

    def get_approval(self, call_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE call_id = ?", (call_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise AuditError("audit_run_not_found", "找不到审计运行。")
        return dict(row)

    def list_attempts(self, call_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_attempts WHERE call_id = ? ORDER BY attempt_no", (call_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def verify_chain(self, run_id: str) -> ChainVerification:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE run_id = ? ORDER BY sequence", (run_id,)
            ).fetchall()
        previous = ZERO_HASH
        for expected_sequence, row in enumerate(rows, start=1):
            sequence = int(row["sequence"])
            if sequence != expected_sequence:
                return ChainVerification(
                    False, run_id, len(rows), previous, "audit_sequence_gap", sequence
                )
            if not _secure_equal(str(row["prev_hash"]), previous):
                return ChainVerification(
                    False, run_id, len(rows), previous, "audit_prev_hash_mismatch", sequence
                )
            expected_hash = _event_hash(
                run_id=run_id,
                sequence=sequence,
                event_type=str(row["event_type"]),
                occurred_at_utc=str(row["occurred_at_utc"]),
                actor_kind=str(row["actor_kind"]),
                safe_payload_json=str(row["safe_payload_json"]),
                prev_hash=previous,
            )
            if not _secure_equal(str(row["event_hash"]), expected_hash):
                return ChainVerification(
                    False, run_id, len(rows), previous, "audit_event_hash_mismatch", sequence
                )
            previous = expected_hash
        return ChainVerification(True, run_id, len(rows), previous)

    def export_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise AuditError("audit_run_not_found", "找不到审计运行。")
            calls = connection.execute(
                "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY created_at_utc, call_id",
                (run_id,),
            ).fetchall()
            call_ids = [str(row["call_id"]) for row in calls]
            attempts: list[sqlite3.Row] = []
            approvals: list[sqlite3.Row] = []
            for call_id in call_ids:
                attempts.extend(
                    connection.execute(
                        "SELECT * FROM tool_attempts WHERE call_id = ? ORDER BY attempt_no",
                        (call_id,),
                    ).fetchall()
                )
                approvals.extend(
                    connection.execute(
                        "SELECT * FROM approval_decisions WHERE call_id = ?", (call_id,)
                    ).fetchall()
                )
            events = connection.execute(
                "SELECT * FROM audit_events WHERE run_id = ? ORDER BY sequence", (run_id,)
            ).fetchall()
            model_calls = connection.execute(
                "SELECT * FROM model_calls WHERE run_id = ? ORDER BY started_at_utc",
                (run_id,),
            ).fetchall()

        call_payloads = []
        for row in calls:
            item = dict(row)
            item["safe_args"] = json.loads(item.pop("safe_args_json"))
            raw_result = item.pop("safe_result_summary_json")
            item["safe_result"] = json.loads(raw_result) if raw_result else None
            call_payloads.append(item)
        event_payloads = []
        for row in events:
            item = dict(row)
            item["safe_payload"] = json.loads(item.pop("safe_payload_json"))
            event_payloads.append(item)
        verification = self.verify_chain(run_id)
        return safe_audit_value(
            {
                "schema_version": AUDIT_SCHEMA_VERSION,
                "run": dict(run),
                "tool_calls": call_payloads,
                "tool_attempts": [dict(row) for row in attempts],
                "approval_decisions": [dict(row) for row in approvals],
                "model_calls": [dict(row) for row in model_calls],
                "events": event_payloads,
                "chain_verification": verification.to_dict(),
            }
        )

    def _require_running_attempt(
        self, connection: sqlite3.Connection, call_id: str, attempt_no: int
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT tc.run_id, tc.status, ta.outcome
            FROM tool_calls tc JOIN tool_attempts ta ON ta.call_id = tc.call_id
            WHERE tc.call_id = ? AND ta.attempt_no = ?""",
            (call_id, attempt_no),
        ).fetchone()
        if row is None or row["status"] != "running" or row["outcome"] != "running":
            raise AuditError("tool_call_state_conflict", "工具尝试不处于运行状态。")
        return row

    def _append_event_tx(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        actor_kind: str,
        occurred_at: str,
    ) -> str:
        last = connection.execute(
            """SELECT sequence, event_hash FROM audit_events
            WHERE run_id = ? ORDER BY sequence DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        if last is None:
            sequence, previous = 1, ZERO_HASH
        else:
            sequence, previous = int(last["sequence"]) + 1, str(last["event_hash"])
        safe_payload_json = canonical_json(safe_audit_value(payload))
        event_hash = _event_hash(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            occurred_at_utc=occurred_at,
            actor_kind=actor_kind,
            safe_payload_json=safe_payload_json,
            prev_hash=previous,
        )
        connection.execute(
            """INSERT INTO audit_events(
                run_id, sequence, event_type, occurred_at_utc, actor_kind,
                safe_payload_json, prev_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                sequence,
                event_type,
                occurred_at,
                actor_kind,
                safe_payload_json,
                previous,
                event_hash,
            ),
        )
        return event_hash

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()


def _normalize_json(value: Any) -> Any:
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AuditError("canonical_json_non_finite", "规范 JSON 不允许 NaN 或 Infinity。")
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Path):
        return unicodedata.normalize("NFC", str(value))
    if isinstance(value, Mapping):
        return {
            unicodedata.normalize("NFC", str(key)): _normalize_json(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_json(item) for item in value]
    raise AuditError(
        "canonical_json_unsupported_type",
        f"无法规范化 {type(value).__name__}。",
    )


def _event_hash(
    *,
    run_id: str,
    sequence: int,
    event_type: str,
    occurred_at_utc: str,
    actor_kind: str,
    safe_payload_json: str,
    prev_hash: str,
) -> str:
    payload = canonical_json(
        {
            "run_id": run_id,
            "sequence": sequence,
            "event_type": event_type,
            "occurred_at_utc": occurred_at_utc,
            "actor_kind": actor_kind,
            "safe_payload_json": safe_payload_json,
            "prev_hash": prev_hash,
        }
    )
    return hashlib.sha256(
        b"researchops.audit.v1\0" + payload.encode("utf-8")
    ).hexdigest()


def _secure_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
