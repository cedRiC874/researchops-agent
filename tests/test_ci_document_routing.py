"""Fail-closed CI routing tests; fixtures use only local, isolated Git repos."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / ".github" / "ci" / "document_scope.py"
SPEC = importlib.util.spec_from_file_location("ci_document_scope_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scope)


def process_environment():
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
               "USERPROFILE", "HOME", "LOCALAPPDATA", "APPDATA", "HOMEDRIVE", "HOMEPATH", "LANG"}
    result = {key: os.environ[key] for key in os.environ if key.upper() in allowed}
    result.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", OPENAI_AGENTS_DISABLE_TRACING="1")
    return result


def raw_change(
    path: bytes = b"README.md",
    *,
    old_mode: bytes = b"100644",
    new_mode: bytes = b"100644",
    status: bytes = b"M",
    second_path: bytes | None = None,
) -> bytes:
    record = b":" + old_mode + b" " + new_mode
    record += b" " + b"1" * 40 + b" " + b"2" * 40 + b" " + status + b"\0" + path + b"\0"
    return record if second_path is None else record + second_path + b"\0"


class DocumentRawDiffTests(unittest.TestCase):
    def assert_full(self, raw: bytes) -> None:
        result = scope.classify_raw_diff(raw)
        self.assertEqual(result["mode"], "full")
        self.assertIs(result["run_full"], True)
        self.assertTrue(result["reason"])

    def test_only_exact_allowlisted_regular_modifications_are_light(self) -> None:
        raw = b"".join(raw_change(name.encode()) for name in scope.PRESENTATION_DOCS)
        result = scope.classify_raw_diff(raw)
        self.assertEqual(result["mode"], "docs")
        self.assertIs(result["run_full"], False)
        self.assertEqual(result["changed_count"], len(scope.PRESENTATION_DOCS))
        self.assertEqual(result["diff_sha256"], hashlib.sha256(raw).hexdigest())

    def test_source_dependencies_tests_contracts_and_workflows_are_full(self) -> None:
        for name in (
            "src/researchops/agent.py", "requirements.lock", "requirements.linux.lock",
            "pyproject.toml", "tests/test_example.py", "evals/CONTRACT.md",
            "docs/FROZEN_CONTRACT.md", ".github/workflows/ci.yml",
            ".github/ci/document_scope.py", ".devcontainer/Dockerfile", "probe_out_v3.json",
        ):
            with self.subTest(path=name):
                self.assert_full(raw_change(name.encode()))

    def test_mixed_diff_cannot_hide_code_after_document(self) -> None:
        self.assert_full(raw_change() + raw_change(b"src/researchops/agent.py"))

    def test_unknown_markdown_is_not_blanket_exempted(self) -> None:
        self.assert_full(raw_change(b"docs/new_contract.md"))

    def test_paths_are_case_sensitive_and_not_normalized_into_allowlist(self) -> None:
        for path in (b"readme.md", b"./README.md", b"docs/../README.md", b"docs\\README.md", b"README.md/child", b"/README.md"):
            with self.subTest(path=path):
                self.assert_full(raw_change(path))

    def test_additions_deletions_type_changes_and_unmerged_entries_are_full(self) -> None:
        for status in (b"A", b"D", b"T", b"U", b"X", b"B"):
            with self.subTest(status=status):
                self.assert_full(raw_change(status=status))

    def test_rename_and_copy_are_full_even_when_both_names_are_allowlisted(self) -> None:
        for status in (b"R100", b"R050", b"C100"):
            with self.subTest(status=status):
                self.assert_full(raw_change(status=status, second_path=b"README.en.md"))

    def test_executable_symlink_gitlink_and_mode_changes_are_full(self) -> None:
        for old_mode, new_mode in (
            (b"100644", b"100755"), (b"100755", b"100644"),
            (b"100755", b"100755"), (b"120000", b"120000"),
            (b"100644", b"120000"), (b"160000", b"160000"),
            (b"000000", b"100644"), (b"100644", b"000000"),
        ):
            with self.subTest(old=old_mode, new=new_mode):
                self.assert_full(raw_change(old_mode=old_mode, new_mode=new_mode))

    def test_empty_diff_is_full(self) -> None:
        self.assert_full(b"")

    def test_incomplete_raw_records_are_full(self) -> None:
        valid = raw_change()
        for raw in (valid[:-1], valid.split(b"\0")[0], valid + b":100644", valid + b"\0"):
            with self.subTest(raw=raw):
                self.assert_full(raw)

    def test_invalid_utf8_and_control_characters_are_full(self) -> None:
        for path in (b"README.\xffmd", b"README.md\n", b"README.md\r", b"README.md\t", b"README.md\x1b", b"README.md\x7f"):
            with self.subTest(path=path):
                self.assert_full(raw_change(path))

    def test_duplicate_raw_paths_do_not_become_doc_only(self) -> None:
        self.assert_full(raw_change() + raw_change())

    def test_diff_after_three_hundred_records_is_not_truncated(self) -> None:
        raw = b"".join(raw_change(f"docs/item-{n}.md".encode()) for n in range(301))
        raw += raw_change(b"src/researchops/agent.py")
        self.assert_full(raw)


class DocumentRouteGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="ci-doc-route-")
        self.addCleanup(self._directory.cleanup)
        self.repo = Path(self._directory.name).resolve()
        self.git_executable = shutil.which("git")
        self.assertIsNotNone(self.git_executable, "CI routing checks require the Git used by CI")
        self.git("init", "-b", "main")
        (self.repo / "README.md").write_text("# Original\n", encoding="utf-8")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.git("add", "README.md", "src/sample.py")
        self.git("commit", "-m", "synthetic base")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-b", "docs-fixture")
        (self.repo / "README.md").write_text("# Modified\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-m", "synthetic document change")
        self.head = self.git("rev-parse", "HEAD").strip()

    def git(self, *args: str) -> str:
        completed = subprocess.run(
            [self.git_executable, "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
             "-c", "user.name=Offline CI Fixture", "-c", "user.email=ci-fixture@example.invalid", *args],
            cwd=self.repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            timeout=30,
        )
        return completed.stdout.decode("utf-8", errors="strict")

    def push_event(self, before: str | None = None) -> dict:
        return {"before": before or self.base, "after": self.head, "ref": "refs/heads/docs-fixture", "created": False, "deleted": False}

    def pr_event(self) -> dict:
        return {"pull_request": {"base": {"sha": self.base}, "head": {"sha": self.head}}}

    def run_route(self, name: str, event: dict, *, ref: str = "refs/heads/docs-fixture") -> dict:
        return scope.route(self.repo, name, event, self.head, ref)

    def test_existing_branch_push_uses_complete_before_after_diff(self) -> None:
        result = self.run_route("push", self.push_event())
        self.assertEqual(result["mode"], "docs")
        self.assertEqual(result["changed_count"], 1)

    def test_pull_request_uses_base_head_not_synthetic_merge_ref(self) -> None:
        result = self.run_route("pull_request", self.pr_event(), ref="refs/pull/41/merge")
        self.assertEqual(result["mode"], "docs")

    def test_main_push_uses_same_document_only_rule(self) -> None:
        event = dict(self.push_event(), ref="refs/heads/main")
        result = self.run_route("push", event, ref="refs/heads/main")
        self.assertEqual(result["mode"], "docs")
        self.assertIs(result["run_full"], False)

    def test_main_push_with_code_still_requires_full(self) -> None:
        (self.repo / "src/sample.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.git("add", "src/sample.py")
        self.git("commit", "-m", "synthetic source change")
        self.head = self.git("rev-parse", "HEAD").strip()
        event = dict(self.push_event(), ref="refs/heads/main")
        self.assertEqual(self.run_route("push", event, ref="refs/heads/main")["mode"], "full")

    def test_manual_dispatch_requires_full(self) -> None:
        result = self.run_route("workflow_dispatch", {})
        self.assertEqual(result["mode"], "full")

    def test_new_branch_push_requires_full(self) -> None:
        event = self.push_event("0" * 40)
        event["created"] = True
        self.assertEqual(self.run_route("push", event)["mode"], "full")

    def test_missing_base_git_object_requires_full(self) -> None:
        self.assertEqual(self.run_route("push", self.push_event("3" * 40))["mode"], "full")

    def test_missing_event_fields_and_unknown_event_require_full(self) -> None:
        missing_base = self.push_event()
        missing_base.pop("before")
        for name, event in (("push", missing_base), ("pull_request", {}), ("schedule", {})):
            with self.subTest(name=name):
                self.assertEqual(self.run_route(name, event)["mode"], "full")

    def test_missing_or_mismatched_push_identity_rejects(self) -> None:
        for altered in ({}, {"after": self.base}, {"ref": "refs/heads/unexpected"}):
            with self.subTest(altered=altered):
                event = self.push_event() if altered else {}
                event.update(altered)
                with self.assertRaises(scope.ScopeError):
                    self.run_route("push", event)

    def test_worktree_head_mismatch_rejects_instead_of_verifying_wrong_tree(self) -> None:
        with self.assertRaises(scope.ScopeError):
            scope.route(self.repo, "push", self.push_event(), self.base, "refs/heads/docs-fixture")

    def test_event_head_mismatch_rejects(self) -> None:
        event = self.pr_event()
        event["pull_request"]["head"]["sha"] = self.base
        with self.assertRaises(scope.ScopeError):
            self.run_route("pull_request", event, ref="refs/pull/41/merge")

    def test_pull_request_cumulative_code_change_is_not_hidden_by_last_doc_commit(self) -> None:
        (self.repo / "src" / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.git("add", "src/sample.py")
        self.git("commit", "-m", "synthetic code change")
        (self.repo / "README.md").write_text("# Latest docs only\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-m", "synthetic final docs change")
        self.head = self.git("rev-parse", "HEAD").strip()
        result = self.run_route("pull_request", self.pr_event(), ref="refs/pull/41/merge")
        self.assertEqual(result["mode"], "full")

    def test_real_git_mode_only_change_requires_full(self) -> None:
        self.git("update-index", "--chmod=+x", "README.md")
        self.git("commit", "-m", "synthetic executable document")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.assertEqual(self.run_route("push", self.push_event())["mode"], "full")

    def test_real_git_rename_requires_full(self) -> None:
        self.git("mv", "README.md", "README.en.md")
        self.git("commit", "-m", "synthetic rename")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.assertEqual(self.run_route("push", self.push_event())["mode"], "full")

    def test_git_diff_failure_routes_full_without_masking_head_identity(self) -> None:
        original = scope.git_read
        observed = []

        def reject_diff(repo, *args):
            observed.append(args[0])
            if args[0] == "diff":
                raise scope.ScopeError("git_read_failed")
            return original(repo, *args)

        with mock.patch.object(scope, "git_read", side_effect=reject_diff):
            result = self.run_route("push", self.push_event())
        self.assertEqual(result["mode"], "full")
        self.assertIn("rev-parse", observed)
        self.assertIn("diff", observed)

    def test_multiple_merge_bases_are_not_arbitrarily_selected(self) -> None:
        original = scope.git_read
        observed = []

        def multiple_bases(repo, *args):
            observed.append(args[0])
            if args[0] == "merge-base":
                return (self.base + "\n" + self.head + "\n").encode("ascii")
            return original(repo, *args)

        with mock.patch.object(scope, "git_read", side_effect=multiple_bases):
            result = self.run_route("pull_request", self.pr_event())
        self.assertEqual(result["mode"], "full")
        self.assertIn("merge-base", observed)
        self.assertNotIn("diff", observed)

    def run_cli(self, *, event=None, expected_head=None, report=None, output=None):
        event_path = self.repo / "event.json"
        event_path.write_text(json.dumps(self.push_event() if event is None else event), encoding="utf-8")
        args = [sys.executable, "-I", "-B", str(MODULE_PATH), "route", "--repo", str(self.repo),
                "--event", str(event_path), "--event-name", "push", "--ref", "refs/heads/docs-fixture",
                "--head", expected_head or self.head]
        if report is not None:
            args += ["--report", str(report)]
        if output is not None:
            args += ["--github-output", str(output)]
        return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=process_environment(), timeout=60, check=False)

    def test_cli_emits_exact_outputs_after_persisting_real_git_decision(self) -> None:
        report, output = self.repo / "scope.json", self.repo / "github-output"
        child = self.run_cli(report=report, output=output)
        self.assertEqual(child.returncode, 0, child.stderr)
        record = json.loads(report.read_bytes())
        self.assertEqual(record["checkout_commit"], self.head)
        self.assertEqual(record["comparison_base"], self.base)
        self.assertEqual(record["mode"], "docs")
        self.assertEqual(output.read_text(encoding="utf-8"), "mode=docs\nrun_full=false\n")

    def test_cli_report_io_failure_cannot_emit_success_outputs(self) -> None:
        report, output = self.repo / "scope.json", self.repo / "github-output"
        report.write_text("preserve existing evidence", encoding="utf-8")
        child = self.run_cli(report=report, output=output)
        self.assertEqual(child.returncode, 2)
        self.assertEqual(json.loads(child.stdout)["error_code"], "record_or_encoding_failure")
        self.assertFalse(output.exists())
        self.assertEqual(report.read_text(encoding="utf-8"), "preserve existing evidence")

    def test_cli_checkout_mismatch_has_no_success_output(self) -> None:
        report, output = self.repo / "scope.json", self.repo / "github-output"
        child = self.run_cli(expected_head=self.base, report=report, output=output)
        self.assertEqual(child.returncode, 2)
        self.assertEqual(json.loads(child.stdout)["error_code"], "checkout_identity")
        self.assertFalse(report.exists())
        self.assertFalse(output.exists())

    def test_cli_output_io_failure_is_nonzero_even_with_saved_decision(self) -> None:
        report = self.repo / "scope.json"
        child = self.run_cli(report=report, output=self.repo)
        self.assertEqual(child.returncode, 2)
        self.assertEqual(json.loads(child.stdout)["error_code"], "record_or_encoding_failure")
        self.assertEqual(json.loads(report.read_bytes())["mode"], "docs")


def needs_result(mode: str, *, full_jobs: tuple[str, ...] = ("heavy",)) -> dict:
    result = {
        "change-scope": {"result": "success", "outputs": {"mode": mode, "run_full": "true" if mode == "full" else "false"}},
        "presentation-docs": {"result": "success", "outputs": {}},
    }
    result.update({name: {"result": "success" if mode == "full" else "skipped", "outputs": {}} for name in full_jobs})
    return result


class DocumentResultGateTests(unittest.TestCase):
    def test_full_route_requires_all_heavy_jobs_success(self) -> None:
        scope.check_results(needs_result("full", full_jobs=("linux", "windows")), ("linux", "windows"))

    def test_docs_route_accepts_only_intentional_heavy_skips(self) -> None:
        scope.check_results(needs_result("docs"), ("heavy",))

    def test_expected_heavy_cannot_skip_fail_cancel_or_timeout(self) -> None:
        for outcome in ("skipped", "failure", "cancelled", "timed_out", "neutral", "", None):
            with self.subTest(outcome=outcome):
                needs = needs_result("full")
                needs["heavy"]["result"] = outcome
                with self.assertRaises(scope.ScopeError):
                    scope.check_results(needs, ("heavy",))

    def test_route_failure_cannot_be_hidden_by_skipped_dependents(self) -> None:
        for outcome in ("failure", "cancelled", "skipped", "", None):
            with self.subTest(outcome=outcome):
                needs = needs_result("docs")
                needs["change-scope"]["result"] = outcome
                with self.assertRaises(scope.ScopeError):
                    scope.check_results(needs, ("heavy",))

    def test_light_validation_must_actually_pass(self) -> None:
        for outcome in ("failure", "cancelled", "skipped", "neutral", None):
            with self.subTest(outcome=outcome):
                needs = needs_result("docs")
                needs["presentation-docs"]["result"] = outcome
                with self.assertRaises(scope.ScopeError):
                    scope.check_results(needs, ("heavy",))

    def test_unknown_or_missing_mode_rejects(self) -> None:
        for mode in ("", "unknown", "DOCS", "false", None):
            with self.subTest(mode=mode):
                needs = needs_result("docs")
                needs["change-scope"]["outputs"]["mode"] = mode
                with self.assertRaises(scope.ScopeError):
                    scope.check_results(needs, ("heavy",))

    def test_missing_job_does_not_count_as_skipped(self) -> None:
        for name in ("change-scope", "presentation-docs", "heavy"):
            with self.subTest(name=name):
                needs = needs_result("docs")
                del needs[name]
                with self.assertRaises(scope.ScopeError):
                    scope.check_results(needs, ("heavy",))

    def test_docs_route_cannot_accept_unexpected_heavy_result(self) -> None:
        for outcome in ("success", "failure", "cancelled"):
            with self.subTest(outcome=outcome):
                needs = needs_result("docs")
                needs["heavy"]["result"] = outcome
                with self.assertRaises(scope.ScopeError):
                    scope.check_results(needs, ("heavy",))

    def test_mode_and_boolean_string_must_agree(self) -> None:
        for value in ("true", True, False, "", None):
            with self.subTest(value=value):
                needs = needs_result("docs")
                needs["change-scope"]["outputs"]["run_full"] = value
                with self.assertRaises(scope.ScopeError):
                    scope.check_results(needs, ("heavy",))

    def test_unaccounted_jobs_and_duplicate_required_jobs_reject(self) -> None:
        needs = needs_result("full")
        needs["unexpected"] = {"result": "failure"}
        with self.assertRaises(scope.ScopeError):
            scope.check_results(needs, ("heavy",))
        with self.assertRaises(scope.ScopeError):
            scope.check_results(needs_result("full"), ("heavy", "heavy"))

    def test_cli_gate_propagates_passed_scope_without_claiming_skips_are_runs(self) -> None:
        for mode in ("docs", "full"):
            with self.subTest(mode=mode):
                environment = process_environment()
                environment["CI_NEEDS_JSON"] = json.dumps(needs_result(mode))
                child = subprocess.run(
                    [sys.executable, "-I", "-B", str(MODULE_PATH), "check-results", "--full-job", "heavy"],
                    env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=30, check=False,
                )
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertEqual(json.loads(child.stdout)["full_jobs_executed"], mode == "full")

    def test_cli_gate_returns_nonzero_for_required_cancelled_job(self) -> None:
        environment = process_environment()
        needs = needs_result("full")
        needs["heavy"]["result"] = "cancelled"
        environment["CI_NEEDS_JSON"] = json.dumps(needs)
        child = subprocess.run(
            [sys.executable, "-I", "-B", str(MODULE_PATH), "check-results", "--full-job", "heavy"],
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, check=False,
        )
        self.assertEqual(child.returncode, 2)
        self.assertEqual(json.loads(child.stdout)["error_code"], "required_job_outcome")


class PresentationDocumentValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="ci-doc-content-")
        self.addCleanup(self._directory.cleanup)
        self.repo = Path(self._directory.name).resolve()
        self.git_executable = shutil.which("git")
        self.assertIsNotNone(self.git_executable, "CI document checks require Git")
        self.git("init", "-b", "main")
        common = "Python 3.12; Windows x86-64; Linux x86-64; requirements.linux.lock\n"
        docs = {
            "README.md": "# Synthetic\n" + common + "macOS 与 ARM\nscripts\\portfolio_demo.ps1\nscripts/portfolio_demo.sh\n[Docs](docs/README.md)\n",
            "README.en.md": "# Synthetic\n" + common + "macOS and ARM\n[Docs](docs/README.md)\n",
            "STATUS.md": "# Synthetic status\n状态：`open / cross-cutting / causal attribution incomplete`\n",
            "docs/PORTFOLIO.md": "# Synthetic portfolio\n",
            "docs/README.md": "# Synthetic navigation\n[Portfolio](PORTFOLIO.md)\n",
            "docs/CODESPACES.md": "# Synthetic container guide\n",
            "docs/DEMO.md": "# Synthetic demonstration\n",
            "docs/RESEARCHOPS_INTERVIEW_GUIDE.md": "See [portfolio](PORTFOLIO.md).\n",
        }
        self.assertEqual(set(docs), set(scope.PRESENTATION_DOCS))
        for name, text in docs.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.git("add", *docs)
        self.git("commit", "-m", "synthetic presentation contract")

    def git(self, *args: str) -> str:
        completed = subprocess.run(
            [self.git_executable, "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
             "-c", "user.name=Offline CI Fixture", "-c", "user.email=ci-fixture@example.invalid", *args],
            cwd=self.repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            timeout=30,
        )
        return completed.stdout.decode("utf-8", errors="strict")

    def commit_text(self, name: str, text: str) -> None:
        (self.repo / name).write_text(text, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-m", "synthetic document variation")

    def assert_document_error(self, code: str) -> None:
        with self.assertRaises(scope.ScopeError) as failure:
            scope.check_documents(self.repo)
        self.assertEqual(failure.exception.code, code)

    def test_valid_fixed_git_documents_pass_without_full_or_provider(self) -> None:
        result = scope.check_documents(self.repo)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(set(result["files"]), set(scope.PRESENTATION_DOCS))
        self.assertIs(result["full_regression"], False)
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(result["checkout_commit"], self.git("rev-parse", "HEAD").strip())

    def test_document_check_binds_git_blobs_not_uncommitted_display_edits(self) -> None:
        before = scope.check_documents(self.repo)
        (self.repo / "README.md").write_text("uncommitted broken link", encoding="utf-8")
        after = scope.check_documents(self.repo)
        self.assertEqual(before["files"], after["files"])

    def test_broken_local_link_rejects(self) -> None:
        self.commit_text("docs/DEMO.md", "[Missing](missing.md)\n")
        self.assert_document_error("document_link_missing")

    def test_status_without_open_marker_rejects(self) -> None:
        self.commit_text("STATUS.md", "# Everything is complete\n")
        self.assert_document_error("status_open_contract")

    def test_platform_claim_regression_rejects(self) -> None:
        original = (self.repo / "README.en.md").read_text(encoding="utf-8")
        self.commit_text("README.en.md", original.replace("Python 3.12", "Python 3.11+"))
        self.assert_document_error("readme_platform_contract")

    def test_document_must_not_be_executable(self) -> None:
        self.git("update-index", "--chmod=+x", "docs/README.md")
        self.git("commit", "-m", "synthetic executable document")
        self.assert_document_error("document_not_regular")

    def test_symlink_tree_entry_rejects_without_following_it(self) -> None:
        # This unlinked page reaches the document-type check, not the earlier link check.
        oid = self.git("rev-parse", "HEAD:docs/CODESPACES.md").strip()
        self.git("update-index", "--cacheinfo", "120000," + oid + ",docs/CODESPACES.md")
        self.git("commit", "-m", "synthetic link-mode document")
        self.assertEqual(self.git("ls-tree", "HEAD", "docs/CODESPACES.md").split()[0], "120000")
        self.assert_document_error("document_not_regular")

    def test_linked_symlink_tree_entry_hits_link_target_guard(self) -> None:
        oid = self.git("rev-parse", "HEAD:docs/README.md").strip()
        self.git("update-index", "--cacheinfo", "120000," + oid + ",docs/README.md")
        self.git("commit", "-m", "synthetic linked target")
        self.assertEqual(self.git("ls-tree", "HEAD", "docs/README.md").split()[0], "120000")
        self.assert_document_error("document_link_target")

    def test_invalid_utf8_document_rejects(self) -> None:
        (self.repo / "docs/DEMO.md").write_bytes(b"\xff\xfe")
        self.git("add", "docs/DEMO.md")
        self.git("commit", "-m", "synthetic invalid encoding")
        self.assert_document_error("document_encoding")

    def test_external_links_are_checked_without_network(self) -> None:
        self.commit_text("docs/DEMO.md", "[Official](https://example.invalid/docs)\n[Mail](mailto:demo@example.invalid)\n")
        result = scope.check_documents(self.repo)
        self.assertEqual(result["status"], "passed")

    def test_unsafe_link_schemes_reject(self) -> None:
        self.commit_text("docs/DEMO.md", "[Bad](file:///private/file)\n")
        self.assert_document_error("document_link_scheme")

    def test_malformed_external_url_is_a_safe_document_error(self) -> None:
        self.commit_text("docs/DEMO.md", "[Bad](https://[broken)\n")
        self.assert_document_error("document_link_target")

    def test_protocol_relative_url_is_not_a_local_link(self) -> None:
        self.commit_text("docs/DEMO.md", "[Bad](//example.invalid/file)\n")
        self.assert_document_error("document_link_target")

    def test_link_escaping_repository_rejects(self) -> None:
        self.commit_text("docs/DEMO.md", "[Bad](../../outside.md)\n")
        self.assert_document_error("document_link_target")

    def test_empty_document_rejects(self) -> None:
        self.commit_text("docs/DEMO.md", "")
        self.assert_document_error("document_size")


if __name__ == "__main__":
    unittest.main()
