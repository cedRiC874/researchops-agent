"""v2 source-bundle algorithm: same closure today, different digest, subpackages fixed."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from researchops.phase6_source_bundle import (
    DEFAULT_SOURCE_BUNDLE_ALGORITHM,
    phase6_depth60_source_bundle_sha256,
    phase6_depth60_source_bundle_sha256_for,
    phase6_depth60_source_bundle_sha256_v2,
    phase6_depth60_source_files,
    phase6_depth60_source_files_for,
    phase6_depth60_source_files_v2,
)

ROOT = Path(__file__).resolve().parents[1]
_V2_DOMAIN = b"researchops-phase6-depth60-source-bundle-v2\0"


class SourceBundleV2Tests(unittest.TestCase):
    def test_v2_selects_the_same_files_as_v1_on_this_tree(self) -> None:
        self.assertEqual(
            phase6_depth60_source_files_v2(ROOT),
            phase6_depth60_source_files(ROOT),
        )

    def test_v2_digest_differs_from_v1_only_by_the_domain_prefix(self) -> None:
        v1 = phase6_depth60_source_bundle_sha256(ROOT)
        v2 = phase6_depth60_source_bundle_sha256_v2(ROOT)
        self.assertNotEqual(v1, v2)

        source_root = ROOT / "src" / "researchops"
        digest = hashlib.sha256()
        digest.update(_V2_DOMAIN)
        for name in phase6_depth60_source_files(ROOT):
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update((source_root / name).read_bytes())
            digest.update(b"\0")
        self.assertEqual(digest.hexdigest(), v2)

    def test_dispatcher_defaults_to_v1_and_rejects_unknown_algorithms(self) -> None:
        self.assertEqual(DEFAULT_SOURCE_BUNDLE_ALGORITHM, "v1")
        self.assertEqual(
            phase6_depth60_source_bundle_sha256_for(ROOT),
            phase6_depth60_source_bundle_sha256(ROOT),
        )
        self.assertEqual(
            phase6_depth60_source_bundle_sha256_for(ROOT, "v2"),
            phase6_depth60_source_bundle_sha256_v2(ROOT),
        )
        self.assertEqual(
            phase6_depth60_source_files_for(ROOT, "v1"),
            phase6_depth60_source_files(ROOT),
        )
        for bad in ("v3", "V1", "", "sha256"):
            with self.subTest(algorithm=bad):
                with self.assertRaises(ValueError):
                    phase6_depth60_source_bundle_sha256_for(ROOT, bad)

    def test_v1_skips_a_reachable_subpackage_and_v2_includes_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            source_root = tmp / "src" / "researchops"
            source_root.parent.mkdir(parents=True)
            shutil.copytree(
                ROOT / "src" / "researchops",
                source_root,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            package = source_root / "successor_pkg"
            package.mkdir()
            (package / "__init__.py").write_text(
                "from .member import VALUE\n\n__all__ = [\"VALUE\"]\n",
                encoding="utf-8",
            )
            (package / "member.py").write_text("VALUE = 1\n", encoding="utf-8")
            runner = source_root / "phase6_runner.py"
            runner.write_text(
                runner.read_text(encoding="utf-8")
                + "\nfrom .successor_pkg import VALUE as _SUCCESSOR_VALUE\n",
                encoding="utf-8",
            )

            v1_files = phase6_depth60_source_files(tmp)
            v2_files = phase6_depth60_source_files_v2(tmp)

            # v1 resolves the target to "successor_pkg.py", which is not a file,
            # and skips it silently. The package bytes never enter the digest.
            self.assertNotIn("successor_pkg/__init__.py", v1_files)
            self.assertNotIn("successor_pkg/member.py", v1_files)

            self.assertIn("successor_pkg/__init__.py", v2_files)
            self.assertIn("successor_pkg/member.py", v2_files)
            self.assertEqual(len(v2_files), len(v1_files) + 2)

            # And an edit inside the subpackage moves the v2 digest but not v1's.
            before_v1 = phase6_depth60_source_bundle_sha256(tmp)
            before_v2 = phase6_depth60_source_bundle_sha256_v2(tmp)
            (package / "member.py").write_text("VALUE = 2\n", encoding="utf-8")
            self.assertEqual(phase6_depth60_source_bundle_sha256(tmp), before_v1)
            self.assertNotEqual(phase6_depth60_source_bundle_sha256_v2(tmp), before_v2)

    def test_direct_module_import_forms_include_the_parent_initializer(self) -> None:
        import_forms = (
            "import researchops.direct_pkg.member\n",
            "from researchops.direct_pkg.member import VALUE\n",
            "from .direct_pkg.member import VALUE\n",
        )
        for import_form in import_forms:
            with self.subTest(import_form=import_form), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                source_root = tmp / "src" / "researchops"
                source_root.parent.mkdir(parents=True)
                shutil.copytree(
                    ROOT / "src" / "researchops",
                    source_root,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
                package = source_root / "direct_pkg"
                package.mkdir()
                (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
                (package / "member.py").write_text("VALUE = 2\n", encoding="utf-8")
                (package / "sibling.py").write_text("VALUE = 3\n", encoding="utf-8")
                runner = source_root / "phase6_runner.py"
                runner.write_text(
                    runner.read_text(encoding="utf-8") + "\n" + import_form,
                    encoding="utf-8",
                )

                files = phase6_depth60_source_files_v2(tmp)
                self.assertIn("direct_pkg/__init__.py", files)
                self.assertIn("direct_pkg/member.py", files)
                self.assertNotIn("direct_pkg/sibling.py", files)

    def test_nested_parent_initializers_and_their_dependencies_enter_v2(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            source_root = tmp / "src" / "researchops"
            source_root.parent.mkdir(parents=True)
            shutil.copytree(
                ROOT / "src" / "researchops",
                source_root,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            outer = source_root / "outer_pkg"
            inner = outer / "inner_pkg"
            inner.mkdir(parents=True)
            (outer / "__init__.py").write_text(
                "from . import support\n", encoding="utf-8"
            )
            (outer / "support.py").write_text("VALUE = 1\n", encoding="utf-8")
            (inner / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
            (inner / "member.py").write_text("VALUE = 3\n", encoding="utf-8")
            (inner / "sibling.py").write_text("VALUE = 4\n", encoding="utf-8")
            runner = source_root / "phase6_runner.py"
            runner.write_text(
                runner.read_text(encoding="utf-8")
                + "\nfrom .outer_pkg.inner_pkg.member import VALUE\n",
                encoding="utf-8",
            )

            v1_before = phase6_depth60_source_bundle_sha256(tmp)
            files = phase6_depth60_source_files_v2(tmp)
            self.assertIn("outer_pkg/__init__.py", files)
            self.assertIn("outer_pkg/support.py", files)
            self.assertIn("outer_pkg/inner_pkg/__init__.py", files)
            self.assertIn("outer_pkg/inner_pkg/member.py", files)
            self.assertNotIn("outer_pkg/inner_pkg/sibling.py", files)

            v2_before = phase6_depth60_source_bundle_sha256_v2(tmp)
            (outer / "__init__.py").write_text(
                "from . import support\nVALUE = 5\n", encoding="utf-8"
            )
            v2_after_outer = phase6_depth60_source_bundle_sha256_v2(tmp)
            self.assertNotEqual(v2_after_outer, v2_before)
            (inner / "__init__.py").write_text("VALUE = 6\n", encoding="utf-8")
            self.assertNotEqual(
                phase6_depth60_source_bundle_sha256_v2(tmp),
                v2_after_outer,
            )
            self.assertEqual(phase6_depth60_source_bundle_sha256(tmp), v1_before)

    def test_namespace_parent_and_fromlist_imports_include_the_member(self) -> None:
        import_forms = (
            "import researchops.namespace_pkg.member\n",
            "from researchops.namespace_pkg import member\n",
            "from .namespace_pkg import member\n",
        )
        for import_form in import_forms:
            with self.subTest(import_form=import_form), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                source_root = tmp / "src" / "researchops"
                source_root.parent.mkdir(parents=True)
                shutil.copytree(
                    ROOT / "src" / "researchops",
                    source_root,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
                namespace = source_root / "namespace_pkg"
                namespace.mkdir()
                member = namespace / "member.py"
                member.write_text("VALUE = 1\n", encoding="utf-8")
                (namespace / "sibling.py").write_text(
                    "VALUE = 2\n", encoding="utf-8"
                )
                runner = source_root / "phase6_runner.py"
                runner.write_text(
                    runner.read_text(encoding="utf-8") + "\n" + import_form,
                    encoding="utf-8",
                )

                v1_before = phase6_depth60_source_bundle_sha256(tmp)
                v2_before = phase6_depth60_source_bundle_sha256_v2(tmp)
                files = phase6_depth60_source_files_v2(tmp)
                self.assertIn("namespace_pkg/member.py", files)
                self.assertNotIn("namespace_pkg/__init__.py", files)
                self.assertNotIn("namespace_pkg/sibling.py", files)

                member.write_text("VALUE = 3\n", encoding="utf-8")
                self.assertEqual(
                    phase6_depth60_source_bundle_sha256(tmp),
                    v1_before,
                )
                self.assertNotEqual(
                    phase6_depth60_source_bundle_sha256_v2(tmp),
                    v2_before,
                )

    def test_v2_rejects_a_relative_import_that_escapes_the_package(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            source_root = tmp / "src" / "researchops"
            source_root.parent.mkdir(parents=True)
            shutil.copytree(
                ROOT / "src" / "researchops",
                source_root,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            runner = source_root / "phase6_runner.py"
            runner.write_text(
                runner.read_text(encoding="utf-8") + "\nfrom .. import outside\n",
                encoding="utf-8",
            )
            # v1 ignores level>=2 entirely; v2 fails closed.
            phase6_depth60_source_files(tmp)
            with self.assertRaises(ValueError):
                phase6_depth60_source_files_v2(tmp)


if __name__ == "__main__":
    unittest.main()
