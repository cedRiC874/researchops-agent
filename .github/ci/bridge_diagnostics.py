"""CI-only, post-mortem projection around the unchanged bridge suite parent.

No retries, background observer, fixture patches, new network capability or cleanup.
Text markers are untrusted hints, never proof of executed tests. A job/VM hard kill
can leave only start.json; it cannot be made into a sealed result by this wrapper.
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from collections import deque

FIXTURE = "tests/item6_experiment_fixture.py"
MODULE = "tests.item6_experiment_fixture"
WRAPPER = ".github/ci/bridge_diagnostics.py"
WORKFLOW = ".github/workflows/item6-experiment-bridge-offline.yml"
TEST = "tests/test_bridge_ci_diagnostics.py"
MAX_FILE = 4 * 1024 * 1024
MAX_LOG = 8 * 1024 * 1024
MAX_LINE = 4096
MAX_ROWS = 4096
PARITY_CODES = frozenset({
    "fixture_selected_paths_mismatch", "fixture_selected_bytes_mismatch",
    "fixture_separate_workflow_mismatch", "fixture_manifest_projection_mismatch",
    "fixture_source_changed_during_verification",
})
TIMING_PREFIX = b"ITEM6_BRIDGE_TIMING "
TIMING_SCHEMA = "item6-bridge-segment-timing/1"
TIMING_MAX_SEQUENCE = 16384
TIMING_MAX_NS = 86400000000000
TIMING_KEYS = (
    "schema", "sequence", "kind", "monotonic_offset_ns", "test_ordinal", "test_id",
    "span", "stage", "mode", "duration_ns", "outcome", "exception_type", "safe_marker",
)
TIMING_MODES = frozenset({
    "expiry_after_scoring", "expiry_after_export", "expiry_after_seal", "expiry_after_terminal",
})
TIMING_STAGES = frozenset({
    "suite_pre_source", "suite_load", "suite_run", "suite_post_source", "case_prepare",
    "case_source_parity", "case_exercise", "case_result", "independent_readback",
})
TIMING_MARKER_KEYS = (
    "mode", "point", "injection_hit", "process_exit_code", "verifier_exit_code",
    "failure_stage", "seal_present", "partial_observation_count", "calls_count",
    "error_code", "verifier_error_code",
)


class DiagnosticError(Exception):
    """Only fixed codes raised here are public; exception text is never copied."""


def require(condition, code):
    if not condition:
        raise DiagnosticError(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run_name(value):
    require(type(value) is str and re.fullmatch(r"CI-[0-9]{1,20}-[0-9]{1,6}", value), "run_id")
    return value


def checked_path(root, relative):
    """No links/reparse points in either the root ancestry or relative path."""
    root = Path(root).absolute()
    parts = Path(relative).parts
    require(parts and not Path(relative).is_absolute() and not Path(relative).drive and
            all(p not in {".", ".."} for p in parts), "path")
    target = root.joinpath(*parts)
    for path in reversed((target, *target.parents)):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400), "linked_path")
    return target


def read_bounded(root, relative, limit=MAX_FILE, *, tail=False):
    path = checked_path(root, relative)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode), "file_type")
        require(before.st_nlink == 1, "linked_file")
        require(not (getattr(before, "st_file_attributes", 0) & 0x400), "linked_file")
        offset = max(0, before.st_size - limit) if tail else 0
        require(tail or before.st_size <= limit, "file_size")
        stream.seek(offset)
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    checked_path(root, relative)
    require(stat.S_ISREG(current.st_mode) and current.st_nlink == 1, "file_type")
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) ==
            (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns), "file_changed")
    require(len(raw) <= limit, "file_grew")
    return raw, offset, before.st_size


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)


def write_once(root, relative, record):
    raw = (json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n").encode()
    require(len(raw) <= MAX_FILE, "record_size")
    path = checked_path(root, relative)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def static_inventory(root):
    """AST only: do not import/discover test modules or execute their methods."""
    raw = read_bounded(root, FIXTURE)[0]
    tree = ast.parse(raw)
    groups = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"NEW_TESTS", "RELATED_TESTS"}:
                    require(target.id not in groups, "duplicate_group")
                    groups[target.id] = ast.literal_eval(node.value)
    require(set(groups) == {"NEW_TESTS", "RELATED_TESTS"}, "test_groups")
    modules = []
    for values in groups.values():
        require(type(values) in (list, tuple) and 0 < len(values) <= 64, "test_groups")
        for name in values:
            require(type(name) is str and re.fullmatch(r"tests\.test_[a-z0-9_]+", name), "test_module")
            require(name not in modules, "duplicate_module")
            modules.append(name)
    files = [WRAPPER, WORKFLOW, TEST, FIXTURE, "tests/__init__.py"]
    identifiers = set()
    for name in modules:
        relative = name.replace(".", "/") + ".py"
        files.append(relative)
        parsed = ast.parse(read_bounded(root, relative)[0])
        for cls in parsed.body:
            if isinstance(cls, ast.ClassDef):
                for method in cls.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name.startswith("test_"):
                        identifier = name + "." + cls.name + "." + method.name
                        require(re.fullmatch(r"[A-Za-z0-9_.]{1,256}", identifier), "test_id")
                        identifiers.add(identifier)
    require(len(identifiers) <= MAX_ROWS, "test_id_count")
    return sorted(files), frozenset(identifiers)


def bindings(root, names):
    return {name: digest(read_bounded(root, name)[0]) for name in names}


def log_hints(raw, identifiers, *, offset=0, total_bytes=None):
    """Bounded advisory projection; even a matching marker can have been forged."""
    require(type(raw) is bytes and len(raw) <= MAX_LOG, "log_size")
    if offset:
        # A tail starts mid-line and possibly mid-UTF8 character; discard it.
        raw = raw.partition(b"\n")[2]
    hints, ignored, oversized = [], 0, 0
    # Bounded memory even for millions of tiny newline-only records. The byte
    # cap also bounds the single-line temporary allocation.
    lines, seen = deque(maxlen=MAX_ROWS), 0
    for line in io.BytesIO(raw):
        seen += 1
        lines.append(line.rstrip(b"\n"))
    row_cut = seen > MAX_ROWS
    for line in lines:
        if not line:
            continue
        if len(line) > MAX_LINE:
            oversized += 1
            continue
        try:
            text = line.rstrip(b"\r").decode("utf-8", errors="strict")
        except UnicodeError:
            ignored += 1
            continue
        match = re.fullmatch(r"(test_[A-Za-z0-9_]+) \(([A-Za-z0-9_.]+)\) \.\.\. (.*)", text)
        if not match:
            ignored += 1
            continue
        method, owner, result = match.groups()
        identifier = owner if owner.endswith("." + method) else owner + "." + method
        if identifier not in identifiers:
            ignored += 1
            continue
        state = {"": "start_hint", "ok": "ok_hint", "FAIL": "fail_hint", "ERROR": "error_hint",
                 "expected failure": "expected_failure_hint", "unexpected success": "unexpected_success_hint"}.get(result)
        if result.startswith("skipped "):
            state = "skip_hint"  # Never retain the reason.
        if state is None:
            ignored += 1
            continue
        hints.append({"test_id": identifier, "marker": state})
    counts = {name: sum(row["marker"] == name for row in hints) for name in
              ("start_hint", "ok_hint", "fail_hint", "error_hint", "skip_hint", "expected_failure_hint", "unexpected_success_hint")}
    return {"kind": "untrusted_log_hints", "trusted_execution_counts": False,
            "cannot_prove_current_test_or_completion": True, "static_allowlist_may_be_incomplete": True,
            "observed_bytes": len(raw), "total_file_bytes": total_bytes, "tail_offset": offset,
            "truncated": bool(offset or row_cut), "ignored_lines": ignored, "oversized_lines": oversized,
            "marker_counts_not_test_counts": counts, "last_markers": hints[-20:]}


def timing_hints(raw, identifiers, *, offset=0, total_bytes=None):
    """Strict, bounded text hints; neither source nor claimed timing is authenticated."""
    require(type(raw) is bytes and len(raw) <= MAX_LOG, "timing_log_size")
    require(type(offset) is int and offset >= 0, "timing_log_offset")
    require(total_bytes is None or (type(total_bytes) is int and total_bytes >= 0), "timing_log_total")
    if offset:
        raw = raw.partition(b"\n")[2]

    def integer(value, low, high, *, nullable=False):
        return (nullable and value is None) or (type(value) is int and low <= value <= high)

    def choice(value, allowed, *, nullable=False):
        return (nullable and value is None) or (type(value) is str and value in allowed)

    def project(line):
        value = decode(line[len(TIMING_PREFIX):])
        require(type(value) is dict and set(value) == set(TIMING_KEYS), "timing_fields")
        require(value["schema"] == TIMING_SCHEMA, "timing_schema")
        require(integer(value["sequence"], 1, TIMING_MAX_SEQUENCE), "timing_sequence")
        require(integer(value["monotonic_offset_ns"], 0, TIMING_MAX_NS), "timing_offset")
        require(integer(value["duration_ns"], 0, TIMING_MAX_NS, nullable=True), "timing_duration")
        require(value["duration_ns"] is None or value["duration_ns"] <= value["monotonic_offset_ns"],
                "timing_duration_offset")
        require(integer(value["test_ordinal"], 1, MAX_ROWS, nullable=True), "timing_test_ordinal")
        require(integer(value["span"], 1, TIMING_MAX_SEQUENCE, nullable=True), "timing_span")
        identifier = value["test_id"]
        require(identifier is None or (type(identifier) is str and
                re.fullmatch(r"[A-Za-z0-9_.]{1,256}", identifier) and identifier in identifiers), "timing_test_id")
        require(choice(value["kind"], {"test_start", "test_end", "segment_start", "segment_end",
                                      "finalization_marker"}), "timing_kind")
        require(choice(value["stage"], TIMING_STAGES, nullable=True), "timing_stage")
        require(choice(value["mode"], TIMING_MODES | {"other_fixture_mode"}, nullable=True), "timing_mode")
        require(choice(value["outcome"], {"started", "returned", "raised", "callback_complete", "observed"}),
                "timing_outcome")
        require(choice(value["exception_type"], {"TimeoutExpired", "OSError", "PermissionError",
                "FileNotFoundError", "KeyboardInterrupt", "SystemExit", "FixtureSourceParityError", "other"},
                nullable=True), "timing_exception")
        kind = value["kind"]
        if kind in {"test_start", "test_end"}:
            require(value["test_ordinal"] is not None and value["stage"] is None and value["span"] is None,
                    "timing_test_context")
            require(value["outcome"] == ("started" if kind == "test_start" else "callback_complete"),
                    "timing_test_outcome")
            require((value["duration_ns"] is None) == (kind == "test_start"), "timing_test_duration")
        elif kind in {"segment_start", "segment_end"}:
            require(value["stage"] is not None and value["span"] is not None, "timing_segment_context")
            require(value["outcome"] == "started" if kind == "segment_start" else
                    value["outcome"] in {"returned", "raised"}, "timing_segment_outcome")
            require((value["duration_ns"] is None) == (kind == "segment_start"), "timing_segment_duration")
        else:
            require(value["outcome"] == "observed" and value["stage"] is None and
                    value["span"] is None and value["duration_ns"] is None, "timing_marker_context")
        require(value["exception_type"] is None or (kind == "segment_end" and value["outcome"] == "raised"),
                "timing_exception_context")
        marker = value["safe_marker"]
        if kind != "finalization_marker":
            require(marker is None, "timing_marker_kind")
        else:
            require(type(marker) is dict and set(marker) == set(TIMING_MARKER_KEYS), "timing_marker_fields")
            require(choice(marker["mode"], TIMING_MODES) and marker["mode"] == value["mode"], "timing_marker_mode")
            require(choice(marker["point"], {"after_run_case", "after_verify"}), "timing_marker_point")
            require(all(marker[key] is None or type(marker[key]) is bool
                        for key in ("injection_hit", "seal_present")), "timing_marker_boolean")
            require(all(integer(marker[key], -1, 4096, nullable=True)
                        for key in ("process_exit_code", "verifier_exit_code")), "timing_marker_exit")
            require(all(integer(marker[key], 0, 4096, nullable=True)
                        for key in ("partial_observation_count", "calls_count")), "timing_marker_count")
            require(choice(marker["failure_stage"], {"start", "scoring_after", "export_after", "seal_after",
                                                    "terminal_after_write"}, nullable=True), "timing_failure_stage")
            require(choice(marker["error_code"], {"item6_finalization_deadline"}, nullable=True), "timing_error_code")
            require(choice(marker["verifier_error_code"], {"item6_finalization_failed"}, nullable=True),
                    "timing_verifier_error_code")
        projected = {key: value[key] for key in TIMING_KEYS}
        projected["safe_marker"] = None if marker is None else {key: marker[key] for key in TIMING_MARKER_KEYS}
        return projected

    # Keep only complete, size-bounded candidate lines, with an independent
    # candidate cap. Matching text is still forgeable, even after validation.
    lines = deque(maxlen=TIMING_MAX_SEQUENCE)
    ignored, oversized, incomplete, candidates = 0, 0, 0, 0
    for line in io.BytesIO(raw):
        if not line.endswith(b"\n"):
            incomplete += 1
            continue
        line = line[:-1].removesuffix(b"\r")
        if len(line) > MAX_LINE:
            oversized += 1
        elif line.startswith(TIMING_PREFIX):
            candidates += 1
            lines.append(line)
        elif line:
            ignored += 1
    events, markers = deque(maxlen=50), {}
    open_tests, open_segments = {}, {}
    accepted, rejected, out_of_order, gaps = 0, 0, 0, 0
    duplicate_starts, unmatched_ends, mismatched_ends, duration_mismatches = 0, 0, 0, 0
    previous_sequence, previous_offset = 0, 0
    for line in lines:
        try:
            event = project(line)
        except (DiagnosticError, ValueError, UnicodeError, RecursionError):
            rejected += 1
            continue
        sequence, tick = event["sequence"], event["monotonic_offset_ns"]
        if sequence <= previous_sequence or tick < previous_offset:
            out_of_order += 1
            continue
        gaps += sequence - previous_sequence - 1
        previous_sequence, previous_offset = sequence, tick
        accepted += 1
        events.append(event)
        kind = event["kind"]
        context = (event["test_ordinal"], event["test_id"], event["stage"], event["mode"])
        if kind in {"test_start", "test_end", "segment_start", "segment_end"}:
            pending = open_tests if kind.startswith("test_") else open_segments
            key = context[:2] if kind.startswith("test_") else event["span"]
            if kind.endswith("_start"):
                duplicate_starts += int(key in pending)
                pending[key] = (tick, context)
            else:
                start = pending.get(key)
                if start is None:
                    unmatched_ends += 1
                elif start[1] != context:
                    mismatched_ends += 1
                else:
                    del pending[key]
                    if event["duration_ns"] is not None and event["duration_ns"] != tick - start[0]:
                        duration_mismatches += 1
        marker = event["safe_marker"]
        if kind == "finalization_marker" and marker is not None:
            markers[(marker["mode"], marker["point"])] = {
                "sequence": sequence, "test_ordinal": event["test_ordinal"],
                "test_id": event["test_id"], "safe_marker": marker,
            }
    row_cut = candidates > TIMING_MAX_SEQUENCE
    return {
        "kind": "untrusted_segment_timing_hints", "source_schema": TIMING_SCHEMA,
        "independently_attested": False, "marker_source_authenticated": False,
        "trusted_execution_counts": False, "cannot_prove_current_test_or_completion": True,
        "missing_end_is_not_timeout_or_failure_proof": True, "static_allowlist_may_be_incomplete": True,
        "observed_bytes": len(raw), "total_file_bytes": total_bytes, "tail_offset": offset,
        "truncated": bool(offset or row_cut or incomplete), "event_tail_truncated": accepted > 50,
        "ignored_lines": ignored, "oversized_lines": oversized, "incomplete_lines": incomplete,
        "rejected_markers": rejected, "out_of_order_markers": out_of_order,
        "accepted_marker_count_not_execution_count": accepted,
        "sequence_gap_count": gaps, "duplicate_start_count": duplicate_starts,
        "unmatched_end_count": unmatched_ends, "mismatched_end_context_count": mismatched_ends,
        "duration_mismatch_count": duration_mismatches,
        "missing_end_count": len(open_tests) + len(open_segments),
        "ordering_and_pairing_are_untrusted_hints": True,
        "last_events": list(events),
        "finalization_marker_summary": [markers[key] for key in sorted(markers)],
    }


def safe_exception(exc, parent, run_id, parity_type):
    kind = {subprocess.TimeoutExpired: "TimeoutExpired", FileNotFoundError: "FileNotFoundError",
            PermissionError: "PermissionError", OSError: "OSError", ValueError: "ValueError",
            RuntimeError: "RuntimeError", KeyboardInterrupt: "KeyboardInterrupt", SystemExit: "SystemExit"}.get(type(exc), "unclassified")
    result = {"type": kind, "code": None, "scope": "unknown", "body_recorded": False, "args_recorded": False}
    if parity_type is not None and type(exc) is parity_type:
        code = getattr(exc, "code", None)
        result.update(type="FixtureSourceParityError", code=code if type(code) is str and code in PARITY_CODES else None)
    if type(exc) is subprocess.TimeoutExpired:
        frames, tb = [], exc.__traceback__
        while tb is not None:
            frames.append(tb.tb_frame.f_code)
            tb = tb.tb_next
        expected = [sys.executable, "-B", "-m", MODULE, "suite-child", run_id]
        if type(exc.timeout) in (int, float) and exc.timeout == 10800 and type(exc.cmd) is list and exc.cmd == expected and parent.__code__ in frames:
            result["scope"] = "original_suite_child_10800_seconds"
        else:
            result["scope"] = "other_or_unverified_timeout"
    return result


def original_receipts(root, run_id):
    """Strict numeric projection, not cryptographic attestation of runner output."""
    base = f"output/item6-bridge-validation/{run_id}"
    process = decode(read_bounded(root, base + "/process-result.json")[0])
    report = decode(read_bounded(root, base + "/validation.json")[0])
    require(type(process) is dict and type(report) is dict, "receipt_type")
    code = process.get("actual_exit_code")
    require(type(code) is int and -2147483648 <= code <= 4294967295, "exit_code")
    before, after = process.get("source_before"), process.get("source_after")
    require(type(before) is dict and type(after) is dict and 0 < len(before) <= 4096 and set(before) == set(after), "source_maps")
    require(all(type(x) is str and re.fullmatch(r"[0-9a-f]{64}", x) for m in (before, after) for x in m.values()), "source_hashes")
    require(type(process.get("inputs_stable")) is bool and process["inputs_stable"] == (before == after), "source_stability")
    keys = ("planned_tests", "tests", "failures", "errors", "skips")
    require(all(type(report.get(k)) is int and 0 <= report[k] <= 100000 for k in keys), "counts")
    require(0 < report["planned_tests"] and report["tests"] <= report["planned_tests"] and report["skips"] <= report["tests"], "denominator")
    require(type(report.get("test_plan_complete")) is bool and report["test_plan_complete"] == (report["tests"] == report["planned_tests"]), "denominator")
    require(type(report.get("inputs_stable")) is bool, "report_stability")
    return {"kind": "original_runner_receipt_projection", "independently_attested": False,
            "child_actual_exit_code": code, "source_inputs_stable": process["inputs_stable"],
            "validation_inputs_stable": report["inputs_stable"], "test_plan_complete": report["test_plan_complete"],
            "counts": {key: report[key] for key in keys}}


def checked_fixture(root):
    """Preflight source identity before importing the original module, no aliasing."""
    for name, relative in (("tests", "tests/__init__.py"), (MODULE, FIXTURE)):
        spec = importlib.util.find_spec(name)
        require(spec is not None and spec.origin is not None and Path(spec.origin).absolute() == checked_path(root, relative), "import_origin")
    fixture = importlib.import_module(MODULE)
    require(Path(fixture.__file__).absolute() == checked_path(root, FIXTURE), "import_origin")
    require(Path(fixture.suite_parent.__code__.co_filename).absolute() == checked_path(root, FIXTURE), "parent_origin")
    require(fixture.ROOT == Path(root).absolute() and fixture.PYTHON == sys.executable, "fixture_context")
    return fixture.suite_parent, fixture.FixtureSourceParityError


def run_attempt(root, run_id, load_parent, *, checkout=None):
    """Injection seam for offline synthetic unit tests, not a CLI fake/live switch."""
    run_name(run_id)
    directory = f"output/item6-bridge-diagnostics/{run_id}"
    checked_path(root, directory).mkdir(parents=True, exist_ok=False)
    names, ids = static_inventory(root)
    before = bindings(root, names)
    start = time.monotonic()
    write_once(root, directory + "/start.json", {"schema": "bridge-ci-start/1", "run_id": run_id,
        "binding_sha256": before, "checkout": checkout, "planned_tests": None,
        "binding_scope": "diagnostic_inputs_and_static_test_modules_not_full_source_selector",
        "diagnostic_only_not_validation": True, "original_suite_timeout_seconds": 10800})
    record = {"schema": "bridge-ci-diagnostic/1", "run_id": run_id, "status": "not_started",
        "parent_return_code": None, "child_actual_exit_code": None, "process_tree_cleanup_proved": None,
        "exception": None, "original_receipts": None, "log_hints": None, "timing_hints": None, "diagnostic_errors": [],
        "selected_source_postcheck": None, "binding_stable": None,
        "hard_job_kill_sealing_guaranteed": False, "all_tests_passed_without_skips": None,
        "validation_success_evaluated_by_diagnostic_wrapper": False}
    intended = 1
    # Only the original parent owns the child/process.log/fixture lifecycle.
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            parent, parity_type = load_parent()
            require(bindings(root, names) == before, "binding_changed_before_invocation")
            require(not checked_path(root, f"output/item6-bridge-validation/{run_id}").exists(), "original_output_exists")
            record["import_elapsed_seconds"] = time.monotonic() - start
            invoked = time.monotonic()
            try:
                returned = parent(run_id)
                require(type(returned) is int and 0 <= returned <= 255, "parent_return")
                record.update(status="parent_returned", parent_return_code=returned)
                intended = returned
            except BaseException as exc:
                record["exception"] = safe_exception(exc, parent, run_id, parity_type)
                record["status"] = "parent_raised"
                intended = 130 if type(exc) is KeyboardInterrupt else 1
            finally:
                record["parent_elapsed_seconds"] = time.monotonic() - invoked
    except BaseException:
        record["diagnostic_errors"].append("bootstrap_or_binding_failed")
        intended = intended or 1
    # Projection is deliberately after the parent returns/raises: no competing
    # log handles, observer thread or implicit extension of the original timer.
    try:
        raw, offset, total = read_bounded(root, f"output/item6-bridge-validation/{run_id}/process.log", MAX_LOG, tail=True)
        record["log_hints"] = log_hints(raw, ids, offset=offset, total_bytes=total)
        try:
            record["timing_hints"] = timing_hints(raw, ids, offset=offset, total_bytes=total)
        except BaseException:
            # This optional hint projection never changes the original verdict.
            record["timing_projection_state"] = "unavailable"
    except FileNotFoundError:
        record["log_state"] = "not_available"
        if record["status"] == "parent_returned":
            record["diagnostic_errors"].append("expected_log_missing")
    except BaseException:
        record["diagnostic_errors"].append("log_projection_failed")
    if record["status"] == "parent_returned":
        try:
            receipt = original_receipts(root, run_id)
            record.update(original_receipts=receipt, child_actual_exit_code=receipt["child_actual_exit_code"],
                          selected_source_postcheck=receipt["source_inputs_stable"])
            if (receipt["child_actual_exit_code"] != record["parent_return_code"] or
                    not receipt["source_inputs_stable"] or not receipt["validation_inputs_stable"] or
                    (intended == 0 and (not receipt["test_plan_complete"] or receipt["counts"]["failures"] or receipt["counts"]["errors"]))):
                record["diagnostic_errors"].append("original_receipts_inconsistent")
        except BaseException:
            record["diagnostic_errors"].append("original_receipts_unavailable_or_invalid")
    try:
        record["binding_stable"] = bindings(root, names) == before
        if not record["binding_stable"]:
            record["diagnostic_errors"].append("binding_changed_after_invocation")
    except BaseException:
        record["diagnostic_errors"].append("binding_postcheck_failed")
    if record["diagnostic_errors"]:
        intended = intended or 1
    record.update(intended_wrapper_exit_code=intended, elapsed_seconds=time.monotonic() - start)
    write_once(root, directory + "/result.json", record)
    return intended


def checkout_record(root, run_id, expected_head):
    run_name(run_id)
    require(type(expected_head) is str and re.fullmatch(r"[0-9a-f]{40}", expected_head), "expected_head")
    value = decode(read_bounded(root, f"output/item6-checkout-source/{run_id}/checkout-source.json")[0])
    require(type(value) is dict and value.get("status") == "valid" and value.get("stage") == "complete", "checkout_receipt")
    require(value.get("actual_head") == value.get("expected_head") == expected_head, "checkout_head")
    keys = ("actual_head", "tree", "source_commitment_sha256", "manifest_sha256")
    for key in keys:
        length = 40 if key in ("actual_head", "tree") else 64
        require(type(value.get(key)) is str and re.fullmatch("[0-9a-f]{" + str(length) + "}", value[key]), "checkout_field")
    return {key: value[key] for key in keys}


def record_exit(root, run_id, code):
    require(type(code) is int and -2147483648 <= code <= 4294967295, "outer_exit")
    directory = f"output/item6-bridge-diagnostics/{run_name(run_id)}"
    # Do not create a successful terminal record if the wrapper never started.
    start = decode(read_bounded(root, directory + "/start.json")[0])
    require(start.get("schema") == "bridge-ci-start/1" and start.get("run_id") == run_id, "start_record")
    try:
        result_hash = digest(read_bounded(root, directory + "/result.json")[0])
    except FileNotFoundError:
        result_hash = None
    write_once(root, directory + "/wrapper-exit.json", {"schema": "bridge-ci-wrapper-exit/1", "run_id": run_id,
        "actual_wrapper_exit_code": code, "observed_by": "workflow_powershell_LASTEXITCODE",
        "result_sha256": result_hash, "child_actual_exit_code": None, "process_tree_cleanup_proved": None})
    require(code != 0 or result_hash is not None, "successful_exit_missing_receipt")


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        require(len(args) in (2, 3), "arguments")
        action, run_id = args[:2]
        run_name(run_id)
        root = Path(__file__).absolute().parents[2]
        require(Path.cwd() == root, "cwd")
        if action == "run" and len(args) == 2:
            require(run_id == "CI-" + os.environ.get("GITHUB_RUN_ID", "") + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", ""), "workflow_run")
            code = run_attempt(root, run_id, lambda: checked_fixture(root),
                checkout=checkout_record(root, run_id, os.environ.get("ITEM6_EXPECTED_HEAD")))
            print(json.dumps({"schema": "bridge-ci-console/1", "diagnostic_sealed": True,
                              "intended_wrapper_exit_code": code}), flush=True)
            return code
        if action == "record-exit" and len(args) == 3:
            record_exit(root, run_id, int(args[2]))
            return 0
        raise DiagnosticError("arguments")
    except BaseException:
        # No str(exc), args, arbitrary type name, traceback, paths or raw output.
        print('{"schema":"bridge-ci-safe-failure/1","status":"diagnostics_incomplete"}', flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
