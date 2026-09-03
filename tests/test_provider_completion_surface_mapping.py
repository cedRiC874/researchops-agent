from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import researchops_completion_telemetry.surface_mapping as surface_mapping_module
from researchops_completion_telemetry.mapping import map_completion
from researchops_completion_telemetry.surface_mapping import (
    SurfaceMappingError,
    VerifiedRuntimeCompletionBinding,
    VerifiedSurfaceSelection,
    VerifiedSurfaceRegistry,
    create_runtime_completion_binding,
    load_and_select_surface_mapping,
    load_verified_surface_registry,
    select_surface_mapping,
)


ROOT = Path(__file__).resolve().parents[1]
V1_RELATIVE = Path(
    "evals/provider_completion_telemetry_v1/provider_completion_mapping_v1.json"
)
V2_RELATIVE = Path("evals/provider_completion_telemetry_v2")
MANIFEST_NAME = "fixture_manifest_v2.json"
REGISTRY_NAME = "provider_completion_surface_registry_v2.json"


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain an object")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _clone_artifacts() -> tempfile.TemporaryDirectory[str]:
    temporary = tempfile.TemporaryDirectory()
    target = Path(temporary.name)
    shutil.copytree(ROOT / V1_RELATIVE.parent, target / V1_RELATIVE.parent)
    shutil.copytree(ROOT / V2_RELATIVE, target / V2_RELATIVE)
    shutil.copy2(ROOT / "probe_out_v3.json", target / "probe_out_v3.json")
    return temporary


def _refresh_registry_commitment(root: Path) -> None:
    registry_path = root / V2_RELATIVE / REGISTRY_NAME
    raw = registry_path.read_bytes()
    manifest_path = root / V2_RELATIVE / MANIFEST_NAME
    manifest = _load(manifest_path)
    manifest["registry"]["bytes"] = len(raw)
    manifest["registry"]["sha256"] = hashlib.sha256(raw).hexdigest()
    _write(manifest_path, manifest)


def _refresh_fixture_commitment(root: Path, fixture_id: str) -> None:
    manifest_path = root / V2_RELATIVE / MANIFEST_NAME
    manifest = _load(manifest_path)
    entry = next(
        item for item in manifest["fixtures"] if item["fixture_id"] == fixture_id
    )
    path = root / V2_RELATIVE / entry["file"]
    raw = path.read_bytes()
    entry["bytes"] = len(raw)
    entry["sha256"] = hashlib.sha256(raw).hexdigest()
    _write(manifest_path, manifest)


def _refresh_v1_fixture_commitment(root: Path, fixture_id: str) -> None:
    manifest_path = root / V1_RELATIVE.parent / "fixture_manifest.json"
    manifest = _load(manifest_path)
    entry = next(
        item for item in manifest["fixtures"] if item["fixture_id"] == fixture_id
    )
    path = root / V1_RELATIVE.parent / "fixtures" / entry["file"]
    raw = path.read_bytes()
    entry["bytes"] = len(raw)
    entry["sha256"] = hashlib.sha256(raw).hexdigest()
    _write(manifest_path, manifest)


def _refresh_all_probe_commitments(root: Path) -> tuple[int, str]:
    receipt_path = root / "probe_out_v3.json"
    receipt_raw = receipt_path.read_bytes()
    receipt_sha = hashlib.sha256(receipt_raw).hexdigest()
    registry_path = root / V2_RELATIVE / REGISTRY_NAME
    registry = _load(registry_path)
    source = registry["entries"][0]["source"]
    source["bytes"] = len(receipt_raw)
    source["sha256"] = receipt_sha
    _write(registry_path, registry)

    manifest = _load(root / V2_RELATIVE / MANIFEST_NAME)
    fixture_ids = [item["fixture_id"] for item in manifest["fixtures"]]
    for item in manifest["fixtures"]:
        fixture_path = root / V2_RELATIVE / item["file"]
        fixture = _load(fixture_path)
        fixture["provenance"]["source_receipt_sha256"] = receipt_sha
        _write(fixture_path, fixture)
    for fixture_id in fixture_ids:
        _refresh_fixture_commitment(root, fixture_id)
    _refresh_registry_commitment(root)
    return len(receipt_raw), receipt_sha


class ProviderCompletionSurfaceMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.verified = load_verified_surface_registry(ROOT)

    def select(
        self,
        provider_id: str = "deepseek",
        api_surface: str = "responses",
        transport_id: str = "openai_compatible_responses",
        *,
        purpose: str = "offline_validation",
    ) -> VerifiedSurfaceSelection:
        return select_surface_mapping(
            provider_id,
            api_surface,
            transport_id,
            self.verified,
            purpose=purpose,
        )

    def test_loader_verifies_fixed_manifest_registry_predecessor_fixtures_and_probe(self) -> None:
        self.assertIsInstance(self.verified, VerifiedSurfaceRegistry)
        self.assertEqual(self.verified.repository_root, ROOT.resolve())
        self.assertEqual(
            self.verified.fixture_ids(),
            (
                "deepseek_responses_completed_20260903",
                "deepseek_responses_length_capped_20260903",
                "deepseek_responses_missing_fields_20260903",
                "deepseek_responses_unknown_value_20260903",
            ),
        )
        with self.assertRaises(TypeError):
            VerifiedSurfaceRegistry()

    def test_selector_rejects_unverified_plain_dict(self) -> None:
        with self.assertRaises(SurfaceMappingError) as caught:
            select_surface_mapping(
                "deepseek",
                "responses",
                "openai_compatible_responses",
                {},  # type: ignore[arg-type]
                purpose="offline_validation",
            )
        self.assertEqual(caught.exception.code, "surface_registry_not_verified")

        class ForgedVerifiedRegistry(VerifiedSurfaceRegistry):
            pass

        forged = object.__new__(ForgedVerifiedRegistry)
        with self.assertRaises(SurfaceMappingError) as subclass:
            select_surface_mapping(
                "deepseek",
                "responses",
                "openai_compatible_responses",
                forged,
                purpose="offline_validation",
            )
        self.assertEqual(subclass.exception.code, "surface_registry_not_verified")

    def test_verified_registry_is_immutable_and_snapshots_are_defensive(self) -> None:
        snapshot = self.verified.registry_snapshot()
        snapshot["status"] = "mutated-copy"
        self.assertNotEqual(
            self.verified.registry_snapshot()["status"], "mutated-copy"
        )
        with self.assertRaises(AttributeError):
            self.verified._registry = {}  # type: ignore[attr-defined]

    def test_exact_triple_selects_deepseek_responses_offline(self) -> None:
        selection = self.select()
        selected = selection.mapping_snapshot()
        self.assertEqual(selected["schema_version"], "provider-completion-mapping/2.0")
        self.assertEqual(set(selected["providers"]), {"deepseek"})
        metadata = selected["surface_selection"]
        self.assertEqual(metadata["provider_id"], "deepseek")
        self.assertEqual(metadata["api_surface"], "responses")
        self.assertEqual(metadata["transport_id"], "openai_compatible_responses")
        self.assertEqual(selection.purpose, "offline_validation")
        self.assertTrue(metadata["offline_selection_allowed"])
        self.assertFalse(selection.runtime_binding_allowed)
        self.assertEqual(selection.output_counter_path, "output_tokens")
        mapping_bytes = json.dumps(
            selected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(
            selection.mapping_sha256, hashlib.sha256(mapping_bytes).hexdigest()
        )
        schema_path = (
            ROOT
            / "evals/provider_completion_telemetry_v1/schemas/provider_completion_record_v1.schema.json"
        )
        self.assertEqual(
            selection.telemetry_schema_sha256,
            hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        )

    def test_offline_selection_cannot_create_runtime_binding_authority(self) -> None:
        selection = self.select()
        for factory in (
            selection.create_runtime_binding,
            lambda: create_runtime_completion_binding(selection),
        ):
            with self.subTest(factory=factory):
                with self.assertRaises(SurfaceMappingError) as caught:
                    factory()
                self.assertEqual(
                    caught.exception.code, "surface_runtime_authority_missing"
                )
        with self.assertRaises(TypeError):
            VerifiedRuntimeCompletionBinding()
        forged = object.__new__(VerifiedRuntimeCompletionBinding)
        with self.assertRaises(SurfaceMappingError) as authority:
            forged.assert_runtime_authority()
        self.assertEqual(
            authority.exception.code, "surface_runtime_authority_missing"
        )

    def test_load_and_select_entrypoint_also_uses_verified_artifacts(self) -> None:
        selection = load_and_select_surface_mapping(
            ROOT,
            "deepseek",
            "responses",
            "openai_compatible_responses",
            purpose="offline_validation",
        )
        self.assertEqual(selection.provider_id, "deepseek")

    def test_unknown_provider_wrong_surface_and_wrong_transport_fail_closed(self) -> None:
        cases = (
            (
                ("unregistered", "responses", "openai_compatible_responses"),
                "surface_mapping_provider_unknown",
            ),
            (
                (
                    "deepseek",
                    "openai_compatible_chat_completions",
                    "openai_compatible_responses",
                ),
                "surface_mapping_surface_mismatch",
            ),
            (
                ("deepseek", "responses", "openai_responses"),
                "surface_mapping_transport_mismatch",
            ),
        )
        for arguments, code in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(SurfaceMappingError) as caught:
                    select_surface_mapping(
                        *arguments,
                        self.verified,
                        purpose="offline_validation",
                    )
                self.assertEqual(caught.exception.code, code)

    def test_all_current_entries_are_offline_only_and_runtime_gate_is_executable(self) -> None:
        cases = (
            ("deepseek", "responses", "openai_compatible_responses"),
            ("openai", "responses", "openai_responses"),
            ("anthropic", "messages", "litellm_anthropic_chat_completions"),
            (
                "moonshot_kimi",
                "openai_compatible_chat_completions",
                "moonshot_direct_chat_completions_sse_v3",
            ),
        )
        for provider, surface, transport in cases:
            with self.subTest(provider=provider):
                offline = select_surface_mapping(
                    provider,
                    surface,
                    transport,
                    self.verified,
                    purpose="offline_validation",
                )
                self.assertEqual(offline.purpose, "offline_validation")
                self.assertFalse(offline.runtime_binding_allowed)
                with self.assertRaises(SurfaceMappingError) as caught:
                    select_surface_mapping(
                        provider,
                        surface,
                        transport,
                        self.verified,
                        purpose="runtime_binding",
                    )
                self.assertEqual(
                    caught.exception.code, "surface_mapping_runtime_binding_blocked"
                )

    def test_anthropic_and_kimi_transport_boundaries_are_explicit(self) -> None:
        anthropic = self.select(
            "anthropic", "messages", "litellm_anthropic_chat_completions"
        )
        self.assertIn(
            "stop_sequence",
            anthropic.mapping_snapshot()["surface_selection"][
                "runtime_binding_blocker"
            ],
        )
        with self.assertRaises(SurfaceMappingError) as caught:
            self.select(
                "moonshot_kimi",
                "openai_compatible_chat_completions",
                "moonshot_direct_chat_completions_json_v1",
            )
        self.assertEqual(caught.exception.code, "surface_mapping_transport_mismatch")

    def test_v1_alias_metadata_is_not_promoted(self) -> None:
        cases = (
            ("openai", "responses", "openai_responses", "official_schema"),
            (
                "anthropic",
                "messages",
                "litellm_anthropic_chat_completions",
                "official_schema",
            ),
            (
                "moonshot_kimi",
                "openai_compatible_chat_completions",
                "moonshot_direct_chat_completions_sse_v3",
                "doc_prose",
            ),
        )
        for provider, surface, transport, provenance in cases:
            selected = self.select(provider, surface, transport).mapping_snapshot()
            metadata = selected["surface_selection"]
            self.assertEqual(metadata["mapping_source"], "v1_provider_alias")
            self.assertEqual(metadata["provenance_tier"], provenance)
            self.assertTrue(metadata["unverified_shape"])
            self.assertTrue(metadata["first_live_validation_required"])
            self.assertFalse(metadata["provenance_promotion_allowed"])

    def test_deepseek_four_verified_fixtures_execute_exactly(self) -> None:
        selected = self.select().mapping_snapshot()
        expectations = selected["fixture_expectations"]
        self.assertEqual(set(expectations), set(self.verified.fixture_ids()))
        for fixture_id in self.verified.fixture_ids():
            fixture = self.verified.fixture_snapshot(fixture_id)
            expected = expectations[fixture_id]
            self.assertEqual(
                map_completion(fixture["response_projection"], "deepseek", selected),
                (
                    expected["normalized_completion_state"],
                    expected["truncation_signal_source"],
                    expected["preserved_native_value"],
                    expected["matched_rule_id"],
                ),
            )

    def test_deepseek_completed_requires_explicit_null_details(self) -> None:
        selected = self.select().mapping_snapshot()
        cases = (
            (
                {"status": "completed", "incomplete_details": None},
                ("completed", "deepseek-resp-recognized-001"),
            ),
            (
                {"status": "completed"},
                ("unmapped", "deepseek-resp-unknown-007"),
            ),
            (
                {"status": "completed", "incomplete_details": {}},
                ("unmapped", "deepseek-resp-conflict-001"),
            ),
            (
                {"status": "completed", "incomplete_details": "future"},
                ("unmapped", "deepseek-resp-conflict-001"),
            ),
        )
        for projection, expected in cases:
            with self.subTest(projection=projection):
                result = map_completion(projection, "deepseek", selected)
                self.assertEqual((result[0], result[3]), expected)

    def test_deepseek_missing_null_and_incomplete_matrix_is_explicit(self) -> None:
        selected = self.select().mapping_snapshot()
        cases = (
            ({}, ("not_provided", "none", "deepseek-resp-absent-001")),
            (
                {"status": None},
                ("not_provided", "none", "deepseek-resp-absent-002"),
            ),
            (
                {"status": None, "incomplete_details": None},
                ("not_provided", "none", "deepseek-resp-absent-003"),
            ),
            (
                {"incomplete_details": None},
                ("not_provided", "none", "deepseek-resp-absent-004"),
            ),
            (
                {"status": "incomplete"},
                ("unmapped", "native_status", "deepseek-resp-unknown-003"),
            ),
            (
                {"status": "incomplete", "incomplete_details": None},
                ("unmapped", "native_status", "deepseek-resp-unknown-004"),
            ),
            (
                {"status": "incomplete", "incomplete_details": {}},
                ("unmapped", "native_status", "deepseek-resp-unknown-005"),
            ),
            (
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": None},
                },
                ("unmapped", "native_status", "deepseek-resp-unknown-006"),
            ),
        )
        for projection, expected in cases:
            with self.subTest(projection=projection):
                result = map_completion(projection, "deepseek", selected)
                self.assertEqual((result[0], result[1], result[3]), expected)

    def test_unobserved_values_remain_unmapped(self) -> None:
        selected = self.select().mapping_snapshot()
        for status in ("failed", "cancelled", "queued", "in_progress"):
            result = map_completion(
                {"status": status, "incomplete_details": None},
                "deepseek",
                selected,
            )
            self.assertEqual(result[:3], ("unmapped", "native_status", status))
        result = map_completion(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
            },
            "deepseek",
            selected,
        )
        self.assertEqual(
            result[:3], ("unmapped", "native_status", "content_filter")
        )

    def test_manifest_path_traversal_and_duplicate_fixture_id_fail(self) -> None:
        for mutation, expected_code in (
            ("path", "surface_artifact_path_invalid"),
            ("duplicate", "surface_manifest_duplicate_fixture_id"),
        ):
            with self.subTest(mutation=mutation):
                temporary = _clone_artifacts()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                manifest_path = root / V2_RELATIVE / MANIFEST_NAME
                manifest = _load(manifest_path)
                if mutation == "path":
                    manifest["registry"]["file"] = "../outside.json"
                else:
                    manifest["fixtures"].append(copy.deepcopy(manifest["fixtures"][0]))
                _write(manifest_path, manifest)
                with self.assertRaises(SurfaceMappingError) as caught:
                    load_verified_surface_registry(root)
                self.assertEqual(caught.exception.code, expected_code)

    def test_registry_fixture_predecessor_and_probe_tampering_fail(self) -> None:
        cases = (
            (V2_RELATIVE / REGISTRY_NAME, "surface_artifact_commitment_mismatch"),
            (
                V2_RELATIVE / "fixtures/deepseek_responses_completed_20260903.json",
                "surface_artifact_commitment_mismatch",
            ),
            (V1_RELATIVE, "surface_artifact_commitment_mismatch"),
            (Path("probe_out_v3.json"), "surface_artifact_commitment_mismatch"),
        )
        for relative, expected_code in cases:
            with self.subTest(relative=str(relative)):
                temporary = _clone_artifacts()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                with (root / relative).open("ab") as stream:
                    stream.write(b"\n")
                with self.assertRaises(SurfaceMappingError) as caught:
                    load_verified_surface_registry(root)
                self.assertEqual(caught.exception.code, expected_code)

    def test_probe_source_metadata_is_semantically_recomputed(self) -> None:
        mutations = (
            "api_origin",
            "model",
            "probe_id",
            "observed_count",
            "observed_values",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                temporary = _clone_artifacts()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                registry_path = root / V2_RELATIVE / REGISTRY_NAME
                registry = _load(registry_path)
                source = registry["entries"][0]["source"]
                if mutation == "api_origin":
                    source["api_origin"] = "https://wrong.example"
                elif mutation == "model":
                    source["model"] = "wrong-model"
                elif mutation == "probe_id":
                    source["probe_id"] = "wrong_probe"
                elif mutation == "observed_count":
                    source["observed_distinct_shape_count"] = 3
                else:
                    source["observed_native_values"].reverse()
                _write(registry_path, registry)
                _refresh_registry_commitment(root)
                with self.assertRaises(SurfaceMappingError) as caught:
                    load_verified_surface_registry(root)
                self.assertEqual(
                    caught.exception.code, "surface_source_receipt_mismatch"
                )

    def test_unmodified_live_fixture_is_replayed_from_exact_probe(self) -> None:
        mutations = ("projection", "http_status", "request_id", "probe_label")
        fixture_id = "deepseek_responses_completed_20260903"
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                temporary = _clone_artifacts()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                path = root / V2_RELATIVE / "fixtures" / f"{fixture_id}.json"
                fixture = _load(path)
                if mutation == "projection":
                    fixture["response_projection"]["status"] = "failed"
                elif mutation == "http_status":
                    fixture["response_projection"]["http_status"] = 201
                elif mutation == "request_id":
                    fixture["response_projection"]["provider_request_id_sha256"] = "a" * 64
                else:
                    fixture["provenance"]["source_probe_label"] = "unknown_probe"
                _write(path, fixture)
                _refresh_fixture_commitment(root, fixture_id)
                with self.assertRaises(SurfaceMappingError) as caught:
                    load_verified_surface_registry(root)
                self.assertIn(
                    caught.exception.code,
                    {
                        "surface_fixture_shape_invalid",
                        "surface_fixture_live_projection_mismatch",
                    },
                )

    def test_synthetic_fixture_mutation_is_replayed_from_live_base(self) -> None:
        fixture_id = "deepseek_responses_unknown_value_20260903"
        for mutation in ("projection", "operation", "base"):
            with self.subTest(mutation=mutation):
                temporary = _clone_artifacts()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                path = root / V2_RELATIVE / "fixtures" / f"{fixture_id}.json"
                fixture = _load(path)
                if mutation == "projection":
                    fixture["response_projection"]["status"] = "different_unknown"
                elif mutation == "operation":
                    fixture["derivation"]["operations"][0]["path"] = (
                        "/response_projection/usage"
                    )
                else:
                    fixture["derivation"]["base_fixture_id"] = (
                        "deepseek_responses_length_capped_20260903"
                    )
                _write(path, fixture)
                _refresh_fixture_commitment(root, fixture_id)
                with self.assertRaises(SurfaceMappingError) as caught:
                    load_verified_surface_registry(root)
                self.assertIn(
                    caught.exception.code,
                    {
                        "surface_fixture_derivation_invalid",
                        "surface_fixture_derivation_mismatch",
                    },
                )

    def test_usage_condition_and_forged_alias_metadata_fail_during_loader(self) -> None:
        mutations = (
            "usage_condition",
            "alias_promotion",
            "blocked_alias_version",
            "runtime_promotion",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                temporary = _clone_artifacts()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                registry_path = root / V2_RELATIVE / REGISTRY_NAME
                registry = _load(registry_path)
                if mutation == "usage_condition":
                    rule = registry["entries"][0]["provider_mapping"]["active_rules"][0]
                    rule["condition"] = {
                        "op": "equals",
                        "field": "usage.output_tokens",
                        "value": 9,
                    }
                    expected = "surface_mapping_condition_field_forbidden"
                elif mutation == "alias_promotion":
                    entry = registry["entries"][3]
                    entry["provenance_tier"] = "live_capture"
                    entry["unverified_shape"] = False
                    entry["first_live_validation_required"] = False
                    entry["mapping_version"] = "forged-live-v99"
                    expected = "surface_registry_v2_runtime_promotion_forbidden"
                else:
                    if mutation == "blocked_alias_version":
                        registry["entries"][2]["mapping_version"] = "forged-blocked-v99"
                        expected = "surface_alias_metadata_mismatch"
                    else:
                        entry = registry["entries"][0]
                        entry["runtime_binding_allowed"] = True
                        entry["first_live_validation_required"] = False
                        entry["provider_mapping"]["first_live_validation_required"] = False
                        expected = "surface_registry_v2_runtime_promotion_forbidden"
                _write(registry_path, registry)
                _refresh_registry_commitment(root)
                with self.assertRaises(SurfaceMappingError) as caught:
                    load_verified_surface_registry(root)
                self.assertEqual(caught.exception.code, expected)

    def test_v2_fixture_expectation_is_executed_by_the_loader(self) -> None:
        with _clone_artifacts() as directory:
            root = Path(directory)
            registry_path = root / V2_RELATIVE / REGISTRY_NAME
            registry = _load(registry_path)
            registry["entries"][0]["fixture_expectations"][
                "deepseek_responses_completed_20260903"
            ]["normalized_completion_state"] = "incomplete_other"
            _write(registry_path, registry)
            _refresh_registry_commitment(root)
            with self.assertRaises(SurfaceMappingError) as caught:
                load_verified_surface_registry(root)
            self.assertEqual(
                caught.exception.code, "surface_fixture_expectation_invalid"
            )

    def test_v1_fixture_privacy_and_usage_rules_survive_refreshed_hashes(self) -> None:
        mutations = (
            "extra_body",
            "secret_source",
            "usage_leaves",
            "usage_depth",
            "usage_bytes",
            "provenance_upgrade",
            "mapping_result",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), _clone_artifacts() as directory:
                root = Path(directory)
                fixture_id = (
                    "deepseek_missing_fields_20260902"
                    if mutation == "provenance_upgrade"
                    else "deepseek_completed_20260902"
                )
                fixture_path = (
                    root
                    / V1_RELATIVE.parent
                    / "fixtures"
                    / f"{fixture_id}.json"
                )
                fixture = _load(fixture_path)
                if mutation == "extra_body":
                    fixture["raw_response_body"] = "LEAK-SENTINEL"
                    expected = "surface_v1_fixture_invalid"
                elif mutation == "secret_source":
                    fixture["provenance"]["source"]["model"] = (
                        "sk-forbidden-fixture-secret"
                    )
                    expected = "surface_artifact_sensitive_value"
                elif mutation == "usage_leaves":
                    fixture["response_projection"]["usage"] = {
                        f"counter_{index}": index for index in range(65)
                    }
                    expected = "surface_fixture_usage_invalid"
                elif mutation == "usage_depth":
                    fixture["response_projection"]["usage"] = {
                        "a": {"b": {"c": {"d": {"e": 1}}}}
                    }
                    expected = "surface_fixture_usage_invalid"
                elif mutation == "usage_bytes":
                    fixture["response_projection"]["usage"] = {
                        f"counter_{index}_{'x' * 54}": index for index in range(64)
                    }
                    expected = "surface_fixture_usage_invalid"
                elif mutation == "provenance_upgrade":
                    fixture["provenance"]["unverified_shape"] = False
                    expected = "surface_v1_fixture_invalid"
                else:
                    fixture["response_projection"]["finish_reason"] = "future_status"
                    expected = "surface_v1_fixture_invalid"
                _write(fixture_path, fixture)
                _refresh_v1_fixture_commitment(root, fixture_id)
                with self.assertRaises(SurfaceMappingError) as caught:
                    load_verified_surface_registry(root)
                self.assertEqual(caught.exception.code, expected)

    def test_v2_fixture_schema_usage_and_provenance_survive_refreshed_hashes(self) -> None:
        mutations = ("extra_body", "usage_leaves", "provenance_upgrade")
        for mutation in mutations:
            with self.subTest(mutation=mutation), _clone_artifacts() as directory:
                root = Path(directory)
                fixture_id = (
                    "deepseek_responses_missing_fields_20260903"
                    if mutation == "provenance_upgrade"
                    else "deepseek_responses_completed_20260903"
                )
                fixture_path = (
                    root / V2_RELATIVE / "fixtures" / f"{fixture_id}.json"
                )
                fixture = _load(fixture_path)
                if mutation == "extra_body":
                    fixture["message_content"] = "LEAK-SENTINEL"
                    expected = "surface_fixture_shape_invalid"
                elif mutation == "usage_leaves":
                    fixture["response_projection"]["usage"] = {
                        f"counter_{index}": index for index in range(65)
                    }
                    expected = "surface_fixture_usage_invalid"
                else:
                    fixture["provenance"]["unverified_shape"] = False
                    expected = "surface_fixture_shape_invalid"
                _write(fixture_path, fixture)
                _refresh_fixture_commitment(root, fixture_id)
                with self.assertRaises(SurfaceMappingError) as caught:
                    load_verified_surface_registry(root)
                self.assertEqual(caught.exception.code, expected)

    def test_probe_semantics_reject_refreshed_receipt_registry_and_manifest_hashes(self) -> None:
        mutations = ("extra_raw_body", "usage_secret", "semantic_shape")
        for mutation in mutations:
            with self.subTest(mutation=mutation), _clone_artifacts() as directory:
                root = Path(directory)
                receipt_path = root / "probe_out_v3.json"
                receipt = _load(receipt_path)
                if mutation == "extra_raw_body":
                    receipt["raw_response_body"] = "LEAK-SENTINEL"
                    expected = "surface_source_receipt_shape_invalid"
                elif mutation == "usage_secret":
                    receipt["probes"][0]["response_projection"]["usage"][
                        "provider_note"
                    ] = "sk-forbidden-probe-secret"
                    expected = "surface_fixture_usage_invalid"
                else:
                    receipt["probes"][2]["output_item_shapes"] = [
                        {
                            "index": 0,
                            "type": "message",
                            "status_key_present": True,
                            "status": "incomplete",
                        }
                    ]
                    expected = "surface_source_receipt_shape_invalid"
                _write(receipt_path, receipt)
                receipt_bytes, receipt_sha = _refresh_all_probe_commitments(root)
                with patch.object(
                    surface_mapping_module,
                    "_EXPECTED_PROBE_RECEIPT_BYTES",
                    receipt_bytes,
                ), patch.object(
                    surface_mapping_module,
                    "_EXPECTED_PROBE_RECEIPT_SHA256",
                    receipt_sha,
                ), self.assertRaises(SurfaceMappingError) as caught:
                    load_verified_surface_registry(root)
                self.assertEqual(caught.exception.code, expected)

    def test_probe_provenance_and_message_observation_scope_are_explicit(self) -> None:
        registry = _load(ROOT / V2_RELATIVE / REGISTRY_NAME)
        source = registry["entries"][0]["source"]
        self.assertEqual(
            source["provenance_limitations"],
            {
                "capture_time": "not_persisted",
                "authorization_linkage": "external_not_machine_bound",
                "probe_script_linkage": "external_not_machine_bound",
                "predecessor_receipt_bytes": "v1_v2_excluded_unavailable",
                "message_output_item_observed_scope": (
                    "responses_message_stage_cap_attempt_only"
                ),
                "normal_probe_message_completed_observed": True,
            },
        )
        receipt = _load(ROOT / "probe_out_v3.json")
        self.assertFalse(receipt["limitations"]["message_output_item_observed"])
        normal = receipt["probes"][0]["output_item_shapes"]
        self.assertTrue(
            any(
                item.get("type") == "message" and item.get("status") == "completed"
                for item in normal
            )
        )

    def test_synthetic_fixture_provenance_names_source_and_fixture_kind_separately(self) -> None:
        for fixture_id in self.verified.fixture_ids():
            fixture = self.verified.fixture_snapshot(fixture_id)
            provenance = fixture["provenance"]
            self.assertEqual(provenance["source_tier"], "live_capture")
            expected_kind = (
                "synthetic_mutation"
                if "derivation" in fixture
                else "unmodified_live_projection"
            )
            self.assertEqual(provenance["fixture_kind"], expected_kind)
            self.assertNotIn("tier", provenance)


if __name__ == "__main__":
    unittest.main()
