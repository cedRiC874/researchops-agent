"""Compare every protected file's raw-byte SHA-256 with the captured baseline.

This is a development-change check, not a revalidation of the historical
SHA-256 values stored in the source manifest. No researchops code is imported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/expert_review_v1"
BASELINE = SERVICE / "evidence/protected_baseline.json"
MANIFEST = "evals/provider_completion_internal_source_v3/source_manifest_v3.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_path(relative: str) -> Path:
    candidate = PurePosixPath(relative)
    if candidate.is_absolute() or ".." in candidate.parts or "\\" in relative:
        raise ValueError("Invalid protected relative path")
    result = (ROOT / relative).resolve()
    if not result.is_relative_to(ROOT):
        raise ValueError("Protected path escapes repository")
    return result


def verify() -> dict:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    rows = []
    for entry in baseline["files"]:
        path = safe_path(entry["path"])
        actual = digest(path) if path.is_file() else None
        rows.append({
            "path": entry["path"],
            "expected_sha256": entry["sha256"],
            "actual_sha256": actual,
            "unchanged": actual == entry["sha256"],
        })
    current_evals = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "evals/v2").rglob("*") if path.is_file()
    }
    expected_evals = set(baseline["evals_v2_paths"])
    scope_errors = []
    try:
        current_manifest = json.loads(safe_path(MANIFEST).read_text(encoding="utf-8"))
        current_manifest_paths = {entry["path"] for entry in current_manifest["files"]}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        current_manifest_paths = set()
        scope_errors.append("source_manifest_missing_or_invalid")
    expected_manifest_paths = set(baseline["manifest_paths"])
    scope_changes = {
        "evals_v2_added": sorted(current_evals - expected_evals),
        "evals_v2_missing": sorted(expected_evals - current_evals),
        "manifest_entries_added": sorted(current_manifest_paths - expected_manifest_paths),
        "manifest_entries_removed": sorted(expected_manifest_paths - current_manifest_paths),
    }
    return {
        "check": "development-before-after-raw-byte-sha256",
        "base_commit": baseline["base_commit"],
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "historical_manifest_hashes_revalidated": False,
        "passed": all(row["unchanged"] for row in rows) and not any(scope_changes.values()) and not scope_errors,
        "protected_file_count": len(rows),
        "unchanged_file_count": sum(row["unchanged"] for row in rows),
        "scope_changes": scope_changes,
        "scope_errors": scope_errors,
        "files": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Optional JSON report within this service directory")
    args = parser.parse_args()
    report_path = args.report.resolve() if args.report else None
    if report_path and (
        not report_path.is_relative_to(SERVICE.resolve())
        or report_path in {BASELINE.resolve(), Path(__file__).resolve()}
    ):
        parser.error("Report must be within services/expert_review_v1 and must not replace the baseline or verifier")
    report = verify()
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "files"}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
