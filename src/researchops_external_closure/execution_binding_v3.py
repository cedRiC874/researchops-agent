"""Verify timed source profiles from fixed raw Git objects, never execute them."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import execution_components as base
from . import execution_components_v2 as v2
from . import execution_components_v3 as v3
from .errors import ExternalClosurePrimitiveError
from .git_objects import read_git_object_snapshot


@dataclass(frozen=True, slots=True)
class HistoricalTimedProfileComponents:
    commit: str
    tree: str
    components: v3.VerifiedTimedProfileComponents
    runtime_authority_granted: bool = False
    execution_observed: bool = False


def verify_historical_timed_profile(project_root, *, commit, tree, profile):
    if not isinstance(project_root, Path) or type(profile) is not str or profile not in v3.PROFILE_PATHS:
        raise ExternalClosurePrimitiveError("execution_v3_profile_invalid")
    manifest_path, plan_path = v3.PROFILE_PATHS[profile]
    metadata = read_git_object_snapshot(project_root, commit,
        (v3.RECIPE_PATH, v2.RECIPE_PATH, base.RECIPE_PATH, manifest_path, plan_path), expected_tree_oid=tree)
    values = {blob.path: blob.payload for blob in metadata.blobs}
    paths = tuple(entry.path for entry in metadata.entries if entry.mode in {"100644", "100755"})
    selected = v3.select_profile_paths(paths, values[v3.RECIPE_PATH], values[v2.RECIPE_PATH], values[base.RECIPE_PATH], profile=profile)
    snapshot = read_git_object_snapshot(project_root, commit, selected, expected_tree_oid=tree)
    if snapshot.entries != metadata.entries:
        raise ExternalClosurePrimitiveError("execution_v3_tree_changed")
    components = v3.verify_profile_documents({blob.path: blob.payload for blob in snapshot.blobs}, available_paths=paths,
        profile=profile, manifest=values[manifest_path], plan=values[plan_path])
    return HistoricalTimedProfileComponents(snapshot.commit_oid, snapshot.tree_oid, components)
