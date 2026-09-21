"""Separate historical baseline, exact source bytes and tested Git identity."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
BASE = "dfb568d26f0745e397f6cc06dc020ad1ee870de2"
SCORER = "fcc2026c60943de6016495ad244291689a9d491d"
MANIFEST = ROOT.relative_to(REPO).as_posix() + "/runtime-manifest.json"
PREFIX = "services/agent_workflow_comparison_v1/"
FOLDERS = ("controlled_comparison_v1", "deepseek_flash_v1", "controlled_publication_v1")
SHARED = ("requirements.lock", "requirements.linux.lock", "pyproject.toml",
          ".github/workflows/item6-controlled-comparison.yml",
          "src/researchops_behavior_eval_v1/__init__.py", "src/researchops_behavior_eval_v1/core.py",
          "src/researchops_behavior_eval_v1/__main__.py", "evals/internal_behavior_eval_v1/CONTRACT.md",
          "evals/internal_behavior_eval_v1/text_observation_v1_1/REVISION.md",
          PREFIX + "__init__.py", PREFIX + "core.py", PREFIX + "aggregate_read_v1/__init__.py",
          PREFIX + "aggregate_read_v1/core.py")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def read_manifest(repo=REPO):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result: raise ValueError("duplicate_manifest_key")
            result[k] = v
        return result
    value = json.loads((repo / MANIFEST).read_text(encoding="utf-8"), object_pairs_hook=unique)
    if set(value) != {"schema", "base_commit", "scorer_commit", "files"} or value["schema"] != "item6-offline-publication-source/1":
        raise ValueError("manifest_schema")
    if value["base_commit"] != BASE or value["scorer_commit"] != SCORER or type(value["files"]) is not dict:
        raise ValueError("manifest_identity")
    for name, digest in value["files"].items():
        parts = PurePosixPath(name).parts
        if not parts or ":" in name or "\\" in name or name.startswith("/") or any(x in {".", ".."} for x in parts) or "/".join(parts) != name:
            raise ValueError("manifest_path")
        if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("manifest_hash")
    return value


def source_names(repo=REPO):
    names = set(SHARED)
    for folder in FOLDERS:
        for path in (repo / PREFIX / folder).rglob("*"):
            relative = path.relative_to(repo)
            if {"outputs", "public", "__pycache__"}.intersection(relative.parts): continue
            if path.is_symlink() or path.is_junction(): raise ValueError("source_link")
            if path.is_file() and path.suffix in {".py", ".json", ".md"} and relative.as_posix() != MANIFEST:
                names.add(relative.as_posix())
    return sorted(names)


def verify_files(repo=REPO):
    manifest = read_manifest(repo)
    if set(manifest["files"]) != set(source_names(repo)):
        raise ValueError("source_inventory_mismatch")
    for name, expected in manifest["files"].items():
        p = repo / name
        if p.is_symlink() or p.is_junction() or sha(p) != expected:
            raise ValueError("source_bytes_mismatch")
    return manifest


def runtime_snapshot(repo=REPO):
    # Gold is independently bound at launch/scoring, never read by path decisions.
    manifest = read_manifest(repo)
    names = [n for n in manifest["files"] if "/scoring/" not in n]
    return {n: sha(repo / n) for n in [*names, MANIFEST]}


def _git(repo, *args):
    return subprocess.check_output(["git", "--no-replace-objects", "-C", str(repo), *args], stderr=subprocess.PIPE)


def verify_identity(repo=REPO, *, require_committed=False):
    manifest = verify_files(repo)
    head = _git(repo, "rev-parse", "HEAD").decode().strip()
    _git(repo, "merge-base", "--is-ancestor", BASE, head)
    tracked = set(_git(repo, "ls-tree", "-r", "--name-only", head).decode().splitlines())
    all_names = [*manifest["files"], MANIFEST]
    matching = all(name in tracked and hashlib.sha256(_git(repo, "show", head + ":" + name)).hexdigest() == sha(repo / name)
                   for name in all_names)
    if require_committed and not matching:
        raise ValueError("committed_source_required")
    scorer_hashes = {}
    for name in SHARED:
        if "researchops_behavior_eval_v1/" not in name and not name.startswith("evals/"): continue
        scorer_hashes[name] = hashlib.sha256(_git(repo, "show", SCORER + ":" + name)).hexdigest()
        if sha(repo / name) != scorer_hashes[name]: raise ValueError("scorer_dependency_drift")
    return {"execution_base_commit": BASE, "tested_checkout_head": head,
            "execution_commit": head if matching else None, "source_matches_commit": matching,
            "scorer_commit": SCORER, "scorer_files_sha256": scorer_hashes,
            "source_manifest_sha256": sha(repo / MANIFEST),
            "status": "committed_offline_source" if matching else "uncommitted_offline_candidate",
            "online_authorized": False, "historical_origin_check": "not_performed_in_this_context"}
