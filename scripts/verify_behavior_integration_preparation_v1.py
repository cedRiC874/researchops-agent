"""Related-only integration preparation checks; not a full root regression.

Always creates a fresh receipt outside committed source-selection directories.
Never invokes the manifest generator or any runtime entrypoint.
"""
import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
MODULES = (
    "tests.test_behavior_eval_v1", "tests.test_behavior_eval_v1_tolerance",
    "tests.test_behavior_eval_v1_text_observation", "tests.test_behavior_integration_v1",
    "tests.test_internal_source_integrity_v2",
)


def inputs():
    files = set()
    for pattern in ("src/researchops_behavior_eval_v1/*.py", "src/researchops_internal_telemetry/*.py",
        "evals/internal_behavior_eval_v1/**/*", "evals/provider_completion_internal_source_v2/**/*",
        "tests/test_behavior*.py", "tests/test_internal_source_integrity_v2.py",
        "scripts/build_behavior_publication_v1.py", "scripts/build_internal_source_integrity_v2.py",
        "scripts/verify_behavior_integration_preparation_v1.py"):
        files.update(path for path in ROOT.glob(pattern) if path.is_file() and "__pycache__" not in path.parts)
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(files)}


def reserve_report(root, requested):
    """Reserve only a new regular file beneath output; reject traversal/reparse paths."""
    root = Path(root).absolute()
    requested = Path(requested)
    if ".." in requested.parts:
        raise ValueError("report_parent_traversal")
    target = requested if requested.is_absolute() else root / requested
    anchor = root / "output"
    try:
        relative = target.relative_to(anchor)
    except ValueError:
        raise ValueError("report_must_be_outside_source_selector_under_output") from None
    if not relative.parts:
        raise ValueError("report_file_required")
    # Check every existing directory before creating the next one. Do not resolve
    # a link and silently accept a different physical output location.
    current = root
    for part in (None, "output", *relative.parts[:-1]):
        if part is not None:
            current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if part is None:
                raise
            current.mkdir()
            info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("report_directory_link_or_kind")
    if current.resolve(strict=True) != target.parent.absolute():
        raise ValueError("report_physical_path_mismatch")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    with reserve_report(ROOT, args.report) as stream:
        before = inputs()
        started = datetime.now(timezone.utc).isoformat()
        log = io.StringIO()
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            suite = unittest.TestLoader().loadTestsFromNames(MODULES)
            result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        after = inputs()
        exit_code = 0 if result.wasSuccessful() and before == after else 1
        record = dict(schema_version="behavior-integration-preparation-checks/1.0",
            scope="new_and_directly_affected_only", root_full_regression=False, modules=MODULES,
            started_at_utc=started, finished_at_utc=datetime.now(timezone.utc).isoformat(),
            tests=result.testsRun, failures=len(result.failures), errors=len(result.errors), skips=len(result.skipped),
            exit_code=exit_code, binding=dict(before=before, after=after, unchanged=before == after),
            log=log.getvalue(), current_repository_manifest_generated=False,
            online_execution_authorized=False, provider_calls=0)
        json.dump(record, stream, sort_keys=True, ensure_ascii=True, indent=2)
        stream.write("\n")
    print(json.dumps({key: record[key] for key in ("scope", "root_full_regression", "tests", "failures", "errors", "skips", "exit_code")}))
    if exit_code:
        print(log.getvalue())
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
