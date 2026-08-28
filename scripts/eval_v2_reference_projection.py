from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import tempfile
from pathlib import Path
from typing import Any, Sequence


PACKAGE_ROOT = Path("evals/v2/external_review_pre_results_v2")
STAT_ROOT = PACKAGE_ROOT / "statistical_crosscheck"
REFERENCE_SOURCE_MANIFEST_PATH = STAT_ROOT / "reference_source_manifest.json"
DATA_PATH = Path("data/synthetic_trial.csv")
DESIGN_PATH = Path("data/synthetic_trial_design.json")
EXPECTED_DATA_SHA256 = "7ae3c201ccb543b5c647c8c50b2a754294d1d62aaaa458d0f2fb4b0af990ca00"
EXPECTED_DESIGN_SHA256 = "e8ab569e2f877028431d58c3a676d68917d67237303cc194705b5850d400938b"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REFERENCE_SOURCE_PATHS = (
    "scripts/eval_v2_statistical_compare.py",
    "scripts/eval_v2_reference_projection.py",
    "src/researchops/analysis_tools.py",
    "src/researchops/contracts.py",
    "src/researchops/data_quality.py",
    "requirements.lock",
    "data/synthetic_trial.csv",
    "data/synthetic_trial_design.json",
)


class ReferenceProjectionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_bundle_v1(root: Path, relative_paths: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(relative_paths):
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
            raise ReferenceProjectionError("reference_source_bundle_invalid")
        path_bytes = relative.encode("utf-8")
        content = path.read_bytes()
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _verify_reference_source_manifest(root: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((root / REFERENCE_SOURCE_MANIFEST_PATH).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ReferenceProjectionError("reference_source_manifest_invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("status") != "pre_results_locked":
        raise ReferenceProjectionError("reference_source_manifest_invalid")
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) != len(REFERENCE_SOURCE_PATHS):
        raise ReferenceProjectionError("reference_source_manifest_invalid")
    if {entry.get("path") for entry in entries if isinstance(entry, dict)} != set(REFERENCE_SOURCE_PATHS):
        raise ReferenceProjectionError("reference_source_manifest_invalid")
    preimage = "researchops-stat-xcheck-reference-source-v1\n"
    for entry in sorted(entries, key=lambda item: item["path"]):
        path = (root / entry["path"]).resolve(strict=True)
        if (
            not path.is_relative_to(root)
            or path.is_symlink()
            or path.stat().st_size != entry.get("bytes")
            or _sha256(path) != entry.get("sha256")
        ):
            raise ReferenceProjectionError("reference_source_manifest_mismatch")
        preimage += f'{entry["path"]}\t{entry["bytes"]}\t{entry["sha256"]}\n'
    actual = hashlib.sha256(preimage.encode("utf-8")).hexdigest()
    if actual != manifest.get("reference_source_bundle_sha256"):
        raise ReferenceProjectionError("reference_source_manifest_mismatch")
    package = json.loads((root / PACKAGE_ROOT / "package_commitments.json").read_text(encoding="utf-8"))
    if (
        manifest.get("base_commit_sha") != package.get("base_commit_sha")
        or manifest.get("base_tree_sha") != package.get("base_tree_sha")
    ):
        raise ReferenceProjectionError("reference_source_manifest_mismatch")
    return manifest


def _canonical_exact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _canonical_decimal(value: Any) -> str:
    number = float(value)
    if not (-float("inf") < number < float("inf")):
        raise ReferenceProjectionError("reference_numeric_value_invalid")
    return format(number, ".16e")


def _sample_flow(flow: Any) -> dict[str, Any]:
    control = flow.by_group["control"]
    treatment = flow.by_group["treatment"]
    return {
        "source_rows": flow.source_rows,
        "included_rows": flow.included_rows,
        "excluded_rows": flow.excluded_rows,
        "required_columns": list(flow.required_columns),
        "missing_by_column": dict(flow.missing_by_column),
        "control_source": control["source_rows"],
        "control_included": control["included_rows"],
        "control_excluded": control["excluded_rows"],
        "treatment_source": treatment["source_rows"],
        "treatment_included": treatment["included_rows"],
        "treatment_excluded": treatment["excluded_rows"],
        "realized_population": "available_case" if flow.excluded_rows else "intention_to_treat",
    }


def _reference_anchor_values(root: Path) -> dict[str, Any]:
    import pandas as pd

    from researchops.analysis_tools import run_ancova, run_welch_t_test
    from researchops.contracts import ResearchDesign
    from researchops.data_quality import profile_csv

    data_path = root / DATA_PATH
    design_path = root / DESIGN_PATH
    if _sha256(data_path) != EXPECTED_DATA_SHA256 or _sha256(design_path) != EXPECTED_DESIGN_SHA256:
        raise ReferenceProjectionError("reference_input_hash_mismatch")
    frame = pd.read_csv(data_path, encoding="utf-8-sig", low_memory=False)
    profile = profile_csv(data_path)
    design = ResearchDesign.from_dict(json.loads(design_path.read_text(encoding="utf-8")))
    ancova = run_ancova(frame, profile, design)
    welch = run_welch_t_test(frame, profile, design)

    ancova_contrast = ancova.estimates["contrast"]
    control_adjusted = ancova.estimates["reference_group"]["adjusted_mean"]
    treatment_adjusted = ancova.estimates["contrast_group"]["adjusted_mean"]
    interaction = ancova.diagnostics["slope_homogeneity"][0]
    interaction_t = interaction["slope_difference"] / interaction["standard_error"]
    welch_contrast = welch.estimates["contrast"]
    control = welch.estimates["reference_group"]
    treatment = welch.estimates["contrast_group"]
    cohen_d = welch_contrast["cohen_d_pooled_sd"]
    hedges_g = welch_contrast["hedges_g_pooled_sd"]
    pooled_sd = welch_contrast["mean_difference"] / cohen_d
    correction = hedges_g / cohen_d

    anchors: dict[str, dict[str, Any]] = {
        "RXC-SF-ANCOVA-001": _sample_flow(ancova.sample_flow),
        "RXC-ANCOVA-HC3-001": {
            "adjusted_difference": ancova_contrast["adjusted_mean_difference"],
            "hc3_standard_error": ancova_contrast["standard_error_hc3"],
            "t_statistic": ancova.test["statistic"],
            "residual_df": ancova.test["degrees_of_freedom"],
            "two_sided_p_value": ancova.test["p_value"],
            "confidence_interval_low": ancova_contrast["confidence_interval"]["lower"],
            "confidence_interval_high": ancova_contrast["confidence_interval"]["upper"],
        },
        "RXC-ANCOVA-MEANS-001": {
            "baseline_center": ancova.input_spec["covariate_center_values"]["baseline_sbp"],
            "control_adjusted_mean": control_adjusted["estimate"],
            "control_standard_error": control_adjusted["standard_error_hc3"],
            "control_ci_low": control_adjusted["confidence_interval"]["lower"],
            "control_ci_high": control_adjusted["confidence_interval"]["upper"],
            "treatment_adjusted_mean": treatment_adjusted["estimate"],
            "treatment_standard_error": treatment_adjusted["standard_error_hc3"],
            "treatment_ci_low": treatment_adjusted["confidence_interval"]["lower"],
            "treatment_ci_high": treatment_adjusted["confidence_interval"]["upper"],
        },
        "RXC-ANCOVA-SLOPE-001": {
            "interaction_estimate": interaction["slope_difference"],
            "hc3_standard_error": interaction["standard_error"],
            "t_statistic": interaction_t,
            "residual_df": interaction["degrees_of_freedom"],
            "two_sided_p_value": interaction["p_value"],
            "confidence_interval_low": interaction["confidence_interval"]["lower"],
            "confidence_interval_high": interaction["confidence_interval"]["upper"],
        },
        "RXC-SF-WELCH-001": _sample_flow(welch.sample_flow),
        "RXC-WELCH-001": {
            "control_n": control["n"],
            "control_mean": control["mean"],
            "control_sample_sd": control["standard_deviation"],
            "treatment_n": treatment["n"],
            "treatment_mean": treatment["mean"],
            "treatment_sample_sd": treatment["standard_deviation"],
            "difference": welch_contrast["mean_difference"],
            "standard_error": welch_contrast["standard_error"],
            "welch_df": welch.test["degrees_of_freedom"],
            "t_statistic": welch.test["statistic"],
            "two_sided_p_value": welch.test["p_value"],
            "confidence_interval_low": welch_contrast["confidence_interval"]["lower"],
            "confidence_interval_high": welch_contrast["confidence_interval"]["upper"],
        },
        "RXC-WELCH-HG-001": {
            "pooled_standard_deviation": pooled_sd,
            "cohen_d": cohen_d,
            "hedges_exact_correction": correction,
            "hedges_g": hedges_g,
        },
    }
    return {
        f"{anchor_id}.{field}": value
        for anchor_id, values in anchors.items()
        for field, value in values.items()
    }


def _field_rows(universe: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for anchor in universe["anchors"]:
        anchor_id = anchor["anchor_id"]
        if anchor_id == "RXC-RUNTIME-001":
            continue
        fields = anchor["fields"]
        if isinstance(fields, dict):
            for field, comparison_class in fields.items():
                rows.append((f"{anchor_id}.{field}", "exact" if comparison_class == "exact_numeric" else "numeric"))
        else:
            rows.extend((f"{anchor_id}.{field}", anchor["comparison_mode"]) for field in fields)
    if len(rows) != 64 or len({path for path, _ in rows}) != 64:
        raise ReferenceProjectionError("reference_field_universe_invalid")
    return rows


def _projection_commitment(document: dict[str, Any]) -> str:
    body = dict(document)
    body.pop("reference_projection_commitment_sha256", None)
    _assert_ascii_no_float(body)
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(
        b"researchops-stat-xcheck-reference-projection-v1\n" + canonical.encode("utf-8")
    ).hexdigest()


def _assert_ascii_no_float(value: Any) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > 9_007_199_254_740_991:
            raise ReferenceProjectionError("reference_commitment_value_invalid")
        return
    if isinstance(value, float):
        raise ReferenceProjectionError("reference_commitment_value_invalid")
    if isinstance(value, str):
        try:
            value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ReferenceProjectionError("reference_commitment_value_invalid") from exc
        return
    if isinstance(value, list):
        for item in value:
            _assert_ascii_no_float(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReferenceProjectionError("reference_commitment_value_invalid")
            _assert_ascii_no_float(key)
            _assert_ascii_no_float(item)
        return
    raise ReferenceProjectionError("reference_commitment_value_invalid")


def build_reference_projection(
    *, project_root: Path, package_commitment: str, delivery_commitment: str, pre_governance_commitment: str
) -> dict[str, Any]:
    root = project_root.resolve(strict=True)
    if not all(
        SHA256_PATTERN.fullmatch(value)
        for value in (package_commitment, delivery_commitment, pre_governance_commitment)
    ):
        raise ReferenceProjectionError("reference_binding_invalid")
    source_manifest = _verify_reference_source_manifest(root)
    universe_path = root / STAT_ROOT / "comparison_field_universe.json"
    universe = json.loads(universe_path.read_text(encoding="utf-8"))
    reference_values = _reference_anchor_values(root)
    source_manifest_after = _verify_reference_source_manifest(root)
    if source_manifest_after != source_manifest:
        raise ReferenceProjectionError("reference_source_manifest_changed_during_run")
    values = []
    for field_path, mode in _field_rows(universe):
        value = reference_values[field_path]
        canonical = _canonical_exact(value) if mode == "exact" else _canonical_decimal(value)
        values.append(
            {
                "field_path": field_path,
                "canonical_value": canonical,
                "value_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            }
        )
    from researchops.analysis_tools import runtime_versions

    runtime_payload = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": runtime_versions(),
        "requirements_lock_sha256": _sha256(root / "requirements.lock"),
    }
    runtime_commitment = hashlib.sha256(
        json.dumps(runtime_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    document = {
        "schema_version": "1.0",
        "document_type": "sanitized_statistical_reference_projection",
        "public_package_commitment_sha256": package_commitment,
        "statistical_delivery_commitment_sha256": delivery_commitment,
        "pre_governance_commitment_sha256": pre_governance_commitment,
        "input_dataset_sha256": EXPECTED_DATA_SHA256,
        "input_design_sha256": EXPECTED_DESIGN_SHA256,
        "field_universe_sha256": _sha256(universe_path),
        "reference_source_manifest_sha256": _sha256(root / REFERENCE_SOURCE_MANIFEST_PATH),
        "reference_source_bundle_sha256": source_manifest["reference_source_bundle_sha256"],
        "reference_generator_sha256": next(
            entry["sha256"]
            for entry in source_manifest["files"]
            if entry["path"] == "scripts/eval_v2_reference_projection.py"
        ),
        "requirements_lock_sha256": _sha256(root / "requirements.lock"),
        "reference_runtime_manifest_sha256": runtime_commitment,
        "value_count": 64,
        "values": values,
        "privacy": {
            "raw_rows_included": False,
            "direct_identifiers_included": False,
            "local_paths_included": False,
            "private_content_included": False,
            "provider_or_model_content_included": False,
        },
    }
    document["reference_projection_commitment_sha256"] = _projection_commitment(document)
    return document


def _safe_output(project_root: Path, output: Path) -> Path:
    root = project_root.resolve()
    target = output.resolve(strict=False)
    if target.is_relative_to(root) or target.exists() or target.parent.is_symlink() or not target.parent.is_dir():
        raise ReferenceProjectionError("reference_output_path_invalid")
    return target


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    descriptor: int | None = None
    temp_name: str | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix=".reference-projection-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--pre-governance-commitment", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--confirm-generate", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_generate:
        print(json.dumps({"status": "not_run", "error_code": "reference_generation_confirmation_required", "output_written": False, "network_calls": 0}, sort_keys=True))
        return 4
    try:
        root = Path(args.project_root).resolve(strict=True)
        package = json.loads((root / PACKAGE_ROOT / "package_commitments.json").read_text(encoding="utf-8"))
        delivery = json.loads((root / STAT_ROOT / "detached_delivery_manifest.json").read_text(encoding="utf-8"))
        projection = build_reference_projection(
            project_root=root,
            package_commitment=package["package_commitment_sha256"],
            delivery_commitment=delivery["role_delivery_commitment_sha256"],
            pre_governance_commitment=args.pre_governance_commitment,
        )
        target = _safe_output(root, Path(args.output))
        _write_atomic(target, projection)
    except Exception as exc:
        code = exc.code if isinstance(exc, ReferenceProjectionError) else "reference_generation_failed"
        print(json.dumps({"status": "failed", "error_code": code, "output_written": False, "network_calls": 0}, sort_keys=True))
        return 4
    print(json.dumps({"status": "completed", "field_count": 64, "output_written": True, "network_calls": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
