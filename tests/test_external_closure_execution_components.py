from __future__ import annotations

import ast
import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from researchops_external_closure import execution_components as components
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.primitives import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[1]


def component_fixture_files() -> tuple[dict[str, bytes], tuple[str, ...]]:
    recipe_bytes = (ROOT / components.RECIPE_PATH).read_bytes()
    recipe = json.loads(recipe_bytes)
    paths = set(recipe["mandatory_files"])
    paths.update(recipe["raw_components"].values())
    for group in recipe["file_groups"].values():
        paths.update(group["files"])
        for prefix in group["prefixes"]:
            paths.update(
                path.relative_to(ROOT).as_posix()
                for path in (ROOT / prefix).rglob("*")
                if path.is_file() and path.name.endswith(group["suffix"])
            )
    selected = components.select_execution_component_paths(tuple(paths), recipe_bytes)
    return {path: (ROOT / path).read_bytes() for path in selected}, tuple(sorted(paths))


class ExecutionComponentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.files, cls.paths = component_fixture_files()
        cls.documents = components.build_execution_component_documents(
            cls.files, available_paths=cls.paths
        )

    def verify(self, files=None, paths=None, documents=None):
        documents = self.documents if documents is None else documents
        return components.verify_execution_component_documents(
            self.files if files is None else files,
            available_paths=self.paths if paths is None else paths,
            implementation_manifest=documents.implementation_manifest,
            source_integrity_v6_plan=documents.source_integrity_v6_plan,
        )

    def test_deterministic_no_cycle_and_independent_domain_recomputation(self) -> None:
        second = components.build_execution_component_documents(self.files, available_paths=self.paths)
        self.assertEqual(second, self.documents)
        recipe = json.loads(self.files[components.RECIPE_PATH])
        manifest = json.loads(self.documents.implementation_manifest)
        plan = json.loads(self.documents.source_integrity_v6_plan)
        for document, field, domain in (
            (manifest, "commitment_sha256", recipe["domains"]["implementation_manifest"]),
            (plan, "plan_commitment_sha256", recipe["domains"]["source_integrity_v6"]),
        ):
            body = dict(document)
            recorded = body.pop(field)
            self.assertEqual(
                recorded, hashlib.sha256(domain.encode() + b"\0" + canonical_json_bytes(body)).hexdigest()
            )
        self.assertEqual(plan["implementation_manifest_sha256"], hashlib.sha256(self.documents.implementation_manifest).hexdigest())
        self.assertNotIn(components.MANIFEST_PATH, self.files)
        self.assertNotIn(components.V6_PLAN_PATH, self.files)
        result = self.verify()
        self.assertFalse(result.runtime_authority_granted)
        self.assertFalse(result.admission_verified)
        self.assertEqual(result.file_count, len(self.files))
        with self.assertRaises(TypeError):
            components.VerifiedExecutionComponents()
        with self.assertRaises(FrozenInstanceError):
            result.file_count = 0
        with self.assertRaises(TypeError):
            result.component_hashes["mapping_sha256"] = "0" * 64

    def test_new_python_file_cannot_be_omitted_from_existing_commitment(self) -> None:
        path = "src/researchops_external_closure/new_security_boundary.py"
        paths = self.paths + (path,)
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "file_set_mismatch"):
            self.verify(paths=paths)
        files = {**self.files, path: b"# additional first-party implementation\n"}
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "implementation_drift"):
            self.verify(files=files, paths=paths)
        updated = components.build_execution_component_documents(files, available_paths=paths)
        before = json.loads(self.documents.implementation_manifest)["component_hashes"]
        after = json.loads(updated.implementation_manifest)["component_hashes"]
        self.assertNotEqual(before["source_bundle_sha256"], after["source_bundle_sha256"])
        self.assertNotEqual(before["closure_verifier_sha256"], after["closure_verifier_sha256"])

    def test_source_lock_config_recipe_and_historical_tamper_fail_closed(self) -> None:
        cases = (
            ("src/researchops_external_closure/final.py", "implementation_drift"),
            ("requirements.lock", "implementation_drift"),
            (components.RECIPE_PATH, "recipe_mismatch"),
            ("evals/phase6_deepseek_depth60_plan_v3.json", "historical_plan_mismatch"),
            ("evals/provider_completion_external_preregistration_v1/closure_evidence_contract_v1.json", "frozen_binding_mismatch"),
        )
        for path, code in cases:
            with self.subTest(path=path):
                modified = {**self.files, path: self.files[path] + b"\n"}
                with self.assertRaisesRegex(ExternalClosurePrimitiveError, code):
                    self.verify(files=modified)
        config = json.loads(self.files[components.CONFIG_PATH])
        config["stream"] = True
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "runner_config_mismatch"):
            self.verify(files={**self.files, components.CONFIG_PATH: canonical_json_bytes(config) + b"\n"})

    def test_schema_deletion_and_self_consistent_manifest_edits_do_not_hide_drift(self) -> None:
        path = "evals/provider_completion_external_preregistration_v1/schemas/closure_receipt_v1.schema.json"
        files = dict(self.files)
        files.pop(path)
        paths = tuple(item for item in self.paths if item != path)
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "bound_file_missing"):
            self.verify(files=files, paths=paths)
        recipe = json.loads(self.files[components.RECIPE_PATH])
        manifest = json.loads(self.documents.implementation_manifest)
        manifest["file_inventory"].pop()
        manifest.pop("commitment_sha256")
        manifest["commitment_sha256"] = hashlib.sha256(
            recipe["domains"]["implementation_manifest"].encode() + b"\0" + canonical_json_bytes(manifest)
        ).hexdigest()
        forged = components.ExecutionComponentDocuments(
            canonical_json_bytes(manifest) + b"\n", self.documents.source_integrity_v6_plan
        )
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "implementation_drift"):
            self.verify(documents=forged)

    def test_v6_authority_fields_and_bytes_are_exact(self) -> None:
        plan = json.loads(self.documents.source_integrity_v6_plan)
        plan["online_execution_authorized"] = True
        forged = components.ExecutionComponentDocuments(
            self.documents.implementation_manifest, canonical_json_bytes(plan) + b"\n"
        )
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "v6_drift"):
            self.verify(documents=forged)
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "artifact_bytes_invalid"):
            components.verify_execution_component_documents(
                self.files, available_paths=self.paths,
                implementation_manifest=bytearray(self.documents.implementation_manifest),
                source_integrity_v6_plan=self.documents.source_integrity_v6_plan,
            )

    def test_mapping_hash_matches_public_offline_selector_without_minting_authority(self) -> None:
        from researchops_completion_telemetry.surface_mapping import load_and_select_surface_mapping
        selected = load_and_select_surface_mapping(
            ROOT, "deepseek", "responses", "openai_compatible_responses", purpose="offline_validation"
        )
        result = self.verify()
        self.assertEqual(result.component_hashes["mapping_sha256"], selected.mapping_sha256)
        self.assertEqual(result.component_hashes["telemetry_schema_sha256"], selected.telemetry_schema_sha256)
        self.assertFalse(selected.runtime_binding_allowed)

    def test_pure_builder_has_no_io_or_authority_constructor(self) -> None:
        with patch("builtins.open", side_effect=AssertionError("unexpected file read")), patch(
            "pathlib.Path.read_bytes", side_effect=AssertionError("unexpected file read")
        ):
            self.verify()
        syntax = ast.parse(Path(components.__file__).read_text(encoding="utf-8"))
        attributes = {node.attr for node in ast.walk(syntax) if isinstance(node, ast.Attribute)}
        self.assertFalse(attributes.intersection({"read_bytes", "write_bytes", "write_text", "open", "environ", "now", "run", "Popen", "create_runtime_binding"}))


if __name__ == "__main__":
    unittest.main()
