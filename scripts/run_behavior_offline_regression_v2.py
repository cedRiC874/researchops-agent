"""One named, unfiltered offline root regression for the behavior-v2 integration.

The original Internal v1 driver and all historical receipts remain unchanged.
This driver grants no runtime authority. Its exclusive started marker is never
released, including on failure, cancellation, incomplete logging or source drift.
"""
from __future__ import annotations

import argparse
import contextlib
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from researchops_internal_telemetry import source_integrity_v2 as source
from scripts.verify_behavior_integration_preparation_v1 import reserve_report

BATCH = "behavior-integration-20260915-user-context-v4"
BASE = "8bfc56e1e56dbd1161190f84ded273a5abd12273"
DIRECTORY = "output/internal-offline-regression-v2/" + BATCH
KEY_NAMES = ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
             "MOONSHOT_API_KEY", "ANTHROPIC_AUTH_TOKEN")
TEST_IDENTITY_VERSION = "unittest-declared-test-identity/1.0"
IDENTITY_ERROR_CODES = frozenset(("native_id_invalid", "function_origin_unavailable",
    "function_description_invalid", "anonymous_function_description_missing"))


def verification_inputs(root):
    root = Path(root).absolute()
    paths = set()
    for pattern in ("tests/**/*.py", "scripts/**/*.py", ".github/workflows/*.yml",
                    ".github/workflows/*.yaml", "evals/internal_behavior_eval_v1/**/*"):
        paths.update(path for path in root.glob(pattern)
                     if path.is_file() and "__pycache__" not in path.parts)
    rows = []
    for path in sorted(paths):
        name = path.relative_to(root).as_posix()
        payload = source._regular(root, name)
        rows.append(dict(path=name, bytes=len(payload), sha256=source.digest(payload)))
    return dict(files=rows, sha256=source.digest(source.canonical(rows)))


def iter_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_cases(item)
        else:
            yield item


def case_ids(suite):
    """Raw unittest IDs, retained for diagnostics rather than assumed unique."""
    for item in iter_cases(suite):
        yield item.id()


class TestIdentityError(ValueError):
    pass


def declared_case_identity(item, native_id):
    """Use declaration metadata, never sequence numbers, object IDs or deduplication."""
    if type(native_id) is not str or not native_id:
        raise TestIdentityError("native_id_invalid")
    if not isinstance(item, unittest.FunctionTestCase):
        return native_id
    function = item._testFunc
    module = getattr(function, "__module__", None)
    qualified_name = getattr(function, "__qualname__", None)
    description = item.shortDescription()
    if type(module) is not str or not module or type(qualified_name) is not str or not qualified_name:
        raise TestIdentityError("function_origin_unavailable")
    if description is not None and (type(description) is not str or not description.strip()):
        raise TestIdentityError("function_description_invalid")
    if getattr(function, "__name__", None) == "<lambda>" and description is None:
        raise TestIdentityError("anonymous_function_description_missing")
    # The JSON tuple is unambiguous and preserves descriptions exactly as declared.
    return "function:" + source.canonical([module, qualified_name, description]).decode("ascii")


def checked_case_ids(suite, *, observation=None):
    observation = {} if observation is None else observation
    cases = list(iter_cases(suite))
    observation.update(status="identifying", discovered_tests=len(cases),
                       native_ids=[], canonical_ids=[], identity_errors=[], duplicate_ids=None)
    ids, native_ids, errors = observation["canonical_ids"], observation["native_ids"], observation["identity_errors"]
    for item in cases:
        native_id, identity = None, None
        try:
            native_id = item.id()
            identity = declared_case_identity(item, native_id)
        except Exception as error:
            # Only our static reason codes are retained, not exception messages.
            reason = (error.args[0] if type(error) is TestIdentityError and len(error.args) == 1
                      and type(error.args[0]) is str and error.args[0] in IDENTITY_ERROR_CODES
                      else "test_identity_unavailable")
            errors.append(dict(native_id=native_id if type(native_id) is str else None, reason=reason))
        native_ids.append(native_id if type(native_id) is str else None)
        ids.append(identity)
    counts = Counter(identity for identity in ids if identity is not None)
    duplicates = {identity: count for identity, count in sorted(counts.items()) if count > 1}
    code = ("root_discovery_empty_or_duplicate_ids" if not cases or duplicates
            else "root_discovery_unresolved_identity" if errors else None)
    observation.update(status="rejected" if code else "valid", duplicate_ids=duplicates, error_code=code)
    if code:
        raise ValueError(code)
    return ids


def collect_unfiltered(root, *, observation=None):
    observation = {} if observation is None else observation
    observation.clear()
    observation.update(status="discovering", discovered_tests=None)
    loader = unittest.TestLoader()
    try:
        suite = loader.discover(str(Path(root) / "tests"), pattern="test*.py", top_level_dir=str(root))
    except BaseException:
        observation.update(status="failed_before_count", error_code="root_discovery_failed_before_count")
        raise
    observation["loader_error_count"] = len(loader.errors)
    ids = checked_case_ids(suite, observation=observation)
    # Loader errors stay in the suite as failing tests; they are not filtered out.
    return suite, ids


def discovery_summary(observation):
    """Serialize the observed denominator even if identity checking raised."""
    count = observation.get("discovered_tests")
    native_ids = observation.get("native_ids")
    identities = observation.get("canonical_ids")

    def fully_observed(values):
        return (values is not None and count is not None and len(values) == count
                and all(type(value) is str for value in values))

    def complete_hash(values):
        return source.digest(source.canonical(values)) if fully_observed(values) else None

    return dict(test_identity_version=TEST_IDENTITY_VERSION,
        discovery_status=observation.get("status", "not_completed"), discovered_tests=count,
        discovered_test_ids_sha256=complete_hash(identities), native_test_ids_sha256=complete_hash(native_ids),
        unique_test_id_count=len(set(identities)) if fully_observed(identities) else None,
        native_unique_test_id_count=len(set(native_ids)) if fully_observed(native_ids) else None,
        duplicate_test_ids=observation.get("duplicate_ids"), test_identity_errors=observation.get("identity_errors"),
        discovery_error_code=observation.get("error_code"), discovery_loader_error_count=observation.get("loader_error_count"))


def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_suite(suite, stream):
    return unittest.TextTestRunner(stream=stream, verbosity=2, failfast=False).run(suite)


def stable_binding(before, after, inputs_before, inputs_after, manifest_before, manifest_after):
    return (after == before and inputs_after == inputs_before
            and manifest_after is not None and manifest_after == manifest_before)


def main():
    if any(name in os.environ for name in KEY_NAMES):
        print('{"status":"not_started","error_code":"test_environment_key_names_present"}')
        return 2
    try:
        head = source._git(ROOT, "rev-parse", "HEAD").decode("ascii").strip()
        source.require(head == BASE, "regression_base_commit")
        before = source.verify_source(ROOT)
        inputs_before = verification_inputs(ROOT)
        started_ns = time.monotonic_ns()
        initial = dict(schema_version="behavior-offline-regression/2.0", status="running",
            batch_id=BATCH, pid=os.getpid(), started_at_utc=utc(), base_commit=head,
            source_commitment_sha256=before["commitment_sha256"],
            source_manifest_sha256=source.digest(source._regular(ROOT, source.MANIFEST)),
            verification_inputs_sha256=inputs_before["sha256"],
            driver_sha256=source.digest(Path(__file__).read_bytes()),
            discovery="unittest discover -s tests -t .", filter=None,
            root_full_regression=True, online_execution_authorized=False)
        with reserve_report(ROOT, DIRECTORY + "/started.json") as stream:
            json.dump(initial, stream, sort_keys=True)
    except Exception as error:
        code = error.code if type(error) is source.SourceIntegrityV2Error else "regression_preparation_failed"
        print(json.dumps(dict(status="not_started", error_code=code)))
        return 2

    result, ids, driver_error, after, inputs_after = None, [], None, None, None
    discovery_observation = {}
    manifest_after = None
    try:
        with reserve_report(ROOT, DIRECTORY + "/unittest.log") as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                try:
                    suite, ids = collect_unfiltered(ROOT, observation=discovery_observation)
                    result = run_suite(suite, log)
                except BaseException as error:
                    driver_error = type(error).__name__
                    import traceback
                    traceback.print_exc(file=log)
    except BaseException as error:
        driver_error = driver_error or type(error).__name__
    try:
        after = source.verify_source(ROOT, expected=before["commitment_sha256"])
        inputs_after = verification_inputs(ROOT)
        manifest_after = source.digest(source._regular(ROOT, source.MANIFEST))
    except Exception:
        driver_error = driver_error or "source_or_verification_inputs_changed"
    stable = stable_binding(before, after, inputs_before, inputs_after,
                            initial["source_manifest_sha256"], manifest_after)
    complete = result is not None and result.testsRun == len(ids)
    code = 0 if complete and result.wasSuccessful() and stable and driver_error is None else 1
    receipt = dict(initial, status="completed", finished_at_utc=utc(),
        elapsed_milliseconds=(time.monotonic_ns() - started_ns) // 1_000_000,
        **discovery_summary(discovery_observation),
        tests=None if result is None else result.testsRun,
        failures=None if result is None else len(result.failures),
        errors=None if result is None else len(result.errors),
        skips=None if result is None else len(result.skipped),
        expected_failures=None if result is None else len(result.expectedFailures),
        unexpected_successes=None if result is None else len(result.unexpectedSuccesses),
        source_stable=stable, complete_discovered_denominator=complete,
        source_after_commitment_sha256=None if after is None else after["commitment_sha256"],
        source_after_manifest_sha256=manifest_after,
        verification_inputs_after_sha256=None if inputs_after is None else inputs_after["sha256"],
        driver_error=driver_error, exit_code=code, full_regression_passed=code == 0,
        internal_online_acceptance_passed=False, status_t7_closed=False)
    try:
        with reserve_report(ROOT, DIRECTORY + "/receipt.json") as stream:
            json.dump(receipt, stream, sort_keys=True, ensure_ascii=True, indent=2)
            stream.write("\n")
    except Exception:
        print('{"status":"sealing_failed","full_regression_passed":false,"exit_code":1}')
        return 1
    print(json.dumps({key: receipt[key] for key in ("batch_id", "tests", "failures", "errors", "skips",
        "source_stable", "complete_discovered_denominator", "exit_code", "full_regression_passed")}))
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-unfiltered-offline", action="store_true", required=True)
    parser.parse_args()
    raise SystemExit(main())
