from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


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
QUALITY_PROFILE_REQUIREMENTS: dict[str, tuple[tuple[str, int | float], ...]] = {
    "phase5-ci-v1": (
        ("task_count", 50),
        ("passed_count", 50),
        ("failed_count", 0),
        ("success_rate", 1.0),
        ("evidence_citations_required", 21),
        ("evidence_citations_matched", 21),
        ("evidence_citation_accuracy", 1.0),
    )
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="验证 Phase 5 产物哈希、审计链索引、脱敏与可选质量门禁"
    )
    parser.add_argument(
        "artifact_directory", nargs="?", type=Path, default=Path("artifacts/phase5")
    )
    parser.add_argument(
        "--quality-profile",
        choices=tuple(QUALITY_PROFILE_REQUIREMENTS),
        help="可选的版本化发布质量阈值；不传时保持仅验证产物完整性的历史语义。",
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

    quality_report: Mapping[str, Any] | None = None
    quality_report_readable = True
    if args.quality_profile is not None:
        try:
            loaded_report = json.loads(
                (artifact_directory / "eval_report.json").read_text(encoding="utf-8")
            )
            if not isinstance(loaded_report, Mapping):
                quality_report_readable = False
            else:
                quality_report = loaded_report
        except (OSError, UnicodeError, json.JSONDecodeError):
            quality_report_readable = False
    quality_gate = _build_quality_gate(
        args.quality_profile,
        quality_report,
        report_readable=quality_report_readable,
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
        and quality_gate["status"] != "invalid"
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
        "quality_gate": quality_gate,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if valid else 1


def _build_quality_gate(
    profile: str | None,
    report: Mapping[str, Any] | None,
    *,
    report_readable: bool = True,
) -> dict[str, Any]:
    if profile is None:
        return {
            "profile": None,
            "status": "not_enforced",
            "error_code": None,
            "mismatches": [],
        }
    if not report_readable or report is None:
        return {
            "profile": profile,
            "status": "invalid",
            "error_code": "phase5_quality_report_unreadable",
            "mismatches": [],
        }

    mismatches = [
        {
            "field": field,
            "expected": expected,
            "actual": report.get(field),
        }
        for field, expected in QUALITY_PROFILE_REQUIREMENTS[profile]
        if type(report.get(field)) is not type(expected) or report.get(field) != expected
    ]
    return {
        "profile": profile,
        "status": "valid" if not mismatches else "invalid",
        "error_code": None if not mismatches else "phase5_quality_threshold_mismatch",
        "mismatches": mismatches,
    }


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
