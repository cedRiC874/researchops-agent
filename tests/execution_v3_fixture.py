"""Synthetic timed-source inventories; never write the real worktree's plans."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from researchops_external_closure import execution_components as base
from researchops_external_closure import execution_components_v2 as v2
from researchops_external_closure import execution_components_v3 as v3
from tests import execution_v2_fixture as prior


ROOT = Path(__file__).resolve().parents[1]


def archived_first_live_files():
    """Explicit legacy replay; never claim coverage of the current expanded tree.

    Keep first_live_files() unchanged for callers still testing current-tree
    selection. The archived bytes are verified before adding a synthetic marker.
    """
    directory = ROOT / 'docs/diagnostics/t6c-pre-source-v4-20260908'
    payload = (directory / 'manifest.json').read_bytes()
    if hashlib.sha256(payload).hexdigest() != '871bdce5acb63fb6eab2d525465c1ce93ac8dc8c32b80bc2d81942504ae57e11':
        raise ValueError('legacy_fixture_manifest_drift')
    manifest = json.loads(payload)
    files = {}
    for row in manifest['files']:
        payload = (directory / 'files' / row['path']).read_bytes()
        if len(payload) != row['bytes'] or hashlib.sha256(payload).hexdigest() != row['sha256']:
            raise ValueError('legacy_fixture_file_drift')
        files[row['path']] = payload
    paths = tuple(sorted(files))
    selected = v3.select_profile_paths(paths, files[v3.RECIPE_PATH], files[v2.RECIPE_PATH],
                                       files[base.RECIPE_PATH], profile='first_live')
    if len(files) != 252 or set(selected) != set(files):
        raise ValueError('legacy_fixture_inventory_drift')
    files['src/researchops/__init__.py'] += b'\n# Synthetic archived source-profile fixture; not an execution anchor.\n'
    return files, paths


def first_live_files():
    files, paths = prior.first_live_source_files()
    files, paths = dict(files), set(paths)
    recipe_bytes = (ROOT / v3.RECIPE_PATH).read_bytes()
    recipe = json.loads(recipe_bytes)
    paths.update(recipe["mandatory_paths"])
    # Selection derives from the complete synthetic tree, not the generated
    # manifest. Include all public contract paths present in the test fixture.
    for directory in (ROOT / "evals").glob("provider_completion_*"):
        if directory.is_dir():
            paths.update(path.relative_to(ROOT).as_posix() for path in directory.rglob("*.json"))
    selected = v3.select_profile_paths(tuple(sorted(paths)), recipe_bytes, files[v2.RECIPE_PATH], files[base.RECIPE_PATH], profile="first_live")
    for path in selected:
        if path not in files:
            files[path] = (ROOT / path).read_bytes()
    # Deliberately differ from the real current tree. These profiles exercise
    # algorithms and temporary Git, not a publishable worktree source anchor.
    files["src/researchops/__init__.py"] += b"\n# Synthetic source-profile fixture; not an execution anchor.\n"
    return {path: files[path] for path in selected}, tuple(sorted(paths))


def campaign_files(files, paths, first):
    manifest, plan = v3.PROFILE_PATHS["first_live"]
    result = dict(files)
    result.update({manifest: first.manifest, plan: first.plan,
        "evals/provider_completion_runtime_registry_v3/runtime_registry_v3.json": b"{}",
        "evals/provider_completion_runtime_registry_v3/registry_admission_bundle_v1.json": b"{}"})
    paths = tuple(sorted(set(paths) | set(result)))
    selected = v3.select_profile_paths(paths, result[v3.RECIPE_PATH], result[v2.RECIPE_PATH], result[base.RECIPE_PATH], profile="campaign")
    return {path: result[path] for path in selected}, paths
