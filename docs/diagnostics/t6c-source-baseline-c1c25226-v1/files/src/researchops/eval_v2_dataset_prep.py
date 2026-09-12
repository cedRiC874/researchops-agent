from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from .data_quality import profile_csv
from .eval_v2_contracts import EVAL_V2_SCHEMA_VERSION, EvalV2ContractError
from .eval_v2_dataset_verify import ByteFetcher, download_verified_dataset
from .eval_v2_public import VerifiedDataset, load_eval_v2_dataset_manifest


PREPARATION_VERSION = "1.0"
REGISTRY_SCHEMA_VERSION = "1.0"
_LOGICAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_REGISTRY_FIELDS = frozenset(
    {"schema_version", "registry_id", "dataset_manifest_sha256", "entries"}
)
_REGISTRY_ENTRY_FIELDS = frozenset(
    {
        "dataset_id",
        "relative_path",
        "prepared_sha256",
        "prepared_bytes",
        "row_count",
        "column_count",
        "source_asset_sha256",
        "preparation_version",
        "privacy_class",
        "model_access",
        "domain",
        "repeated_subjects",
        "analysis_boundaries",
        "transformations",
    }
)
_HEART_COLUMNS = (
    "age",
    "sex",
    "chest_pain_type",
    "resting_blood_pressure",
    "cholesterol",
    "fasting_blood_sugar_high",
    "resting_ecg",
    "maximum_heart_rate",
    "exercise_induced_angina",
    "st_depression",
    "st_slope",
    "major_vessels",
    "thalassemia",
    "heart_disease_class",
)
_PARKINSONS_COLUMNS = (
    "subject_key",
    "age",
    "sex",
    "test_time",
    "motor_updrs",
    "total_updrs",
    "jitter_percent",
    "jitter_abs",
    "jitter_rap",
    "jitter_ppq5",
    "jitter_ddp",
    "shimmer",
    "shimmer_db",
    "shimmer_apq3",
    "shimmer_apq5",
    "shimmer_apq11",
    "shimmer_dda",
    "nhr",
    "hnr",
    "rpde",
    "dfa",
    "ppe",
)


@dataclass(frozen=True)
class PreparedDatasetHandle:
    dataset_id: str
    path: Path
    prepared_sha256: str
    row_count: int
    column_count: int
    domain: str
    repeated_subjects: bool
    analysis_boundaries: tuple[str, ...]

    def public_metadata(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "domain": self.domain,
            "repeated_subjects": self.repeated_subjects,
            "analysis_boundaries": list(self.analysis_boundaries),
            "model_access": "aggregate_tools_only",
        }


class EvalV2LogicalDatasetRegistry:
    """Resolve logical dataset IDs only after path and hash revalidation."""

    def __init__(
        self,
        *,
        registry_path: Path,
        dataset_manifest_sha256: str,
        entries: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self._registry_path = registry_path.resolve()
        self._root = self._registry_path.parent
        self.dataset_manifest_sha256 = dataset_manifest_sha256
        self._entries = MappingProxyType(
            {dataset_id: MappingProxyType(dict(entry)) for dataset_id, entry in entries.items()}
        )

    @classmethod
    def load(cls, registry_path: str | Path) -> "EvalV2LogicalDatasetRegistry":
        source = Path(registry_path).resolve()
        payload = _load_strict_json(source)
        _require_exact_fields(payload, _REGISTRY_FIELDS, "registry")
        if payload["schema_version"] != REGISTRY_SCHEMA_VERSION:
            raise EvalV2ContractError(
                "eval_v2_registry_schema_invalid", "registry schema_version 无效。"
            )
        _logical_id(payload["registry_id"], "registry_id")
        manifest_hash = _sha256_value(
            payload["dataset_manifest_sha256"], "dataset_manifest_sha256"
        )
        raw_entries = payload["entries"]
        if not isinstance(raw_entries, list) or not raw_entries:
            raise EvalV2ContractError(
                "eval_v2_registry_invalid", "registry entries 必须是非空数组。"
            )
        entries: dict[str, Mapping[str, Any]] = {}
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, Mapping):
                raise EvalV2ContractError(
                    "eval_v2_registry_invalid", "registry entry 必须是对象。"
                )
            _require_exact_fields(raw_entry, _REGISTRY_ENTRY_FIELDS, "registry entry")
            dataset_id = _logical_id(raw_entry["dataset_id"], "entry.dataset_id")
            if dataset_id in entries:
                raise EvalV2ContractError(
                    "eval_v2_registry_duplicate_id", f"重复 dataset_id：{dataset_id}。"
                )
            _validate_registry_entry(raw_entry, dataset_id)
            entries[dataset_id] = dict(raw_entry)
        return cls(
            registry_path=source,
            dataset_manifest_sha256=manifest_hash,
            entries=entries,
        )

    @property
    def dataset_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def resolve(self, dataset_id: str) -> PreparedDatasetHandle:
        normalized = _logical_id(dataset_id, "dataset_id")
        entry = self._entries.get(normalized)
        if entry is None:
            raise EvalV2ContractError(
                "eval_v2_dataset_not_authorized", "未知或未授权的 Eval v2 dataset_id。"
            )
        relative = PurePosixPath(str(entry["relative_path"]))
        path = (self._root / Path(*relative.parts)).resolve()
        if not path.is_relative_to(self._root) or not path.is_file():
            raise EvalV2ContractError(
                "eval_v2_prepared_dataset_missing", f"dataset {normalized} 准备产物不存在。"
            )
        if path.stat().st_size != entry["prepared_bytes"] or _sha256_file(path) != entry["prepared_sha256"]:
            raise EvalV2ContractError(
                "eval_v2_prepared_dataset_tampered",
                f"dataset {normalized} 准备产物 hash/size 不匹配。",
            )
        return PreparedDatasetHandle(
            dataset_id=normalized,
            path=path,
            prepared_sha256=str(entry["prepared_sha256"]),
            row_count=int(entry["row_count"]),
            column_count=int(entry["column_count"]),
            domain=str(entry["domain"]),
            repeated_subjects=bool(entry["repeated_subjects"]),
            analysis_boundaries=tuple(entry["analysis_boundaries"]),
        )

    def public_catalog(self) -> list[dict[str, Any]]:
        return [self.resolve(dataset_id).public_metadata() for dataset_id in self.dataset_ids]


def prepare_eval_v2_datasets(
    *,
    project_root: str | Path,
    dataset_manifest_path: str | Path,
    output_directory: str | Path,
    confirm_download: bool,
    timeout_seconds: float = 30.0,
    fetcher: ByteFetcher | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    manifest_source = Path(dataset_manifest_path).resolve()
    manifest = load_eval_v2_dataset_manifest(manifest_source)
    output = _validate_output_directory(root, Path(output_directory))
    if not confirm_download:
        return {
            "status": "not_run",
            "reason_code": "explicit_download_confirmation_required",
            "dataset_count": len(manifest.datasets),
            "network_calls": 0,
            "files_written": 0,
        }
    if output.exists():
        raise EvalV2ContractError(
            "eval_v2_output_exists", "准备输出目录已存在；不会覆盖。"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".eval-v2-prepare-", dir=output.parent)
    ).resolve()
    entries: list[dict[str, Any]] = []
    try:
        for dataset in manifest.datasets:
            verified = download_verified_dataset(
                dataset,
                timeout_seconds=timeout_seconds,
                fetcher=fetcher,
            )
            prepared_bytes, transformations = _prepare_dataset(
                dataset, verified.selected_bytes
            )
            relative_path = f"{dataset.dataset_id}.csv"
            prepared_path = staging / relative_path
            prepared_path.write_bytes(prepared_bytes)
            profile = profile_csv(prepared_path)
            if profile.row_count != dataset.row_count or profile.column_count != dataset.column_count:
                raise EvalV2ContractError(
                    "eval_v2_prepared_structure_mismatch",
                    f"dataset {dataset.dataset_id} 准备后结构不匹配。",
                )
            entries.append(
                {
                    "dataset_id": dataset.dataset_id,
                    "relative_path": relative_path,
                    "prepared_sha256": profile.sha256,
                    "prepared_bytes": len(prepared_bytes),
                    "row_count": profile.row_count,
                    "column_count": profile.column_count,
                    "source_asset_sha256": dataset.selected_asset_sha256,
                    "preparation_version": PREPARATION_VERSION,
                    "privacy_class": _privacy_class(dataset.dataset_id),
                    "model_access": "aggregate_tools_only",
                    "domain": dataset.domain,
                    "repeated_subjects": dataset.repeated_subjects,
                    "analysis_boundaries": list(dataset.analysis_boundaries),
                    "transformations": list(transformations),
                }
            )
        registry = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "registry_id": "researchops-eval-v2-logical-datasets-v1",
            "dataset_manifest_sha256": _sha256_file(manifest_source),
            "entries": entries,
        }
        _write_json(staging / "logical_dataset_registry.json", registry)
        preparation_manifest = {
            "schema_version": EVAL_V2_SCHEMA_VERSION,
            "status": "prepared",
            "preparation_version": PREPARATION_VERSION,
            "dataset_manifest_sha256": _sha256_file(manifest_source),
            "dataset_count": len(entries),
            "network_calls": len(entries),
            "raw_downloads_persisted": False,
            "model_row_access": False,
            "files": [
                {
                    "dataset_id": entry["dataset_id"],
                    "file_name": entry["relative_path"],
                    "sha256": entry["prepared_sha256"],
                    "byte_size": entry["prepared_bytes"],
                }
                for entry in entries
            ],
        }
        _write_json(staging / "preparation_manifest.json", preparation_manifest)
        staged_registry_path = staging / "logical_dataset_registry.json"
        staged_registry = EvalV2LogicalDatasetRegistry.load(staged_registry_path)
        staged_registry.public_catalog()
        dataset_ids = list(staged_registry.dataset_ids)
        staging.replace(output)
        registry_path = output / "logical_dataset_registry.json"
        return {
            "status": "prepared",
            "dataset_count": len(entries),
            "network_calls": len(entries),
            "raw_downloads_persisted": False,
            "model_row_access": False,
            "output_directory": output.relative_to(root).as_posix(),
            "registry": (registry_path.relative_to(root)).as_posix(),
            "dataset_ids": dataset_ids,
        }
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _prepare_dataset(
    dataset: VerifiedDataset, selected_bytes: bytes
) -> tuple[bytes, tuple[str, ...]]:
    text = selected_bytes.decode("utf-8-sig")
    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    if dataset.has_header:
        source_header = rows[0]
        data_rows = rows[1:]
    else:
        source_header = list(_HEART_COLUMNS)
        data_rows = rows

    if dataset.dataset_id == "uci_parkinsons_telemonitoring_189":
        if len(source_header) != len(_PARKINSONS_COLUMNS):
            raise EvalV2ContractError(
                "eval_v2_preparation_schema_mismatch", "Parkinsons 源表头列数变化。"
            )
        header = list(_PARKINSONS_COLUMNS)
        transformed_rows = []
        for row in data_rows:
            normalized = _normalize_missing(row, dataset.missing_tokens)
            normalized[0] = _subject_key(dataset.dataset_id, normalized[0])
            transformed_rows.append(normalized)
        transformations = (
            "normalize_headers_to_snake_case",
            "replace_missing_tokens_with_empty_csv_cells",
            "pseudonymize_subject_number_with_sha256_prefix",
            "drop_original_subject_number",
        )
    elif dataset.dataset_id == "uci_heart_disease_cleveland_45":
        header = list(_HEART_COLUMNS)
        transformed_rows = [
            _normalize_missing(row, dataset.missing_tokens) for row in data_rows
        ]
        transformations = (
            "attach_verified_processed_cleveland_headers",
            "replace_question_mark_missing_tokens_with_empty_csv_cells",
            "exclude_unprocessed_identifier_columns",
        )
    elif dataset.dataset_id == "palmer_penguins_v0_1_0":
        header = [_safe_header(name) for name in source_header]
        transformed_rows = [
            _normalize_missing(row, dataset.missing_tokens) for row in data_rows
        ]
        transformations = (
            "normalize_headers_to_snake_case",
            "replace_missing_tokens_with_empty_csv_cells",
            "retain_curated_eight_column_view_without_individual_id",
        )
    else:
        raise EvalV2ContractError(
            "eval_v2_preparer_not_registered", "dataset 没有注册受控准备器。"
        )
    if len(header) != dataset.column_count or len(set(header)) != len(header):
        raise EvalV2ContractError(
            "eval_v2_preparation_schema_mismatch", f"dataset {dataset.dataset_id} 表头无效。"
        )
    if len(transformed_rows) != dataset.row_count or any(
        len(row) != len(header) for row in transformed_rows
    ):
        raise EvalV2ContractError(
            "eval_v2_preparation_schema_mismatch", f"dataset {dataset.dataset_id} 行列数变化。"
        )
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(transformed_rows)
    return output.getvalue().encode("utf-8"), transformations


def _normalize_missing(row: list[str], missing_tokens: tuple[str, ...]) -> list[str]:
    tokens = set(missing_tokens)
    return ["" if value.strip() in tokens else value.strip() for value in row]


def _subject_key(dataset_id: str, subject_value: str) -> str:
    if not subject_value:
        raise EvalV2ContractError(
            "eval_v2_subject_id_missing", "Parkinsons subject number 不能为空。"
        )
    digest = hashlib.sha256(f"{dataset_id}:{subject_value}".encode("utf-8")).hexdigest()
    return "SUBJ-" + digest[:16].upper()


def _safe_header(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    if not normalized:
        raise EvalV2ContractError(
            "eval_v2_preparation_schema_mismatch", "准备后出现空列名。"
        )
    return normalized


def _privacy_class(dataset_id: str) -> str:
    return {
        "palmer_penguins_v0_1_0": "public_animal_observation",
        "uci_parkinsons_telemonitoring_189": "public_health_pseudonymized",
        "uci_heart_disease_cleveland_45": "public_health_deidentified",
    }[dataset_id]


def _validate_output_directory(project_root: Path, output_directory: Path) -> Path:
    artifacts_root = (project_root / "artifacts").resolve()
    resolved = output_directory.resolve()
    if resolved == artifacts_root or not resolved.is_relative_to(artifacts_root):
        raise EvalV2ContractError(
            "eval_v2_output_path_not_allowed",
            "Eval v2 准备产物必须位于项目 artifacts 的独立子目录。",
        )
    return resolved


def _validate_registry_entry(entry: Mapping[str, Any], dataset_id: str) -> None:
    relative = PurePosixPath(str(entry["relative_path"]))
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != f"{dataset_id}.csv":
        raise EvalV2ContractError(
            "eval_v2_registry_path_invalid", f"dataset {dataset_id} registry 路径无效。"
        )
    _sha256_value(entry["prepared_sha256"], "entry.prepared_sha256")
    _sha256_value(entry["source_asset_sha256"], "entry.source_asset_sha256")
    for name in ("prepared_bytes", "row_count", "column_count"):
        if isinstance(entry[name], bool) or not isinstance(entry[name], int) or entry[name] < 1:
            raise EvalV2ContractError(
                "eval_v2_registry_invalid", f"entry.{name} 必须是正整数。"
            )
    if entry["preparation_version"] != PREPARATION_VERSION:
        raise EvalV2ContractError(
            "eval_v2_registry_version_invalid", "entry preparation_version 无效。"
        )
    if entry["model_access"] != "aggregate_tools_only":
        raise EvalV2ContractError(
            "eval_v2_registry_model_access_invalid", "模型不得直接访问准备后的行级数据。"
        )
    for name in ("privacy_class", "domain"):
        if not isinstance(entry[name], str) or not entry[name].strip():
            raise EvalV2ContractError(
                "eval_v2_registry_invalid", f"entry.{name} 必须是非空字符串。"
            )
    if not isinstance(entry["repeated_subjects"], bool):
        raise EvalV2ContractError(
            "eval_v2_registry_invalid", "entry.repeated_subjects 必须是布尔值。"
        )
    for name in ("analysis_boundaries", "transformations"):
        values = entry[name]
        if not isinstance(values, list) or not values or not all(
            isinstance(value, str) and value.strip() for value in values
        ):
            raise EvalV2ContractError(
                "eval_v2_registry_invalid", f"entry.{name} 必须是非空字符串数组。"
            )


def _load_strict_json(path: Path) -> Mapping[str, Any]:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except EvalV2ContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvalV2ContractError(
            "eval_v2_registry_unreadable", "无法读取 logical dataset registry。"
        ) from exc


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvalV2ContractError(
                "eval_v2_duplicate_json_key", f"JSON 对象包含重复键 {key!r}。"
            )
        result[key] = value
    return result


def _require_exact_fields(
    value: Mapping[str, Any], fields: frozenset[str], label: str
) -> None:
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields)
    if missing or unknown:
        raise EvalV2ContractError(
            "eval_v2_registry_fields_invalid",
            f"{label} 字段不匹配；missing={missing}, unknown={unknown}。",
        )


def _logical_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _LOGICAL_ID_PATTERN.fullmatch(value) is None:
        raise EvalV2ContractError(
            "eval_v2_invalid_logical_id", f"{label} 不是安全逻辑 ID。"
        )
    return value


def _sha256_value(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise EvalV2ContractError(
            "eval_v2_invalid_sha256", f"{label} 必须是小写 SHA-256。"
        )
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
