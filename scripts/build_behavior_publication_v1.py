"""Offline create-once public derivatives; never edit the retained development tree.

Only explicitly named historical JSON reports are transformed. ZIPs remain in the
development tree; their safe relative member metadata is inspected, not extracted.
This utility is neither a scorer nor a Provider execution entrypoint.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BASE = "evals/internal_behavior_eval_v1"
REPORTS = (
    "repair_p1/validation-red.json",
    "text_observation_v1_1/validation-before-implementation.json",
)
ARCHIVES = ("repair_p1/pre-repair-snapshot.zip", "text_observation_v1_1/pre-revision-snapshot.zip")
HANDOFFS = ("HANDOFF.md", "repair_p1/HANDOFF.md", "text_observation_v1_1/HANDOFF.md")
LOCAL_USER = re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\\/\r\n\"']+")
ABSOLUTE = re.compile(r"[A-Za-z]:[\\/]|(?<!:)//(?:Users|home)/|/home/[^/]+|/Users/[^/]+")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()


def transform(value, source_root):
    if type(value) is str:
        value = value.replace(str(source_root), "{DEVELOPMENT_ROOT}")
        value = value.replace(source_root.as_posix(), "{DEVELOPMENT_ROOT}")
        return LOCAL_USER.sub("{LOCAL_USER}", value)
    if type(value) is list:
        return [transform(item, source_root) for item in value]
    if type(value) is dict:
        return {key: transform(item, source_root) for key, item in value.items()}
    return value


def private_paths(value):
    if type(value) is str:
        return bool(ABSOLUTE.search(value))
    if type(value) is list:
        return any(private_paths(item) for item in value)
    if type(value) is dict:
        return any(private_paths(key) or private_paths(item) for key, item in value.items())
    return False


def archive_members(payload, *, depth=0, budget=None):
    budget = [0, 0] if budget is None else budget
    if depth > 3 or len(payload) > 8 * 1024 * 1024:
        raise ValueError("archive_bound")
    members = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for entry in archive.infolist():
            name = entry.filename
            parts = PurePosixPath(name).parts
            if "\\" in name or ":" in name or name.startswith("/") or ".." in parts:
                raise ValueError("unsafe_archive_member")
            budget[0] += 1
            budget[1] += entry.file_size
            if budget[0] > 256 or budget[1] > 16 * 1024 * 1024:
                raise ValueError("archive_bound")
            data = archive.read(entry)
            nested = archive_members(data, depth=depth + 1, budget=budget) if name.endswith(".zip") else None
            if nested is not None:
                contains_private_path = any(row["contains_private_absolute_path"] for row in nested)
            elif name.endswith(".json"):
                contains_private_path = private_paths(json.loads(data))
            else:
                contains_private_path = bool(ABSOLUTE.search(data.decode("utf-8", errors="strict")))
            row = dict(path=name, bytes=len(data), sha256=sha(data),
                contains_private_absolute_path=contains_private_path)
            if nested is not None:
                row["nested_members"] = nested
            members.append(row)
    return members


def build(source_root):
    source_root = Path(source_root).absolute()
    if source_root == ROOT:
        raise ValueError("source_must_be_retained_development_tree")
    planned, report_rows, archive_rows = [], [], []
    for relative in REPORTS:
        source = source_root / BASE / relative
        original = source.read_bytes()
        document = json.loads(original)
        sanitized = transform(document, source_root)
        if private_paths(sanitized):
            raise ValueError("unhandled_private_path")
        derivative = canonical(sanitized)
        target = relative.removesuffix(".json") + ".public-redacted-v1.json"
        counts = {key: document[key] for key in ("tests", "failures", "errors", "skips", "exit_code")}
        if counts != {key: sanitized[key] for key in counts}:
            raise ValueError("outcome_changed")
        report_rows.append(dict(original_relative_path=relative, original_sha256=sha(original),
            original_bytes=len(original), derivative_relative_path=target, derivative_sha256=sha(derivative),
            derivative_bytes=len(derivative), outcome_preserved=counts,
            historical_byte_binding_is_not_current_integration_validation=True))
        planned.append((ROOT / BASE / target, derivative))
    for relative in ARCHIVES:
        source = source_root / BASE / relative
        payload = source.read_bytes()
        members = archive_members(payload)
        archive_rows.append(dict(original_relative_path=relative, original_sha256=sha(payload),
            original_bytes=len(payload), public_archive_created=False, originals_retained_locally=True,
            members=members))
    provenance = dict(schema_version="behavior-evaluator-public-derivation/1.0",
        source_kind="historical_validation_redaction_not_new_test_execution",
        original_development_files_modified=False, original_local_absolute_path_recorded=False,
        rules=["Replace the exact development root with {DEVELOPMENT_ROOT} in decoded string values.",
               "Replace Windows user-profile prefixes with {LOCAL_USER} in decoded string values.",
               "Preserve JSON keys, numbers, booleans, null, list order and all other text.",
               "Emit deterministic UTF-8 JSON; hashes are new derivative hashes, never original byte identity.",
               "Do not publish or extract either original ZIP; publish only relative member metadata and digests."],
        reports=report_rows, archives=archive_rows,
        limitations=["Redaction changes report bytes; retained bindings describe historical files only.",
                     "A digest does not disclose an original file or verify unseen private contents."])
    planned.append((ROOT / BASE / "PUBLIC_DERIVATION_PROVENANCE_v1.json", canonical(provenance)))
    if any(path.exists() for path, _ in planned):
        raise FileExistsError("public_derivative_already_exists")
    for path, data in planned:
        with path.open("xb") as stream:
            stream.write(data)
    return dict(created_count=len(planned), originals_modified=False, test_execution=False)


def build_handoff_derivatives(source_root):
    source_root = Path(source_root).absolute()
    if source_root == ROOT:
        raise ValueError("source_must_be_retained_development_tree")
    planned, rows = [], []
    for relative in HANDOFFS:
        original = (source_root / BASE / relative).read_bytes()
        text = original.decode("utf-8")
        sanitized = transform(text, source_root)
        if private_paths(sanitized):
            raise ValueError("unhandled_private_path")
        derivative = sanitized.encode("utf-8")
        target = relative.removesuffix(".md") + ".public-redacted-v1.md"
        planned.append((ROOT / BASE / target, derivative))
        rows.append(dict(original_relative_path=relative, original_sha256=sha(original), original_bytes=len(original),
            derivative_relative_path=target, derivative_sha256=sha(derivative), derivative_bytes=len(derivative),
            transformation="Only exact development-root and Windows user-profile prefix replacement; all other characters retained."))
    record = dict(schema_version="behavior-evaluator-handoff-public-derivation/1.0", originals_modified=False,
        source_kind="historical_handoff_redaction_not_new_validation", original_development_absolute_path_recorded=False,
        reports=rows, replaces_existing_public_provenance=False)
    planned.append((ROOT / BASE / "PUBLIC_HANDOFF_DERIVATION_PROVENANCE_v1.json", canonical(record)))
    if any(path.exists() for path, _ in planned):
        raise FileExistsError("public_derivative_already_exists")
    for path, data in planned:
        with path.open("xb") as stream:
            stream.write(data)
    return dict(created_count=len(planned), originals_modified=False, test_execution=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retained-development-root", required=True, type=Path)
    parser.add_argument("--handoffs-only", action="store_true")
    args = parser.parse_args()
    operation = build_handoff_derivatives if args.handoffs_only else build
    print(json.dumps(operation(args.retained_development_root), sort_keys=True))
