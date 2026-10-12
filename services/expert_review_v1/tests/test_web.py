from __future__ import annotations

import contextlib
import http.client
import io
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

from researchops_expert_review.core import ExpertReviewError
from researchops_expert_review import web


class WebTests(unittest.TestCase):
    def setUp(self):
        self.review = Mock()
        self.review.catalog.return_value = [{"dataset_id": "public-data", "domain": "test", "path": "C:\\private\\prepared.csv"}]
        self.review.examples.return_value = [{"task_id": "V2-DEV-049", "prompt": "Publish an approved summary.", "context": {"bundle_id": "not-for-browser"}}]
        self.review.history.return_value = [{"sequence": 1, "dataset_id": "public-data", "status": "完成", "run_id": "not-for-browser"}]
        self.result = {
            "status": "完成", "answers": {"english": "Summary.", "chinese": "汇总。", "bilingual_complete": True},
            "tool_call_count": 2, "output_redacted": False,
            "notice": "无自动评分；本次执行已结束，发布仍待审批，未实际发布。",
            "question": "never-return-this-question", "provider_request": {"api_key": "sk-never-return-this"},
            "path": "C:\\private\\prepared.csv",
        }
        self.review.ask.return_value = self.result
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web._build_handler(self.review, "test-token", 1))
        self.port = self.server.server_address[1]
        self.server.RequestHandlerClass = web._build_handler(self.review, "test-token", self.port)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)

    def _close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path="/api/ask", payload=None, *, method="POST", headers=None, raw=None):
        request_headers = {
            "Content-Type": "application/json", "X-ResearchOps-Token": "test-token",
            "Origin": f"http://127.0.0.1:{self.port}",
        }
        request_headers.update(headers or {})
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if raw is None and method == "POST" else raw
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            data = response.read().decode("utf-8")
            return response.status, data, dict(response.getheaders())
        finally:
            connection.close()

    @staticmethod
    def question(**updates):
        return {"confirm_online": True, "dataset_id": "public-data", "question": "Summarize the data.", **updates}

    def test_backend_requires_literal_boolean_confirmation(self):
        for value in (None, False, "true", "false", 1, [], {}):
            with self.subTest(value=value):
                status, body, _ = self.request(payload=self.question(confirm_online=value))
                self.assertEqual(status, 400)
                self.assertIn("online_confirmation_required", body)
        missing = self.question()
        missing.pop("confirm_online")
        self.assertEqual(self.request(payload=missing)[0], 400)
        self.review.ask.assert_not_called()

    def test_only_selected_dataset_and_question_are_forwarded(self):
        status, _, _ = self.request(payload=self.question())
        self.assertEqual(status, 200)
        self.review.ask.assert_called_once_with(confirm_online=True, dataset_id="public-data", question="Summarize the data.")

    def test_example_source_uses_task_without_browser_context(self):
        self.assertEqual(self.request(payload={"confirm_online": True, "from_task": "V2-DEV-049"})[0], 200)
        self.review.ask.assert_called_once_with(confirm_online=True, from_task="V2-DEV-049")
        for payload in (
            {"confirm_online": True, "from_task": "V2-DEV-049", "dataset_id": "other"},
            {"confirm_online": True, "from_task": "V2-DEV-049", "context": {"bundle_id": "override"}},
            self.question(model="different-model"),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.request(payload=payload)[0], 400)
        self.assertEqual(self.review.ask.call_count, 1)

    def test_response_exposes_only_safe_display_fields(self):
        status, body, _ = self.request(payload=self.question())
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(set(data), {"status", "answers", "tool_call_count", "output_redacted", "notice"})
        self.assertEqual(data["answers"], {"english": "Summary.", "chinese": "汇总。"})
        self.assertIn("未实际发布", data["notice"])
        for forbidden in ("never-return", "private", "api_key", "provider_request", "question"):
            self.assertNotIn(forbidden, body)

    def test_catalog_history_and_examples_strip_internal_fields(self):
        status, body, _ = self.request("/api/state", method="GET")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state["datasets"], [{"dataset_id": "public-data", "domain": "test"}])
        self.assertEqual(set(state["history"][0]), {"sequence", "dataset_id", "status"})
        self.assertNotIn("private", body)
        status, body, _ = self.request("/api/examples?dataset_id=public-data", method="GET")
        self.assertEqual(status, 200)
        self.review.examples.assert_called_once_with("public-data")
        self.assertEqual(set(json.loads(body)["examples"][0]), {"task_id", "prompt"})
        self.assertNotIn("not-for-browser", body)

    def test_invalid_dataset_error_is_returned_without_execution(self):
        self.review.examples.side_effect = ExpertReviewError("dataset_not_authorized", "数据集不在登记表中。")
        status, body, _ = self.request("/api/examples?dataset_id=unknown", method="GET")
        self.assertEqual(status, 400)
        self.assertIn("dataset_not_authorized", body)
        self.review.ask.assert_not_called()

    def test_host_origin_and_csrf_rejected_before_execution(self):
        for headers in ({"Host": "evil.example"}, {"Host": "127.0.0.1:9"}, {"Origin": "https://evil.example"}, {"Origin": "null"}, {"X-ResearchOps-Token": ""}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(payload=self.question(), headers=headers)[0], 400)
        self.review.ask.assert_not_called()
        self.assertEqual(self.request("/api/state", method="GET", headers={"Host": "evil.example"})[0], 400)

    def test_invalid_or_oversized_json_is_rejected(self):
        for raw in (b"{", b"[]", b"x" * (web._MAX_BODY_BYTES + 1)):
            with self.subTest(length=len(raw)):
                self.assertEqual(self.request(raw=raw)[0], 400)
        self.assertEqual(self.request(payload=self.question(), headers={"Content-Type": "text/plain"})[0], 400)
        self.review.ask.assert_not_called()

    def test_concurrent_submission_is_rejected(self):
        started = threading.Event()
        release = threading.Event()
        first_response = []

        def slow_ask(**kwargs):
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("Test synchronization timeout")
            return self.result

        self.review.ask.side_effect = slow_ask
        first = threading.Thread(target=lambda: first_response.append(self.request(payload=self.question())))
        first.start()
        try:
            self.assertTrue(started.wait(timeout=1))
            status, body, _ = self.request(payload=self.question(question="Another question."))
            self.assertEqual(status, 409)
            self.assertIn("web_run_busy", body)
        finally:
            release.set()
            first.join(timeout=3)
        self.assertEqual(first_response[0][0], 200)
        self.review.ask.assert_called_once()

    def test_persistent_duplicate_refusal_is_presented(self):
        self.review.ask.side_effect = ExpertReviewError("question_already_run", "该问题已提交；不允许重跑。")
        status, body, _ = self.request(payload=self.question())
        self.assertEqual(status, 400)
        self.assertIn("question_already_run", body)

    def test_internal_error_is_not_leaked_or_logged(self):
        self.review.ask.side_effect = RuntimeError("sk-secret C:\\private\\file.csv SUBJ-123")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            status, body, _ = self.request(payload=self.question())
        self.assertEqual(status, 500)
        for forbidden in ("sk-secret", "private", "SUBJ-123"):
            self.assertNotIn(forbidden, body + stderr.getvalue())

    def test_page_confirmation_busy_state_and_safe_rendering(self):
        status, page, headers = self.request("/", method="GET")
        self.assertEqual(status, 200)
        self.assertIn("我确认会把问题文本和汇总统计发送给 DeepSeek", page)
        self.assertIn("不是完备检测", page)
        self.assertIn("无自动评分", page)
        self.assertIn("尚未开放真实在线运行", page)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("重跑</button>", page)
        _, script, _ = self.request("/app.js", method="GET")
        self.assertIn("busy || !$('confirm').checked", script)
        self.assertIn("$('run').disabled = true", script)
        self.assertIn("$('controls').disabled = true", script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)


class ServerBindingTests(unittest.TestCase):
    def test_serve_binds_only_ipv4_loopback(self):
        review = Mock()
        with patch.object(web, "ThreadingHTTPServer") as factory, contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.serve_forever.side_effect = KeyboardInterrupt
            web.serve(review, port=8765)
        self.assertEqual(factory.call_args.args[0], ("127.0.0.1", 8765))
        self.assertTrue(factory.return_value.daemon_threads)
        factory.return_value.server_close.assert_called_once()

    def test_no_host_override_and_invalid_ports_rejected(self):
        with self.assertRaises(TypeError):
            web.serve(Mock(), host="0.0.0.0")
        for port in (0, -1, 65536, True, "8765"):
            with self.subTest(port=port), patch.object(web, "ThreadingHTTPServer") as factory:
                with self.assertRaises(ExpertReviewError):
                    web.serve(Mock(), port=port)
                factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
