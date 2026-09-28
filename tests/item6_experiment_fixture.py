"""Isolated real source/claim/Adapter/ledger fixture; only OS locator and HTTP are replaced."""
from pathlib import Path
from datetime import timedelta
import asyncio
import copy
import functools
from contextlib import ExitStack, contextmanager
import ctypes
import hashlib
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import io
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BASE = "5605396e165fc5140eba54340d5a9c93f67540dc"
PYTHON = sys.executable
_temporary = []
RESULTS = []
LOCK_OBSERVATION = False  # Explicit single-test diagnostic; never inherited by live entrypoints.
CLAIM_MODES = {"claim_write": "write", "claim_fsync": "fsync", "claim_close": "close"}
CLAIM_ERROR_CODES = frozenset((
    "local_claim_write_unknown", "local_claim_close_unknown", "local_claim_failed",
    "local_claim_already_exists", "local_claim_file_invalid", "local_claim_store_invalid",
    "local_claim_store_changed", "local_claim_environment_mismatch", "local_claim_request_invalid",
    "local_claim_contract_invalid", "local_claim_receipt_unverifiable",
    "item6_approval_window", "item6_source_manifest_drift", "item6_entry_mode_mismatch",
    "item6_prepared_required", "item6_output_exists", "item6_execution_failed", "item6_offline_store_isolation",
))
STORE_CHECKS = ("exact_path_type", "absolute", "parent_matches_temp", "name_prefix",
                "resolved_identity", "not_symlink", "not_junction")
DIAGNOSTIC_ERROR_TYPES = {OSError: "OSError", PermissionError: "PermissionError",
    FileNotFoundError: "FileNotFoundError", RuntimeError: "RuntimeError", ValueError: "ValueError"}
NEW_TESTS = (
    "tests.test_item6_privacy_diagnostics",
    "tests.test_item6_refusal_isolation",
    "tests.test_item6_case_isolation",
    "tests.test_item6_experiment_authority", "tests.test_item6_experiment_session",
    "tests.test_item6_experiment_budget", "tests.test_item6_experiment_artifacts",
    "tests.test_item6_experiment_integration", "tests.test_internal_source_integrity_v3")
RELATED_TESTS = (
    "tests.test_phase6_providers", "tests.test_phase6_provider_completion_capture",
    "tests.test_provider_completion_surface_mapping", "tests.test_completion_telemetry_ledger",
    "tests.test_completion_timed_ledger", "tests.test_internal_source_integrity_v2",
    "tests.test_deepseek_completion_first_live_validation", "tests.test_kimi_k3_handshake",
    "tests.test_phase6_depth60")
OVERLAY = (
    "tests/test_item6_privacy_diagnostics.py",
    "tests/test_item6_refusal_isolation.py",
    "tests/test_item6_case_isolation.py",
    "src/researchops/model_providers.py", "src/researchops_completion_telemetry/surface_mapping.py",
    "src/researchops_external_closure/git_objects.py",
    "src/researchops_internal_telemetry/source_integrity_v3.py", "scripts/build_internal_source_integrity_v3.py",
    "scripts/verify_pre_v6_integrity.py", ".github/workflows/ci.yml", ".github/workflows/item6-experiment-bridge-offline.yml",
    "tests/internal_source_v2_historical_support.py", "tests/test_internal_source_integrity_v2.py",
    "tests/test_internal_source_integrity_v3.py", "tests/test_deepseek_completion_first_live_validation.py",
    "tests/test_kimi_k3_handshake.py", "tests/test_phase6_depth60.py", "tests/item6_experiment_fixture.py",
    "tests/test_item6_experiment_authority.py", "tests/test_item6_experiment_session.py", "tests/test_item6_experiment_budget.py",
    "tests/test_item6_experiment_artifacts.py", "tests/test_item6_experiment_integration.py")


def environment(root):
    env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT") if k in os.environ}
    env.update(PYTHONPATH=str(root / "src") + os.pathsep + str(root), PYTHONDONTWRITEBYTECODE="1",
               PYTHONUTF8="1", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    return env


def command(args, root):
    result = subprocess.run(args, cwd=root, env=environment(root), capture_output=True, text=True, encoding="utf-8", timeout=180)
    if result.returncode:
        raise RuntimeError("fixture_command_failed:" + str(args[:2]) + ":" + result.stderr[-2000:])
    return result.stdout


def allocate(prefix):
    # Retain fixture sources/stores after failures; never delete a claim to retry.
    path = Path(tempfile.mkdtemp(prefix=prefix))
    if prefix == "item6-test-store-":
        original = path
        try:
            path = canonical_test_store_path(original)
        except BaseException:
            _temporary.append(original)
            raise
    _temporary.append(path)
    return path


def canonical_test_store_path(path):
    """Canonicalize only a new fixture store; never repair the production locator."""
    def regular_directory(candidate, metadata):
        return (stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)
                and not getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                and not candidate.is_symlink() and not candidate.is_junction())

    if type(path) is not type(Path()) or not path.is_absolute() or not path.name.startswith("item6-test-store-"):
        raise ValueError("fixture_store_path_invalid")
    before = path.lstat()
    if not regular_directory(path, before): raise ValueError("fixture_store_link_or_kind")
    temp = Path(tempfile.gettempdir()).resolve(strict=True)
    if path.parent.resolve(strict=True) != temp: raise ValueError("fixture_store_parent")
    canonical = path.resolve(strict=True)
    if canonical.parent != temp or not canonical.name.startswith("item6-test-store-"):
        raise ValueError("fixture_store_canonical_parent")
    after, original_after = canonical.lstat(), path.lstat()
    identities = [(entry.st_dev, entry.st_ino) for entry in (before, after, original_after)]
    if not (before.st_ino and identities[0] == identities[1] == identities[2]
            and regular_directory(canonical, after) and regular_directory(path, original_after)
            and path.samefile(canonical) and canonical.resolve(strict=True) == canonical):
        raise ValueError("fixture_store_identity_changed")
    return canonical


class FixtureSourceParityError(ValueError):
    def __init__(self, code, differing_paths=()):
        self.code = code
        self.differing_paths = tuple(differing_paths)
        super().__init__(code)


def verify_fixture_source_parity(source_root, fixture_root):
    """Compare all selected bytes, not the potentially stale source manifest."""
    from researchops_internal_telemetry import source_integrity_v3 as source
    def snapshot(root):
        rows = []
        for name in source.source_files(root):
            body = source.v2._regular(root, name)
            rows.append(dict(path=name, bytes=len(body), sha256=source.digest(body)))
        workflows = {name: source.digest(source.v2._regular(root, name))
                     for name in source.SEPARATE_GIT_BINDINGS}
        return rows, workflows
    original, workflow_before = snapshot(source_root)
    derived, workflow_derived = snapshot(fixture_root)
    names = [row["path"] for row in original]
    copied_names = [row["path"] for row in derived]
    if names != copied_names:
        raise FixtureSourceParityError("fixture_selected_paths_mismatch", sorted(set(names) ^ set(copied_names)))
    differences = [left["path"] for left, right in zip(original, derived) if left != right]
    if differences:
        raise FixtureSourceParityError("fixture_selected_bytes_mismatch", differences)
    if workflow_before != workflow_derived:
        raise FixtureSourceParityError("fixture_separate_workflow_mismatch",
            [name for name in workflow_before if workflow_before[name] != workflow_derived[name]])
    checked = source.verify_source(fixture_root)
    if checked["files"] != derived:
        raise FixtureSourceParityError("fixture_manifest_projection_mismatch")
    if snapshot(source_root) != (original, workflow_before) or snapshot(fixture_root) != (derived, workflow_derived):
        raise FixtureSourceParityError("fixture_source_changed_during_verification")
    return dict(schema="item6-fixture-full-source-parity/1", selected_count=len(names),
        selected_rows_sha256=source.digest(source.canonical(original)),
        source_commitment_sha256=checked["commitment_sha256"], separately_bound_workflows=workflow_before,
        source_manifest_need_not_be_current=True, generated_fixture_manifest_verified=True,
        historical_fixture_rebased=False, provider_calls=0, real_store_touched=False)


def record_fixture_source_parity(source_root, fixture_root, name):
    value = verify_fixture_source_parity(source_root, fixture_root)
    directory = fixture_root / "output/item6-bridge-validation"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    return value


@functools.lru_cache(maxsize=1)
def seed():
    root = allocate("i6-seed-")
    command(["git", "clone", "--shared", "--no-checkout", str(ROOT), str(root)], ROOT)
    command(["git", "checkout", "--detach", BASE], root)
    names = list(OVERLAY)
    for folder in ("src/researchops_item6_experiment_v1", "evals/item6_experiment_bridge_v1", "evals/provider_completion_internal_source_v3"):
        names.extend(p.relative_to(ROOT).as_posix() for p in (ROOT / folder).rglob("*") if p.is_file()
                     and "__pycache__" not in p.parts and not p.name.startswith("source_manifest_v3.json"))
    for name in names:
        target = root / name; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    command([PYTHON, "-B", "-m", "tests.item6_experiment_fixture", "freeze-source"], root)
    record_fixture_source_parity(ROOT, root, "seed-source-parity.json")
    command(["git", "add", "--", *names, "evals/provider_completion_internal_source_v3/source_manifest_v3.json"], root)
    command(["git", "-c", "user.name=Item6 Fixture", "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=" + str(root / "no-hooks"),
             "commit", "--no-gpg-sign", "-m", "Synthetic offline Item6 fixture; never published"], root)
    commit = command(["git", "rev-parse", "HEAD"], root).strip()
    bundles = ROOT / "output/item6-bridge-validation/fixture-bundles"
    bundles.mkdir(parents=True, exist_ok=True)
    command(["git", "bundle", "create", str(bundles / (commit + ".bundle")), "HEAD", "^" + BASE], root)
    return root


def run_case(mode="normal"):
    root = allocate("i6-case-")
    command(["git", "clone", "--shared", str(seed()), str(root)], ROOT)
    # Verify the execution clone, too: cached seeds are not authority for a
    # later source tree. This runs before exercise/key/store/transport setup.
    record_fixture_source_parity(ROOT, root, "case-source-parity.json")
    env = environment(root)
    if LOCK_OBSERVATION and mode != "isolation_design":
        raise ValueError("lock_observation_single_mode_only")
    action = "exercise-lock-observed" if LOCK_OBSERVATION else "exercise"
    result = subprocess.run([PYTHON, "-B", "-m", "tests.item6_experiment_fixture", action, mode],
        cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=900)
    rows = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if not rows: raise AssertionError("fixture_missing_result:" + result.stderr[-1500:])
    value = json.loads(rows[-1]); value["process_exit_code"] = result.returncode
    value["fixture_root"] = str(root)
    value["stderr"] = result.stderr
    if "store_isolation_diagnostic" in value:
        value["store_isolation_diagnostic"] = project_store_observation(value["store_isolation_diagnostic"])
        emit_safe_diagnostic("ITEM6_STORE_DIAGNOSTIC", value["store_isolation_diagnostic"])
    if mode in CLAIM_MODES:
        diagnostic = claim_diagnostic(mode, value)
        from researchops_external_closure.io import scan_public_artifact_bytes
        encoded_diagnostic = json.dumps(diagnostic, ensure_ascii=True, sort_keys=True)
        scan_public_artifact_bytes((encoded_diagnostic.encode(),))
        value["claim_fault_diagnostic"] = diagnostic
        # Emitted before any original assertion; never includes stdout/stderr or exception text.
        print("ITEM6_CLAIM_DIAGNOSTIC " + encoded_diagnostic, flush=True)
    RESULTS.append(value)
    directory = ROOT / "output/item6-bridge-validation/probes"
    directory.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2)
    if "offline-fixture-key-item6" in encoded:
        encoded = json.dumps({"diagnostic_copy_redacted": True, "payload": json.loads(encoded.replace("offline-fixture-key-item6", "<synthetic-key-redacted>"))}, ensure_ascii=False, indent=2)
    (directory / (mode + "-" + uuid.uuid4().hex + ".json")).write_text(encoded, encoding="utf-8")
    return value


@functools.lru_cache(maxsize=None)
def isolation_case(mode="isolation_design"):
    return run_case(mode)


def historical_case(mode="ic11_tool_before_design"):
    """Original assertion on its actual fixed producer, without a new source overlay."""
    commit = "1e41012696519a180a5a4e59909b08ef29180494"
    root = allocate("i6-historical-")
    command(["git", "clone", "--shared", "--no-checkout", str(ROOT), str(root)], ROOT)
    command(["git", "checkout", "--detach", commit], root)
    actual = command(["git", "rev-parse", "HEAD"], root).strip()
    if actual != commit: raise ValueError("historical_fixture_identity")
    process = subprocess.run([PYTHON, "-B", "-m", "tests.item6_experiment_fixture", "exercise", mode],
        cwd=root, env=environment(root), capture_output=True, text=True, encoding="utf-8", timeout=900)
    rows = [line for line in process.stdout.splitlines() if line.startswith("{")]
    if not rows: raise ValueError("historical_fixture_missing_result")
    result = json.loads(rows[-1])
    result.update(process_exit_code=process.returncode, fixture_root=str(root), historical_commit=actual)
    RESULTS.append(dict(kind="historical_fixture", mode=mode, commit=actual, actual_exit_code=process.returncode))
    return result


def claim_error_observation(error):
    """Exact trusted exception classes, literal codes only; never stringify errors."""
    from researchops_completion_timing.local_claim import LocalClaimError
    from researchops_item6_experiment_v1.contract import ExperimentError
    trusted = {LocalClaimError: "LocalClaimError", ExperimentError: "ExperimentError"}
    error_type = type(error)
    name = trusted.get(error_type)
    code = getattr(error, "code", None) if error_type in trusted else None
    retained = type(code) is str and code in CLAIM_ERROR_CODES
    return dict(exception_type=name, code=code if retained else None,
                code_status="allowlisted" if retained else "not_allowlisted")


def project_store_observation(value):
    source = value.get("checks") if type(value) is dict else None
    source = source if type(source) is dict else {}
    checks = {}
    for name in STORE_CHECKS:
        row = source.get(name)
        row = row if type(row) is dict else {}
        status, observed, error = row.get("status"), row.get("value"), row.get("error_type")
        if type(status) is str and status == "observed" and type(observed) is bool:
            checks[name] = dict(status="observed", value=observed, error_type=None)
        elif type(status) is str and status == "error" and observed is None and type(error) is str and error in (*DIAGNOSTIC_ERROR_TYPES.values(), "other"):
            checks[name] = dict(status="error", value=None, error_type=error)
        else:
            checks[name] = dict(status="not_observed", value=None, error_type=None)
    return dict(schema="item6-store-isolation-observation/1", checks=checks,
        observation_scope="pre_admission_snapshot_not_gate_result", authority_granted=False,
        path_values_recorded=False, exception_text_recorded=False)


def observe_test_location(path):
    """Observe the seven predicates without replacing, calling or overriding the gate."""
    checks = {name: dict(status="not_observed", value=None, error_type=None) for name in STORE_CHECKS}
    exact = type(path) is type(Path())
    checks["exact_path_type"] = dict(status="observed", value=exact, error_type=None)
    if exact:
        operations = {
            "absolute": path.is_absolute,
            "parent_matches_temp": lambda: path.parent.resolve() == Path(tempfile.gettempdir()).resolve(),
            "name_prefix": lambda: path.name.startswith("item6-test-store-"),
            "resolved_identity": lambda: path.resolve() == path,
            "not_symlink": lambda: not path.is_symlink(),
            "not_junction": lambda: not path.is_junction(),
        }
        for name, operation in operations.items():
            try:
                observed = operation()
                if type(observed) is bool:
                    checks[name] = dict(status="observed", value=observed, error_type=None)
            except Exception as error:
                checks[name] = dict(status="error", value=None, error_type=DIAGNOSTIC_ERROR_TYPES.get(type(error), "other"))
    return project_store_observation(dict(checks=checks))


def emit_safe_diagnostic(marker, value):
    from researchops_external_closure.io import scan_public_artifact_bytes
    if marker not in {"ITEM6_STORE_DIAGNOSTIC", "ITEM6_CLAIM_RACE_DIAGNOSTIC"}:
        raise ValueError("diagnostic_marker")
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True)
    if len(encoded.encode()) > 16384: raise ValueError("diagnostic_size")
    scan_public_artifact_bytes((encoded.encode(),))
    print(marker + " " + encoded, flush=True)


def record_test_location(path):
    value = observe_test_location(path)
    emit_safe_diagnostic("ITEM6_STORE_DIAGNOSTIC", value)
    RESULTS.append(dict(kind="store_isolation_diagnostic", diagnostic=value))
    return value


def write_race_diagnostic(directory, index, stage, value):
    if index not in ("0", "1") or stage not in {"location", "failure"}:
        raise ValueError("race_diagnostic_identity")
    from researchops_external_closure.io import scan_public_artifact_bytes
    data = (json.dumps(value, ensure_ascii=True, sort_keys=True) + "\n").encode()
    if len(data) > 4096: raise ValueError("race_diagnostic_size")
    scan_public_artifact_bytes((data,))
    with (directory / ("diagnostic-" + index + "-" + stage + ".json")).open("xb") as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def claim_child_with_diagnostics(command_name, known, directory, index):
    # No returned receipt/code is replaced; the original child exception is re-raised.
    if command_name != "claim-only": raise ValueError("race_diagnostic_command")
    write_race_diagnostic(directory, index, "location", observe_test_location(known))
    try:
        return claim_fixture_child(command_name, known, directory, index)
    except BaseException as error:
        try:
            write_race_diagnostic(directory, index, "failure", claim_error_observation(error))
        except Exception as evidence_error:
            raise error from evidence_error
        raise


def race_children_diagnostic(processes, directory):
    from researchops_external_closure.io import read_regular_file_no_follow
    if len(processes) != 2: raise ValueError("race_process_count")
    rows = []
    for index, process in enumerate(processes):
        try:
            code = process.poll()
            status = "observed" if type(code) is int and -(2**31) <= code <= 2**32-1 else "still_running" if code is None else "invalid"
        except Exception:
            code, status = None, "observation_error"
        row = dict(index=index, actual_exit_code=code if status == "observed" else None, exit_status=status,
                   location=None, location_status="missing", failure_code=None, failure_code_status="missing")
        for stage in ("location", "failure"):
            try:
                payload = json.loads(read_regular_file_no_follow(directory / ("diagnostic-"+str(index)+"-"+stage+".json"), max_bytes=4096))
                if type(payload) is not dict: raise ValueError("race_diagnostic_shape")
                if stage == "location":
                    row.update(location=project_store_observation(payload), location_status="observed")
                else:
                    trusted = payload.get("exception_type") in ("LocalClaimError", "ExperimentError")
                    code_value = payload.get("code")
                    allowed = trusted and type(code_value) is str and code_value in CLAIM_ERROR_CODES and payload.get("code_status") == "allowlisted"
                    row.update(failure_code=code_value if allowed else None, failure_code_status="allowlisted" if allowed else "not_allowlisted")
            except FileNotFoundError:
                pass
            except Exception:
                row["location_status" if stage == "location" else "failure_code_status"] = "unavailable"
        rows.append(row)
    return dict(schema="item6-race-early-exit-diagnostic/1", children=rows,
                stdout_recorded=False, stderr_recorded=False, exception_text_recorded=False,
                cleanup_proved=False, processes_modified=False)


def record_race_early_exit(processes, directory):
    diagnostic = race_children_diagnostic(processes, directory)
    emit_safe_diagnostic("ITEM6_CLAIM_RACE_DIAGNOSTIC", diagnostic)
    RESULTS.append(dict(kind="claim_race_early_exit", diagnostic=diagnostic))


def claim_diagnostic(mode, value):
    """Bounded pure projection of existing observations, not replacement assertions."""
    if type(mode) is not str or mode not in CLAIM_MODES or type(value) is not dict:
        raise ValueError("claim_diagnostic_input")

    def field(mapping, key, kind, choices=()):
        if type(mapping) is not dict or key not in mapping:
            return dict(status="missing", value=None)
        item = mapping[key]
        if item is None:
            return dict(status="null", value=None)
        valid = ((kind == "bool" and type(item) is bool)
                 or (kind == "int" and type(item) is int and -(2**31) <= item <= 2**32-1)
                 or (kind == "enum" and type(item) is str and item in choices))
        return dict(status="observed" if valid else "redacted_invalid", value=item if valid else None)

    def length(key):
        if key not in value: return dict(status="missing", value=None)
        items = value[key]
        if items is None: return dict(status="null", value=None)
        valid = type(items) is list and len(items) <= 4096
        return dict(status="observed" if valid else "redacted_invalid", value=len(items) if valid else None)

    post = value.get("post_checks")
    fault = post.get("claim_fault") if type(post) is dict else None
    observations = {
        "process_exit_code": field(value, "process_exit_code", "int"),
        "claim_may_exist": field(value, "claim_may_exist", "bool"),
        "claim_files": field(value, "claim_files", "int"),
        "calls_count": length("calls"),
        "target": field(fault, "target", "enum", tuple(CLAIM_MODES.values())),
        "call_count": field(fault, "call_count", "int"),
    }
    hits = value.get("hits")
    hit_valid = type(hits) is list and len(hits) <= 4096
    observations["mode_in_hits"] = dict(status="observed" if hit_valid else "missing" if "hits" not in value else "null" if hits is None else "redacted_invalid",
        value=any(type(item) is str and item == mode for item in hits) if hit_valid else None)
    for key in ("claim_parent_matched", "module_os_isolated", "module_os_restored", "process_os_unchanged"):
        observations[key] = field(fault, key, "bool")
    expectations = {"process_exit_code": 2, "claim_may_exist": True, "claim_files": 1, "calls_count": 0,
                    "mode_in_hits": True, "target": CLAIM_MODES[mode], "call_count": 1,
                    "claim_parent_matched": True, "module_os_isolated": True, "module_os_restored": True,
                    "process_os_unchanged": True}
    checks = {}
    for key, expected in expectations.items():
        observed = observations[key]
        comparison = "at_least" if key == "call_count" else "equals"
        matches = (observed["value"] >= expected if key == "call_count" else observed["value"] == expected) if observed["status"] == "observed" else None
        checks[key] = dict(expected=expected, comparison=comparison, actual=observed, matches=matches)
    error = value.get("claim_error_observation")
    error = error if type(error) is dict else {}
    return dict(schema="item6-claim-assertion-diagnostic/1", mode=mode, checks=checks,
        result_error=field(value, "error", "enum", CLAIM_ERROR_CODES),
        exception_type=field(error, "exception_type", "enum", ("LocalClaimError", "ExperimentError")),
        exception_code=field(error, "code", "enum", CLAIM_ERROR_CODES),
        exception_code_status=field(error, "code_status", "enum", ("allowlisted", "not_allowlisted")),
        exception_text_recorded=False, exception_args_recorded=False, stderr_recorded=False,
        observation_scope="before_original_assertions", diagnostic_only=True)


def claim_diagnostics_for_report(results):
    # Reproject original records; do not trust a nested diagnostic supplied by a fixture.
    selected = [row for row in results if type(row) is dict and type(row.get("mode")) is str and row["mode"] in CLAIM_MODES]
    if len(selected) > 3: raise ValueError("claim_diagnostic_count")
    return [claim_diagnostic(row["mode"], row) for row in selected]


def public_validation_report(report, result):
    from services.agent_workflow_comparison_v1.controlled_publication_v1.public_artifacts import project
    from researchops_external_closure.io import scan_public_artifact_bytes
    derived = project(report)
    try: scan_public_artifact_bytes((json.dumps(derived, ensure_ascii=False).encode(),))
    except Exception:
        derived["log"] = "Raw diagnostic log retained locally; not exported because of privacy-pattern detection."
        derived["failure_test_ids"] = [str(test) for test, _ in result.failures + result.errors]
    return derived


@functools.lru_cache(maxsize=3)
def unfinished_case(mode):
    assert mode in {"cross_case_handle", "missing_usage", "cross_case_handle_after_one"}
    return run_case(mode)


def verify_unfinished(result):
    """Independent process, real failed-evidence reader; never treats it as a normal seal."""
    root = Path(result["fixture_root"])
    run = result["run"]
    assert run["archive_kind"] == "unfinished_failure"
    program = ("import json,sys; from researchops_item6_experiment_v1.failed_archive import verify_failed_archive; "
               "print(json.dumps(verify_failed_archive(sys.argv[1], expected_failure_sha256=sys.argv[2])))")
    child = subprocess.run([PYTHON, "-B", "-c", program, str(root / run["archive_namespace"]), run["failure_receipt_sha256"]],
        cwd=root, env=environment(root), capture_output=True, text=True, encoding="utf-8", timeout=900)
    assert child.returncode == 0, "unfinished_readback_exit:" + str(child.returncode)
    value = json.loads(child.stdout)
    value["actual_exit_code"] = child.returncode
    RESULTS.append(dict(kind="unfinished_independent_readback", result=value))
    return value


@functools.lru_cache(maxsize=1)
def normal(): return run_case("normal")


def verify_result(result):
    root = Path(result["fixture_root"])
    run = result["run"]
    process = subprocess.run([PYTHON, "-B", "-m", "researchops_item6_experiment_v1", "verify",
        "--archive", run["archive_namespace"], "--seal-sha256", run["seal_sha256"], "--finalization-sha256", run["finalization_sha256"]],
        cwd=root, env=environment(root), capture_output=True, text=True, encoding="utf-8", timeout=120)
    value = json.loads(process.stdout.splitlines()[-1]); value["actual_exit_code"] = process.returncode
    RESULTS.append({"kind": "independent_readback", "result": value})
    return value


def concurrent_claims():
    root = allocate("i6-race-")
    command(["git", "clone", "--shared", str(seed()), str(root)], ROOT)
    known = allocate("item6-test-store-")
    config = root / "output/claim-race"
    config.mkdir(parents=True)
    command([PYTHON, "-B", "-m", "tests.item6_experiment_fixture", "prepare-claim-fixture", str(known), str(config)], root)
    processes = [subprocess.Popen([PYTHON, "-B", "-m", "tests.item6_experiment_fixture", "claim-only", str(known), str(config), str(index)],
        cwd=root, env=environment(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8") for index in range(2)]
    deadline = time.monotonic() + 180
    while not all((config / ("ready-" + str(index))).exists() for index in range(2)):
        if time.monotonic() > deadline: raise AssertionError("claim race readiness timeout")
        if any(process.poll() is not None for process in processes):
            record_race_early_exit(processes, config)
            raise AssertionError("claim race child exited before barrier")
        time.sleep(0.05)
    (config / "go").write_text("go", encoding="utf-8")
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=180)
        value = json.loads(stdout.splitlines()[-1]); value["actual_exit_code"] = process.returncode
        results.append(value)
    rows = list((known / "ResearchOpsAgent/completion-claims-v1/claims").glob("*.json"))
    result = dict(kind="concurrent_claims", contenders=results, claim_files=len(rows), real_store_touched=False, provider_calls=0)
    RESULTS.append(result)
    return result


def claim_fixture_child(command_name, known, directory, index=None):
    from researchops_completion_timing import local_claim
    from researchops_item6_experiment_v1 import authority, contract as c
    with patch.object(local_claim, "_windows_local_app_data", return_value=known):
        if command_name == "prepare-claim-fixture":
            status = local_claim.provision_local_claim_store()
            freeze = c.build_freeze(mode="offline_test", environment_id=status["execution_environment_id"], pricing=dict(
                kind="synthetic_fixture", input_per_million="1", output_per_million="2", cost_limit="10",
                input_accounting="synthetic_bytes", provider_bill_hard_cap=False, evidence_sha256=None))
            instant = c.now()
            candidate = dict(scope=c.SCOPE, mode="offline_test", authorization_id="race-fixture-"+uuid.uuid4().hex,
                freeze_sha256=c.commit("freeze", freeze), environment_id=status["execution_environment_id"],
                not_before_utc=(instant-timedelta(seconds=5)).isoformat().replace("+00:00","Z"),
                expires_at_utc=(instant+timedelta(hours=1)).isoformat().replace("+00:00","Z"))
            approved = c.commit("approval-candidate",candidate)
            approval = dict(schema_version="item6-experiment-approval/1.0", candidate=candidate,
                observation=dict(kind="synthetic_approval_fixture", approved_digest=approved,
                    observed_at_utc=instant.isoformat().replace("+00:00","Z"), message_reference="synthetic-race-only"))
            (directory/"inputs.json").write_bytes(c.raw(dict(freeze=freeze,approval=approval,approved_digest=approved)))
            return 0
        data = c.decode((directory/"inputs.json").read_bytes())
        prepared = authority.validate_experiment(freeze_bytes=c.raw(data["freeze"]), approval_bytes=c.raw(data["approval"]),
            approved_digest=data["approved_digest"], expected_mode="offline_test")
        (directory/("ready-"+index)).write_text("ready",encoding="utf-8")
        deadline = time.monotonic()+120
        while not (directory/"go").exists():
            if time.monotonic()>deadline: raise AssertionError("claim barrier timeout")
            time.sleep(0.01)
        try:
            owner = authority._claim_experiment(prepared)
            value = dict(owner_created=True,run_id=owner.run_id,dispatches=0)
            owner.stop("item6_fixture_claim_only"); owner.close()
            print(json.dumps(value)); return 0
        except BaseException as error:
            print(json.dumps(dict(owner_created=False,error=c.safe_error(error),dispatches=0,claim_may_exist=bool(getattr(error,"claim_may_exist",False)))))
            return 2


def suite_child(run_id, names):
    """Executed in an actual synthetic committed checkout, never a mock source verifier."""
    from researchops_internal_telemetry import source_integrity_v3 as source
    before = source.verify_source(ROOT)
    directory = ROOT / "output/item6-bridge-validation" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    public = directory / "public"; public.mkdir()
    def guard(event, args):
        if event in {"socket.connect", "socket.sendto", "socket.sendmsg"}:
            address = args[1] if event != "socket.sendmsg" else args[-1]
            if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
                raise PermissionError("item6 offline suite forbids external sockets")
        if event == "socket.getaddrinfo" and args[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise PermissionError("item6 offline suite forbids external DNS")
    sys.addaudithook(guard)
    class Progress(io.StringIO):
        def write(self, value):
            result = super().write(value)
            sys.stdout.write(value); sys.stdout.flush()
            return result
    stream = Progress()
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    planned_tests = suite.countTestCases()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, failfast=True).run(suite)
    after = source.verify_source(ROOT)
    code = 0 if result.wasSuccessful() and before == after and result.testsRun == planned_tests else 1
    report = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors), skips=len(result.skipped),
        intended_exit_code=code, scope=list(names), source_before=before, source_after=after, inputs_stable=before == after,
        synthetic_commit=command(["git", "rev-parse", "HEAD"], ROOT).strip(), test_commit_not_project_publication=True,
        source_commitment_sha256=before["commitment_sha256"], provider_calls=0, real_store_touched=False,
        formal_comparison_result=False, log=stream.getvalue())
    report.update(planned_tests=planned_tests, test_plan_complete=result.testsRun == planned_tests, failfast=True)
    fixture_results = getattr(sys.modules.get("tests.item6_experiment_fixture"), "RESULTS", RESULTS)
    report["claim_fault_diagnostics"] = claim_diagnostics_for_report(fixture_results)
    store_diagnostics = [project_store_observation(row.get("diagnostic") if row.get("kind") == "store_isolation_diagnostic" else row["store_isolation_diagnostic"])
        for row in fixture_results if type(row) is dict and (row.get("kind") == "store_isolation_diagnostic" or "store_isolation_diagnostic" in row)]
    race_diagnostics = [row["diagnostic"] for row in fixture_results if type(row) is dict and row.get("kind") == "claim_race_early_exit"]
    if len(store_diagnostics) > 256 or len(race_diagnostics) > 1: raise ValueError("store_diagnostic_count")
    report.update(store_isolation_diagnostics=store_diagnostics, claim_race_diagnostics=race_diagnostics)
    (directory / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    derived = public_validation_report(report, result)
    (public / "validation.json").write_text(json.dumps(derived, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "fixture-results.json").write_text(json.dumps(fixture_results, ensure_ascii=False, indent=2).replace("offline-fixture-key-item6", "<synthetic-key-redacted>"), encoding="utf-8")
    print(json.dumps({k:report[k] for k in ("tests", "failures", "errors", "skips", "intended_exit_code", "inputs_stable")}), flush=True)
    return code


def suite_parent(run_id, *, remaining=False):
    from researchops_internal_telemetry import source_integrity_v3 as source
    import re
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", run_id) is None: raise ValueError("isolated_run_id")
    target = ROOT / "output/item6-bridge-validation" / run_id
    target.mkdir(parents=True, exist_ok=False)
    before = {name: source.digest((ROOT / name).read_bytes()) for name in source.source_files(ROOT)}
    fixture = seed()
    with (target / "process.log").open("x", encoding="utf-8") as log:
        process = subprocess.run([PYTHON, "-B", "-m", "tests.item6_experiment_fixture", "suite-remaining-child" if remaining else "suite-child", run_id],
            cwd=fixture, env=environment(fixture), stdout=log, stderr=subprocess.STDOUT, timeout=6000)
    source_dir = fixture / "output/item6-bridge-validation" / run_id
    if source_dir.exists():
        for path in source_dir.iterdir():
            if path.is_dir(): shutil.copytree(path, target / path.name)
            else: shutil.copyfile(path, target / path.name)
    after = {name: source.digest((ROOT / name).read_bytes()) for name in source.source_files(ROOT)}
    receipt = dict(actual_exit_code=process.returncode, source_before=before, source_after=after,
                   inputs_stable=before == after, synthetic_fixture_root=str(fixture), project_git_published=False)
    (target / "process-result.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"actual_exit_code": process.returncode, "inputs_stable": before == after}), flush=True)
    return process.returncode if before == after else 1


@contextmanager
def observe_test_store_locks(local_claim, known, *, enabled):
    """Pass native parameters/results unchanged; retain only bounded safe diagnostics."""
    if not enabled:
        yield None
        return
    temp = Path(tempfile.gettempdir()).resolve(strict=True)
    if (ROOT.resolve().parent != temp or not ROOT.name.startswith("i6-case-")
            or known.parent != temp or not known.name.startswith("item6-test-store-")
            or known.resolve(strict=True) != known or known.is_symlink() or known.is_junction()):
        raise ValueError("lock_observation_temporary_scope")
    directory = ROOT / "output/item6-bridge-validation/directory-lock-observation"
    directory.mkdir(parents=True, exist_ok=False)
    factory = local_claim._kernel
    invalid = ctypes.c_void_p(-1).value
    active, components, failures = {}, {}, []
    state = dict(schema="item6-test-store-lock-observation/1", stopped=False,
        create_calls=0, close_calls=0, close_failures=0, evidence_io_failed=False,
        first_failure=None, directory=directory.relative_to(ROOT).as_posix(),
        provider_calls=0, real_store_touched=False, native_parameters_unchanged=True)
    def write(name, value):
        data = (json.dumps(value, sort_keys=True) + "\n").encode()
        try:
            with (directory / name).open("xb") as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
        except BaseException:
            state["stopped"] = True; state["evidence_io_failed"] = True
            raise
    def safe_component(path):
        candidate = Path(path)
        if candidate not in known.parents and candidate != known and known not in candidate.parents:
            state["stopped"] = True
            raise ValueError("lock_observation_outside_temporary_chain")
        identity = hashlib.sha256(os.path.normcase(str(candidate)).encode()).hexdigest()
        if identity not in components:
            if len(components) >= 32:
                state["stopped"] = True
                raise ValueError("lock_observation_component_limit")
            components[identity] = dict(path_sha256=identity, path_chars=len(str(candidate)),
                component_index=len(candidate.parts)-1,
                role="test_store_component" if candidate == known or known in candidate.parents else "ancestor",
                create_calls=0, create_successes=0, close_successes=0)
        return components[identity]
    def stage():
        allowed = {"provision_local_claim_store", "local_claim_store_status",
                   "read_reserved_local_claim", "_reserve_local_claim_receipt"}
        frame = sys._getframe(1)
        for _ in range(12):
            if frame is None: break
            if frame.f_code.co_name in allowed and frame.f_globals.get("__name__") == local_claim.__name__:
                return frame.f_code.co_name
            frame = frame.f_back
        return "not_identified"
    class ObservedKernel:
        def __init__(self, native): self.native = native
        def GetDriveTypeW(self, *args): return self.native.GetDriveTypeW(*args)
        def CreateFileW(self, path, access, sharing, security, creation, flags, template):
            if state["stopped"]: raise RuntimeError("lock_observation_stopped")
            if (access, sharing, security, creation, flags, template) != (1, 3, None, 3, 0x02200000, None):
                state["stopped"] = True
                raise ValueError("lock_observation_native_parameters")
            component = safe_component(path)
            state["create_calls"] += 1; component["create_calls"] += 1
            handle = self.native.CreateFileW(path, access, sharing, security, creation, flags, template)
            os_error = ctypes.get_last_error()  # Capture immediately; no intervening native call.
            if handle not in (None, invalid):
                component["create_successes"] += 1
                active[handle] = dict(component=component, native=self.native, close_attempted=False)
            else:
                row = dict(operation="CreateFileW", call_index=state["create_calls"], stage=stage(),
                    **{key: component[key] for key in ("path_sha256", "path_chars", "component_index", "role")},
                    desired_access=access, share_mode=sharing, flags=flags, winerror=os_error,
                    observed_success=False, exception_body_recorded=False)
                if state["first_failure"] is None: state["first_failure"] = row
                if len(failures) >= 32:
                    state["stopped"] = True
                    raise RuntimeError("lock_observation_failure_limit")
                failures.append(row)
                write("native-failure-%02d.json" % len(failures), row)
            return handle
        def CloseHandle(self, handle):
            entry = active[handle]
            entry["close_attempted"] = True
            result = self.native.CloseHandle(handle)
            os_error = ctypes.get_last_error()
            state["close_calls"] += 1
            if result:
                entry["component"]["close_successes"] += 1
                del active[handle]
            else:
                state["stopped"] = True; state["close_failures"] += 1
                row = dict(operation="CloseHandle", path_sha256=entry["component"]["path_sha256"],
                    winerror=os_error, observed_success=False, close_retried=False)
                failures.append(row)
                if not state["evidence_io_failed"]: write("native-failure-%02d.json" % len(failures), row)
            return result
    write("started.json", dict(schema=state["schema"], fixture_module_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        production_module_sha256=hashlib.sha256(Path(local_claim.__file__).read_bytes()).hexdigest(),
        native_access=1, native_sharing=3, native_flags=0x02200000, full_path_values_recorded=False,
        real_store_touched=False, network_authorized=False))
    with patch.object(local_claim, "_kernel", side_effect=lambda: ObservedKernel(factory())):
        try:
            yield state
        finally:
            # Unknown/failed close is never retried. Only reclaim a successful
            # open that an evidence exception prevented returning to its caller.
            for handle, entry in list(active.items()):
                if not entry["close_attempted"]:
                    try:
                        ObservedKernel(entry["native"]).CloseHandle(handle)
                    except BaseException as exc:
                        state["stopped"] = True
                        state.setdefault("cleanup_exception_types", []).append(type(exc).__name__)
            state.update(remaining_handles=len(active), components=list(components.values()),
                         failures=failures, observation_complete=not state["evidence_io_failed"])
            write("result.json", state)


async def exercise(mode, *, observe_lock=False):
    logging.disable(logging.CRITICAL)
    from agents import set_tracing_disabled
    set_tracing_disabled(True)
    from researchops_completion_timing import local_claim
    from researchops_item6_experiment_v1 import contract as c, runner, session, authority, observations, case_isolation
    from services.agent_workflow_comparison_v1.deepseek_flash_v1.wire import ResponsesFixture
    import httpx2
    known = allocate("item6-test-store-")
    if observe_lock and mode != "isolation_design": raise ValueError("lock_observation_single_mode_only")
    scripts = json.loads((ROOT / "services/agent_workflow_comparison_v1/controlled_comparison_v1/fixtures/development/mock_responses.json").read_text(encoding="utf-8"))
    calls, hits, responders = [], [], {}
    mixed_modes = {"mixed_before", "mixed_tool_audit_failure"}
    mixed_observation = {"origin": "synthetic_mocktransport_only", "injected_output": None,
                         "followup_input": None}
    os.environ["DEEPSEEK_API_KEY"] = "offline-fixture-key-item6"
    faults = {"http401": {"http_status": 401}, "http429": {"http_status": 429}, "http503": {"http_status": 503},
              "timeout": {"timeout": True}, "cancel": {"cancel": True}, "missing_usage": {"missing_usage": True},
              "usage_boolean": {"usage_boolean": True}, "usage_overrun": {"usage_overrun": True},
              "cache_overrun": {"cache_overrun": True}, "truncated": {"truncated": True},
              "privacy": {"privacy_text": True}, "reasoning": {"reasoning_output": True},
              "duplicate_call": {"duplicate_call_id": True}, "bad_tool": {"unknown_tool": True},
              "bad_arguments": {"invalid_arguments": True}, "oversize": {"oversized_response": True}}
    if mode == "after_unknown_retry": faults[mode] = {"timeout": True}
    if mode == "free_expression":
        scripts["IC-01"]["steps"][-1]["text"] = "The source contains a small mass; see the aggregate package."
    text_variants = {"null_text": None, "empty_text": "", "whitespace_text": " \t\n　",
                     "path_text": "C:/Users/example/private.txt", "email_text": "sample@example.invalid",
                     "traceback_text": "Traceback: synthetic frame", "authorization_text": "Authorization: synthetic"}
    if mode in text_variants:
        scripts["IC-01"]["steps"][-1]["text"] = text_variants[mode]
    async def handler(request):
        body = json.loads(request.content)
        task = json.loads(body["input"][0]["content"])
        tid = task["task_id"]
        calls.append({"task_id": tid, "model": body["model"], "bytes": len(request.content)})
        if tid not in responders: responders[tid] = ResponsesFixture(scripts[tid], faults.get(mode, {}) if tid == "IC-01" else {})
        if mode in mixed_modes and tid == "IC-01" and mixed_observation["injected_output"] is not None:
            # Synthetic request items only; no headers, credentials or real response body.
            mixed_observation["followup_input"] = copy.deepcopy(body["input"])
            hits.append("mixed_followup_request_observed")
        if mode in faults and tid == "IC-01": hits.append(mode)
        if mode in text_variants and tid == "IC-01": hits.append(mode)
        response = await responders[tid].handle(request)
        if mode.startswith("privacy_diag_") and tid == "IC-01":
            target = 2 if mode == "privacy_diag_second" else 1
            if responders[tid].count == target:
                data = response.json()
                if mode == "privacy_diag_args":
                    data["output"] = [{"type": "function_call", "id": "fc_privacy", "call_id": "call_privacy",
                        "name": "inspect_sources", "arguments": json.dumps({"scope_id": "Authorization"}), "status": "completed"}]
                else:
                    text = "sk-SyntheticFixtureOnly123456" if mode == "privacy_diag_public" else "Authorization"
                    data["output"] = [{"type": "message", "id": "msg_privacy", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text}]}]
                hits.append(mode + "_response_injected")
                response = httpx2.Response(200, json=data)
        refusal_mode = mode.startswith("refusal_isolation_")
        isolation_target = ((refusal_mode and tid in {"IC-14", "IC-15", "IC-16"}) or (mode.startswith("isolation_") and
            (tid in {"IC-11", "IC-13"} if mode in {"isolation_design", "isolation_cross_case_error"}
             else tid == ("IC-14" if mode == "isolation_refusal" else "IC-11"))))
        if isolation_target:
            assert responders[tid].count == 1
            data = response.json()
            tool, arguments = "inspect_sources", {"scope_id": task["scope_id"]}
            if mode == "isolation_scope": arguments = {"scope_id": "outside-frozen-scope"}
            if mode == "isolation_catalog": tool, arguments = "read_aggregate", {"bundle_id": "aggregate-01"}
            if mode == "refusal_isolation_scope": arguments = {"scope_id": "outside-frozen-scope"}
            data["output"] = [{"type": "function_call", "id": "fc_isolation_" + tid, "call_id": "call_isolation_" + tid,
                               "name": tool, "arguments": json.dumps(arguments), "status": "completed"}]
            hits.append("isolation_response_" + tid)
            response = httpx2.Response(200, json=data)
        if mode == "ic11_tool_before_design" and tid == "IC-11":
            # Reconstruct the retained action plan, not a historical raw response.
            assert responders[tid].count == 1
            assert task["design_requests"] == []
            data = response.json()
            data["output"] = [{"type": "function_call", "id": "fc_ic11_design",
                "call_id": "call_ic11_design", "name": "inspect_sources",
                "arguments": json.dumps({"scope_id": task["scope_id"]}), "status": "completed"}]
            hits.append("ic11_tool_before_design_response_injected")
            response = httpx2.Response(200, json=data)
        if mode in mixed_modes and tid == "IC-01" and responders[tid].count == 1:
            data = response.json()
            assert len(data["output"]) == 1 and data["output"][0]["type"] == "function_call"
            data["output"].insert(0, {"type": "message", "id": "item6_mixed_companion",
                "role": "assistant", "status": "completed", "content": [{"type": "output_text",
                "text": "我先读取合成聚合结果。", "annotations": []}]})
            mixed_observation["injected_output"] = copy.deepcopy(data["output"])
            hits.extend((mode, "mixed_response_injected"))
            response = httpx2.Response(200, json=data)
        if tid == "IC-01" and mode in {"function_incomplete", "function_status_missing", "function_status_null"}:
            data = response.json()
            for item in data["output"]:
                if item["type"] == "function_call":
                    if mode == "function_status_missing": item.pop("status", None)
                    else: item["status"] = "incomplete" if mode == "function_incomplete" else None
                    hits.append(mode)
            response = httpx2.Response(200, json=data)
        if tid == "IC-01" and mode == "duplicate_call":
            data = response.json()
            if data["output"] and data["output"][0]["type"] == "function_call":
                data["output"].append(copy.deepcopy(data["output"][0]))
                hits.append("duplicate_in_single_response")
                response = httpx2.Response(200, json=data)
        if tid == "IC-01" and mode in {"missing_status", "null_status", "unknown_status"}:
            hits.append(mode); data = response.json()
            if mode == "missing_status": data.pop("status")
            else: data["status"] = None if mode == "null_status" else "future_state"
            response = httpx2.Response(200, json=data)
        if tid == "IC-01" and mode == "source_drift":
            hits.append(mode)
            path = ROOT / "src/researchops_item6_experiment_v1/budget.py"
            path.write_bytes(path.read_bytes() + b"\n# isolated live source mutation\n")
        return response
    real_network_attempts = []
    def no_network(*a, **kw):
        real_network_attempts.append("blocked"); raise AssertionError("real network forbidden")
    with observe_test_store_locks(local_claim, known, enabled=observe_lock) as lock_observation, \
         patch.object(local_claim, "_windows_local_app_data", return_value=known), \
         patch.object(session, "_native_transport", side_effect=lambda mode: httpx2.MockTransport(handler)), \
         patch("socket.socket.connect", side_effect=no_network), patch("socket.getaddrinfo", side_effect=no_network), \
         patch("socket.socket.sendto", side_effect=no_network):
        status = local_claim.provision_local_claim_store()
        pricing = dict(kind="synthetic_fixture", input_per_million="1", output_per_million="2", cost_limit="10",
                       input_accounting="synthetic_bytes", provider_bill_hard_cap=False, evidence_sha256=None)
        freeze = c.build_freeze(mode="offline_test", environment_id=status["execution_environment_id"], pricing=pricing)
        if mode == "manifest_hash_mismatch":
            freeze["source_manifest_sha256"] = "1" * 64
            hits.append(mode)
        instant = c.now()
        candidate = dict(scope=c.SCOPE, mode="offline_test", authorization_id="item6-fixture-" + uuid.uuid4().hex,
            freeze_sha256=c.commit("freeze", freeze), environment_id=status["execution_environment_id"],
            not_before_utc=(instant-timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
            expires_at_utc=(instant+timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        if mode == "expired": candidate["expires_at_utc"] = (instant-timedelta(seconds=1)).isoformat().replace("+00:00", "Z"); hits.append(mode)
        approved = c.commit("approval-candidate", candidate)
        approval = dict(schema_version="item6-experiment-approval/1.0", candidate=candidate,
            observation=dict(kind="synthetic_approval_fixture", approved_digest=approved,
                observed_at_utc=instant.isoformat().replace("+00:00", "Z"), message_reference="synthetic-fixture-only"))
        kwargs = dict(freeze_bytes=c.raw(freeze), approval_bytes=c.raw(approval), approved_digest=approved)
        if mode == "noncanonical_documents":
            hits.append(mode)
            kwargs["freeze_bytes"] = json.dumps(freeze, ensure_ascii=False, indent=2).encode()
        result = None
        error = None
        claim_may_exist = False
        admission_observations = None
        partial_observations = None
        primary_stop_reason = None
        factories = []
        post_checks = {}
        extra = ExitStack()
        if mode in {"old_freeze", "previous_isolation_freeze", "isolation_policy_drift"}:
            if mode == "old_freeze":
                freeze["schema_version"] = "item6-experiment-freeze/1.0"
                freeze.pop("case_rejection_policy_revision"); freeze.pop("case_rejection_policy_sha256")
            elif mode == "previous_isolation_freeze":
                freeze["schema_version"] = "item6-experiment-freeze/1.1"
                freeze["case_rejection_policy_revision"] = "item6-case-rejection-isolation/1.0"
                freeze["case_rejection_policy_sha256"] = c.digest(c.read(c.DIRECTORY + "/case_rejection_policy_v1.json"))
            else: freeze["case_rejection_policy_sha256"] = "1" * 64
            # Keep the synthetic approval coherent, so rejection is attributable
            # to protocol/policy admission rather than an unrelated hash mismatch.
            candidate["freeze_sha256"] = c.commit("freeze", freeze)
            approved = c.commit("approval-candidate", candidate)
            approval["candidate"] = candidate
            approval["observation"]["approved_digest"] = approved
            kwargs.update(freeze_bytes=c.raw(freeze), approval_bytes=c.raw(approval), approved_digest=approved)
            hits.append(mode)
        if mode.startswith(("isolation_", "refusal_isolation_")):
            original_read = observations.Case._read_tool
            def counted_read(self, tool, arguments):
                if self.path == "agent" and self.task["task_id"] in {"IC-11", "IC-13", "IC-14", "IC-15", "IC-16"}:
                    hits.append("target_tool_read_" + self.task["task_id"])
                return original_read(self, tool, arguments)
            extra.enter_context(patch.object(observations.Case, "_read_tool", new=counted_read))
        audit_points = {"reject": case_isolation.REJECT, "close": case_isolation.CLOSE, "seal": case_isolation.SEALED}
        if mode in {"privacy_diag_io_before", "privacy_diag_io_after"}:
            from researchops_item6_experiment_v1 import privacy_diagnostics
            privacy_append = session.AuditLedger.append_event
            def fail_privacy_append(self, run_id, event_type, payload, **kw):
                if event_type == privacy_diagnostics.EVENT:
                    hits.append(mode + "_append_hit")
                    if mode.endswith("after"):
                        privacy_append(self, run_id, event_type, payload, **kw)
                        hits.append("privacy_diagnostic_committed_before_error")
                    raise OSError("synthetic diagnostic write failure")
                return privacy_append(self, run_id, event_type, payload, **kw)
            extra.enter_context(patch.object(session.AuditLedger, "append_event", new=fail_privacy_append))
        if mode == "refusal_isolation_seal_failure":
            original_append = session.AuditLedger.append_event
            def fail_refusal_seal(self, run_id, event_type, payload, **kw):
                if event_type == case_isolation.SEALED and payload.get("case_run_id", "").endswith("-agent-IC-14"):
                    hits.append(mode)
                    raise OSError("synthetic refusal seal failure")
                return original_append(self, run_id, event_type, payload, **kw)
            extra.enter_context(patch.object(session.AuditLedger, "append_event", new=fail_refusal_seal))
        for label, event in audit_points.items():
            if mode in {"isolation_audit_before_" + label, "isolation_audit_after_" + label}:
                original_append = session.AuditLedger.append_event
                def injected_append(self, run_id, event_type, payload, _event=event, **kw):
                    if event_type == _event and payload.get("case_run_id", "").endswith("-agent-IC-11"):
                        hits.append(mode)
                        if "_after_" in mode:
                            original_append(self, run_id, event_type, payload, **kw)
                            hits.append("original_append_committed_before_failure")
                        raise OSError("synthetic isolation audit failure")
                    return original_append(self, run_id, event_type, payload, **kw)
                extra.enter_context(patch.object(session.AuditLedger, "append_event", new=injected_append))
        if mode in {"isolation_capture_subclass", "isolation_capture_deep_cause",
                    "isolation_capture_context", "isolation_capture_pid"}:
            original_capture = case_isolation.capture
            def reject_foreign_capture(case, error):
                if case.task["task_id"] != "IC-11":
                    return original_capture(case, error)
                from agents.exceptions import UserError
                assert type(error) is UserError and type(error.__cause__) is c.ExperimentError
                original_error = error.__cause__
                assert case_isolation._rejections[case]["error"] is original_error
                hits.append("isolation_registered_original_verified")
                hits.append(mode)
                if mode == "isolation_capture_pid":
                    # Assign only the isolation module's reference: never patch
                    # global os.getpid or the authority/store process identity.
                    native_os = case_isolation.os
                    class ProcessProxy:
                        def getpid(self):
                            hits.append("isolation_local_pid_proxy_hit")
                            return native_os.getpid() + 1
                        def __getattr__(self, name):
                            return getattr(native_os, name)
                    try:
                        with patch.object(case_isolation, "os", new=ProcessProxy()):
                            return original_capture(case, error)
                    finally:
                        post_checks["isolation_os_reference_restored"] = case_isolation.os is native_os
                if mode == "isolation_capture_subclass":
                    class DerivedUserError(UserError):
                        pass
                    counterfeit = DerivedUserError("synthetic subclass")
                    counterfeit.__cause__ = original_error
                elif mode == "isolation_capture_deep_cause":
                    counterfeit = UserError("synthetic deep wrapper")
                    counterfeit.__cause__ = error
                else:
                    counterfeit = UserError("synthetic context wrapper")
                    counterfeit.__cause__ = None
                    counterfeit.__context__ = original_error
                return original_capture(case, counterfeit)
            extra.enter_context(patch.object(case_isolation, "capture", new=reject_foreign_capture))
        if mode == "isolation_tool_after_reject":
            original_close = session._ExperimentTransport.aclose
            injected = []
            async def tool_during_context_exit(self):
                await original_close(self)
                case = self.session.factory.current
                if case is None or case.task["task_id"] != "IC-11" or injected:
                    return
                assert case.record["case_rejection"] is not None
                assert case_isolation._rejections[case]["captured"]
                injected.append(True)
                hits.append(mode)
                hits.append("original_transport_closed")
                try:
                    await case.call("inspect_sources", {"scope_id": case.task["scope_id"]})
                except c.ExperimentError as error:
                    post_checks["isolation_extra_tool_error"] = error.code
                    hits.append("isolation_extra_tool_rejected")
                    raise
                raise AssertionError("post-rejection tool unexpectedly executed")
            extra.enter_context(patch.object(session._ExperimentTransport, "aclose", new=tool_during_context_exit))
        if mode == "isolation_open_before_sealed":
            original_append = case_isolation._append
            def open_during_sealing(case, event_type, payload):
                if event_type != case_isolation.SEALED or case.task["task_id"] != "IC-11":
                    return original_append(case, event_type, payload)
                factory = case.factory
                assert factory.current is None and factory.active is None
                assert case.record["case_lifecycle"]["closed_event_hash"] is not None
                assert case.record["case_rejection"]["sealed_event_hash"] is None
                assert case_isolation._factory(factory)["pending"] is case
                next_item = factory.freeze["business_plan"][factory.case_index]
                next_task = next(task for task in c.decode(c.read(c.TASKS))["tasks"]
                                 if task["task_id"] == next_item["task_id"])
                next_case = observations.Case(factory, next_task, next_item["path_kind"])
                def count_next_starts():
                    return sum(event["event_type"] == case_isolation.START
                        and event["safe_payload"].get("case_run_id") == next_case.record["run_id"]
                        for event in factory.ledger.export_run(factory.owner.run_id)["events"])
                before = count_next_starts()
                assert before == 0
                hits.append(mode)
                try:
                    factory.open_case(next_case)
                except c.ExperimentError as error:
                    post_checks["isolation_next_case_probe"] = dict(
                        task_id=next_item["task_id"], path_kind=next_item["path_kind"],
                        started_before=before, started_after=count_next_starts(), error_code=error.code)
                    hits.append("isolation_next_case_open_rejected")
                    raise
                raise AssertionError("next case admitted before durable isolation seal")
            extra.enter_context(patch.object(case_isolation, "_append", new=open_during_sealing))
        if mode == "isolation_provider_close_timeout":
            # Keep both original five-second timeout layers. Only the isolated
            # MockTransport delegate's close is made to remain pending.
            original_delegate_close = session._CheckedDelegate.aclose
            injected = []
            async def pending_fake_delegate_close(self):
                await original_delegate_close(self)
                case = self.session.factory.current
                if case is None or case.task["task_id"] != "IC-11" or injected:
                    return
                assert case.record["case_rejection"] is not None
                assert case_isolation._rejections[case]["captured"]
                assert type(self.delegate) is httpx2.MockTransport
                injected.append(True)
                from researchops.model_providers import _DEEPSEEK_POST_REQUEST_CLEANUP_TIMEOUT_SECONDS
                assert _DEEPSEEK_POST_REQUEST_CLEANUP_TIMEOUT_SECONDS == 5
                import time
                began = time.monotonic()
                hits.append("original_fake_delegate_close_returned")
                hits.append(mode)
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    post_checks["isolation_cleanup_timeout"] = dict(
                        elapsed_seconds=time.monotonic() - began,
                        original_timeout_seconds=_DEEPSEEK_POST_REQUEST_CLEANUP_TIMEOUT_SECONDS,
                        awaited_real_pending_close=True, wait_cancelled=True, isolated_mock_delegate=True)
                    hits.append("isolation_close_wait_cancelled")
                    raise
                raise AssertionError("pending synthetic close unexpectedly returned")
            extra.enter_context(patch.object(session._CheckedDelegate, "aclose", new=pending_fake_delegate_close))
        if mode in {"isolation_capture_clone", "isolation_repeat_capture"}:
            original_capture = case_isolation.capture
            def injected_capture(case, error):
                if case.task["task_id"] == "IC-11":
                    hits.append(mode)
                    if mode == "isolation_capture_clone":
                        from agents.exceptions import UserError
                        counterfeit = UserError("synthetic wrapper")
                        counterfeit.__cause__ = c.ExperimentError("tool_before_design_or_refusal")
                        return original_capture(case, counterfeit)
                    original_capture(case, error)
                return original_capture(case, error)
            extra.enter_context(patch.object(case_isolation, "capture", new=injected_capture))
        if mode == "isolation_cross_case_error":
            original_capture = case_isolation.capture
            earlier_error = []
            def cross_case_capture(case, error):
                if case.task["task_id"] == "IC-11":
                    earlier_error.append(error.__cause__)
                if case.task["task_id"] == "IC-13":
                    from agents.exceptions import UserError
                    assert len(earlier_error) == 1
                    hits.append(mode)
                    wrong = UserError("synthetic cross-case wrapper")
                    wrong.__cause__ = earlier_error[0]
                    return original_capture(case, wrong)
                return original_capture(case, error)
            extra.enter_context(patch.object(case_isolation, "capture", new=cross_case_capture))
        if mode in {"isolation_provider_close", "isolation_provider_cancel"}:
            # Invoke the real transport close first, then inject the exit failure.
            original_close = session._ExperimentTransport.aclose
            async def injected_close(self):
                await original_close(self)
                case = self.session.factory.current
                if case is not None and case.task["task_id"] == "IC-11":
                    hits.append("original_transport_closed")
                    hits.append(mode)
                    if mode == "isolation_provider_cancel": raise asyncio.CancelledError()
                    raise OSError("synthetic resource close failure")
            extra.enter_context(patch.object(session._ExperimentTransport, "aclose", new=injected_close))
        if mode in {"isolation_forged_reconciliation", "isolation_missing_indices"}:
            from researchops_completion_telemetry.capture import RuntimeDenominatorTracker
            from dataclasses import replace
            original_seal = RuntimeDenominatorTracker.seal_case
            def injected_seal(self, case_id, **kw):
                if case_id == freeze["model_case_handles"]["IC-11"] and mode == "isolation_missing_indices":
                    hits.append(mode); kw["sdk_request_usage_indices_by_response"] = None
                returned = original_seal(self, case_id, **kw)
                if case_id == freeze["model_case_handles"]["IC-11"] and mode == "isolation_forged_reconciliation":
                    hits.append(mode); return replace(returned)
                return returned
            extra.enter_context(patch.object(RuntimeDenominatorTracker, "seal_case", new=injected_seal))
        if mode == "isolation_send_after_reject":
            original_register = case_isolation.register_rejection
            def injected_register(case, error, tool, arguments):
                result = original_register(case, error, tool, arguments)
                if result:
                    hits.append(mode)
                    case.factory.active.begin_attempt()
                    raise AssertionError("post-rejection send unexpectedly admitted")
                return result
            extra.enter_context(patch.object(case_isolation, "register_rejection", new=injected_register))
        if mode in {"isolation_source_drift", "isolation_expired"}:
            original_register = case_isolation.register_rejection
            def drift_after_registered(case, error, tool, arguments):
                result = original_register(case, error, tool, arguments)
                if result:
                    hits.append(mode)
                    if mode == "isolation_source_drift":
                        path = ROOT / "src/researchops_item6_experiment_v1/budget.py"
                        path.write_bytes(path.read_bytes() + b"\n# isolated post-rejection drift\n")
                    else:
                        extra.enter_context(patch.object(c, "now", return_value=c.utc(candidate["expires_at_utc"]) + timedelta(seconds=1)))
                return result
            extra.enter_context(patch.object(case_isolation, "register_rejection", new=drift_after_registered))
        if mode == "isolation_later_unfinished":
            from dataclasses import replace
            original_finalize = session.ExperimentSession._finalize
            def later_foreign_handle(self, handle, *args, **kw):
                if self.case_id == freeze["model_case_handles"]["IC-14"]:
                    hits.append(mode)
                    wrong = freeze["model_case_handles"]["IC-01"]
                    return original_finalize(self, replace(handle, case_id=wrong), *args, **kw)
                return original_finalize(self, handle, *args, **kw)
            extra.enter_context(patch.object(session.ExperimentSession, "_finalize", new=later_foreign_handle))
        if mode in {"factory_freeze_drift", "budget_policy_drift"}:
            original_open = session.ExperimentFactory.open_case
            def mutate_policy(self, case):
                hits.append(mode)
                if mode == "factory_freeze_drift": self.freeze["budget"]["output_per_request"] += 1
                else: self.budget.limits["output_per_request"] += 1
                return original_open(self, case)
            extra.enter_context(patch.object(session.ExperimentFactory, "open_case", new=mutate_policy))
        if mode in text_variants or mode in {"tool_missing_facts", "expiry_after_scoring", "expiry_after_export", "expiry_after_terminal"}:
            original_open_case = session.ExperimentFactory.open_case
            def stop_after_boundary_case(self, case):
                if self.case_index == 2:
                    hits.append("declared_stop_after_boundary_case")
                    raise c.ExperimentError("fixture_boundary_case_stop")
                return original_open_case(self, case)
            extra.enter_context(patch.object(session.ExperimentFactory, "open_case", new=stop_after_boundary_case))
        if mode in {"after_unknown_retry", "factory_clone"}:
            original_init = session.ExperimentFactory.__init__
            def observe_factory(self, *args):
                original_init(self, *args); factories.append(self)
                if mode == "factory_clone":
                    hits.append(mode)
                    self.owner.assert_factory(copy.copy(self))
            extra.enter_context(patch.object(session.ExperimentFactory, "__init__", new=observe_factory))
        if mode in {"cross_case_handle", "cross_case_handle_after_one"}:
            from dataclasses import replace
            original_finalize = session.ExperimentSession._finalize
            def foreign_handle(self, handle, *args, **kw):
                if mode == "cross_case_handle_after_one" and len(self.event_commitment()["started"]) == 1:
                    return original_finalize(self, handle, *args, **kw)
                hits.append(mode)
                wrong = next(x for x in self.factory.freeze["model_case_handles"].values() if x != self.case_id)
                return original_finalize(self, replace(handle, case_id=wrong), *args, **kw)
            extra.enter_context(patch.object(session.ExperimentSession, "_finalize", new=foreign_handle))
        if mode in {"claim_write", "claim_fsync", "claim_close"}:
            original_write = local_claim._write_once
            def damaged_write(path, data):
                if path.parent.name != "claims": return original_write(path, data)
                target = {"claim_write": "write", "claim_fsync": "fsync", "claim_close": "close"}[mode]
                real_os = local_claim.os
                original_operation = getattr(real_os, target)
                observation = dict(target=target, call_count=0, claim_parent_matched=True,
                    module_os_isolated=False, module_os_restored=False, process_os_unchanged=False)
                post_checks["claim_fault"] = observation
                def fail_operation(*args):
                    observation["call_count"] += 1
                    if mode not in hits: hits.append(mode)
                    if mode == "claim_close": original_operation(*args)
                    elif mode == "claim_write": original_operation(args[0], args[1][:8])
                    raise OSError("synthetic claim operation failure")
                class FaultOS:
                    def __getattr__(self, name): return getattr(real_os, name)
                fault_os = FaultOS()
                setattr(fault_os, target, fail_operation)
                try:
                    with patch.object(local_claim, "os", fault_os):
                        observation["module_os_isolated"] = local_claim.os is fault_os and os is real_os
                        return original_write(path, data)
                finally:
                    observation["module_os_restored"] = local_claim.os is real_os
                    observation["process_os_unchanged"] = getattr(os, target) is original_operation
            extra.enter_context(patch.object(local_claim, "_write_once", side_effect=damaged_write))
        if mode in {"audit_start", "audit_intent"}:
            if mode == "audit_start":
                original_write = session.ExperimentSession._write
                def fail_start(self, event_type, payload, **kw):
                    if event_type == "model_request_started":
                        hits.append(mode); raise OSError("synthetic audit failure")
                    return original_write(self, event_type, payload, **kw)
                extra.enter_context(patch.object(session.ExperimentSession, "_write", new=fail_start))
            else:
                original_append = session.AuditLedger.append_event
                def fail_intent(self, run_id, event_type, payload, **kw):
                    if event_type == "item6_send_intent_v1":
                        hits.append(mode); raise OSError("synthetic intent failure")
                    return original_append(self, run_id, event_type, payload, **kw)
                extra.enter_context(patch.object(session.AuditLedger, "append_event", new=fail_intent))
        if mode in {"audit_tool_start", "audit_tool_finish", "mixed_tool_audit_failure"}:
            original_tool_append = session.AuditLedger.append_event
            original_observed_read = observations.Case._read_tool
            target_event = "item6_tool_started_v1" if mode == "audit_tool_start" else "item6_tool_finished_v1"
            def fail_tool_audit(self, run_id, event_type, payload, **kw):
                if event_type == target_event and payload.get("case_run_id", "").endswith("-agent-IC-01"):
                    hits.append(mode)
                    if mode == "mixed_tool_audit_failure": hits.append("mixed_tool_finish_audit_failure")
                    raise OSError("synthetic tool audit failure")
                return original_tool_append(self, run_id, event_type, payload, **kw)
            def observe_tool_read(self, tool, arguments):
                if self.path == "agent" and self.task["task_id"] == "IC-01": hits.append("agent_tool_read_observed")
                return original_observed_read(self, tool, arguments)
            extra.enter_context(patch.object(session.AuditLedger, "append_event", new=fail_tool_audit))
            extra.enter_context(patch.object(observations.Case, "_read_tool", new=observe_tool_read))
        if mode in {"tool_failure", "tool_timeout", "tool_missing_facts"}:
            original_read = observations.Case._read_tool
            def fail_tool(self, tool, arguments):
                if self.task["task_id"] == "IC-01" and self.path == "agent":
                    hits.append(mode)
                    if mode == "tool_timeout": time.sleep(self.factory.freeze["budget"]["tool_seconds"] + 0.1)
                    elif mode == "tool_failure": raise OSError("synthetic read failure")
                    else:
                        damaged = original_read(self, tool, arguments)
                        damaged.pop("facts")
                        return damaged
                return original_read(self, tool, arguments)
            extra.enter_context(patch.object(observations.Case, "_read_tool", new=fail_tool))
        if mode == "wrong_origin":
            hits.append(mode)
            extra.enter_context(patch.object(runner.DeepSeekProvider, "base_url", "https://gateway.invalid"))
        if mode == "offline_native_delegate":
            hits.append(mode)
            extra.enter_context(patch.object(session, "_native_transport", side_effect=lambda mode: httpx2.AsyncHTTPTransport()))
        if mode == "expiry_after_source":
            original_source = c.check_source
            original_before = session.ExperimentFactory.before_send
            armed = [False]
            def before(self, *args):
                armed[0] = True
                return original_before(self, *args)
            def checked_then_expired(freeze):
                answer = original_source(freeze)
                if armed[0]:
                    hits.append(mode)
                    extra.enter_context(patch.object(c, "now", return_value=instant+timedelta(hours=2)))
                    armed[0] = False
                return answer
            extra.enter_context(patch.object(session.ExperimentFactory, "before_send", new=before))
            extra.enter_context(patch.object(c, "check_source", side_effect=checked_then_expired))
        if mode in {"expiry_after_scoring", "expiry_after_export", "expiry_after_seal", "expiry_after_terminal"}:
            from researchops_item6_experiment_v1 import finalization
            def expire():
                hits.append(mode)
                extra.enter_context(patch.object(c, "now", return_value=instant+timedelta(hours=2)))
            if mode == "expiry_after_scoring":
                original_score = runner.score_business
                def score_then_expire(*args):
                    result = original_score(*args); expire(); return result
                extra.enter_context(patch.object(runner, "score_business", side_effect=score_then_expire))
            elif mode == "expiry_after_export":
                original_export = session.AuditLedger.export_run
                def export_then_expire(self, *args, **kw):
                    result = original_export(self, *args, **kw); expire(); return result
                extra.enter_context(patch.object(session.AuditLedger, "export_run", new=export_then_expire))
            elif mode == "expiry_after_seal":
                original_archive = runner.write_archive
                def seal_then_expire(*args):
                    result = original_archive(*args); expire(); return result
                extra.enter_context(patch.object(runner, "write_archive", side_effect=seal_then_expire))
            else:
                original_check = finalization.FinalizationDeadline.check
                def terminal_then_expire(self, stage):
                    if stage == "terminal_after_write": expire()
                    return original_check(self, stage)
                extra.enter_context(patch.object(finalization.FinalizationDeadline, "check", new=terminal_then_expire))
        failed_archive = None
        store_diagnostic = observe_test_location(known)
        try:
            if mode == "offline_to_live":
                hits.append(mode); result = await runner.run_live(**kwargs)
            elif mode == "forged_prepared":
                hits.append(mode); authority._claim_experiment({"receipt": "success"})
            else:
                result = await runner._run_offline_test(**kwargs)
                if mode == "after_unknown_retry":
                    before_retry = len(calls)
                    try: factories[0].sessions[0].begin_attempt()
                    except BaseException as exc: post_checks["retry_error"] = c.safe_error(exc)
                    else: raise AssertionError("unknown outcome retry accepted")
                    post_checks["calls_before_retry"] = before_retry
                    post_checks["calls_after_retry"] = len(calls)
                if mode == "duplicate":
                    hits.append(mode)
                    try: await runner._run_offline_test(**kwargs)
                    except BaseException as exc: error = c.safe_error(exc)
                    else: raise AssertionError("duplicate accepted")
        except BaseException as exc:
            error = c.safe_error(exc)
            if mode in CLAIM_MODES:
                post_checks["claim_error_observation"] = claim_error_observation(exc)
            claim_may_exist = bool(getattr(exc, "claim_may_exist", False))
            admission_observations = getattr(exc, "admission_observations", None)
            partial_observations = getattr(exc, "partial_observations", None)
            primary_stop_reason = getattr(exc, "primary_stop_reason", None)
            failed_archive = getattr(exc, "archive_namespace", None)
        finally:
            extra.close()
        files = list((known / "ResearchOpsAgent/completion-claims-v1/claims").glob("*.json"))
        value = dict(mode=mode, run=result, error=error, calls=calls, hits=hits, claim_files=len(files), claim_may_exist=claim_may_exist, post_checks=post_checks,
            admission_observations=admission_observations, partial_observations=partial_observations, primary_stop_reason=primary_stop_reason,
            real_store_touched=False, provider_calls=0, network_attempts=len(real_network_attempts),
            synthetic_approval=True, synthetic_commit=True)
        if mode in mixed_modes: value["mixed_response_observation"] = mixed_observation
        if mode in CLAIM_MODES:
            value["claim_error_observation"] = post_checks.get("claim_error_observation")
        value["store_isolation_diagnostic"] = store_diagnostic
        if observe_lock: value["directory_lock_observation"] = lock_observation
        if result is not None:
            if "archive_kind" in result:
                assert result["archive_kind"] == "unfinished_failure"
                failure = c.decode((ROOT / result["archive_namespace"] / "failed-experiment.json").read_bytes(), c.policy()["archive_bytes"])
                artifact = failure["artifact"]
                value["attempt_failures"] = failure["attempt_failures"]
            else:
                artifact = c.decode((ROOT / result["archive_namespace"] / "experiment.json").read_bytes(), c.policy()["archive_bytes"])
            value["artifact"] = artifact
        if failed_archive is not None:
            directory = ROOT / failed_archive
            value["failed_archive_namespace"] = failed_archive
            value["finalization_failure"] = c.decode((directory / "finalization-failure.json").read_bytes())
            value["candidate_status"] = c.decode((directory / "experiment.json").read_bytes(), c.policy()["archive_bytes"])["status"] if (directory / "experiment.json").exists() else None
        return value


if __name__ == "__main__":
    if sys.argv[1] in {"prepare-claim-fixture", "claim-only"}:
        child = claim_child_with_diagnostics if sys.argv[1] == "claim-only" else claim_fixture_child
        raise SystemExit(child(sys.argv[1],Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4] if len(sys.argv)>4 else None))
    elif sys.argv[1] == "suite":
        raise SystemExit(suite_parent(sys.argv[2]))
    elif sys.argv[1] == "suite-remaining":
        raise SystemExit(suite_parent(sys.argv[2], remaining=True))
    elif sys.argv[1] == "suite-child":
        raise SystemExit(suite_child(sys.argv[2], NEW_TESTS + RELATED_TESTS))
    elif sys.argv[1] == "suite-remaining-child":
        raise SystemExit(suite_child(sys.argv[2], ("tests.test_internal_source_integrity_v3",) + RELATED_TESTS))
    elif sys.argv[1] == "freeze-source":
        from researchops_internal_telemetry import source_integrity_v3 as v3
        report = v3.write_manifest_exclusive(ROOT)
        manifest = json.loads((ROOT / v3.MANIFEST).read_bytes())
        workflow = ROOT / ".github/workflows/ci.yml"
        text = workflow.read_text(encoding="utf-8").replace("ITEM6_V3_COMMITMENT", manifest["commitment_sha256"]).replace("ITEM6_V3_MANIFEST_SHA256", report["sha256"])
        workflow.write_text(text, encoding="utf-8", newline="\n")
        print(json.dumps(report))
    else:
        try:
            result = asyncio.run(exercise(sys.argv[2], observe_lock=sys.argv[1] == "exercise-lock-observed")); print(json.dumps(result, ensure_ascii=True))
            raise SystemExit(result["run"]["actual_exit_code"] if result["error"] is None and result["run"] else 2)
        except SystemExit: raise
        except BaseException as error:
            print(json.dumps({"fixture_error": type(error).__name__, "code": getattr(error, "code", None)}))
            raise SystemExit(3)
