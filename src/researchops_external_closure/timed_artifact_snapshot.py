"""Scanned exact-prefix snapshots, not container semantics or closure proof."""
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from collections.abc import Mapping

from . import timed_contract as wire
from .errors import ExternalClosurePrimitiveError
from .io import _link_like, _same_identity, read_regular_file_no_follow, scan_public_artifact_bytes


def _fail(code):
    raise ExternalClosurePrimitiveError('timed_artifact_' + code) from None


@dataclass(frozen=True, slots=True)
class TimedArtifactSnapshot:
    publication_disposition: str
    public_scan_status: str
    timed_last_completed_write_stage: str
    _payloads: Mapping[str, bytes] = field(repr=False)
    artifact_count: int | None
    total_byte_count: int | None
    complete_write_attested: bool
    quarantine_error: str | None
    bundle_semantics_verified: bool = field(default=False, init=False)
    runtime_authority_granted: bool = field(default=False, init=False)
    closure_claim_allowed: bool = field(default=False, init=False)

    def file_commitments(self):
        # Only normal snapshots contain bytes; quarantine emits no file hashes.
        return {name: {'bytes': len(payload), 'sha256': wire._sha(payload)}
                for name, payload in self._payloads.items()}


def _read_prefix(directory, order, completed, limits):
    if not isinstance(directory, Path):
        _fail('directory_invalid')
    try:
        before = directory.lstat()
        if _link_like(directory, before) or not stat.S_ISDIR(before.st_mode):
            _fail('directory_identity_invalid')
        names = set()
        wanted = set(order)
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name not in wanted or entry.name in names:
                    _fail('prefix_invalid')
                names.add(entry.name)
        if len(names) not in {completed, min(completed + 1, len(order))} or names != set(order[:len(names)]):
            _fail('prefix_invalid')
        payloads = {}
        total = 0
        for index, name in enumerate(order[:len(names)]):
            payload = read_regular_file_no_follow(directory / name, max_bytes=limits['artifact_file_bytes'])
            if index < completed and not payload:
                _fail('completed_file_empty')
            total += len(payload)
            if total > limits['artifact_total_bytes']:
                _fail('byte_limit')
            payloads[name] = payload
        after = directory.lstat()
        if _link_like(directory, after) or not _same_identity(before, after):
            _fail('directory_changed')
        return payloads
    except ExternalClosurePrimitiveError:
        raise
    except (OSError, ValueError):
        _fail('read_failed')


def inspect_timed_artifact_snapshot(project_root, directory, *, postrun_attested_facts, sensitive_canaries=()):
    """Validate raw v2 facts before any artifact path operation.

    Invalid prefixes or contradictory completed-file attestations reject rather
    than manufacturing zero hashes or inventing corrected operational facts.
    Complete file presence does not verify a manifest, SQLite database or timing.
    """
    facts = wire.validate_document('facts', postrun_attested_facts, root=project_root)
    profile, _ = wire.load_contract(project_root)
    stage = facts['timed_last_completed_write_stage']
    completed = profile['timed_write_stages'].index(stage)

    def quarantined(disposition, scan_status, error):
        return TimedArtifactSnapshot(disposition, scan_status, stage, MappingProxyType({}),
            None, None, False, error)

    private, public = facts['custodian_private_leak_canary_scan_status'], facts['public_generic_privacy_scan_status']
    if private == 'leak_detected' or public == 'sensitive_detected':
        return quarantined('sensitive_quarantined', public, 'closure_sensitive_content_detected')
    if private == 'unavailable' or public == 'unavailable':
        return quarantined('privacy_unverified_quarantined', public, 'closure_sensitive_scan_unavailable')
    if facts['database_origin_status'] != 'fresh_path_confirmed' or facts['database_mutation_status'] != 'normative_only':
        return quarantined('privacy_unverified_quarantined', public, 'closure_database_origin_untrusted')
    order = tuple(profile['artifact_write_order'])
    payloads = _read_prefix(directory, order, completed, profile['limits'])
    try:
        count = scan_public_artifact_bytes(tuple(payloads.values()), sensitive_canaries=tuple(sensitive_canaries))
    except ExternalClosurePrimitiveError as error:
        if error.code == 'external_closure_sensitive_content_detected':
            return quarantined('sensitive_quarantined', 'sensitive_detected', 'closure_sensitive_content_detected')
        raise
    if count != len(payloads):
        _fail('scan_incomplete')
    if _read_prefix(directory, order, completed, profile['limits']) != payloads:
        _fail('snapshot_changed')
    return TimedArtifactSnapshot('normal', 'passed', stage, MappingProxyType(payloads),
        len(payloads), sum(map(len, payloads.values())), completed == len(order), None)
