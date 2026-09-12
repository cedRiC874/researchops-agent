from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.pre_receipt import verify_preregistration_envelope
from tests.test_external_closure_documents import SyntheticPreReceipt


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "researchops_external_closure" / "pre_receipt.py"


class ExternalClosurePreReceiptFacadeTests(unittest.TestCase):
    def test_frozen_contract_and_signed_pre_receipt_documents_verify_together(self) -> None:
        fixture = SyntheticPreReceipt()
        documents, observation = fixture.inputs()
        first = verify_preregistration_envelope(
            ROOT,
            documents,
            external_observation_bundle=observation,
        )
        second = verify_preregistration_envelope(
            ROOT,
            documents,
            external_observation_bundle=observation,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first.preregistration_envelope_sha256,
            fixture.documents["preregistration_envelope"]["document_sha256"],
        )
        self.assertFalse(hasattr(first, "runtime_binding"))
        self.assertFalse(hasattr(first, "case_ids"))

    def test_contract_or_document_tamper_is_one_safe_error(self) -> None:
        fixture = SyntheticPreReceipt()
        fixture.documents["authorization_grant"]["model_id"] = "forged"
        documents, observation = fixture.inputs()
        with self.assertRaises(ExternalClosurePrimitiveError) as caught:
            verify_preregistration_envelope(
                ROOT,
                documents,
                external_observation_bundle=observation,
            )
        self.assertRegex(caught.exception.code, r"^external_closure_[a-z0-9_]+$")
        self.assertNotIn("forged", str(caught.exception))

    def test_public_signature_has_no_final_or_runtime_inputs(self) -> None:
        signature = inspect.signature(verify_preregistration_envelope)
        self.assertEqual(
            tuple(signature.parameters),
            ("project_root", "documents", "external_observation_bundle"),
        )
        forbidden = {
            "closure_receipt",
            "final_anchor",
            "provider",
            "api_key",
            "runtime_binding",
            "tracker",
            "session",
        }
        self.assertFalse(forbidden.intersection(signature.parameters))

    def test_module_has_no_provider_network_write_or_clock_import(self) -> None:
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
        blocked = {
            "socket",
            "subprocess",
            "urllib.request",
            "httpx",
            "openai",
            "agents",
            "researchops.model_providers",
            "researchops.audit",
            "researchops.phase6_runner",
            "researchops_completion_telemetry.surface_mapping",
            "researchops_completion_telemetry.capture",
        }
        self.assertFalse(imports.intersection(blocked))
        attributes = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        self.assertFalse(
            attributes.intersection(
                {"write_text", "write_bytes", "mkdir", "unlink", "environ", "now"}
            )
        )


if __name__ == "__main__":
    unittest.main()
