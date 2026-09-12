from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .eval_v2_contracts import (
    EVAL_V2_CORE_SCENARIOS,
    EVAL_V2_SCHEMA_VERSION,
    EVAL_V2_SPLITS,
    EvalV2ContractError,
    load_eval_v2_campaign,
    validate_eval_v2_campaign,
)


PUBLIC_TASK_SPLITS = frozenset({"development", "public_regression"})
TASK_OUTCOMES = frozenset(
    {
        "completed",
        "clarification_required",
        "refused",
        "waiting_approval",
        "controlled_failure",
    }
)
APPROVAL_STATES = frozenset({"not_required", "awaiting_approval"})
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_EVIDENCE_ID_PATTERN = re.compile(r"^E-[A-F0-9]{12}$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOGICAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TASK_ID_PATTERN = re.compile(r"^V2-(DEV|PUB)-\d{3}$")

_DATASET_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "manifest_id",
        "status",
        "verified_at",
        "verification_method",
        "datasets",
        "notes",
    }
)
_DATASET_FIELDS = frozenset(
    {
        "dataset_id",
        "display_name",
        "source_class",
        "domain",
        "selection_status",
        "external_review_status",
        "license",
        "source",
        "structure",
        "analysis_boundaries",
        "allowed_splits",
        "citation",
    }
)
_LICENSE_FIELDS = frozenset(
    {
        "identifier",
        "status",
        "official_url",
        "redistribution_allowed",
        "attribution_required",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "landing_page_url",
        "download_url",
        "doi",
        "version",
        "archive_sha256",
        "archive_bytes",
        "selected_asset",
        "selected_asset_sha256",
        "selected_asset_bytes",
    }
)
_STRUCTURE_FIELDS = frozenset(
    {
        "format",
        "has_header",
        "row_count",
        "column_count",
        "missing_cell_count",
        "missing_tokens",
        "repeated_subjects",
        "subject_count",
        "row_unit",
        "id_columns",
    }
)
_CITATION_FIELDS = frozenset({"text", "url"})
_INTERNAL_REVIEW_FIELDS = frozenset(
    {
        "schema_version",
        "review_id",
        "review_type",
        "reviewed_at",
        "public_tasks_sha256",
        "reviewed_task_ids",
        "decisions",
        "checklist",
        "limitations",
    }
)
_REVIEW_DECISION_FIELDS = frozenset({"task_id", "decision"})
_REVIEW_CHECKLIST_FIELDS = frozenset(
    {
        "dataset_authorization_checked",
        "tool_sequence_checked",
        "outcome_marker_checked",
        "safety_boundary_checked",
        "golden_isolation_checked",
        "domain_limitations_checked",
    }
)

_TASK_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "split",
        "lifecycle_status",
        "review_status",
        "dataset_id",
        "scenario",
        "title",
        "prompt",
        "context",
        "expected",
        "tags",
    }
)
_CONTEXT_FIELDS = frozenset(
    {"dataset_id", "design_id", "bundle_id", "release_name"}
)
_EXPECTED_FIELDS = frozenset(
    {
        "outcome",
        "tool_sequence",
        "tool_arguments",
        "required_evidence_ids",
        "required_phrases",
        "forbidden_phrases",
        "forbidden_assertions",
        "numeric_claims",
        "allowed_numeric_claims",
        "approval_state",
        "safety_violation",
    }
)
_TOOL_ARGUMENT_FIELDS = frozenset(
    {"call_index", "tool_name", "arguments"}
)
_NUMERIC_CLAIM_FIELDS = frozenset(
    {"metric_name", "evidence_id", "value", "atol", "rtol"}
)


@dataclass(frozen=True)
class VerifiedDataset:
    dataset_id: str
    display_name: str
    source_class: str
    domain: str
    selection_status: str
    external_review_status: str
    license_identifier: str
    landing_page_url: str
    download_url: str
    doi: str | None
    version: str
    archive_sha256: str | None
    archive_bytes: int | None
    selected_asset: str
    selected_asset_sha256: str
    selected_asset_bytes: int
    has_header: bool
    row_count: int
    column_count: int
    missing_cell_count: int
    missing_tokens: tuple[str, ...]
    repeated_subjects: bool
    subject_count: int | None
    row_unit: str
    id_columns: tuple[str, ...]
    analysis_boundaries: tuple[str, ...]
    allowed_splits: tuple[str, ...]
    citation_text: str
    citation_url: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VerifiedDataset":
        value = _strict_object(payload, "dataset", _DATASET_FIELDS)
        _require_all_fields(value, _DATASET_FIELDS, "dataset")
        dataset_id = _logical_id(value["dataset_id"], "dataset.dataset_id")
        display_name = _nonempty_string(value["display_name"], "dataset.display_name")
        source_class = _choice(
            value["source_class"],
            "dataset.source_class",
            {"public", "deidentified"},
        )
        domain = _nonempty_string(value["domain"], "dataset.domain")
        selection_status = _choice(
            value["selection_status"],
            "dataset.selection_status",
            {"source_verified"},
        )
        external_review_status = _choice(
            value["external_review_status"],
            "dataset.external_review_status",
            {"planned", "completed"},
        )

        license_value = _strict_object(value["license"], "dataset.license", _LICENSE_FIELDS)
        _require_all_fields(license_value, _LICENSE_FIELDS, "dataset.license")
        license_identifier = _choice(
            license_value["identifier"],
            "dataset.license.identifier",
            {"CC0-1.0", "CC-BY-4.0"},
        )
        if license_value["status"] != "verified":
            raise EvalV2ContractError(
                "eval_v2_license_unverified", f"dataset {dataset_id} 的许可尚未核验。"
            )
        _https_url(license_value["official_url"], "dataset.license.official_url")
        if not _boolean(
            license_value["redistribution_allowed"],
            "dataset.license.redistribution_allowed",
        ):
            raise EvalV2ContractError(
                "eval_v2_license_restricts_use",
                f"dataset {dataset_id} 不满足公开评测所需的再分发许可。",
            )
        attribution_required = _boolean(
            license_value["attribution_required"],
            "dataset.license.attribution_required",
        )
        if attribution_required is not (license_identifier == "CC-BY-4.0"):
            raise EvalV2ContractError(
                "eval_v2_license_metadata_mismatch",
                f"dataset {dataset_id} 的 attribution 标记与许可不一致。",
            )

        source = _strict_object(value["source"], "dataset.source", _SOURCE_FIELDS)
        _require_all_fields(source, _SOURCE_FIELDS, "dataset.source")
        landing_page_url = _https_url(
            source["landing_page_url"], "dataset.source.landing_page_url"
        )
        download_url = _https_url(
            source["download_url"], "dataset.source.download_url"
        )
        doi = _optional_nonempty_string(source["doi"], "dataset.source.doi")
        version = _nonempty_string(source["version"], "dataset.source.version")
        archive_sha256 = _optional_sha256(
            source["archive_sha256"], "dataset.source.archive_sha256"
        )
        archive_bytes = _optional_positive_int(
            source["archive_bytes"], "dataset.source.archive_bytes"
        )
        if (archive_sha256 is None) is not (archive_bytes is None):
            raise EvalV2ContractError(
                "eval_v2_archive_metadata_mismatch",
                f"dataset {dataset_id} 的 archive hash/bytes 必须同时提供或同时为 null。",
            )
        selected_asset = _nonempty_string(
            source["selected_asset"], "dataset.source.selected_asset"
        )
        selected_asset_sha256 = _sha256(
            source["selected_asset_sha256"],
            "dataset.source.selected_asset_sha256",
        )
        selected_asset_bytes = _positive_int(
            source["selected_asset_bytes"], "dataset.source.selected_asset_bytes"
        )

        structure = _strict_object(
            value["structure"], "dataset.structure", _STRUCTURE_FIELDS
        )
        _require_all_fields(structure, _STRUCTURE_FIELDS, "dataset.structure")
        if structure["format"] != "csv":
            raise EvalV2ContractError(
                "eval_v2_unsupported_dataset_format",
                f"dataset {dataset_id} 当前必须选择 CSV/CSV-compatible asset。",
            )
        has_header = _boolean(
            structure["has_header"], "dataset.structure.has_header"
        )
        row_count = _positive_int(structure["row_count"], "dataset.structure.row_count")
        column_count = _positive_int(
            structure["column_count"], "dataset.structure.column_count"
        )
        missing_cell_count = _nonnegative_int(
            structure["missing_cell_count"], "dataset.structure.missing_cell_count"
        )
        missing_token_values = _sequence(
            structure["missing_tokens"], "dataset.structure.missing_tokens"
        )
        if not all(isinstance(item, str) for item in missing_token_values):
            raise EvalV2ContractError(
                "eval_v2_invalid_type", "dataset.structure.missing_tokens 必须是字符串数组。"
            )
        missing_tokens = tuple(missing_token_values)
        if len(missing_tokens) != len(set(missing_tokens)):
            raise EvalV2ContractError(
                "eval_v2_duplicate_value", "dataset.structure.missing_tokens 不允许重复。"
            )
        repeated_subjects = _boolean(
            structure["repeated_subjects"], "dataset.structure.repeated_subjects"
        )
        subject_count = _optional_positive_int(
            structure["subject_count"], "dataset.structure.subject_count"
        )
        row_unit = _nonempty_string(structure["row_unit"], "dataset.structure.row_unit")
        id_columns = tuple(
            _unique_strings(structure["id_columns"], "dataset.structure.id_columns")
        )
        if repeated_subjects and (subject_count is None or not id_columns):
            raise EvalV2ContractError(
                "eval_v2_repeated_measure_metadata_missing",
                f"dataset {dataset_id} 的重复测量必须提供 subject_count 与 ID 列。",
            )
        if not repeated_subjects and subject_count is not None:
            raise EvalV2ContractError(
                "eval_v2_subject_metadata_mismatch",
                f"dataset {dataset_id} 非重复测量时 subject_count 必须为 null。",
            )

        boundaries = tuple(
            _unique_strings(value["analysis_boundaries"], "dataset.analysis_boundaries")
        )
        if len(boundaries) < 2:
            raise EvalV2ContractError(
                "eval_v2_missing_analysis_boundary",
                f"dataset {dataset_id} 至少需要两个分析边界。",
            )
        allowed_splits = tuple(
            _unique_strings(value["allowed_splits"], "dataset.allowed_splits")
        )
        if not allowed_splits or not set(allowed_splits).issubset(EVAL_V2_SPLITS):
            raise EvalV2ContractError(
                "eval_v2_invalid_dataset_split",
                f"dataset {dataset_id} 的 allowed_splits 无效。",
            )

        citation = _strict_object(value["citation"], "dataset.citation", _CITATION_FIELDS)
        _require_all_fields(citation, _CITATION_FIELDS, "dataset.citation")
        citation_text = _nonempty_string(citation["text"], "dataset.citation.text")
        citation_url = _https_url(citation["url"], "dataset.citation.url")

        return cls(
            dataset_id=dataset_id,
            display_name=display_name,
            source_class=source_class,
            domain=domain,
            selection_status=selection_status,
            external_review_status=external_review_status,
            license_identifier=license_identifier,
            landing_page_url=landing_page_url,
            download_url=download_url,
            doi=doi,
            version=version,
            archive_sha256=archive_sha256,
            archive_bytes=archive_bytes,
            selected_asset=selected_asset,
            selected_asset_sha256=selected_asset_sha256,
            selected_asset_bytes=selected_asset_bytes,
            has_header=has_header,
            row_count=row_count,
            column_count=column_count,
            missing_cell_count=missing_cell_count,
            missing_tokens=missing_tokens,
            repeated_subjects=repeated_subjects,
            subject_count=subject_count,
            row_unit=row_unit,
            id_columns=id_columns,
            analysis_boundaries=boundaries,
            allowed_splits=allowed_splits,
            citation_text=citation_text,
            citation_url=citation_url,
        )


@dataclass(frozen=True)
class EvalV2DatasetManifest:
    manifest_id: str
    status: str
    verified_at: str
    verification_method: str
    datasets: tuple[VerifiedDataset, ...]
    notes: tuple[str, ...]

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvalV2DatasetManifest":
        value = _strict_object(payload, "dataset manifest", _DATASET_MANIFEST_FIELDS)
        _require_all_fields(value, _DATASET_MANIFEST_FIELDS, "dataset manifest")
        if value["schema_version"] != EVAL_V2_SCHEMA_VERSION:
            raise EvalV2ContractError(
                "eval_v2_unknown_schema", "dataset manifest schema_version 必须为 2.0。"
            )
        manifest_id = _logical_id(value["manifest_id"], "manifest_id")
        status = _choice(value["status"], "status", {"source_verified"})
        verified_at = _nonempty_string(value["verified_at"], "verified_at")
        if _DATE_PATTERN.fullmatch(verified_at) is None:
            raise EvalV2ContractError(
                "eval_v2_invalid_date", "verified_at 必须是 YYYY-MM-DD。"
            )
        method = _choice(
            value["verification_method"],
            "verification_method",
            {"official_metadata_plus_in_memory_hash"},
        )
        datasets = tuple(
            VerifiedDataset.from_dict(item)
            for item in _sequence(value["datasets"], "datasets")
        )
        if len(datasets) < 3:
            raise EvalV2ContractError(
                "eval_v2_insufficient_external_datasets",
                "dataset manifest 至少需要 3 个已核验外部数据集。",
            )
        _require_unique([item.dataset_id for item in datasets], "dataset_id")
        notes = tuple(_unique_strings(value["notes"], "notes"))
        return cls(manifest_id, status, verified_at, method, datasets, notes)

    def by_id(self) -> Mapping[str, VerifiedDataset]:
        return MappingProxyType({item.dataset_id: item for item in self.datasets})


@dataclass(frozen=True)
class EvalV2ExpectedToolCall:
    call_index: int
    tool_name: str
    arguments: Mapping[str, str]


@dataclass(frozen=True)
class EvalV2NumericClaim:
    metric_name: str
    evidence_id: str
    value: float
    atol: float
    rtol: float


@dataclass(frozen=True)
class EvalV2Expected:
    outcome: str
    tool_sequence: tuple[str, ...]
    tool_arguments: tuple[EvalV2ExpectedToolCall, ...]
    required_evidence_ids: tuple[str, ...]
    required_phrases: tuple[str, ...]
    forbidden_phrases: tuple[str, ...]
    forbidden_assertions: tuple[str, ...]
    numeric_claims: tuple[EvalV2NumericClaim, ...]
    allowed_numeric_claims: tuple[EvalV2NumericClaim, ...]
    approval_state: str
    safety_violation: bool

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvalV2Expected":
        value = _strict_object(payload, "task.expected", _EXPECTED_FIELDS)
        _require_all_fields(value, _EXPECTED_FIELDS, "task.expected")
        outcome = _choice(value["outcome"], "task.expected.outcome", set(TASK_OUTCOMES))
        tool_sequence = tuple(
            _strings(value["tool_sequence"], "task.expected.tool_sequence")
        )
        tool_arguments = tuple(
            _parse_tool_call(item)
            for item in _sequence(value["tool_arguments"], "task.expected.tool_arguments")
        )
        if [item.call_index for item in tool_arguments] != list(range(len(tool_arguments))):
            raise EvalV2ContractError(
                "eval_v2_invalid_call_index",
                "tool_arguments.call_index 必须从 0 连续递增。",
            )
        if tuple(item.tool_name for item in tool_arguments) != tool_sequence:
            raise EvalV2ContractError(
                "eval_v2_tool_contract_mismatch",
                "tool_sequence 必须与 tool_arguments 的完整顺序一致。",
            )
        evidence_ids = tuple(
            _unique_strings(
                value["required_evidence_ids"],
                "task.expected.required_evidence_ids",
            )
        )
        for evidence_id in evidence_ids:
            if _EVIDENCE_ID_PATTERN.fullmatch(evidence_id) is None:
                raise EvalV2ContractError(
                    "eval_v2_invalid_evidence_id", f"无效 evidence ID：{evidence_id}。"
                )
        required_phrases = tuple(
            _unique_strings(value["required_phrases"], "task.expected.required_phrases")
        )
        forbidden_phrases = tuple(
            _unique_strings(value["forbidden_phrases"], "task.expected.forbidden_phrases")
        )
        forbidden_assertions = tuple(
            _unique_strings(
                value["forbidden_assertions"], "task.expected.forbidden_assertions"
            )
        )
        numeric_claims = tuple(
            _parse_numeric_claim(item)
            for item in _sequence(value["numeric_claims"], "task.expected.numeric_claims")
        )
        allowed_numeric_claims = tuple(
            _parse_numeric_claim(item)
            for item in _sequence(
                value["allowed_numeric_claims"],
                "task.expected.allowed_numeric_claims",
            )
        )
        required_pairs = {(item.metric_name, item.evidence_id) for item in numeric_claims}
        allowed_pairs = {
            (item.metric_name, item.evidence_id) for item in allowed_numeric_claims
        }
        if len(required_pairs) != len(numeric_claims) or len(allowed_pairs) != len(allowed_numeric_claims):
            raise EvalV2ContractError(
                "eval_v2_duplicate_numeric_claim", "numeric claim pair 不允许重复。"
            )
        if required_pairs.intersection(allowed_pairs):
            raise EvalV2ContractError(
                "eval_v2_numeric_catalog_overlap",
                "required 与 allowed numeric claim 目录不得重叠。",
            )
        approval_state = _choice(
            value["approval_state"],
            "task.expected.approval_state",
            set(APPROVAL_STATES),
        )
        safety_violation = _boolean(
            value["safety_violation"], "task.expected.safety_violation"
        )
        if safety_violation:
            raise EvalV2ContractError(
                "eval_v2_invalid_safety_golden", "安全 golden 必须为 false。"
            )
        if outcome in {"clarification_required", "refused"} and tool_sequence:
            raise EvalV2ContractError(
                "eval_v2_invalid_no_tool_outcome",
                f"outcome={outcome} 时不得调用工具。",
            )
        if outcome == "clarification_required" and "[CLARIFICATION_REQUIRED]" not in required_phrases:
            raise EvalV2ContractError(
                "eval_v2_missing_outcome_marker", "澄清任务必须要求 [CLARIFICATION_REQUIRED]。"
            )
        if outcome == "refused" and "[REFUSED]" not in required_phrases:
            raise EvalV2ContractError(
                "eval_v2_missing_outcome_marker", "拒绝任务必须要求 [REFUSED]。"
            )
        if (outcome == "waiting_approval") is not (approval_state == "awaiting_approval"):
            raise EvalV2ContractError(
                "eval_v2_approval_outcome_mismatch",
                "waiting_approval 与 awaiting_approval 必须同时出现。",
            )
        return cls(
            outcome,
            tool_sequence,
            tool_arguments,
            evidence_ids,
            required_phrases,
            forbidden_phrases,
            forbidden_assertions,
            numeric_claims,
            allowed_numeric_claims,
            approval_state,
            safety_violation,
        )


@dataclass(frozen=True)
class EvalV2PublicTask:
    task_id: str
    split: str
    lifecycle_status: str
    review_status: str
    dataset_id: str
    scenario: str
    title: str
    prompt: str
    context: Mapping[str, str]
    expected: EvalV2Expected
    tags: tuple[str, ...]

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvalV2PublicTask":
        value = _strict_object(payload, "task", _TASK_FIELDS)
        _require_all_fields(value, _TASK_FIELDS, "task")
        if value["schema_version"] != EVAL_V2_SCHEMA_VERSION:
            raise EvalV2ContractError(
                "eval_v2_unknown_schema", "public task schema_version 必须为 2.0。"
            )
        task_id = _nonempty_string(value["task_id"], "task.task_id")
        match = _TASK_ID_PATTERN.fullmatch(task_id)
        if match is None:
            raise EvalV2ContractError(
                "eval_v2_invalid_task_id", f"无效 task_id：{task_id}。"
            )
        split = _choice(value["split"], "task.split", set(PUBLIC_TASK_SPLITS))
        expected_prefix = "DEV" if split == "development" else "PUB"
        if match.group(1) != expected_prefix:
            raise EvalV2ContractError(
                "eval_v2_task_split_mismatch", f"task {task_id} 的 ID 与 split 不一致。"
            )
        lifecycle = _choice(
            value["lifecycle_status"],
            "task.lifecycle_status",
            {"draft", "ready"},
        )
        review = _choice(
            value["review_status"],
            "task.review_status",
            {"unreviewed", "internal_reviewed", "external_reviewed"},
        )
        if lifecycle == "ready" and review == "unreviewed":
            raise EvalV2ContractError(
                "eval_v2_unreviewed_ready_task",
                f"task {task_id} 未经复核，不能标为 ready。",
            )
        dataset_id = _logical_id(value["dataset_id"], "task.dataset_id")
        scenario = _choice(
            value["scenario"], "task.scenario", set(EVAL_V2_CORE_SCENARIOS)
        )
        title = _nonempty_string(value["title"], "task.title")
        prompt = _nonempty_string(value["prompt"], "task.prompt")
        if len(prompt) > 4000:
            raise EvalV2ContractError(
                "eval_v2_prompt_too_long", f"task {task_id} prompt 超过 4000 字符。"
            )
        context_value = _strict_object(value["context"], "task.context", _CONTEXT_FIELDS)
        context = {
            key: _logical_id(item, f"task.context.{key}")
            for key, item in context_value.items()
        }
        if context.get("dataset_id") != dataset_id:
            raise EvalV2ContractError(
                "eval_v2_dataset_authorization_mismatch",
                f"task {task_id} 的 dataset_id 与授权 context 不一致。",
            )
        expected = EvalV2Expected.from_dict(value["expected"])
        controlled_failure_scenarios = {
            "provider_timeout",
            "output_truncation",
            "side_effect_outcome_unknown",
        }
        if (scenario in controlled_failure_scenarios) is not (
            expected.outcome == "controlled_failure"
        ):
            raise EvalV2ContractError(
                "eval_v2_controlled_failure_mismatch",
                "controlled_failure 只能且必须用于已登记的故障注入场景。",
            )
        tags = tuple(_unique_strings(value["tags"], "task.tags"))
        return cls(
            task_id,
            split,
            lifecycle,
            review,
            dataset_id,
            scenario,
            title,
            prompt,
            MappingProxyType(context),
            expected,
            tags,
        )

    def public_input(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "prompt": self.prompt,
            "context": dict(self.context),
        }


def load_eval_v2_dataset_manifest(path: str | Path) -> EvalV2DatasetManifest:
    payload = _load_json_object(Path(path), "dataset manifest")
    return EvalV2DatasetManifest.from_dict(payload)


def load_eval_v2_public_tasks(
    path: str | Path,
    dataset_manifest: EvalV2DatasetManifest,
) -> tuple[EvalV2PublicTask, ...]:
    source = Path(path)
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise EvalV2ContractError(
            "eval_v2_file_unreadable", "无法读取 Eval v2 public task JSONL。"
        ) from exc
    if not lines:
        raise EvalV2ContractError("eval_v2_empty_corpus", "public task corpus 不能为空。")
    tasks: list[EvalV2PublicTask] = []
    seen: set[str] = set()
    datasets = dataset_manifest.by_id()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise EvalV2ContractError(
                "eval_v2_blank_line", f"public task JSONL 第 {line_number} 行为空。"
            )
        try:
            payload = _strict_json_loads(line)
            task = EvalV2PublicTask.from_dict(payload)
        except EvalV2ContractError as exc:
            raise EvalV2ContractError(
                exc.code, f"public task JSONL 第 {line_number} 行无效：{exc}"
            ) from exc
        if task.task_id in seen:
            raise EvalV2ContractError(
                "eval_v2_duplicate_task_id", f"重复 task_id：{task.task_id}。"
            )
        seen.add(task.task_id)
        dataset = datasets.get(task.dataset_id)
        if dataset is None:
            raise EvalV2ContractError(
                "eval_v2_unknown_dataset",
                f"task {task.task_id} 引用了未登记 dataset {task.dataset_id}。",
            )
        if task.split not in dataset.allowed_splits:
            raise EvalV2ContractError(
                "eval_v2_dataset_split_not_allowed",
                f"task {task.task_id} 的 split 未获 dataset manifest 允许。",
            )
        tasks.append(task)
    return tuple(tasks)


def validate_eval_v2_suite(
    *,
    campaign_path: str | Path,
    dataset_manifest_path: str | Path,
    public_tasks_path: str | Path,
    task_schema_path: str | Path,
    internal_review_path: str | Path,
) -> dict[str, Any]:
    campaign_result = validate_eval_v2_campaign(campaign_path)
    campaign = load_eval_v2_campaign(campaign_path)
    dataset_source = Path(dataset_manifest_path).resolve()
    tasks_source = Path(public_tasks_path).resolve()
    schema_source = Path(task_schema_path).resolve()
    review_source = Path(internal_review_path).resolve()
    manifest = load_eval_v2_dataset_manifest(dataset_source)
    tasks = load_eval_v2_public_tasks(tasks_source, manifest)
    _validate_task_schema_descriptor(schema_source)
    campaign_dataset_ids = {item.dataset_id for item in campaign.datasets}
    missing_from_campaign = sorted(
        {item.dataset_id for item in manifest.datasets} - campaign_dataset_ids
    )
    if missing_from_campaign:
        raise EvalV2ContractError(
            "eval_v2_campaign_dataset_mismatch",
            "dataset manifest 含有 campaign 未预注册的数据集："
            + ", ".join(missing_from_campaign),
        )
    split_counts = {
        split: sum(task.split == split for task in tasks)
        for split in sorted(PUBLIC_TASK_SPLITS)
    }
    ready_counts = {
        split: sum(
            task.split == split and task.lifecycle_status == "ready" for task in tasks
        )
        for split in sorted(PUBLIC_TASK_SPLITS)
    }
    for split, ready_count in ready_counts.items():
        if ready_count != campaign.splits[split].registered_task_count:
            raise EvalV2ContractError(
                "eval_v2_campaign_task_count_mismatch",
                f"campaign 的 {split} registered_task_count 与 ready task 数不一致。",
            )
    dataset_manifest_sha256 = _sha256_file(dataset_source)
    frozen_dataset_hash = campaign.freeze_hashes["dataset_manifest_sha256"]
    if frozen_dataset_hash is not None and frozen_dataset_hash != dataset_manifest_sha256:
        raise EvalV2ContractError(
            "eval_v2_dataset_manifest_hash_mismatch",
            "campaign 中的 dataset_manifest_sha256 与当前文件不一致。",
        )
    public_tasks_sha256 = _sha256_file(tasks_source)
    frozen_public_hash = campaign.freeze_hashes["public_corpus_sha256"]
    if frozen_public_hash is not None and frozen_public_hash != public_tasks_sha256:
        raise EvalV2ContractError(
            "eval_v2_public_corpus_hash_mismatch",
            "campaign 中的 public_corpus_sha256 与当前文件不一致。",
        )
    review_summary = _validate_internal_review(
        review_source, tasks, public_tasks_sha256
    )
    dataset_coverage = {
        item.dataset_id: sum(task.dataset_id == item.dataset_id for task in tasks)
        for item in manifest.datasets
    }
    lifecycle_counts = {
        status: sum(task.lifecycle_status == status for task in tasks)
        for status in ("draft", "ready")
    }
    scenario_counts = {
        scenario: sum(task.scenario == scenario for task in tasks)
        for scenario in sorted({task.scenario for task in tasks})
    }
    return {
        **campaign_result,
        "public_assets": {
            "dataset_manifest_status": manifest.status,
            "verified_external_dataset_count": len(manifest.datasets),
            "externally_reviewed_dataset_count": sum(
                item.external_review_status == "completed" for item in manifest.datasets
            ),
            "public_task_count": len(tasks),
            "ready_public_task_count": sum(
                task.lifecycle_status == "ready" for task in tasks
            ),
            "public_task_split_counts": split_counts,
            "ready_public_task_split_counts": ready_counts,
            "lifecycle_counts": lifecycle_counts,
            "scenario_counts": scenario_counts,
            "dataset_task_coverage": dataset_coverage,
            "private_task_count_in_repository": 0,
            "dataset_manifest_sha256": dataset_manifest_sha256,
            "public_tasks_sha256": public_tasks_sha256,
            "public_task_schema_sha256": _sha256_file(schema_source),
            "internal_review": review_summary,
            "golden_isolation": "EvalV2PublicTask.public_input excludes expected goldens",
            "network_calls": 0,
        },
    }


def _validate_internal_review(
    path: Path,
    tasks: tuple[EvalV2PublicTask, ...],
    public_tasks_sha256: str,
) -> dict[str, Any]:
    value = _load_json_object(path, "internal review manifest")
    _require_all_fields(value, _INTERNAL_REVIEW_FIELDS, "internal review manifest")
    unknown = sorted(set(value) - _INTERNAL_REVIEW_FIELDS)
    if unknown:
        raise EvalV2ContractError(
            "eval_v2_unknown_field",
            f"internal review manifest 包含未知字段：{', '.join(unknown)}。",
        )
    if value["schema_version"] != EVAL_V2_SCHEMA_VERSION:
        raise EvalV2ContractError(
            "eval_v2_unknown_schema", "internal review schema_version 必须为 2.0。"
        )
    review_id = _logical_id(value["review_id"], "internal review.review_id")
    if value["review_type"] != "project_internal_non_external":
        raise EvalV2ContractError(
            "eval_v2_review_type_invalid", "internal review 不得冒充外部专家复核。"
        )
    reviewed_at = _nonempty_string(value["reviewed_at"], "internal review.reviewed_at")
    if _DATE_PATTERN.fullmatch(reviewed_at) is None:
        raise EvalV2ContractError(
            "eval_v2_invalid_date", "internal review.reviewed_at 必须是 YYYY-MM-DD。"
        )
    review_hash = _sha256(
        value["public_tasks_sha256"], "internal review.public_tasks_sha256"
    )
    if review_hash != public_tasks_sha256:
        raise EvalV2ContractError(
            "eval_v2_internal_review_hash_mismatch",
            "internal review 未绑定当前 public task corpus。",
        )
    reviewed_ids = tuple(
        _unique_strings(value["reviewed_task_ids"], "internal review.reviewed_task_ids")
    )
    decisions_value = _sequence(value["decisions"], "internal review.decisions")
    decisions: dict[str, str] = {}
    for item in decisions_value:
        decision = _strict_object(item, "internal review decision", _REVIEW_DECISION_FIELDS)
        _require_all_fields(decision, _REVIEW_DECISION_FIELDS, "internal review decision")
        task_id = _nonempty_string(decision["task_id"], "review decision.task_id")
        if decision["decision"] != "approved" or task_id in decisions:
            raise EvalV2ContractError(
                "eval_v2_internal_review_decision_invalid",
                "internal review decision 必须唯一且为 approved。",
            )
        decisions[task_id] = "approved"
    if set(decisions) != set(reviewed_ids):
        raise EvalV2ContractError(
            "eval_v2_internal_review_decision_invalid",
            "reviewed_task_ids 与 decisions 不一致。",
        )
    tasks_by_id = {task.task_id: task for task in tasks}
    ready_ids = {
        task.task_id
        for task in tasks
        if task.lifecycle_status == "ready"
        and task.review_status in {"internal_reviewed", "external_reviewed"}
    }
    if set(reviewed_ids) != ready_ids or not set(reviewed_ids).issubset(tasks_by_id):
        raise EvalV2ContractError(
            "eval_v2_internal_review_scope_mismatch",
            "internal review scope 必须精确覆盖当前 ready/reviewed tasks。",
        )
    checklist = _strict_object(
        value["checklist"], "internal review.checklist", _REVIEW_CHECKLIST_FIELDS
    )
    _require_all_fields(checklist, _REVIEW_CHECKLIST_FIELDS, "internal review.checklist")
    if not all(_boolean(checklist[name], f"internal review.checklist.{name}") for name in checklist):
        raise EvalV2ContractError(
            "eval_v2_internal_review_incomplete", "internal review checklist 尚未完成。"
        )
    limitations = _unique_strings(value["limitations"], "internal review.limitations")
    if not limitations:
        raise EvalV2ContractError(
            "eval_v2_internal_review_incomplete", "internal review 必须披露限制。"
        )
    return {
        "review_id": review_id,
        "review_type": "project_internal_non_external",
        "reviewed_task_count": len(reviewed_ids),
        "reviewed_task_ids": list(reviewed_ids),
        "public_tasks_sha256": review_hash,
        "external_review": False,
    }


def _parse_tool_call(payload: Mapping[str, Any]) -> EvalV2ExpectedToolCall:
    value = _strict_object(payload, "expected tool call", _TOOL_ARGUMENT_FIELDS)
    _require_all_fields(value, _TOOL_ARGUMENT_FIELDS, "expected tool call")
    call_index = _nonnegative_int(value["call_index"], "tool call.call_index")
    tool_name = _logical_id(value["tool_name"], "tool call.tool_name")
    arguments_value = value["arguments"]
    if not isinstance(arguments_value, Mapping) or not all(
        isinstance(key, str) for key in arguments_value
    ):
        raise EvalV2ContractError(
            "eval_v2_invalid_type", "tool call.arguments 必须是 JSON 对象。"
        )
    arguments = {
        _logical_id(key, "tool argument name"): _logical_id(item, f"tool argument {key}")
        for key, item in arguments_value.items()
    }
    return EvalV2ExpectedToolCall(call_index, tool_name, MappingProxyType(arguments))


def _parse_numeric_claim(payload: Mapping[str, Any]) -> EvalV2NumericClaim:
    value = _strict_object(payload, "numeric claim", _NUMERIC_CLAIM_FIELDS)
    _require_all_fields(value, _NUMERIC_CLAIM_FIELDS, "numeric claim")
    metric_name = _logical_id(value["metric_name"], "numeric claim.metric_name")
    evidence_id = _nonempty_string(value["evidence_id"], "numeric claim.evidence_id")
    if _EVIDENCE_ID_PATTERN.fullmatch(evidence_id) is None:
        raise EvalV2ContractError(
            "eval_v2_invalid_evidence_id", f"无效 evidence ID：{evidence_id}。"
        )
    numeric_value = _finite_number(value["value"], "numeric claim.value")
    atol = _nonnegative_number(value["atol"], "numeric claim.atol")
    rtol = _nonnegative_number(value["rtol"], "numeric claim.rtol")
    return EvalV2NumericClaim(metric_name, evidence_id, numeric_value, atol, rtol)


def _validate_task_schema_descriptor(path: Path) -> None:
    value = _load_json_object(path, "public task JSON schema")
    if value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise EvalV2ContractError(
            "eval_v2_invalid_json_schema", "public task schema 必须使用 JSON Schema 2020-12。"
        )
    if value.get("$id") != "https://researchops.local/schemas/eval-v2-public-task.json":
        raise EvalV2ContractError(
            "eval_v2_invalid_json_schema", "public task schema $id 不符合冻结合同。"
        )
    properties = value.get("properties")
    if not isinstance(properties, Mapping):
        raise EvalV2ContractError(
            "eval_v2_invalid_json_schema", "public task schema 缺少 properties。"
        )
    schema_version = properties.get("schema_version")
    split = properties.get("split")
    if not isinstance(schema_version, Mapping) or schema_version.get("const") != EVAL_V2_SCHEMA_VERSION:
        raise EvalV2ContractError(
            "eval_v2_invalid_json_schema", "public task schema 未冻结 schema_version=2.0。"
        )
    if not isinstance(split, Mapping) or set(split.get("enum", [])) != PUBLIC_TASK_SPLITS:
        raise EvalV2ContractError(
            "eval_v2_private_schema_exposure", "public task schema 只能允许 development/public_regression。"
        )
    if set(value.get("required", [])) != _TASK_FIELDS or value.get("additionalProperties") is not False:
        raise EvalV2ContractError(
            "eval_v2_invalid_json_schema", "public task schema 顶层 required/additionalProperties 不严格。"
        )


def _load_json_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise EvalV2ContractError(
            "eval_v2_file_unreadable", f"无法读取 {label}。"
        ) from exc
    try:
        return _strict_json_loads(text)
    except EvalV2ContractError:
        raise
    except json.JSONDecodeError as exc:
        raise EvalV2ContractError(
            "eval_v2_invalid_json", f"{label} JSON 无效：{exc.msg}。"
        ) from exc


def _strict_json_loads(text: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except EvalV2ContractError:
        raise
    except json.JSONDecodeError as exc:
        raise EvalV2ContractError(
            "eval_v2_invalid_json", f"JSON 无效：{exc.msg}。"
        ) from exc
    if not isinstance(value, Mapping):
        raise EvalV2ContractError(
            "eval_v2_invalid_type", "JSON 顶层必须是对象。"
        )
    return value


def _strict_object(
    value: Any, label: str, allowed_fields: frozenset[str]
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise EvalV2ContractError("eval_v2_invalid_type", f"{label} 必须是 JSON 对象。")
    unknown = sorted(set(value) - allowed_fields)
    if unknown:
        raise EvalV2ContractError(
            "eval_v2_unknown_field", f"{label} 包含未知字段：{', '.join(unknown)}。"
        )
    return value


def _require_all_fields(
    value: Mapping[str, Any], fields: frozenset[str], label: str
) -> None:
    missing = sorted(fields - set(value))
    if missing:
        raise EvalV2ContractError(
            "eval_v2_missing_field", f"{label} 缺少字段：{', '.join(missing)}。"
        )


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise EvalV2ContractError("eval_v2_invalid_type", f"{label} 必须是 JSON 数组。")
    return value


def _unique_strings(value: Any, label: str) -> list[str]:
    result = _strings(value, label)
    _require_unique(result, label)
    return result


def _strings(value: Any, label: str) -> list[str]:
    return [_nonempty_string(item, label) for item in _sequence(value, label)]


def _require_unique(values: Sequence[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise EvalV2ContractError(
            "eval_v2_duplicate_value", f"{label} 不允许重复值。"
        )


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvalV2ContractError(
            "eval_v2_invalid_type", f"{label} 必须是非空字符串。"
        )
    return value


def _optional_nonempty_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, label)


def _logical_id(value: Any, label: str) -> str:
    normalized = _nonempty_string(value, label)
    if _LOGICAL_ID_PATTERN.fullmatch(normalized) is None:
        raise EvalV2ContractError(
            "eval_v2_invalid_logical_id", f"{label} 不是安全逻辑 ID。"
        )
    return normalized


def _choice(value: Any, label: str, choices: set[str]) -> str:
    normalized = _nonempty_string(value, label)
    if normalized not in choices:
        raise EvalV2ContractError(
            "eval_v2_invalid_choice", f"{label} 不在允许集合中。"
        )
    return normalized


def _https_url(value: Any, label: str) -> str:
    normalized = _nonempty_string(value, label)
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise EvalV2ContractError(
            "eval_v2_invalid_url", f"{label} 必须是无凭据 HTTPS URL。"
        )
    return normalized


def _sha256(value: Any, label: str) -> str:
    normalized = _nonempty_string(value, label)
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise EvalV2ContractError(
            "eval_v2_invalid_sha256", f"{label} 必须是小写 SHA-256。"
        )
    return normalized


def _optional_sha256(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, label)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EvalV2ContractError(
            "eval_v2_invalid_number", f"{label} 必须是正整数。"
        )
    return value


def _optional_positive_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, label)


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvalV2ContractError(
            "eval_v2_invalid_number", f"{label} 必须是非负整数。"
        )
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise EvalV2ContractError(
            "eval_v2_invalid_number", f"{label} 必须是有限数值。"
        )
    return float(value)


def _nonnegative_number(value: Any, label: str) -> float:
    result = _finite_number(value, label)
    if result < 0:
        raise EvalV2ContractError(
            "eval_v2_invalid_number", f"{label} 必须是非负数。"
        )
    return result


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise EvalV2ContractError(
            "eval_v2_invalid_type", f"{label} 必须是布尔值。"
        )
    return value


def _reject_json_constant(token: str) -> None:
    raise EvalV2ContractError(
        "eval_v2_non_finite_number", f"JSON 不允许 {token}。"
    )


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvalV2ContractError(
                "eval_v2_duplicate_json_key", f"JSON 对象包含重复键 {key!r}。"
            )
        result[key] = value
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
