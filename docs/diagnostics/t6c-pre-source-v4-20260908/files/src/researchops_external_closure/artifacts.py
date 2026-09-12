"""Bounded, read-only verification of T6-C closure artifact bundles.

The module deliberately stops at the artifact-container boundary.  It proves the
post-run attestation shape, writer-prefix file set, public byte scan, canonical
JSON containers, manifest byte commitments, and immutable SQLite schema.  Audit
event and completion-record semantics belong to the later closure evaluator.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, NoReturn, TypeAlias

import jsonschema

from .errors import ExternalClosurePrimitiveError
from .io import (
    decode_canonical_json_file,
    read_exact_artifact_directory,
    scan_public_artifact_bytes,
)
from .primitives import canonical_json_bytes, decode_strict_json_object, parse_utc_timestamp
from .schema_guard import (
    LOCAL_ONLY_SCHEMA_REGISTRY,
    only_local_schema_references,
    snapshot_local_schema,
)


_MAX_POSTRUN_FACT_BYTES = 2_000_000
_MAX_ARTIFACT_FILE_BYTES = 100_000_000
_MAX_ARTIFACT_TOTAL_BYTES = 100_000_000
_REPARSE_POINT = 0x400

_POSTRUN_SCHEMA = "postrun_attested_facts_v1.schema.json"
_MANIFEST_SCHEMA = "closure_bundle_manifest_v1.schema.json"

_AUDIT_DATABASE = "phase6_audit.sqlite3"
_AUDIT_INDEX = "phase6_audit_index.json"
_COMPLETION_TELEMETRY = "phase6_completion_telemetry.json"
_MANIFEST = "completion_closure_manifest.json"
_WRITER_ORDER = (
    _AUDIT_DATABASE,
    _AUDIT_INDEX,
    _COMPLETION_TELEMETRY,
    _MANIFEST,
)
_EXPECTED_NAMES_BY_STAGE = {
    "not_started": (),
    "audit_database_written": (_AUDIT_DATABASE,),
    "audit_index_written": (_AUDIT_DATABASE, _AUDIT_INDEX),
    "completion_telemetry_written": (
        _AUDIT_DATABASE,
        _AUDIT_INDEX,
        _COMPLETION_TELEMETRY,
    ),
    "manifest_written": _WRITER_ORDER,
}

_SCHEMA_COMMITMENT_DOMAIN = (
    "researchops-provider-completion-audit-database-schema-v1"
)
_EXPECTED_SCHEMA_COMMITMENT_SHA256 = (
    "c0df1f54a96fe3c4f5ee196e654203a62e5e775a3e21fd5f47ace9e349169385"
)
_EXPECTED_SCHEMA_OBJECT_COUNT = 11
_EXPECTED_CLOSURE_EVIDENCE_COMMITMENT_SHA256 = (
    "a96b39a1f4bafa3bcf512fd88a5e7b0ca3987ca2bc9804642a1d1259cbf8cad0"
)

_ASCII_SQL_WHITESPACE = re.compile(rb"[\x09\x0a\x0b\x0c\x0d\x20]+")
_CASE_HANDLE = re.compile(r"^PCECASE-[A-F0-9]{32}$", re.ASCII)
_RUN_ID = re.compile(r"^PCERUN-[A-F0-9]{32}$", re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)

BundleStatus: TypeAlias = Literal[
    "complete", "manifest_present_invalid", "manifestless_failure"
]
PublicationDisposition: TypeAlias = Literal[
    "normal", "sensitive_quarantined", "privacy_unverified_quarantined"
]

_VERIFIED_POSTRUN_TOKEN = object()
_VERIFIED_FILE_TOKEN = object()
_VERIFIED_BUNDLE_TOKEN = object()


def _fail(code: str) -> NoReturn:
    raise ExternalClosurePrimitiveError(code)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _schema(
    schemas: Mapping[str, Mapping[str, Any]], name: str
) -> Mapping[str, Any]:
    if not isinstance(schemas, Mapping):
        _fail("external_closure_schema_mapping_invalid")
    candidate = schemas.get(name)
    if not isinstance(candidate, Mapping) or not only_local_schema_references(candidate):
        _fail("external_closure_schema_mapping_invalid")
    return candidate


def _validate_with_schema(
    value: object,
    *,
    schemas: Mapping[str, Mapping[str, Any]],
    schema_name: str,
    error_code: str,
) -> None:
    try:
        schema = snapshot_local_schema(_schema(schemas, schema_name))
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
            registry=LOCAL_ONLY_SCHEMA_REGISTRY,
        ).validate(value)
    except ExternalClosurePrimitiveError:
        raise
    except Exception:
        _fail(error_code)


_POSTRUN_FIELDS = (
    "schema_version",
    "campaign_id",
    "receipt_id",
    "completed_at_utc",
    "receipt_issued_at_utc",
    "terminal_stage",
    "local_terminal_outcome",
    "terminal_provider_outcome_state",
    "terminal_error_code",
    "last_completed_artifact_write_stage",
    "artifact_error_code",
    "task_released",
    "task_released_at_utc",
    "provider_key_loaded",
    "provider_key_loaded_at_utc",
    "released_task_bundle_commitment_sha256",
    "released_case_handle_order_commitment_sha256",
    "released_execution_input_order_commitment_sha256",
    "task_bundle_opening_verified_in_memory",
    "task_bundle_opening_persisted",
    "custodian_private_leak_canary_scan_status",
    "public_generic_privacy_scan_status",
    "canary_injection_verified_for_every_send",
    "database_origin_status",
    "database_mutation_status",
    "network_attempt_count_observed",
    "model_request_count_observed",
    "raw_response_cleanup_status",
    "post_request_cleanup_status",
    "provider_key_reference_release_status",
    "task_content_included_in_facts",
    "provider_content_included_in_facts",
    "derived_closure_claim_included",
)


@dataclass(frozen=True, slots=True, init=False)
class VerifiedPostrunFacts:
    """Immutable operational facts produced only by strict byte validation."""

    document_sha256: str
    schema_version: str
    campaign_id: str
    receipt_id: str
    completed_at_utc: str
    receipt_issued_at_utc: str
    terminal_stage: str
    local_terminal_outcome: str
    terminal_provider_outcome_state: str
    terminal_error_code: str | None
    last_completed_artifact_write_stage: str
    artifact_error_code: str | None
    task_released: bool
    task_released_at_utc: str | None
    provider_key_loaded: bool
    provider_key_loaded_at_utc: str | None
    released_task_bundle_commitment_sha256: str | None
    released_case_handle_order_commitment_sha256: str | None
    released_execution_input_order_commitment_sha256: str | None
    task_bundle_opening_verified_in_memory: bool
    task_bundle_opening_persisted: bool
    custodian_private_leak_canary_scan_status: str
    public_generic_privacy_scan_status: str
    canary_injection_verified_for_every_send: bool
    database_origin_status: str
    database_mutation_status: str
    network_attempt_count_observed: int
    model_request_count_observed: int
    raw_response_cleanup_status: str
    post_request_cleanup_status: str
    provider_key_reference_release_status: str
    task_content_included_in_facts: bool
    provider_content_included_in_facts: bool
    derived_closure_claim_included: bool
    _verification_token: object = field(repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("VerifiedPostrunFacts requires successful byte verification")

    @classmethod
    def _create(
        cls,
        token: object,
        *,
        payload_sha256: str,
        values: Mapping[str, Any],
    ) -> "VerifiedPostrunFacts":
        if (
            token is not _VERIFIED_POSTRUN_TOKEN
            or not _is_sha256(payload_sha256)
            or set(values) != set(_POSTRUN_FIELDS)
        ):
            raise TypeError("verified_postrun_facts_construction_forbidden")
        instance = object.__new__(cls)
        object.__setattr__(instance, "document_sha256", payload_sha256)
        for name in _POSTRUN_FIELDS:
            object.__setattr__(instance, name, values[name])
        object.__setattr__(instance, "_verification_token", token)
        return instance

    def _assert_verified(self) -> None:
        if self._verification_token is not _VERIFIED_POSTRUN_TOKEN:
            _fail("external_closure_postrun_facts_invalid")


@dataclass(frozen=True, slots=True, init=False)
class ArtifactFileCommitment:
    """One safe commitment for a fixed public artifact identifier."""

    name: str
    byte_count: int
    sha256: str
    _verification_token: object = field(repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("ArtifactFileCommitment requires verified artifact bytes")

    @classmethod
    def _create(
        cls, token: object, *, name: str, payload: bytes
    ) -> "ArtifactFileCommitment":
        if token is not _VERIFIED_FILE_TOKEN or name not in _WRITER_ORDER:
            raise TypeError("artifact_file_commitment_construction_forbidden")
        instance = object.__new__(cls)
        object.__setattr__(instance, "name", name)
        object.__setattr__(instance, "byte_count", len(payload))
        object.__setattr__(instance, "sha256", _sha256(payload))
        object.__setattr__(instance, "_verification_token", token)
        return instance


@dataclass(frozen=True, slots=True, init=False)
class ArtifactBundleSummary:
    """Content-free result of container verification.

    Quarantine results intentionally contain no file hashes.  ``artifact_count``
    remains the count implied by the attested writer stage; ``total_byte_count``
    is zero when bytes may not be published.
    """

    bundle_status: BundleStatus
    artifact_publication_disposition: PublicationDisposition
    last_completed_artifact_write_stage: str
    artifact_count: int
    file_commitments: tuple[ArtifactFileCommitment, ...]
    total_byte_count: int
    manifest_sha256: str | None
    audit_database_present: bool
    audit_database_schema_commitment_sha256: str | None
    audit_database_schema_object_count: int
    audit_database_schema_verified: bool
    audit_database_encoding_utf8: bool
    audit_database_freelist_empty: bool
    public_generic_privacy_scan_status: str
    _verification_token: object = field(repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("ArtifactBundleSummary requires artifact verification")

    @classmethod
    def _create(cls, token: object, **values: object) -> "ArtifactBundleSummary":
        public_fields = set(cls.__dataclass_fields__) - {"_verification_token"}
        if token is not _VERIFIED_BUNDLE_TOKEN or set(values) != public_fields:
            raise TypeError("artifact_bundle_summary_construction_forbidden")
        instance = object.__new__(cls)
        for name in public_fields:
            object.__setattr__(instance, name, values[name])
        object.__setattr__(instance, "_verification_token", token)
        return instance


def validate_postrun_attested_facts(
    payload: bytes,
    *,
    schemas: Mapping[str, Mapping[str, Any]],
) -> VerifiedPostrunFacts:
    """Validate caller-supplied post-run facts without reading any capability."""

    value = decode_strict_json_object(payload, max_bytes=_MAX_POSTRUN_FACT_BYTES)
    _validate_with_schema(
        value,
        schemas=schemas,
        schema_name=_POSTRUN_SCHEMA,
        error_code="external_closure_postrun_facts_schema_invalid",
    )

    try:
        completed = parse_utc_timestamp(value["completed_at_utc"])
        issued = parse_utc_timestamp(value["receipt_issued_at_utc"])
        for name in ("task_released_at_utc", "provider_key_loaded_at_utc"):
            timestamp = value[name]
            if timestamp is not None:
                parse_utc_timestamp(timestamp)
    except (ExternalClosurePrimitiveError, KeyError, TypeError):
        _fail("external_closure_postrun_facts_timeline_invalid")
    if not completed < issued:
        _fail("external_closure_postrun_facts_timeline_invalid")

    return VerifiedPostrunFacts._create(
        _VERIFIED_POSTRUN_TOKEN,
        payload_sha256=_sha256(payload),
        values=value,
    )


def _publication_disposition(facts: VerifiedPostrunFacts) -> PublicationDisposition:
    if (
        facts.custodian_private_leak_canary_scan_status == "leak_detected"
        or facts.public_generic_privacy_scan_status == "sensitive_detected"
    ):
        return "sensitive_quarantined"
    if (
        facts.custodian_private_leak_canary_scan_status == "unavailable"
        or facts.public_generic_privacy_scan_status == "unavailable"
    ):
        return "privacy_unverified_quarantined"
    if (
        facts.database_origin_status != "fresh_path_confirmed"
        or facts.database_mutation_status != "normative_only"
    ):
        return "privacy_unverified_quarantined"
    return "normal"


def _stage_bundle_status(stage: str) -> BundleStatus:
    return "complete" if stage == "manifest_written" else "manifestless_failure"


def _quarantine_summary(
    *,
    facts: VerifiedPostrunFacts,
    disposition: PublicationDisposition,
    public_scan_status: str,
) -> ArtifactBundleSummary:
    expected_names = _EXPECTED_NAMES_BY_STAGE[facts.last_completed_artifact_write_stage]
    return ArtifactBundleSummary._create(
        _VERIFIED_BUNDLE_TOKEN,
        bundle_status=_stage_bundle_status(
            facts.last_completed_artifact_write_stage
        ),
        artifact_publication_disposition=disposition,
        last_completed_artifact_write_stage=(
            facts.last_completed_artifact_write_stage
        ),
        artifact_count=len(expected_names),
        file_commitments=(),
        total_byte_count=0,
        manifest_sha256=None,
        audit_database_present=False,
        audit_database_schema_commitment_sha256=None,
        audit_database_schema_object_count=0,
        audit_database_schema_verified=False,
        audit_database_encoding_utf8=False,
        audit_database_freelist_empty=False,
        public_generic_privacy_scan_status=public_scan_status,
    )


def _link_like(path: Path, metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & _REPARSE_POINT)


def _directory_identity(directory: Path) -> tuple[int, int, int, int, int]:
    try:
        metadata = directory.lstat()
    except OSError:
        _fail("external_closure_directory_read_failed")
    if _link_like(directory, metadata) or not stat.S_ISDIR(metadata.st_mode):
        _fail("external_closure_directory_identity_invalid")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_nlink,
    )


def _read_expected_directory(
    directory: Path, expected_names: tuple[str, ...]
) -> Mapping[str, bytes]:
    if expected_names:
        return read_exact_artifact_directory(
            directory,
            expected_names=expected_names,
            max_file_bytes=_MAX_ARTIFACT_FILE_BYTES,
            max_total_bytes=_MAX_ARTIFACT_TOTAL_BYTES,
        )
    try:
        before = _directory_identity(directory)
        if any(True for _entry in directory.iterdir()):
            _fail("external_closure_directory_file_set_invalid")
        after = _directory_identity(directory)
    except ExternalClosurePrimitiveError:
        raise
    except OSError:
        _fail("external_closure_directory_read_failed")
    if before != after:
        _fail("external_closure_directory_identity_changed")
    return {}


def _require_exact_fields(
    value: object, expected: frozenset[str], error_code: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        _fail(error_code)
    return value


_INDEX_FIELDS = frozenset({"schema_version", "runs"})
_INDEX_RUN_FIELDS = frozenset(
    {
        "task_id",
        "run_id",
        "provider",
        "transport",
        "chain_verification",
        "completion_telemetry_event_commitment",
    }
)
_CHAIN_FIELDS = frozenset(
    {
        "valid",
        "run_id",
        "event_count",
        "chain_head",
        "error_code",
        "error_sequence",
    }
)
_BRIDGE_FIELDS = frozenset(
    {
        "schema_version",
        "case_id",
        "binding_sha256",
        "started",
        "terminals",
        "all_started_attempts_terminal",
        "write_failed",
        "commitment_sha256",
    }
)
_EVENT_HASH_FIELDS = frozenset({"attempt_index", "event_hash"})


def _validate_event_hash_list(value: object) -> bool:
    if type(value) is not list or len(value) > 800:
        return False
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _EVENT_HASH_FIELDS:
            return False
        if (
            type(item.get("attempt_index")) is not int
            or not 0 <= item["attempt_index"] < 800
            or not _is_sha256(item.get("event_hash"))
        ):
            return False
    return True


def _validate_audit_index(value: object) -> None:
    error = "external_closure_audit_index_invalid"
    index = _require_exact_fields(value, _INDEX_FIELDS, error)
    runs = index.get("runs")
    if index.get("schema_version") != "1.1" or type(runs) is not list or len(runs) > 100:
        _fail(error)
    for raw_run in runs:
        run = _require_exact_fields(raw_run, _INDEX_RUN_FIELDS, error)
        chain = _require_exact_fields(run.get("chain_verification"), _CHAIN_FIELDS, error)
        bridge = _require_exact_fields(
            run.get("completion_telemetry_event_commitment"), _BRIDGE_FIELDS, error
        )
        task_id = run.get("task_id")
        run_id = run.get("run_id")
        if (
            type(task_id) is not str
            or _CASE_HANDLE.fullmatch(task_id) is None
            or type(run_id) is not str
            or _RUN_ID.fullmatch(run_id) is None
            or run.get("provider") != "deepseek"
            or run.get("transport") != "openai_compatible_responses"
            or type(chain.get("valid")) is not bool
            or chain.get("run_id") != run_id
            or type(chain.get("event_count")) is not int
            or not 0 <= chain["event_count"] <= 10_000
            or not _is_sha256(chain.get("chain_head"))
            or (
                chain.get("error_code") is not None
                and type(chain.get("error_code")) is not str
            )
            or (
                chain.get("error_sequence") is not None
                and type(chain.get("error_sequence")) is not int
            )
            or bridge.get("schema_version")
            != "provider-completion-ledger-bridge-commitment/1.0"
            or bridge.get("case_id") != task_id
            or not _is_sha256(bridge.get("binding_sha256"))
            or not _validate_event_hash_list(bridge.get("started"))
            or not _validate_event_hash_list(bridge.get("terminals"))
            or type(bridge.get("all_started_attempts_terminal")) is not bool
            or type(bridge.get("write_failed")) is not bool
            or not _is_sha256(bridge.get("commitment_sha256"))
        ):
            _fail(error)


_TELEMETRY_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "runtime_plan",
        "runtime_denominator",
        "ledger_reconciliation",
        "closure",
        "historical_backfill_performed",
    }
)


def _validate_completion_telemetry(value: object) -> None:
    """Validate the strict outer container, leaving record semantics to layer A."""

    error = "external_closure_completion_telemetry_invalid"
    telemetry = _require_exact_fields(value, _TELEMETRY_FIELDS, error)
    if (
        telemetry.get("schema_version") != "phase6-completion-telemetry/1.0"
        or telemetry.get("status") != "recorded"
        or telemetry.get("historical_backfill_performed") is not False
        or not isinstance(telemetry.get("runtime_plan"), Mapping)
        or not isinstance(telemetry.get("runtime_denominator"), Mapping)
        or not isinstance(telemetry.get("ledger_reconciliation"), Mapping)
        or not isinstance(telemetry.get("closure"), Mapping)
    ):
        _fail(error)


@dataclass(frozen=True, slots=True)
class _DatabaseChecks:
    schema_commitment_sha256: str
    schema_object_count: int
    schema_verified: bool
    encoding_utf8: bool
    freelist_empty: bool


def _normalize_sql(value: object) -> str:
    if type(value) is not str:
        _fail("external_closure_audit_schema_invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
        normalized = _ASCII_SQL_WHITESPACE.sub(b" ", encoded).strip(b" ")
        return normalized.decode("utf-8", errors="strict")
    except UnicodeError:
        _fail("external_closure_audit_schema_invalid")


def _pragma_scalar(connection: sqlite3.Connection, statement: str) -> object:
    row = connection.execute(statement).fetchone()
    if row is None or len(row) != 1:
        _fail("external_closure_audit_schema_invalid")
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


def _verify_audit_database(path: Path, *, expected_payload: bytes) -> _DatabaseChecks:
    connection: sqlite3.Connection | None = None
    try:
        uri = path.absolute().as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        _assert_sqlite_connection_bytes(connection, expected_payload)
        connection.execute("PRAGMA query_only = ON")
        query_only = _pragma_scalar(connection, "PRAGMA query_only")
        encoding = _pragma_scalar(connection, "PRAGMA encoding")
        freelist_count = _pragma_scalar(connection, "PRAGMA freelist_count")
        rows = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type COLLATE BINARY,name COLLATE BINARY LIMIT ?",
            (_EXPECTED_SCHEMA_OBJECT_COUNT + 1,),
        ).fetchall()
        # SQLite must have interpreted the exact bytes already read, scanned,
        # and committed above.  Re-reading the pathname after close is not
        # sufficient: an adversary could temporarily replace the directory
        # entry for the duration of connect/query and then restore it.  The
        # serialized image binds the queried connection to the committed bytes.
        _assert_sqlite_connection_bytes(connection, expected_payload)
    except ExternalClosurePrimitiveError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError, UnicodeError):
        _fail("external_closure_audit_schema_invalid")
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                _fail("external_closure_audit_schema_invalid")

    projection: list[list[str]] = []
    try:
        for row in rows:
            if len(row) != 4 or any(type(item) is not str for item in row):
                _fail("external_closure_audit_schema_invalid")
            projection.append([row[0], row[1], row[2], _normalize_sql(row[3])])
        commitment = _sha256(
            _SCHEMA_COMMITMENT_DOMAIN.encode("utf-8")
            + b"\x00"
            + canonical_json_bytes(projection)
        )
    except ExternalClosurePrimitiveError:
        raise
    except (TypeError, ValueError):
        _fail("external_closure_audit_schema_invalid")

    if (
        query_only != 1
        or type(encoding) is not str
        or encoding.upper().replace("-", "") != "UTF8"
        or type(freelist_count) is not int
        or freelist_count != 0
        or len(projection) != _EXPECTED_SCHEMA_OBJECT_COUNT
        or commitment != _EXPECTED_SCHEMA_COMMITMENT_SHA256
    ):
        _fail("external_closure_audit_schema_invalid")
    return _DatabaseChecks(
        schema_commitment_sha256=commitment,
        schema_object_count=len(projection),
        schema_verified=True,
        encoding_utf8=True,
        freelist_empty=True,
    )


def _manifest_valid(
    payload: bytes,
    *,
    facts: VerifiedPostrunFacts,
    schemas: Mapping[str, Mapping[str, Any]],
) -> tuple[bool, Mapping[str, Any] | None]:
    try:
        manifest = decode_canonical_json_file(
            payload, max_bytes=_MAX_ARTIFACT_FILE_BYTES
        )
        _validate_with_schema(
            manifest,
            schemas=schemas,
            schema_name=_MANIFEST_SCHEMA,
            error_code="external_closure_bundle_manifest_invalid",
        )
    except ExternalClosurePrimitiveError as error:
        if error.code in {
            "external_closure_artifact_json_bytes_invalid",
            "external_closure_artifact_json_not_canonical",
            "external_closure_json_bom_forbidden",
            "external_closure_json_duplicate_key",
            "external_closure_json_invalid",
            "external_closure_json_nesting_invalid",
            "external_closure_json_nonfinite",
            "external_closure_json_object_required",
            "external_closure_json_too_large",
            "external_closure_json_type_invalid",
            "external_closure_json_unicode_invalid",
            "external_closure_json_utf8_invalid",
            "external_closure_bundle_manifest_invalid",
        }:
            return False, None
        raise
    if (
        manifest.get("campaign_id") != facts.campaign_id
        or manifest.get("closure_evidence_contract_commitment_sha256")
        != _EXPECTED_CLOSURE_EVIDENCE_COMMITMENT_SHA256
        or manifest.get("audit_database_schema_commitment_sha256")
        != _EXPECTED_SCHEMA_COMMITMENT_SHA256
    ):
        # Canonical/schema validity and bound-value consistency are distinct.
        # The manifest_present_invalid disposition is reserved by the frozen
        # contract for a concrete canonical or schema failure.
        _fail("external_closure_bundle_hash_mismatch")
    return True, manifest


def _commitments(
    payloads: Mapping[str, bytes], expected_names: tuple[str, ...]
) -> tuple[ArtifactFileCommitment, ...]:
    return tuple(
        ArtifactFileCommitment._create(
            _VERIFIED_FILE_TOKEN, name=name, payload=payloads[name]
        )
        for name in expected_names
    )


def verify_artifact_bundle(
    artifact_directory: Path | None,
    *,
    postrun_facts: VerifiedPostrunFacts,
    schemas: Mapping[str, Mapping[str, Any]],
) -> ArtifactBundleSummary:
    """Verify a normal bundle or return a zero-read quarantine disposition."""

    if type(postrun_facts) is not VerifiedPostrunFacts:
        _fail("external_closure_postrun_facts_invalid")
    postrun_facts._assert_verified()
    stage = postrun_facts.last_completed_artifact_write_stage
    expected_names = _EXPECTED_NAMES_BY_STAGE.get(stage)
    if expected_names is None:
        _fail("external_closure_artifact_writer_stage_invalid")

    disposition = _publication_disposition(postrun_facts)
    if disposition != "normal":
        return _quarantine_summary(
            facts=postrun_facts,
            disposition=disposition,
            public_scan_status=postrun_facts.public_generic_privacy_scan_status,
        )

    if not isinstance(artifact_directory, Path):
        _fail("external_closure_artifact_directory_required")
    directory_identity = _directory_identity(artifact_directory)
    payloads = _read_expected_directory(artifact_directory, expected_names)

    ordered_payloads = tuple(payloads[name] for name in expected_names)
    try:
        scanned = scan_public_artifact_bytes(ordered_payloads)
    except ExternalClosurePrimitiveError as error:
        if error.code == "external_closure_sensitive_content_detected":
            return _quarantine_summary(
                facts=postrun_facts,
                disposition="sensitive_quarantined",
                public_scan_status="sensitive_detected",
            )
        return _quarantine_summary(
            facts=postrun_facts,
            disposition="privacy_unverified_quarantined",
            public_scan_status="unavailable",
        )
    except Exception:
        return _quarantine_summary(
            facts=postrun_facts,
            disposition="privacy_unverified_quarantined",
            public_scan_status="unavailable",
        )
    if scanned != len(expected_names):
        return _quarantine_summary(
            facts=postrun_facts,
            disposition="privacy_unverified_quarantined",
            public_scan_status="unavailable",
        )

    commitments = _commitments(payloads, expected_names)
    bundle_status: BundleStatus = _stage_bundle_status(stage)
    manifest_sha256: str | None = None
    manifest: Mapping[str, Any] | None = None
    if _MANIFEST in payloads:
        valid, manifest = _manifest_valid(
            payloads[_MANIFEST],
            facts=postrun_facts,
            schemas=schemas,
        )
        if not valid:
            # Manifest validity precedes hash, audit-schema, event, denominator,
            # outer-projection, budget, and timeline errors in the frozen
            # evidence-error priority.  Do not let a later DB defect mask it.
            final_payloads = _read_expected_directory(
                artifact_directory, expected_names
            )
            if _directory_identity(artifact_directory) != directory_identity or any(
                final_payloads[name] != payloads[name] for name in expected_names
            ):
                _fail("external_closure_artifact_bundle_identity_changed")
            return ArtifactBundleSummary._create(
                _VERIFIED_BUNDLE_TOKEN,
                bundle_status="manifest_present_invalid",
                artifact_publication_disposition="normal",
                last_completed_artifact_write_stage=stage,
                artifact_count=len(expected_names),
                file_commitments=commitments,
                total_byte_count=sum(item.byte_count for item in commitments),
                manifest_sha256=None,
                audit_database_present=_AUDIT_DATABASE in payloads,
                audit_database_schema_commitment_sha256=None,
                audit_database_schema_object_count=0,
                audit_database_schema_verified=False,
                audit_database_encoding_utf8=False,
                audit_database_freelist_empty=False,
                public_generic_privacy_scan_status="passed",
            )
        manifest_sha256 = _sha256(payloads[_MANIFEST])
        manifest_files = manifest.get("files")
        if not isinstance(manifest_files, Mapping):
            _fail("external_closure_bundle_manifest_invalid")
        for name in _WRITER_ORDER[:-1]:
            recorded = manifest_files.get(name)
            if not isinstance(recorded, Mapping):
                _fail("external_closure_bundle_manifest_invalid")
            payload = payloads[name]
            if (
                recorded.get("bytes") != len(payload)
                or recorded.get("sha256") != _sha256(payload)
            ):
                _fail("external_closure_bundle_hash_mismatch")

    # Audit schema is the next frozen priority after manifest validity and
    # byte commitments.  It must be established before any higher-layer index
    # or telemetry projection is interpreted.
    database_checks: _DatabaseChecks | None = None
    if _AUDIT_DATABASE in payloads:
        database_checks = _verify_audit_database(
            artifact_directory / _AUDIT_DATABASE,
            expected_payload=payloads[_AUDIT_DATABASE],
        )

    if _AUDIT_INDEX in payloads:
        try:
            index = decode_canonical_json_file(
                payloads[_AUDIT_INDEX], max_bytes=_MAX_ARTIFACT_FILE_BYTES
            )
            _validate_audit_index(index)
        except ExternalClosurePrimitiveError as error:
            if error.code == "external_closure_audit_index_invalid":
                raise
            _fail("external_closure_audit_index_invalid")

    if _COMPLETION_TELEMETRY in payloads:
        try:
            telemetry = decode_canonical_json_file(
                payloads[_COMPLETION_TELEMETRY],
                max_bytes=_MAX_ARTIFACT_FILE_BYTES,
            )
            _validate_completion_telemetry(telemetry)
        except ExternalClosurePrimitiveError as error:
            if error.code == "external_closure_completion_telemetry_invalid":
                raise
            _fail("external_closure_completion_telemetry_invalid")

    # Re-read the exact set after SQLite access.  This detects mutation and any
    # journal sidecar creation without granting a write-capable connection.
    final_payloads = _read_expected_directory(artifact_directory, expected_names)
    if _directory_identity(artifact_directory) != directory_identity or any(
        final_payloads[name] != payloads[name] for name in expected_names
    ):
        _fail("external_closure_artifact_bundle_identity_changed")

    database_present = database_checks is not None
    return ArtifactBundleSummary._create(
        _VERIFIED_BUNDLE_TOKEN,
        bundle_status=bundle_status,
        artifact_publication_disposition="normal",
        last_completed_artifact_write_stage=stage,
        artifact_count=len(expected_names),
        file_commitments=commitments,
        total_byte_count=sum(item.byte_count for item in commitments),
        manifest_sha256=manifest_sha256,
        audit_database_present=database_present,
        audit_database_schema_commitment_sha256=(
            database_checks.schema_commitment_sha256
            if database_checks is not None
            else None
        ),
        audit_database_schema_object_count=(
            database_checks.schema_object_count
            if database_checks is not None
            else 0
        ),
        audit_database_schema_verified=(
            database_checks.schema_verified
            if database_checks is not None
            else False
        ),
        audit_database_encoding_utf8=(
            database_checks.encoding_utf8
            if database_checks is not None
            else False
        ),
        audit_database_freelist_empty=(
            database_checks.freelist_empty
            if database_checks is not None
            else False
        ),
        public_generic_privacy_scan_status="passed",
    )


__all__ = [
    "ArtifactBundleSummary",
    "ArtifactFileCommitment",
    "VerifiedPostrunFacts",
    "validate_postrun_attested_facts",
    "verify_artifact_bundle",
]
