"""Bind current timed source bytes to expected fixed Git identity; no permit."""
from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass

from .git_objects import _Reader
from .execution_binding_v3 import verify_historical_timed_profile
from .execution_current_v3 import _root, verify_current_timed_profile
from .errors import ExternalClosurePrimitiveError


def _fail(code):
    raise ExternalClosurePrimitiveError("execution_local_v3_" + code) from None


def _head(root):
    # Reuse the credential-free environment and absolute binary resolution, not
    # the object reader's command method. Its cat-file-only whitelist is intact.
    reader = _Reader(root)
    command = [reader.executable, "--no-replace-objects", "-c", "protocol.allow=never",
        "-c", "credential.helper=", "-c", "core.askPass=", "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=" + os.devnull, "-c", "core.attributesFile=" + os.devnull,
        "rev-parse", "--verify", "HEAD"]
    try:
        with subprocess.Popen(command, cwd=reader.repository, env=reader.environment,
                shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
            expired = threading.Event()
            def expire():
                expired.set()
                try:
                    process.kill()
                except OSError:
                    pass
            timer = threading.Timer(10, expire); timer.daemon = True; timer.start()
            try:
                if process.stdout is None:
                    _fail("head_unavailable")
                payload = process.stdout.read(42)
                if len(payload) > 41:
                    process.kill()
                process.wait(timeout=10)
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill(); process.wait(timeout=10)
            if expired.is_set():
                _fail("head_timeout")
            if process.returncode != 0 or re.fullmatch(rb"[0-9a-f]{40}\n", payload) is None:
                _fail("head_unavailable")
            return payload[:-1].decode("ascii")
    except ExternalClosurePrimitiveError:
        raise
    except (OSError, ValueError, subprocess.TimeoutExpired):
        _fail("head_unavailable")


@dataclass(frozen=True, slots=True)
class LocalTimedExecutionIdentity:
    profile: str
    execution_commit: str
    execution_tree: str
    source_integrity_commitment_sha256: str
    source_manifest_commitment_sha256: str
    current_and_historical_source_matched: bool = True
    head_matched_at_both_observations: bool = True
    repository_globally_clean_verified: bool = False
    installed_environment_verified: bool = False
    fresh_authorization_verified: bool = False
    winning_claim_verified: bool = False
    runtime_authority_granted: bool = False
    closure_claim_allowed: bool = False


def verify_local_timed_execution_identity(project_root, *, profile, expected_commit, expected_tree,
        expected_source_integrity_commitment_sha256, expected_source_manifest_commitment_sha256):
    """Read-only checkpoint proof, not a lock against future source/HEAD changes."""
    if type(profile) is not str or profile not in {"first_live", "campaign"}:
        _fail("expectation_invalid")
    for value, length in ((expected_commit, 40), (expected_tree, 40),
                          (expected_source_integrity_commitment_sha256, 64), (expected_source_manifest_commitment_sha256, 64)):
        if type(value) is not str or re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value) is None or value == "0" * length:
            _fail("expectation_invalid")
    root = _root(project_root)
    if _head(root) != expected_commit:
        _fail("head_mismatch")
    historical = verify_historical_timed_profile(root, commit=expected_commit, tree=expected_tree, profile=profile)
    current = verify_current_timed_profile(root, profile=profile)
    for checked in (historical.components, current):
        if (checked.profile != profile or checked.source_integrity_commitment_sha256 != expected_source_integrity_commitment_sha256
            or checked.implementation_commitment_sha256 != expected_source_manifest_commitment_sha256):
            _fail("commitment_mismatch")
    if historical.components.component_hashes != current.component_hashes:
        _fail("component_mismatch")
    if _head(root) != expected_commit:
        _fail("head_changed")
    return LocalTimedExecutionIdentity(profile, expected_commit, expected_tree,
        expected_source_integrity_commitment_sha256, expected_source_manifest_commitment_sha256)
