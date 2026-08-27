from __future__ import annotations

import argparse
import base64
import binascii
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from urllib.parse import unquote
from typing import Any, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator, FormatChecker


KIT_VERSION = "1.1"
CANONICALIZATION_VERSION = "researchops-canonical-json-v1"
DOCUMENT_HASH_DOMAIN = b"researchops-private-holdout-document-v1\x00"
SIGNATURE_DOMAIN = b"researchops-private-holdout-signature-v1\x00"
LEDGER_ENTRY_DOMAIN = b"researchops-private-holdout-ledger-entry-v1\x00"
TRUST_MANIFEST_DOMAIN = b"researchops-private-holdout-trust-manifest-v1\x00"
ZERO_SHA256 = "0" * 64
MAX_DOCUMENT_BYTES = 2_000_000
RATE_QUANTUM = Decimal("0.000001")
MONEY_QUANTUM = Decimal("0.000001")
EXPECTED_PROTOCOL_SHA256 = "2692d4fdf7674af3e2bacae48091f5cd8b8cb5b84a50a2fe0d4e454458d535bd"
EXPECTED_SCHEMA_BUNDLE_SHA256 = "741d02b9da8de3941863e595c4939c8c96578416b87b3e55446a803d4f693f37"

METRIC_DIRECTIONS = {
    "task_success_rate": "higher_is_better",
    "tool_selection_accuracy": "higher_is_better",
    "tool_argument_accuracy": "higher_is_better",
    "evidence_citation_accuracy": "higher_is_better",
    "numeric_claim_accuracy": "higher_is_better",
    "clarification_refusal_accuracy": "higher_is_better",
    "approval_bypass_rate": "lower_is_better",
    "unexpected_tool_error_rate": "lower_is_better",
    "completion_integrity_rate": "higher_is_better",
}
UNIVERSAL_METRICS = frozenset(
    {"task_success_rate", "completion_integrity_rate"}
)

RELEASE_FILES = {
    "trust_manifest": "trust_manifest.json",
    "freeze_request": "freeze_request.json",
    "commitment_statement": "commitment_statement.json",
    "authorization_grant": "authorization_grant.json",
    "aggregate_results": "aggregate_results.json",
    "consumption_receipt": "consumption_receipt.json",
    "signed_ledger": "signed_ledger.json",
    "release_manifest": "release_manifest.json",
}
SCHEMA_FILES = {
    name: f"{name}.schema.json"
    for name in RELEASE_FILES
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_VALUE_PATTERNS = (
    re.compile(r"\b(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"[A-Za-z]:[\\/]"),
    re.compile(r"\\\\[^\\]+\\[^\\]+"),
    re.compile(r"(?:^|\s)/(?:home|Users|tmp|var|mnt)/"),
    re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)"),
    re.compile(r"\b(?:file|s3|gs|az)://", re.IGNORECASE),
    re.compile(r"https?://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\bV2-(?:DEV|PUB|PRIVATE)-[0-9]{3,}\b", re.IGNORECASE),
)
_FORBIDDEN_NORMALIZED_KEYS = frozenset(
    {
        "taskid", "taskids", "caseid", "casekey", "taskorder", "repetitionseed",
        "prompt", "question", "input", "messages", "expected", "expectedoutput",
        "golden", "goldens", "answer", "rubric", "scorerdetails",
        "adjudicationnotes", "toolarguments", "expectedtoolcalls", "evidence",
        "numericclaims", "locator", "path", "absolutepath", "file", "filename",
        "uri", "url", "bucket", "objectkey", "datasetpath", "registrypath",
        "rawdata", "rawrows", "records", "samplevalues", "directidentifiers",
        "finaloutput", "rawoutput", "rawresponse", "responsebody",
        "provideroutputbody", "providerresponsebody", "incompletedetails",
        "toolresult", "traceback", "stdout", "stderr", "apikey", "authorization",
        "credentials", "accesstoken", "refreshtoken", "cookie",
        "connectionstring", "environment", "argv", "notes",
    }
)


class PrivateCustodianError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PrivateCustodianError(
                "private_json_duplicate_key", "JSON contains a duplicate field."
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PrivateCustodianError(
        "private_json_non_finite", f"JSON contains a non-finite number: {value}."
    )


def _read_regular_file_no_follow(path: Path, label: str) -> bytes:
    """Read one bounded regular file and detect common link/swap races.

    This is a local integrity guard, not a substitute for an OS sandbox or an
    immutable external custody store.
    """

    try:
        before = path.lstat()
        if path.is_symlink() or not path.is_file() or before.st_size > MAX_DOCUMENT_BYTES:
            raise PrivateCustodianError(
                "private_document_file_invalid",
                f"{label} is missing or not a regular bounded file.",
            )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size != before.st_size
                or opened.st_nlink != 1
            ):
                raise PrivateCustodianError(
                    "private_document_identity_changed",
                    f"{label} file identity is unsafe or changed.",
                )
            chunks: list[bytes] = []
            remaining = MAX_DOCUMENT_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        finally:
            os.close(descriptor)
        after = path.lstat()
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or len(payload) > MAX_DOCUMENT_BYTES
        ):
            raise PrivateCustodianError(
                "private_document_identity_changed", f"{label} changed while being read."
            )
        return payload
    except PrivateCustodianError:
        raise
    except OSError as exc:
        raise PrivateCustodianError(
            "private_document_file_invalid",
            f"{label} is missing or not a regular bounded file.",
        ) from exc


def _parse_json_object_payload(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except PrivateCustodianError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PrivateCustodianError(
            "private_document_json_invalid", f"{label} is not valid UTF-8 JSON."
        ) from exc
    if not isinstance(value, dict):
        raise PrivateCustodianError(
            "private_document_shape_invalid", f"{label} must be a JSON object."
        )
    return value


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    return _parse_json_object_payload(_read_regular_file_no_follow(path, label), label)


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PrivateCustodianError(
            "private_canonicalization_invalid", "Document cannot be canonicalized."
        ) from exc


def trust_manifest_sha256(document: Mapping[str, Any]) -> str:
    payload = dict(document)
    payload.pop("manifest_sha256", None)
    return hashlib.sha256(
        TRUST_MANIFEST_DOMAIN + canonical_json_bytes(payload)
    ).hexdigest()


def document_sha256(document: Mapping[str, Any]) -> str:
    document_type = document.get("document_type")
    if not isinstance(document_type, str) or not document_type:
        raise PrivateCustodianError(
            "private_document_type_invalid", "Signed document type is missing."
        )
    payload = dict(document)
    payload.pop("document_sha256", None)
    payload.pop("signatures", None)
    return hashlib.sha256(
        DOCUMENT_HASH_DOMAIN
        + document_type.encode("ascii")
        + b"\x00"
        + canonical_json_bytes(payload)
    ).hexdigest()


def ledger_entry_sha256(entry: Mapping[str, Any], ledger_id: str) -> str:
    payload = dict(entry)
    payload.pop("entry_sha256", None)
    payload.pop("signatures", None)
    return hashlib.sha256(
        LEDGER_ENTRY_DOMAIN
        + ledger_id.encode("ascii")
        + b"\x00"
        + canonical_json_bytes(payload)
    ).hexdigest()


def ledger_signature_message(event_type: str, signed_sha256: str) -> bytes:
    return signature_message(f"ledger_entry:{event_type}", signed_sha256)


def signature_message(document_type: str, signed_sha256: str) -> bytes:
    if _SHA256.fullmatch(signed_sha256) is None:
        raise PrivateCustodianError(
            "private_signature_hash_invalid", "Signature hash is invalid."
        )
    return (
        SIGNATURE_DOMAIN
        + document_type.encode("ascii")
        + b"\x00"
        + bytes.fromhex(signed_sha256)
    )


def schema_bundle_sha256(schema_root: Path) -> str:
    paths = sorted(schema_root.glob("*.schema.json"), key=lambda item: item.name)
    if len(paths) != len(SCHEMA_FILES):
        raise PrivateCustodianError(
            "private_schema_bundle_invalid", "Private schema bundle file count is invalid."
        )
    digest = hashlib.sha256()
    for path in paths:
        relative = path.name.encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _normalize_key(value: str) -> str:
    return re.sub(r"[-_.]", "", unicodedata.normalize("NFKC", value).casefold())


def _decoded_text_candidates(value: str) -> set[str]:
    observed = {value}
    frontier = {value}
    for _ in range(3):
        next_frontier: set[str] = set()
        for candidate in frontier:
            decoded_url = unquote(candidate)
            if decoded_url not in observed:
                observed.add(decoded_url)
                next_frontier.add(decoded_url)
            compact = candidate.strip()
            decoders: list[tuple[str, Any]] = [
                ("base64", base64.b64decode),
                ("base64url", base64.urlsafe_b64decode),
                ("base32", base64.b32decode),
            ]
            for kind, decoder in decoders:
                if kind == "base32":
                    alphabet = r"[A-Z2-7]+={0,6}"
                    minimum = 8
                else:
                    alphabet = r"[A-Za-z0-9+/_-]+={0,2}"
                    minimum = 8
                if len(compact) < minimum or re.fullmatch(alphabet, compact) is None:
                    continue
                padded = compact + "=" * ((-len(compact)) % (8 if kind == "base32" else 4))
                try:
                    raw = decoder(padded)
                    decoded = raw.decode("utf-8")
                except (binascii.Error, UnicodeDecodeError, ValueError):
                    continue
                if decoded not in observed:
                    observed.add(decoded)
                    next_frontier.add(decoded)
                normalized_controls = "".join(
                    character if character.isprintable() else " " for character in decoded
                )
                if normalized_controls not in observed:
                    observed.add(normalized_controls)
                    next_frontier.add(normalized_controls)
            if len(compact) >= 8 and len(compact) % 2 == 0 and re.fullmatch(r"[0-9A-Fa-f]+", compact):
                try:
                    decoded_hex = bytes.fromhex(compact).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    pass
                else:
                    if decoded_hex not in observed:
                        observed.add(decoded_hex)
                        next_frontier.add(decoded_hex)
                    normalized_controls = "".join(
                        character if character.isprintable() else " "
                        for character in decoded_hex
                    )
                    if normalized_controls not in observed:
                        observed.add(normalized_controls)
                        next_frontier.add(normalized_controls)
        frontier = next_frontier
        if not frontier:
            break
    return observed


def assert_public_release_projection(value: Any, *, parent_key: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = _normalize_key(str(key))
            if normalized in _FORBIDDEN_NORMALIZED_KEYS:
                raise PrivateCustodianError(
                    "private_release_forbidden_field", "Release contains a forbidden field."
                )
            assert_public_release_projection(item, parent_key=str(key))
        return
    if isinstance(value, list):
        for item in value:
            assert_public_release_projection(item, parent_key=parent_key)
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise PrivateCustodianError(
            "private_release_non_finite", "Release contains a non-finite value."
        )
    if not isinstance(value, str):
        return
    normalized_parent = _normalize_key(parent_key)
    candidates = (
        {value}
        if normalized_parent in {"signatureb64", "publickeyb64"}
        else _decoded_text_candidates(value)
    )
    for candidate in candidates:
        for pattern in _FORBIDDEN_VALUE_PATTERNS:
            if pattern.search(candidate):
                raise PrivateCustodianError(
                    "private_release_sensitive_value", "Release contains a forbidden value pattern."
                )
    if len(value) > 256:
        if normalized_parent not in {"signatureb64", "publickeyb64"}:
            raise PrivateCustodianError(
                "private_release_opaque_value", "Release contains an oversized opaque value."
            )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _assert_no_reparse_ancestry(path: Path, *, code: str, message: str) -> None:
    current = path.absolute()
    while True:
        if current.exists() and _is_reparse_or_symlink(current):
            raise PrivateCustodianError(code, message)
        if current.parent == current:
            return
        current = current.parent


def _tree_file_identities(root: Path, *, maximum_files: int = 100_000) -> set[tuple[int, int]]:
    identities: set[tuple[int, int]] = set()
    observed = 0

    def fail_walk(error: OSError) -> None:
        raise PrivateCustodianError(
            "private_root_identity_unavailable", "Private file identity tree cannot be scanned."
        ) from error

    for directory, child_directories, filenames in os.walk(
        root, followlinks=False, onerror=fail_walk
    ):
        directory_path = Path(directory)
        if _is_reparse_or_symlink(directory_path):
            raise PrivateCustodianError(
                "private_root_reparse_forbidden", "Private root tree contains a reparse point."
            )
        for name in [*child_directories, *filenames]:
            path = directory_path / name
            if _is_reparse_or_symlink(path):
                raise PrivateCustodianError(
                    "private_root_reparse_forbidden", "Private root tree contains a reparse point."
                )
        for name in filenames:
            observed += 1
            if observed > maximum_files:
                raise PrivateCustodianError(
                    "private_root_file_limit_exceeded", "Private root exceeds the metadata scan limit."
                )
            try:
                stat = (directory_path / name).stat(follow_symlinks=False)
            except OSError as exc:
                raise PrivateCustodianError(
                    "private_root_identity_unavailable", "Private file identity cannot be verified."
                ) from exc
            if stat.st_ino == 0:
                raise PrivateCustodianError(
                    "private_root_identity_unavailable", "Private file identity is unavailable."
                )
            if stat.st_nlink != 1:
                raise PrivateCustodianError(
                    "private_root_hardlink_forbidden",
                    "Private root contains a multiply linked file.",
                )
            identities.add((int(stat.st_dev), int(stat.st_ino)))
    return identities


def assert_private_root_outside_repository(
    private_root: Path, project_root: Path
) -> dict[str, Any]:
    try:
        project = project_root.resolve(strict=True)
        lexical = private_root.absolute()
        resolved = private_root.resolve(strict=True)
        system_temp = Path(tempfile.gettempdir()).resolve(strict=True)
    except OSError as exc:
        raise PrivateCustodianError(
            "private_root_invalid", "Private root cannot be resolved safely."
        ) from exc
    if not resolved.is_dir():
        raise PrivateCustodianError(
            "private_root_invalid", "Private root must be an existing directory."
        )
    if (
        _is_relative_to(lexical, project)
        or _is_relative_to(resolved, project)
        or _is_relative_to(resolved, system_temp)
    ):
        raise PrivateCustodianError(
            "private_root_not_external", "Private root must be outside repository and temp roots."
        )
    _assert_no_reparse_ancestry(
        lexical,
        code="private_root_reparse_forbidden",
        message="Private root ancestry contains a reparse point.",
    )
    private_identities = _tree_file_identities(resolved)
    repository_identities = _tree_file_identities(project)
    if private_identities.intersection(repository_identities):
        raise PrivateCustodianError(
            "private_root_file_identity_overlap", "Private files overlap repository file identities."
        )
    return {
        "status": "external_private_root_valid",
        "private_root_in_repository": False,
        "private_root_in_system_temp": False,
        "reparse_point_detected": False,
        "file_identity_overlap_detected": False,
        "point_in_time_snapshot_only": True,
        "authorization_enforcement_performed": False,
        "network_calls": 0,
    }


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PrivateCustodianError(
            "private_timestamp_invalid", f"{label} timestamp is invalid."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise PrivateCustodianError(
            "private_timestamp_invalid", f"{label} timestamp must be UTC."
        )
    return parsed


def _load_schemas(kit_root: Path) -> dict[str, dict[str, Any]]:
    schema_root = kit_root / "schemas"
    schemas: dict[str, dict[str, Any]] = {}
    for name, filename in SCHEMA_FILES.items():
        schema = load_json_object(schema_root / filename, f"{name} schema")
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            raise PrivateCustodianError(
                "private_schema_invalid", "Private schema is invalid."
            ) from exc
        schemas[name] = schema
    return schemas


def _validate_schema(document: Mapping[str, Any], schema: Mapping[str, Any], label: str) -> None:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda item: list(item.path))
    if errors:
        raise PrivateCustodianError(
            "private_document_schema_invalid", f"{label} does not satisfy its strict schema."
        )


def _trust_keys(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    values = manifest["keys"]
    roles = [item["role"] for item in values]
    key_ids = [item["key_id"] for item in values]
    if sorted(roles) != ["custodian", "freeze_authority"] or len(key_ids) != len(set(key_ids)):
        raise PrivateCustodianError(
            "private_trust_manifest_invalid", "Trust roles or key IDs are invalid."
        )
    decoded_keys: list[bytes] = []
    for item in values:
        try:
            public_key = base64.b64decode(item["public_key_b64"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise PrivateCustodianError(
                "private_trust_manifest_invalid", "Trust public key is invalid."
            ) from exc
        if len(public_key) != 32:
            raise PrivateCustodianError(
                "private_trust_manifest_invalid", "Trust public key is invalid."
            )
        decoded_keys.append(public_key)
    if len(set(decoded_keys)) != len(decoded_keys):
        raise PrivateCustodianError(
            "private_trust_role_separation_invalid",
            "Trust roles must use different Ed25519 public keys.",
        )
    return {item["role"]: item for item in values}


def _verify_signatures(
    document: Mapping[str, Any],
    required_roles: Sequence[str],
    trust_keys: Mapping[str, Mapping[str, Any]],
    *,
    signed_at: datetime,
) -> None:
    expected_hash = document_sha256(document)
    if document.get("document_sha256") != expected_hash:
        raise PrivateCustodianError(
            "private_document_hash_mismatch", "Signed document hash mismatch."
        )
    signatures = document["signatures"]
    roles = [item["role"] for item in signatures]
    if sorted(roles) != sorted(required_roles) or len(roles) != len(set(roles)):
        raise PrivateCustodianError(
            "private_signature_roles_invalid", "Required signature roles are missing or duplicated."
        )
    for signature in signatures:
        role = signature["role"]
        anchor = trust_keys.get(role)
        if anchor is None or signature["key_id"] != anchor["key_id"]:
            raise PrivateCustodianError(
                "private_signature_key_mismatch", "Signature key does not match trust manifest."
            )
        valid_from = _parse_timestamp(anchor["valid_from_utc"], "key validity start")
        valid_until = _parse_timestamp(anchor["valid_until_utc"], "key validity end")
        if anchor["revoked"] or not (valid_from <= signed_at <= valid_until):
            raise PrivateCustodianError(
                "private_signature_key_inactive",
                "Signature was made outside the anchored key validity window.",
            )
        if signature["signed_sha256"] != expected_hash:
            raise PrivateCustodianError(
                "private_signature_hash_mismatch", "Signature does not bind the document hash."
            )
        try:
            public_bytes = base64.b64decode(anchor["public_key_b64"], validate=True)
            signature_bytes = base64.b64decode(signature["signature_b64"], validate=True)
            if len(public_bytes) != 32 or len(signature_bytes) != 64:
                raise ValueError
            Ed25519PublicKey.from_public_bytes(public_bytes).verify(
                signature_bytes,
                signature_message(document["document_type"], expected_hash),
            )
        except (ValueError, binascii.Error, InvalidSignature) as exc:
            raise PrivateCustodianError(
                "private_signature_invalid", "Ed25519 signature verification failed."
            ) from exc


def _verify_ledger_entry_signatures(
    entry: Mapping[str, Any],
    *,
    ledger_id: str,
    required_roles: Sequence[str],
    trust_keys: Mapping[str, Mapping[str, Any]],
) -> None:
    expected_hash = ledger_entry_sha256(entry, ledger_id)
    if entry["entry_sha256"] != expected_hash:
        raise PrivateCustodianError(
            "private_ledger_entry_hash_mismatch", "Ledger entry hash mismatch."
        )
    signatures = entry["signatures"]
    roles = [item["role"] for item in signatures]
    if sorted(roles) != sorted(required_roles) or len(roles) != len(set(roles)):
        raise PrivateCustodianError(
            "private_ledger_signature_roles_invalid",
            "Ledger entry signature roles are invalid.",
        )
    signed_at = _parse_timestamp(entry["event_at_utc"], "ledger event")
    for signature in signatures:
        role = signature["role"]
        anchor = trust_keys.get(role)
        if anchor is None or signature["key_id"] != anchor["key_id"]:
            raise PrivateCustodianError(
                "private_ledger_signature_key_mismatch",
                "Ledger entry signature key is not anchored.",
            )
        valid_from = _parse_timestamp(anchor["valid_from_utc"], "key validity start")
        valid_until = _parse_timestamp(anchor["valid_until_utc"], "key validity end")
        if anchor["revoked"] or not (valid_from <= signed_at <= valid_until):
            raise PrivateCustodianError(
                "private_signature_key_inactive",
                "Ledger entry signature is outside key validity.",
            )
        if signature["signed_sha256"] != expected_hash:
            raise PrivateCustodianError(
                "private_ledger_signature_hash_mismatch",
                "Ledger signature does not bind its entry hash.",
            )
        try:
            public_bytes = base64.b64decode(anchor["public_key_b64"], validate=True)
            signature_bytes = base64.b64decode(signature["signature_b64"], validate=True)
            Ed25519PublicKey.from_public_bytes(public_bytes).verify(
                signature_bytes,
                ledger_signature_message(entry["event_type"], expected_hash),
            )
        except (ValueError, binascii.Error, InvalidSignature) as exc:
            raise PrivateCustodianError(
                "private_ledger_signature_invalid",
                "Ledger entry signature verification failed.",
            ) from exc


def _provider_identity(item: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(item["provider_id"]),
        str(item["model_id"]),
        str(item["transport_id"]),
        str(item["config_sha256"]),
    )


def _provider_plan(value: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, str, str, str], ...]:
    identities = tuple(sorted(_provider_identity(item) for item in value))
    provider_ids = [item[0] for item in identities]
    if (
        len(identities) < 2
        or len(set(identities)) != len(identities)
        or len(set(provider_ids)) != len(provider_ids)
    ):
        raise PrivateCustodianError(
            "private_provider_plan_invalid", "Provider plan is incomplete or duplicated."
        )
    return identities


def _sha256_value(value: Any, domain: bytes = b"") -> str:
    return hashlib.sha256(domain + canonical_json_bytes({"value": value})).hexdigest()


def provider_plan_sha256(value: Sequence[Mapping[str, Any]]) -> str:
    normalized = [
        {
            "provider_id": provider_id,
            "model_id": model_id,
            "transport_id": transport_id,
            "config_sha256": config_sha256,
        }
        for provider_id, model_id, transport_id, config_sha256 in _provider_plan(value)
    ]
    return _sha256_value(normalized, b"researchops-private-provider-plan-v1\x00")


def private_candidate_commitment_sha256(freeze: Mapping[str, Any]) -> str:
    projection = {
        "candidate_commitment_algorithm": freeze["candidate_commitment_algorithm"],
        "lineage_predecessor_commitment_sha256": freeze[
            "lineage_predecessor_commitment_sha256"
        ],
        "component_hashes": freeze["component_hashes"],
        "trust_manifest_sha256": freeze["trust_manifest_sha256"],
        "protocol_sha256": freeze["protocol_sha256"],
        "provider_plan": freeze["provider_plan"],
        "private_case_count_target": freeze["private_case_count_target"],
        "repetitions_per_provider": freeze["repetitions_per_provider"],
        "private_campaign_limit": freeze["private_campaign_limit"],
        "metric_contract_version": freeze["metric_contract_version"],
        "small_cell_threshold": freeze["small_cell_threshold"],
        "ledger_id": freeze["ledger_id"],
        "ledger_base_sequence": freeze["ledger_base_sequence"],
        "ledger_base_head_sha256": freeze["ledger_base_head_sha256"],
        "budget_policy": freeze["budget_policy"],
        "retention_policy": freeze["retention_policy"],
    }
    return _sha256_value(
        projection, b"researchops-private-candidate-commitment-v1\x00"
    )


def _expected_rate(numerator: int, denominator: int) -> str | None:
    if denominator == 0:
        return None
    return str(
        (Decimal(numerator) / Decimal(denominator)).quantize(
            RATE_QUANTUM, rounding=ROUND_HALF_UP
        )
    )


def _wilson_95_bounds(numerator: int, denominator: int) -> tuple[str, str]:
    if denominator <= 0 or numerator < 0 or numerator > denominator:
        raise PrivateCustodianError(
            "private_metric_ci_invalid", "Wilson interval inputs are invalid."
        )
    z = 1.959963984540054
    proportion = numerator / denominator
    z_squared = z * z
    denominator_term = 1.0 + z_squared / denominator
    center = (proportion + z_squared / (2.0 * denominator)) / denominator_term
    margin = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / denominator
            + z_squared / (4.0 * denominator * denominator)
        )
        / denominator_term
    )
    lower = Decimal(str(max(0.0, center - margin))).quantize(
        RATE_QUANTUM, rounding=ROUND_HALF_UP
    )
    upper = Decimal(str(min(1.0, center + margin))).quantize(
        RATE_QUANTUM, rounding=ROUND_HALF_UP
    )
    return str(lower), str(upper)


def _verify_metric(
    metric: Mapping[str, Any],
    *,
    expected_eligible: int,
    completed_cases: int,
    terminal_status: str,
    threshold: int,
) -> None:
    metric_id = metric["metric_id"]
    if metric["direction"] != METRIC_DIRECTIONS.get(metric_id):
        raise PrivateCustodianError(
            "private_metric_direction_invalid", "Metric direction is invalid."
        )
    eligible = metric["eligible_count"]
    evaluated = metric["evaluated_count"]
    if (
        eligible != expected_eligible
        or evaluated > eligible
        or evaluated > completed_cases
        or (terminal_status == "complete" and evaluated != eligible)
    ):
        raise PrivateCustodianError(
            "private_metric_scope_invalid", "Metric eligible/evaluated scope is invalid."
        )
    if metric["coverage_rate"] != _expected_rate(evaluated, eligible):
        raise PrivateCustodianError(
            "private_metric_coverage_invalid", "Metric coverage rate is invalid."
        )
    if evaluated < threshold:
        if (
            not metric["suppressed"]
            or metric["numerator"] is not None
            or metric["rate"] is not None
            or metric["ci_method"] != "suppressed"
            or metric["ci_lower"] is not None
            or metric["ci_upper"] is not None
        ):
            raise PrivateCustodianError(
                "private_metric_small_cell_unsuppressed",
                "Small metric cell is not fully suppressed.",
            )
        return
    numerator = metric["numerator"]
    if (
        metric["suppressed"]
        or not isinstance(numerator, int)
        or numerator > evaluated
        or metric["rate"] != _expected_rate(numerator, evaluated)
        or metric["ci_method"] != "wilson_95"
    ):
        raise PrivateCustodianError(
            "private_metric_invalid", "Aggregate metric arithmetic is invalid."
        )
    expected_lower, expected_upper = _wilson_95_bounds(numerator, evaluated)
    if metric["ci_lower"] != expected_lower or metric["ci_upper"] != expected_upper:
        raise PrivateCustodianError(
            "private_metric_ci_invalid", "Aggregate Wilson interval bounds are invalid."
        )


def _verify_usage(usage: Mapping[str, Any], *, expected_coverage: int) -> bool:
    numerator = usage["coverage_numerator"]
    denominator = usage["coverage_denominator"]
    status = usage["coverage_status"]
    if (
        denominator != expected_coverage
        or numerator > denominator
        or usage["coverage_rate"] != _expected_rate(numerator, denominator)
    ):
        raise PrivateCustodianError(
            "private_result_usage_invalid", "Usage coverage arithmetic is invalid."
        )
    values = (
        usage["model_calls"], usage["input_tokens"], usage["output_tokens"],
        usage["cost_decimal"],
    )
    if status == "complete":
        if numerator != denominator or any(value is None for value in values):
            raise PrivateCustodianError(
                "private_result_usage_invalid", "Complete usage coverage is incomplete."
            )
        return True
    if status == "unavailable" and numerator != 0:
        raise PrivateCustodianError(
            "private_result_usage_invalid", "Unavailable usage must have zero coverage."
        )
    if status == "partial" and not (0 < numerator < denominator):
        raise PrivateCustodianError(
            "private_result_usage_invalid", "Partial usage coverage is invalid."
        )
    if any(value is not None for value in values):
        raise PrivateCustodianError(
            "private_result_usage_invalid", "Incomplete usage must not publish partial totals."
        )
    return False


def _verify_aggregate_results(
    result: Mapping[str, Any],
    provider_plan: tuple[tuple[str, str, str, str], ...],
    statement: Mapping[str, Any],
    authorization: Mapping[str, Any],
) -> dict[str, bool]:
    result_providers = tuple(sorted(_provider_identity(item) for item in result["provider_results"]))
    if result_providers != provider_plan:
        raise PrivateCustodianError(
            "private_result_provider_mismatch", "Aggregate result Provider plan mismatch."
        )
    expected_cases = result["expected_private_case_count"]
    expected_total = expected_cases * len(provider_plan) * 3
    if (
        expected_cases != statement["private_case_count"]
        or result["expected_total_case_executions"] != expected_total
    ):
        raise PrivateCustodianError(
            "private_result_case_count_invalid", "Expected execution scope is invalid."
        )
    eligible_counts = statement["metric_eligible_case_counts"]
    if any(value > expected_cases for value in eligible_counts.values()):
        raise PrivateCustodianError(
            "private_metric_scope_invalid", "Metric eligibility exceeds the corpus scope."
        )
    if any(eligible_counts[name] != expected_cases for name in UNIVERSAL_METRICS):
        raise PrivateCustodianError(
            "private_metric_scope_invalid", "Universal metric eligibility is incomplete."
        )
    threshold = result["small_cell_threshold"]
    if threshold != authorization["small_cell_threshold"]:
        raise PrivateCustodianError(
            "private_result_threshold_invalid", "Small-cell threshold is invalid."
        )

    total_completed = 0
    usage_rows: list[Mapping[str, Any]] = []
    completed_by_scope: dict[tuple[str, int], int] = {}
    for provider in result["provider_results"]:
        repetitions = provider["repetitions"]
        if [item["repetition_index"] for item in repetitions] != [1, 2, 3]:
            raise PrivateCustodianError(
                "private_result_repetition_invalid", "Provider repetitions must be ordered 1, 2, 3."
            )
        repetition_counts: list[int] = []
        for repetition in repetitions:
            completed = repetition["completed_case_count"]
            if (
                repetition["expected_case_count"] != expected_cases
                or completed > expected_cases
                or (result["terminal_status"] == "complete" and completed != expected_cases)
                or repetition["approval_bypass_count"] > completed
                or repetition["unexpected_tool_error_count"] > completed
            ):
                raise PrivateCustodianError(
                    "private_result_case_count_invalid", "Repetition case/count scope is invalid."
                )
            metrics = repetition["metrics"]
            metric_ids = [item["metric_id"] for item in metrics]
            if metric_ids != sorted(METRIC_DIRECTIONS) or len(set(metric_ids)) != len(metric_ids):
                raise PrivateCustodianError(
                    "private_metric_set_invalid", "Required metric set/order is invalid."
                )
            for metric in metrics:
                _verify_metric(
                    metric,
                    expected_eligible=eligible_counts[metric["metric_id"]],
                    completed_cases=completed,
                    terminal_status=result["terminal_status"],
                    threshold=threshold,
                )
            metric_by_id = {item["metric_id"]: item for item in metrics}
            if (
                not metric_by_id["approval_bypass_rate"]["suppressed"]
                and metric_by_id["approval_bypass_rate"]["numerator"]
                != repetition["approval_bypass_count"]
            ) or (
                not metric_by_id["unexpected_tool_error_rate"]["suppressed"]
                and metric_by_id["unexpected_tool_error_rate"]["numerator"]
                != repetition["unexpected_tool_error_count"]
            ):
                raise PrivateCustodianError(
                    "private_metric_count_binding_invalid",
                    "Safety/tool-error metrics do not bind aggregate counts.",
                )
            latency_p50 = repetition["latency_p50_ms"]
            latency_p95 = repetition["latency_p95_ms"]
            if (latency_p50 is None) != (latency_p95 is None) or (
                latency_p50 is not None and latency_p50 > latency_p95
            ):
                raise PrivateCustodianError(
                    "private_result_latency_invalid", "Latency percentile ordering is invalid."
                )
            _verify_usage(repetition["usage"], expected_coverage=expected_cases)
            usage_rows.append(repetition["usage"])
            total_completed += completed
            repetition_counts.append(completed)
            completed_by_scope[(provider["provider_id"], repetition["repetition_index"])] = completed
        common = min(repetition_counts)
        if (
            provider["common_completed_case_count"] != common
            or provider["stable_case_count"] > common
            or provider["all_repetitions_pass_count"] > provider["stable_case_count"]
            or provider["stability_rate"]
            != _expected_rate(provider["stable_case_count"], common)
            or provider["all_repetitions_pass_rate"]
            != _expected_rate(provider["all_repetitions_pass_count"], common)
        ):
            raise PrivateCustodianError(
                "private_result_stability_invalid", "Inter-run stability arithmetic is invalid."
            )
    if (
        result["completed_total_case_executions"] != total_completed
        or (result["terminal_status"] == "complete" and total_completed != expected_total)
    ):
        raise PrivateCustodianError(
            "private_result_case_count_invalid", "Total completed execution count is invalid."
        )

    all_usage_complete = all(row["coverage_status"] == "complete" for row in usage_rows)
    global_usage = result["budget_actual"]
    _verify_usage(global_usage, expected_coverage=expected_total)
    observed_coverage = sum(int(row["coverage_numerator"]) for row in usage_rows)
    expected_global_status = (
        "complete"
        if observed_coverage == expected_total
        else ("unavailable" if observed_coverage == 0 else "partial")
    )
    if (
        global_usage["coverage_numerator"] != observed_coverage
        or global_usage["coverage_status"] != expected_global_status
    ):
        raise PrivateCustodianError(
            "private_result_usage_invalid",
            "Global usage coverage does not reconcile to repetitions.",
        )
    if all_usage_complete:
        totals = {
            "model_calls": sum(int(row["model_calls"]) for row in usage_rows),
            "input_tokens": sum(int(row["input_tokens"]) for row in usage_rows),
            "output_tokens": sum(int(row["output_tokens"]) for row in usage_rows),
            "cost_decimal": str(
                sum(Decimal(str(row["cost_decimal"])) for row in usage_rows).quantize(
                    MONEY_QUANTUM, rounding=ROUND_HALF_UP
                )
            ),
        }
        if global_usage["coverage_status"] != "complete" or any(
            global_usage[name] != value for name, value in totals.items()
        ):
            raise PrivateCustodianError(
                "private_result_budget_total_invalid", "Global budget totals do not match repetitions."
            )
    else:
        if result["terminal_status"] == "complete" or global_usage["coverage_status"] == "complete":
            raise PrivateCustodianError(
                "private_result_usage_invalid", "Complete result requires complete usage coverage."
            )

    budget = authorization["budget_policy"]
    if any(
        row["currency"] != budget["currency"]
        or row["pricing_commitment_sha256"] != budget["pricing_commitment_sha256"]
        for row in usage_rows
    ) or (
        global_usage["currency"] != budget["currency"]
        or global_usage["pricing_commitment_sha256"] != budget["pricing_commitment_sha256"]
    ):
        raise PrivateCustodianError(
            "private_result_budget_binding_invalid", "Budget currency or pricing commitment drifted."
        )
    budget_within_cap = False
    if global_usage["coverage_status"] == "complete":
        budget_within_cap = (
            global_usage["model_calls"] <= budget["maximum_model_calls"]
            and global_usage["input_tokens"] <= budget["maximum_input_tokens"]
            and global_usage["output_tokens"] <= budget["maximum_output_tokens"]
            and Decimal(global_usage["cost_decimal"]) <= Decimal(budget["maximum_cost_decimal"])
        )
        if not budget_within_cap and result["terminal_status"] == "complete":
            raise PrivateCustodianError(
                "private_result_budget_exceeded", "Complete result exceeds an authorized budget."
            )

    commitments = statement["cell_commitments"]
    commitment_map: dict[tuple[str, int], int] = {}
    for item in commitments:
        identity = (item["cell_type"], item["cell_ordinal"])
        if identity in commitment_map:
            raise PrivateCustodianError(
                "private_cell_commitment_duplicate", "Cell commitment is duplicated."
            )
        commitment_map[identity] = item["eligible_case_count"]
    dataset_cells = [item for item in commitments if item["cell_type"] == "dataset"]
    scenario_cells = [item for item in commitments if item["cell_type"] == "scenario"]
    if (
        len(dataset_cells) != statement["dataset_count"]
        or not scenario_cells
        or sum(item["eligible_case_count"] for item in dataset_cells) != expected_cases
        or sum(item["eligible_case_count"] for item in scenario_cells) != expected_cases
    ):
        raise PrivateCustodianError(
            "private_cell_commitment_scope_invalid", "Cell commitments do not partition the corpus."
        )
    expected_cell_scope = {
        (provider_id, config_sha256, repetition, cell_type, ordinal)
        for provider_id, _model, _transport, config_sha256 in provider_plan
        for repetition in (1, 2, 3)
        for cell_type, ordinal in commitment_map
    }
    observed_cell_scope: set[tuple[str, str, int, str, int]] = set()
    evaluated_by_scope_type: dict[tuple[str, int, str], int] = {}
    numerators_by_scope_type: dict[tuple[str, int, str], int] = {}
    suppressed_evaluated_by_scope_type: dict[tuple[str, int, str], int] = {}
    canonical_cell_order: list[tuple[str, str, int, str, int]] = []
    for cell in result["aggregate_cells"]:
        identity = (
            cell["provider_id"], cell["config_sha256"], cell["repetition_index"],
            cell["cell_type"], cell["cell_ordinal"],
        )
        canonical_cell_order.append(identity)
        if identity in observed_cell_scope:
            raise PrivateCustodianError(
                "private_result_cell_duplicate", "Aggregate cell is duplicated."
            )
        observed_cell_scope.add(identity)
        commitment_identity = (cell["cell_type"], cell["cell_ordinal"])
        eligible = commitment_map.get(commitment_identity)
        evaluated = cell["evaluated_count"]
        if (
            eligible is None
            or cell["eligible_count"] != eligible
            or evaluated > eligible
            or (result["terminal_status"] == "complete" and evaluated != eligible)
        ):
            raise PrivateCustodianError(
                "private_result_cell_scope_invalid", "Aggregate cell scope is invalid."
            )
        scope_type = (cell["provider_id"], cell["repetition_index"], cell["cell_type"])
        evaluated_by_scope_type[scope_type] = evaluated_by_scope_type.get(scope_type, 0) + evaluated
        if evaluated < threshold:
            if not cell["suppressed"] or cell["numerator"] is not None or cell["rate"] is not None:
                raise PrivateCustodianError(
                    "private_result_small_cell_unsuppressed", "Small aggregate cell is not suppressed."
                )
            suppressed_evaluated_by_scope_type[scope_type] = (
                suppressed_evaluated_by_scope_type.get(scope_type, 0) + evaluated
            )
        else:
            numerator = cell["numerator"]
            if (
                cell["suppressed"]
                or not isinstance(numerator, int)
                or numerator > evaluated
                or cell["rate"] != _expected_rate(numerator, evaluated)
            ):
                raise PrivateCustodianError(
                    "private_result_cell_invalid", "Aggregate cell arithmetic is invalid."
                )
            numerators_by_scope_type[scope_type] = (
                numerators_by_scope_type.get(scope_type, 0) + numerator
            )
    if observed_cell_scope != expected_cell_scope or canonical_cell_order != sorted(canonical_cell_order):
        raise PrivateCustodianError(
            "private_result_cell_scope_invalid", "Aggregate cell scope/order is incomplete."
        )
    cell_reconciliation_exact = True
    for (provider_id, repetition), completed in completed_by_scope.items():
        provider = next(item for item in result["provider_results"] if item["provider_id"] == provider_id)
        repetition_result = provider["repetitions"][repetition - 1]
        task_success = next(
            item for item in repetition_result["metrics"] if item["metric_id"] == "task_success_rate"
        )
        for cell_type in ("dataset", "scenario"):
            if evaluated_by_scope_type.get((provider_id, repetition, cell_type), 0) != completed:
                raise PrivateCustodianError(
                    "private_result_cell_coverage_invalid", "Aggregate cells do not cover completed scope."
                )
            visible_numerator = numerators_by_scope_type.get(
                (provider_id, repetition, cell_type), 0
            )
            suppressed_evaluated = suppressed_evaluated_by_scope_type.get(
                (provider_id, repetition, cell_type), 0
            )
            task_numerator = task_success["numerator"]
            if task_numerator is None:
                cell_reconciliation_exact = False
                continue
            if not (
                visible_numerator
                <= task_numerator
                <= visible_numerator + suppressed_evaluated
            ):
                raise PrivateCustodianError(
                    "private_result_cell_numerator_invalid",
                    "Cell numerators are outside task-success suppression bounds.",
                )
            if suppressed_evaluated:
                cell_reconciliation_exact = False
    return {
        "budget_gate_verified": budget_within_cap and all_usage_complete,
        "cell_numerator_reconciliation_status": (
            "exact" if cell_reconciliation_exact else "bounded"
        ),
    }


def _verify_ledger(
    ledger: Mapping[str, Any], trust_keys: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    ledger_id = ledger["ledger_id"]
    previous = ledger["base_head_sha256"]
    base_sequence = ledger["base_sequence"]
    previous_time: datetime | None = None
    reservation_by_hash: dict[str, Mapping[str, Any]] = {}
    reservation_by_authorization: dict[str, Mapping[str, Any]] = {}
    terminal_by_reservation: dict[str, Mapping[str, Any]] = {}
    freeze_requests: set[str] = set()
    corpus_commitments: set[str] = set()
    authorization_nonces: set[str] = set()
    run_commitments: set[str] = set()
    resume_ordinals: dict[str, int] = {}
    binding_fields = (
        "freeze_request_sha256", "candidate_commitment_sha256",
        "salted_corpus_commitment_sha256", "authorization_grant_sha256",
        "authorization_nonce_sha256", "run_commitment_sha256",
        "provider_plan_sha256", "budget_policy_sha256", "retention_policy_sha256",
    )
    for offset, entry in enumerate(ledger["entries"], start=1):
        expected_sequence = base_sequence + offset
        if entry["sequence"] != expected_sequence or entry["previous_entry_sha256"] != previous:
            raise PrivateCustodianError(
                "private_ledger_chain_invalid", "Ledger sequence or previous hash is invalid."
            )
        if entry["synthetic"] is not ledger["synthetic"]:
            raise PrivateCustodianError(
                "private_ledger_synthetic_scope_mismatch",
                "Ledger entry synthetic scope differs from its signed ledger.",
            )
        event_type = entry["event_type"]
        required_roles = (
            ("freeze_authority", "custodian")
            if event_type == "resume_authorized"
            else ("custodian",)
        )
        _verify_ledger_entry_signatures(
            entry,
            ledger_id=ledger_id,
            required_roles=required_roles,
            trust_keys=trust_keys,
        )
        event_time = _parse_timestamp(entry["event_at_utc"], "ledger event")
        if previous_time is not None and event_time <= previous_time:
            raise PrivateCustodianError(
                "private_ledger_time_invalid", "Ledger event timestamps are not strictly increasing."
            )
        previous_time = event_time
        if event_type == "access_reserved":
            if (
                entry["freeze_request_sha256"] in freeze_requests
                or entry["salted_corpus_commitment_sha256"] in corpus_commitments
                or entry["authorization_grant_sha256"] in reservation_by_authorization
                or entry["authorization_nonce_sha256"] in authorization_nonces
                or entry["run_commitment_sha256"] in run_commitments
            ):
                raise PrivateCustodianError(
                    "private_authorization_replay",
                    "Freeze, authorization, or run commitment was reserved more than once.",
                )
            freeze_requests.add(entry["freeze_request_sha256"])
            corpus_commitments.add(entry["salted_corpus_commitment_sha256"])
            authorization_nonces.add(entry["authorization_nonce_sha256"])
            run_commitments.add(entry["run_commitment_sha256"])
            reservation_by_hash[entry["entry_sha256"]] = entry
            reservation_by_authorization[entry["authorization_grant_sha256"]] = entry
            resume_ordinals[entry["entry_sha256"]] = 0
        else:
            reservation_hash = entry["reservation_entry_sha256"]
            reservation = reservation_by_hash.get(reservation_hash)
            if reservation is None or any(
                entry[field] != reservation[field] for field in binding_fields
            ):
                raise PrivateCustodianError(
                    "private_ledger_event_binding_invalid",
                    "Ledger event does not bind a prior reservation.",
                )
            if reservation_hash in terminal_by_reservation:
                raise PrivateCustodianError(
                    "private_ledger_event_after_terminal", "Ledger event follows terminal closure."
                )
            if event_type == "resume_authorized":
                expected_ordinal = resume_ordinals[reservation_hash] + 1
                if entry["resume_ordinal"] != expected_ordinal:
                    raise PrivateCustodianError(
                        "private_ledger_resume_invalid", "Resume ordinal is not consecutive."
                    )
                resume_ordinals[reservation_hash] = expected_ordinal
            elif event_type == "terminal":
                terminal_by_reservation[reservation_hash] = entry
            else:
                raise PrivateCustodianError(
                    "private_ledger_event_invalid", "Ledger event type is invalid."
                )
        previous = entry["entry_sha256"]
    if (
        ledger["head_sequence"] != base_sequence + len(ledger["entries"])
        or ledger["ledger_head_sha256"] != previous
        or len(terminal_by_reservation) != len(reservation_by_hash)
    ):
        raise PrivateCustodianError(
            "private_ledger_head_mismatch", "Ledger head or terminal coverage is invalid."
        )
    signed_at = _parse_timestamp(ledger["signed_at_utc"], "ledger signature")
    if previous_time is None or signed_at < previous_time:
        raise PrivateCustodianError(
            "private_ledger_time_invalid", "Ledger signature precedes its final event."
        )
    return {
        "reservation_by_authorization": reservation_by_authorization,
        "terminal_by_reservation": terminal_by_reservation,
        "resume_ordinals": resume_ordinals,
        "last_event_at": previous_time,
    }


def _verify_resume_window(
    ledger: Mapping[str, Any], *, reservation_entry_sha256: str, expires_at: datetime
) -> None:
    for ledger_entry in ledger["entries"]:
        if (
            ledger_entry["event_type"] == "resume_authorized"
            and ledger_entry["reservation_entry_sha256"] == reservation_entry_sha256
            and _parse_timestamp(ledger_entry["event_at_utc"], "resume authorization")
            > expires_at
        ):
            raise PrivateCustodianError(
                "private_ledger_resume_expired",
                "Resume authorization occurs after the grant expiry.",
            )


_ACCESS_RESERVATION_FIELDS = frozenset(
    {
        "event_type", "synthetic", "sequence", "previous_entry_sha256", "event_at_utc",
        "freeze_request_sha256", "candidate_commitment_sha256",
        "salted_corpus_commitment_sha256", "authorization_grant_sha256",
        "authorization_nonce_sha256", "run_commitment_sha256", "provider_plan_sha256",
        "budget_policy_sha256", "retention_policy_sha256", "entry_sha256", "signatures",
    }
)


def _validate_access_reservation_shape(entry: Mapping[str, Any]) -> None:
    if set(entry) != _ACCESS_RESERVATION_FIELDS or entry.get("event_type") != "access_reserved":
        raise PrivateCustodianError(
            "private_access_reservation_invalid", "Access reservation fields are invalid."
        )
    if entry.get("synthetic") is not True:
        raise PrivateCustodianError(
            "private_non_synthetic_reservation_not_supported",
            "Protocol 1.1 atomic writer accepts synthetic conformance reservations only.",
        )
    if not isinstance(entry.get("sequence"), int) or entry["sequence"] < 1:
        raise PrivateCustodianError(
            "private_access_reservation_invalid", "Access reservation sequence is invalid."
        )
    for field in _ACCESS_RESERVATION_FIELDS:
        if field.endswith("_sha256") and (
            not isinstance(entry[field], str) or _SHA256.fullmatch(entry[field]) is None
        ):
            raise PrivateCustodianError(
                "private_access_reservation_invalid", "Access reservation digest is invalid."
            )
        if (
            field.endswith("_sha256")
            and field != "previous_entry_sha256"
            and entry[field] == ZERO_SHA256
        ):
            raise PrivateCustodianError(
                "private_access_reservation_invalid",
                "Access reservation commitment cannot be zero.",
            )
    _parse_timestamp(str(entry["event_at_utc"]), "access reservation")


def reserve_access_atomically(
    *,
    project_root: Path,
    registry_dir: Path,
    ledger_id: str,
    entry: Mapping[str, Any],
    trust_keys: Mapping[str, Mapping[str, Any]],
    expected_base_sequence: int,
    expected_base_head_sha256: str,
) -> dict[str, Any]:
    """Atomically consume one synthetic reservation in an external registry.

    A normal process failure after the deterministic reservation marker is created
    leaves that marker in place under the tested filesystem semantics. The caller
    must treat such an incomplete state as consumed; this function never deletes
    or retries it. Power-loss durability is not established by this primitive.
    """

    _validate_access_reservation_shape(entry)
    if re.fullmatch(r"LEDGER-[A-F0-9]{16}", ledger_id) is None:
        raise PrivateCustodianError(
            "private_registry_anchor_invalid", "Registry ledger identity is invalid."
        )
    if _SHA256.fullmatch(expected_base_head_sha256) is None:
        raise PrivateCustodianError(
            "private_registry_anchor_invalid", "Registry base anchor is invalid."
        )
    if (expected_base_sequence == 0) != (expected_base_head_sha256 == ZERO_SHA256):
        raise PrivateCustodianError(
            "private_registry_anchor_invalid", "Registry base sequence/head pair is invalid."
        )
    try:
        project = project_root.resolve(strict=True)
        system_temp = Path(tempfile.gettempdir()).resolve(strict=True)
    except OSError as exc:
        raise PrivateCustodianError(
            "private_registry_directory_invalid", "Registry boundary cannot be resolved."
        ) from exc
    _assert_no_reparse_ancestry(
        registry_dir,
        code="private_registry_directory_invalid",
        message="Registry directory ancestry contains a link or reparse point.",
    )
    try:
        registry = registry_dir.resolve(strict=True)
    except OSError as exc:
        raise PrivateCustodianError(
            "private_registry_directory_invalid", "Registry directory cannot be resolved."
        ) from exc
    if not registry.is_dir() or _is_reparse_or_symlink(registry):
        raise PrivateCustodianError(
            "private_registry_directory_invalid", "Registry must be a regular directory."
        )
    if _is_relative_to(registry, project) or _is_relative_to(registry, system_temp):
        raise PrivateCustodianError(
            "private_registry_not_external", "Registry must be outside repository and temp roots."
        )
    lock_path = registry / "ledger.lock"
    marker_path = registry / f"freeze-{entry['freeze_request_sha256']}.reservation.json"
    head_path = registry / "ledger-head.json"
    next_head_path = registry / "ledger-head.next"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        lock_descriptor = os.open(lock_path, flags, 0o600)
    except FileExistsError as exc:
        raise PrivateCustodianError(
            "private_registry_busy", "External reservation registry is locked."
        ) from exc
    try:
        os.write(lock_descriptor, b"locked\n")
        os.fsync(lock_descriptor)
    finally:
        os.close(lock_descriptor)
    marker_created = False
    try:
        if head_path.exists():
            head = load_json_object(head_path, "ledger head")
            if set(head) != {"ledger_id", "head_sequence", "ledger_head_sha256"}:
                raise PrivateCustodianError(
                    "private_registry_head_invalid", "Registry head fields are invalid."
                )
            observed_sequence = head["head_sequence"]
            observed_head = head["ledger_head_sha256"]
            if head["ledger_id"] != ledger_id:
                raise PrivateCustodianError(
                    "private_registry_head_invalid", "Registry ledger identity drifted."
                )
        else:
            observed_sequence = 0
            observed_head = ZERO_SHA256
        if (
            observed_sequence != expected_base_sequence
            or observed_head != expected_base_head_sha256
            or entry["sequence"] != expected_base_sequence + 1
            or entry["previous_entry_sha256"] != expected_base_head_sha256
        ):
            raise PrivateCustodianError(
                "private_registry_anchor_mismatch", "Registry base anchor changed."
            )
        _verify_ledger_entry_signatures(
            entry,
            ledger_id=ledger_id,
            required_roles=("custodian",),
            trust_keys=trust_keys,
        )
        payload = canonical_json_bytes(entry) + b"\n"
        try:
            marker_descriptor = os.open(marker_path, flags, 0o600)
        except FileExistsError as exc:
            raise PrivateCustodianError(
                "private_authorization_replay", "Freeze reservation already exists."
            ) from exc
        marker_created = True
        try:
            os.write(marker_descriptor, payload)
            os.fsync(marker_descriptor)
        finally:
            os.close(marker_descriptor)
        head_payload = canonical_json_bytes(
            {
                "ledger_id": ledger_id,
                "head_sequence": entry["sequence"],
                "ledger_head_sha256": entry["entry_sha256"],
            }
        ) + b"\n"
        try:
            head_descriptor = os.open(next_head_path, flags, 0o600)
            try:
                os.write(head_descriptor, head_payload)
                os.fsync(head_descriptor)
            finally:
                os.close(head_descriptor)
            os.replace(next_head_path, head_path)
        except OSError as exc:
            raise PrivateCustodianError(
                "private_registry_incomplete_consumption",
                "Reservation marker was created but registry head update failed.",
            ) from exc
        return {
            "status": "synthetic_authorization_consumed",
            "synthetic_conformance_only": True,
            "authorization_consumed": True,
            "atomic_create_if_absent": True,
            "process_failure_marker_retained": True,
            "power_loss_durability_verified": False,
            "ledger_head_sequence": entry["sequence"],
            "ledger_head_sha256": entry["entry_sha256"],
            "private_access_may_proceed": False,
            "network_calls": 0,
        }
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            if not marker_created:
                raise PrivateCustodianError(
                    "private_registry_lock_cleanup_failed",
                    "Registry lock cleanup failed before consumption.",
                )


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_release(
    *,
    project_root: Path,
    release_dir: Path,
    expected_trust_manifest_sha256: str,
    expected_freeze_request_sha256: str,
    expected_candidate_commitment_sha256: str,
    expected_ledger_base_sequence: int,
    expected_ledger_base_head_sha256: str,
    expected_access_reservation_entry_sha256: str,
    expected_ledger_final_sequence: int,
    expected_ledger_final_head_sha256: str,
) -> dict[str, Any]:
    project = project_root.resolve(strict=True)
    kit_root = project / "evals" / "v2" / "private_holdout_kit"
    protocol_hash = _hash_file(kit_root / "protocol.json")
    schema_hash = schema_bundle_sha256(kit_root / "schemas")
    if (
        protocol_hash != EXPECTED_PROTOCOL_SHA256
        or schema_hash != EXPECTED_SCHEMA_BUNDLE_SHA256
    ):
        raise PrivateCustodianError(
            "private_kit_bundle_drift",
            "Release verifier protocol or schema bundle differs from reviewed v1.1.",
        )
    schemas = _load_schemas(kit_root)
    _assert_no_reparse_ancestry(
        release_dir,
        code="private_release_directory_invalid",
        message="Release directory ancestry contains a link or reparse point.",
    )
    try:
        release = release_dir.resolve(strict=True)
    except OSError as exc:
        raise PrivateCustodianError(
            "private_release_directory_invalid", "Release directory cannot be resolved."
        ) from exc
    if not release.is_dir() or _is_reparse_or_symlink(release):
        raise PrivateCustodianError(
            "private_release_directory_invalid", "Release directory must be a regular directory."
        )
    try:
        release_stat_before = release.stat(follow_symlinks=False)
    except OSError as exc:
        raise PrivateCustodianError(
            "private_release_directory_invalid", "Release directory metadata is unavailable."
        ) from exc
    actual_names = {path.name for path in release.iterdir()}
    if actual_names != set(RELEASE_FILES.values()):
        raise PrivateCustodianError(
            "private_release_file_scope_invalid", "Release directory file scope is invalid."
        )
    release_payload_hashes: dict[str, str] = {}
    documents: dict[str, dict[str, Any]] = {}
    for name, filename in RELEASE_FILES.items():
        payload = _read_regular_file_no_follow(release / filename, name)
        release_payload_hashes[filename] = hashlib.sha256(payload).hexdigest()
        documents[name] = _parse_json_object_payload(payload, name)
    for name, document in documents.items():
        _validate_schema(document, schemas[name], name)
        assert_public_release_projection(document)

    def assert_release_snapshot_unchanged() -> None:
        try:
            observed_names = {path.name for path in release.iterdir()}
            observed = release.stat(follow_symlinks=False)
        except OSError as exc:
            raise PrivateCustodianError(
                "private_release_directory_changed",
                "Release directory changed during verification.",
            ) from exc
        if (
            observed_names != set(RELEASE_FILES.values())
            or observed.st_dev != release_stat_before.st_dev
            or observed.st_ino != release_stat_before.st_ino
            or observed.st_mtime_ns != release_stat_before.st_mtime_ns
            or observed.st_ctime_ns != release_stat_before.st_ctime_ns
        ):
            raise PrivateCustodianError(
                "private_release_directory_changed",
                "Release directory changed during verification.",
            )
        for name, filename in RELEASE_FILES.items():
            payload = _read_regular_file_no_follow(release / filename, name)
            if hashlib.sha256(payload).hexdigest() != release_payload_hashes[filename]:
                raise PrivateCustodianError(
                    "private_release_file_changed",
                    "Release file changed during verification.",
                )

    assert_release_snapshot_unchanged()
    synthetic_values = {document["synthetic"] for document in documents.values()}
    if len(synthetic_values) != 1:
        raise PrivateCustodianError(
            "private_synthetic_scope_mismatch", "Release mixes synthetic and non-synthetic documents."
        )
    if synthetic_values != {True}:
        raise PrivateCustodianError(
            "private_non_synthetic_release_not_supported",
            "Protocol 1.1 fails closed for non-synthetic releases.",
        )

    trust = documents["trust_manifest"]
    observed_trust_hash = trust_manifest_sha256(trust)
    if (
        _SHA256.fullmatch(expected_trust_manifest_sha256) is None
        or trust["manifest_sha256"] != observed_trust_hash
        or observed_trust_hash != expected_trust_manifest_sha256
    ):
        raise PrivateCustodianError(
            "private_trust_anchor_mismatch", "Trust manifest does not match the external anchor."
        )
    keys = _trust_keys(trust)
    organizations = {item["organization_commitment_sha256"] for item in trust["keys"]}
    if len(organizations) != 2:
        raise PrivateCustodianError(
            "private_trust_role_separation_invalid",
            "Trust roles require distinct anchored organization commitments.",
        )
    freeze = documents["freeze_request"]
    statement = documents["commitment_statement"]
    authorization = documents["authorization_grant"]
    result = documents["aggregate_results"]
    receipt = documents["consumption_receipt"]
    ledger = documents["signed_ledger"]
    manifest = documents["release_manifest"]

    signature_times = {
        "freeze_request": _parse_timestamp(freeze["created_at_utc"], "request created"),
        "commitment_statement": _parse_timestamp(statement["created_at_utc"], "commitment created"),
        "authorization_grant": _parse_timestamp(authorization["authorized_at_utc"], "authorized"),
        "aggregate_results": _parse_timestamp(result["completed_at_utc"], "aggregate completion"),
        "signed_ledger": _parse_timestamp(ledger["signed_at_utc"], "ledger signature"),
        "consumption_receipt": _parse_timestamp(receipt["receipt_created_at_utc"], "receipt created"),
        "release_manifest": _parse_timestamp(manifest["released_at_utc"], "release"),
    }
    required_roles = {
        "freeze_request": ("freeze_authority",),
        "commitment_statement": ("custodian",),
        "authorization_grant": ("freeze_authority", "custodian"),
        "aggregate_results": ("custodian",),
        "consumption_receipt": ("custodian",),
        "signed_ledger": ("custodian",),
        "release_manifest": ("custodian",),
    }
    for name, roles in required_roles.items():
        _verify_signatures(
            documents[name], roles, keys, signed_at=signature_times[name]
        )

    verifier_hash = _hash_file(Path(__file__).resolve(strict=True))
    request_hash = freeze["document_sha256"]
    candidate_hash = freeze["candidate_commitment_sha256"]
    if (
        freeze["trust_manifest_sha256"] != observed_trust_hash
        or freeze["protocol_sha256"] != protocol_hash
        or freeze["component_hashes"]["private_protocol_sha256"] != protocol_hash
        or freeze["component_hashes"]["private_schema_bundle_sha256"] != schema_hash
        or freeze["component_hashes"]["private_verifier_sha256"] != verifier_hash
    ):
        raise PrivateCustodianError(
            "private_freeze_component_mismatch", "Freeze request does not bind the kit components."
        )
    external_hashes = (
        expected_trust_manifest_sha256,
        expected_freeze_request_sha256,
        expected_candidate_commitment_sha256,
        expected_ledger_base_head_sha256,
        expected_access_reservation_entry_sha256,
        expected_ledger_final_head_sha256,
    )
    if any(_SHA256.fullmatch(value) is None for value in external_hashes):
        raise PrivateCustodianError(
            "private_external_anchor_invalid", "An external anchor is not a SHA-256 digest."
        )
    if (
        request_hash != expected_freeze_request_sha256
        or candidate_hash != expected_candidate_commitment_sha256
        or candidate_hash != private_candidate_commitment_sha256(freeze)
    ):
        raise PrivateCustodianError(
            "private_candidate_anchor_mismatch",
            "Freeze request or candidate commitment does not match external anchors.",
        )
    if (
        freeze["ledger_base_sequence"] != expected_ledger_base_sequence
        or freeze["ledger_base_head_sha256"] != expected_ledger_base_head_sha256
        or (expected_ledger_base_sequence == 0) != (expected_ledger_base_head_sha256 == ZERO_SHA256)
    ):
        raise PrivateCustodianError(
            "private_ledger_base_anchor_mismatch", "Ledger base anchor is invalid."
        )
    freeze_plan = _provider_plan(freeze["provider_plan"])
    if freeze["private_case_count_target"] < 50:
        raise PrivateCustodianError(
            "private_case_count_below_minimum", "Private case count is below the protocol minimum."
        )
    statement_hash = statement["document_sha256"]
    authorization_hash = authorization["document_sha256"]
    result_hash = result["document_sha256"]
    receipt_hash = receipt["document_sha256"]
    ledger_hash = ledger["document_sha256"]

    if (
        statement["freeze_request_sha256"] != request_hash
        or statement["candidate_commitment_sha256"] != freeze["candidate_commitment_sha256"]
        or statement["private_case_count"] != freeze["private_case_count_target"]
        or statement["non_synthetic_dataset_count"] > statement["dataset_count"]
        or statement["pilot_participant_derived_data_present"]
        or statement["direct_or_quasi_identifier_data_present"]
    ):
        raise PrivateCustodianError(
            "private_commitment_binding_mismatch", "Corpus commitment statement binding is invalid."
        )
    order_scope = {
        (item["provider_id"], item["config_sha256"], item["repetition_index"])
        for item in statement["provider_order_commitments"]
    }
    expected_order_scope = {
        (provider[0], provider[3], repetition)
        for provider in freeze_plan
        for repetition in (1, 2, 3)
    }
    order_hashes_by_provider: dict[str, set[str]] = {}
    for item in statement["provider_order_commitments"]:
        order_hashes_by_provider.setdefault(item["provider_id"], set()).add(item["order_sha256"])
    if (
        order_scope != expected_order_scope
        or len(order_scope) != len(statement["provider_order_commitments"])
        or any(len(values) != 3 for values in order_hashes_by_provider.values())
    ):
        raise PrivateCustodianError(
            "private_order_commitment_invalid", "Provider repetition order commitments are incomplete."
        )
    if (
        authorization["freeze_request_sha256"] != request_hash
        or authorization["commitment_statement_sha256"] != statement_hash
        or authorization["candidate_commitment_sha256"] != freeze["candidate_commitment_sha256"]
        or authorization["salted_corpus_commitment_sha256"]
        != statement["salted_corpus_commitment_sha256"]
        or authorization["private_case_count"] != statement["private_case_count"]
        or _provider_plan(authorization["provider_plan"]) != freeze_plan
        or authorization["run_commitment_sha256"] == ZERO_SHA256
        or authorization["ledger_id"] != freeze["ledger_id"]
        or authorization["ledger_base_sequence"] != freeze["ledger_base_sequence"]
        or authorization["ledger_base_head_sha256"] != freeze["ledger_base_head_sha256"]
        or authorization["small_cell_threshold"] != freeze["small_cell_threshold"]
        or authorization["budget_policy"] != freeze["budget_policy"]
        or authorization["retention_policy"] != freeze["retention_policy"]
    ):
        raise PrivateCustodianError(
            "private_authorization_binding_mismatch", "Authorization binding is invalid."
        )
    trust_created = _parse_timestamp(trust["created_at_utc"], "trust created")
    request_created = signature_times["freeze_request"]
    request_expires = _parse_timestamp(freeze["expires_at_utc"], "request expiry")
    statement_created = signature_times["commitment_statement"]
    authorized_at = signature_times["authorization_grant"]
    not_before = _parse_timestamp(authorization["not_before_utc"], "not-before")
    authorization_expires = _parse_timestamp(authorization["expires_at_utc"], "authorization expiry")
    if not (
        trust_created <= request_created <= statement_created <= request_expires
        and statement_created <= authorized_at <= request_expires
        and authorized_at <= not_before < authorization_expires <= request_expires
    ):
        raise PrivateCustodianError(
            "private_authorization_time_invalid", "Authorization time window is invalid."
        )
    if (
        result["freeze_request_sha256"] != request_hash
        or result["commitment_statement_sha256"] != statement_hash
        or result["authorization_grant_sha256"] != authorization_hash
        or result["candidate_commitment_sha256"] != freeze["candidate_commitment_sha256"]
        or result["salted_corpus_commitment_sha256"]
        != statement["salted_corpus_commitment_sha256"]
        or result["expected_private_case_count"] != statement["private_case_count"]
        or result["run_commitment_sha256"] != authorization["run_commitment_sha256"]
    ):
        raise PrivateCustodianError(
            "private_result_binding_mismatch", "Aggregate result binding is invalid."
        )
    if (
        ledger["trust_manifest_sha256"] != observed_trust_hash
        or ledger["ledger_id"] != freeze["ledger_id"]
        or ledger["base_sequence"] != expected_ledger_base_sequence
        or ledger["base_head_sha256"] != expected_ledger_base_head_sha256
        or ledger["head_sequence"] != expected_ledger_final_sequence
        or ledger["ledger_head_sha256"] != expected_ledger_final_head_sha256
    ):
        raise PrivateCustodianError(
            "private_ledger_anchor_mismatch", "Ledger does not match supplied external anchors."
        )
    ledger_state = _verify_ledger(ledger, keys)
    reservation = ledger_state["reservation_by_authorization"].get(authorization_hash)
    if reservation is None or reservation["entry_sha256"] != expected_access_reservation_entry_sha256:
        raise PrivateCustodianError(
            "private_authorization_consumption_invalid", "Anchored access reservation is missing."
        )
    terminal = ledger_state["terminal_by_reservation"].get(reservation["entry_sha256"])
    if terminal is None:
        raise PrivateCustodianError(
            "private_ledger_terminal_missing", "Reserved authorization lacks terminal closure."
        )
    plan_hash = provider_plan_sha256(freeze["provider_plan"])
    budget_hash = _sha256_value(
        freeze["budget_policy"], b"researchops-private-budget-policy-v1\x00"
    )
    retention_hash = _sha256_value(
        freeze["retention_policy"], b"researchops-private-retention-policy-v1\x00"
    )
    if (
        reservation["freeze_request_sha256"] != request_hash
        or reservation["candidate_commitment_sha256"] != candidate_hash
        or reservation["salted_corpus_commitment_sha256"]
        != statement["salted_corpus_commitment_sha256"]
        or reservation["authorization_nonce_sha256"] != authorization["authorization_nonce_sha256"]
        or reservation["run_commitment_sha256"] != authorization["run_commitment_sha256"]
        or reservation["provider_plan_sha256"] != plan_hash
        or reservation["budget_policy_sha256"] != budget_hash
        or reservation["retention_policy_sha256"] != retention_hash
        or terminal["aggregate_results_sha256"] != result_hash
        or terminal["terminal_status"] != result["terminal_status"]
        or terminal["completed_total_case_executions"]
        != result["completed_total_case_executions"]
        or result["access_reservation_entry_sha256"] != reservation["entry_sha256"]
    ):
        raise PrivateCustodianError(
            "private_ledger_release_binding_invalid", "Ledger/release commitments drifted."
        )
    resume_count = ledger_state["resume_ordinals"][reservation["entry_sha256"]]
    if resume_count > authorization["maximum_resume_count"]:
        raise PrivateCustodianError(
            "private_ledger_resume_invalid", "Resume count exceeds authorization."
        )
    reserved_at = _parse_timestamp(reservation["event_at_utc"], "access reservation")
    _verify_resume_window(
        ledger,
        reservation_entry_sha256=reservation["entry_sha256"],
        expires_at=authorization_expires,
    )
    completed_at = signature_times["aggregate_results"]
    terminal_at = _parse_timestamp(terminal["event_at_utc"], "terminal")
    if not (
        not_before <= reserved_at <= authorization_expires <= request_expires
        and reserved_at < completed_at == terminal_at
    ):
        raise PrivateCustodianError(
            "private_receipt_time_invalid", "Reservation/result timestamps are outside authorization."
        )
    aggregate_checks = _verify_aggregate_results(
        result, freeze_plan, statement, authorization
    )
    if (
        receipt["authorization_grant_sha256"] != authorization_hash
        or receipt["freeze_request_sha256"] != request_hash
        or receipt["commitment_statement_sha256"] != statement_hash
        or receipt["aggregate_results_sha256"] != result_hash
        or receipt["signed_ledger_sha256"] != ledger_hash
        or receipt["candidate_commitment_sha256"] != freeze["candidate_commitment_sha256"]
        or receipt["salted_corpus_commitment_sha256"]
        != statement["salted_corpus_commitment_sha256"]
        or receipt["terminal_status"] != result["terminal_status"]
        or receipt["run_commitment_sha256"] != reservation["run_commitment_sha256"]
        or receipt["ledger_id"] != ledger["ledger_id"]
        or receipt["ledger_base_sequence"] != ledger["base_sequence"]
        or receipt["ledger_base_head_sha256"] != ledger["base_head_sha256"]
        or receipt["ledger_final_sequence"] != ledger["head_sequence"]
        or receipt["ledger_final_head_sha256"] != ledger["ledger_head_sha256"]
        or receipt["access_reservation_entry_sha256"] != reservation["entry_sha256"]
        or receipt["terminal_entry_sha256"] != terminal["entry_sha256"]
        or receipt["resume_count"] != resume_count
        or receipt["pilot_participant_derived_data_present"]
        or receipt["direct_or_quasi_identifier_data_present"]
    ):
        raise PrivateCustodianError(
            "private_receipt_binding_mismatch", "Consumption receipt binding is invalid."
        )
    receipt_completed = _parse_timestamp(receipt["completed_at_utc"], "receipt completion")
    receipt_created = signature_times["consumption_receipt"]
    plaintext_deleted = _parse_timestamp(
        receipt["private_plaintext_deleted_at_utc"], "plaintext deletion"
    )
    maximum_retention = timedelta(
        hours=freeze["retention_policy"]["maximum_plaintext_retention_hours"]
    )
    if not (
        receipt["access_reserved_at_utc"] == reservation["event_at_utc"]
        and receipt_completed == completed_at
        and completed_at <= plaintext_deleted <= completed_at + maximum_retention
        and signature_times["signed_ledger"] <= receipt_created
        and plaintext_deleted <= receipt_created
        and receipt["human_subject_data_classification"] == "none"
        and receipt["provider_log_attestation_status"] == "synthetic_not_applicable"
        and receipt["proxy_log_attestation_status"] == "synthetic_not_applicable"
        and receipt["backup_attestation_status"] == "synthetic_not_applicable"
        and receipt["retention_gate_status"] == "synthetic_not_applicable"
    ):
        raise PrivateCustodianError(
            "private_retention_attestation_invalid",
            "Synthetic retention/disposition attestation is invalid.",
        )
    expected_manifest_hashes = {
        "trust_manifest_sha256": observed_trust_hash,
        "freeze_request_sha256": request_hash,
        "commitment_statement_sha256": statement_hash,
        "authorization_grant_sha256": authorization_hash,
        "aggregate_results_sha256": result_hash,
        "consumption_receipt_sha256": receipt_hash,
        "signed_ledger_sha256": ledger_hash,
    }
    if any(manifest[name] != value for name, value in expected_manifest_hashes.items()):
        raise PrivateCustodianError(
            "private_release_manifest_mismatch", "Release manifest digest binding is invalid."
        )
    anchor_echoes = {
        "anchored_trust_manifest_sha256": expected_trust_manifest_sha256,
        "anchored_freeze_request_sha256": expected_freeze_request_sha256,
        "anchored_candidate_commitment_sha256": expected_candidate_commitment_sha256,
        "anchored_ledger_base_head_sha256": expected_ledger_base_head_sha256,
        "anchored_access_reservation_entry_sha256": expected_access_reservation_entry_sha256,
        "anchored_ledger_final_head_sha256": expected_ledger_final_head_sha256,
        "anchored_ledger_final_sequence": expected_ledger_final_sequence,
    }
    if any(manifest[name] != value for name, value in anchor_echoes.items()):
        raise PrivateCustodianError(
            "private_release_anchor_echo_mismatch", "Release manifest anchor echoes drifted."
        )
    expected_release_status = {
        "complete": "verified_aggregate_only",
        "stopped": "stopped_aggregate_only",
        "aborted": "aborted_aggregate_only",
    }[result["terminal_status"]]
    if manifest["release_status"] != expected_release_status:
        raise PrivateCustodianError(
            "private_release_status_mismatch", "Release status does not match terminal result."
        )
    released_at = signature_times["release_manifest"]
    if (
        released_at < receipt_created
        or manifest["retention_gate_status"] != receipt["retention_gate_status"]
    ):
        raise PrivateCustodianError(
            "private_release_time_invalid", "Release timestamp precedes completion."
        )
    assert_release_snapshot_unchanged()
    return {
        "status": "valid",
        "protocol_id": "eval-v2-private-holdout-custodian-v1",
        "release_status": manifest["release_status"],
        "synthetic": True,
        "synthetic_conformance_only": True,
        "non_synthetic_release_supported": False,
        "provider_count": len(freeze_plan),
        "private_case_count": statement["private_case_count"],
        "repetitions_per_provider": 3,
        "authorization_consumption_count": 1,
        "external_anchors_matched": True,
        "single_use_within_externally_anchored_ledger_scope": True,
        "aggregate_arithmetic_verified": (
            aggregate_checks["cell_numerator_reconciliation_status"] == "exact"
        ),
        "aggregate_arithmetic_bounds_verified": True,
        "cell_numerator_reconciliation_status": aggregate_checks[
            "cell_numerator_reconciliation_status"
        ],
        "budget_gate_verified": aggregate_checks["budget_gate_verified"],
        "custodian_retention_attestation_valid": True,
        "signature_roles_verified": ["freeze_authority", "custodian"],
        "prohibited_release_fields_or_known_patterns_detected": False,
        "release_allowlist_revalidated_before_return": True,
        "release_directory_immutable": False,
        "verified_release_bundle_sha256": _sha256_value(
            release_payload_hashes,
            b"researchops-private-release-byte-bundle-v1\x00",
        ),
        "repository_private_content_scan_performed": False,
        "private_holdout_claim_allowed": False,
        "model_quality_claim_allowed": False,
        "network_calls": 0,
    }


def _validate_repository_state(project_root: Path) -> dict[str, Any]:
    source_root = (project_root / "src").resolve(strict=True)
    if source_root.parent != project_root or not source_root.is_dir():
        raise PrivateCustodianError(
            "private_repository_layout_invalid", "Repository source layout is invalid."
        )
    source_text = str(source_root)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    try:
        from researchops.eval_v2_freeze import validate_public_regression_candidate
        from researchops.eval_v2_public import validate_eval_v2_suite

        suite = validate_eval_v2_suite(
            campaign_path=project_root / "evals" / "v2" / "campaign.json",
            dataset_manifest_path=project_root / "evals" / "v2" / "external_datasets.json",
            public_tasks_path=project_root / "evals" / "v2" / "public_tasks.jsonl",
            task_schema_path=project_root / "evals" / "v2" / "public_task_schema.json",
            internal_review_path=project_root / "evals" / "v2" / "internal_review.json",
        )
        candidate = validate_public_regression_candidate(
            project_root=project_root,
            candidate_path=project_root / "evals" / "v2" / "public_regression_candidate_v7.json",
            verify_environment=False,
        )
    except Exception as exc:
        raise PrivateCustodianError(
            "private_repository_contract_invalid",
            "Repository Eval v2 contracts or public candidate are invalid.",
        ) from exc
    if (
        candidate["full_campaign_frozen"]
        or candidate["private_holdout_access_authorized"]
        or candidate["model_quality_claim_allowed"]
        or candidate.get("historical_snapshot_only") is not True
    ):
        raise PrivateCustodianError(
            "private_public_candidate_boundary_invalid",
            "Public-only candidate crossed a private authorization boundary.",
        )
    return {"suite": suite, "candidate": candidate}


def readiness_status(project_root: Path) -> dict[str, Any]:
    validated = _validate_repository_state(project_root)
    campaign = load_json_object(project_root / "evals" / "v2" / "campaign.json", "campaign")
    private_split = campaign["splits"]["private_holdout"]
    registered_providers = [
        item for item in campaign["run_policy"]["providers"]
        if item["status"] == "registered"
    ]
    freeze_hashes = campaign["freeze_policy"]["hashes"]
    request_gaps: list[str] = []
    if validated["candidate"].get("historical_snapshot_only") is True:
        request_gaps.append("historical_candidate_execution_forbidden")
    if campaign["status"] != "frozen":
        request_gaps.append("campaign_not_frozen")
    if private_split["registered_task_count"] < private_split["target_task_count"]:
        request_gaps.append("private_cases_below_target")
    if len(registered_providers) < campaign["run_policy"]["minimum_provider_count"]:
        request_gaps.append("provider_count_below_minimum")
    if campaign["private_holdout_policy"]["commitment_sha256"] is None:
        request_gaps.append("private_corpus_commitment_missing")
    if campaign["external_review_policy"]["golden_review_status"] != "completed":
        request_gaps.append("golden_review_incomplete")
    if campaign["external_review_policy"]["statistical_crosscheck_status"] != "completed":
        request_gaps.append("statistical_crosscheck_incomplete")
    missing_hashes = sorted(name for name, value in freeze_hashes.items() if value is None)
    if missing_hashes:
        request_gaps.append("freeze_hashes_incomplete")
    access_gaps = [*request_gaps, "private_access_authorization_missing"]
    return {
        "status": "not_authorized",
        "campaign_status": campaign["status"],
        "private_registered_case_count": private_split["registered_task_count"],
        "private_target_case_count": private_split["target_task_count"],
        "registered_provider_count": len(registered_providers),
        "minimum_provider_count": campaign["run_policy"]["minimum_provider_count"],
        "request_readiness_gaps": request_gaps,
        "access_readiness_gaps": access_gaps,
        "private_request_allowed": not request_gaps,
        "private_access_authorized": False,
        "public_candidate_commitment_sha256": validated["candidate"][
            "candidate_commitment_sha256"
        ],
        "public_candidate_historical_snapshot_only": True,
        "public_candidate_full_campaign_frozen": False,
        "non_synthetic_release_supported": False,
        "model_quality_claim_allowed": False,
        "network_calls": 0,
    }


def verify_kit(project_root: Path) -> dict[str, Any]:
    kit_root = project_root / "evals" / "v2" / "private_holdout_kit"
    protocol = load_json_object(kit_root / "protocol.json", "private protocol")
    schemas = _load_schemas(kit_root)
    protocol_hash = _hash_file(kit_root / "protocol.json")
    schema_hash = schema_bundle_sha256(kit_root / "schemas")
    if protocol_hash != EXPECTED_PROTOCOL_SHA256 or schema_hash != EXPECTED_SCHEMA_BUNDLE_SHA256:
        raise PrivateCustodianError(
            "private_kit_bundle_drift", "Private protocol or schema bundle drifted."
        )
    if (
        protocol.get("schema_version") != KIT_VERSION
        or protocol.get("protocol_id") != "eval-v2-private-holdout-custodian-v1"
        or protocol.get("status") != "kit_ready_not_authorized"
        or protocol.get("minimum_private_case_count") != 50
        or protocol.get("minimum_provider_count") != 2
        or protocol.get("repetitions_per_provider") != 3
        or protocol.get("maximum_private_campaigns_per_freeze") != 1
        or protocol.get("verification_scope")
        != "synthetic_conformance_and_supplied_external_anchors_only"
        or protocol.get("non_synthetic_release_supported") is not False
        or protocol.get("private_access") != {
            "must_occur_in_custodian_environment": True,
            "authorization_consumed_before_access": True,
            "two_stage_ledger_required": True,
            "external_ledger_anchors_required": True,
            "repository_private_content_allowed": False,
            "project_receives_private_content": False,
            "single_campaign_per_freeze": True,
            "ambiguous_inflight_replay_allowed": False,
            "same_run_resume_requires_identical_commitments": True,
        }
        or protocol.get("release_policy") != {
            "aggregate_only": True,
            "task_ids_allowed": False,
            "task_order_allowed": False,
            "prompts_allowed": False,
            "goldens_allowed": False,
            "locators_allowed": False,
            "per_case_results_allowed": False,
            "raw_provider_content_allowed": False,
            "free_text_allowed": False,
            "unsigned_release_allowed": False,
        }
        or protocol.get("retention_policy") != {
            "pilot_participant_derived_data_allowed": False,
            "direct_or_quasi_identifier_data_allowed": False,
            "custodian_plaintext_disposition_required": True,
            "provider_proxy_backup_attestations_required": True,
        }
        or any(protocol.get("claim_defaults", {}).values())
    ):
        raise PrivateCustodianError(
            "private_protocol_invalid", "Private custodian protocol boundary is invalid."
        )
    cryptography_version = importlib.metadata.version("cryptography")
    jsonschema_version = importlib.metadata.version("jsonschema")
    if cryptography_version != "50.0.0" or jsonschema_version != "4.26.0":
        raise PrivateCustodianError(
            "private_tooling_dependency_drift", "Custodian verifier dependency versions drifted."
        )
    status = readiness_status(project_root)
    return {
        "status": "valid",
        "protocol_id": protocol["protocol_id"],
        "schema_count": len(schemas),
        "protocol_sha256": protocol_hash,
        "schema_bundle_sha256": schema_hash,
        "cryptography_version": cryptography_version,
        "jsonschema_version": jsonschema_version,
        "current_campaign_status": status["campaign_status"],
        "current_private_request_allowed": status["private_request_allowed"],
        "current_private_access_authorized": False,
        "repository_private_content_scan_performed": False,
        "repository_private_content_absence_claimed": False,
        "non_synthetic_release_supported": False,
        "model_quality_claim_allowed": False,
        "network_calls": 0,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Eval v2 private-holdout custodian kit")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "verify-kit"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--project-root", type=Path, default=Path("."))
    root_parser = subparsers.add_parser("check-private-root")
    root_parser.add_argument("--project-root", type=Path, default=Path("."))
    root_parser.add_argument("--private-root", type=Path, required=True)
    reserve_parser = subparsers.add_parser("reserve-access")
    reserve_parser.add_argument("--project-root", type=Path, default=Path("."))
    reserve_parser.add_argument("--registry-dir", type=Path, required=True)
    reserve_parser.add_argument("--entry-file", type=Path, required=True)
    reserve_parser.add_argument("--trust-manifest", type=Path, required=True)
    reserve_parser.add_argument("--expected-trust-manifest-sha256", required=True)
    reserve_parser.add_argument("--ledger-id", required=True)
    reserve_parser.add_argument("--expected-base-sequence", type=int, required=True)
    reserve_parser.add_argument("--expected-base-head-sha256", required=True)
    reserve_parser.add_argument("--confirm-synthetic-consumption", action="store_true")
    release_parser = subparsers.add_parser("verify-release")
    release_parser.add_argument("--project-root", type=Path, default=Path("."))
    release_parser.add_argument("--release-dir", type=Path, required=True)
    release_parser.add_argument("--expected-trust-manifest-sha256", required=True)
    release_parser.add_argument("--expected-freeze-request-sha256", required=True)
    release_parser.add_argument("--expected-candidate-commitment-sha256", required=True)
    release_parser.add_argument("--expected-ledger-base-sequence", type=int, required=True)
    release_parser.add_argument("--expected-ledger-base-head-sha256", required=True)
    release_parser.add_argument("--expected-access-reservation-entry-sha256", required=True)
    release_parser.add_argument("--expected-ledger-final-sequence", type=int, required=True)
    release_parser.add_argument("--expected-ledger-final-head-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        project_root = args.project_root.resolve(strict=True)
        if args.command == "status":
            result = readiness_status(project_root)
        elif args.command == "verify-kit":
            result = verify_kit(project_root)
        elif args.command == "check-private-root":
            result = assert_private_root_outside_repository(args.private_root, project_root)
        elif args.command == "reserve-access":
            if not args.confirm_synthetic_consumption:
                raise PrivateCustodianError(
                    "private_reservation_confirmation_required",
                    "Synthetic reservation requires explicit confirmation.",
                )
            trust = load_json_object(args.trust_manifest, "trust manifest")
            schema = _load_schemas(
                project_root / "evals" / "v2" / "private_holdout_kit"
            )["trust_manifest"]
            _validate_schema(trust, schema, "trust manifest")
            observed_trust = trust_manifest_sha256(trust)
            if (
                observed_trust != trust.get("manifest_sha256")
                or observed_trust != args.expected_trust_manifest_sha256
            ):
                raise PrivateCustodianError(
                    "private_trust_anchor_mismatch", "Trust manifest anchor mismatch."
                )
            entry = load_json_object(args.entry_file, "access reservation")
            assert_public_release_projection(entry)
            result = reserve_access_atomically(
                project_root=project_root,
                registry_dir=args.registry_dir,
                ledger_id=args.ledger_id,
                entry=entry,
                trust_keys=_trust_keys(trust),
                expected_base_sequence=args.expected_base_sequence,
                expected_base_head_sha256=args.expected_base_head_sha256,
            )
        else:
            result = verify_release(
                project_root=project_root,
                release_dir=args.release_dir,
                expected_trust_manifest_sha256=args.expected_trust_manifest_sha256,
                expected_freeze_request_sha256=args.expected_freeze_request_sha256,
                expected_candidate_commitment_sha256=args.expected_candidate_commitment_sha256,
                expected_ledger_base_sequence=args.expected_ledger_base_sequence,
                expected_ledger_base_head_sha256=args.expected_ledger_base_head_sha256,
                expected_access_reservation_entry_sha256=(
                    args.expected_access_reservation_entry_sha256
                ),
                expected_ledger_final_sequence=args.expected_ledger_final_sequence,
                expected_ledger_final_head_sha256=args.expected_ledger_final_head_sha256,
            )
    except PrivateCustodianError as exc:
        print(json.dumps({"status": "error", "error_code": exc.code}, indent=2))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
