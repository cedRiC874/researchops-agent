"""Historical v11/v12 identity from raw Git; no current-tree fallback."""
from dataclasses import dataclass
from pathlib import Path

from . import execution_components_v4 as source
from .errors import ExternalClosurePrimitiveError
from .git_objects_v2 import read_git_object_snapshot


@dataclass(frozen=True,slots=True)
class HistoricalTimedProfileComponents:
    commit: str
    tree: str
    components: source.VerifiedTimedProfileComponents
    runtime_authority_granted: bool=False
    execution_observed: bool=False


def verify_historical_timed_profile(project_root,*,commit,tree,profile):
    if not isinstance(project_root,Path) or type(profile) is not str or profile not in source.PROFILE_PATHS:
        raise ExternalClosurePrimitiveError('execution_v4_profile_invalid')
    manifest_path,plan_path=source.PROFILE_PATHS[profile]
    metadata=read_git_object_snapshot(project_root,commit,(source.RECIPE_PATH,manifest_path,plan_path),expected_tree_oid=tree)
    values={blob.path:blob.payload for blob in metadata.blobs}
    recipe=source.decode_recipe(values[source.RECIPE_PATH])
    predecessor_paths=tuple(spec['path'] for spec in recipe['predecessor_recipes'])
    parents=read_git_object_snapshot(project_root,commit,predecessor_paths,expected_tree_oid=tree)
    if parents.entries!=metadata.entries: raise ExternalClosurePrimitiveError('execution_v4_tree_changed')
    predecessors={blob.path:blob.payload for blob in parents.blobs}
    paths=tuple(entry.path for entry in metadata.entries if entry.mode in {'100644','100755'})
    selected=source.select_profile_paths(paths,values[source.RECIPE_PATH],predecessor_recipe_bytes=predecessors,profile=profile)
    snapshot=read_git_object_snapshot(project_root,commit,selected,expected_tree_oid=tree)
    if snapshot.entries!=metadata.entries: raise ExternalClosurePrimitiveError('execution_v4_tree_changed')
    components=source.verify_profile_documents({blob.path:blob.payload for blob in snapshot.blobs},available_paths=paths,
        profile=profile,manifest=values[manifest_path],plan=values[plan_path])
    return HistoricalTimedProfileComponents(snapshot.commit_oid,snapshot.tree_oid,components)
