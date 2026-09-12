"""Fixed Windows-environment local replay barrier, not Provider authorization.

No public path override, repair, release or reset API. Production provisioning
is explicit; tests replace only the private Known Folder locator. An intact,
operator-protected store is required; this is not cross-host/rollback attestation.
"""
from __future__ import annotations

import ctypes
import os
import re
import secrets
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from researchops_external_closure.io import read_regular_file_no_follow, scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes, decode_strict_json_object, parse_utc_timestamp
from .contract import digest


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_SHA256 = "10e6a6f50712b9f6b7bdf1bcf628f1745d2bfcf5723fd9f7d230ea682ee90695"
_ENVIRONMENT = re.compile(r"PCEENV-[A-F0-9]{32}")
_SHA = re.compile(r"(?!0{64}$)[0-9a-f]{64}")


class LocalClaimError(ValueError):
    def __init__(self, code, *, claim_may_exist=False):
        self.code, self.claim_may_exist = code, claim_may_exist
        super().__init__(code)


def _fail(code, *, claim_may_exist=False):
    raise LocalClaimError(code, claim_may_exist=claim_may_exist) from None


def _profile():
    raw = read_regular_file_no_follow(ROOT / "evals/provider_completion_local_claim_v1/contract_v1.json", max_bytes=8192)
    if len(raw) != 2855 or digest(raw) != CONTRACT_SHA256:
        _fail("local_claim_contract_invalid")
    return decode_strict_json_object(raw, max_bytes=8192)


def _windows_local_app_data():
    if os.name != "nt":
        _fail("local_claim_environment_unsupported")
    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]
    guid = GUID.from_buffer_copy(uuid.UUID("F1B32785-6FBA-4FCF-9D55-7B8E7F157091").bytes_le)
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    pointer = ctypes.c_void_p()
    try:
        if shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(pointer)) != 0 or not pointer.value:
            _fail("local_claim_known_folder_unavailable")
        return Path(ctypes.wstring_at(pointer))
    finally:
        if pointer.value:
            ole.CoTaskMemFree(pointer)


def _kernel():
    if os.name != "nt":
        _fail("local_claim_environment_unsupported")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                  ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    kernel.GetDriveTypeW.restype = ctypes.c_uint32
    return kernel


def _store_root(profile):
    base = _windows_local_app_data()
    if (type(base) is not type(Path()) or not base.is_absolute() or ".." in base.parts
        or not re.fullmatch(r"[A-Za-z]:", base.drive)):
        _fail("local_claim_location_invalid")
    if _kernel().GetDriveTypeW(base.anchor) != 3:  # DRIVE_FIXED; no network/removable store.
        _fail("local_claim_location_invalid")
    return base.joinpath(*profile["fixed_location"]["suffix"])


def _directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        _fail("local_claim_path_invalid")


@contextmanager
def _locked_chain(path):
    kernel, handles = _kernel(), []
    try:
        for component in (*reversed(path.parents), path):
            # Directory handle; READ/WRITE sharing but deliberately no DELETE
            # sharing, so a component cannot be renamed/deleted during the write.
            # FILE_LIST_DIRECTORY is required for effective sharing checks;
            # metadata-only access does not prevent directory rename.
            handle = kernel.CreateFileW(str(component), 0x1, 0x3, None, 3, 0x02000000 | 0x00200000, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                _fail("local_claim_directory_lock_failed")
            handles.append(handle)
            _directory(component)
        yield
    finally:
        failed = False
        for handle in reversed(handles):
            if not kernel.CloseHandle(handle):
                failed = True
        if failed:
            _fail("local_claim_handle_close_unknown", claim_may_exist=True)


def _location_hash(root):
    return digest(os.path.normcase(str(root)).encode("utf-8"))


def _write_once(path, raw):
    descriptor = None
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        created = True
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _fail("local_claim_file_invalid", claim_may_exist=True)
        position = 0
        while position < len(raw):
            count = os.write(descriptor, raw[position:])
            if type(count) is not int or not 0 < count <= len(raw) - position:
                _fail("local_claim_write_unknown", claim_may_exist=True)
            position += count
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if (not stat.S_ISREG(after.st_mode) or after.st_nlink != 1 or after.st_size != len(raw)
            or (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino)):
            _fail("local_claim_file_invalid", claim_may_exist=True)
    except FileExistsError:
        _fail("local_claim_already_exists", claim_may_exist=True)
    except LocalClaimError:
        raise
    except Exception:
        _fail("local_claim_write_unknown", claim_may_exist=created)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                _fail("local_claim_close_unknown", claim_may_exist=True)
    # No unlink/rollback even for an empty or partially written file.


def _authority(root, profile):
    _directory(root / profile["claim_subdirectory"])
    raw = read_regular_file_no_follow(root / profile["authority_file"], max_bytes=profile["max_document_bytes"])
    value = decode_strict_json_object(raw, max_bytes=profile["max_document_bytes"])
    if (canonical_json_bytes(value) != raw or set(value) != set(profile["authority_fields"])
        or value["schema_version"] != "provider-completion-local-environment/1.0"
        or type(value["execution_environment_id"]) is not str or not _ENVIRONMENT.fullmatch(value["execution_environment_id"])
        or value["contract_sha256"] != CONTRACT_SHA256 or value["location_sha256"] != _location_hash(root)):
        _fail("local_claim_authority_invalid")
    return value


def provision_local_claim_store():
    """Explicit create-once provisioning. Never run implicitly during claim."""
    try:
        profile = _profile()
        root = _store_root(profile)
        with _locked_chain(root.parent.parent):
            try:
                root.parent.mkdir()
            except FileExistsError:
                pass
            with _locked_chain(root.parent):
                root.mkdir(exist_ok=False)
                with _locked_chain(root):
                    (root / profile["claim_subdirectory"]).mkdir(exist_ok=False)
                    value = dict(schema_version="provider-completion-local-environment/1.0",
                        execution_environment_id="PCEENV-" + secrets.token_hex(16).upper(),
                        location_sha256=_location_hash(root), contract_sha256=CONTRACT_SHA256)
                    _write_once(root / profile["authority_file"], canonical_json_bytes(value))
        return {"status": "provisioned", "execution_environment_id": value["execution_environment_id"],
                "runtime_authority_granted": False, "provider_actions_authorized": False}
    except LocalClaimError:
        raise
    except FileExistsError:
        _fail("local_claim_store_already_exists")
    except Exception:
        _fail("local_claim_provisioning_failed")


def local_claim_store_status():
    """No creation, repair, claim enumeration or path disclosure."""
    try:
        profile = _profile()
        root = _store_root(profile)
        if not root.exists():
            return {"status": "not_provisioned", "execution_environment_id": None, "runtime_authority_granted": False}
        with _locked_chain(root / profile["claim_subdirectory"]):
            value = _authority(root, profile)
        return {"status": "ready", "execution_environment_id": value["execution_environment_id"], "runtime_authority_granted": False}
    except LocalClaimError:
        raise
    except Exception:
        _fail("local_claim_store_invalid")


def _request(request_bytes, profile):
    request = decode_strict_json_object(request_bytes, max_bytes=profile["max_document_bytes"])
    scan_public_artifact_bytes((request_bytes, canonical_json_bytes(request)))
    if set(request) != set(profile["request_fields"]) or request["schema_version"] != "provider-completion-local-claim-request/1.0":
        _fail("local_claim_request_invalid")
    for name in ("authorization_id_sha256", "authorization_grant_sha256", "consumption_entry_sha256", "timing_plan_commitment_sha256"):
        if type(request[name]) is not str or not _SHA.fullmatch(request[name]):
            _fail("local_claim_request_invalid")
    for name, pattern in (("execution_environment_id", r"PCEENV-[A-F0-9]{32}"),
                          ("execution_commit", r"(?!0{40}$)[0-9a-f]{40}"), ("clock_domain_id", r"PCECLOCK-[A-F0-9]{32}")):
        if type(request[name]) is not str or re.fullmatch(pattern, request[name]) is None:
            _fail("local_claim_request_invalid")
    return request


def read_reserved_local_claim(request_bytes, *, expected_receipt_sha256):
    """Read a matching fixed-store snapshot; never acquire/recover a permit.

    This can be used in readonly evidence verification. A readable entry after
    an uncertain write does not retroactively prove successful fsync/close, and
    must never authorize retry, resume or continuing an abandoned execution.
    """
    try:
        profile = _profile()
        request = _request(request_bytes, profile)
        if type(expected_receipt_sha256) is not str or not _SHA.fullmatch(expected_receipt_sha256):
            _fail("local_claim_receipt_expectation_invalid")
        root = _store_root(profile)
        with _locked_chain(root / profile["claim_subdirectory"]):
            authority = _authority(root, profile)
            if authority["execution_environment_id"] != request["execution_environment_id"]:
                _fail("local_claim_environment_mismatch")
            path = root / profile["claim_subdirectory"] / (request["authorization_id_sha256"] + ".json")
            payload = read_regular_file_no_follow(path, max_bytes=profile["max_document_bytes"])
            if digest(payload) != expected_receipt_sha256:
                _fail("local_claim_receipt_hash_mismatch")
            receipt = decode_strict_json_object(payload, max_bytes=profile["max_document_bytes"])
            scan_public_artifact_bytes((payload, canonical_json_bytes(receipt)))
            expected = dict(request, schema_version="provider-completion-local-claim/1.0", status="reserved",
                claimed_at_utc=receipt.get("claimed_at_utc"), claim_contract_sha256=CONTRACT_SHA256,
                store_scope="single_host_fixed_store", provider_actions_authorized=False)
            if canonical_json_bytes(expected) != payload:
                _fail("local_claim_receipt_binding_mismatch")
            stamp = receipt["claimed_at_utc"]
            if type(stamp) is not str or len(stamp) > 27:
                _fail("local_claim_receipt_time_invalid")
            parse_utc_timestamp(stamp)
            # The directory handles prevent parent retargeting, not writes to
            # existing files. Recheck both bounded snapshots before returning.
            if (_authority(root, profile) != authority
                or read_regular_file_no_follow(path, max_bytes=profile["max_document_bytes"]) != payload):
                _fail("local_claim_store_changed")
        return payload
    except LocalClaimError:
        raise
    except Exception:
        _fail("local_claim_receipt_unverifiable")


def _reserve_local_claim_receipt(request_bytes):
    """Return metadata only after write, fsync and all handle closes succeeded."""
    created = False
    try:
        profile = _profile()
        request = _request(request_bytes, profile)
        root = _store_root(profile)
        with _locked_chain(root / profile["claim_subdirectory"]):
            authority = _authority(root, profile)
            if request["execution_environment_id"] != authority["execution_environment_id"]:
                _fail("local_claim_environment_mismatch")
            receipt = dict(request, schema_version="provider-completion-local-claim/1.0", status="reserved",
                claimed_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                claim_contract_sha256=CONTRACT_SHA256, store_scope="single_host_fixed_store", provider_actions_authorized=False)
            raw = canonical_json_bytes(receipt)
            if len(raw) > profile["max_document_bytes"]:
                _fail("local_claim_request_invalid")
            _write_once(root / profile["claim_subdirectory"] / (request["authorization_id_sha256"] + ".json"), raw)
            created = True
        summary = {"status": "reserved", "execution_environment_id": authority["execution_environment_id"],
                "authorization_id_sha256": request["authorization_id_sha256"], "receipt_sha256": digest(raw),
                "runtime_authority_granted": False, "provider_actions_authorized": False, "cross_host_execution_authorized": False}
        return summary, raw
    except LocalClaimError:
        raise
    except Exception:
        _fail("local_claim_failed", claim_may_exist=created)


def reserve_local_claim(request_bytes):
    """Public metadata receipt only; not a transferable winning-execution handle."""
    summary, _receipt = _reserve_local_claim_receipt(request_bytes)
    return summary


_OWNERSHIP_TOKEN = object()


class _WinningLocalClaim:
    """Non-serializable in-process handle, not standalone Provider authority."""
    __slots__ = ("_receipt", "_pid", "_taken", "_lock")

    def __init__(self, token, receipt):
        if token is not _OWNERSHIP_TOKEN or type(receipt) is not bytes:
            raise TypeError("winning claim requires the successful writer return")
        self._receipt = receipt
        self._pid = os.getpid()
        self._taken = False
        self._lock = threading.Lock()

    def _take(self):
        """Transfer once to a future checked start gate; never restore from disk."""
        with self._lock:
            if os.getpid() != self._pid:
                self._taken = True
                _fail("local_claim_ownership_wrong_process", claim_may_exist=True)
            if self._taken:
                _fail("local_claim_ownership_already_taken", claim_may_exist=True)
            self._taken = True
            return self._receipt

    def _invalidate(self):
        with self._lock:
            self._taken = True

    def __copy__(self):
        raise TypeError("winning claim cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("winning claim cannot be copied")

    def __reduce_ex__(self, protocol):
        raise TypeError("winning claim cannot be serialized")


def _reserve_with_ownership(request_bytes):
    """Private fresh-writer path. A readable existing receipt cannot enter it."""
    _summary, receipt = _reserve_local_claim_receipt(request_bytes)
    try:
        return _WinningLocalClaim(_OWNERSHIP_TOKEN, receipt)
    except Exception:
        _fail("local_claim_ownership_unavailable", claim_may_exist=True)
