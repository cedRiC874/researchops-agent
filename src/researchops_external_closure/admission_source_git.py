"""Read-only historical Git coverage for exact first-live/campaign source reuse.

The result verifies byte identity, not execution, review, dependencies installed
at runtime, evidence completeness, registry admission or online authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .admission_contract import FrozenAdmissionLinkContract
from .admission_source import SourceEqualityResult, compare_source_bytes, select_source_paths
from .errors import ExternalClosurePrimitiveError
from .git_objects import read_git_object_snapshot


_OID = re.compile(r"(?!0{40}\Z)[0-9a-f]{40}\Z")


@dataclass(frozen=True, slots=True)
class HistoricalSourceEqualityResult:
    first_live_commit: str
    first_live_tree: str
    campaign_commit: str
    campaign_tree: str
    equality: SourceEqualityResult
    git_completeness_verified: bool = True
    first_live_evidence_verified: bool = False
    registry_admission_verified: bool = False
    runtime_authority_granted: bool = False


def verify_historical_source_equality(
    project_root: Path, contract: FrozenAdmissionLinkContract, *,
    first_live_commit: str, first_live_tree: str, campaign_commit: str, campaign_tree: str,
) -> HistoricalSourceEqualityResult:
    if not isinstance(project_root, Path) or type(contract) is not FrozenAdmissionLinkContract:
        raise ExternalClosurePrimitiveError("admission_source_git_input_invalid")
    if any(type(value) is not str or _OID.fullmatch(value) is None
           for value in (first_live_commit, first_live_tree, campaign_commit, campaign_tree)):
        raise ExternalClosurePrimitiveError("admission_source_git_input_invalid")

    def snapshot(commit: str, tree: str):
        inventory = read_git_object_snapshot(project_root, commit, ("requirements.lock",), expected_tree_oid=tree)
        paths = tuple(entry.path for entry in inventory.entries if entry.mode in {"100644", "100755"})
        selected = select_source_paths(contract, paths)
        blobs = read_git_object_snapshot(project_root, commit, selected, expected_tree_oid=tree)
        if inventory.entries != blobs.entries:
            raise ExternalClosurePrimitiveError("admission_source_git_tree_changed")
        return {blob.path: blob.payload for blob in blobs.blobs}, paths

    first, first_paths = snapshot(first_live_commit, first_live_tree)
    campaign, campaign_paths = snapshot(campaign_commit, campaign_tree)
    equality = compare_source_bytes(contract, first_live_files=first, first_live_tree_paths=first_paths,
                                    campaign_files=campaign, campaign_tree_paths=campaign_paths)
    return HistoricalSourceEqualityResult(first_live_commit, first_live_tree, campaign_commit, campaign_tree, equality)


__all__ = ["HistoricalSourceEqualityResult", "verify_historical_source_equality"]
