"""Dependency-free CI routing. Not a runtime/Provider admission mechanism."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit


# Exact, reviewed display pages only. Never replace this with **/*.md or docs/**.
PRESENTATION_DOCS = frozenset({
    "README.md", "README.en.md", "STATUS.md", "docs/PORTFOLIO.md",
    "docs/README.md", "docs/CODESPACES.md", "docs/DEMO.md",
    "docs/RESEARCHOPS_INTERVIEW_GUIDE.md",
})
STATUS_MARKER = "状态：`open / cross-cutting / causal attribution incomplete`"
SHA = re.compile(r"[0-9a-f]{40}\Z")
RAW_HEADER = re.compile(rb":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40}) ([0-9a-f]{40}) ([A-Z][0-9]*)\Z")
MAX_GIT_BYTES = 4 * 1024 * 1024
MAX_DOCUMENT_BYTES = 256 * 1024


class ScopeError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def require(value, code):
    if not value:
        raise ScopeError(code)


def valid_sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None and value != "0" * 40


def git_read(repo: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args], cwd=repo,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ScopeError("git_read_timeout") from exc
    except OSError as exc:
        raise ScopeError("git_read_unavailable") from exc
    require(result.returncode == 0, "git_read_failed")
    require(len(result.stdout) <= MAX_GIT_BYTES, "git_read_size_limit")
    return result.stdout


def full(reason, **extra):
    return dict(mode="full", run_full=True, reason=reason, changed_count=None,
                diff_sha256=None, **extra)


def safe_relative(path):
    return (isinstance(path, str) and path and len(path) <= 512
            and "\\" not in path and not any(ord(c) < 32 or ord(c) == 127 for c in path)
            and not path.startswith("/") and ":" not in path
            and all(p not in ("", ".", "..") for p in path.split("/")))


def classify_raw_diff(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_GIT_BYTES or not raw.endswith(b"\0"):
        return full("empty_or_incomplete_diff")
    fields = raw[:-1].split(b"\0")
    if len(fields) % 2 or len(fields) > 32768:
        return full("invalid_diff")
    rows = []
    try:
        for offset in range(0, len(fields), 2):
            header = RAW_HEADER.fullmatch(fields[offset])
            name = fields[offset + 1].decode("utf-8")
            if header is None or not safe_relative(name):
                return full("invalid_diff")
            rows.append((name, header.groups()))
    except UnicodeError:
        return full("invalid_diff")
    if len({name for name, _ in rows}) != len(rows):
        return full("invalid_diff")
    result = dict(mode="full", run_full=True, reason="outside_presentation_allowlist",
                  changed_count=len(rows), diff_sha256=hashlib.sha256(raw).hexdigest())
    if any(name not in PRESENTATION_DOCS for name, _ in rows):
        return result
    if any(old != b"100644" or new != b"100644" or status != b"M" or old_oid == new_oid
           for _, (old, new, old_oid, new_oid, status) in rows):
        result["reason"] = "non_regular_modification"
        return result
    return dict(result, mode="docs", run_full=False, reason="presentation_only")


def route(repo: Path, event_name: str, event: dict, expected_head: str, ref: str) -> dict:
    require(valid_sha(expected_head), "expected_head_invalid")
    actual_head = git_read(repo, "rev-parse", "HEAD").decode("ascii").strip()
    require(actual_head == expected_head, "checkout_identity")
    common = dict(schema="ci-document-scope/1", checkout_commit=actual_head,
                  event=event_name if event_name in ("push", "pull_request", "workflow_dispatch") else "other",
                  comparison_base=None)
    if event_name == "workflow_dispatch":
        return dict(full("manual_dispatch"), **common)
    if not isinstance(event, dict):
        return dict(full("comparison_unavailable"), **common)
    if event_name == "pull_request":
        pr = event.get("pull_request")
        if not isinstance(pr, dict) or not isinstance(pr.get("head"), dict):
            return dict(full("comparison_unavailable"), **common)
        require(pr["head"].get("sha") == expected_head, "event_head_identity")
        base = pr.get("base", {}).get("sha") if isinstance(pr.get("base"), dict) else None
    elif event_name == "push":
        require(event.get("after") == expected_head, "event_head_identity")
        require(event.get("ref") == ref, "event_ref_identity")
        if event.get("created") or event.get("deleted") or event.get("before") == "0" * 40:
            return dict(full("new_or_deleted_branch"), **common)
        base = event.get("before")
    else:
        return dict(full("unhandled_event"), **common)
    if not valid_sha(base):
        return dict(full("comparison_unavailable"), **common)
    try:
        if event_name == "pull_request":
            bases = git_read(repo, "merge-base", "--all", base, expected_head).decode("ascii").splitlines()
            if len(bases) != 1 or not valid_sha(bases[0]):
                return dict(full("comparison_unavailable"), **common)
            base = bases[0]
        raw = git_read(repo, "diff", "--raw", "-z", "--no-renames", "--no-abbrev",
                       "--no-ext-diff", "--no-textconv", base, expected_head, "--")
    except (ScopeError, UnicodeError):
        return dict(full("comparison_unavailable"), **common)
    common["comparison_base"] = base
    return dict(classify_raw_diff(raw), **common)


def tree_entries(raw):
    require(raw.endswith(b"\0"), "document_tree_incomplete")
    result = {}
    for item in raw[:-1].split(b"\0"):
        try:
            header, path_bytes = item.split(b"\t", 1)
            mode, kind, oid = header.decode("ascii").split()
            path = path_bytes.decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise ScopeError("document_tree_invalid") from exc
        require(safe_relative(path) and path not in result and valid_sha(oid), "document_tree_invalid")
        result[path] = (mode, kind, oid)
    return result


def validate_document_bytes(name, data, entries):
    require(isinstance(data, bytes) and 0 < len(data) <= MAX_DOCUMENT_BYTES, "document_size")
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise ScopeError("document_encoding") from exc
    require("\0" not in text, "document_encoding")
    # Markdown inline links/images only; no HTTP requests, HTML execution or installs.
    for target in re.findall(r"\]\(([^)\r\n]+)\)", text):
        target = target.split(' "', 1)[0].strip("<>")
        try:
            parsed = urlsplit(target)
        except ValueError as exc:
            raise ScopeError("document_link_target") from exc
        if parsed.scheme:
            require(parsed.scheme in ("https", "http", "mailto"), "document_link_scheme")
            if parsed.scheme != "mailto":
                require(bool(parsed.netloc), "document_link_target")
            continue
        require(not parsed.netloc, "document_link_target")
        if not parsed.path:
            continue
        relative = unquote(parsed.path)
        require(not relative.startswith("/") and "\\" not in relative, "document_link_target")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), relative))
        require(safe_relative(resolved), "document_link_target")
        if resolved in entries:
            require(entries[resolved][0] in ("100644", "100755") and entries[resolved][1] == "blob", "document_link_target")
        else:
            require(any(p.startswith(resolved + "/") for p in entries), "document_link_missing")
    return text


def check_documents(repo: Path) -> dict:
    entries = tree_entries(git_read(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD"))
    texts, hashes = {}, {}
    for name in sorted(PRESENTATION_DOCS):
        require(name in entries and entries[name][:2] == ("100644", "blob"), "document_not_regular")
        data = git_read(repo, "show", "HEAD:" + name)
        texts[name] = validate_document_bytes(name, data, entries)
        hashes[name] = hashlib.sha256(data).hexdigest()
    for name, mac in (("README.md", "macOS 与 ARM"), ("README.en.md", "macOS and ARM")):
        for required in ("Python 3.12", "Windows x86-64", "Linux x86-64", mac, "requirements.linux.lock"):
            require(required in texts[name], "readme_platform_contract")
        require("Python 3.11+" not in texts[name], "readme_platform_contract")
    require("scripts\\portfolio_demo.ps1" in texts["README.md"] and "scripts/portfolio_demo.sh" in texts["README.md"], "readme_platform_contract")
    require(STATUS_MARKER in texts["STATUS.md"], "status_open_contract")
    return dict(schema="ci-presentation-documents/1", status="passed", files=hashes,
                checkout_commit=git_read(repo, "rev-parse", "HEAD").decode("ascii").strip(),
                full_regression=False, provider_calls=0)


def check_results(needs: dict, full_jobs: tuple[str, ...]) -> dict:
    require(isinstance(needs, dict) and full_jobs and len(set(full_jobs)) == len(full_jobs), "results_shape")
    require(set(needs) == {"change-scope", "presentation-docs", *full_jobs}, "results_shape")
    require(all(isinstance(value, dict) for value in needs.values()), "results_shape")
    require(needs["change-scope"].get("result") == "success", "routing_not_successful")
    require(needs["presentation-docs"].get("result") == "success", "documents_not_successful")
    outputs = needs["change-scope"].get("outputs", {})
    require(isinstance(outputs, dict), "routing_output_invalid")
    mode = outputs.get("mode")
    require((mode, outputs.get("run_full")) in (("docs", "false"), ("full", "true")), "routing_output_invalid")
    expected = "success" if mode == "full" else "skipped"
    require(all(needs[name].get("result") == expected for name in full_jobs), "required_job_outcome")
    return dict(schema="ci-scope-result/1", status="passed", mode=mode,
                full_jobs_executed=mode == "full", intentionally_skipped=list(full_jobs) if mode == "docs" else [])


def write_json_exclusive(path, data):
    if path:
        with Path(path).open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=True, indent=2)
            stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("route", "check-docs", "check-results"))
    parser.add_argument("--repo", default=".")
    parser.add_argument("--event")
    parser.add_argument("--event-name", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--ref", default="")
    parser.add_argument("--report")
    parser.add_argument("--github-output")
    parser.add_argument("--full-job", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.command == "route":
            try:
                event_bytes = Path(args.event).read_bytes() if args.event else b""
                event = json.loads(event_bytes) if len(event_bytes) <= MAX_GIT_BYTES else None
            except (OSError, ValueError, TypeError):
                event = None
            result = route(Path(args.repo), args.event_name, event, args.head, args.ref)
        elif args.command == "check-docs":
            result = check_documents(Path(args.repo))
        else:
            try:
                needs = json.loads(os.environ.get("CI_NEEDS_JSON", ""))
            except ValueError as exc:
                raise ScopeError("results_shape") from exc
            result = check_results(needs, tuple(args.full_job))
        # Persist evidence before emitting workflow outputs. Any I/O failure is fatal.
        write_json_exclusive(args.report, result)
        if args.command == "route" and args.github_output:
            with Path(args.github_output).open("a", encoding="utf-8") as stream:
                stream.write("mode=" + result["mode"] + "\nrun_full=" + str(result["run_full"]).lower() + "\n")
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (ScopeError, OSError, UnicodeError) as exc:
        code = exc.code if isinstance(exc, ScopeError) else "record_or_encoding_failure"
        print(json.dumps(dict(status="failed", error_code=code, exception_body_recorded=False)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
