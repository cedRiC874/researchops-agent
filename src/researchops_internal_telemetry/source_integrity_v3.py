"""Separate offline v3 source closure; never an online authority issuer."""
from pathlib import Path
import hashlib
import json
import re

from . import source_integrity_v2 as v2

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = "evals/provider_completion_internal_source_v3"
RECIPE = DIRECTORY + "/recipe_v3.json"
SCHEMA = DIRECTORY + "/source_manifest_v3.schema.json"
MANIFEST = DIRECTORY + "/source_manifest_v3.json"
ALGORITHM = "internal-current-tree-offline-source-v3"
DOMAIN = b"researchops.internal.current-tree.offline.source.v3\0"
HISTORICAL_COMMIT = "5605396e165fc5140eba54340d5a9c93f67540dc"
HISTORICAL_TREE = "60a29c8407e55de8ea9b5700fc478f37044d1a49"
HISTORICAL_MANIFEST = v2.MANIFEST
HISTORICAL_MANIFEST_SHA256 = "072c1a542427e3282bb3072b7db662dfda43fd0a1e8f062a6a1dd9e545f7750e"
HISTORICAL_COMMITMENT = "0157e921793e0afe5cd897456535c324ccede9b203eb6792375f1d9b7d67e66d"
FLAGS = dict(source_integrity_only=True, online_execution_authorized=False,
             runtime_admission_verified=False, historical_result_revalidated=False)
SERVICE_MANIFEST = "services/agent_workflow_comparison_v1/controlled_publication_v1/runtime-manifest.json"
SEPARATE_GIT_BINDINGS = (".github/workflows/ci.yml", ".github/workflows/item6-experiment-bridge-offline.yml")
EXPLICIT = (RECIPE, SCHEMA, "src/researchops_internal_telemetry/source_integrity_v3.py",
            "scripts/build_internal_source_integrity_v3.py", "scripts/verify_pre_v6_integrity.py",
            "evals/item6_experiment_bridge_v1/CONTRACT.md", SERVICE_MANIFEST,
            "evals/item6_experiment_bridge_v1/CASE_REJECTION_ISOLATION_V1.md",
            "evals/item6_experiment_bridge_v1/CASE_REJECTION_ISOLATION_V1_1.md",
            "evals/item6_experiment_bridge_v1/PRIVACY_DIAGNOSTICS_V1.md",
            "tests/internal_source_v2_historical_support.py", "tests/test_internal_source_integrity_v3.py",
            "tests/item6_experiment_fixture.py", "tests/test_item6_experiment_authority.py",
            "tests/test_item6_experiment_session.py", "tests/test_item6_experiment_budget.py",
            "tests/test_item6_experiment_artifacts.py", "tests/test_item6_experiment_integration.py",
            "tests/test_item6_case_isolation.py",
            "tests/test_item6_refusal_isolation.py",
            "tests/test_item6_privacy_diagnostics.py",
            "tests/test_internal_source_integrity_v2.py", "tests/test_deepseek_completion_first_live_validation.py",
            "tests/test_kimi_k3_handshake.py", "tests/test_phase6_depth60.py")


class SourceIntegrityV3Error(ValueError):
    def __init__(self, code):
        self.code = "internal_source_v3_" + code
        super().__init__(self.code)


def require(value, code):
    if not value:
        raise SourceIntegrityV3Error(code)


digest = v2.digest
canonical = v2.canonical
decode = v2.decode


def expected_recipe():
    return dict(schema_version="internal-current-tree-source-recipe/3.0", algorithm=ALGORITHM,
                scan_roots=["src", "evals"], suffixes=list(v2.SUFFIXES), explicit_files=list(EXPLICIT),
                service_manifest=SERVICE_MANIFEST, excluded_file=MANIFEST,
                separately_git_bound_workflows=list(SEPARATE_GIT_BINDINGS),
                historical_commit=HISTORICAL_COMMIT, historical_tree=HISTORICAL_TREE,
                historical_manifest=HISTORICAL_MANIFEST, historical_manifest_sha256=HISTORICAL_MANIFEST_SHA256,
                limits=v2.LIMITS, **FLAGS)


def historical_blobs(root, names):
    return committed_blobs(root, HISTORICAL_COMMIT, names)


def committed_blobs(root, commit, names):
    require(type(commit) is str and re.fullmatch(r"[0-9a-f]{40}", commit), "git_commit")
    require(0 < len(names) <= v2.LIMITS["files"], "historical_files")
    for name in names:
        v2._relative(name)
    data = v2._git(root, "cat-file", "--batch", input="".join(commit + ":" + n + "\n" for n in names).encode())
    offset, payloads = 0, []
    for name in names:
        end = data.find(b"\n", offset)
        require(end >= 0, "historical_blob_missing")
        header = data[offset:end].split()
        require(len(header) == 3 and header[1] == b"blob" and header[2].isdigit(), "historical_blob_missing")
        size, offset = int(header[2]), end + 1
        require(size <= v2.LIMITS["file_bytes"] and data[offset+size:offset+size+1] == b"\n", "historical_blob_size")
        payloads.append(data[offset:offset+size])
        offset += size + 1
    require(offset == len(data), "historical_blob_trailing")
    return payloads


def verify_lineage(root=ROOT):
    require(v2._git(root, "rev-parse", HISTORICAL_COMMIT + "^{tree}").decode().strip() == HISTORICAL_TREE, "historical_tree")
    raw = historical_blobs(root, [HISTORICAL_MANIFEST])[0]
    require(digest(raw) == HISTORICAL_MANIFEST_SHA256 and v2._regular(root, HISTORICAL_MANIFEST) == raw, "historical_manifest_bytes")
    manifest = decode(raw)
    body = {k: v for k, v in manifest.items() if k != "commitment_sha256"}
    require(manifest["schema_version"] == "internal-current-tree-source/2.0"
            and manifest["algorithm"] == v2.ALGORITHM
            and digest(v2.DOMAIN + canonical(body)) == manifest["commitment_sha256"] == HISTORICAL_COMMITMENT,
            "historical_commitment")
    rows = manifest["files"]
    require(type(rows) is list and 0 < len(rows) <= v2.LIMITS["files"], "historical_files")
    names = []
    for row in rows:
        require(type(row) is dict and set(row) == {"path", "bytes", "sha256"}, "historical_row")
        v2._relative(row["path"])
        require(type(row["bytes"]) is int and 0 <= row["bytes"] <= v2.LIMITS["file_bytes"]
                and type(row["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "historical_row")
        names.append(row["path"])
    require(names == sorted(set(names)), "historical_order")
    for row, data in zip(rows, historical_blobs(root, names)):
        require(row["bytes"] == len(data) and row["sha256"] == digest(data), "historical_source_bytes")
    v1 = v2.verify_lineage(root)
    require(v1 == manifest["lineage"], "historical_v1_lineage")
    return dict(commit=HISTORICAL_COMMIT, tree=HISTORICAL_TREE, manifest_path=HISTORICAL_MANIFEST,
                manifest_sha256=digest(raw), source_commitment_sha256=HISTORICAL_COMMITMENT,
                verified_git_blob_count=len(rows), historical_result_revalidated=False)


def source_files(root=ROOT):
    root = Path(root).absolute()
    require(decode(v2._regular(root, RECIPE)) == expected_recipe(), "recipe")
    # v2's unchanged bounded enumerator validates all src/evals nodes, not its saved manifest.
    names = set(v2.source_files(root)) | set(EXPLICIT) | {v2.MANIFEST}
    carried = decode(v2._regular(root, SERVICE_MANIFEST))
    require(carried.get("schema") == "item6-offline-publication-source/1" and type(carried.get("files")) is dict, "service_manifest")
    for name, expected in carried["files"].items():
        v2._relative(name)
        require(digest(v2._regular(root, name)) == expected, "carried_service_drift")
        names.add(name)
    names.discard(MANIFEST)
    require(not set(SEPARATE_GIT_BINDINGS).intersection(names), "cyclic_workflow_selection")
    require(len(names) <= v2.LIMITS["files"], "file_limit")
    return sorted(names)


def validate_schema(root, document):
    from jsonschema import Draft202012Validator
    schema = decode(v2._regular(root, SCHEMA))
    pending, count = [(schema, 0)], 0
    while pending:
        item, depth = pending.pop(); count += 1
        require(depth <= 32 and count <= 8192, "schema_limit")
        if type(item) is dict:
            require(not any(k in item for k in ("$ref", "$dynamicRef", "$recursiveRef")), "schema_reference")
            pending.extend((v, depth + 1) for v in item.values())
        elif type(item) is list:
            pending.extend((v, depth + 1) for v in item)
    Draft202012Validator.check_schema(schema)
    require(not list(Draft202012Validator(schema).iter_errors(document)), "manifest_schema")


def build_manifest(root=ROOT):
    lineage = verify_lineage(root)
    rows, total = [], 0
    for name in source_files(root):
        data = v2._regular(root, name)
        total += len(data)
        require(total <= v2.LIMITS["total_bytes"], "byte_limit")
        rows.append(dict(path=name, bytes=len(data), sha256=digest(data)))
    body = dict(schema_version="internal-current-tree-source/3.0", algorithm=ALGORITHM,
                recipe_sha256=digest(v2._regular(root, RECIPE)), lineage=lineage, files=rows, **FLAGS)
    result = dict(body, commitment_sha256=digest(DOMAIN + canonical(body)))
    validate_schema(root, result)
    return result


def verify_source(root=ROOT, *, expected=None):
    current = build_manifest(root)
    saved = decode(v2._regular(root, MANIFEST, 2 * 1024 * 1024))
    validate_schema(root, saved)
    require(saved == current and (expected is None or saved["commitment_sha256"] == expected), "source_drift")
    return current


def write_manifest_exclusive(root=ROOT):
    root = Path(root).absolute()
    target = root / MANIFEST
    v2._regular(root, RECIPE)
    require(not target.exists() and not target.is_symlink(), "manifest_exists")
    data = canonical(build_manifest(root)) + b"\n"
    with target.open("xb") as stream: stream.write(data)
    return dict(path=MANIFEST, sha256=digest(data), bytes=len(data), online_execution_authorized=False)
