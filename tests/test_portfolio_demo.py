from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts/portfolio_demo.py"
SPEC = importlib.util.spec_from_file_location("portfolio_demo", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("unable to load portfolio demo")
DEMO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEMO)


class PortfolioDemoTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows PowerShell wrapper test")
    def test_powershell_wrapper_streams_output_and_propagates_failure(self) -> None:
        wrapper = PROJECT_ROOT / "scripts/portfolio_demo.ps1"
        with tempfile.TemporaryDirectory() as directory:
            fake_python = Path(directory) / "fake-python.cmd"
            fake_python.write_text(
                "@echo off\r\n"
                "echo stream-start\r\n"
                "ping 127.0.0.1 -n 3 >nul\r\n"
                "echo stream-end\r\n"
                "exit /b 7\r\n",
                encoding="ascii",
            )
            process = subprocess.Popen(
                (
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(wrapper),
                    "-PythonPath",
                    str(fake_python),
                ),
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert process.stdout is not None
            first_line = process.stdout.readline().strip()
            self.assertEqual(first_line, "stream-start")
            self.assertIsNone(process.poll(), "wrapper buffered the child output")
            remaining, _ = process.communicate(timeout=10)

        self.assertEqual(process.returncode, 1)
        self.assertIn("stream-end", remaining)
        self.assertIn("exit code 7", remaining)

    def test_strict_demo_accepts_only_windows_and_linux_x86_64(self) -> None:
        for system, machine in (("Windows", "AMD64"), ("Linux", "x86_64")):
            with self.subTest(system=system, machine=machine):
                with (
                    patch.object(DEMO.platform, "system", return_value=system),
                    patch.object(DEMO.platform, "machine", return_value=machine),
                    patch.object(DEMO.sys, "maxsize", 2**63 - 1),
                ):
                    self.assertEqual(DEMO._require_supported_platform(), system)

        unsupported = (
            ("Darwin", "x86_64", 2**63 - 1),
            ("Darwin", "arm64", 2**63 - 1),
            ("Windows", "ARM64", 2**63 - 1),
            ("Linux", "aarch64", 2**63 - 1),
            ("Windows", "AMD64", 2**31 - 1),
        )
        for system, machine, maxsize in unsupported:
            with self.subTest(system=system, machine=machine, maxsize=maxsize):
                with (
                    patch.object(DEMO.platform, "system", return_value=system),
                    patch.object(DEMO.platform, "machine", return_value=machine),
                    patch.object(DEMO.sys, "maxsize", maxsize),
                ):
                    with self.assertRaisesRegex(
                        DEMO.DemoError, "supports only Windows x86-64 and Linux x86-64"
                    ):
                        DEMO._require_supported_platform()

    def test_windows_and_linux_use_separate_canonical_evidence_ids(self) -> None:
        self.assertEqual(
            DEMO.CANONICAL_EVIDENCE_IDS,
            {
                "Windows": "E-36034128278C",
                "Linux": "E-14EBFFCA843E",
            },
        )

    def test_offline_environment_pins_numerics_and_removes_provider_credentials(self) -> None:
        canaries = {
            name: f"secret-{index}"
            for index, name in enumerate(DEMO.PROVIDER_CREDENTIAL_VARIABLES)
        }
        with patch.dict(os.environ, canaries, clear=False):
            environment = DEMO._offline_environment(PROJECT_ROOT)

        for name in DEMO.PROVIDER_CREDENTIAL_VARIABLES:
            self.assertNotIn(name, environment)
        self.assertEqual(environment["OPENBLAS_CORETYPE"], "NEHALEM")
        self.assertEqual(environment["OPENBLAS_NUM_THREADS"], "1")
        self.assertEqual(environment["PYTHONPATH"], str(PROJECT_ROOT / "src"))

    def test_output_must_be_a_new_child_of_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts").mkdir()
            output = DEMO._resolve_output(root, "artifacts/new-demo")
            self.assertEqual(output, (root / "artifacts/new-demo").resolve())

            with self.assertRaises(DEMO.DemoError):
                DEMO._resolve_output(root, "artifacts")
            with self.assertRaises(DEMO.DemoError):
                DEMO._resolve_output(root, "outside-demo")

            output.mkdir()
            with self.assertRaises(DEMO.DemoError):
                DEMO._resolve_output(root, "artifacts/new-demo")

    def test_cli_help_requires_no_repository_or_credentials(self) -> None:
        parser = DEMO.build_parser()
        parsed = parser.parse_args([])
        self.assertIsNone(parsed.output_dir)
        parsed = parser.parse_args(["--output-dir", "artifacts/demo"])
        self.assertEqual(parsed.output_dir, "artifacts/demo")


if __name__ == "__main__":
    unittest.main()
