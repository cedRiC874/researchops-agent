"""Raw-Git component verification for the two v2 source profiles; no authority."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import execution_components as base
from .execution_components_v2 import (PROFILE_PATHS, RECIPE_PATH, VerifiedProfileComponents,
                                      select_profile_paths, verify_profile_documents)
from .git_objects import read_git_object_snapshot
from .errors import ExternalClosurePrimitiveError


@dataclass(frozen=True,slots=True)
class HistoricalProfileComponents:
    commit: str
    tree: str
    components: VerifiedProfileComponents
    runtime_authority_granted: bool = False
    execution_observed: bool = False


def verify_historical_profile(project_root: Path,*,commit: str,tree: str,profile: str) -> HistoricalProfileComponents:
    if not isinstance(project_root,Path) or type(profile) is not str or profile not in PROFILE_PATHS:
        raise ExternalClosurePrimitiveError("execution_v2_profile_invalid")
    manifest_path,plan_path=PROFILE_PATHS[profile]
    metadata=read_git_object_snapshot(project_root,commit,(RECIPE_PATH,base.RECIPE_PATH,manifest_path,plan_path),expected_tree_oid=tree)
    raw={blob.path:blob.payload for blob in metadata.blobs}
    paths=tuple(entry.path for entry in metadata.entries if entry.mode in {"100644","100755"})
    selected=select_profile_paths(paths,raw[RECIPE_PATH],raw[base.RECIPE_PATH],profile=profile)
    snapshot=read_git_object_snapshot(project_root,commit,selected,expected_tree_oid=tree)
    if snapshot.entries!=metadata.entries:
        raise ExternalClosurePrimitiveError("execution_v2_tree_changed")
    result=verify_profile_documents({blob.path:blob.payload for blob in snapshot.blobs},available_paths=paths,
            profile=profile,manifest=raw[manifest_path],plan=raw[plan_path])
    return HistoricalProfileComponents(snapshot.commit_oid,snapshot.tree_oid,result)


__all__=["HistoricalProfileComponents","verify_historical_profile"]
