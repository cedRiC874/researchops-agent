"""Bounded readonly v9/v10 current-source verification; no snapshot generation."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from . import execution_components as base
from . import execution_components_v2 as v2
from . import execution_components_v3 as v3
from .execution_binding import _current_file
from .errors import ExternalClosurePrimitiveError


_MAX_ENTRIES = 16384


def _fail(code):
    raise ExternalClosurePrimitiveError("execution_current_v3_" + code) from None


def _directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        _fail("path_invalid")
    return info.st_dev, info.st_ino, info.st_mtime_ns


def _root(project_root):
    if not isinstance(project_root, Path):
        _fail("input_invalid")
    root = project_root.absolute()
    if ".." in root.parts:
        _fail("path_invalid")
    for path in (*reversed(root.parents), root):
        _directory(path)
    return root


def _snapshot(root, profile):
    recipe_raw = _current_file(root, v3.RECIPE_PATH)
    recipe = v3.decode_recipe(recipe_raw)
    v2_raw, base_raw = _current_file(root, v2.RECIPE_PATH), _current_file(root, base.RECIPE_PATH)
    previous, original = v2.decode_recipe(v2_raw), base._recipe(base_raw)
    directories = {}; paths = set(); count = 0

    def entries(directory):
        nonlocal count
        directories[directory] = _directory(directory)
        # Stream and bound enumeration before materializing a directory list.
        with os.scandir(directory) as children:
            for entry in children:
                count += 1
                if count > _MAX_ENTRIES:
                    _fail("inventory_limit")
                yield entry

    prefixes = {prefix for group in original["file_groups"].values() for prefix in group["prefixes"]}
    prefixes.update(previous["extensions"]["contract_json_roots"])
    # Discover new provider_completion directories, not only those known to v2.
    # Recording evals itself also detects an added directory during validation.
    evals = root / "evals"
    for entry in entries(evals):
        if not entry.name.startswith("provider_completion_"):
            continue
        info = entry.stat(follow_symlinks=False)
        if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            _fail("path_invalid")
        if stat.S_ISDIR(info.st_mode):
            prefixes.add("evals/" + entry.name + "/")
    minimal = []
    for prefix in sorted(prefixes):
        if not any(prefix.startswith(parent) for parent in minimal):
            minimal.append(prefix)
    for prefix in minimal:
        current = root
        for component in prefix.rstrip("/").split("/"):
            current = current / component
            _directory(current)
        pending = [current]
        while pending:
            directory = pending.pop()
            for entry in entries(directory):
                info = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    _fail("path_invalid")
                path = Path(entry.path)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    paths.add(path.relative_to(root).as_posix())
                else:
                    _fail("path_invalid")
    required = set(original["mandatory_files"]) | set(original["raw_components"].values())
    required.update(name for group in original["file_groups"].values() for name in group["files"])
    required.update(item["path"] for item in original["predecessors"].values())
    required.update(previous["extensions"]["mandatory_paths"])
    required.update(recipe["mandatory_paths"])
    if profile == "campaign":
        required.update(recipe["campaign_required_paths"])
    paths.update(required)
    paths = tuple(sorted(paths))
    selected = v3.select_profile_paths(paths, recipe_raw, v2_raw, base_raw, profile=profile)
    files = {}; total = 0
    for name in selected:
        payload = _current_file(root, name)
        total += len(payload)
        if total > recipe["limits"]["total_bytes"]:
            _fail("byte_limit")
        files[name] = payload
    # Do not silently interpret a different recipe than the bytes inventoried.
    if any(files[name] != payload for name, payload in ((v3.RECIPE_PATH, recipe_raw), (v2.RECIPE_PATH, v2_raw), (base.RECIPE_PATH, base_raw))):
        _fail("tree_changed")
    return files, paths, directories


def verify_current_timed_profile(project_root, *, profile="first_live"):
    """Require existing exact profiles; do not write/recover or access Git/Key."""
    try:
        if type(profile) is not str or profile not in v3.PROFILE_PATHS:
            _fail("input_invalid")
        root = _root(project_root)
        root_identity = _directory(root)[:2]
        manifest_path, plan_path = v3.PROFILE_PATHS[profile]
        manifest, plan = _current_file(root, manifest_path), _current_file(root, plan_path)
        files, paths, directories = _snapshot(root, profile)
        result = v3.verify_profile_documents(files, available_paths=paths, profile=profile, manifest=manifest, plan=plan)
        # Re-enumerate, not only reread already known files. Directory mtime can
        # be restored (or have coarse resolution) while new contracts appear.
        final_files, _final_paths, final_directories = _snapshot(root, profile)
        if files != final_files or set(directories) != set(final_directories):
            _fail("tree_changed")
        if _current_file(root, manifest_path) != manifest or _current_file(root, plan_path) != plan:
            _fail("tree_changed")
        for path, identity in directories.items():
            if _directory(path) != identity:
                _fail("tree_changed")
        if _root(project_root) != root or _directory(root)[:2] != root_identity:
            _fail("tree_changed")
        return result
    except ExternalClosurePrimitiveError:
        raise
    except (OSError, ValueError, RuntimeError):
        _fail("source_unavailable")
