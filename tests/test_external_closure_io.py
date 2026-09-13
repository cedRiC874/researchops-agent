from __future__ import annotations

import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.io import (
    decode_canonical_json_file,
    read_exact_artifact_directory,
    read_regular_file_no_follow,
    scan_public_artifact_bytes,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "researchops_external_closure" / "io.py"


class ExternalClosureIoTests(unittest.TestCase):
    def test_binary_adjacent_headers_and_unambiguous_key_prefixes_are_scanned(self) -> None:
        for value in (b"precedingcellAuthorization: Bearer FAKECANARY",
                      b"precedingcellsk-FAKECANARY12345678",
                      b"precedingcellghp_FAKECANARY12345678",
                      b"precedingcellAKIA0000000000000000nextcell"):
            with self.subTest(prefix=value[:13]):
                with self.assertRaisesRegex(ExternalClosurePrimitiveError, "external_closure_sensitive_content_detected"):
                    scan_public_artifact_bytes((value,))
        # No blanket removal of sk_ boundaries: these public schema names are
        # not secrets. This is a deliberate residual heuristic limitation.
        scan_public_artifact_bytes((b'{"task_content_persisted":false,"task_custodian":"public-role"}',))

    def test_directory_enumeration_stops_at_first_unexpected_entry(self) -> None:
        # A full directory listing is not a bounded read merely because the
        # allowed artifact file set is small. Do not allocate a giant listing
        # in this regression: stop the synthetic iterator if it is over-read.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            class GuardedEntries:
                def __init__(self):
                    self.count = 0

                def __iter__(self):
                    return self

                def __next__(self):
                    self.count += 1
                    if self.count == 1:
                        return root / "a.json"
                    if self.count == 2:
                        return root / "unexpected.json"
                    raise AssertionError("directory enumeration exceeded the exact file-set bound")

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            entries = GuardedEntries()
            # Cover both the old pathlib boundary and the bounded scandir
            # implementation; no real files or large directory are required.
            with patch.object(Path, "iterdir", return_value=entries), patch(
                "researchops_external_closure.io.os.scandir", return_value=entries
            ), patch(
                "researchops_external_closure.io.read_regular_file_no_follow",
                side_effect=AssertionError("unexpected directory must fail before reading a file"),
            ):
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "external_closure_directory_file_set_invalid",
                ):
                    read_exact_artifact_directory(
                        root, expected_names=("a.json",), max_file_bytes=16, max_total_bytes=32
                    )
            self.assertEqual(entries.count, 2)

    def test_bounded_single_link_read_and_exact_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.json").write_bytes(b'{"a":1}\n')
            (root / "b.bin").write_bytes(b"safe")
            self.assertEqual(
                read_regular_file_no_follow(root / "a.json", max_bytes=8),
                b'{"a":1}\n',
            )
            values = read_exact_artifact_directory(
                root,
                expected_names=("a.json", "b.bin"),
                max_file_bytes=16,
                max_total_bytes=32,
            )
            self.assertEqual(tuple(values), ("a.json", "b.bin"))
            with self.assertRaises(TypeError):
                values["a.json"] = b"changed"  # type: ignore[index]
            (root / "extra").write_bytes(b"x")
            with self.assertRaisesRegex(
                ExternalClosurePrimitiveError,
                "external_closure_directory_file_set_invalid",
            ):
                read_exact_artifact_directory(
                    root,
                    expected_names=("a.json", "b.bin"),
                    max_file_bytes=16,
                    max_total_bytes=32,
                )

    def test_links_directories_oversize_and_swap_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_bytes(b"safe")
            with self.assertRaisesRegex(
                ExternalClosurePrimitiveError,
                "external_closure_file_identity_invalid",
            ):
                read_regular_file_no_follow(root, max_bytes=10)
            with self.assertRaisesRegex(
                ExternalClosurePrimitiveError,
                "external_closure_file_identity_invalid",
            ):
                read_regular_file_no_follow(target, max_bytes=3)
            link = root / "link"
            try:
                link.symlink_to(target)
            except OSError:
                link = None
            if link is not None:
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "external_closure_file_identity_invalid",
                ):
                    read_regular_file_no_follow(link, max_bytes=10)
            with patch(
                "researchops_external_closure.io._same_identity",
                return_value=False,
            ):
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "external_closure_file_identity_changed",
                ):
                    read_regular_file_no_follow(target, max_bytes=10)

    def test_hardlink_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            linked = root / "linked"
            source.write_bytes(b"safe")
            try:
                os.link(source, linked)
            except OSError:
                self.skipTest("hardlink creation unavailable")
            for path in (source, linked):
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "external_closure_file_identity_invalid",
                ):
                    read_regular_file_no_follow(path, max_bytes=10)

    def test_canonical_json_file_rejects_whitespace_bom_and_newline_drift(self) -> None:
        self.assertEqual(
            decode_canonical_json_file(b'{"a":1}\n', max_bytes=16),
            {"a": 1},
        )
        for payload in (
            b'{ "a": 1 }\n',
            b'{"a":1}',
            b'{"a":1}\n\n',
            b'\xef\xbb\xbf{"a":1}\n',
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ExternalClosurePrimitiveError):
                    decode_canonical_json_file(payload, max_bytes=32)

    def test_privacy_scan_covers_raw_nul_separated_and_canary_bytes(self) -> None:
        self.assertEqual(scan_public_artifact_bytes([b"safe", b"also safe"]), 2)
        self.assertEqual(
            scan_public_artifact_bytes(
                [b"https://api.deepseek.com/responses", b"https://example.org/a"]
            ),
            2,
        )
        unsafe = (
            b"Authorization: Bearer value",
            b"Authorization: Basic dGVzdDp0ZXN0",
            b"Proxy-Authorization: Digest opaque-value",
            b"sk-abcdefghijk",
            b"sk_abcdefghijk",
            b"C:\\Users\\person\\secret",
            b'{"path":"/home/person/private"}',
            b'{"path":"/etc/private/credential"}',
            b"Traceback (most recent call last)",
            b"person@example.com",
            b"s\x00k\x00-\x00a\x00b\x00c\x00d\x00e\x00f\x00g\x00h\x00i\x00j\x00k\x00",
        )
        for payload in unsafe:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "external_closure_sensitive_content_detected",
                ):
                    scan_public_artifact_bytes([payload])
        with self.assertRaisesRegex(
            ExternalClosurePrimitiveError,
            "external_closure_sensitive_content_detected",
        ):
            scan_public_artifact_bytes(
                [b"prefix private-canary suffix"],
                sensitive_canaries=(b"private-canary",),
            )

    def test_production_module_has_no_write_network_or_environment_capability(self) -> None:
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        self.assertFalse(
            imports.intersection(
                {"socket", "subprocess", "urllib.request", "httpx", "openai", "agents"}
            )
        )
        attributes = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        self.assertFalse(
            attributes.intersection(
                {
                    "write",
                    "write_bytes",
                    "write_text",
                    "mkdir",
                    "unlink",
                    "remove",
                    "rename",
                    "environ",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
