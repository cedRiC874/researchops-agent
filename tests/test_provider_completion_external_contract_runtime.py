from __future__ import annotations

import ast
import os
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import researchops.provider_completion_external_contract as external_contract


ROOT = Path(__file__).resolve().parents[1]


class ProviderCompletionExternalContractRuntimeTests(unittest.TestCase):
    def test_frozen_contract_graph_loads_to_read_only_summary(self) -> None:
        summary = external_contract.load_frozen_external_preregistration_contract(
            ROOT
        )

        self.assertIsInstance(summary, MappingProxyType)
        self.assertEqual(
            summary["status"], "valid_frozen_external_preregistration_design"
        )
        self.assertEqual(summary["schema_count"], 14)
        self.assertEqual(summary["predecessor_count"], 5)
        self.assertEqual(summary["supporting_predecessor_count"], 2)
        self.assertEqual(len(summary["schema_files"]), 14)
        self.assertIsInstance(summary["schema_files"], tuple)
        self.assertEqual(set(summary["schemas"]), set(summary["schema_files"]))
        schema = summary["schemas"][summary["schema_files"][0]]
        with self.assertRaises(TypeError):
            schema["type"] = "array"
        self.assertIsInstance(schema["required"], tuple)
        with self.assertRaises(AttributeError):
            schema["required"].append("forged")
        with self.assertRaises(TypeError):
            schema["properties"]["schema_version"]["const"] = "forged"
        self.assertTrue(summary["field_contract_frozen"])
        self.assertTrue(summary["semantic_contract_frozen"])
        self.assertFalse(summary["online_execution_authorized"])
        self.assertEqual(summary["network_calls"], 0)
        self.assertEqual(summary["model_calls"], 0)
        self.assertFalse(summary["provider_key_loaded"])
        self.assertEqual(
            summary["design_contract"]["semantic_commitment_sha256"],
            external_contract.DESIGN_CONTRACT_COMMITMENT_SHA256,
        )
        self.assertEqual(
            summary["supporting_contract"]["semantic_commitment_sha256"],
            external_contract.SUPPORTING_CONTRACT_COMMITMENT_SHA256,
        )
        with self.assertRaises(TypeError):
            summary["status"] = "forged"  # type: ignore[index]
        with self.assertRaises(TypeError):
            summary["design_contract"]["bytes"] = 0  # type: ignore[index]

    def test_loader_opens_no_file_for_write(self) -> None:
        original_open = os.open
        write_mask = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

        def guarded_open(path, flags, *args, **kwargs):
            self.assertEqual(flags & write_mask, 0)
            return original_open(path, flags, *args, **kwargs)

        with patch.object(external_contract.os, "open", side_effect=guarded_open):
            external_contract.load_frozen_external_preregistration_contract(ROOT)

    def test_strict_json_rejects_duplicate_and_nonfinite_values(self) -> None:
        cases = (
            (b'{"field":1,"field":2}', "external_contract_json_duplicate_key"),
            (b'{"field":NaN}', "external_contract_json_nonfinite"),
            (b"[]", "external_contract_json_shape_invalid"),
        )
        for payload, expected_code in cases:
            with self.subTest(expected_code=expected_code), self.assertRaises(
                external_contract.ExternalPreregistrationContractError
            ) as caught:
                external_contract._parse_json_object(payload)
            self.assertEqual(caught.exception.code, expected_code)

    def test_bounded_reader_rejects_non_regular_and_oversized_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            regular = root / "regular.json"
            regular.write_bytes(b"{}")
            with self.assertRaises(
                external_contract.ExternalPreregistrationContractError
            ) as oversized:
                external_contract._read_regular_file_no_follow(
                    regular, maximum_bytes=1
                )
            self.assertEqual(
                oversized.exception.code, "external_contract_file_invalid"
            )
            with self.assertRaises(
                external_contract.ExternalPreregistrationContractError
            ) as non_regular:
                external_contract._read_regular_file_no_follow(root)
            self.assertEqual(
                non_regular.exception.code, "external_contract_file_invalid"
            )

    def test_bounded_reader_rejects_link_like_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_bytes(b"{}")
            with patch.object(
                external_contract, "_is_link_like", return_value=True
            ), self.assertRaises(
                external_contract.ExternalPreregistrationContractError
            ) as simulated:
                external_contract._read_regular_file_no_follow(target)
            self.assertEqual(
                simulated.exception.code, "external_contract_file_invalid"
            )

    def test_bounded_reader_rejects_symlink_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            link = root / "link.json"
            target.write_bytes(b"{}")
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("symlink creation is unavailable on this host")
            with self.assertRaises(
                external_contract.ExternalPreregistrationContractError
            ) as caught:
                external_contract._read_regular_file_no_follow(link)
            self.assertEqual(caught.exception.code, "external_contract_file_invalid")

    def test_module_has_no_runtime_authority_or_provider_import(self) -> None:
        source = Path(external_contract.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        forbidden = {
            "researchops.audit",
            "researchops.completion_telemetry_ledger",
            "researchops.model_providers",
            "researchops.phase6_runner",
            "researchops_completion_telemetry.capture",
            "researchops_completion_telemetry.surface_mapping",
        }
        self.assertTrue(imported.isdisjoint(forbidden), imported & forbidden)
        self.assertNotIn("subprocess", imported)
        self.assertNotIn("socket", imported)
        self.assertNotIn("httpx", imported)


if __name__ == "__main__":
    unittest.main()
