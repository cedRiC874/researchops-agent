"""One unfiltered root unittest run with durable before/after receipts.

This is an offline test driver, not a Provider entrypoint. Its create-once batch
marker prevents accidentally launching this authorized full regression twice.
"""
from __future__ import annotations

import contextlib
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unittest
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/"src"))
from researchops_internal_telemetry.source import verify_source, MANIFEST
from researchops_internal_telemetry.contract import raw, digest

BATCHES=("batch-20260913", "batch-20260914-repair1")


def verification_inputs():
    paths=sorted({*ROOT.glob("tests/**/*.py"),*ROOT.glob("scripts/*.py"),ROOT/".github/workflows/ci.yml"})
    return digest(raw([dict(path=path.relative_to(ROOT).as_posix(),sha256=digest(path.read_bytes())) for path in paths]))


def main(batch="batch-20260913"):
    if type(batch) is not str or batch not in BATCHES:
        raise ValueError("regression_batch_not_authorized")
    keys=("DEEPSEEK_API_KEY","OPENAI_API_KEY","ANTHROPIC_API_KEY","MOONSHOT_API_KEY","ANTHROPIC_AUTH_TOKEN")
    if any(name in os.environ for name in keys):
        print('{"status":"not_started","error_code":"test_environment_key_names_present"}')
        return 2
    before=verify_source(ROOT)
    inputs_before=verification_inputs()
    directory=ROOT/"output/internal-offline-regression-v1"/batch
    directory.mkdir(parents=True,exist_ok=False)
    started=datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
    started_ns=time.monotonic_ns()
    initial=dict(schema_version="internal-offline-regression/1.0",status="running",pid=os.getpid(),started_at_utc=started,
        batch_id=batch,driver_sha256=digest(Path(__file__).read_bytes()),
        source_commitment_sha256=before["commitment_sha256"],source_manifest_sha256=digest((ROOT/MANIFEST).read_bytes()),
        verification_inputs_sha256=inputs_before,discovery="unittest discover -s tests -t .",filter=None,
        project_commit_created=False,online_execution_authorized=False)
    with (directory/"started.json").open("xb") as stream:stream.write(raw(initial))
    result=None;driver_error=None
    with (directory/"unittest.log").open("x",encoding="utf-8",buffering=1) as log:
        with contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
            try:
                suite=unittest.defaultTestLoader.discover(str(ROOT/"tests"),pattern="test*.py",top_level_dir=str(ROOT))
                result=unittest.TextTestRunner(stream=log,verbosity=2,failfast=False).run(suite)
            except BaseException as error:
                driver_error=type(error).__name__
                import traceback
                traceback.print_exc(file=log)
    after=None;stable=False
    try:
        after=verify_source(ROOT,expected=before["commitment_sha256"])
        stable=after==before and verification_inputs()==inputs_before
    except Exception:
        driver_error=driver_error or "source_or_verification_inputs_changed"
    exit_code=0 if result is not None and result.wasSuccessful() and stable and driver_error is None else 1
    receipt=dict(initial,status="completed",finished_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
        elapsed_milliseconds=(time.monotonic_ns()-started_ns)//1_000_000,
        tests=result.testsRun if result is not None else None,
        failures=len(result.failures) if result is not None else None,
        errors=len(result.errors) if result is not None else None,
        skips=len(result.skipped) if result is not None else None,
        expected_failures=len(result.expectedFailures) if result is not None else None,
        unexpected_successes=len(result.unexpectedSuccesses) if result is not None else None,
        source_stable=stable,source_after_commitment_sha256=after["commitment_sha256"] if after is not None else None,
        driver_error=driver_error,exit_code=exit_code,full_regression_passed=exit_code==0,
        internal_online_acceptance_passed=False,status_t7_closed=False)
    with (directory/"receipt.json").open("xb") as stream:stream.write(raw(receipt))
    print(json.dumps(receipt,ensure_ascii=True))
    return exit_code


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch",choices=BATCHES,default=BATCHES[0])
    raise SystemExit(main(parser.parse_args().batch))
