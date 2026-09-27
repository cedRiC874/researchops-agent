"""Bounded local Git object snapshots, not execution or admission proofs.

Only the commit, its complete tree structure, and explicitly selected blobs are
read. Unselected blob existence/content, ancestry, installed dependencies, and
execution facts are deliberately not attested. No checkout, archive, filter,
snapshot code, or repository write is used. The installed Git executable and
local repository/object database are inputs to this read-only boundary.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Sequence

from .errors import ExternalClosurePrimitiveError


_OID = re.compile(r"[0-9a-fA-F]{40}\Z")
_ENV_KEYS = ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR")
_MAX_OBJECT_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_OBJECTS = 1024
_MAX_PATHS = 256
_MAX_TREE_ENTRIES = 16_384
_MAX_PATH_BYTES = 1024
_MAX_DEPTH = 32
_COMMAND_SECONDS = 10.0
_SNAPSHOT_SECONDS = 120.0
_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"{prefix}{number}" for prefix in ("com", "lpt") for number in "123456789¹²³"}
)


@dataclass(frozen=True, slots=True)
class GitBlobSnapshot:
    path: str
    mode: str
    oid: str
    sha256: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class GitTreeSummary:
    path: str
    oid: str


@dataclass(frozen=True, slots=True)
class GitEntrySummary:
    """Verified tree-entry metadata; no unselected blob payload is read."""

    path: str
    mode: str
    oid: str


@dataclass(frozen=True, slots=True)
class GitObjectSnapshot:
    commit_oid: str
    tree_oid: str
    trees: tuple[GitTreeSummary, ...]
    entries: tuple[GitEntrySummary, ...]
    blobs: tuple[GitBlobSnapshot, ...]
    objects_read: int
    total_payload_bytes: int


def _fail(suffix: str) -> NoReturn:
    raise ExternalClosurePrimitiveError("external_closure_git_" + suffix) from None


def _oid(value: object) -> str:
    if type(value) is not str or _OID.fullmatch(value) is None:
        _fail("oid_invalid")
    return value.lower()


def _path(value: object) -> str:
    if type(value) is not str or not value:
        _fail("path_invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail("path_invalid")
    components = value.split("/")
    if len(encoded) > _MAX_PATH_BYTES or len(components) > _MAX_DEPTH:
        _fail("path_limit")
    for component in components:
        if (
            not component
            or component in (".", "..")
            or component.casefold() == ".git"
            or component[-1:] in (".", " ")
            or len(component.encode("utf-8")) > 255
            or unicodedata.normalize("NFC", component) != component
            or component.split(".", 1)[0].rstrip(" ").casefold() in _DEVICE_NAMES
            or any(ord(character) < 32 or ord(character) == 127 for character in component)
            or any(character in '<>:"\\|?*' for character in component)
        ):
            _fail("path_invalid")
    return value


def _environment() -> dict[str, str]:
    # Never copy the ambient environment: no keys, HOME, SSH, credential,
    # GIT_DIR, alternates, config injection, replace, or tracing context leaks.
    environment = {
        name: value
        for name in _ENV_KEYS
        if (value := os.environ.get(name)) is not None
    }
    environment.update(
        {
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            # The empty allowlist also overrides repository-specific protocol
            # settings if an older Git attempts a promisor fetch.
            "GIT_ALLOW_PROTOCOL": "",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    return environment


class _Reader:
    def __init__(self, repository: Path) -> None:
        self.environment = _environment()
        # Resolve only the literal binary name in absolute trusted PATH
        # directories. No cwd fallback, PATHEXT, shell script, or caller seam.
        try:
            executable = None
            for directory in self.environment.get("PATH", os.defpath).split(os.pathsep):
                candidate = Path(directory) / ("git.exe" if os.name == "nt" else "git")
                if candidate.is_absolute() and candidate.is_file() and os.access(candidate, os.X_OK):
                    executable = candidate
                    break
            if executable is None:
                _fail("unavailable")
            self.executable = str(Path(executable).resolve(strict=True))
            self.repository = repository.resolve(strict=True)
            if not self.repository.is_dir():
                _fail("repository_invalid")
        except ExternalClosurePrimitiveError:
            raise
        except (OSError, ValueError, RuntimeError):
            _fail("repository_invalid")
        self.deadline = time.monotonic() + _SNAPSHOT_SECONDS
        self.cache: dict[str, tuple[str, bytes]] = {}
        self.total_bytes = 0
        self.stopped = False

    def _release(self, process, timer, *, stdin_close_attempted=False, stdin_close_failed=False) -> bool:
        """Bounded cleanup; any uncertainty poisons this reader, never retries."""
        deadline = time.monotonic() + _COMMAND_SECONDS
        clean = not stdin_close_failed
        if timer is not None:
            try:
                timer.cancel()
                timer.join(timeout=max(0.001, deadline - time.monotonic()))
                clean = not timer.is_alive() and clean
            except (OSError, RuntimeError):
                clean = False
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except (OSError, ValueError, subprocess.TimeoutExpired):
                clean = False
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        # Do not retry a stdin close that already failed or
                        # returned an uncertain state in the request phase.
                        if stream is not process.stdin or not stdin_close_attempted:
                            stream.close()
                        if not stream.closed:
                            clean = False
                    except (OSError, ValueError):
                        clean = False
        return clean

    def command(self, oid: str, expected_type: str) -> bytes:
        # One exact OID, one process, one bounded header/payload exchange.
        if self.stopped:
            _fail("reader_stopped")
        if _OID.fullmatch(oid) is None or expected_type not in ("commit", "tree", "blob"):
            self.stopped = True
            _fail("command_invalid")
        deadline = min(self.deadline, time.monotonic() + _COMMAND_SECONDS)
        if deadline <= time.monotonic():
            self.stopped = True
            _fail("timeout")
        command = [
            self.executable,
            "--no-replace-objects",
            "-c", "protocol.allow=never",
            "-c", "credential.helper=",
            "-c", "core.askPass=",
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=" + os.devnull,
            "-c", "core.attributesFile=" + os.devnull,
            "cat-file", "--batch",
        ]
        process = timer = None
        expired = threading.Event()
        completed = False
        stdin_close_attempted = stdin_close_failed = False

        def remaining():
            value = deadline - time.monotonic()
            if expired.is_set() or value <= 0:
                _fail("timeout")
            return value

        try:
            process = subprocess.Popen(
                command, shell=False, cwd=self.repository, env=self.environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            # Process creation cannot be interrupted portably; charge its elapsed
            # time and reject before sending if the deadline has already elapsed.
            interval = remaining()

            def expire():
                expired.set()
                try:
                    process.kill()
                except OSError:
                    pass

            timer = threading.Timer(interval, expire)
            timer.daemon = True
            timer.start()
            if process.stdin is None or process.stdout is None:
                _fail("read_failed")
            remaining()
            request = oid.encode("ascii") + b"\n"
            if process.stdin.write(request) != len(request):
                _fail("read_failed")
            stdin_close_attempted = True
            try:
                process.stdin.close()  # EOF: no second OID is accepted.
            except BaseException:
                stdin_close_failed = True
                raise
            header = process.stdout.readline(129)
            remaining()
            if header == oid.encode("ascii") + b" missing\n":
                _fail("object_unavailable")
            match = re.fullmatch(
                rb"([0-9a-f]{40}) (commit|tree|blob|tag) (0|[1-9][0-9]{0,19})\n", header,
            )
            if len(header) > 128 or match is None:
                _fail("batch_header_invalid")
            returned_oid, kind, size_bytes = match.groups()
            if returned_oid.decode("ascii") != oid:
                _fail("object_hash_invalid")
            if kind != expected_type.encode("ascii"):
                _fail("object_type_invalid")
            size = int(size_bytes)
            if size > _MAX_OBJECT_BYTES:
                _fail("object_size_limit")
            if self.total_bytes + size > _MAX_TOTAL_BYTES:
                _fail("total_size_limit")
            # Exact payload, one delimiter, at most one excess byte; never
            # communicate() or an unbounded read even for hostile output.
            framed = process.stdout.read(size + 2)
            remaining()
            if len(framed) > size + 1:
                _fail("output_limit")
            if len(framed) != size + 1:
                _fail("object_size_invalid")
            if framed[-1:] != b"\n":
                _fail("batch_framing_invalid")
            process.wait(timeout=remaining())
            if process.returncode != 0:
                _fail("object_unavailable")
            payload = framed[:-1]
            object_header = kind + b" " + size_bytes + b"\0"
            if hashlib.sha1(object_header + payload).hexdigest() != oid:
                _fail("object_hash_invalid")
            remaining()
            completed = True
            return payload
        except ExternalClosurePrimitiveError:
            self.stopped = True
            raise
        except subprocess.TimeoutExpired:
            self.stopped = True
            _fail("timeout")
        except (OSError, ValueError):
            self.stopped = True
            if expired.is_set() or time.monotonic() >= deadline:
                _fail("timeout")
            _fail("read_failed")
        except BaseException:
            self.stopped = True
            raise
        finally:
            if not self._release(process, timer, stdin_close_attempted=stdin_close_attempted,
                                 stdin_close_failed=stdin_close_failed):
                self.stopped = True
                _fail("cleanup_failed")
            if completed and (expired.is_set() or time.monotonic() >= deadline):
                self.stopped = True
                _fail("timeout")

    def read(self, oid: str, expected_type: str) -> bytes:
        if self.stopped:
            _fail("reader_stopped")
        if time.monotonic() >= self.deadline:
            self.stopped = True
            _fail("timeout")
        if oid in self.cache:
            actual_type, payload = self.cache[oid]
            if actual_type != expected_type:
                self.stopped = True
                _fail("object_type_invalid")
            return payload
        if len(self.cache) >= _MAX_OBJECTS:
            self.stopped = True
            _fail("object_count_limit")
        payload = self.command(oid, expected_type)
        self.cache[oid] = (expected_type, payload)
        self.total_bytes += len(payload)
        return payload

def _tree_entries(payload: bytes) -> tuple[tuple[str, str, str], ...]:
    entries: list[tuple[str, str, str]] = []
    folded: set[str] = set()
    previous_sort_key: bytes | None = None
    offset = 0
    while offset < len(payload):
        space = payload.find(b" ", offset)
        end = payload.find(b"\0", space + 1) if space >= 0 else -1
        if space < 0 or end < 0 or end + 21 > len(payload):
            _fail("tree_invalid")
        mode = payload[offset:space]
        if mode not in (b"40000", b"100644", b"100755"):
            _fail("tree_mode_invalid")
        try:
            name = payload[space + 1:end].decode("utf-8", errors="strict")
        except UnicodeError:
            _fail("path_invalid")
        if "/" in name:
            _fail("path_invalid")
        _path(name)
        if name.casefold() in folded:
            _fail("path_collision")
        folded.add(name.casefold())
        sort_key = payload[space + 1:end] + (b"/" if mode == b"40000" else b"")
        if previous_sort_key is not None and sort_key <= previous_sort_key:
            _fail("tree_order_invalid")
        previous_sort_key = sort_key
        entries.append((mode.decode("ascii"), name, payload[end + 1:end + 21].hex()))
        if len(entries) > _MAX_TREE_ENTRIES:
            _fail("tree_entry_limit")
        offset = end + 21
    return tuple(entries)


def read_git_object_snapshot(
    repository: Path,
    commit_oid: str,
    required_paths: Sequence[str],
    *,
    expected_tree_oid: str | None = None,
) -> GitObjectSnapshot:
    """Verify historical raw objects and return immutable selected blob bytes.

    Exact 40-hex object names (normalized to lowercase) are required. All tree
    paths must be portable UTF-8/NFC names without links, gitlinks, or casefold
    collisions. Every tree is verified; only required blob payloads are opened.
    Upper bounds and the local Git command/environment are not injectable.
    """

    if not isinstance(repository, Path):
        _fail("repository_invalid")
    commit_oid = _oid(commit_oid)
    expected = _oid(expected_tree_oid) if expected_tree_oid is not None else None
    if (
        not isinstance(required_paths, Sequence)
        or isinstance(required_paths, (str, bytes, bytearray))
        or not required_paths
        or len(required_paths) > _MAX_PATHS
    ):
        _fail("paths_invalid")
    paths = tuple(_path(value) for value in required_paths)
    if len({value.casefold() for value in paths}) != len(paths):
        _fail("path_collision")
    wanted = set(paths)
    reader = _Reader(repository)
    commit = reader.read(commit_oid, "commit")
    headers, separator, _ = commit.partition(b"\n\n")
    lines = headers.split(b"\n")
    if (
        not separator
        or not lines
        or re.fullmatch(rb"tree [0-9a-f]{40}", lines[0]) is None
        or any(line.startswith(b"tree ") for line in lines[1:])
    ):
        _fail("commit_invalid")
    tree_oid = lines[0][5:].decode("ascii")
    if expected is not None and tree_oid != expected:
        _fail("tree_mismatch")
    pending = [("", tree_oid)]
    trees: list[GitTreeSummary] = []
    entries: list[GitEntrySummary] = []
    selected: dict[str, GitBlobSnapshot] = {}
    entry_count = 0
    while pending:
        prefix, oid = pending.pop()
        trees.append(GitTreeSummary(path=prefix, oid=oid))
        for mode, name, entry_oid in _tree_entries(reader.read(oid, "tree")):
            entry_count += 1
            if entry_count > _MAX_TREE_ENTRIES:
                _fail("tree_entry_limit")
            path = _path(prefix + "/" + name if prefix else name)
            entries.append(GitEntrySummary(path=path, mode=mode, oid=entry_oid))
            if mode == "40000":
                pending.append((path, entry_oid))
            elif path in wanted:
                payload = reader.read(entry_oid, "blob")
                selected[path] = GitBlobSnapshot(
                    path=path,
                    mode=mode,
                    oid=entry_oid,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    payload=payload,
                )
    if set(selected) != wanted:
        _fail("required_path_missing")
    return GitObjectSnapshot(
        commit_oid=commit_oid,
        tree_oid=tree_oid,
        trees=tuple(sorted(trees, key=lambda value: value.path)),
        entries=tuple(sorted(entries, key=lambda value: value.path)),
        blobs=tuple(selected[path] for path in paths),
        objects_read=len(reader.cache),
        total_payload_bytes=reader.total_bytes,
    )


__all__ = [
    "GitBlobSnapshot",
    "GitEntrySummary",
    "GitObjectSnapshot",
    "GitTreeSummary",
    "read_git_object_snapshot",
]
