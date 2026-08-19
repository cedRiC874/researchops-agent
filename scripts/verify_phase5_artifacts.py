from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from researchops.audit import AuditLedger  # noqa: E402


TEXT_SUFFIXES = {".json", ".jsonl", ".md"}
FORBIDDEN_CANARIES = {
    "row_id_P0001": "p0001",
    "api_key_prefix": "sk-canary",
    "authorization_header": "authorization: bearer",
    "traceback": "traceback (most recent call last)",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 Phase 5 产物哈希、审计链索引与脱敏")
    parser.add_argument(
        "artifact_directory", nargs="?", type=Path, default=Path("artifacts/phase5")
    )
    args = parser.parse_args()
    artifact_directory = args.artifact_directory.resolve()
    project_root = PROJECT_ROOT
    manifest = json.loads(
        (artifact_directory / "eval_manifest.json").read_text(encoding="utf-8")
    )
    audit_index = json.loads(
        (artifact_directory / "eval_audit_index.json").read_text(encoding="utf-8")
    )

    hash_mismatches: list[str] = []
    for name, metadata in manifest["artifacts"].items():
        path = artifact_directory / name
        if not path.is_file() or _sha256(path) != metadata["sha256"]:
            hash_mismatches.append(name)
    provenance_mismatches: list[str] = []
    if _sha256(project_root / "evals" / "tasks.jsonl") != manifest["task_corpus"][
        "sha256"
    ]:
        provenance_mismatches.append("task_corpus")
    if _sha256(project_root / "data" / "synthetic_trial.csv") != manifest[
        "dataset_sha256"
    ]:
        provenance_mismatches.append("dataset")
    if _source_tree_sha256(project_root / "src" / "researchops") != manifest[
        "source_tree_sha256"
    ]:
        provenance_mismatches.append("source_tree")

    texts = [
        path.read_text(encoding="utf-8", errors="ignore")
        for path in artifact_directory.iterdir()
        if path.is_file() and path.suffix in TEXT_SUFFIXES
    ]
    database = sqlite3.connect(artifact_directory / "eval_audit.sqlite3")
    try:
        tables = [
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if not str(row[0]).startswith("sqlite_")
        ]
        for table in tables:
            for row in database.execute(f'SELECT * FROM "{table}"'):
                texts.extend(value for value in row if isinstance(value, str))
    finally:
        database.close()
    combined = "\n".join(texts).casefold()
    forbidden = {
        "absolute_project_root": str(project_root.resolve()).casefold() in combined,
        **{
            name: value.casefold() in combined
            for name, value in FORBIDDEN_CANARIES.items()
        },
    }
    ledger = AuditLedger(artifact_directory / "eval_audit.sqlite3")
    invalid_chains = []
    index_mismatches = []
    for item in audit_index["runs"]:
        verification = ledger.verify_chain(item["run_id"])
        if not verification.valid:
            invalid_chains.append(item["task_id"])
        if verification.to_dict() != item["chain_verification"]:
            index_mismatches.append(item["task_id"])
    valid = (
        not hash_mismatches
        and not provenance_mismatches
        and not any(forbidden.values())
        and not invalid_chains
        and not index_mismatches
    )
    payload = {
        "status": "valid" if valid else "invalid",
        "artifact_directory": artifact_directory.name,
        "task_count": len(audit_index["runs"]),
        "hash_mismatches": hash_mismatches,
        "provenance_mismatches": provenance_mismatches,
        "invalid_chains": invalid_chains,
        "audit_index_mismatches": index_mismatches,
        "forbidden_content": forbidden,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if valid else 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_tree_sha256(source_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(source_root.glob("*.py"), key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
