"""独立于核心台账的运行有效期及原子配额存储。"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from researchops.tool_runtime import ToolRuntimeError


class StateStore:
    def __init__(self, database_path: str | Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS gateway_runs (
                    run_id TEXT PRIMARY KEY,
                    created_at_utc TEXT NOT NULL,
                    expires_at_utc TEXT NOT NULL,
                    ttl_seconds INTEGER NOT NULL,
                    max_calls INTEGER NOT NULL,
                    call_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS gateway_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            connection.execute(
                "INSERT OR IGNORE INTO gateway_metadata(key,value) VALUES ('management_run_id',?)",
                (str(uuid.uuid4()),),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @property
    def management_run_id(self) -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM gateway_metadata WHERE key='management_run_id'").fetchone()
        return str(row["value"])

    def create_run(self, run_id: str, *, ttl_seconds: int, max_calls: int) -> dict[str, Any]:
        self.validate_run_id(run_id)
        if type(ttl_seconds) is not int or ttl_seconds <= 0 or type(max_calls) is not int or max_calls <= 0:
            raise ValueError("运行有效期及调用上限必须为正整数。")
        now = self.now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO gateway_runs(run_id,created_at_utc,expires_at_utc,ttl_seconds,max_calls) VALUES (?,?,?,?,?)",
                (run_id, now.isoformat(), (now + timedelta(seconds=ttl_seconds)).isoformat(), ttl_seconds, max_calls),
            )
        return self.check_run(run_id)

    @staticmethod
    def validate_run_id(run_id: Any) -> str:
        if not isinstance(run_id, str):
            raise ToolRuntimeError("gateway_run_invalid", "运行句柄无效。")
        try:
            parsed = uuid.UUID(run_id)
        except (ValueError, AttributeError):
            raise ToolRuntimeError("gateway_run_invalid", "运行句柄无效。") from None
        if parsed.version != 4 or str(parsed) != run_id:
            raise ToolRuntimeError("gateway_run_invalid", "运行句柄无效。")
        return run_id

    def _checked_row(self, connection: sqlite3.Connection, run_id: Any) -> dict[str, Any]:
        self.validate_run_id(run_id)
        row = connection.execute("SELECT * FROM gateway_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ToolRuntimeError("gateway_run_not_found", "运行句柄不存在。")
        result = dict(row)
        if datetime.fromisoformat(result["expires_at_utc"]) <= self.now():
            raise ToolRuntimeError("gateway_run_expired", "运行已过期，请创建新运行。")
        return result

    def check_run(self, run_id: Any) -> dict[str, Any]:
        with self._connect() as connection:
            return self._checked_row(connection, run_id)

    def consume_call(self, run_id: Any) -> dict[str, Any]:
        # 同一运行的多个进程先串行扣减，再允许进入受控执行器。
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._checked_row(connection, run_id)
            if row["call_count"] >= row["max_calls"]:
                raise ToolRuntimeError("gateway_rate_limited", "该运行的工具调用次数已达到上限。")
            connection.execute("UPDATE gateway_runs SET call_count=call_count+1 WHERE run_id=?", (run_id,))
            row["call_count"] += 1
            return row

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM gateway_runs ORDER BY created_at_utc,run_id")]

    def list_run_ids(self) -> list[str]:
        return [row["run_id"] for row in self.list_runs()]
