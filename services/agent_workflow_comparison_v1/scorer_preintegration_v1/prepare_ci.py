"""Stage an explicit source-file set on the fixed scorer commit, using local Git only."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

COMMIT = "fcc2026c60943de6016495ad244291689a9d491d"
TREE = "cb4fd9421d6f25da121aefa51bb30244900f0a40"
NAMESPACE = "services/agent_workflow_comparison_v1/"
DIRECTORY = NAMESPACE + "scorer_preintegration_v1/"
MANIFEST = DIRECTORY + "publication-files.json"


class PreparationError(ValueError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def decode_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PreparationError("duplicate_manifest_field")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def read_json(path):
    return decode_json(path.read_bytes())


def validate_manifest(data):
    if type(data) is not dict or set(data) != {"schema_version", "scorer_commit", "scorer_tree", "files", "self_excluded"}:
        raise PreparationError("manifest_structure_invalid")
    if data["schema_version"] != "item6-public-file-set-v1" or data["scorer_commit"] != COMMIT or data["scorer_tree"] != TREE or data["self_excluded"] != MANIFEST:
        raise PreparationError("manifest_anchor_invalid")
    if type(data["files"]) is not dict or not data["files"]:
        raise PreparationError("manifest_files_empty")
    for name, value in data["files"].items():
        if type(name) is not str or not name.startswith(NAMESPACE) or "\\" in name or ":" in name or (
            any(part in {"", ".", ".."} for part in name.split("/"))
        ) or PurePosixPath(name).as_posix() != name or name == MANIFEST:
            raise PreparationError("manifest_path_invalid")
        if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise PreparationError("manifest_hash_invalid")
    required = {DIRECTORY + name for name in (
        "identity.py", "bootstrap.py", "bridge.py", "run.py", "verify.py", "prepare_ci.py",
        "contracts.json", "expectations.json", "carryover.json", "upstream.json",
        "test_preintegration.py", "test_publication.py")}
    if not required <= set(data["files"]):
        raise PreparationError("manifest_required_files_missing")


def safe_file(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise PreparationError("source_file_missing_or_escape:" + relative)
    current = path
    while current != root:
        if current.is_symlink():
            raise PreparationError("source_symlink_denied:" + relative)
        current = current.parent
    return path


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def snapshot(root):
    manifest_bytes = safe_file(root, MANIFEST).read_bytes()
    manifest = decode_json(manifest_bytes)
    validate_manifest(manifest)
    buffers = {}
    for name, expected in manifest["files"].items():
        data = safe_file(root, name).read_bytes()
        if sha(data) != expected:
            raise PreparationError("source_hash_mismatch:" + name)
        buffers[name] = data
    buffers[MANIFEST] = manifest_bytes
    return manifest, buffers


def prepare(source, destination):
    source, destination = source.resolve(), destination.resolve()
    if destination == source or destination.is_relative_to(source):
        raise PreparationError("destination_must_be_outside_source_checkout")
    head = git(source, "rev-parse", "HEAD").decode().strip()
    if git(source, "rev-parse", COMMIT + "^{tree}").decode().strip() != TREE:
        raise PreparationError("fixed_scorer_object_mismatch")
    manifest, buffers = snapshot(source)
    inputs = {name: sha(data) for name, data in buffers.items()}
    identity = {"scorer_commit": COMMIT, "scorer_tree": TREE, "tested_source_commit": head,
                "tested_source_has_uncommitted_changes": bool(git(source, "status", "--porcelain", "--untracked-files=all").strip()),
                "tested_source_files_sha256": inputs,
                "tested_source_set_sha256": sha(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()),
                "historical_origin_check": "not_performed_in_this_context",
                "formal_integration_accepted": False}
    receipt_path = destination / DIRECTORY / "outputs/preparation.json"
    if destination.exists():
        if not receipt_path.is_file() or git(destination, "rev-parse", "HEAD").decode().strip() != COMMIT:
            raise PreparationError("existing_destination_not_reusable")
        old = read_json(receipt_path)
        if old != identity or any(sha(safe_file(destination, n).read_bytes()) != h for n, h in inputs.items()):
            raise PreparationError("existing_destination_does_not_match_source")
        return {"status": "reused", **old}
    destination.parent.mkdir(parents=True, exist_ok=True)
    git(source, "worktree", "add", "--detach", str(destination), COMMIT)
    for name, data in buffers.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as f:
            f.write(data)
    # Check the fixed scorer files from immutable Git objects without importing source modules.
    fixed = read_json(destination / DIRECTORY / "upstream.json")["fixed_files_sha256"]
    for name, expected in fixed.items():
        if sha(git(destination, "show", COMMIT + ":" + name)) != expected or sha((destination / name).read_bytes()) != expected:
            raise PreparationError("staged_fixed_file_mismatch:" + name)
    if git(source, "rev-parse", "HEAD").decode().strip() != head or any(
        sha(safe_file(source, name).read_bytes()) != value for name, value in inputs.items()
    ):
        raise PreparationError("source_changed_during_preparation")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with receipt_path.open("x", encoding="utf-8", newline="\n") as f:
        json.dump(identity, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return {"status": "created", **identity}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = prepare(args.source_root, args.destination)
    except (PreparationError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({k: v for k, v in result.items() if k != "tested_source_files_sha256"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
