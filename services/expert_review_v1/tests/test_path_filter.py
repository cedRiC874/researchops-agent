from __future__ import annotations

import contextlib
import http.client
import io
import json
import socket
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch

from researchops import self_pilot
from researchops_expert_review import __main__ as cli, core, web
import test_core as core_fixtures


NORMAL_TEXTS = (
    "mg/dl", "mmol/L", "kg/m²", "ml/kg/min", "mL/min/1.73m²", "beats/min",
    "yes/no", "and/or", "male/female", "ca/thal", "297/303", "2026/10/13",
    "(~2%)", "n/a", "治疗组/对照组", "收缩压/舒张压", "治疗组/对照组/安慰剂组", "中位数/四分位距",
)
REAL_PATHS = (
    r"C:\private\data.csv", "C:/data/a.csv", r"\\server\share\x.csv", "file:///C:/x.csv",
    "/home/private/output.csv", "/opt/private", "/opt/data.csv", "路径：/opt/private",
    "文件在/home/user/a.csv", "~/data/x.csv", "./x.csv", "../data/x.csv",
    "data/private", "data/file.csv", "artifacts/prepared/heart.csv", r"data\raw\heart.csv",
)
REDACTION_ORDER = ["file_path", "api_key", "subject_id", "self_pilot_filter"]
UNKNOWN_CANARY = "UNKNOWN-CATEGORY-PRIVATE-CANARY"
BODY_CANARY = "SYNTHETIC-RAW-RESPONSE-CANARY"


def _bilingual(text):
    return "## English\n" + text + "\n## 中文\n仅返回合成汇总。"


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


class _OfflineTestCase(unittest.TestCase):
    def setUp(self):
        original_connect = socket.socket.connect
        original_getaddrinfo = socket.getaddrinfo
        def local_connect(sock, address):
            if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
                raise AssertionError("Offline path-filter tests forbid external sockets")
            return original_connect(sock, address)
        def local_getaddrinfo(host, *args, **kwargs):
            if host not in {"127.0.0.1", "::1", "localhost", None}:
                raise AssertionError("Offline path-filter tests forbid external DNS")
            return original_getaddrinfo(host, *args, **kwargs)
        for target, replacement in (("socket.socket.connect", local_connect), ("socket.getaddrinfo", local_getaddrinfo)):
            guard = patch(target, replacement)
            guard.start()
            self.addCleanup(guard.stop)


class PathFilterMatrixTests(_OfflineTestCase):
    def test_all_18_normal_texts_pass_input_and_output_filters(self):
        self.assertEqual(len(NORMAL_TEXTS), 18)
        for text in NORMAL_TEXTS:
            with self.subTest(text=text):
                self.assertEqual(core.input_rejection_types(text), [])
                # These examples must not depend on changing the inherited filter.
                self.assertEqual(self_pilot._safe_display_output(text), (text, False))
                safe, categories = core.filter_output(text)
                self.assertEqual(safe, text)
                self.assertEqual(categories, [])

    def test_all_16_real_paths_are_rejected_and_output_redacted(self):
        self.assertEqual(len(REAL_PATHS), 16)
        for text in REAL_PATHS:
            with self.subTest(text=text):
                self.assertIn("file_path", core.input_rejection_types(text))
                safe, categories = core.filter_output(text)
                expected = ["file_path"]
                if self_pilot._safe_display_output(text)[1]:
                    expected.append("self_pilot_filter")
                self.assertEqual(categories, expected)
                self.assertNotEqual(safe, text)
                self.assertNotIn(text, safe)

    def test_unc_paths_allow_unicode_server_and_share_segments(self):
        for text in (r"\\服务器\共享", r"\\服\共\x.csv", r"\\server\共享", r"\\服务器\共享\x.csv"):
            with self.subTest(text=text):
                self.assertIsNotNone(core._PATTERNS["file_path"].match(text))
                self.assertEqual(core.input_rejection_types(text), ["file_path"])
                safe, categories = core.filter_output(text)
                self.assertEqual(categories, ["file_path"])
                self.assertNotIn(text, safe)

    def test_redaction_categories_have_fixed_order_and_no_duplicates(self):
        unsafe = r"C:\private\data.csv sk-synthetic-token SUBJ-SYNTHETIC Bearer synthetic-token"
        safe, categories = core.filter_output(unsafe, api_key="sk-synthetic-token")
        self.assertEqual(categories, REDACTION_ORDER)
        self.assertEqual(len(categories), len(set(categories)))
        for part in (r"C:\private\data.csv", "sk-synthetic-token", "SUBJ-SYNTHETIC", "Bearer synthetic-token"):
            self.assertNotIn(part, safe)

    def test_repository_prefixes_and_relative_file_extensions(self):
        directories = ("artifacts", "data", "evals", "src", "services", "output", "docs", "tests", "scripts", ".venv", ".git")
        extensions = ("csv", "tsv", "json", "jsonl", "parquet", "sqlite", "sqlite3", "db", "xlsx", "xls", "txt", "md", "py", "ps1", "yml", "yaml", "toml", "log", "zip", "data")
        paths = [directory + separator + "private" for directory in directories for separator in ("/", "\\")]
        paths += ["custom" + separator + "sample." + extension for extension in extensions for separator in ("/", "\\")]
        paths += [r"~\private", r"..\private", "custom/sample.CSV", "custom/数据.csv"]
        for text in paths:
            with self.subTest(text=text):
                self.assertIn("file_path", core.input_rejection_types(text))
                self.assertIn("file_path", core.filter_output(text)[1])

    def test_path_boundaries_do_not_match_unqualified_text(self):
        for text in ("/single", "~3.5", "\\\\server\\", "metadata/file", "report.csv", "custom/sample.csvx", "custom/sample.csv/child"):
            with self.subTest(text=text):
                self.assertNotIn("file_path", core.input_rejection_types(text))
                self.assertNotIn("file_path", core.filter_output(text)[1])

    def test_literal_process_credential_is_reported_as_api_key(self):
        key = core_fixtures.FAKE_KEY
        text = "Synthetic reply containing " + key
        self.assertFalse(self_pilot._safe_display_output(text)[1])
        safe, categories = core.filter_output(text, api_key=key)
        self.assertEqual(categories, ["api_key"])
        self.assertNotIn(key, safe)
        for missing in (None, ""):
            self.assertEqual(core.filter_output("Synthetic safe summary.", api_key=missing), ("Synthetic safe summary.", []))

    def test_subject_id_and_inherited_only_secret_are_distinct(self):
        self.assertEqual(core.filter_output("SUBJ-SYNTHETIC")[1], ["subject_id"])
        self.assertEqual(core.filter_output("Bearer synthetic-token")[1], ["self_pilot_filter"])

    def test_url_keeps_known_inherited_filter_limitation(self):
        text = "https://example.org/summary"
        self.assertNotIn("file_path", core.input_rejection_types(text))
        inherited, redacted = self_pilot._safe_display_output(text)
        self.assertTrue(redacted)
        safe, categories = core.filter_output(text)
        self.assertEqual(safe, inherited)
        self.assertEqual(categories, ["self_pilot_filter"])

    def test_existing_nonpath_input_rules_are_not_added_to_output_filter(self):
        for text, category in (("synthetic@example.org", "email"), ("13800138000", "phone_number"), ("11010519491231002X", "identity_number")):
            with self.subTest(category=category):
                self.assertIn(category, core.input_rejection_types(text))
                self.assertEqual(core.filter_output(text), (text, []))

    def test_fixed_message_ignores_unknown_categories_and_never_echoes_body(self):
        expected = core.output_redaction_message({"output_redacted": True, "output_redaction_types": ["file_path"]})
        self.assertEqual(expected, "回答被安全过滤器整段屏蔽（命中：疑似本机路径）")
        self.assertEqual(core.output_redaction_message({"output_redacted": True, "output_redaction_types": REDACTION_ORDER}),
                         "回答被安全过滤器整段屏蔽（命中：疑似本机路径、疑似 API Key、疑似受试者编号、原 self-pilot 安全规则）")
        self.assertEqual(core.output_redaction_message({"output_redacted": True}), "回答被安全过滤器整段屏蔽。")
        record = {"output_redacted": True, "output_redaction_types": ["file_path", UNKNOWN_CANARY],
                  "question": BODY_CANARY, "final_output": BODY_CANARY}
        self.assertEqual(core.output_redaction_message(record), expected)
        self.assertNotIn(UNKNOWN_CANARY, expected)
        self.assertNotIn(BODY_CANARY, expected)
        self.assertNotIn("C:", expected)
        self.assertEqual(core.output_redaction_message({"output_redacted": False, "output_redaction_types": []}), "")


class PathFilterRecordTests(_OfflineTestCase):
    def setUp(self):
        super().setUp()
        # Composition keeps the imported TestCase out of this module's discovery.
        self.fixture = core_fixtures.ExpertReviewCoreTests("test_unconfirmed_question_refuses_before_key_read_and_execution")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def _ask(self, question, output, *, failure=None):
        result = SimpleNamespace(final_output=output, outcome="completed", completion_status="complete",
                                 input_tokens=3, output_tokens=4)
        with patch.object(core, "EvalV2ProviderExecutor") as executor, patch.object(
            core.os.environ, "get", side_effect=lambda name, default=None: core_fixtures.FAKE_KEY if name == "DEEPSEEK_API_KEY" else default
        ):
            executor.return_value.execute.return_value = result
            executor.return_value.execute.side_effect = failure
            return self.fixture.review().ask(confirm_online=True, dataset_id=core_fixtures.DATASETS[0], question=question)

    def _stored_strings(self):
        for path in self.fixture.session.glob("*.json"):
            yield from _strings(json.loads(path.read_text(encoding="utf-8")))

    def test_all_normal_samples_can_be_asked_and_persist_without_redaction(self):
        for ordinal, text in enumerate(NORMAL_TEXTS):
            with self.subTest(text=text):
                record = self._ask("Explain the synthetic term " + text + ".", _bilingual(text))
                self.assertEqual(record["status"], "完成")
                self.assertFalse(record["output_redacted"])
                self.assertEqual(record["output_redaction_types"], [])
                self.assertEqual(record["input_rejection_types"], [])
                self.assertIn(text, record["answers"]["english"])
                self.assertEqual(record["sequence"], ordinal + 1)
        self.assertEqual(len(self.fixture.review().history()), 18)

    def test_all_path_inputs_reject_before_key_executor_and_keep_empty_output_categories(self):
        review = self.fixture.review()
        with patch.object(core.os.environ, "get", side_effect=AssertionError("Input rejection must not read credential")) as key, patch.object(core, "EvalV2ProviderExecutor") as executor:
            for text in REAL_PATHS:
                with self.subTest(text=text):
                    record = review.ask(confirm_online=True, dataset_id=core_fixtures.DATASETS[0], question="Inspect " + text)
                    self.assertEqual(record["status"], "拒绝")
                    self.assertEqual(record["error_code"], "input_format_rejected")
                    self.assertIn("file_path", record["input_rejection_types"])
                    self.assertEqual(record["output_redaction_types"], [])
                    self.assertFalse(record["output_redacted"])
        key.assert_not_called()
        executor.assert_not_called()
        for saved in self._stored_strings():
            for text in REAL_PATHS:
                self.assertNotIn(text, saved)

    def test_all_path_answers_persist_only_redaction_categories_not_original_body(self):
        for ordinal, path in enumerate(REAL_PATHS):
            with self.subTest(path=path):
                raw = _bilingual(BODY_CANARY + " " + path)
                record = self._ask(f"Summarize synthetic aggregate number {ordinal}.", raw)
                self.assertTrue(record["output_redacted"])
                self.assertEqual(record["output_redaction_types"], core.filter_output(raw, api_key=core_fixtures.FAKE_KEY)[1])
                self.assertEqual(record["output_redaction_types"][0], "file_path")
                self.assertNotIn(path, " ".join(_strings(record["answers"])))
        for saved in self._stored_strings():
            self.assertNotIn(BODY_CANARY, saved)
            for path in REAL_PATHS:
                self.assertNotIn(path, saved)

    def test_literal_credential_category_is_saved_without_credential(self):
        record = self._ask("Summarize a synthetic aggregate.", _bilingual(core_fixtures.FAKE_KEY))
        self.assertTrue(record["output_redacted"])
        self.assertEqual(record["output_redaction_types"], ["api_key"])
        for text in self._stored_strings():
            self.assertNotIn(core_fixtures.FAKE_KEY, text)

    def test_execution_failure_also_saves_empty_output_redaction_types(self):
        record = self._ask("Summarize synthetic failing aggregate.", "", failure=RuntimeError(BODY_CANARY))
        self.assertEqual(record["status"], "出错")
        self.assertEqual(record["output_redaction_types"], [])
        self.assertFalse(record["output_redacted"])
        for text in self._stored_strings():
            self.assertNotIn(BODY_CANARY, text)


class PathFilterDisplayTests(_OfflineTestCase):
    @staticmethod
    def _record():
        return {"status": "完成", "answers": {"english": "Answer redacted.", "chinese": "回答已屏蔽。"},
                "tool_call_count": 0, "output_redacted": True,
                "output_redaction_types": ["file_path", UNKNOWN_CANARY], "notice": "无自动评分",
                "question": BODY_CANARY, "provider_payload": BODY_CANARY}

    def test_cli_prints_only_fixed_redaction_message(self):
        record = self._record()
        output = io.StringIO()
        with patch.object(cli, "ExpertReview") as review, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            review.return_value.ask.return_value = record
            code = cli.main(["ask", "--session-dir", "artifacts/synthetic", "--registry", "synthetic.json",
                             "--from-task", "V2-DEV-001", "--provider", "deepseek", "--model", "deepseek-v4-flash", "--confirm-online"])
        self.assertEqual(code, 0)
        self.assertIn(core.output_redaction_message(record), output.getvalue())
        self.assertNotIn(UNKNOWN_CANARY, output.getvalue())
        self.assertNotIn(BODY_CANARY, output.getvalue())

    def test_web_response_adds_fixed_message_without_echoing_unknown_category(self):
        record = self._record()
        review = Mock()
        review.ask.return_value = record
        server = ThreadingHTTPServer(("127.0.0.1", 0), web._build_handler(review, "synthetic-token", 1))
        port = server.server_address[1]
        server.RequestHandlerClass = web._build_handler(review, "synthetic-token", port)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            body = json.dumps({"confirm_online": True, "dataset_id": "synthetic", "question": "Summarize synthetic data."})
            connection.request("POST", "/api/ask", body, headers={"Content-Type": "application/json",
                "X-ResearchOps-Token": "synthetic-token", "Origin": f"http://127.0.0.1:{port}"})
            response = connection.getresponse()
            rendered = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            payload = json.loads(rendered)
            self.assertEqual(payload["output_redaction_message"], core.output_redaction_message(record))
            self.assertNotIn("output_redaction_types", payload)
            self.assertNotIn(UNKNOWN_CANARY, rendered)
            self.assertNotIn(BODY_CANARY, rendered)
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_legacy_unredacted_web_record_keeps_exact_five_fields(self):
        record = self._record()
        record["output_redacted"] = False
        record.pop("output_redaction_types")
        self.assertEqual(set(web._public_result(record)), {"status", "answers", "tool_call_count", "output_redacted", "notice"})


if __name__ == "__main__":
    unittest.main()
