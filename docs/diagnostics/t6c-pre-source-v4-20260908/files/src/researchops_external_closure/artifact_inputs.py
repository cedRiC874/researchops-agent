"""Read-only extraction of already container-verified closure evidence.

The public artifact verifier intentionally returns a content-free summary.  The
layer-A evaluator nevertheless needs the exact persisted JSON objects and audit
rows in order to recompute their semantics.  This module bridges those two
requirements without returning mutable caller-owned values: it checks that a
second bounded snapshot has the exact commitments already scanned by the
container verifier, reads SQLite through an immutable read-only connection,
checks the same byte snapshot again, and stores all semantic inputs as private
canonical bytes.

It has no write, clock, environment, Provider, key, or network capability.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifacts import (
    ArtifactBundleSummary,
    VerifiedPostrunFacts,
    verify_artifact_bundle,
)
from .errors import ExternalClosurePrimitiveError
from .io import decode_canonical_json_file, read_exact_artifact_directory
from .primitives import canonical_json_bytes


_MAX_FILE_BYTES = 100_000_000
_MAX_TOTAL_BYTES = 100_000_000
_INPUT_TOKEN = object()


def _fail(code: str) -> None:
    raise ExternalClosurePrimitiveError(code)


def _canonical_sequence_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    try:
        return canonical_json_bytes([dict(row) for row in rows])
    except ExternalClosurePrimitiveError:
        raise
    except Exception:
        _fail("external_closure_audit_rows_invalid")


def _decode_private_object(payload: bytes | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError):  # pragma: no cover - constructor invariant
        _fail("external_closure_artifact_snapshot_invalid")
    if type(value) is not dict:  # pragma: no cover - constructor invariant
        _fail("external_closure_artifact_snapshot_invalid")
    return value


def _decode_private_rows(payload: bytes) -> tuple[dict[str, Any], ...]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError):  # pragma: no cover - constructor invariant
        _fail("external_closure_artifact_snapshot_invalid")
    if type(value) is not list or any(type(row) is not dict for row in value):
        _fail("external_closure_artifact_snapshot_invalid")
    return tuple(dict(row) for row in value)


@dataclass(frozen=True, slots=True, init=False)
class ArtifactEvaluationInputs:
    """Opaque immutable semantic inputs derived from one stable byte snapshot."""

    summary: ArtifactBundleSummary
    _audit_index_bytes: bytes | None = field(repr=False, compare=False)
    _telemetry_bytes: bytes | None = field(repr=False, compare=False)
    _run_rows_bytes: bytes = field(repr=False, compare=False)
    _event_rows_bytes: bytes = field(repr=False, compare=False)
    _model_call_rows_bytes: bytes = field(repr=False, compare=False)
    tool_call_count: int
    tool_attempt_count: int
    approval_decision_count: int
    _verification_token: object = field(repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("ArtifactEvaluationInputs requires verified artifact bytes")

    @classmethod
    def _create(
        cls,
        token: object,
        *,
        summary: ArtifactBundleSummary,
        audit_index: Mapping[str, Any] | None,
        telemetry: Mapping[str, Any] | None,
        run_rows: Sequence[Mapping[str, Any]],
        event_rows: Sequence[Mapping[str, Any]],
        model_call_rows: Sequence[Mapping[str, Any]],
        tool_call_count: int,
        tool_attempt_count: int,
        approval_decision_count: int,
    ) -> "ArtifactEvaluationInputs":
        if token is not _INPUT_TOKEN or type(summary) is not ArtifactBundleSummary:
            raise TypeError("artifact_evaluation_inputs_construction_forbidden")
        instance = object.__new__(cls)
        object.__setattr__(instance, "summary", summary)
        object.__setattr__(
            instance,
            "_audit_index_bytes",
            None if audit_index is None else canonical_json_bytes(dict(audit_index)),
        )
        object.__setattr__(
            instance,
            "_telemetry_bytes",
            None if telemetry is None else canonical_json_bytes(dict(telemetry)),
        )
        object.__setattr__(
            instance, "_run_rows_bytes", _canonical_sequence_bytes(run_rows)
        )
        object.__setattr__(
            instance, "_event_rows_bytes", _canonical_sequence_bytes(event_rows)
        )
        object.__setattr__(
            instance,
            "_model_call_rows_bytes",
            _canonical_sequence_bytes(model_call_rows),
        )
        for name, value in (
            ("tool_call_count", tool_call_count),
            ("tool_attempt_count", tool_attempt_count),
            ("approval_decision_count", approval_decision_count),
        ):
            if type(value) is not int or value < 0:
                raise TypeError("artifact_evaluation_count_invalid")
            object.__setattr__(instance, name, value)
        object.__setattr__(instance, "_verification_token", token)
        return instance

    def _assert_verified(self) -> None:
        if self._verification_token is not _INPUT_TOKEN:
            _fail("external_closure_artifact_snapshot_invalid")

    def _semantic_inputs(self) -> tuple[
        dict[str, Any] | None,
        dict[str, Any] | None,
        tuple[dict[str, Any], ...],
        tuple[dict[str, Any], ...],
        tuple[dict[str, Any], ...],
    ]:
        """Return fresh copies only to the in-process layer-A evaluator."""

        self._assert_verified()
        return (
            _decode_private_object(self._telemetry_bytes),
            _decode_private_object(self._audit_index_bytes),
            _decode_private_rows(self._run_rows_bytes),
            _decode_private_rows(self._event_rows_bytes),
            _decode_private_rows(self._model_call_rows_bytes),
        )


def _row_dicts(
    connection: sqlite3.Connection, statement: str, *, max_rows: int
) -> tuple[dict[str, Any], ...]:
    if type(max_rows) is not int or max_rows < 1:
        _fail("external_closure_audit_rows_invalid")
    try:
        cursor = connection.execute(statement)
        names = tuple(item[0] for item in cursor.description or ())
        # Bound materialization before creating Python dictionaries. Limiting
        # only in the later semantic verifier would allocate the whole table.
        rows = cursor.fetchmany(max_rows + 1)
    except sqlite3.Error:
        _fail("external_closure_audit_rows_invalid")
    if len(rows) > max_rows:
        _fail("external_closure_audit_rows_invalid")
    result: list[dict[str, Any]] = []
    for row in rows:
        if len(row) != len(names):
            _fail("external_closure_audit_rows_invalid")
        candidate = dict(zip(names, row))
        if any(
            type(value) is float and not math.isfinite(value)
            for value in candidate.values()
        ):
            _fail("external_closure_audit_rows_invalid")
        result.append(candidate)
    return tuple(result)


def _scalar_count(connection: sqlite3.Connection, table: str) -> int:
    if table not in {"tool_calls", "tool_attempts", "approval_decisions"}:
        _fail("external_closure_audit_rows_invalid")
    try:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    except sqlite3.Error:
        _fail("external_closure_audit_rows_invalid")
    if row is None or len(row) != 1 or type(row[0]) is not int or row[0] < 0:
        _fail("external_closure_audit_rows_invalid")
    return row[0]


def _assert_sqlite_connection_bytes(
    connection: sqlite3.Connection, expected_payload: bytes
) -> None:
    try:
        observed = connection.serialize()
    except sqlite3.Error:
        _fail("external_closure_bundle_hash_mismatch")
    if type(observed) is not bytes or observed != expected_payload:
        _fail("external_closure_bundle_hash_mismatch")


def _read_database_rows(
    path: Path,
    *,
    expected_payload: bytes,
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    int,
    int,
    int,
]:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            path.absolute().as_uri() + "?mode=ro&immutable=1",
            uri=True,
            timeout=1.0,
        )
        _assert_sqlite_connection_bytes(connection, expected_payload)
        connection.execute("PRAGMA query_only = ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or tuple(query_only) != (1,):
            _fail("external_closure_audit_rows_invalid")
        runs = _row_dicts(
            connection,
            "SELECT run_id,mode,status,request_sha256,dataset_sha256,"
            "created_at_utc,updated_at_utc,terminal_error_code FROM runs "
            "ORDER BY run_id COLLATE BINARY",
            max_rows=100,
        )
        events = _row_dicts(
            connection,
            "SELECT event_id,run_id,sequence,event_type,occurred_at_utc,"
            "actor_kind,safe_payload_json,prev_hash,event_hash FROM audit_events "
            "ORDER BY run_id COLLATE BINARY,sequence",
            max_rows=20_000,
        )
        model_calls = _row_dicts(
            connection,
            "SELECT model_call_id,run_id,provider,model,started_at_utc,latency_ms,"
            "input_tokens,output_tokens,cached_tokens,cost_usd,outcome,error_code "
            "FROM model_calls ORDER BY run_id COLLATE BINARY,started_at_utc,"
            "model_call_id COLLATE BINARY",
            max_rows=800,
        )
        counts = (
            _scalar_count(connection, "tool_calls"),
            _scalar_count(connection, "tool_attempts"),
            _scalar_count(connection, "approval_decisions"),
        )
        # Bind all extracted logical rows to the exact database bytes already
        # scanned and committed by the container verifier.  A pathname re-read
        # alone cannot detect a temporary swap confined to the SQLite query.
        _assert_sqlite_connection_bytes(connection, expected_payload)
    except ExternalClosurePrimitiveError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError):
        _fail("external_closure_audit_rows_invalid")
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                _fail("external_closure_audit_rows_invalid")
    return runs, events, model_calls, *counts


def _assert_same_commitments(
    payloads: Mapping[str, bytes], summary: ArtifactBundleSummary
) -> None:
    expected = {
        item.name: (item.byte_count, item.sha256) for item in summary.file_commitments
    }
    observed = {
        name: (
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        )
        for name, payload in payloads.items()
    }
    if observed != expected:
        _fail("external_closure_artifact_bundle_identity_changed")


def extract_artifact_evaluation_inputs(
    artifact_directory: Path | None,
    *,
    postrun_facts: VerifiedPostrunFacts,
    schemas: Mapping[str, Mapping[str, Any]],
) -> ArtifactEvaluationInputs:
    """Verify a bundle and snapshot the exact same committed evidence for layer A."""

    summary = verify_artifact_bundle(
        artifact_directory,
        postrun_facts=postrun_facts,
        schemas=schemas,
    )
    if summary.artifact_publication_disposition != "normal":
        return ArtifactEvaluationInputs._create(
            _INPUT_TOKEN,
            summary=summary,
            audit_index=None,
            telemetry=None,
            run_rows=(),
            event_rows=(),
            model_call_rows=(),
            tool_call_count=0,
            tool_attempt_count=0,
            approval_decision_count=0,
        )
    if not isinstance(artifact_directory, Path):
        _fail("external_closure_artifact_directory_required")
    expected_names = tuple(item.name for item in summary.file_commitments)
    if not expected_names:
        return ArtifactEvaluationInputs._create(
            _INPUT_TOKEN,
            summary=summary,
            audit_index=None,
            telemetry=None,
            run_rows=(),
            event_rows=(),
            model_call_rows=(),
            tool_call_count=0,
            tool_attempt_count=0,
            approval_decision_count=0,
        )
    payloads = read_exact_artifact_directory(
        artifact_directory,
        expected_names=expected_names,
        max_file_bytes=_MAX_FILE_BYTES,
        max_total_bytes=_MAX_TOTAL_BYTES,
    )
    _assert_same_commitments(payloads, summary)

    audit_index = (
        decode_canonical_json_file(
            payloads["phase6_audit_index.json"], max_bytes=_MAX_FILE_BYTES
        )
        if "phase6_audit_index.json" in payloads
        else None
    )
    telemetry = (
        decode_canonical_json_file(
            payloads["phase6_completion_telemetry.json"],
            max_bytes=_MAX_FILE_BYTES,
        )
        if "phase6_completion_telemetry.json" in payloads
        else None
    )
    if "phase6_audit.sqlite3" in payloads:
        (
            runs,
            events,
            model_calls,
            tool_calls,
            tool_attempts,
            approval_decisions,
        ) = _read_database_rows(
            artifact_directory / "phase6_audit.sqlite3",
            expected_payload=payloads["phase6_audit.sqlite3"],
        )
    else:
        runs = events = model_calls = ()
        tool_calls = tool_attempts = approval_decisions = 0

    final_payloads = read_exact_artifact_directory(
        artifact_directory,
        expected_names=expected_names,
        max_file_bytes=_MAX_FILE_BYTES,
        max_total_bytes=_MAX_TOTAL_BYTES,
    )
    _assert_same_commitments(final_payloads, summary)
    if any(final_payloads[name] != payloads[name] for name in expected_names):
        _fail("external_closure_artifact_bundle_identity_changed")
    return ArtifactEvaluationInputs._create(
        _INPUT_TOKEN,
        summary=summary,
        audit_index=audit_index,
        telemetry=telemetry,
        run_rows=runs,
        event_rows=events,
        model_call_rows=model_calls,
        tool_call_count=tool_calls,
        tool_attempt_count=tool_attempts,
        approval_decision_count=approval_decisions,
    )


__all__ = ["extract_artifact_evaluation_inputs"]
