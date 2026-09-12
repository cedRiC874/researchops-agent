"""Bounded, read-only filesystem primitives for closure artifacts."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from .errors import ExternalClosurePrimitiveError
from .primitives import canonical_json_bytes, decode_strict_json_object


_REPARSE_POINT = 0x400
_CHUNK_BYTES = 65_536
_PUBLIC_SENSITIVE_PATTERNS = (
    # Binary cell boundaries need not coincide with regex word boundaries.
    # Fail closed even when sensitive text abuts an ASCII byte in another cell.
    re.compile(rb"(?i)(?:proxy-)?authorization\s*:\s*\S"),
    re.compile(rb"(?i)(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{8,}"),
    # Retain the legacy boundary for the ambiguous underscore form: otherwise
    # normal fields such as task_content_persisted are matched as sk_content... .
    re.compile(rb"(?i)\bsk_[A-Za-z0-9_-]{8,}\b"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"(?i)traceback \(most recent call last\)"),
    # The left boundary prevents the ``s:/`` suffix in ``https://`` from
    # being misclassified as an ``S:/`` Windows drive path.
    re.compile(rb"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|\\\\)[^\r\n\t\"']+"),
    re.compile(
        rb"(?<![A-Za-z0-9_/:])/(?:home|Users|root|tmp|var|mnt|etc|run|"
        rb"srv|opt|private|Volumes|media|workspace|workspaces)/"
    ),
    re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)


def _fail(code: str) -> None:
    raise ExternalClosurePrimitiveError(code)


def _link_like(path: Path, metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & _REPARSE_POINT)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_nlink == right.st_nlink
    )


def read_regular_file_no_follow(path: Path, *, max_bytes: int) -> bytes:
    """Read one bounded single-link regular file and detect common swap races."""

    if not isinstance(path, Path) or type(max_bytes) is not int or max_bytes < 1:
        _fail("external_closure_file_argument_invalid")
    try:
        before = path.lstat()
        if (
            _link_like(path, before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > max_bytes
        ):
            _fail("external_closure_file_identity_invalid")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not _same_identity(before, opened):
                _fail("external_closure_file_identity_changed")
            remaining = max_bytes + 1
            chunks: list[bytes] = []
            while remaining > 0:
                chunk = os.read(descriptor, min(_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        finally:
            os.close(descriptor)
        after = path.lstat()
        if (
            not _same_identity(before, after)
            or _link_like(path, after)
            or len(payload) > max_bytes
            or len(payload) != before.st_size
        ):
            _fail("external_closure_file_identity_changed")
        return payload
    except ExternalClosurePrimitiveError:
        raise
    except (OSError, ValueError):
        _fail("external_closure_file_read_failed")


def read_exact_artifact_directory(
    directory: Path,
    *,
    expected_names: Sequence[str],
    max_file_bytes: int,
    max_total_bytes: int,
) -> Mapping[str, bytes]:
    """Read an exact flat file set without following directory entries."""

    if (
        not isinstance(directory, Path)
        or not isinstance(expected_names, Sequence)
        or isinstance(expected_names, (str, bytes, bytearray))
        or not expected_names
        or any(
            type(name) is not str
            or not name
            or Path(name).name != name
            or "/" in name
            or "\\" in name
            for name in expected_names
        )
        or len(set(expected_names)) != len(expected_names)
        or type(max_file_bytes) is not int
        or type(max_total_bytes) is not int
        or max_file_bytes < 1
        or max_total_bytes < 1
    ):
        _fail("external_closure_directory_argument_invalid")
    try:
        metadata = directory.lstat()
        if _link_like(directory, metadata) or not stat.S_ISDIR(metadata.st_mode):
            _fail("external_closure_directory_identity_invalid")
        # Path.iterdir()/listdir can materialize the whole directory before
        # its names are checked. Bound enumeration itself, not just payloads:
        # at most the expected set plus one rejecting entry is ever observed.
        wanted = frozenset(expected_names)
        names: set[str] = set()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name not in wanted or entry.name in names:
                    _fail("external_closure_directory_file_set_invalid")
                names.add(entry.name)
        if names != wanted:
            _fail("external_closure_directory_file_set_invalid")
        result: dict[str, bytes] = {}
        total = 0
        for name in expected_names:
            payload = read_regular_file_no_follow(
                directory / name,
                max_bytes=max_file_bytes,
            )
            total += len(payload)
            if total > max_total_bytes:
                _fail("external_closure_directory_too_large")
            result[name] = payload
        after = directory.lstat()
        if not _same_identity(metadata, after) or _link_like(directory, after):
            _fail("external_closure_directory_identity_changed")
        return MappingProxyType(result)
    except ExternalClosurePrimitiveError:
        raise
    except OSError:
        _fail("external_closure_directory_read_failed")


def decode_canonical_json_file(payload: bytes, *, max_bytes: int) -> dict[str, object]:
    """Decode artifact JSON that must be canonical UTF-8 plus exactly one LF."""

    if type(payload) is not bytes or not payload.endswith(b"\n") or payload.endswith(b"\n\n"):
        _fail("external_closure_artifact_json_bytes_invalid")
    document = decode_strict_json_object(payload[:-1], max_bytes=max_bytes)
    if canonical_json_bytes(document) + b"\n" != payload:
        _fail("external_closure_artifact_json_not_canonical")
    return document


def scan_public_artifact_bytes(
    payloads: Sequence[bytes],
    *,
    sensitive_canaries: Sequence[bytes] = (),
) -> int:
    """Scan bounded artifact bytes without returning content or match locations."""

    if (
        not isinstance(payloads, Sequence)
        or isinstance(payloads, (str, bytes, bytearray))
        or any(type(payload) is not bytes for payload in payloads)
        or not isinstance(sensitive_canaries, Sequence)
        or isinstance(sensitive_canaries, (str, bytes, bytearray))
        or any(type(value) is not bytes or not value for value in sensitive_canaries)
    ):
        _fail("external_closure_privacy_scan_argument_invalid")
    scanned = 0
    for payload in payloads:
        scanned += 1
        candidates = (payload, payload.replace(b"\x00", b""))
        for candidate in candidates:
            if any(pattern.search(candidate) is not None for pattern in _PUBLIC_SENSITIVE_PATTERNS):
                _fail("external_closure_sensitive_content_detected")
            if any(canary in candidate for canary in sensitive_canaries):
                _fail("external_closure_sensitive_content_detected")
    return scanned


__all__ = [
    "decode_canonical_json_file",
    "read_exact_artifact_directory",
    "read_regular_file_no_follow",
    "scan_public_artifact_bytes",
]
