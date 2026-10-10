"""Synthetic tests only: never import the bridge fixture or launch its suite."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


REPOSITORY = Path(__file__).absolute().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "_bridge_ci_diagnostics_synthetic_subject",
    REPOSITORY / ".github/ci/bridge_diagnostics.py",
)
DIAGNOSTICS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIAGNOSTICS)
CHECKED_FIXTURE = DIAGNOSTICS.checked_fixture
RUN_ID = "CI-1-1"
CANARY = "PRIVATE_BODY_CANARY_must_never_be_public"
FIRST_ID = "tests.test_synthetic_new.Example.test_first"
SECOND_ID = "tests.test_synthetic_related.Related.test_second"


def timing_event(sequence=1, **changes):
    event = {
        "schema": "item6-bridge-segment-timing/1", "sequence": sequence,
        "kind": "segment_start", "monotonic_offset_ns": sequence * 10 if type(sequence) is int else 10,
        "test_ordinal": 1, "test_id": FIRST_ID, "span": 1,
        "stage": "case_exercise", "mode": "expiry_after_scoring",
        "duration_ns": None, "outcome": "started", "exception_type": None, "safe_marker": None,
    }
    event.update(changes)
    return event


def timing_line(event):
    return b"ITEM6_BRIDGE_TIMING " + json.dumps(event).encode("utf-8") + b"\n"


def finalization_marker(mode="expiry_after_scoring", point="after_run_case"):
    return {
        "mode": mode, "point": point, "injection_hit": True,
        "process_exit_code": 1, "verifier_exit_code": 1 if point == "after_verify" else None,
        "failure_stage": {"expiry_after_scoring": "scoring_after", "expiry_after_export": "export_after",
                          "expiry_after_seal": "seal_after", "expiry_after_terminal": "terminal_after_write"}[mode],
        "seal_present": False, "partial_observation_count": 1, "calls_count": 1,
        "error_code": "item6_finalization_deadline",
        "verifier_error_code": "item6_finalization_failed" if point == "after_verify" else None,
    }


class SyntheticParityError(Exception):
    def __init__(self, code):
        super().__init__(CANARY)
        self.code = code


class BridgeDiagnosticsSyntheticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bridge-diagnostics-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).absolute()
        # Poisoned top-level code proves the inventory is read through AST only.
        for relative, content in {
            DIAGNOSTICS.WRAPPER: "# synthetic wrapper binding only\n",
            DIAGNOSTICS.WORKFLOW: "# synthetic workflow binding only\n",
            DIAGNOSTICS.TEST: "raise AssertionError('synthetic test was imported')\n",
            "tests/__init__.py": "raise AssertionError('synthetic package was imported')\n",
            DIAGNOSTICS.FIXTURE: (
                "raise AssertionError('synthetic fixture was imported')\n"
                "NEW_TESTS = ('tests.test_synthetic_new',)\n"
                "RELATED_TESTS = ['tests.test_synthetic_related']\n"
            ),
            "tests/test_synthetic_new.py": (
                "raise AssertionError('synthetic module was imported')\n"
                "class Example:\n"
                "    def test_first(self): raise AssertionError('must not run')\n"
            ),
            "tests/test_synthetic_related.py": (
                "raise AssertionError('synthetic module was imported')\n"
                "class Related:\n"
                "    async def test_second(self): raise AssertionError('must not run')\n"
            ),
        }.items():
            self.put(relative, content)
        # A regression must fail in-process before any test subprocess can start.
        process_guard = mock.patch.object(
            subprocess, "Popen", side_effect=AssertionError("subprocess forbidden in synthetic tests")
        )
        self.no_process = process_guard.start()
        self.addCleanup(process_guard.stop)
        fixture_guard = mock.patch.object(
            DIAGNOSTICS, "checked_fixture", side_effect=AssertionError("real fixture import forbidden")
        )
        self.no_fixture = fixture_guard.start()
        self.addCleanup(fixture_guard.stop)

    def put(self, relative, content):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return target

    def evidence(self, code=0, *, process=None, report=None, log=None):
        process_record = {
            "actual_exit_code": code,
            "source_before": {"synthetic.py": "a" * 64},
            "source_after": {"synthetic.py": "a" * 64},
            "inputs_stable": True,
            "private_unused": CANARY,
        }
        report_record = {
            "planned_tests": 2, "tests": 2, "failures": 0, "errors": 0,
            "skips": 0, "test_plan_complete": True, "inputs_stable": True,
            "private_unused": CANARY,
        }
        process_record.update(process or {})
        report_record.update(report or {})
        base = f"output/item6-bridge-validation/{RUN_ID}"
        self.put(base + "/process-result.json", json.dumps(process_record))
        self.put(base + "/validation.json", json.dumps(report_record))
        self.put(base + "/process.log", log if log is not None else (
            "test_first (tests.test_synthetic_new.Example) ... ok\n"
            "test_second (tests.test_synthetic_related.Related) ... ok\n"
            + CANARY + "\n"
        ))

    def attempt(self, parent, *, loader=None):
        load_parent = loader or mock.Mock(return_value=(parent, SyntheticParityError))
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = DIAGNOSTICS.run_attempt(self.root, RUN_ID, load_parent)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        self.no_process.assert_not_called()
        self.no_fixture.assert_not_called()
        return code, self.result()

    def result(self):
        return json.loads(self.public_path("result.json").read_text(encoding="utf-8"))

    def public_path(self, name):
        return self.root / f"output/item6-bridge-diagnostics/{RUN_ID}" / name

    def assert_private(self):
        for path in self.public_path("start.json").parent.glob("*.json"):
            self.assertNotIn(CANARY, path.read_text(encoding="utf-8"))

    def test_complete_receipts_project_success_without_claiming_attestation(self):
        def parent(run_id):
            self.assertEqual(run_id, RUN_ID)
            print(CANARY)
            print(CANARY, file=sys.stderr)
            self.evidence()
            return 0

        load_parent = mock.Mock(return_value=(parent, SyntheticParityError))
        code, record = self.attempt(parent, loader=load_parent)
        load_parent.assert_called_once_with()
        self.assertEqual(code, 0)
        self.assertEqual(record["status"], "parent_returned")
        self.assertEqual(record["parent_return_code"], 0)
        self.assertEqual(record["child_actual_exit_code"], 0)
        self.assertEqual(record["diagnostic_errors"], [])
        self.assertTrue(record["binding_stable"])
        self.assertTrue(record["selected_source_postcheck"])
        self.assertFalse(record["original_receipts"]["independently_attested"])
        self.assertIsNone(record["all_tests_passed_without_skips"])
        self.assertFalse(record["validation_success_evaluated_by_diagnostic_wrapper"])
        self.assertFalse(record["hard_job_kill_sealing_guaranteed"])
        self.assertIsNone(record["process_tree_cleanup_proved"])
        self.assert_private()

    def test_nonzero_parent_and_child_code_are_preserved(self):
        def parent(_):
            self.evidence(7, report={"failures": 1})
            return 7

        code, record = self.attempt(parent)
        self.assertEqual(code, 7)
        self.assertEqual(record["parent_return_code"], 7)
        self.assertEqual(record["child_actual_exit_code"], 7)
        self.assertEqual(record["intended_wrapper_exit_code"], 7)
        self.assertEqual(record["diagnostic_errors"], [])

    def test_missing_receipts_cannot_turn_parent_zero_into_success(self):
        code, record = self.attempt(lambda _: 0)
        self.assertEqual(code, 1)
        self.assertIsNone(record["child_actual_exit_code"])
        self.assertIn("original_receipts_unavailable_or_invalid", record["diagnostic_errors"])

    def test_missing_expected_log_invalidates_otherwise_valid_receipts(self):
        def parent(_):
            self.evidence()
            (self.root / f"output/item6-bridge-validation/{RUN_ID}/process.log").unlink()
            return 0

        code, record = self.attempt(parent)
        self.assertEqual(code, 1)
        self.assertEqual(record["log_state"], "not_available")
        self.assertEqual(record["child_actual_exit_code"], 0)
        self.assertIn("expected_log_missing", record["diagnostic_errors"])

    def test_invalid_receipt_and_duplicate_json_keys_are_rejected(self):
        self.evidence()
        target = self.root / f"output/item6-bridge-validation/{RUN_ID}/validation.json"
        for raw in (b"not-json", b"[]", b'{"tests": 2, "tests": 2}'):
            with self.subTest(raw=raw):
                target.write_bytes(raw)
                with self.assertRaises((DIAGNOSTICS.DiagnosticError, ValueError)):
                    DIAGNOSTICS.original_receipts(self.root, RUN_ID)

    def test_boolean_values_are_not_accepted_as_numeric_receipt_fields(self):
        for field in ("actual_exit_code", "planned_tests", "tests", "failures", "errors", "skips"):
            with self.subTest(field=field):
                self.evidence(
                    process={field: True} if field == "actual_exit_code" else None,
                    report={field: True} if field != "actual_exit_code" else None,
                )
                with self.assertRaises(DIAGNOSTICS.DiagnosticError):
                    DIAGNOSTICS.original_receipts(self.root, RUN_ID)

    def test_empty_denominator_cannot_claim_success(self):
        def parent(_):
            self.evidence(report={"planned_tests": 0, "tests": 0})
            return 0

        code, record = self.attempt(parent)
        self.assertNotEqual(code, 0)
        self.assertIn("original_receipts_unavailable_or_invalid", record["diagnostic_errors"])

    def test_incomplete_denominator_cannot_claim_success(self):
        def parent(_):
            self.evidence(report={"tests": 1, "test_plan_complete": False})
            return 0

        code, record = self.attempt(parent)
        self.assertEqual(code, 1)
        self.assertFalse(record["original_receipts"]["test_plan_complete"])
        self.assertIn("original_receipts_inconsistent", record["diagnostic_errors"])

    def test_receipt_stability_types_and_process_maps_are_checked(self):
        invalid = (
            ({"inputs_stable": False}, {}),
            ({"source_after": {"synthetic.py": "b" * 64}}, {}),
            ({"inputs_stable": 1}, {}),
            ({}, {"inputs_stable": 1}),
            ({}, {"test_plan_complete": 1}),
        )
        for process, report in invalid:
            with self.subTest(process=process, report=report):
                self.evidence(process=process, report=report)
                with self.assertRaises(DIAGNOSTICS.DiagnosticError):
                    DIAGNOSTICS.original_receipts(self.root, RUN_ID)

    def test_original_and_derived_source_stability_are_independent(self):
        for source_stable, validation_stable in ((True, False), (False, True)):
            with self.subTest(source_stable=source_stable, validation_stable=validation_stable):
                self.evidence(process={
                    "source_after": {"synthetic.py": ("a" if source_stable else "b") * 64},
                    "inputs_stable": source_stable,
                }, report={"inputs_stable": validation_stable})
                receipt = DIAGNOSTICS.original_receipts(self.root, RUN_ID)
                self.assertEqual(receipt["source_inputs_stable"], source_stable)
                self.assertEqual(receipt["validation_inputs_stable"], validation_stable)

    def test_derived_source_instability_invalidates_zero_exit(self):
        def parent(_):
            self.evidence(report={"inputs_stable": False})
            return 0

        code, record = self.attempt(parent)
        self.assertEqual(code, 1)
        self.assertTrue(record["selected_source_postcheck"])
        self.assertFalse(record["original_receipts"]["validation_inputs_stable"])
        self.assertIn("original_receipts_inconsistent", record["diagnostic_errors"])

    def test_valid_failed_stability_receipt_is_projected_as_failure(self):
        def parent(_):
            self.evidence(1, process={
                "source_after": {"synthetic.py": "b" * 64}, "inputs_stable": False,
            }, report={"inputs_stable": False})
            return 1

        code, record = self.attempt(parent)
        self.assertEqual(code, 1)
        self.assertIsNotNone(record["original_receipts"])
        self.assertFalse(record["selected_source_postcheck"])
        self.assertIn("original_receipts_inconsistent", record["diagnostic_errors"])

    def test_skips_are_reported_without_asserting_all_tests_passed(self):
        def parent(_):
            self.evidence(report={"skips": 1})
            return 0

        code, record = self.attempt(parent)
        self.assertEqual(code, 0)
        self.assertEqual(record["original_receipts"]["counts"]["skips"], 1)
        self.assertIsNone(record["all_tests_passed_without_skips"])
        self.assertFalse(record["validation_success_evaluated_by_diagnostic_wrapper"])

    def test_mismatched_child_exit_code_fails_closed(self):
        def parent(_):
            self.evidence(9)
            return 0

        code, record = self.attempt(parent)
        self.assertEqual(code, 1)
        self.assertEqual(record["child_actual_exit_code"], 9)
        self.assertIn("original_receipts_inconsistent", record["diagnostic_errors"])

    def test_exact_original_timeout_uses_parent_frame_and_preserves_unknowns(self):
        def parent(run_id):
            print(CANARY)
            raise subprocess.TimeoutExpired(
                [sys.executable, "-B", "-m", DIAGNOSTICS.MODULE, "suite-child", run_id],
                10800, output=CANARY, stderr=CANARY,
            )

        code, record = self.attempt(parent)
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "parent_raised")
        self.assertEqual(record["exception"]["type"], "TimeoutExpired")
        self.assertEqual(record["exception"]["scope"], "original_suite_child_10800_seconds")
        self.assertEqual(json.loads(self.public_path("start.json").read_text(encoding="utf-8"))["original_suite_timeout_seconds"], 10800)
        self.assertIsNone(record["parent_return_code"])
        self.assertIsNone(record["child_actual_exit_code"])
        self.assertIsNone(record["process_tree_cleanup_proved"])
        self.assertFalse(record["exception"]["body_recorded"])
        self.assertFalse(record["exception"]["args_recorded"])
        self.assert_private()

    def test_other_timeouts_and_spoofed_types_cannot_claim_original_timeout(self):
        expected = [sys.executable, "-B", "-m", DIAGNOSTICS.MODULE, "suite-child", RUN_ID]

        class TimeoutExpired(subprocess.TimeoutExpired):
            pass

        class FakeTimeoutExpired(Exception):
            cmd = expected
            timeout = 10800

        cases = (
            subprocess.TimeoutExpired(expected, 5999),
            subprocess.TimeoutExpired(expected, 6000),
            subprocess.TimeoutExpired(expected, True),
            subprocess.TimeoutExpired(tuple(expected), 10800),
            subprocess.TimeoutExpired(expected[:-1] + ["CI-2-1"], 10800),
            subprocess.TimeoutExpired([CANARY], 10800),
            TimeoutExpired(expected, 10800),
            FakeTimeoutExpired(CANARY),
        )
        for exception in cases:
            with self.subTest(exception_type=type(exception).__name__, timeout=getattr(exception, "timeout", None)):
                def parent(_):
                    raise exception

                try:
                    parent(RUN_ID)
                except BaseException as caught:
                    record = DIAGNOSTICS.safe_exception(caught, parent, RUN_ID, SyntheticParityError)
                self.assertNotEqual(record["scope"], "original_suite_child_10800_seconds")
                self.assertNotIn(CANARY, json.dumps(record))
                if type(exception) is not subprocess.TimeoutExpired:
                    self.assertEqual(record["type"], "unclassified")

    def test_exact_timeout_without_matching_parent_frame_is_unverified(self):
        def parent(_):
            return 0

        try:
            raise subprocess.TimeoutExpired(
                [sys.executable, "-B", "-m", DIAGNOSTICS.MODULE, "suite-child", RUN_ID], 10800,
            )
        except subprocess.TimeoutExpired as caught:
            record = DIAGNOSTICS.safe_exception(caught, parent, RUN_ID, SyntheticParityError)
        self.assertEqual(record["scope"], "other_or_unverified_timeout")

    def test_arbitrary_exception_body_and_class_name_are_not_public(self):
        secret_type = type(CANARY, (Exception,), {})

        def parent(_):
            print(CANARY)
            raise secret_type(CANARY)

        code, record = self.attempt(parent)
        self.assertEqual(code, 1)
        self.assertEqual(record["exception"]["type"], "unclassified")
        self.assert_private()

    def test_parity_error_code_uses_exact_type_and_fixed_code_allowlist(self):
        def parent(_):
            return 0

        allowed = next(iter(DIAGNOSTICS.PARITY_CODES))
        record = DIAGNOSTICS.safe_exception(SyntheticParityError(allowed), parent, RUN_ID, SyntheticParityError)
        self.assertEqual(record["type"], "FixtureSourceParityError")
        self.assertEqual(record["code"], allowed)
        record = DIAGNOSTICS.safe_exception(SyntheticParityError(CANARY), parent, RUN_ID, SyntheticParityError)
        self.assertIsNone(record["code"])
        self.assertNotIn(CANARY, json.dumps(record))

    def test_inventory_is_static_and_never_imports_synthetic_modules(self):
        with mock.patch.object(DIAGNOSTICS.importlib, "import_module", side_effect=AssertionError("no imports")):
            files, identifiers = DIAGNOSTICS.static_inventory(self.root)
        self.assertEqual(identifiers, frozenset((FIRST_ID, SECOND_ID)))
        self.assertEqual(len(files), 7)
        self.assertIn(DIAGNOSTICS.FIXTURE, files)

    def test_same_named_shadow_package_or_fixture_is_rejected_before_import(self):
        expected_package = str(self.root / "tests/__init__.py")
        expected_fixture = str(self.root / DIAGNOSTICS.FIXTURE)
        for package, fixture in (
            (str(self.root / "shadow/tests/__init__.py"), expected_fixture),
            (expected_package, str(self.root / "shadow" / DIAGNOSTICS.FIXTURE)),
        ):
            with self.subTest(package=package, fixture=fixture):
                with mock.patch.object(DIAGNOSTICS, "checked_fixture", CHECKED_FIXTURE):
                    with mock.patch.object(DIAGNOSTICS.importlib.util, "find_spec", side_effect=[
                        SimpleNamespace(origin=package), SimpleNamespace(origin=fixture),
                    ]):
                        with mock.patch.object(DIAGNOSTICS.importlib, "import_module") as importing:
                            with self.assertRaises(DIAGNOSTICS.DiagnosticError):
                                DIAGNOSTICS.checked_fixture(self.root)
                            importing.assert_not_called()

    def test_checkout_receipt_is_bound_to_expected_head_and_run_path(self):
        expected_head = "a" * 40
        receipt = {
            "status": "valid", "stage": "complete",
            "actual_head": expected_head, "expected_head": expected_head,
            "tree": "b" * 40, "source_commitment_sha256": "c" * 64,
            "manifest_sha256": "d" * 64, "private_unused": CANARY,
        }
        path = self.put(f"output/item6-checkout-source/{RUN_ID}/checkout-source.json", json.dumps(receipt))
        projection = DIAGNOSTICS.checkout_record(self.root, RUN_ID, expected_head)
        self.assertEqual(projection["actual_head"], expected_head)
        self.assertNotIn(CANARY, json.dumps(projection))
        with self.assertRaises(DIAGNOSTICS.DiagnosticError):
            DIAGNOSTICS.checkout_record(self.root, RUN_ID, "e" * 40)
        with self.assertRaises(FileNotFoundError):
            DIAGNOSTICS.checkout_record(self.root, "CI-2-1", expected_head)
        with self.assertRaises(DIAGNOSTICS.DiagnosticError):
            DIAGNOSTICS.checkout_record(self.root, "../CI-1-1", expected_head)
        receipt.update(actual_head="f" * 40, expected_head="f" * 40)
        path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaises(DIAGNOSTICS.DiagnosticError):
            DIAGNOSTICS.checkout_record(self.root, RUN_ID, expected_head)

    def test_main_rejects_run_identity_mismatch_before_loading_checkout_or_parent(self):
        output = io.StringIO()
        with mock.patch.object(DIAGNOSTICS, "__file__", str(self.root / DIAGNOSTICS.WRAPPER)):
            with mock.patch.object(Path, "cwd", return_value=self.root):
                with mock.patch.dict(DIAGNOSTICS.os.environ, {"GITHUB_RUN_ID": "2", "GITHUB_RUN_ATTEMPT": "1"}):
                    with mock.patch.object(DIAGNOSTICS, "checkout_record") as checkout:
                        with mock.patch.object(DIAGNOSTICS, "run_attempt") as attempt:
                            with contextlib.redirect_stdout(output):
                                code = DIAGNOSTICS.main(["run", RUN_ID])
        self.assertEqual(code, 1)
        checkout.assert_not_called()
        attempt.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["status"], "diagnostics_incomplete")
        self.no_fixture.assert_not_called()

    def test_log_allowlist_discards_reasons_ansi_and_unknown_identifiers(self):
        lines = [
            "test_first (tests.test_synthetic_new.Example) ... ok",
            "test_second (tests.test_synthetic_related.Related.test_second) ... skipped '" + CANARY + "'",
            "test_first (tests.test_foreign.Example) ... ok",
            "test_forged (tests.test_synthetic_new.Example.test_first) ... ok",
            "\x1b[32mtest_first (tests.test_synthetic_new.Example) ... ok\x1b[0m",
            "test_first (tests.test_synthetic_new.Example) ... ok\x1b[0m",
            "test_first (tests.test_synthetic_new.Example) ... " + CANARY,
        ]
        hints = DIAGNOSTICS.log_hints(("\n".join(lines) + "\n").encode(), {FIRST_ID, SECOND_ID})
        self.assertEqual(hints["last_markers"], [
            {"test_id": FIRST_ID, "marker": "ok_hint"},
            {"test_id": SECOND_ID, "marker": "skip_hint"},
        ])
        self.assertEqual(hints["ignored_lines"], 5)
        self.assertFalse(hints["trusted_execution_counts"])
        self.assertTrue(hints["cannot_prove_current_test_or_completion"])
        self.assertNotIn(CANARY, json.dumps(hints))

    def test_partial_tail_invalid_utf8_long_lines_and_row_limit_are_bounded(self):
        valid = b"test_first (tests.test_synthetic_new.Example) ... ok\n"
        hints = DIAGNOSTICS.log_hints(valid + b"\xff\n" + b"x" * (DIAGNOSTICS.MAX_LINE + 1) + b"\n" + valid,
                                      {FIRST_ID}, offset=17, total_bytes=99999)
        self.assertEqual(hints["marker_counts_not_test_counts"]["ok_hint"], 1)
        self.assertEqual(hints["ignored_lines"], 1)
        self.assertEqual(hints["oversized_lines"], 1)
        self.assertTrue(hints["truncated"])
        self.assertEqual(hints["tail_offset"], 17)
        with mock.patch.object(DIAGNOSTICS, "MAX_ROWS", 3):
            repeated = DIAGNOSTICS.log_hints(valid * 8, {FIRST_ID})
        self.assertTrue(repeated["truncated"])
        self.assertLessEqual(repeated["marker_counts_not_test_counts"]["ok_hint"], 3)
        self.assertFalse(repeated["trusted_execution_counts"])

    def test_duplicate_log_markers_remain_hints_and_cannot_supply_receipts(self):
        def parent(_):
            self.put(f"output/item6-bridge-validation/{RUN_ID}/process.log",
                     "test_first (tests.test_synthetic_new.Example) ... ok\n" * 30)
            return 0

        code, record = self.attempt(parent)
        hints = record["log_hints"]
        self.assertEqual(code, 1)
        self.assertEqual(hints["marker_counts_not_test_counts"]["ok_hint"], 30)
        self.assertEqual(len(hints["last_markers"]), 20)
        self.assertFalse(hints["trusted_execution_counts"])
        self.assertIsNone(record["original_receipts"])
        self.assertIsNone(record["child_actual_exit_code"])

    def test_timing_pairs_are_bounded_untrusted_hints_even_when_complete(self):
        events = [
            timing_event(1, kind="test_start", stage=None, span=None, mode=None),
            timing_event(2),
            timing_event(3, kind="segment_end", outcome="returned", duration_ns=10),
            timing_event(4, kind="test_end", stage=None, span=None, mode=None,
                         outcome="callback_complete", duration_ns=30),
        ]
        hints = DIAGNOSTICS.timing_hints(b"".join(map(timing_line, events)), {FIRST_ID})
        self.assertEqual(hints["last_events"], events)
        self.assertEqual(hints["accepted_marker_count_not_execution_count"], 4)
        for key in ("missing_end_count", "unmatched_end_count", "duration_mismatch_count", "sequence_gap_count"):
            self.assertEqual(hints[key], 0)
        self.assertFalse(hints["independently_attested"])
        self.assertFalse(hints["marker_source_authenticated"])
        self.assertFalse(hints["trusted_execution_counts"])
        self.assertTrue(hints["cannot_prove_current_test_or_completion"])
        self.assertTrue(hints["ordering_and_pairing_are_untrusted_hints"])
        self.assertEqual(set(hints["last_events"][0]), set(DIAGNOSTICS.TIMING_KEYS))

    def test_timing_rejects_canaries_unknown_fields_enums_and_test_ids(self):
        events = [timing_event(**{key: CANARY}) for key in DIAGNOSTICS.TIMING_KEYS]
        events += [timing_event(private_unused=CANARY), timing_event(test_id="tests.test_foreign.Example.test_first")]
        missing = timing_event()
        del missing["exception_type"]
        events.append(missing)
        for event in events:
            with self.subTest(field_changes=event):
                hints = DIAGNOSTICS.timing_hints(timing_line(event), {FIRST_ID})
                self.assertEqual(hints["last_events"], [])
                self.assertEqual(hints["rejected_markers"], 1)
                self.assertNotIn(CANARY, json.dumps(hints))

    def test_timing_rejects_booleans_negative_duration_and_numeric_overflow(self):
        invalid = {
            "sequence": (True, False, 0, 16385, 1.0, None),
            "monotonic_offset_ns": (True, -1, 86400000000001, 1.0, None),
            "duration_ns": (True, False, -1, 86400000000001, 1.0),
            "test_ordinal": (True, 0, 4097, 1.0),
            "span": (True, 0, 16385, 1.0),
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    event = timing_event(kind="segment_end", outcome="returned", duration_ns=0)
                    event[field] = value
                    hints = DIAGNOSTICS.timing_hints(timing_line(event), {FIRST_ID})
                    self.assertEqual(hints["last_events"], [])
                    self.assertEqual(hints["rejected_markers"], 1)
        boundary = timing_event(16384, monotonic_offset_ns=86400000000000, duration_ns=86400000000000,
                                kind="segment_end", outcome="returned", test_ordinal=4096,
                                span=16384, mode="other_fixture_mode")
        hints = DIAGNOSTICS.timing_hints(timing_line(boundary), {FIRST_ID})
        self.assertEqual(hints["last_events"], [boundary])

    def test_timing_safe_marker_is_an_exact_typed_allowlist(self):
        changes = [(key, CANARY) for key in DIAGNOSTICS.TIMING_MARKER_KEYS]
        changes += [("private_unused", CANARY), ("mode", "other_fixture_mode"), ("point", None),
                    ("injection_hit", 1), ("seal_present", 0)]
        changes += [(key, value) for key in ("process_exit_code", "verifier_exit_code")
                    for value in (True, False, -2, 4097, 1.0)]
        changes += [(key, value) for key in ("partial_observation_count", "calls_count")
                    for value in (True, False, -1, 4097, 1.0)]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                marker = finalization_marker()
                marker[field] = value
                event = timing_event(kind="finalization_marker", stage=None, span=None,
                                     safe_marker=marker, outcome="observed")
                hints = DIAGNOSTICS.timing_hints(timing_line(event), {FIRST_ID})
                self.assertEqual(hints["last_events"], [])
                self.assertEqual(hints["finalization_marker_summary"], [])
                self.assertEqual(hints["rejected_markers"], 1)
                self.assertNotIn(CANARY, json.dumps(hints))
        marker = finalization_marker()
        del marker["calls_count"]
        event = timing_event(kind="finalization_marker", stage=None, span=None, safe_marker=marker, outcome="observed")
        hints = DIAGNOSTICS.timing_hints(timing_line(event), {FIRST_ID})
        self.assertEqual(hints["rejected_markers"], 1)

    def test_timing_rejects_schema_valid_fields_in_invalid_event_shapes(self):
        cases = [
            (timing_event(kind="test_start", stage=None, span=None), [
                {"test_ordinal": None}, {"stage": "case_exercise"}, {"span": 1},
                {"duration_ns": 0}, {"outcome": "callback_complete"},
                {"safe_marker": finalization_marker()}, {"exception_type": "OSError"},
            ]),
            (timing_event(kind="test_end", stage=None, span=None, duration_ns=10, outcome="callback_complete"), [
                {"test_ordinal": None}, {"stage": "case_exercise"}, {"span": 1},
                {"duration_ns": None}, {"outcome": "returned"},
                {"safe_marker": finalization_marker()}, {"exception_type": "OSError"},
            ]),
            (timing_event(), [
                {"stage": None}, {"span": None}, {"duration_ns": 0}, {"outcome": "returned"},
                {"safe_marker": finalization_marker()}, {"exception_type": "OSError"},
            ]),
            (timing_event(kind="segment_end", duration_ns=10, outcome="returned"), [
                {"stage": None}, {"span": None}, {"duration_ns": None}, {"duration_ns": 11},
                {"outcome": "observed"}, {"safe_marker": finalization_marker()}, {"exception_type": "OSError"},
            ]),
            (timing_event(kind="finalization_marker", stage=None, span=None,
                          outcome="observed", safe_marker=finalization_marker()), [
                {"stage": "case_exercise"}, {"span": 1}, {"duration_ns": 0}, {"outcome": "returned"},
                {"safe_marker": None}, {"mode": None}, {"mode": "other_fixture_mode"},
                {"mode": "expiry_after_export"}, {"exception_type": "OSError"},
            ]),
        ]
        for valid, changes in cases:
            baseline = DIAGNOSTICS.timing_hints(timing_line(valid), {FIRST_ID})
            self.assertEqual(baseline["last_events"], [valid])
            for fields in changes:
                with self.subTest(kind=valid["kind"], changes=fields):
                    event = {**valid, **fields}
                    hints = DIAGNOSTICS.timing_hints(timing_line(event), {FIRST_ID})
                    self.assertEqual(hints["last_events"], [])
                    self.assertEqual(hints["finalization_marker_summary"], [])
                    self.assertEqual(hints["rejected_markers"], 1)
                    self.assertFalse(hints["marker_source_authenticated"])
                    self.assertTrue(hints["cannot_prove_current_test_or_completion"])

    def test_timing_projects_all_four_expiry_modes_and_both_marker_points(self):
        events = []
        for mode in ("expiry_after_scoring", "expiry_after_export", "expiry_after_seal", "expiry_after_terminal"):
            for point in ("after_run_case", "after_verify"):
                events.append(timing_event(len(events) + 1, kind="finalization_marker", mode=mode,
                    stage=None, span=None, outcome="observed", safe_marker=finalization_marker(mode, point)))
        hints = DIAGNOSTICS.timing_hints(b"".join(map(timing_line, events)), {FIRST_ID})
        self.assertEqual(hints["last_events"], events)
        self.assertEqual(len(hints["finalization_marker_summary"]), 8)
        self.assertEqual({(row["safe_marker"]["mode"], row["safe_marker"]["point"])
                          for row in hints["finalization_marker_summary"]},
                         {(event["mode"], event["safe_marker"]["point"]) for event in events})
        for row in hints["finalization_marker_summary"]:
            self.assertEqual(set(row["safe_marker"]), set(DIAGNOSTICS.TIMING_MARKER_KEYS))
        self.assertTrue(hints["cannot_prove_current_test_or_completion"])
        self.assertFalse(hints["marker_source_authenticated"])

    def test_timing_rejects_duplicate_json_keys_and_non_json_without_leaking_text(self):
        valid = timing_line(timing_event())
        duplicate = valid.replace(b'"schema":', b'"schema": "' + CANARY.encode() + b'", "schema":', 1)
        marker = timing_line(timing_event(kind="finalization_marker", stage=None, span=None,
                                         outcome="observed", safe_marker=finalization_marker()))
        duplicate_marker = marker.replace(b'"calls_count":', b'"calls_count": "' + CANARY.encode() + b'", "calls_count":', 1)
        raw = duplicate + duplicate_marker + b"ITEM6_BRIDGE_TIMING \xff\n" + b"ITEM6_BRIDGE_TIMING [1, 2]\n"
        hints = DIAGNOSTICS.timing_hints(raw, {FIRST_ID})
        self.assertEqual(hints["rejected_markers"], 4)
        self.assertEqual(hints["last_events"], [])
        self.assertNotIn(CANARY, json.dumps(hints))

    def test_timing_sequence_and_pairing_anomalies_cannot_establish_timeout_or_completion(self):
        events = [timing_event(1), timing_event(1), timing_event(2, monotonic_offset_ns=9),
                  timing_event(4, kind="segment_end", span=2, outcome="returned", duration_ns=1)]
        hints = DIAGNOSTICS.timing_hints(b"".join(map(timing_line, events)), {FIRST_ID})
        self.assertEqual([event["sequence"] for event in hints["last_events"]], [1, 4])
        self.assertEqual(hints["out_of_order_markers"], 2)
        self.assertEqual(hints["sequence_gap_count"], 2)
        self.assertEqual(hints["unmatched_end_count"], 1)
        self.assertEqual(hints["missing_end_count"], 1)
        self.assertTrue(hints["missing_end_is_not_timeout_or_failure_proof"])
        self.assertTrue(hints["cannot_prove_current_test_or_completion"])
        mismatched = [timing_event(1), timing_event(2, kind="segment_end", stage="case_result",
                      outcome="raised", duration_ns=10, exception_type="TimeoutExpired"),
                      timing_event(3, kind="segment_end", outcome="returned", duration_ns=1)]
        hints = DIAGNOSTICS.timing_hints(b"".join(map(timing_line, mismatched)), {FIRST_ID})
        self.assertEqual(hints["mismatched_end_context_count"], 1)
        self.assertEqual(hints["duration_mismatch_count"], 1)
        self.assertFalse(hints["trusted_execution_counts"])

    def test_timing_requires_complete_standalone_lines_and_bounds_tail_projection(self):
        first, second = timing_line(timing_event(1)), timing_line(timing_event(2))
        raw = first + second.replace(b"\n", b"\r\n") + b"\x1b[32m" + first
        raw += b"prefix " + first + b"x" * (DIAGNOSTICS.MAX_LINE + 1) + b"\n" + first[:-1]
        hints = DIAGNOSTICS.timing_hints(raw, {FIRST_ID}, offset=17, total_bytes=99999)
        self.assertEqual([event["sequence"] for event in hints["last_events"]], [2])
        self.assertEqual(hints["ignored_lines"], 2)
        self.assertEqual(hints["oversized_lines"], 1)
        self.assertEqual(hints["incomplete_lines"], 1)
        self.assertTrue(hints["truncated"])
        self.assertEqual(hints["tail_offset"], 17)
        self.assertEqual(hints["total_file_bytes"], 99999)
        events = [timing_event(sequence, span=sequence) for sequence in range(1, 56)]
        hints = DIAGNOSTICS.timing_hints(b"".join(map(timing_line, events)), {FIRST_ID})
        self.assertEqual(hints["last_events"], events[-50:])
        self.assertTrue(hints["event_tail_truncated"])
        with mock.patch.object(DIAGNOSTICS, "TIMING_MAX_SEQUENCE", 3):
            bounded = DIAGNOSTICS.timing_hints(first * 8, {FIRST_ID})
        self.assertTrue(bounded["truncated"])
        self.assertEqual(bounded["out_of_order_markers"], 2)
        with self.assertRaises(DIAGNOSTICS.DiagnosticError):
            DIAGNOSTICS.timing_hints(b"x" * (DIAGNOSTICS.MAX_LOG + 1), {FIRST_ID})

    def test_timing_uses_the_same_single_log_read_and_missing_end_does_not_change_exit(self):
        raw = b"test_first (tests.test_synthetic_new.Example) ... ok\n" + timing_line(timing_event())

        def parent(_):
            self.evidence(log=raw)
            return 0

        with mock.patch.object(DIAGNOSTICS, "read_bounded", wraps=DIAGNOSTICS.read_bounded) as reading:
            with mock.patch.object(DIAGNOSTICS, "log_hints", wraps=DIAGNOSTICS.log_hints) as legacy:
                with mock.patch.object(DIAGNOSTICS, "timing_hints", wraps=DIAGNOSTICS.timing_hints) as timing:
                    code, record = self.attempt(parent)
        self.assertEqual(sum(call.args[1].endswith("/process.log") for call in reading.call_args_list), 1)
        self.assertEqual(legacy.call_args, timing.call_args)
        self.assertEqual(code, 0)
        self.assertEqual(record["diagnostic_errors"], [])
        self.assertEqual(record["timing_hints"]["missing_end_count"], 1)
        self.assertTrue(record["timing_hints"]["cannot_prove_current_test_or_completion"])

    def test_timing_projection_failure_is_private_and_does_not_change_original_verdict(self):
        def parent(_):
            self.evidence()
            return 0

        with mock.patch.object(DIAGNOSTICS, "timing_hints", side_effect=ValueError(CANARY)):
            code, record = self.attempt(parent)
        self.assertEqual(code, 0)
        self.assertEqual(record["diagnostic_errors"], [])
        self.assertIsNone(record["timing_hints"])
        self.assertEqual(record["timing_projection_state"], "unavailable")
        self.assert_private()

    def test_rejected_timing_markers_do_not_change_original_verdict_or_publish_canary(self):
        def parent(_):
            self.evidence(log=timing_line(timing_event(private_unused=CANARY)))
            return 0

        code, record = self.attempt(parent)
        self.assertEqual(code, 0)
        self.assertEqual(record["diagnostic_errors"], [])
        self.assertEqual(record["timing_hints"]["rejected_markers"], 1)
        self.assertEqual(record["timing_hints"]["last_events"], [])
        self.assert_private()

    def test_forged_complete_timing_cannot_supply_missing_original_receipts(self):
        def parent(_):
            self.put(f"output/item6-bridge-validation/{RUN_ID}/process.log", b"".join(map(timing_line, [
                timing_event(1, kind="test_start", stage=None, span=None),
                timing_event(2, kind="test_end", stage=None, span=None, outcome="callback_complete", duration_ns=10),
            ])))
            return 0

        code, record = self.attempt(parent)
        self.assertEqual(code, 1)
        self.assertEqual(record["timing_hints"]["missing_end_count"], 0)
        self.assertIsNone(record["original_receipts"])
        self.assertIsNone(record["child_actual_exit_code"])
        self.assertIsNone(record["all_tests_passed_without_skips"])
        self.assertIn("original_receipts_unavailable_or_invalid", record["diagnostic_errors"])

    def test_run_ids_and_unsafe_paths_are_rejected(self):
        for invalid in ("", "CI-1", "CI-1-1/escape", "CI-1-1\n", "CI-" + "1" * 21 + "-1", True, 1):
            with self.subTest(run_id=invalid), self.assertRaises(DIAGNOSTICS.DiagnosticError):
                DIAGNOSTICS.run_name(invalid)
        for relative in ("", "../outside", "tests/../../outside", str(self.root / "absolute")):
            with self.subTest(relative=relative), self.assertRaises(DIAGNOSTICS.DiagnosticError):
                DIAGNOSTICS.checked_path(self.root, relative)

    def test_link_and_windows_reparse_paths_are_rejected_without_creating_links(self):
        original_lstat = Path.lstat
        target = self.root / "tests"
        for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)):
            def synthetic_lstat(path, *args, **kwargs):
                if path == target:
                    return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                return original_lstat(path, *args, **kwargs)

            with self.subTest(mode=mode, attributes=attributes):
                with mock.patch.object(Path, "lstat", synthetic_lstat):
                    with self.assertRaises(DIAGNOSTICS.DiagnosticError):
                        DIAGNOSTICS.checked_path(self.root, "tests/test_synthetic_new.py")

    def test_oversized_source_file_fails_before_parent_is_loaded(self):
        self.put(DIAGNOSTICS.WRAPPER, b"x" * (DIAGNOSTICS.MAX_FILE + 1))
        loader = mock.Mock()
        with self.assertRaises(DIAGNOSTICS.DiagnosticError):
            DIAGNOSTICS.run_attempt(self.root, RUN_ID, loader)
        loader.assert_not_called()
        self.assertFalse(self.public_path("start.json").exists())
        self.assertFalse(self.public_path("result.json").exists())

    def test_bounded_tail_reads_only_the_requested_suffix(self):
        self.put("synthetic.log", b"0123456789abcdefghij")
        raw, offset, total = DIAGNOSTICS.read_bounded(self.root, "synthetic.log", 8, tail=True)
        self.assertEqual((raw, offset, total), (b"cdefghij", 12, 20))
        with self.assertRaises(DIAGNOSTICS.DiagnosticError):
            DIAGNOSTICS.read_bounded(self.root, "synthetic.log", 8)

    def test_existing_diagnostic_directory_is_never_reused(self):
        sentinel = self.put(f"output/item6-bridge-diagnostics/{RUN_ID}/sentinel", CANARY)
        loader = mock.Mock()
        with self.assertRaises(FileExistsError):
            DIAGNOSTICS.run_attempt(self.root, RUN_ID, loader)
        loader.assert_not_called()
        self.assertEqual(sentinel.read_text(), CANARY)

    def test_existing_original_output_blocks_parent_invocation(self):
        sentinel = self.put(f"output/item6-bridge-validation/{RUN_ID}/sentinel", CANARY)
        parent = mock.Mock(return_value=0)
        code, record = self.attempt(parent)
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "not_started")
        parent.assert_not_called()
        self.assertIn("bootstrap_or_binding_failed", record["diagnostic_errors"])
        self.assertEqual(sentinel.read_text(), CANARY)
        self.assert_private()

    def test_source_changed_by_loader_blocks_parent_invocation(self):
        parent = mock.Mock(return_value=0)

        def loader():
            self.put(DIAGNOSTICS.WRAPPER, "# changed during synthetic load\n")
            return parent, SyntheticParityError

        code, record = self.attempt(parent, loader=loader)
        self.assertEqual(code, 1)
        parent.assert_not_called()
        self.assertFalse(record["binding_stable"])
        self.assertIn("bootstrap_or_binding_failed", record["diagnostic_errors"])

    def test_source_changed_by_parent_invalidates_zero_exit(self):
        def parent(_):
            self.evidence()
            self.put(DIAGNOSTICS.TEST, "# modified synthetic source\n")
            return 0

        code, record = self.attempt(parent)
        self.assertEqual(code, 1)
        self.assertFalse(record["binding_stable"])
        self.assertIn("binding_changed_after_invocation", record["diagnostic_errors"])

    def test_log_evidence_io_error_fails_closed_without_copying_exception(self):
        original_read = DIAGNOSTICS.read_bounded

        def fail_log(root, relative, *args, **kwargs):
            if relative.endswith("/process.log"):
                raise PermissionError(CANARY)
            return original_read(root, relative, *args, **kwargs)

        def parent(_):
            self.evidence()
            return 0

        with mock.patch.object(DIAGNOSTICS, "read_bounded", side_effect=fail_log):
            code, record = self.attempt(parent)
        self.assertEqual(code, 1)
        self.assertIn("log_projection_failed", record["diagnostic_errors"])
        self.assert_private()

    def test_receipt_io_failure_does_not_override_nonzero_parent_code(self):
        def parent(_):
            self.evidence(7)
            return 7

        with mock.patch.object(DIAGNOSTICS, "original_receipts", side_effect=OSError(CANARY)):
            code, record = self.attempt(parent)
        self.assertEqual(code, 7)
        self.assertIn("original_receipts_unavailable_or_invalid", record["diagnostic_errors"])
        self.assert_private()

    def test_start_evidence_write_failure_prevents_callback_loading(self):
        loader = mock.Mock()
        with mock.patch.object(DIAGNOSTICS, "write_once", side_effect=OSError(CANARY)):
            with self.assertRaises(OSError):
                DIAGNOSTICS.run_attempt(self.root, RUN_ID, loader)
        loader.assert_not_called()
        self.assertFalse(self.public_path("result.json").exists())

    def test_final_seal_failure_cannot_return_success(self):
        original_write = DIAGNOSTICS.write_once

        def fail_result(root, relative, record):
            if relative.endswith("/result.json"):
                raise OSError(CANARY)
            return original_write(root, relative, record)

        def parent(_):
            self.evidence()
            return 0

        callback = mock.Mock(side_effect=parent)
        with mock.patch.object(DIAGNOSTICS, "write_once", side_effect=fail_result):
            with self.assertRaises(OSError):
                DIAGNOSTICS.run_attempt(self.root, RUN_ID, lambda: (callback, SyntheticParityError))
        callback.assert_called_once_with(RUN_ID)
        self.assertTrue(self.public_path("start.json").exists())
        self.assertFalse(self.public_path("result.json").exists())
        self.assertFalse(self.public_path("wrapper-exit.json").exists())

    def test_record_exit_separates_observed_code_and_is_exclusive(self):
        def parent(_):
            self.evidence()
            return 0

        code, result = self.attempt(parent)
        result_bytes = self.public_path("result.json").read_bytes()
        self.assertEqual(code, 0)
        self.assertEqual(result["intended_wrapper_exit_code"], 0)
        DIAGNOSTICS.record_exit(self.root, RUN_ID, 137)
        path = self.public_path("wrapper-exit.json")
        first_bytes = path.read_bytes()
        observed = json.loads(first_bytes)
        self.assertEqual(observed["actual_wrapper_exit_code"], 137)
        self.assertEqual(observed["result_sha256"], DIAGNOSTICS.digest(result_bytes))
        self.assertIsNone(observed["child_actual_exit_code"])
        self.assertIsNone(observed["process_tree_cleanup_proved"])
        with self.assertRaises(FileExistsError):
            DIAGNOSTICS.record_exit(self.root, RUN_ID, 0)
        self.assertEqual(path.read_bytes(), first_bytes)
        self.assertEqual(self.public_path("result.json").read_bytes(), result_bytes)

    def test_record_exit_requires_start_but_can_report_an_unsealed_attempt(self):
        with self.assertRaises(FileNotFoundError):
            DIAGNOSTICS.record_exit(self.root, RUN_ID, 1)
        self.put(f"output/item6-bridge-diagnostics/{RUN_ID}/start.json", json.dumps({
            "schema": "bridge-ci-start/1", "run_id": RUN_ID,
        }))
        with self.assertRaises(DIAGNOSTICS.DiagnosticError):
            DIAGNOSTICS.record_exit(self.root, RUN_ID, True)
        DIAGNOSTICS.record_exit(self.root, RUN_ID, 1)
        observed = json.loads(self.public_path("wrapper-exit.json").read_bytes())
        self.assertIsNone(observed["result_sha256"])
        self.assertEqual(observed["actual_wrapper_exit_code"], 1)
        self.assertFalse(self.public_path("result.json").exists())

    def test_observed_zero_exit_without_final_receipt_is_not_success(self):
        self.put(f"output/item6-bridge-diagnostics/{RUN_ID}/start.json", json.dumps({
            "schema": "bridge-ci-start/1", "run_id": RUN_ID,
        }))
        output = io.StringIO()
        with mock.patch.object(DIAGNOSTICS, "__file__", str(self.root / DIAGNOSTICS.WRAPPER)):
            with mock.patch.object(Path, "cwd", return_value=self.root):
                with contextlib.redirect_stdout(output):
                    code = DIAGNOSTICS.main(["record-exit", RUN_ID, "0"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "diagnostics_incomplete")
        observed = json.loads(self.public_path("wrapper-exit.json").read_bytes())
        self.assertEqual(observed["actual_wrapper_exit_code"], 0)
        self.assertIsNone(observed["result_sha256"])
        self.assertFalse(self.public_path("result.json").exists())


class BridgeWorkflowStaticTests(unittest.TestCase):
    def test_budget_filters_and_public_only_artifact_paths(self):
        # Read only the workflow; no fixture import, suite execution, or byte pin.
        workflow = (REPOSITORY / DIAGNOSTICS.WORKFLOW).read_text(encoding="utf-8")
        self.assertIn("timeout-minutes: 210", workflow)
        for event, following in (("  pull_request:", "  push:"), ("  push:", "  workflow_dispatch:")):
            section = workflow.split(event, 1)[1].split(following, 1)[0]
            self.assertIn('".github/ci/bridge_diagnostics.py"', section)
            self.assertIn('"tests/**"', section)
        artifact = workflow.split("uses: actions/upload-artifact@v4", 1)[1]
        paths = artifact.split("path: |", 1)[1].split("overwrite:", 1)[0]
        entries = [line.strip() for line in paths.splitlines() if line.strip()]
        self.assertEqual(len(entries), 5)
        self.assertTrue(any("item6-bridge-validation/" in path and path.endswith("/public/") for path in entries))
        self.assertTrue(any("item6-checkout-source/" in path for path in entries))
        diagnostics = [path for path in entries if "item6-bridge-diagnostics/" in path]
        self.assertEqual({path.rsplit("/", 1)[-1] for path in diagnostics},
                         {"start.json", "result.json", "wrapper-exit.json"})
        self.assertEqual(len(diagnostics), 3)
        self.assertFalse(any("*" in path for path in diagnostics))
        self.assertNotIn("process.log", paths)
        self.assertNotIn("process-result.json", paths)
        self.assertNotIn("validation.json", paths)
        self.assertIn("overwrite: false", artifact)
        self.assertIn("$bridgeExit = $LASTEXITCODE", workflow)
        self.assertIn("if ($bridgeExit -ne 0) { exit $bridgeExit }", workflow)


if __name__ == "__main__":
    unittest.main()
