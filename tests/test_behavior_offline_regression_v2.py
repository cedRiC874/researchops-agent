"""Short tests of the offline driver; never launch the actual root regression."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from scripts import run_behavior_offline_regression_v2 as driver


class BehaviorRegressionDriverTests(unittest.TestCase):
    def test_root_collection_declares_the_exact_unfiltered_discovery(self):
        tree = ast.parse(Path(driver.__file__).read_bytes())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "collect_unfiltered")
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "discover"]
        self.assertEqual(len(calls), 1)
        keywords = {item.arg: ast.unparse(item.value) for item in calls[0].keywords}
        self.assertEqual(keywords, {"pattern": "'test*.py'", "top_level_dir": "str(root)"})
        self.assertEqual(ast.unparse(calls[0].args[0]), "str(Path(root) / 'tests')")

    def test_key_variable_name_rejects_before_source_or_claim_actions(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic-test-only"}, clear=True), \
                patch.object(driver.source, "_git", side_effect=AssertionError("must not enter")) as git, \
                contextlib.redirect_stdout(output):
            self.assertEqual(driver.main(), 2)
        git.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["error_code"], "test_environment_key_names_present")
        self.assertNotIn("synthetic-test-only", output.getvalue())

    def test_exclusive_start_marker_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            # Match the real driver's resolved ROOT; TEMP may use a path alias.
            root = Path(directory).resolve(strict=True)
            with driver.reserve_report(root, driver.DIRECTORY + "/started.json") as stream:
                stream.write("original")
            with self.assertRaises(FileExistsError):
                driver.reserve_report(root, driver.DIRECTORY + "/started.json")
            self.assertEqual((root / driver.DIRECTORY / "started.json").read_text(), "original")

    def test_start_marker_fixture_resolves_equivalent_directory_spelling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(strict=True)
            (root / "nested").mkdir()
            # A portable real-directory alias, not a mock of Path or the writer.
            alias = root / "nested" / ".."
            self.assertTrue(alias.samefile(root))
            self.assertNotEqual(alias.absolute(), root)
            with patch.object(tempfile, "TemporaryDirectory",
                              return_value=contextlib.nullcontext(str(alias))) as fixture:
                self.test_exclusive_start_marker_is_not_overwritten()
            fixture.assert_called_once_with()
            self.assertEqual((root / driver.DIRECTORY / "started.json").read_bytes(), b"original")

    def test_writer_still_rejects_noncanonical_root_without_creating_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(strict=True)
            (root / "nested").mkdir()
            alias = root / "nested" / ".."
            self.assertTrue(alias.samefile(root))
            self.assertNotEqual(alias.absolute(), root)
            with self.assertRaisesRegex(ValueError, "report_physical_path_mismatch"):
                with driver.reserve_report(alias, driver.DIRECTORY + "/started.json"):
                    pass
            self.assertFalse((root / driver.DIRECTORY / "started.json").exists())

    def test_driver_output_cannot_escape_to_source_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "report_parent_traversal"):
                driver.reserve_report(root, "output/../src/receipt.json")
            self.assertFalse((root / "src").exists())

    def test_verification_inputs_bind_tests_scripts_ci_and_public_markdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ("tests/test_synthetic.py", "scripts/synthetic.py", ".github/workflows/ci.yml",
                     "evals/internal_behavior_eval_v1/example.md")
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic\n")
            before = driver.verification_inputs(root)
            self.assertEqual({row["path"] for row in before["files"]}, set(names))
            (root / names[-1]).write_text("changed\n")
            self.assertNotEqual(driver.verification_inputs(root)["sha256"], before["sha256"])

    def test_logs_outside_selection_do_not_move_verification_input_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = driver.verification_inputs(root)
            (root / "output").mkdir()
            (root / "output/receipt.json").write_text("{}")
            self.assertEqual(driver.verification_inputs(root), before)

    def test_source_manifest_bytes_and_verification_inputs_must_all_stay_stable(self):
        self.assertTrue(driver.stable_binding({"source": 1}, {"source": 1}, ["test"], ["test"], "a", "a"))
        self.assertFalse(driver.stable_binding({"source": 1}, {"source": 2}, ["test"], ["test"], "a", "a"))
        self.assertFalse(driver.stable_binding({"source": 1}, {"source": 1}, ["test"], ["changed"], "a", "a"))
        self.assertFalse(driver.stable_binding({"source": 1}, {"source": 1}, ["test"], ["test"], "a", "b"))
        self.assertFalse(driver.stable_binding({"source": 1}, {"source": 1}, ["test"], ["test"], "a", None))

    def test_real_runner_continues_after_failure_and_preserves_counts(self):
        def synthetic_failure():
            self.fail("synthetic injected failure")
        calls = []
        suite = unittest.TestSuite([unittest.FunctionTestCase(synthetic_failure),
            unittest.FunctionTestCase(lambda: calls.append("second_executed"))])
        result = driver.run_suite(suite, io.StringIO())
        self.assertEqual(calls, ["second_executed"])
        self.assertEqual((result.testsRun, len(result.failures), len(result.errors), len(result.skipped)), (2, 1, 0, 0))
        self.assertFalse(result.wasSuccessful())

    def test_real_runner_preserves_error_and_skip(self):
        def synthetic_error():
            raise RuntimeError("synthetic error")
        def synthetic_skip():
            raise unittest.SkipTest("synthetic skip")
        result = driver.run_suite(unittest.TestSuite([unittest.FunctionTestCase(synthetic_error),
            unittest.FunctionTestCase(synthetic_skip)]), io.StringIO())
        self.assertEqual((result.testsRun, len(result.failures), len(result.errors), len(result.skipped)), (2, 0, 1, 1))
        self.assertFalse(result.wasSuccessful())

    def test_nested_test_ids_keep_original_order_without_filtering(self):
        def first(): pass
        def second(): pass
        tests = [unittest.FunctionTestCase(first), unittest.FunctionTestCase(second)]
        suite = unittest.TestSuite([unittest.TestSuite(tests[:1]), unittest.TestSuite(tests[1:])])
        self.assertEqual(list(driver.case_ids(suite)), [test.id() for test in tests])

    def test_duplicate_and_empty_test_denominators_are_rejected(self):
        def repeated(): pass
        for suite in (unittest.TestSuite(), unittest.TestSuite([
                unittest.FunctionTestCase(repeated), unittest.FunctionTestCase(repeated)])):
            with self.assertRaisesRegex(ValueError, "root_discovery_empty_or_duplicate_ids"):
                driver.checked_case_ids(suite)

    def test_driver_binds_fixed_batch_base_and_output(self):
        self.assertEqual(driver.BATCH, "behavior-integration-20260915-user-context-v4")
        self.assertEqual(driver.BASE, "8bfc56e1e56dbd1161190f84ded273a5abd12273")
        self.assertEqual(driver.DIRECTORY, "output/internal-offline-regression-v2/" + driver.BATCH)

    def test_declared_lambda_identities_are_stable_and_keep_objects_and_order(self):
        def make(name):
            return unittest.FunctionTestCase(lambda: None, description=name)
        cases = [make("alpha"), make("beta")]
        suite = unittest.TestSuite(cases)
        observation = {}
        identities = driver.checked_case_ids(suite, observation=observation)
        self.assertEqual(list(driver.case_ids(suite)), ["<lambda>", "<lambda>"])
        self.assertEqual(identities, driver.checked_case_ids(unittest.TestSuite([make("alpha"), make("beta")])))
        self.assertEqual(len(set(identities)), 2)
        self.assertEqual(list(driver.iter_cases(suite)), cases)
        self.assertIs(list(driver.iter_cases(suite))[0], cases[0])
        fields = driver.discovery_summary(observation)
        self.assertEqual((fields["discovered_tests"], fields["unique_test_id_count"], fields["native_unique_test_id_count"]), (2, 2, 1))
        self.assertEqual(fields["test_identity_version"], "unittest-declared-test-identity/1.0")

    def test_function_identity_includes_module_not_just_description(self):
        def make(module):
            function = lambda: None
            function.__module__ = module
            return unittest.FunctionTestCase(function, description="same_name")
        cases = [make("synthetic.module_a"), make("synthetic.module_b")]
        identities = driver.checked_case_ids(unittest.TestSuite(cases))
        self.assertEqual(len(set(identities)), 2)

    def test_duplicate_declared_function_identity_still_rejected_with_real_count(self):
        def make():
            return unittest.FunctionTestCase(lambda: None, description="same_test")
        observation = {}
        with self.assertRaisesRegex(ValueError, "root_discovery_empty_or_duplicate_ids"):
            driver.checked_case_ids(unittest.TestSuite([make(), make()]), observation=observation)
        fields = json.loads(json.dumps(driver.discovery_summary(observation)))
        self.assertEqual(fields["discovered_tests"], 2)
        self.assertEqual(fields["unique_test_id_count"], 1)
        self.assertEqual(list(fields["duplicate_test_ids"].values()), [2])
        self.assertEqual(fields["discovery_status"], "rejected")
        self.assertIsNotNone(fields["discovered_test_ids_sha256"])

    def test_duplicate_regular_testcase_identity_is_not_given_a_sequence_suffix(self):
        class SyntheticCase(unittest.TestCase):
            def runTest(self): pass
        observation = {}
        with self.assertRaisesRegex(ValueError, "root_discovery_empty_or_duplicate_ids"):
            driver.checked_case_ids(unittest.TestSuite([SyntheticCase(), SyntheticCase()]), observation=observation)
        self.assertEqual(observation["canonical_ids"], [SyntheticCase().id()] * 2)
        self.assertEqual(driver.discovery_summary(observation)["discovered_tests"], 2)

    def test_anonymous_function_without_description_fails_closed_and_keeps_count(self):
        observation = {}
        with self.assertRaisesRegex(ValueError, "root_discovery_unresolved_identity"):
            driver.checked_case_ids(unittest.TestSuite([unittest.FunctionTestCase(lambda: None)]), observation=observation)
        fields = driver.discovery_summary(observation)
        self.assertEqual(fields["discovered_tests"], 1)
        self.assertIsNone(fields["discovered_test_ids_sha256"])
        self.assertIsNone(fields["unique_test_id_count"])
        self.assertEqual(fields["test_identity_errors"], [dict(native_id="<lambda>", reason="anonymous_function_description_missing")])

    def test_unusable_identity_does_not_lose_other_discovered_cases(self):
        class MissingId(unittest.TestCase):
            def runTest(self): pass
            def id(self): return None
        class KnownId(unittest.TestCase):
            def runTest(self): pass
        observation = {}
        suite = unittest.TestSuite([MissingId(), KnownId()])
        with self.assertRaisesRegex(ValueError, "root_discovery_unresolved_identity"):
            driver.checked_case_ids(suite, observation=observation)
        self.assertEqual(len(list(driver.iter_cases(suite))), 2)
        self.assertEqual(driver.discovery_summary(observation)["discovered_tests"], 2)
        self.assertEqual(observation["canonical_ids"], [None, KnownId().id()])

    def test_identity_exception_text_is_not_used_as_reason_code(self):
        class BadId(unittest.TestCase):
            def runTest(self): pass
            def id(self): raise driver.TestIdentityError("synthetic-private-error-canary")
        observation = {}
        with self.assertRaisesRegex(ValueError, "root_discovery_unresolved_identity"):
            driver.checked_case_ids(unittest.TestSuite([BadId()]), observation=observation)
        fields = driver.discovery_summary(observation)
        self.assertEqual(fields["discovered_tests"], 1)
        self.assertNotIn("synthetic-private-error-canary", json.dumps(fields))
        self.assertEqual(fields["test_identity_errors"][0]["reason"], "test_identity_unavailable")

    def test_true_empty_discovery_reports_zero_not_unknown(self):
        observation = {}
        with self.assertRaisesRegex(ValueError, "root_discovery_empty_or_duplicate_ids"):
            driver.checked_case_ids(unittest.TestSuite(), observation=observation)
        fields = driver.discovery_summary(observation)
        self.assertEqual(fields["discovered_tests"], 0)
        self.assertEqual(fields["discovered_test_ids_sha256"], driver.source.digest(driver.source.canonical([])))

    def test_discovery_failure_before_count_reports_null_not_zero(self):
        observation = {}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ImportError):
                driver.collect_unfiltered(Path(directory), observation=observation)
        fields = driver.discovery_summary(observation)
        self.assertEqual(fields["discovery_status"], "failed_before_count")
        self.assertIsNone(fields["discovered_tests"])
        self.assertIsNone(fields["discovered_test_ids_sha256"])
        self.assertIsNone(driver.discovery_summary({})["discovered_tests"])

    def test_main_serializes_persistent_discovery_observation_on_failure(self):
        tree = ast.parse(Path(driver.__file__).read_bytes())
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        calls = [node for node in ast.walk(main) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
        collect = next(node for node in calls if node.func.id == "collect_unfiltered")
        self.assertIn("discovery_observation", [ast.unparse(item.value) for item in collect.keywords if item.arg == "observation"])
        summaries = [node for node in calls if node.func.id == "discovery_summary"]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(ast.unparse(summaries[0].args[0]), "discovery_observation")

    def test_all_18_original_function_cases_are_retained_without_execution(self):
        from tests import test_eval_v2_public_runner as original
        with patch.object(original, "_run_function_test", side_effect=AssertionError("original bodies must not run")) as run:
            suite = original.load_tests(unittest.TestLoader(), unittest.TestSuite(), "test*.py")
            cases = list(driver.iter_cases(suite))
            expected_names = sorted(name for name, value in vars(original).items() if name.startswith("test_") and callable(value))
            self.assertEqual(len(cases), 18)
            self.assertEqual([case.shortDescription() for case in cases], expected_names)
            self.assertEqual(list(driver.case_ids(suite)), ["<lambda>"] * 18)
            identities = driver.checked_case_ids(suite)
            self.assertEqual(len(identities), 18)
            self.assertEqual(len(set(identities)), 18)
            self.assertEqual(list(driver.iter_cases(suite)), cases)
        run.assert_not_called()
        relative = "tests/test_eval_v2_public_runner.py"
        fixed = subprocess.check_output(["git", "--no-replace-objects", "-C", str(driver.ROOT),
            "show", driver.BASE + ":" + relative], timeout=30)
        self.assertEqual((driver.ROOT / relative).read_bytes(), fixed)

    def test_import_failure_remains_a_failed_case_not_filtered_out(self):
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromName("tests.synthetic_missing_module_for_identity_check")
        self.assertEqual(len(loader.errors), 1)
        self.assertEqual(len(driver.checked_case_ids(suite)), 1)
        result = driver.run_suite(suite, io.StringIO())
        self.assertEqual((result.testsRun, len(result.errors)), (1, 1))
        self.assertFalse(result.wasSuccessful())
