"""Pure byte-protocol primitives for external completion-telemetry closure.

This module accepts only caller-supplied in-memory values.  It does not read files,
the environment, a clock, a Provider, or the network, and it never writes data.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, NoReturn

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .errors import ExternalClosurePrimitiveError
from .types import UtcTimestamp, _compare_normalized_fraction_digits


SIGNATURE_DOMAIN = (
    "researchops-provider-completion-external-preregistration-signature-v1"
)
KEY_ID_DOMAIN = "researchops-provider-completion-external-key-id-v1"
LEDGER_ENTRY_DOMAIN = "researchops-provider-completion-external-ledger-entry-v1"
LEDGER_HEAD_DOMAIN = "researchops-provider-completion-external-ledger-head-v1"

_DOCUMENT_HASH_DOMAINS = {
    "completion_telemetry_external_trust_manifest": (
        "researchops-provider-completion-external-trust-manifest-v1"
    ),
    "completion_telemetry_candidate_freeze": (
        "researchops-provider-completion-candidate-freeze-receipt-v1"
    ),
    "completion_telemetry_seen_task_exclusion_manifest": (
        "researchops-provider-completion-seen-case-exclusion-manifest-v1"
    ),
    "completion_telemetry_closure_preregistration": (
        "researchops-provider-completion-external-preregistration-envelope-v1"
    ),
    "completion_telemetry_closure_authorization_grant": (
        "researchops-provider-completion-external-authorization-grant-v1"
    ),
    "completion_telemetry_closure_consumption_receipt": (
        "researchops-provider-completion-external-consumption-receipt-v1"
    ),
    "completion_telemetry_closure_receipt": (
        "researchops-provider-completion-closure-receipt-v1"
    ),
}
_SIGNED_DOCUMENT_TYPES = frozenset(_DOCUMENT_HASH_DOMAINS) | {
    "completion_telemetry_external_ledger_entry"
}
_SIGNATURE_ROLES = frozenset(
    {"freeze_authority", "task_custodian", "ledger_witness"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_KEY_ID_RE = re.compile(r"^PCEKEY-[A-F0-9]{32}$", re.ASCII)
_LEDGER_ID_RE = re.compile(r"^PCELEDGER-[A-F0-9]{32}$", re.ASCII)
_UTC_RE = re.compile(
    r"^(?P<whole>[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.(?P<fraction>[0-9]+))?Z$",
    re.ASCII,
)
_MAX_JSON_SAFE_INTEGER = 9_007_199_254_740_991


def _fail(code: str) -> NoReturn:
    raise ExternalClosurePrimitiveError(code)


def _validate_json_string(value: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _fail("external_closure_json_unicode_invalid")


def _validate_json_value(value: object, *, seen: set[int] | None = None) -> None:
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is str:
        _validate_json_string(value)
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail("external_closure_json_nonfinite")
        return
    if type(value) is list:
        if seen is None:
            seen = set()
        identity = id(value)
        if identity in seen:
            _fail("external_closure_json_cycle")
        seen.add(identity)
        try:
            for item in value:
                _validate_json_value(item, seen=seen)
        finally:
            seen.remove(identity)
        return
    if type(value) is dict:
        if seen is None:
            seen = set()
        identity = id(value)
        if identity in seen:
            _fail("external_closure_json_cycle")
        seen.add(identity)
        try:
            for key, item in value.items():
                if type(key) is not str:
                    _fail("external_closure_json_key_invalid")
                _validate_json_string(key)
                _validate_json_value(item, seen=seen)
        finally:
            seen.remove(identity)
        return
    _fail("external_closure_json_type_invalid")


def decode_strict_json_object(payload: bytes, *, max_bytes: int) -> dict[str, Any]:
    """Decode one bounded UTF-8 JSON object, rejecting ambiguous representations."""

    if type(payload) is not bytes:
        _fail("external_closure_json_bytes_required")
    if type(max_bytes) is not int or max_bytes < 1:
        _fail("external_closure_json_max_bytes_invalid")
    if len(payload) > max_bytes:
        _fail("external_closure_json_too_large")
    if payload.startswith(b"\xef\xbb\xbf"):
        _fail("external_closure_json_bom_forbidden")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("external_closure_json_utf8_invalid")

    def object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                _fail("external_closure_json_duplicate_key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        _fail("external_closure_json_nonfinite")

    try:
        decoded = json.loads(
            text,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except ExternalClosurePrimitiveError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError):
        _fail("external_closure_json_invalid")
    try:
        _validate_json_value(decoded)
    except RecursionError:
        _fail("external_closure_json_nesting_invalid")
    if type(decoded) is not dict:
        _fail("external_closure_json_object_required")
    return decoded


def canonical_json_bytes(value: object) -> bytes:
    """Return the contract's canonical UTF-8 JSON representation."""

    try:
        _validate_json_value(value)
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return rendered.encode("utf-8", errors="strict")
    except ExternalClosurePrimitiveError:
        raise
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError):
        _fail("external_closure_canonical_json_invalid")


def parse_utc_timestamp(value: str) -> UtcTimestamp:
    """Parse a strict RFC3339 UTC timestamp without losing fractional precision."""

    if type(value) is not str:
        _fail("external_closure_timestamp_invalid")
    matched = _UTC_RE.fullmatch(value)
    if matched is None:
        _fail("external_closure_timestamp_invalid")
    try:
        whole_second = datetime.fromisoformat(matched.group("whole") + "+00:00")
    except ValueError:
        _fail("external_closure_timestamp_invalid")
    if (
        whole_second.tzinfo is None
        or whole_second.utcoffset() != timezone.utc.utcoffset(whole_second)
        or whole_second.microsecond != 0
    ):
        _fail("external_closure_timestamp_invalid")
    fraction = (matched.group("fraction") or "").rstrip("0")
    return UtcTimestamp(whole_second.astimezone(timezone.utc), fraction)


def utc_interval_is_positive_and_at_most(
    *, start: UtcTimestamp, end: UtcTimestamp, maximum_seconds: int
) -> bool:
    """Check an exact positive UTC interval without decimal rounding."""

    if type(start) is not UtcTimestamp or type(end) is not UtcTimestamp:
        _fail("external_closure_timestamp_interval_invalid")
    if (
        type(maximum_seconds) is not int
        or maximum_seconds < 0
        or maximum_seconds > _MAX_JSON_SAFE_INTEGER
    ):
        _fail("external_closure_timestamp_interval_invalid")
    if not start < end:
        return False
    whole_delta = end.whole_second_utc - start.whole_second_utc
    whole_seconds = whole_delta.days * 86_400 + whole_delta.seconds
    if whole_seconds < maximum_seconds:
        return True
    if whole_seconds > maximum_seconds:
        return False
    return (
        _compare_normalized_fraction_digits(
            end.fraction_digits, start.fraction_digits
        )
        <= 0
    )


def _require_sha256(value: object, *, nonzero: bool = False) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _fail("external_closure_sha256_invalid")
    if nonzero and value == "0" * 64:
        _fail("external_closure_sha256_zero_forbidden")
    return value


def _ascii(value: str, *, error_code: str) -> bytes:
    try:
        return value.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        _fail(error_code)


def compute_document_sha256(document: dict[str, Any]) -> str:
    """Compute a fixed-domain document hash without trusting document-supplied domains."""

    if type(document) is not dict:
        _fail("external_closure_document_object_required")
    document_type = document.get("document_type")
    if type(document_type) is not str or document_type not in _DOCUMENT_HASH_DOMAINS:
        _fail("external_closure_document_type_unsupported")
    body = dict(document)
    body.pop("document_sha256", None)
    body.pop("signatures", None)
    material = (
        _DOCUMENT_HASH_DOMAINS[document_type].encode("utf-8")
        + b"\x00"
        + _ascii(document_type, error_code="external_closure_document_type_invalid")
        + b"\x00"
        + canonical_json_bytes(body)
    )
    return hashlib.sha256(material).hexdigest()


def verify_document_sha256(document: dict[str, Any]) -> None:
    """Fail closed unless a document's stored hash matches its canonical projection."""

    if type(document) is not dict:
        _fail("external_closure_document_object_required")
    expected = _require_sha256(document.get("document_sha256"), nonzero=True)
    if compute_document_sha256(document) != expected:
        _fail("external_closure_document_hash_mismatch")


def _decode_canonical_base64(value: object, *, length: int, code: str) -> bytes:
    if type(value) is not str:
        _fail(code)
    try:
        encoded = value.encode("ascii", errors="strict")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, ValueError, binascii.Error):
        _fail(code)
    if len(decoded) != length or base64.b64encode(decoded) != encoded:
        _fail(code)
    return decoded


def derive_ed25519_key_id(public_key_b64: str) -> str:
    """Derive the fixed public key identifier from a canonical raw-key encoding."""

    raw_key = _decode_canonical_base64(
        public_key_b64,
        length=32,
        code="external_closure_public_key_invalid",
    )
    digest = hashlib.sha256(KEY_ID_DOMAIN.encode("utf-8") + b"\x00" + raw_key)
    return "PCEKEY-" + digest.hexdigest()[:32].upper()


def build_signature_message(
    *, document_type: str, role: str, key_id: str, signed_sha256: str
) -> bytes:
    """Build the exact role- and key-bound Ed25519 signature message."""

    if type(document_type) is not str or document_type not in _SIGNED_DOCUMENT_TYPES:
        _fail("external_closure_signature_document_type_invalid")
    if type(role) is not str or role not in _SIGNATURE_ROLES:
        _fail("external_closure_signature_role_invalid")
    if type(key_id) is not str or _KEY_ID_RE.fullmatch(key_id) is None:
        _fail("external_closure_key_id_invalid")
    subject = _require_sha256(signed_sha256)
    return b"\x00".join(
        (
            SIGNATURE_DOMAIN.encode("utf-8"),
            _ascii(
                document_type,
                error_code="external_closure_signature_document_type_invalid",
            ),
            _ascii(role, error_code="external_closure_signature_role_invalid"),
            _ascii(key_id, error_code="external_closure_key_id_invalid"),
            bytes.fromhex(subject),
        )
    )


def verify_ed25519_signature(
    *, public_key_b64: str, signature_b64: str, message: bytes
) -> None:
    """Verify one Ed25519 signature with public material only."""

    if type(message) is not bytes:
        _fail("external_closure_signature_message_invalid")
    raw_key = _decode_canonical_base64(
        public_key_b64,
        length=32,
        code="external_closure_public_key_invalid",
    )
    signature = _decode_canonical_base64(
        signature_b64,
        length=64,
        code="external_closure_signature_encoding_invalid",
    )
    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(signature, message)
    except (InvalidSignature, ValueError):
        _fail("external_closure_signature_invalid")


def _require_ledger_core(entry: object) -> tuple[str, int, str]:
    if type(entry) is not dict:
        _fail("external_closure_ledger_entry_object_required")
    if entry.get("document_type") != "completion_telemetry_external_ledger_entry":
        _fail("external_closure_ledger_document_type_invalid")
    ledger_id = entry.get("ledger_id")
    if type(ledger_id) is not str or _LEDGER_ID_RE.fullmatch(ledger_id) is None:
        _fail("external_closure_ledger_id_invalid")
    sequence = entry.get("sequence")
    if (
        type(sequence) is not int
        or sequence < 1
        or sequence > _MAX_JSON_SAFE_INTEGER
    ):
        _fail("external_closure_ledger_sequence_invalid")
    previous_head = _require_sha256(entry.get("previous_head_sha256"))
    return ledger_id, sequence, previous_head


def compute_ledger_entry_sha256(entry: dict[str, Any]) -> str:
    """Compute the fixed-domain hash of one external-ledger entry."""

    ledger_id, sequence, previous_head = _require_ledger_core(entry)
    body = dict(entry)
    body.pop("entry_sha256", None)
    body.pop("resulting_head_sha256", None)
    body.pop("signatures", None)
    material = (
        LEDGER_ENTRY_DOMAIN.encode("utf-8")
        + b"\x00"
        + ledger_id.encode("ascii")
        + b"\x00"
        + sequence.to_bytes(8, "big")
        + bytes.fromhex(previous_head)
        + canonical_json_bytes(body)
    )
    return hashlib.sha256(material).hexdigest()


def compute_ledger_resulting_head(
    previous_head_sha256: str, entry_sha256: str
) -> str:
    """Compute the next external-ledger head from the prior head and entry hash."""

    previous_head = _require_sha256(previous_head_sha256)
    entry_hash = _require_sha256(entry_sha256, nonzero=True)
    material = (
        LEDGER_HEAD_DOMAIN.encode("utf-8")
        + b"\x00"
        + bytes.fromhex(previous_head)
        + bytes.fromhex(entry_hash)
    )
    return hashlib.sha256(material).hexdigest()


def verify_ledger_hashes(entry: dict[str, Any]) -> None:
    """Fail closed unless both stored ledger hashes match the byte protocol."""

    _ledger_id, _sequence, previous_head = _require_ledger_core(entry)
    expected_entry = _require_sha256(entry.get("entry_sha256"), nonzero=True)
    expected_head = _require_sha256(
        entry.get("resulting_head_sha256"), nonzero=True
    )
    actual_entry = compute_ledger_entry_sha256(entry)
    if actual_entry != expected_entry:
        _fail("external_closure_ledger_entry_hash_mismatch")
    if compute_ledger_resulting_head(previous_head, actual_entry) != expected_head:
        _fail("external_closure_ledger_head_hash_mismatch")


__all__ = [
    "KEY_ID_DOMAIN",
    "LEDGER_ENTRY_DOMAIN",
    "LEDGER_HEAD_DOMAIN",
    "SIGNATURE_DOMAIN",
    "build_signature_message",
    "canonical_json_bytes",
    "compute_document_sha256",
    "compute_ledger_entry_sha256",
    "compute_ledger_resulting_head",
    "decode_strict_json_object",
    "derive_ed25519_key_id",
    "parse_utc_timestamp",
    "utc_interval_is_positive_and_at_most",
    "verify_document_sha256",
    "verify_ed25519_signature",
    "verify_ledger_hashes",
]
