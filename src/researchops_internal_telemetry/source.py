"""Named Internal source-only commitment; old v11 artifacts are never rewritten."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import types
from importlib.machinery import ModuleSpec
from pathlib import Path

from researchops_external_closure.io import read_regular_file_no_follow
from researchops_completion_timing.process_environment import verify_process_environment
from researchops.deepseek_completion_first_live_validation import _git_offline_environment
from .contract import ROOT, DIRECTORY, commitment, decode, digest, raw, read, require

MANIFEST = DIRECTORY + "/source_manifest_v1.json"
HISTORICAL_COMMIT = "c945d168ee084f82ab690a2f118912bf28956c27"


def source_files(root=ROOT):
    root = Path(root).absolute()
    files = {"pyproject.toml", "requirements.lock", "requirements.linux.lock", "probe_out_v3.json"}
    # Bind every source/config/policy, not just currently imported packages.
    pending, visited, entry_count = [root / "src", root / "evals"], 0, 0
    while pending:
        current = pending.pop()
        visited += 1
        require(visited <= 4096, "source_enumeration_limit")
        with os.scandir(current) as entries:
            for entry in entries:
                entry_count += 1
                require(entry_count <= 16384 and len(files) <= 8192, "source_enumeration_limit")
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400, "source_link")
                if stat.S_ISDIR(info.st_mode):
                    if entry.name != "__pycache__": pending.append(path)
                    continue
                require(stat.S_ISREG(info.st_mode), "source_file_kind")
                if path.suffix in (".py", ".json", ".jsonl", ".txt", ".yaml", ".yml", ".toml"):
                    relative = path.relative_to(root).as_posix()
                    if relative != MANIFEST: files.add(relative)
    return sorted(files)


def build_manifest(root=ROOT):
    root = Path(root).absolute()
    entries, total = [], 0
    for name in source_files(root):
        payload = read_regular_file_no_follow(root / name, max_bytes=8 * 1024 * 1024)
        total += len(payload)
        require(total <= 64 * 1024 * 1024, "source_byte_limit")
        entries.append(dict(path=name, bytes=len(payload), sha256=digest(payload)))
    body = dict(schema_version="provider-completion-internal-source/1.0", algorithm="internal-all-source-and-policy-v1",
                historical_commit=HISTORICAL_COMMIT, files=entries)
    return dict(body, commitment_sha256=commitment("source", body))


def verify_source(root=ROOT, *, expected=None):
    root = Path(root).absolute()
    current = build_manifest(root)
    saved = decode(read(root / MANIFEST, 2 * 1024 * 1024), 2 * 1024 * 1024)
    require(current == saved and (expected is None or current["commitment_sha256"] == expected), "source_drift")
    return current


def git(root, *args):
    result = subprocess.run(["git", "--no-replace-objects", "-C", str(root), *args], env=_git_offline_environment(), capture_output=True, timeout=30, check=False)
    require(result.returncode == 0, "git_unavailable")
    return result.stdout


def verify_execution(root, execution_commit, source):
    """HEAD and committed source bytes, not merely a dirty tree's self-hash."""
    root = Path(root).absolute()
    require(git(root, "rev-parse", "HEAD").decode("ascii").strip() == execution_commit, "execution_commit")
    # One batch process; filenames come only from the bounded source selector.
    specs = [execution_commit + ":" + row["path"] for row in source["files"]] + [execution_commit + ":" + MANIFEST]
    result = subprocess.run(["git", "--no-replace-objects", "-C", str(root), "cat-file", "--batch"], env=_git_offline_environment(), input=("\n".join(specs) + "\n").encode(), capture_output=True, timeout=60)
    require(result.returncode == 0, "git_unavailable")
    payload, offset = result.stdout, 0
    for name in [row["path"] for row in source["files"]] + [MANIFEST]:
        end = payload.find(b"\n", offset)
        require(end >= 0, "execution_source_missing")
        header = payload[offset:end].split()
        require(len(header) == 3 and header[1] == b"blob" and header[2].isdigit(), "execution_source_missing")
        size = int(header[2]); offset = end + 1
        require(size <= 8 * 1024 * 1024 and payload[offset:offset + size] == read(root / name, 8 * 1024 * 1024), "execution_source_drift")
        offset += size + 1
    require(offset == len(payload), "execution_source_trailing")
    entries = {row["path"]: row for row in source["files"]}
    environment = verify_process_environment(root, expected_dependency_lock_sha256=entries["requirements.lock"]["sha256"],
        expected_pyproject_sha256=entries["pyproject.toml"]["sha256"])
    # The legacy checker deliberately knows only legacy namespaces.
    for name, module in tuple(sys.modules.items()):
        if name == "researchops_internal_telemetry" or name.startswith("researchops_internal_telemetry."):
            require(type(module) is types.ModuleType, "internal_module_origin")
            values = vars(module)
            expected_paths = (root / "src" / (name.replace(".", "/") + ".py"), root / "src" / name.replace(".", "/") / "__init__.py")
            path = Path(values.get("__file__", "")).absolute()
            spec = values.get("__spec__")
            require(path in expected_paths and type(spec) is ModuleSpec and type(spec.origin) is str
                and Path(spec.origin).absolute() == path and values.get("__name__") == name, "internal_module_origin")
            if path.name == "__init__.py":
                require(type(values.get("__path__")) in (list,tuple) and len(values["__path__"]) == 1
                    and Path(values["__path__"][0]).absolute() == path.parent, "internal_package_origin")
    return environment
