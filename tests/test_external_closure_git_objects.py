from __future__ import annotations

import ast
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
import zlib
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from researchops_external_closure import git_objects
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.git_objects import read_git_object_snapshot


MODULE = Path(git_objects.__file__)
GIT = shutil.which("git")


class Repository:
    """Every mutation is confined to an independently initialized temp repo."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        self.environment = {
            name: value
            for name in ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP")
            if (value := os.environ.get(name)) is not None
        }
        self.environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_ALLOW_PROTOCOL": "",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        self.git("init", "--quiet", "--object-format=sha1")

    def git(self, *arguments: str, payload: bytes | None = None) -> bytes:
        return subprocess.run(
            [GIT, *arguments],
            input=payload,
            cwd=self.root,
            env=self.environment,
            shell=False,
            capture_output=True,
            check=True,
            timeout=15,
        ).stdout

    def object(self, kind: str, payload: bytes) -> str:
        return self.git(
            "hash-object", "--literally", "-w", "-t", kind, "--stdin", payload=payload,
        ).decode("ascii").strip()

    def tree(self, *entries: tuple[str, str, str]) -> str:
        ordered = sorted(entries, key=lambda entry: entry[1] + ("/" if entry[0] == "40000" else ""))
        return self.object(
            "tree",
            b"".join(
                mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(oid)
                for mode, name, oid in ordered
            ),
        )

    def commit(self, tree: str, parent: str | None = None) -> str:
        return self.object(
            "commit",
            (
                "tree " + tree + "\n"
                + ("parent " + parent + "\n" if parent else "")
                + "author Fixture <fixture@example.invalid> 1700000000 +0000\n"
                + "committer Fixture <fixture@example.invalid> 1700000000 +0000\n\n"
                + "synthetic local Git object fixture\n"
            ).encode("ascii"),
        )

    def loose_object(self, oid: str) -> Path:
        return self.root / ".git" / "objects" / oid[:2] / oid[2:]


@unittest.skipUnless(GIT, "local Git installation unavailable")
class ExternalClosureGitObjectsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = Repository(self.root / "repo")
        self.content = b"historical\r\n$Format:%H$\n\x00raw\xffbytes"
        self.blob = self.repo.object("blob", self.content)
        self.tree = self.repo.tree(("100644", "kept.bin", self.blob))
        self.commit = self.repo.commit(self.tree)

    def snapshot(self, **kwargs: object):
        return read_git_object_snapshot(
            self.repo.root, self.commit, ("kept.bin",), **kwargs,
        )

    def assert_code(self, code: str, function, *args, **kwargs) -> None:
        with self.assertRaises(ExternalClosurePrimitiveError) as caught:
            function(*args, **kwargs)
        self.assertEqual(str(caught.exception), "external_closure_git_" + code)
        self.assertEqual(caught.exception.code, str(caught.exception))

    def test_real_historical_snapshot_survives_head_and_dirty_tree_changes(self) -> None:
        self.repo.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.repo.git("update-ref", "refs/heads/main", self.commit)
        before = self.snapshot(expected_tree_oid=self.tree)
        newer_blob = self.repo.object("blob", b"new committed content")
        newer_tree = self.repo.tree(("100644", "kept.bin", newer_blob))
        newer_commit = self.repo.commit(newer_tree, self.commit)
        self.repo.git("update-ref", "refs/heads/main", newer_commit)
        (self.repo.root / "kept.bin").write_bytes(b"uncommitted dirty content")
        after = self.snapshot(expected_tree_oid=self.tree)
        self.assertEqual(before, after)
        self.assertEqual(after.blobs[0].payload, self.content)
        self.assertEqual(after.blobs[0].sha256, hashlib.sha256(self.content).hexdigest())
        self.assertEqual(after.objects_read, 3)
        self.assertEqual(after.commit_oid, self.commit)
        self.assertEqual(after.tree_oid, self.tree)
        with self.assertRaises(FrozenInstanceError):
            after.commit_oid = newer_commit
        with self.assertRaises(FrozenInstanceError):
            after.blobs[0].payload = b"mutated"
        self.assertIsInstance(after.blobs, tuple)
        self.assertIsInstance(after.trees, tuple)

    def test_nested_selected_blobs_preserve_modes_and_order(self) -> None:
        script = self.repo.object("blob", b"raise AssertionError('never execute')\n")
        subtree = self.repo.tree(("100755", "runner.py", script), ("100644", "data.bin", self.blob))
        tree = self.repo.tree(("40000", "src", subtree), ("100644", "top.bin", self.blob))
        snapshot = read_git_object_snapshot(
            self.repo.root, self.repo.commit(tree), ("src/runner.py", "top.bin", "src/data.bin"),
        )
        self.assertEqual(tuple(blob.path for blob in snapshot.blobs), ("src/runner.py", "top.bin", "src/data.bin"))
        self.assertEqual(snapshot.blobs[0].mode, "100755")
        self.assertEqual(tuple(tree.path for tree in snapshot.trees), ("", "src"))
        self.assertEqual(
            tuple((entry.path, entry.mode) for entry in snapshot.entries),
            (
                ("src", "40000"),
                ("src/data.bin", "100644"),
                ("src/runner.py", "100755"),
                ("top.bin", "100644"),
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            snapshot.entries[0].path = "forged"
        self.assertEqual(snapshot.objects_read, 5)

    def test_exact_commit_and_optional_expected_tree_are_checked(self) -> None:
        self.assertEqual(
            read_git_object_snapshot(self.repo.root, self.commit.upper(), ("kept.bin",)).commit_oid,
            self.commit,
        )
        for oid in ("HEAD", self.commit[:12], self.commit + "^{tree}", "-t", " " + self.commit, "g" * 40):
            with self.subTest(oid=oid):
                self.assert_code("oid_invalid", read_git_object_snapshot, self.repo.root, oid, ("kept.bin",))
        self.assert_code("tree_mismatch", self.snapshot, expected_tree_oid="1" * 40)
        self.assert_code("object_type_invalid", read_git_object_snapshot, self.repo.root, self.blob, ("kept.bin",))
        self.assert_code("object_unavailable", read_git_object_snapshot, self.repo.root, "1" * 40, ("kept.bin",))
        malformed = self.repo.object("commit", b"tree " + self.tree.encode() + b"\ntree " + self.tree.encode() + b"\n\nmessage")
        self.assert_code("commit_invalid", read_git_object_snapshot, self.repo.root, malformed, ("kept.bin",))

    def test_wrong_tree_object_type_missing_tree_and_missing_selected_blob_reject(self) -> None:
        self.assert_code("object_type_invalid", read_git_object_snapshot, self.repo.root, self.repo.commit(self.blob), ("kept.bin",))
        self.assert_code("object_unavailable", read_git_object_snapshot, self.repo.root, self.repo.commit("1" * 40), ("kept.bin",))
        self.repo.loose_object(self.blob).chmod(0o600)
        self.repo.loose_object(self.blob).unlink()
        self.assert_code("object_unavailable", self.snapshot)

    def test_unselected_blob_is_never_opened_even_when_missing_or_oversized(self) -> None:
        huge = self.repo.object("blob", b"x" * (git_objects._MAX_OBJECT_BYTES + 1))
        tree = self.repo.tree(
            ("100644", "kept.bin", self.blob),
            ("100644", "missing.bin", "1" * 40),
            ("100644", "huge.bin", huge),
        )
        snapshot = read_git_object_snapshot(self.repo.root, self.repo.commit(tree), ("kept.bin",))
        self.assertEqual(snapshot.objects_read, 3)
        self.assertEqual(snapshot.blobs[0].payload, self.content)
        self.assert_code("object_size_limit", read_git_object_snapshot, self.repo.root, self.repo.commit(tree), ("huge.bin",))

    def test_replace_refs_cannot_substitute_commit_tree_or_blob(self) -> None:
        other_blob = self.repo.object("blob", b"replacement content")
        other_tree = self.repo.tree(("100644", "kept.bin", other_blob))
        other_commit = self.repo.commit(other_tree)
        for original, replacement in ((self.blob, other_blob), (self.tree, other_tree), (self.commit, other_commit)):
            self.repo.git("update-ref", "refs/replace/" + original, replacement)
        snapshot = self.snapshot()
        self.assertEqual(snapshot.commit_oid, self.commit)
        self.assertEqual(snapshot.tree_oid, self.tree)
        self.assertEqual(snapshot.blobs[0].payload, self.content)

    def test_export_attributes_and_filters_never_transform_selected_bytes(self) -> None:
        attrs = self.repo.object(
            "blob", b"kept.bin export-ignore export-subst text eol=lf filter=forbidden\n",
        )
        tree = self.repo.tree(("100644", ".gitattributes", attrs), ("100644", "kept.bin", self.blob))
        self.repo.git("config", "filter.forbidden.required", "true")
        self.repo.git("config", "filter.forbidden.smudge", "missing-never-run-command")
        self.repo.git("config", "core.autocrlf", "true")
        snapshot = read_git_object_snapshot(self.repo.root, self.repo.commit(tree), ("kept.bin",))
        self.assertEqual(snapshot.blobs[0].payload, self.content)
        self.assertEqual(snapshot.objects_read, 3)

    def test_git_environment_redirection_config_traces_and_private_values_are_not_inherited(self) -> None:
        other = Repository(self.root / "other")
        allowed = {name: os.environ.get(name) for name in git_objects._ENV_KEYS}
        synthetic = {
            "GIT_DIR": str(other.root / ".git"),
            "GIT_WORK_TREE": str(other.root),
            "GIT_OBJECT_DIRECTORY": str(other.root / ".git" / "objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other.root / ".git" / "objects"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "alias.cat-file",
            "GIT_CONFIG_VALUE_0": "!missing-command",
            "GIT_TRACE": str(self.root / "must-not-exist.trace"),
            "GIT_TRACE2_EVENT": str(self.root / "must-not-exist.trace2"),
            "GIT_ASKPASS": "must-not-run",
            "SSH_AUTH_SOCK": "must-not-forward",
            "RAW_GIT_PRIVATE_CANARY": "must-not-forward",
        }
        reads = []

        class GuardedEnvironment(dict):
            def get(inner, name, default=None):
                reads.append(name)
                if name not in git_objects._ENV_KEYS:
                    raise AssertionError("ambient context was read")
                return allowed.get(name, default)

            def __iter__(inner):
                raise AssertionError("ambient environment was enumerated")

            def copy(inner):
                raise AssertionError("ambient environment was copied")

        observed = []
        real_popen = subprocess.Popen

        def observing_popen(*args, **kwargs):
            observed.append((args[0], kwargs))
            return real_popen(*args, **kwargs)

        with patch.object(git_objects.os, "environ", GuardedEnvironment(synthetic)):
            with patch.object(git_objects.subprocess, "Popen", side_effect=observing_popen):
                snapshot = self.snapshot()
        self.assertEqual(snapshot.blobs[0].payload, self.content)
        self.assertEqual(set(reads), set(git_objects._ENV_KEYS))
        self.assertEqual(len(observed), 9)
        for command, kwargs in observed:
            environment = kwargs["env"]
            self.assertFalse(kwargs["shell"])
            self.assertIn("--no-replace-objects", command)
            self.assertIn("protocol.allow=never", command)
            self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")
            self.assertEqual(environment["GIT_ALLOW_PROTOCOL"], "")
            self.assertEqual(command[-3], "cat-file")
            self.assertTrue(set(environment).isdisjoint(synthetic))
        self.assertFalse((self.root / "must-not-exist.trace").exists())
        self.assertFalse((self.root / "must-not-exist.trace2").exists())

    def test_missing_promisor_blob_is_not_fetched(self) -> None:
        # A real second local repo holds the missing object. Even a permissive
        # repository protocol setting cannot cause the reader to fetch it.
        remote = Repository(self.root / "remote")
        self.assertEqual(remote.object("blob", self.content), self.blob)
        remote_tree = remote.tree(("100644", "kept.bin", self.blob))
        remote.git("update-ref", "refs/heads/main", remote.commit(remote_tree))
        self.repo.git("config", "remote.origin.url", str(remote.root))
        self.repo.git("config", "remote.origin.promisor", "true")
        self.repo.git("config", "remote.origin.partialclonefilter", "blob:none")
        self.repo.git("config", "extensions.partialClone", "origin")
        self.repo.git("config", "protocol.file.allow", "always")
        self.repo.loose_object(self.blob).chmod(0o600)
        self.repo.loose_object(self.blob).unlink()
        before = self._files(self.repo.root)
        self.assert_code("object_unavailable", self.snapshot)
        self.assertEqual(before, self._files(self.repo.root))
        self.assertFalse(self.repo.loose_object(self.blob).exists())

    def test_corrupt_object_payload_is_rehashed_including_git_header(self) -> None:
        corrupt = b"X" * len(self.content)
        self.repo.loose_object(self.blob).chmod(0o600)
        self.repo.loose_object(self.blob).write_bytes(
            zlib.compress(b"blob " + str(len(corrupt)).encode("ascii") + b"\0" + corrupt),
        )
        self.assert_code("object_hash_invalid", self.snapshot)

    def test_object_growth_between_size_probe_and_read_is_bounded(self) -> None:
        real_popen = subprocess.Popen

        def growing_object_popen(command, **kwargs):
            if command[-3:] == ["cat-file", "blob", self.blob]:
                grown = self.content * 1000
                self.repo.loose_object(self.blob).chmod(0o600)
                self.repo.loose_object(self.blob).write_bytes(
                    zlib.compress(b"blob " + str(len(grown)).encode("ascii") + b"\0" + grown),
                )
            return real_popen(command, **kwargs)

        with patch.object(git_objects.subprocess, "Popen", side_effect=growing_object_popen):
            self.assert_code("output_limit", self.snapshot)

    def test_unsafe_tree_modes_are_rejected_even_when_unselected(self) -> None:
        for mode in ("120000", "160000", "100664", "040000"):
            with self.subTest(mode=mode):
                tree = self.repo.tree(("100644", "kept.bin", self.blob), (mode, "unsafe", self.blob))
                self.assert_code("tree_mode_invalid", read_git_object_snapshot, self.repo.root, self.repo.commit(tree), ("kept.bin",))

    def test_portable_path_restrictions_apply_to_requested_and_tree_paths(self) -> None:
        invalid = (
            "../escape", "/absolute", "C:/drive", "a\\b", "a//b", "a/./b",
            "bad\x00name", "bad\tname", ".git", "CON", "lpt1.txt", "COM¹.log", "CON .txt",
            "trailing.", "trailing ", "a:b", "*.txt", "a?b", 'a"b', "e\u0301.txt",
        )
        for name in invalid:
            with self.subTest(request=name):
                self.assert_code("path_invalid", read_git_object_snapshot, self.repo.root, self.commit, (name,))
            if "/" not in name and "\0" not in name:
                tree = self.repo.tree(("100644", "kept.bin", self.blob), ("100644", name, self.blob))
                with self.subTest(tree=name):
                    self.assert_code("path_invalid", read_git_object_snapshot, self.repo.root, self.repo.commit(tree), ("kept.bin",))
        self.assert_code("path_collision", read_git_object_snapshot, self.repo.root, self.commit, ("kept.bin", "KEPT.bin"))
        tree = self.repo.tree(("100644", "A", self.blob), ("100644", "a", self.blob))
        self.assert_code("path_collision", read_git_object_snapshot, self.repo.root, self.repo.commit(tree), ("a",))

    def test_malformed_raw_tree_and_blob_type_mismatch_reject(self) -> None:
        malformed = self.repo.object("tree", b"100644 kept.bin\0short")
        self.assert_code("tree_invalid", read_git_object_snapshot, self.repo.root, self.repo.commit(malformed), ("kept.bin",))
        tree = self.repo.tree(("100644", "kept.bin", self.tree))
        self.assert_code("object_type_invalid", read_git_object_snapshot, self.repo.root, self.repo.commit(tree), ("kept.bin",))
        self.assert_code("required_path_missing", read_git_object_snapshot, self.repo.root, self.commit, ("absent.bin",))

    def test_raw_tree_entries_must_use_strict_git_byte_sort_order(self) -> None:
        unsorted = self.repo.object(
            "tree",
            b"100644 z.bin\0" + bytes.fromhex(self.blob)
            + b"100644 a.bin\0" + bytes.fromhex(self.blob),
        )
        self.assert_code("tree_order_invalid", read_git_object_snapshot, self.repo.root, self.repo.commit(unsorted), ("a.bin",))
        empty = self.repo.tree()
        # Git compares directory names as if suffixed with '/', placing the
        # dotted filename before its shorter directory-name prefix.
        correct = self.repo.tree(
            ("40000", "entry", empty),
            ("100644", "entry.txt", self.blob),
            ("100644", "entry0", self.blob),
            ("100644", "中", self.blob),
        )
        snapshot = read_git_object_snapshot(self.repo.root, self.repo.commit(correct), ("entry.txt", "中"))
        self.assertEqual(snapshot.blobs[0].payload, self.content)
        incorrect = self.repo.object(
            "tree",
            b"40000 entry\0" + bytes.fromhex(empty)
            + b"100644 entry.txt\0" + bytes.fromhex(self.blob),
        )
        self.assert_code("tree_order_invalid", read_git_object_snapshot, self.repo.root, self.repo.commit(incorrect), ("entry.txt",))

    def test_count_size_total_path_and_depth_limits(self) -> None:
        for constant, value, code in (
            ("_MAX_OBJECTS", 2, "object_count_limit"),
            ("_MAX_TOTAL_BYTES", 1, "total_size_limit"),
            ("_MAX_OBJECT_BYTES", 1, "object_size_limit"),
            ("_MAX_TREE_ENTRIES", 0, "tree_entry_limit"),
            ("_MAX_PATH_BYTES", 2, "path_limit"),
        ):
            with self.subTest(limit=constant), patch.object(git_objects, constant, value):
                self.assert_code(code, self.snapshot)
        for paths in ((), "kept.bin", ["kept.bin"] * (git_objects._MAX_PATHS + 1)):
            self.assert_code("paths_invalid", read_git_object_snapshot, self.repo.root, self.commit, paths)
        self.assert_code("path_limit", read_git_object_snapshot, self.repo.root, self.commit, ("/".join(["a"] * 33),))

    @staticmethod
    def _files(root: Path):
        return {
            str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes())
            for path in root.rglob("*") if path.is_file()
        }

    def test_snapshot_has_no_filesystem_writes_and_never_executes_source(self) -> None:
        marker = self.root / "never-created.txt"
        source = ("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\n").encode()
        blob = self.repo.object("blob", source)
        tree = self.repo.tree(("100755", "kept.bin", blob))
        commit = self.repo.commit(tree)
        before = self._files(self.root)
        with patch("builtins.open", side_effect=AssertionError("Python file open forbidden")):
            snapshot = read_git_object_snapshot(self.repo.root, commit, ("kept.bin",))
        self.assertEqual(snapshot.blobs[0].payload, source)
        self.assertEqual(before, self._files(self.root))
        self.assertFalse(marker.exists())

    def test_production_boundary_has_fixed_commands_no_write_or_public_injection_seams(self) -> None:
        syntax = ast.parse(MODULE.read_text(encoding="utf-8"))
        attributes = {node.attr for node in ast.walk(syntax) if isinstance(node, ast.Attribute)}
        self.assertFalse(attributes.intersection({
            "write", "write_bytes", "write_text", "mkdir", "unlink", "remove", "rename",
            "replace", "chmod", "touch", "mkdtemp", "TemporaryDirectory", "run", "check_output",
            "check_call", "system", "exec", "eval", "import_module", "exec_module",
        }))
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(syntax) if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertFalse(imports.intersection({"socket", "httpx", "requests", "urllib", "openai", "tempfile", "importlib"}))
        public = next(node for node in syntax.body if isinstance(node, ast.FunctionDef) and node.name == "read_git_object_snapshot")
        self.assertEqual(
            [argument.arg for argument in public.args.args + public.args.kwonlyargs],
            ["repository", "commit_oid", "required_paths", "expected_tree_oid"],
        )


if __name__ == "__main__":
    unittest.main()
