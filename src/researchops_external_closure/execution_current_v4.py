"""Bounded current v11/v12 checks with two inventories; no generation or Git."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from . import execution_components as base, execution_components_v4 as source
from .execution_binding import _current_file
from .execution_current_v3 import _root, _directory
from .errors import ExternalClosurePrimitiveError


def _fail(code): raise ExternalClosurePrimitiveError('execution_current_v4_'+code) from None


def _snapshot(root,profile):
    recipe_bytes=_current_file(root,source.RECIPE_PATH); recipe=source.decode_recipe(recipe_bytes)
    predecessors={spec['path']:_current_file(root,spec['path']) for spec in recipe['predecessor_recipes']}
    original,previous,third=source._recipes(recipe,predecessors)
    directories={}; paths=set(); count=0
    def entries(directory):
        nonlocal count
        directories[directory]=_directory(directory)
        with os.scandir(directory) as children:
            for entry in children:
                count+=1
                if count>recipe['limits']['available_paths']: _fail('inventory_limit')
                yield entry
    prefixes={prefix for group in original['file_groups'].values() for prefix in group['prefixes']}
    prefixes.update(previous['extensions']['contract_json_roots'])
    for entry in entries(root/'evals'):
        if not entry.name.startswith('provider_completion_'): continue
        info=entry.stat(follow_symlinks=False)
        if entry.is_symlink() or getattr(info,'st_file_attributes',0)&0x400: _fail('path_invalid')
        if stat.S_ISDIR(info.st_mode): prefixes.add('evals/'+entry.name+'/')
    minimal=[]
    for prefix in sorted(prefixes):
        if not any(prefix.startswith(parent) for parent in minimal): minimal.append(prefix)
    for prefix in minimal:
        directory=root
        for component in prefix.rstrip('/').split('/'):
            directory=directory/component; _directory(directory)
        pending=[directory]
        while pending:
            for entry in entries(pending.pop()):
                info=entry.stat(follow_symlinks=False)
                if entry.is_symlink() or getattr(info,'st_file_attributes',0)&0x400: _fail('path_invalid')
                path=Path(entry.path)
                if stat.S_ISDIR(info.st_mode): pending.append(path)
                elif stat.S_ISREG(info.st_mode): paths.add(path.relative_to(root).as_posix())
                else: _fail('path_invalid')
    required=set(original['mandatory_files'])|set(original['raw_components'].values())
    required.update(path for group in original['file_groups'].values() for path in group['files'])
    required.update(spec['path'] for spec in original['predecessors'].values())
    required.update(previous['extensions']['mandatory_paths']); required.update(third['mandatory_paths'])
    required.update(recipe['selection']['mandatory_paths'])
    if profile=='campaign': required.update(recipe['selection']['campaign_required_paths'])
    paths.update(required); paths=tuple(sorted(paths))
    selected=source.select_profile_paths(paths,recipe_bytes,predecessor_recipe_bytes=predecessors,profile=profile)
    files={}; total=0
    for name in selected:
        payload=_current_file(root,name); total+=len(payload)
        if total>recipe['limits']['total_bytes']: _fail('byte_limit')
        files[name]=payload
    for name,payload in {source.RECIPE_PATH:recipe_bytes,**predecessors}.items():
        if files[name]!=payload: _fail('tree_changed')
    return files,paths,directories


def verify_current_timed_profile(project_root,*,profile='first_live'):
    try:
        if type(profile) is not str or profile not in source.PROFILE_PATHS: _fail('input_invalid')
        root=_root(project_root); identity=_directory(root)[:2]
        manifest_path,plan_path=source.PROFILE_PATHS[profile]
        manifest,plan=_current_file(root,manifest_path),_current_file(root,plan_path)
        files,paths,directories=_snapshot(root,profile)
        checked=source.verify_profile_documents(files,available_paths=paths,profile=profile,manifest=manifest,plan=plan)
        final_files,_final_paths,final_directories=_snapshot(root,profile)
        if files!=final_files or set(directories)!=set(final_directories): _fail('tree_changed')
        if _current_file(root,manifest_path)!=manifest or _current_file(root,plan_path)!=plan: _fail('tree_changed')
        for path,before in directories.items():
            if _directory(path)!=before: _fail('tree_changed')
        if _root(project_root)!=root or _directory(root)[:2]!=identity: _fail('tree_changed')
        return checked
    except ExternalClosurePrimitiveError: raise
    except (OSError,ValueError,RuntimeError): _fail('source_unavailable')


def build_current_timed_profile_documents(project_root, *, profile='first_live'):
    """Read a stable source snapshot and build bytes only; never create files."""
    try:
        if type(profile) is not str or profile not in source.PROFILE_PATHS: _fail('input_invalid')
        root = _root(project_root); identity = _directory(root)[:2]
        files, paths, directories = _snapshot(root, profile)
        result = source.build_profile_documents(files, available_paths=paths, profile=profile)
        final_files, _final_paths, final_directories = _snapshot(root, profile)
        if files != final_files or set(directories) != set(final_directories): _fail('tree_changed')
        for path, before in directories.items():
            if _directory(path) != before: _fail('tree_changed')
        if _root(project_root) != root or _directory(root)[:2] != identity: _fail('tree_changed')
        return result
    except ExternalClosurePrimitiveError: raise
    except (OSError, ValueError, RuntimeError): _fail('source_unavailable')
