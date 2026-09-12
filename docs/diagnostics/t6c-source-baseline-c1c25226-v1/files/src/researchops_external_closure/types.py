"""Immutable byte containers and result values for two-layer closure verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import total_ordering
from typing import Literal, TypeAlias


def _require_exact_bytes(name: str, value: object) -> None:
    if type(value) is not bytes:
        raise TypeError(f"{name}_must_be_bytes")


def _is_nonzero_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and value != "0" * 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_optional_nonzero_sha256(name: str, value: object) -> None:
    if value is not None and not _is_nonzero_sha256(value):
        raise TypeError(f"{name}_must_be_nonzero_sha256_or_none")


def _compare_normalized_fraction_digits(left: str, right: str) -> int:
    """Compare canonical decimal fractions without allocating padded copies."""

    for left_digit, right_digit in zip(left, right):
        if left_digit < right_digit:
            return -1
        if left_digit > right_digit:
            return 1
    if len(left) < len(right):
        return -1
    if len(left) > len(right):
        return 1
    return 0


@total_ordering
@dataclass(frozen=True, slots=True, eq=False)
class UtcTimestamp:
    """An exact UTC instant with an arbitrary-precision decimal second fraction."""

    whole_second_utc: datetime
    fraction_digits: str

    def __post_init__(self) -> None:
        if (
            type(self.whole_second_utc) is not datetime
            or self.whole_second_utc.tzinfo is not timezone.utc
            or self.whole_second_utc.microsecond != 0
        ):
            raise TypeError("whole_second_utc_must_be_exact_utc_second")
        if type(self.fraction_digits) is not str:
            raise TypeError("fraction_digits_must_be_string")
        if self.fraction_digits and (
            not self.fraction_digits.isascii()
            or not self.fraction_digits.isdecimal()
            or self.fraction_digits.endswith("0")
        ):
            raise ValueError("fraction_digits_must_be_normalized_ascii_decimal")

    def __eq__(self, other: object) -> bool:
        if type(other) is not UtcTimestamp:
            return False
        return (
            self.whole_second_utc == other.whole_second_utc
            and self.fraction_digits == other.fraction_digits
        )

    def __lt__(self, other: object) -> bool:
        if type(other) is not UtcTimestamp:
            return NotImplemented
        if self.whole_second_utc != other.whole_second_utc:
            return self.whole_second_utc < other.whole_second_utc
        return (
            _compare_normalized_fraction_digits(
                self.fraction_digits, other.fraction_digits
            )
            < 0
        )

    def __hash__(self) -> int:
        return hash((self.whole_second_utc, self.fraction_digits))

    def isoformat(self) -> str:
        """Return a canonical `Z` spelling without discarding fractional precision."""

        value = self.whole_second_utc
        whole = (
            f"{value.year:04d}-{value.month:02d}-{value.day:02d}T"
            f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}"
        )
        if self.fraction_digits:
            return f"{whole}.{self.fraction_digits}Z"
        return whole + "Z"


@dataclass(frozen=True, slots=True)
class PreReceiptDocumentBytes:
    """The exact pre-receipt documents consumed by verifier layer A."""

    trust_manifest: bytes
    candidate_freeze_receipt: bytes
    candidate_frozen_ledger_entry: bytes
    seen_case_exclusion_manifest: bytes
    preregistration_envelope: bytes
    preregistration_frozen_ledger_entry: bytes
    authorization_grant: bytes
    consumption_receipt: bytes
    authorization_consumed_ledger_entry: bytes

    def __post_init__(self) -> None:
        fields = (
            ("trust_manifest", self.trust_manifest),
            ("candidate_freeze_receipt", self.candidate_freeze_receipt),
            ("candidate_frozen_ledger_entry", self.candidate_frozen_ledger_entry),
            ("seen_case_exclusion_manifest", self.seen_case_exclusion_manifest),
            ("preregistration_envelope", self.preregistration_envelope),
            (
                "preregistration_frozen_ledger_entry",
                self.preregistration_frozen_ledger_entry,
            ),
            ("authorization_grant", self.authorization_grant),
            ("consumption_receipt", self.consumption_receipt),
            (
                "authorization_consumed_ledger_entry",
                self.authorization_consumed_ledger_entry,
            ),
        )
        for name, value in fields:
            _require_exact_bytes(name, value)


@dataclass(frozen=True, slots=True)
class FinalDocumentBytes:
    """The complete in-memory document set consumed by verifier layer B."""

    pre_receipt: PreReceiptDocumentBytes
    closure_receipt: bytes
    closure_evidence_anchored_ledger_entry: bytes

    def __post_init__(self) -> None:
        if type(self.pre_receipt) is not PreReceiptDocumentBytes:
            raise TypeError("pre_receipt_must_be_pre_receipt_document_bytes")
        _require_exact_bytes("closure_receipt", self.closure_receipt)
        _require_exact_bytes(
            "closure_evidence_anchored_ledger_entry",
            self.closure_evidence_anchored_ledger_entry,
        )


@dataclass(frozen=True, slots=True)
class ReceiptProjectionReady:
    """Layer-A success: an unsigned receipt projection is ready for external signing."""

    unsigned_receipt_canonical_json: bytes
    receipt_document_sha256: str
    evidence_valid: bool
    pre_anchor_closure_eligible: bool
    evidence_error_code: str | None
    closure_reasons: tuple[str, ...]
    kind: Literal["receipt_projection_ready"] = field(
        default="receipt_projection_ready", init=False
    )

    def __post_init__(self) -> None:
        _require_exact_bytes(
            "unsigned_receipt_canonical_json", self.unsigned_receipt_canonical_json
        )
        if not _is_nonzero_sha256(self.receipt_document_sha256):
            raise TypeError("receipt_document_sha256_must_be_nonzero_sha256")
        if type(self.evidence_valid) is not bool:
            raise TypeError("evidence_valid_must_be_bool")
        if type(self.pre_anchor_closure_eligible) is not bool:
            raise TypeError("pre_anchor_closure_eligible_must_be_bool")
        if self.evidence_error_code is not None and (
            type(self.evidence_error_code) is not str or not self.evidence_error_code
        ):
            raise TypeError("evidence_error_code_must_be_nonempty_string_or_none")
        if type(self.closure_reasons) is not tuple or any(
            type(reason) is not str for reason in self.closure_reasons
        ):
            raise TypeError("closure_reasons_must_be_tuple_of_strings")
        if self.evidence_valid:
            if self.evidence_error_code is not None:
                raise ValueError("valid_evidence_cannot_have_error_code")
        else:
            if self.evidence_error_code is None:
                raise ValueError("invalid_evidence_requires_error_code")
            if self.pre_anchor_closure_eligible:
                raise ValueError("invalid_evidence_cannot_be_closure_eligible")
            if self.closure_reasons:
                raise ValueError("invalid_evidence_cannot_have_closure_reasons")
        if self.pre_anchor_closure_eligible and self.closure_reasons:
            raise ValueError("eligible_receipt_cannot_have_closure_reasons")
        if (
            self.evidence_valid
            and not self.pre_anchor_closure_eligible
            and not self.closure_reasons
        ):
            raise ValueError("ineligible_valid_receipt_requires_closure_reason")


@dataclass(frozen=True, slots=True)
class PreReceiptRejected:
    """Layer-A fail-closed result."""

    error_code: str
    kind: Literal["unclosed_invalid"] = field(default="unclosed_invalid", init=False)

    def __post_init__(self) -> None:
        if type(self.error_code) is not str or not self.error_code:
            raise TypeError("error_code_must_be_nonempty_string")


PreReceiptResult: TypeAlias = ReceiptProjectionReady | PreReceiptRejected


@dataclass(frozen=True, slots=True)
class FinalClosureResult:
    """Layer-B terminal result; verification itself has no runtime capabilities."""

    status: Literal[
        "closed_evidence_verified", "closed_failure_attested", "unclosed_invalid"
    ]
    evidence_valid: bool
    closure_claim_allowed: bool
    error_code: str | None
    receipt_document_sha256: str | None
    final_ledger_entry_sha256: str | None
    verification_network_calls: Literal[0] = field(default=0, init=False)
    verification_provider_calls: Literal[0] = field(default=0, init=False)
    verification_key_loads: Literal[0] = field(default=0, init=False)

    def __post_init__(self) -> None:
        allowed = {
            "closed_evidence_verified",
            "closed_failure_attested",
            "unclosed_invalid",
        }
        if type(self.status) is not str or self.status not in allowed:
            raise ValueError("final_closure_status_invalid")
        if type(self.evidence_valid) is not bool:
            raise TypeError("evidence_valid_must_be_bool")
        if type(self.closure_claim_allowed) is not bool:
            raise TypeError("closure_claim_allowed_must_be_bool")
        if self.error_code is not None and (
            type(self.error_code) is not str or not self.error_code
        ):
            raise TypeError("error_code_must_be_nonempty_string_or_none")
        _require_optional_nonzero_sha256(
            "receipt_document_sha256", self.receipt_document_sha256
        )
        _require_optional_nonzero_sha256(
            "final_ledger_entry_sha256", self.final_ledger_entry_sha256
        )
        if self.status == "unclosed_invalid":
            if self.evidence_valid or self.closure_claim_allowed:
                raise ValueError("unclosed_invalid_truth_values_invalid")
            if self.error_code is None:
                raise ValueError("unclosed_invalid_requires_error_code")
        else:
            if self.error_code is not None:
                raise ValueError("closed_result_cannot_have_verifier_error")
            if self.receipt_document_sha256 is None:
                raise ValueError("closed_result_requires_receipt_hash")
            if self.final_ledger_entry_sha256 is None:
                raise ValueError("closed_result_requires_final_ledger_hash")
        if self.status == "closed_evidence_verified" and not self.evidence_valid:
            raise ValueError("closed_evidence_verified_requires_valid_evidence")
        if self.status == "closed_failure_attested" and (
            self.evidence_valid or self.closure_claim_allowed
        ):
            raise ValueError("closed_failure_attested_truth_values_invalid")


__all__ = [
    "FinalClosureResult",
    "FinalDocumentBytes",
    "PreReceiptDocumentBytes",
    "PreReceiptRejected",
    "PreReceiptResult",
    "ReceiptProjectionReady",
    "UtcTimestamp",
]
