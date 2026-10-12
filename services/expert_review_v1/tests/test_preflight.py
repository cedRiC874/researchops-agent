from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import test_telemetry as fixtures
from researchops import eval_v2_interactive_telemetry as telemetry
from researchops.audit import AuditLedger
from researchops.eval_v2_provider_executor import EvalV2ProviderExecutor
from researchops.model_providers import DeepSeekProvider
from researchops_expert_review import __main__ as cli, core, web
from tests.test_eval_v2_interactive_executor import response_payload


class ExpertPreflightTests(unittest.TestCase):
    setUp = fixtures.InteractiveServiceTests.setUp
    _registry = fixtures.InteractiveServiceTests._registry
    review = fixtures.InteractiveServiceTests.review
    online = fixtures.InteractiveServiceTests.online
    assert_private = fixtures.InteractiveServiceTests.assert_private

    def snapshot(self):
        return {path.relative_to(self.session).as_posix(): path.read_bytes()
                for path in self.session.rglob("*") if path.is_file()}

    def test_environment_refusal_does_not_consume_question_or_write_records(self):
        review = self.review()
        before = self.snapshot()
        for name, code in (("_verify_loaded_sources", "eval_v2_interactive_source_required"),
                           ("_verify_clean_commit", "eval_v2_interactive_dirty_tree")):
            with self.subTest(gate=name), patch.object(telemetry, name, side_effect=telemetry.InteractiveTelemetryError(code, "请检查来源并提交代码。")):
                with patch.object(core.os.environ, "get", side_effect=AssertionError("No Key read")) as key, patch.object(DeepSeekProvider, "open_model") as opened, patch.object(review, "_lock", side_effect=AssertionError("No lock file before preflight")) as locked:
                    with self.assertRaises(core.ExpertReviewError) as rejected:
                        review.ask(confirm_online=True, dataset_id=fixtures.DATASET, question=fixtures.QUESTION)
                self.assertEqual(rejected.exception.code, code)
                self.assertEqual(self.snapshot(), before)
                key.assert_not_called()
                opened.assert_not_called()
                locked.assert_not_called()
        with self.online(payload=response_payload(text=fixtures.ANSWER)) as calls:
            record = review.ask(confirm_online=True, dataset_id=fixtures.DATASET, question=fixtures.QUESTION)
        self.assertEqual(record["status"], "完成")
        self.assertEqual(len(calls), 1)
        self.assert_private()

    def test_model_and_provider_are_rechecked_before_reservation(self):
        review = self.review()
        before = self.snapshot()
        for attribute, value in (("model_id", "unsupported-model"), ("_provider", type("OtherProvider", (), {"provider_id": "openai"})())):
            with self.subTest(attribute=attribute), patch.object(review, attribute, value):
                with patch.object(core.os.environ, "get", side_effect=AssertionError("No Key read")) as key:
                    with self.assertRaises(core.ExpertReviewError) as rejected:
                        review.ask(confirm_online=True, dataset_id=fixtures.DATASET, question=fixtures.QUESTION)
                self.assertEqual(rejected.exception.code, "eval_v2_interactive_provider_denied")
                self.assertEqual(self.snapshot(), before)
                key.assert_not_called()

    def test_preparation_repeats_preflight_after_reservation(self):
        review = self.review()
        refused = telemetry.InteractiveTelemetryError("eval_v2_interactive_dirty_tree", "代码已变化，请提交。")
        with patch.object(telemetry, "_verify_clean_commit", side_effect=["a" * 40, refused]) as clean, patch.object(core.os.environ, "get", side_effect=AssertionError("No Key read")) as key:
            record = review.ask(confirm_online=True, dataset_id=fixtures.DATASET, question=fixtures.QUESTION)
        self.assertEqual(clean.call_count, 2)
        self.assertEqual(record["status"], "出错")
        self.assertEqual(record["error_code"], refused.code)
        key.assert_not_called()
        with self.assertRaises(core.ExpertReviewError) as duplicate:
            review.ask(confirm_online=True, dataset_id=fixtures.DATASET, question=fixtures.QUESTION)
        self.assertEqual(duplicate.exception.code, "question_already_run")

    def test_web_command_reports_chinese_failure_without_starting_server(self):
        review = self.review()
        parser = cli._parser()
        before = self.snapshot()
        error = io.StringIO()
        with patch.object(cli, "_parser", return_value=parser), patch.object(cli, "ExpertReview", return_value=review), patch.object(telemetry, "_verify_clean_commit", side_effect=telemetry.InteractiveTelemetryError("eval_v2_interactive_dirty_tree", "请先提交代码。")):
            with patch.object(core.os.environ, "get", side_effect=AssertionError("No Key read")) as key, patch.object(web, "ThreadingHTTPServer") as server, redirect_stderr(error):
                result = cli.main(["web", "--session-dir", str(self.session), "--registry", str(self.registry), "--provider", "deepseek", "--model", "deepseek-v4-flash"])
        self.assertEqual(result, 1)
        self.assertIn("请先提交代码", error.getvalue())
        self.assertNotIn(str(self.directory), error.getvalue())
        server.assert_not_called()
        key.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_web_command_passes_preflight_before_binding(self):
        review = self.review()
        order = []
        original = review.preflight
        def preflight():
            result = original()
            order.append("preflight")
            return result
        def bind(*args, **kwargs):
            self.assertEqual(order, ["preflight"])
            order.append("bind")
            from unittest.mock import Mock
            server = Mock()
            server.serve_forever.side_effect = KeyboardInterrupt
            return server
        with patch.object(review, "preflight", preflight), patch.object(web, "ThreadingHTTPServer", side_effect=bind), patch("builtins.print"):
            web.serve(review)
        self.assertEqual(order, ["preflight", "bind"])

    def test_failed_owner_with_complete_sdk_output_never_displays_answer(self):
        original = EvalV2ProviderExecutor.execute
        def execute(executor, *args, **kwargs):
            result = original(executor, *args, **kwargs)
            executor._completion_telemetry_session._owner._failed = True
            return result
        with self.online(payload=response_payload(text=fixtures.ANSWER)) as calls, patch.object(EvalV2ProviderExecutor, "execute", execute):
            record = self.review().ask(confirm_online=True, dataset_id=fixtures.DATASET, question=fixtures.QUESTION)
        self.assertEqual(len(calls), 1)
        self.assertTrue(record["completion_telemetry"]["closure_passed"])
        self.assertEqual(record["completion_status"], "complete")
        self.assertFalse(record["completion_telemetry"]["normal_result"])
        self.assertEqual(record["status"], "出错")
        self.assertNotIn(fixtures.ANSWER, json.dumps(record, ensure_ascii=False))
        self.assertIn("是否完整：否", core.completion_summary(record))

    def test_reserved_preparation_failure_retains_summary_without_key_or_retry(self):
        for index, (target, name) in enumerate(((AuditLedger, "__init__"), (AuditLedger, "append_event"),
                                               (telemetry.EvalV2InteractiveTelemetrySession, "__init__"))):
            question = fixtures.QUESTION + str(index)
            with self.subTest(stage=name), patch.object(target, name, side_effect=OSError("PRIVATE-SOURCE-PATH-CANARY")):
                with patch.object(core.os.environ, "get", side_effect=AssertionError("No Key read")) as key:
                    record = self.review().ask(confirm_online=True, dataset_id=fixtures.DATASET, question=question)
            self.assertEqual(record["status"], "出错")
            self.assertEqual(record["error_code"], telemetry.INCOMPLETE_CODE)
            summary = record["completion_telemetry"]
            self.assertTrue(summary["write_failed"])
            self.assertFalse(summary["normal_result"])
            self.assertRegex(summary["run_id"], r"^RUN-INTERACTIVE-[0-9a-f]{32}$")
            self.assertRegex(summary["plan_commitment_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(summary["git_commit"], "a" * 40)
            self.assertNotIn("PRIVATE-SOURCE-PATH-CANARY", json.dumps(record, ensure_ascii=False))
            key.assert_not_called()
            with self.assertRaises(core.ExpertReviewError) as duplicate:
                self.review().ask(confirm_online=True, dataset_id=fixtures.DATASET, question=question)
            self.assertEqual(duplicate.exception.code, "question_already_run")


if __name__ == "__main__":
    unittest.main()
