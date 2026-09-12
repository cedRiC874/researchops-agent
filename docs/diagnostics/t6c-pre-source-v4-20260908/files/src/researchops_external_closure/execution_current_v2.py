"""Bounded current-worktree source snapshots; never a historical Git fallback."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from . import execution_components as base
from .errors import ExternalClosurePrimitiveError
from .execution_binding import _current_file
from .execution_components_v2 import PROFILE_PATHS,RECIPE_PATH,build_profile_documents,decode_recipe,select_profile_paths,verify_profile_documents


def _fail(code):
    raise ExternalClosurePrimitiveError("execution_current_v2_"+code) from None


def _snapshot(project_root: Path,profile: str):
    if not isinstance(project_root,Path) or type(profile) is not str or profile not in PROFILE_PATHS:
        _fail("input_invalid")
    root=project_root.resolve(strict=True)
    recipe_raw=_current_file(root,RECIPE_PATH)
    recipe=decode_recipe(recipe_raw)
    base_raw=_current_file(root,base.RECIPE_PATH)
    base_recipe=base._recipe(base_raw)
    prefixes=sorted({prefix for group in base_recipe["file_groups"].values() for prefix in group["prefixes"]}
                    |set(recipe["extensions"]["contract_json_roots"]))
    roots=[]
    for prefix in prefixes:
        if not any(prefix.startswith(parent) for parent in roots):
            roots.append(prefix)
    paths=set();directories={};count=0
    for prefix in roots:
        current=root
        for part in prefix.rstrip("/").split("/"):
            current=current/part
            metadata=current.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or int(getattr(metadata,"st_file_attributes",0))&0x400 or stat.S_ISLNK(metadata.st_mode):
                _fail("path_invalid")
        pending=[current]
        while pending:
            directory=pending.pop()
            metadata=directory.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or int(getattr(metadata,"st_file_attributes",0))&0x400 or stat.S_ISLNK(metadata.st_mode):
                _fail("path_invalid")
            directories[directory]=(metadata.st_dev,metadata.st_ino,metadata.st_mtime_ns)
            with os.scandir(directory) as entries:
                for entry in entries:
                    count+=1
                    if count>16384:
                        _fail("inventory_limit")
                    info=entry.stat(follow_symlinks=False)
                    if entry.is_symlink() or int(getattr(info,"st_file_attributes",0))&0x400:
                        _fail("path_invalid")
                    path=Path(entry.path)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(info.st_mode):
                        paths.add(path.relative_to(root).as_posix())
                    else:
                        _fail("path_invalid")
    required=set(base_recipe["mandatory_files"])|set(base_recipe["raw_components"].values())
    required.update(name for group in base_recipe["file_groups"].values() for name in group["files"])
    required.update(item["path"] for item in base_recipe["predecessors"].values())
    required.update(recipe["extensions"]["mandatory_paths"])
    if profile=="campaign":
        required.update(recipe["extensions"]["campaign_required_paths"])
    paths.update(required)
    paths=tuple(sorted(paths))
    selected=select_profile_paths(paths,recipe_raw,base_raw,profile=profile)
    files={};total=0
    for path in selected:
        value=_current_file(root,path)
        total+=len(value)
        if total>32*1024*1024:
            _fail("byte_limit")
        files[path]=value
    for path,value in files.items():
        if _current_file(root,path)!=value:
            _fail("tree_changed")
    for path,identity in directories.items():
        observed=path.lstat()
        if (not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode)
            or int(getattr(observed,"st_file_attributes",0))&0x400
            or (observed.st_dev,observed.st_ino,observed.st_mtime_ns)!=identity):
            _fail("tree_changed")
    return root,files,paths


def build_current_profile_documents(project_root: Path,*,profile: str="first_live"):
    _root,files,paths=_snapshot(project_root,profile)
    return build_profile_documents(files,available_paths=paths,profile=profile)


def verify_current_profile(project_root: Path,*,profile: str="first_live"):
    root,files,paths=_snapshot(project_root,profile)
    manifest,plan=PROFILE_PATHS[profile]
    return verify_profile_documents(files,available_paths=paths,profile=profile,
        manifest=_current_file(root,manifest),plan=_current_file(root,plan))


__all__=["build_current_profile_documents","verify_current_profile"]
