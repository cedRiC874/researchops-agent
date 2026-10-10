"""Synthetic pure-observer checks; never import or execute the real fixture entrypoint.

The AST allowlist loads only the diagnostic definitions under test. Temporary
source text is an inventory fixture, not a source commitment or execution claim.
No subprocess, Provider, network transport, real key, or claim store is used.
"""
from contextlib import contextmanager
from pathlib import Path
import ast
import copy
import io
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/item6_experiment_fixture.py"
CANARY = "PRIVATE-CANARY-DO-NOT-EMIT"
SELECTED = frozenset((
    "_TIMING_STAGES", "_FINALIZATION_MODES", "_TIMING_EXCEPTIONS", "_BRIDGE_PROGRESS",
    "BridgeTimingError", "FixtureSourceParityError", "_validate_bridge_timing_row",
    "_bridge_test_ids", "_BridgeProgress", "bridge_segment", "bridge_finalization_marker",
    "_bridge_progress_scope", "_BridgeTimedResult",
))


def _load_pure_observer(source=None, *, root=ROOT):
    tree = ast.parse(FIXTURE.read_bytes() if source is None else source)
    selected, names = [], []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        else:
            continue
        if name in SELECTED:
            names.append(name)
            selected.append(node)
    if len(names) != len(SELECTED) or set(names) != SELECTED:
        raise AssertionError("pure_observer_definition_inventory")
    namespace = dict(
        __name__="synthetic_bridge_progress", ROOT=root, Path=Path, ast=ast,
        re=re, json=json, time=time, threading=threading, subprocess=subprocess,
        sys=sys, unittest=unittest, contextmanager=contextmanager,
    )
    exec(compile(ast.Module(body=selected, type_ignores=[]), "<pure-bridge-observer>", "exec"), namespace)
    return namespace


class _Clock:
    def __init__(self, *values):
        self.values = iter(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return next(self.values)


class _Stream(io.StringIO):
    def __init__(self, *, fault=None, at=1):
        super().__init__()
        self.fault, self.at = fault, at
        self.writes = self.flushes = 0

    def write(self, value):
        self.writes += 1
        if self.writes == self.at and self.fault == "write":
            raise OSError(CANARY)
        if self.writes == self.at and self.fault == "short":
            return max(0, len(value) - 1)
        return super().write(value)

    def flush(self):
        self.flushes += 1
        if self.flushes == self.at and self.fault == "flush":
            raise OSError(CANARY)
        return super().flush()


def _rows(stream):
    prefix = "ITEM6_BRIDGE_TIMING "
    return [json.loads(line[len(prefix):]) for line in stream.getvalue().splitlines() if line.startswith(prefix)]


def _case(*, known=True):
    module = "tests.test_observer_example" if known else CANARY
    case_type = type("ExampleTests", (), {"__module__": module})
    case = case_type()
    case._testMethodName = "test_one"
    case.id = mock.Mock(side_effect=AssertionError("id_callback_must_not_be_used"))
    return case


class BridgeProgressPureTests(unittest.TestCase):
    def setUp(self):
        self.ns = _load_pure_observer()
        self.error = self.ns["BridgeTimingError"]

    def reporter(self, stream=None, values=(100, 110, 140)):
        stream = _Stream() if stream is None else stream
        reporter = self.ns["_BridgeProgress"](stream, clock=_Clock(*values))
        reporter.allowed_ids = frozenset(("tests.test_observer_example.ExampleTests.test_one",))
        return reporter, stream

    def test_disabled_observation_never_reads_clock_or_outputs(self):
        no_clock = mock.Mock(side_effect=AssertionError("clock_must_not_be_called"))
        no_output = mock.Mock(side_effect=AssertionError("output_must_not_be_called"))
        self.ns["time"] = types.SimpleNamespace(monotonic_ns=no_clock)
        self.ns["sys"] = types.SimpleNamespace(stdout=types.SimpleNamespace(write=no_output))
        called = []
        with self.ns["bridge_segment"]("case_exercise", CANARY):
            called.append("operation")
        self.ns["bridge_finalization_marker"]({"untrusted": CANARY})
        self.assertEqual(called, ["operation"])
        no_clock.assert_not_called()
        no_output.assert_not_called()
        self.assertIsNone(self.ns["_BRIDGE_PROGRESS"])

    def test_ast_loader_does_not_execute_other_fixture_top_level(self):
        poison = f"raise RuntimeError({CANARY!r})\n" + FIXTURE.read_text(encoding="utf-8")
        namespace = _load_pure_observer(poison)
        self.assertIsNone(namespace["_BRIDGE_PROGRESS"])
        self.assertNotIn("seed", namespace)
        self.assertNotIn("run_case", namespace)
        self.assertNotIn("suite_parent", namespace)

    def test_ast_test_inventory_does_not_import_poison_module(self):
        with tempfile.TemporaryDirectory(prefix="bridge-observer-inventory-") as temp:
            root = Path(temp)
            path = root / "tests/test_inventory.py"
            path.parent.mkdir()
            path.write_text(
                f"raise RuntimeError({CANARY!r})\n"
                "class ExampleTests:\n"
                "    def test_one(self): pass\n"
                "    async def test_async(self): pass\n"
                "    def helper(self): pass\n",
                encoding="utf-8",
            )
            result = self.ns["_bridge_test_ids"](root, ("tests.test_inventory",))
            self.assertEqual(result, frozenset((
                "tests.test_inventory.ExampleTests.test_one",
                "tests.test_inventory.ExampleTests.test_async",
            )))

    def test_ast_inventory_rejects_nonmodule_input_before_file_read(self):
        for name in ("../private", "tests.test_a/secret", "tests.other", True):
            with self.subTest(name=name), self.assertRaises(self.error):
                self.ns["_bridge_test_ids"](Path("unread-synthetic-root"), (name,))

    def test_fixed_identity_and_test_end_are_not_a_pass_claim(self):
        reporter, stream = self.reporter()
        case = _case()
        reporter.start_test(case)
        reporter.stop_test()
        rows = _rows(stream)
        self.assertEqual([row["test_id"] for row in rows], ["tests.test_observer_example.ExampleTests.test_one"] * 2)
        self.assertEqual([row["test_ordinal"] for row in rows], [1, 1])
        self.assertEqual([row["outcome"] for row in rows], ["started", "callback_complete"])
        self.assertEqual(rows[1]["duration_ns"], 30)
        self.assertNotIn("passed", stream.getvalue())
        self.assertNotIn("success", stream.getvalue())
        self.assertIsNone(reporter.test_started)
        case.id.assert_not_called()

    def test_unknown_identity_is_null_without_calling_id_or_printing_canary(self):
        reporter, stream = self.reporter()
        case = _case(known=False)
        reporter.start_test(case)
        reporter.stop_test()
        self.assertEqual([row["test_id"] for row in _rows(stream)], [None, None])
        self.assertNotIn(CANARY, stream.getvalue())
        case.id.assert_not_called()

    def test_all_case_segments_have_start_end_span_and_duration(self):
        stages = ("case_prepare", "case_source_parity", "case_exercise", "case_result", "independent_readback")
        for stage in stages:
            with self.subTest(stage=stage):
                reporter, stream = self.reporter()
                with reporter.segment(stage, "expiry_after_export"):
                    pass
                rows = _rows(stream)
                self.assertEqual([row["kind"] for row in rows], ["segment_start", "segment_end"])
                self.assertEqual([row["stage"] for row in rows], [stage, stage])
                self.assertEqual(rows[0]["span"], rows[1]["span"])
                self.assertEqual(rows[1]["duration_ns"], 30)
                self.assertEqual(rows[1]["outcome"], "returned")
                self.assertEqual([row["mode"] for row in rows], ["expiry_after_export"] * 2)

    def test_unknown_mode_is_fixed_enum_not_free_text(self):
        reporter, stream = self.reporter()
        with reporter.segment("case_prepare", CANARY):
            pass
        self.assertEqual([row["mode"] for row in _rows(stream)], ["other_fixture_mode"] * 2)
        self.assertNotIn(CANARY, stream.getvalue())

    def test_segment_preserves_exception_object_and_only_allowlists_type(self):
        exceptions = (
            (PermissionError(CANARY), "PermissionError"),
            (self.ns["FixtureSourceParityError"](CANARY, (CANARY,)), "FixtureSourceParityError"),
            (ValueError(CANARY), "other"),
            (type("PermissionError", (RuntimeError,), {})(CANARY), "other"),
        )
        for original, expected in exceptions:
            with self.subTest(expected=expected):
                reporter, stream = self.reporter()
                try:
                    with reporter.segment("case_exercise"):
                        raise original
                except BaseException as observed:
                    self.assertIs(observed, original)
                else:
                    self.fail("original exception did not propagate")
                row = _rows(stream)[-1]
                self.assertEqual(row["exception_type"], expected)
                self.assertEqual(row["outcome"], "raised")
                self.assertNotIn(CANARY, stream.getvalue())

    def test_four_finalization_modes_emit_both_points_with_safe_projection(self):
        for mode in sorted(self.ns["_FINALIZATION_MODES"]):
            with self.subTest(mode=mode):
                reporter, stream = self.reporter()
                value = dict(mode=mode, injection_hit=True, process_exit_code=2,
                             seal_present=False, partial_observation_count=0, calls_count=1,
                             failure_stage="scoring_after", error_code="item6_finalization_deadline",
                             stderr=CANARY, exception_body=CANARY, fixture_root=CANARY)
                reporter.finalization_marker(value)
                value.update(verifier_exit_code=2, verifier_error_code="item6_finalization_failed")
                reporter.finalization_marker(value)
                rows = _rows(stream)
                self.assertEqual([row["safe_marker"]["point"] for row in rows], ["after_run_case", "after_verify"])
                for row in rows:
                    self.assertEqual(row["kind"], "finalization_marker")
                    self.assertEqual(row["mode"], mode)
                    self.assertTrue(row["safe_marker"]["injection_hit"])
                    self.assertEqual(row["safe_marker"]["process_exit_code"], 2)
                    self.assertIsNone(row["duration_ns"])
                    self.assertEqual(set(row["safe_marker"]), {
                        "mode", "point", "injection_hit", "process_exit_code", "verifier_exit_code",
                        "failure_stage", "seal_present", "partial_observation_count", "calls_count",
                        "error_code", "verifier_error_code",
                    })
                self.assertNotIn(CANARY, stream.getvalue())

    def test_marker_does_not_coerce_bool_counts_or_unknown_codes(self):
        reporter, stream = self.reporter(values=(100, 110))
        reporter.finalization_marker(dict(mode="expiry_after_seal", injection_hit="true",
            process_exit_code=True, verifier_exit_code=4097, seal_present=1,
            partial_observation_count=-1, calls_count=False, failure_stage=CANARY,
            error_code=CANARY, verifier_error_code=CANARY, body=CANARY))
        marker = _rows(stream)[0]["safe_marker"]
        for name in ("injection_hit", "process_exit_code", "verifier_exit_code", "seal_present",
                     "partial_observation_count", "calls_count", "failure_stage", "error_code", "verifier_error_code"):
            self.assertIsNone(marker[name], name)
        self.assertNotIn(CANARY, stream.getvalue())

    def test_invalid_marker_mode_locks_without_output(self):
        reporter, stream = self.reporter(values=(100,))
        with self.assertRaises(self.error):
            reporter.finalization_marker({"mode": CANARY})
        self.assertTrue(reporter.failed)
        self.assertEqual(stream.getvalue(), "")

    def test_clock_origin_rejects_bool_negative_or_noninteger(self):
        for origin in (True, -1, 0.5, "100"):
            with self.subTest(origin=origin), self.assertRaises(self.error):
                self.reporter(values=(origin,))

    def test_event_clock_rejects_bool_before_arithmetic_coercion(self):
        reporter, stream = self.reporter(values=(0, True))
        with self.assertRaises(self.error):
            with reporter.segment("case_prepare"):
                self.fail("invalid clock must reject before operation")
        self.assertTrue(reporter.failed)
        self.assertEqual(stream.getvalue(), "")

    def test_clock_backwards_or_over_limit_locks_observer(self):
        for values in ((100, 99), (100, 86400000000101)):
            with self.subTest(values=values):
                reporter, stream = self.reporter(values=values)
                with self.assertRaises(self.error):
                    with reporter.segment("case_prepare"):
                        self.fail("invalid clock must reject before operation")
                self.assertTrue(reporter.failed)
                self.assertEqual(stream.getvalue(), "")
        reporter, stream = self.reporter(values=(100, 120, 119))
        with self.assertRaises(self.error):
            with reporter.segment("case_prepare"):
                pass
        self.assertTrue(reporter.failed)
        self.assertEqual(len(_rows(stream)), 1)

    def test_sequence_and_test_ordinal_limits_reject_without_new_output(self):
        for field, value, action in (("sequence", 16384, "segment"), ("ordinal", 4096, "test")):
            with self.subTest(field=field):
                reporter, stream = self.reporter()
                setattr(reporter, field, value)
                with self.assertRaises(self.error):
                    if action == "segment":
                        with reporter.segment("case_prepare"):
                            self.fail("limit must reject before operation")
                    else:
                        reporter.start_test(_case())
                self.assertTrue(reporter.failed)
                self.assertEqual(stream.getvalue(), "")

    def test_record_validator_rejects_numeric_bool_and_unknown_fields_values(self):
        reporter, stream = self.reporter()
        with reporter.segment("case_prepare"):
            pass
        valid = _rows(stream)[-1]
        changes = (("sequence", True), ("monotonic_offset_ns", True), ("duration_ns", -1),
                   ("duration_ns", valid["monotonic_offset_ns"] + 1), ("span", False),
                   ("test_ordinal", 0), ("stage", CANARY), ("mode", CANARY),
                   ("exception_type", CANARY), ("test_id", CANARY), ("outcome", "passed"))
        for field, value in changes:
            with self.subTest(field=field, value=value):
                row = copy.deepcopy(valid)
                row[field] = value
                with self.assertRaises(self.error):
                    self.ns["_validate_bridge_timing_row"](row, reporter.allowed_ids)

    def test_short_write_write_error_and_flush_error_lock_before_operation(self):
        for fault in ("short", "write", "flush"):
            with self.subTest(fault=fault):
                reporter, stream = self.reporter(_Stream(fault=fault), values=(100, 110))
                called = []
                with self.assertRaises(self.error):
                    with reporter.segment("case_prepare"):
                        called.append("forbidden")
                self.assertTrue(reporter.failed)
                self.assertEqual(called, [])
                observed_io = (stream.writes, stream.flushes)
                with self.assertRaises(self.error):
                    with reporter.segment("case_exercise"):
                        called.append("forbidden-next")
                self.assertEqual((stream.writes, stream.flushes), observed_io)
                self.assertEqual(called, [])
                self.assertNotIn(CANARY, stream.getvalue())

    def test_original_exception_wins_over_end_write_failure_and_locks_next(self):
        reporter, stream = self.reporter(_Stream(fault="write", at=2))
        original = PermissionError(CANARY)
        try:
            with reporter.segment("case_exercise"):
                raise original
        except BaseException as observed:
            self.assertIs(observed, original)
        else:
            self.fail("original exception did not propagate")
        self.assertTrue(reporter.failed)
        with self.assertRaises(self.error):
            with reporter.segment("case_result"):
                self.fail("locked observer allowed another operation")
        self.assertEqual(stream.writes, 2)
        self.assertNotIn(CANARY, stream.getvalue())

    def test_successful_operation_end_failure_is_not_swallowed(self):
        reporter, stream = self.reporter(_Stream(fault="flush", at=2))
        called = []
        with self.assertRaises(self.error):
            with reporter.segment("case_exercise"):
                called.append("returned")
        self.assertEqual(called, ["returned"])
        self.assertTrue(reporter.failed)
        self.assertFalse(stream.closed)

    def test_failure_callback_does_not_replace_evidence_error(self):
        reporter, stream = self.reporter(_Stream(fault="write"), values=(100, 110))
        reporter.on_failure = mock.Mock(side_effect=RuntimeError(CANARY))
        with self.assertRaises(self.error) as caught:
            with reporter.segment("case_prepare"):
                self.fail("operation must not run")
        reporter.on_failure.assert_called_once_with()
        self.assertNotIn(CANARY, str(caught.exception))
        self.assertTrue(reporter.failed)

    def test_timed_result_evidence_failure_requests_stop(self):
        reporter, stream = self.reporter(_Stream(fault="write"), values=(100, 110))
        result = self.ns["_BridgeTimedResult"](io.StringIO(), True, 0, reporter)
        with self.assertRaises(self.error):
            result.startTest(_case())
        self.assertTrue(result.shouldStop)
        self.assertEqual(result.testsRun, 0)

    def test_timed_result_stop_preserves_propagating_keyboard_interrupt(self):
        reporter, stream = self.reporter(_Stream(fault="write", at=2))
        result = self.ns["_BridgeTimedResult"](io.StringIO(), True, 0, reporter)
        case = _case()
        result.startTest(case)
        original = KeyboardInterrupt(CANARY)
        try:
            try:
                raise original
            finally:
                result.stopTest(case)
        except BaseException as observed:
            self.assertIs(observed, original)
        else:
            self.fail("KeyboardInterrupt did not propagate")
        self.assertTrue(reporter.failed)
        self.assertTrue(result.shouldStop)
        self.assertEqual(result.testsRun, 1)
        self.assertNotIn(CANARY, stream.getvalue())

    def test_timed_result_stop_write_failure_without_original_propagates(self):
        reporter, _ = self.reporter(_Stream(fault="write", at=2))
        result = self.ns["_BridgeTimedResult"](io.StringIO(), True, 0, reporter)
        case = _case()
        result.startTest(case)
        with self.assertRaises(self.error):
            result.stopTest(case)
        self.assertTrue(result.shouldStop)

    def test_explicit_two_module_scope_restores_each_value_without_aliases(self):
        imported = types.ModuleType("tests.item6_experiment_fixture")
        imported.__file__ = str(ROOT / "tests/item6_experiment_fixture.py")
        old_local, old_imported, reporter = object(), object(), object()
        self.ns["_BRIDGE_PROGRESS"] = old_local
        imported._BRIDGE_PROGRESS = old_imported
        with mock.patch.dict(sys.modules, {"tests.item6_experiment_fixture": imported}):
            with self.ns["_bridge_progress_scope"](reporter):
                self.assertIs(self.ns["_BRIDGE_PROGRESS"], reporter)
                self.assertIs(imported._BRIDGE_PROGRESS, reporter)
                self.assertIs(sys.modules["tests.item6_experiment_fixture"], imported)
            self.assertIs(self.ns["_BRIDGE_PROGRESS"], old_local)
            self.assertIs(imported._BRIDGE_PROGRESS, old_imported)
            self.assertIs(sys.modules["tests.item6_experiment_fixture"], imported)

    def test_scope_restores_on_original_exception(self):
        imported = types.ModuleType("tests.item6_experiment_fixture")
        imported.__file__ = str(ROOT / "tests/item6_experiment_fixture.py")
        imported._BRIDGE_PROGRESS = "imported-before"
        self.ns["_BRIDGE_PROGRESS"] = "local-before"
        original = RuntimeError(CANARY)
        with mock.patch.dict(sys.modules, {"tests.item6_experiment_fixture": imported}):
            try:
                with self.ns["_bridge_progress_scope"](object()):
                    raise original
            except RuntimeError as observed:
                self.assertIs(observed, original)
            self.assertEqual(self.ns["_BRIDGE_PROGRESS"], "local-before")
            self.assertEqual(imported._BRIDGE_PROGRESS, "imported-before")

    def test_scope_does_not_create_missing_module_alias(self):
        proxy = types.SimpleNamespace(modules={})
        self.ns["sys"] = proxy
        reporter = object()
        with self.ns["_bridge_progress_scope"](reporter):
            self.assertIs(self.ns["_BRIDGE_PROGRESS"], reporter)
            self.assertEqual(proxy.modules, {})
        self.assertIsNone(self.ns["_BRIDGE_PROGRESS"])
        self.assertEqual(proxy.modules, {})

    def test_scope_rejects_wrong_fixture_origin_before_injection(self):
        imported = types.ModuleType("tests.item6_experiment_fixture")
        imported.__file__ = str(ROOT / "tests/not_the_fixture.py")
        imported._BRIDGE_PROGRESS = "unchanged"
        with mock.patch.dict(sys.modules, {"tests.item6_experiment_fixture": imported}):
            with self.assertRaises(self.error):
                with self.ns["_bridge_progress_scope"](object()):
                    self.fail("origin mismatch entered scope")
            self.assertIsNone(self.ns["_BRIDGE_PROGRESS"])
            self.assertEqual(imported._BRIDGE_PROGRESS, "unchanged")


if __name__ == "__main__":
    unittest.main()
