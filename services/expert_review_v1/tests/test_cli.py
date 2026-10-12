from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import patch

from researchops_expert_review import __main__ as cli


class CliTests(unittest.TestCase):
    def test_unconfirmed_cli_does_not_read_question_key_or_construct_review(self):
        with patch.object(cli, "ExpertReview") as review, patch.object(cli.Path, "read_text") as read:
            with contextlib.redirect_stderr(io.StringIO()) as error:
                result = cli.main([
                    "ask", "--session-dir", "artifacts/demo", "--registry", "absent.json",
                    "--dataset-id", "dataset", "--question-file", "question.txt",
                    "--provider", "deepseek", "--model", "deepseek-v4-flash",
                ])
        self.assertEqual(result, 1)
        self.assertIn("未读取密钥", error.getvalue())
        review.assert_not_called()
        read.assert_not_called()

    def test_conflicting_task_scope_is_rejected_before_constructing_review(self):
        with patch.object(cli, "ExpertReview") as review, contextlib.redirect_stderr(io.StringIO()):
            result = cli.main([
                "ask", "--session-dir", "artifacts/demo", "--registry", "absent.json",
                "--dataset-id", "dataset", "--from-task", "V2-DEV-049",
                "--provider", "deepseek", "--model", "deepseek-v4-flash", "--confirm-online",
            ])
        self.assertEqual(result, 1)
        review.assert_not_called()

    def test_file_failure_does_not_echo_local_path_or_exception(self):
        with patch.object(cli, "ExpertReview"), patch.object(cli.Path, "read_text", side_effect=OSError("private-file-canary")):
            with contextlib.redirect_stderr(io.StringIO()) as error:
                result = cli.main([
                    "ask", "--session-dir", "artifacts/demo", "--registry", "registry.json",
                    "--dataset-id", "dataset", "--question-file", "question.txt",
                    "--provider", "deepseek", "--model", "deepseek-v4-flash", "--confirm-online",
                ])
        self.assertEqual(result, 1)
        self.assertNotIn("private-file-canary", error.getvalue())


if __name__ == "__main__":
    unittest.main()
