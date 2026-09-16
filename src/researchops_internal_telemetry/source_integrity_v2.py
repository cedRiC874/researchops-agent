"""Named, current-tree source integrity v2: offline only, never runtime admission.

The v1 online source/admission/run modules are not dispatched to this module.
History is verified against immutable Git blobs; no historical code is executed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

from researchops_external_closure.io import read_regular_file_no_follow
from researchops.deepseek_completion_first_live_validation import _git_offline_environment

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = "evals/provider_completion_internal_source_v2"
RECIPE = DIRECTORY + "/recipe_v2.json"
SCHEMA = DIRECTORY + "/source_manifest_v2.schema.json"
MANIFEST = DIRECTORY + "/source_manifest_v2.json"
HISTORICAL_COMMIT = "551b9e252670be871ff75b0638b033b07d3c6f08"
HISTORICAL_TREE = "fa18ea712732425a0ed035439a97b93ee08e78ce"
HISTORICAL_MANIFEST = "evals/provider_completion_internal_v1/source_manifest_v1.json"
HISTORICAL_MANIFEST_SHA256 = "671a625a1092db071dfa01bc43bed29114d3fcef46a1d54a2ac1483eba0ab0d8"
ALGORITHM = "internal-current-tree-offline-source-v2"
DOMAIN = b"researchops.internal.current-tree.offline.source.v2\0"
SUFFIXES = (".py", ".json", ".jsonl", ".txt", ".yaml", ".yml", ".toml")
EXPLICIT = ("pyproject.toml", "requirements.lock", "requirements.linux.lock", "probe_out_v3.json",
    "scripts/build_internal_source_integrity_v2.py", "scripts/build_behavior_publication_v1.py",
    SCHEMA, "src/researchops_internal_telemetry/source_integrity_v2.py",
    "evals/internal_behavior_eval_v1/CONTRACT.md",
    "evals/internal_behavior_eval_v1/text_observation_v1_1/REVISION.md")
LIMITS = dict(directories=4096, entries=16384, files=8192, file_bytes=8388608,
    total_bytes=67108864, path_characters=512)
FLAGS = dict(source_integrity_only=True, online_execution_authorized=False,
    runtime_admission_verified=False, historical_result_revalidated=False)


class SourceIntegrityV2Error(ValueError):
    def __init__(self, code):
        self.code = "internal_source_v2_" + code
        super().__init__(self.code)


def require(value, code):
    if not value:
        raise SourceIntegrityV2Error(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "duplicate_key")
        value[key] = item
    return value


def decode(data):
    require(type(data) is bytes and len(data) <= 2 * 1024 * 1024, "document_size")
    try:
        value = json.loads(data, object_pairs_hook=_object, parse_constant=lambda _: require(False, "nonfinite"))
    except (UnicodeError, json.JSONDecodeError):
        raise SourceIntegrityV2Error("json") from None
    require(type(value) is dict, "document_type")
    return value


def _relative(name):
    require(type(name) is str and 0 < len(name) <= LIMITS["path_characters"]
        and "\\" not in name and ":" not in name and "\n" not in name and "\r" not in name
        and "\0" not in name and not name.startswith("/"), "path")
    parts = PurePosixPath(name).parts
    require(parts and all(part not in (".", "..", "") for part in parts)
        and "/".join(parts) == name, "path")
    return parts


def _regular(root, name, maximum=8388608):
    parts = _relative(name)
    path = Path(root)
    for index, part in enumerate(parts):
        path = path / part
        info = path.lstat()
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400, "link")
        require(stat.S_ISDIR(info.st_mode) if index < len(parts) - 1 else stat.S_ISREG(info.st_mode), "file_kind")
    return read_regular_file_no_follow(path, max_bytes=maximum)


def expected_recipe():
    return dict(schema_version="internal-current-tree-source-recipe/2.0", algorithm=ALGORITHM,
        scan_roots=["src", "evals"], suffixes=list(SUFFIXES), explicit_files=list(EXPLICIT),
        excluded_file=MANIFEST, ignored_directory_name="__pycache__", limits=LIMITS,
        historical_commit=HISTORICAL_COMMIT, historical_tree=HISTORICAL_TREE,
        historical_manifest=HISTORICAL_MANIFEST, historical_manifest_sha256=HISTORICAL_MANIFEST_SHA256, **FLAGS)


def source_files(root=ROOT):
    root = Path(root).absolute()
    require(canonical(decode(_regular(root, RECIPE))) == canonical(expected_recipe()), "recipe")
    files, pending, directory_count, entry_count = set(EXPLICIT), [root / "src", root / "evals"], 0, 0
    while pending:
        current = pending.pop()
        info = current.lstat()
        require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & 0x400, "directory_kind")
        directory_count += 1
        require(directory_count <= LIMITS["directories"], "enumeration_limit")
        with os.scandir(current) as entries:
            for entry in entries:
                entry_count += 1
                require(entry_count <= LIMITS["entries"], "enumeration_limit")
                path, info = Path(entry.path), entry.stat(follow_symlinks=False)
                name = path.relative_to(root).as_posix()
                _relative(name)
                require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400, "link")
                if stat.S_ISDIR(info.st_mode):
                    if entry.name != "__pycache__":
                        pending.append(path)
                else:
                    require(stat.S_ISREG(info.st_mode), "file_kind")
                    if path.suffix in SUFFIXES and name != MANIFEST:
                        files.add(name)
                        require(len(files) <= LIMITS["files"], "enumeration_limit")
    require(len(files) <= LIMITS["files"], "enumeration_limit")
    return sorted(files)


def _git(root, *args, input=None):
    result = subprocess.run(["git", "--no-replace-objects", "-C", str(root), *args],
        env=_git_offline_environment(), input=input, capture_output=True, timeout=60, check=False)
    require(result.returncode == 0, "historical_git_unavailable")
    return result.stdout


def _blobs(root, names):
    for name in names:
        _relative(name)
    data = _git(root, "cat-file", "--batch", input=("\n".join(HISTORICAL_COMMIT + ":" + name for name in names) + "\n").encode())
    require(len(data) <= LIMITS["total_bytes"] + 1024 * 1024, "historical_byte_limit")
    offset, payloads = 0, []
    for _ in names:
        end = data.find(b"\n", offset)
        require(end >= 0, "historical_blob_missing")
        header = data[offset:end].split()
        require(len(header) == 3 and header[1] == b"blob" and header[2].isdigit(), "historical_blob_missing")
        size, offset = int(header[2]), end + 1
        require(size <= LIMITS["file_bytes"] and offset + size < len(data)
            and data[offset + size:offset + size + 1] == b"\n", "historical_blob_size")
        payloads.append(data[offset:offset + size])
        offset += size + 1
    require(offset == len(data), "historical_blob_trailing")
    return payloads


def verify_lineage(root=ROOT):
    """Recompute v1 commitment and check every saved row against fixed Git bytes."""
    require(_git(root, "rev-parse", HISTORICAL_COMMIT + "^{tree}").decode("ascii").strip() == HISTORICAL_TREE, "historical_tree")
    historical = _blobs(root, [HISTORICAL_MANIFEST])[0]
    require(digest(historical) == HISTORICAL_MANIFEST_SHA256, "historical_manifest_digest")
    require(_regular(root, HISTORICAL_MANIFEST, 2 * 1024 * 1024) == historical, "historical_manifest_local_drift")
    manifest = decode(historical)
    require(set(manifest) == {"schema_version", "algorithm", "historical_commit", "files", "commitment_sha256"}, "historical_manifest_fields")
    body = {key: value for key, value in manifest.items() if key != "commitment_sha256"}
    # Legacy canonical encoding/domain are immutable lineage rules, not v2's domain.
    require(manifest["schema_version"] == "provider-completion-internal-source/1.0"
        and manifest["algorithm"] == "internal-all-source-and-policy-v1"
        and manifest["historical_commit"] == "c945d168ee084f82ab690a2f118912bf28956c27", "historical_manifest_identity")
    legacy_bytes = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    require(digest(b"researchops.internal.v1\0source\0" + legacy_bytes) == manifest["commitment_sha256"], "historical_commitment")
    rows = manifest["files"]
    require(type(rows) is list and 0 < len(rows) <= LIMITS["files"], "historical_files")
    names = []
    for row in rows:
        require(type(row) is dict and set(row) == {"path", "bytes", "sha256"}, "historical_row")
        _relative(row["path"])
        require(type(row["bytes"]) is int and 0 <= row["bytes"] <= LIMITS["file_bytes"]
            and type(row["sha256"]) is str and re.fullmatch("[0-9a-f]{64}", row["sha256"]), "historical_row")
        names.append(row["path"])
    require(names == sorted(set(names)), "historical_file_order")
    for row, data in zip(rows, _blobs(root, names)):
        require(len(data) == row["bytes"] and digest(data) == row["sha256"], "historical_source_bytes")
    return dict(commit=HISTORICAL_COMMIT, tree=HISTORICAL_TREE, manifest_path=HISTORICAL_MANIFEST,
        manifest_sha256=digest(historical), source_commitment_sha256=manifest["commitment_sha256"],
        verified_git_blob_count=len(rows), historical_result_revalidated=False)


def build_manifest(root=ROOT):
    root = Path(root).absolute()
    lineage = verify_lineage(root)
    files, total = [], 0
    for name in source_files(root):
        data = _regular(root, name)
        total += len(data)
        require(total <= LIMITS["total_bytes"], "source_byte_limit")
        files.append(dict(path=name, bytes=len(data), sha256=digest(data)))
    body = dict(schema_version="internal-current-tree-source/2.0", algorithm=ALGORITHM,
        recipe_sha256=digest(_regular(root, RECIPE)), lineage=lineage, files=files, **FLAGS)
    result = dict(body, commitment_sha256=digest(DOMAIN + canonical(body)))
    validate_schema(root, result)
    return result


def validate_schema(root, document):
    """Local-only schema validation; reject every reference before interpretation."""
    from jsonschema import Draft202012Validator
    schema = decode(_regular(root, SCHEMA, 2 * 1024 * 1024))
    pending = [(schema, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        require(depth <= 32 and count <= 8192, "schema_limit")
        if type(item) is dict:
            require(not any(key in item for key in ("$ref", "$dynamicRef", "$recursiveRef")), "schema_reference")
            pending.extend((value, depth + 1) for value in item.values())
        elif type(item) is list:
            pending.extend((value, depth + 1) for value in item)
    Draft202012Validator.check_schema(schema)
    require(not list(Draft202012Validator(schema).iter_errors(document)), "manifest_schema")


def verify_source(root=ROOT, *, expected=None):
    current = build_manifest(root)
    saved = decode(_regular(root, MANIFEST, 2 * 1024 * 1024))
    validate_schema(root, saved)
    require(canonical(saved) == canonical(current) and (expected is None or saved["commitment_sha256"] == expected), "source_drift")
    return current


def write_manifest_exclusive(root=ROOT):
    root = Path(root).absolute()
    target = root / MANIFEST
    # Validate all ancestors via a sibling already bound by the selector.
    _regular(root, RECIPE)
    require(not target.exists() and not target.is_symlink(), "manifest_exists")
    data = canonical(build_manifest(root)) + b"\n"
    with target.open("xb") as stream:
        stream.write(data)
    return dict(path=MANIFEST, sha256=digest(data), bytes=len(data), online_execution_authorized=False)
