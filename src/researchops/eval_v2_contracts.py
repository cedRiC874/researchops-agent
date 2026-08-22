from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence


EVAL_V2_SCHEMA_VERSION = "2.0"
EVAL_V2_SPLITS = ("development", "public_regression", "private_holdout")
EVAL_V2_TARGET_MINIMUMS = MappingProxyType(
    {"development": 80, "public_regression": 40, "private_holdout": 50}
)
EVAL_V2_CORE_SCENARIOS = frozenset(
    {
        "standard_analysis",
        "clarification_required",
        "safe_refusal",
        "approval_pause",
        "unauthorized_resource",
        "prompt_injection",
        "provider_timeout",
        "output_truncation",
        "duplicate_tool_call",
        "side_effect_outcome_unknown",
    }
)
EVAL_V2_FREEZE_HASHES = frozenset(
    {
        "source_sha256",
        "prompt_sha256",
        "tool_schema_sha256",
        "scorer_sha256",
        "public_corpus_sha256",
        "split_manifest_sha256",
        "dataset_manifest_sha256",
        "dependency_lock_sha256",
    }
)
EVAL_V2_REQUIRED_METRICS = frozenset(
    {
        "task_success_rate",
        "tool_selection_accuracy",
        "tool_argument_accuracy",
        "evidence_citation_accuracy",
        "numeric_claim_accuracy",
        "clarification_refusal_accuracy",
        "approval_bypass_rate",
        "unexpected_tool_error_rate",
        "completion_integrity_rate",
        "latency_p50_p95",
        "token_usage",
        "cost_coverage",
        "inter_run_stability",
        "per_dataset_success_rate",
        "per_provider_success_rate",
    }
)

_CAMPAIGN_FIELDS = frozenset(
    {
        "schema_version",
        "campaign_id",
        "status",
        "purpose",
        "splits",
        "dataset_policy",
        "run_policy",
        "required_scenarios",
        "freeze_policy",
        "private_holdout_policy",
        "external_review_policy",
        "reporting_policy",
        "notes",
    }
)
_SPLIT_FIELDS = frozenset(
    {
        "target_task_count",
        "registered_task_count",
        "visibility",
        "storage",
        "prompt_tuning_allowed",
    }
)
_DATASET_POLICY_FIELDS = frozenset(
    {
        "minimum_distinct_datasets",
        "target_distinct_datasets",
        "minimum_non_synthetic_datasets",
        "datasets",
    }
)
_DATASET_FIELDS = frozenset(
    {
        "dataset_id",
        "status",
        "source_class",
        "domain",
        "license_status",
        "external_review_status",
        "allowed_splits",
    }
)
_RUN_POLICY_FIELDS = frozenset(
    {
        "minimum_provider_count",
        "repetitions_per_provider",
        "private_campaigns_per_freeze",
        "randomized_case_order",
        "precommitted_repetition_seeds",
        "providers",
    }
)
_PROVIDER_FIELDS = frozenset(
    {"provider_id", "status", "model_id", "transport_id"}
)
_FREEZE_POLICY_FIELDS = frozenset(
    {"freeze_before_private_access", "changes_invalidate_results", "hashes"}
)
_PRIVATE_POLICY_FIELDS = frozenset(
    {
        "content_in_repository",
        "goldens_in_repository",
        "locator_in_repository",
        "external_custodian_required",
        "single_campaign_per_freeze",
        "commitment_sha256",
        "result_release_policy",
    }
)
_EXTERNAL_REVIEW_FIELDS = frozenset(
    {
        "holdout_custodian_required",
        "minimum_domain_expert_reviewers",
        "independent_statistical_implementation_required",
        "accepted_reference_implementations",
        "reviewer_conflict_declaration_required",
        "golden_review_status",
        "statistical_crosscheck_status",
    }
)
_REPORTING_FIELDS = frozenset(
    {
        "required_metrics",
        "report_by_split",
        "report_by_dataset",
        "report_by_provider",
        "report_each_repetition",
        "aggregate_repetitions",
        "confidence_intervals_required",
        "unknown_cost_is_null",
        "latency_is_not_sla",
    }
)
_LOGICAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_MAX_CAMPAIGN_BYTES = 256 * 1024


class EvalV2ContractError(ValueError):
    """Stable error for an invalid Eval v2 campaign contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict[str, str]:
        return {"status": "error", "error_code": self.code, "message": str(self)}


@dataclass(frozen=True)
class EvalV2SplitPlan:
    name: str
    target_task_count: int
    registered_task_count: int
    visibility: str
    storage: str
    prompt_tuning_allowed: bool

    @classmethod
    def from_dict(
        cls, name: str, payload: Mapping[str, Any]
    ) -> "EvalV2SplitPlan":
        value = _strict_object(payload, f"splits.{name}", _SPLIT_FIELDS)
        _require_all_fields(value, _SPLIT_FIELDS, f"splits.{name}")
        target = _positive_int(value["target_task_count"], f"splits.{name}.target_task_count")
        registered = _nonnegative_int(
            value["registered_task_count"],
            f"splits.{name}.registered_task_count",
        )
        minimum = EVAL_V2_TARGET_MINIMUMS[name]
        if target < minimum:
            raise EvalV2ContractError(
                "eval_v2_target_too_small",
                f"{name} 至少需要规划 {minimum} 题，当前为 {target}。",
            )

        expected_visibility = {
            "development": "repo_local_development",
            "public_regression": "repo_local_public_regression",
            "private_holdout": "external_private",
        }[name]
        expected_storage = {
            "development": "repository",
            "public_regression": "repository",
            "private_holdout": "external_custodian",
        }[name]
        visibility = _nonempty_string(value["visibility"], f"splits.{name}.visibility")
        storage = _nonempty_string(value["storage"], f"splits.{name}.storage")
        if visibility != expected_visibility or storage != expected_storage:
            raise EvalV2ContractError(
                "eval_v2_invalid_split_boundary",
                f"{name} 的 visibility/storage 不符合 Eval v2 隔离边界。",
            )

        tuning = _boolean(
            value["prompt_tuning_allowed"],
            f"splits.{name}.prompt_tuning_allowed",
        )
        if tuning is not (name == "development"):
            raise EvalV2ContractError(
                "eval_v2_invalid_tuning_policy",
                "只有 development 允许 prompt 调优；public/private 均必须冻结。",
            )
        return cls(name, target, registered, visibility, storage, tuning)


@dataclass(frozen=True)
class EvalV2Dataset:
    dataset_id: str
    status: str
    source_class: str
    domain: str
    license_status: str
    external_review_status: str
    allowed_splits: tuple[str, ...]

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvalV2Dataset":
        value = _strict_object(payload, "dataset", _DATASET_FIELDS)
        _require_all_fields(value, _DATASET_FIELDS, "dataset")
        dataset_id = _logical_id(value["dataset_id"], "dataset.dataset_id")
        status = _choice(value["status"], "dataset.status", {"planned", "registered"})
        source_class = _choice(
            value["source_class"],
            "dataset.source_class",
            {"synthetic", "public", "deidentified"},
        )
        domain = _nonempty_string(value["domain"], "dataset.domain")
        license_status = _choice(
            value["license_status"],
            "dataset.license_status",
            {"not_reviewed", "approved", "not_required"},
        )
        review_status = _choice(
            value["external_review_status"],
            "dataset.external_review_status",
            {"planned", "completed", "not_required"},
        )
        allowed_splits = tuple(
            _unique_strings(value["allowed_splits"], "dataset.allowed_splits")
        )
        if not allowed_splits or not set(allowed_splits).issubset(EVAL_V2_SPLITS):
            raise EvalV2ContractError(
                "eval_v2_invalid_dataset_split",
                f"dataset {dataset_id} 的 allowed_splits 无效。",
            )
        if source_class == "synthetic" and "private_holdout" in allowed_splits:
            raise EvalV2ContractError(
                "eval_v2_synthetic_private_holdout",
                "private holdout 不得只靠仓库内 synthetic dataset 支撑。",
            )
        if status == "registered" and source_class != "synthetic":
            if license_status != "approved" or review_status != "completed":
                raise EvalV2ContractError(
                    "eval_v2_dataset_not_reviewed",
                    f"注册外部数据集 {dataset_id} 前必须完成许可与外部复核。",
                )
        return cls(
            dataset_id,
            status,
            source_class,
            domain,
            license_status,
            review_status,
            allowed_splits,
        )


@dataclass(frozen=True)
class EvalV2Provider:
    provider_id: str
    status: str
    model_id: str | None
    transport_id: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvalV2Provider":
        value = _strict_object(payload, "provider", _PROVIDER_FIELDS)
        _require_all_fields(value, _PROVIDER_FIELDS, "provider")
        provider_id = _logical_id(value["provider_id"], "provider.provider_id")
        status = _choice(value["status"], "provider.status", {"planned", "registered"})
        model_id = _optional_nonempty_string(value["model_id"], "provider.model_id")
        transport_id = _optional_nonempty_string(
            value["transport_id"], "provider.transport_id"
        )
        if status == "registered" and (model_id is None or transport_id is None):
            raise EvalV2ContractError(
                "eval_v2_provider_incomplete",
                f"注册 provider {provider_id} 必须固定 model_id 与 transport_id。",
            )
        if status == "planned" and (model_id is not None or transport_id is not None):
            raise EvalV2ContractError(
                "eval_v2_provider_not_frozen",
                f"planned provider {provider_id} 不得伪装成已冻结模型配置。",
            )
        return cls(provider_id, status, model_id, transport_id)


@dataclass(frozen=True)
class EvalV2Campaign:
    campaign_id: str
    status: str
    purpose: str
    splits: Mapping[str, EvalV2SplitPlan]
    datasets: tuple[EvalV2Dataset, ...]
    minimum_distinct_datasets: int
    target_distinct_datasets: int
    minimum_non_synthetic_datasets: int
    providers: tuple[EvalV2Provider, ...]
    minimum_provider_count: int
    repetitions_per_provider: int
    private_campaigns_per_freeze: int
    required_scenarios: tuple[str, ...]
    freeze_hashes: Mapping[str, str | None]
    private_commitment_sha256: str | None
    golden_review_status: str
    statistical_crosscheck_status: str
    required_metrics: tuple[str, ...]
    notes: tuple[str, ...]

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvalV2Campaign":
        value = _strict_object(payload, "campaign", _CAMPAIGN_FIELDS)
        _require_all_fields(value, _CAMPAIGN_FIELDS, "campaign")
        if value["schema_version"] != EVAL_V2_SCHEMA_VERSION:
            raise EvalV2ContractError(
                "eval_v2_unknown_schema",
                f"不支持 schema_version={value['schema_version']!r}。",
            )

        campaign_id = _logical_id(value["campaign_id"], "campaign_id")
        status = _choice(value["status"], "status", {"design_only", "frozen"})
        purpose = _nonempty_string(value["purpose"], "purpose")

        split_payload = _strict_object(
            value["splits"], "splits", frozenset(EVAL_V2_SPLITS)
        )
        _require_all_fields(split_payload, frozenset(EVAL_V2_SPLITS), "splits")
        splits = {
            name: EvalV2SplitPlan.from_dict(name, split_payload[name])
            for name in EVAL_V2_SPLITS
        }

        dataset_policy = _strict_object(
            value["dataset_policy"], "dataset_policy", _DATASET_POLICY_FIELDS
        )
        _require_all_fields(dataset_policy, _DATASET_POLICY_FIELDS, "dataset_policy")
        minimum_datasets = _positive_int(
            dataset_policy["minimum_distinct_datasets"],
            "dataset_policy.minimum_distinct_datasets",
        )
        target_datasets = _positive_int(
            dataset_policy["target_distinct_datasets"],
            "dataset_policy.target_distinct_datasets",
        )
        minimum_non_synthetic = _positive_int(
            dataset_policy["minimum_non_synthetic_datasets"],
            "dataset_policy.minimum_non_synthetic_datasets",
        )
        if minimum_datasets < 3 or target_datasets < minimum_datasets or target_datasets > 5:
            raise EvalV2ContractError(
                "eval_v2_invalid_dataset_target",
                "Eval v2 必须规划 3–5 个数据集，target 不得低于 minimum。",
            )
        if minimum_non_synthetic < 3 or minimum_non_synthetic > target_datasets:
            raise EvalV2ContractError(
                "eval_v2_invalid_dataset_target",
                "至少 3 个数据集必须来自 public/deidentified 来源。",
            )
        datasets = tuple(
            EvalV2Dataset.from_dict(item)
            for item in _sequence(dataset_policy["datasets"], "dataset_policy.datasets")
        )
        _require_unique_ids(
            [item.dataset_id for item in datasets], "dataset_id"
        )
        if len(datasets) != target_datasets:
            raise EvalV2ContractError(
                "eval_v2_dataset_slot_mismatch",
                "datasets 必须显式列出 target_distinct_datasets 个逻辑槽位。",
            )

        run_policy = _strict_object(
            value["run_policy"], "run_policy", _RUN_POLICY_FIELDS
        )
        _require_all_fields(run_policy, _RUN_POLICY_FIELDS, "run_policy")
        minimum_providers = _positive_int(
            run_policy["minimum_provider_count"],
            "run_policy.minimum_provider_count",
        )
        repetitions = _positive_int(
            run_policy["repetitions_per_provider"],
            "run_policy.repetitions_per_provider",
        )
        private_campaigns = _positive_int(
            run_policy["private_campaigns_per_freeze"],
            "run_policy.private_campaigns_per_freeze",
        )
        if minimum_providers < 2 or repetitions < 3 or private_campaigns != 1:
            raise EvalV2ContractError(
                "eval_v2_invalid_run_policy",
                "至少需要 2 个 Provider、每个重复 3 次，且每次 freeze 只允许一次 private campaign。",
            )
        if not _boolean(
            run_policy["randomized_case_order"], "run_policy.randomized_case_order"
        ) or not _boolean(
            run_policy["precommitted_repetition_seeds"],
            "run_policy.precommitted_repetition_seeds",
        ):
            raise EvalV2ContractError(
                "eval_v2_invalid_run_policy",
                "重复运行必须预承诺 seed，并随机化题目顺序。",
            )
        providers = tuple(
            EvalV2Provider.from_dict(item)
            for item in _sequence(run_policy["providers"], "run_policy.providers")
        )
        _require_unique_ids([item.provider_id for item in providers], "provider_id")
        if len(providers) < minimum_providers:
            raise EvalV2ContractError(
                "eval_v2_provider_slot_mismatch",
                "providers 数量低于 minimum_provider_count。",
            )

        scenarios = tuple(
            _unique_strings(value["required_scenarios"], "required_scenarios")
        )
        missing_scenarios = sorted(EVAL_V2_CORE_SCENARIOS - set(scenarios))
        if missing_scenarios:
            raise EvalV2ContractError(
                "eval_v2_missing_scenario",
                f"缺少核心场景：{', '.join(missing_scenarios)}。",
            )

        freeze_policy = _strict_object(
            value["freeze_policy"], "freeze_policy", _FREEZE_POLICY_FIELDS
        )
        _require_all_fields(freeze_policy, _FREEZE_POLICY_FIELDS, "freeze_policy")
        if not _boolean(
            freeze_policy["freeze_before_private_access"],
            "freeze_policy.freeze_before_private_access",
        ) or not _boolean(
            freeze_policy["changes_invalidate_results"],
            "freeze_policy.changes_invalidate_results",
        ):
            raise EvalV2ContractError(
                "eval_v2_invalid_freeze_policy",
                "private access 前必须冻结；冻结后变更必须使结果失效。",
            )
        hashes = _strict_object(
            freeze_policy["hashes"], "freeze_policy.hashes", EVAL_V2_FREEZE_HASHES
        )
        _require_all_fields(hashes, EVAL_V2_FREEZE_HASHES, "freeze_policy.hashes")
        normalized_hashes = {
            name: _optional_sha256(hashes[name], f"freeze_policy.hashes.{name}")
            for name in sorted(EVAL_V2_FREEZE_HASHES)
        }

        private_policy = _parse_private_policy(value["private_holdout_policy"])
        review = _parse_external_review_policy(value["external_review_policy"])
        reporting = _parse_reporting_policy(value["reporting_policy"])
        notes = tuple(_unique_strings(value["notes"], "notes"))

        campaign = cls(
            campaign_id=campaign_id,
            status=status,
            purpose=purpose,
            splits=MappingProxyType(splits),
            datasets=datasets,
            minimum_distinct_datasets=minimum_datasets,
            target_distinct_datasets=target_datasets,
            minimum_non_synthetic_datasets=minimum_non_synthetic,
            providers=providers,
            minimum_provider_count=minimum_providers,
            repetitions_per_provider=repetitions,
            private_campaigns_per_freeze=private_campaigns,
            required_scenarios=scenarios,
            freeze_hashes=MappingProxyType(normalized_hashes),
            private_commitment_sha256=private_policy,
            golden_review_status=review[0],
            statistical_crosscheck_status=review[1],
            required_metrics=reporting,
            notes=notes,
        )
        if status == "frozen" and campaign.readiness_gaps():
            raise EvalV2ContractError(
                "eval_v2_not_ready_to_freeze",
                "campaign 标为 frozen，但仍有未完成的 readiness gate。",
            )
        return campaign

    def readiness_gaps(self) -> tuple[str, ...]:
        gaps: list[str] = []
        for name in EVAL_V2_SPLITS:
            split = self.splits[name]
            if split.registered_task_count < split.target_task_count:
                gaps.append(
                    f"{name}: registered {split.registered_task_count}/{split.target_task_count} tasks"
                )

        registered = [item for item in self.datasets if item.status == "registered"]
        registered_external = [
            item for item in registered if item.source_class != "synthetic"
        ]
        if len(registered) < self.minimum_distinct_datasets:
            gaps.append(
                f"datasets: registered {len(registered)}/{self.minimum_distinct_datasets} minimum"
            )
        if len(registered_external) < self.minimum_non_synthetic_datasets:
            gaps.append(
                "non_synthetic_datasets: registered "
                f"{len(registered_external)}/{self.minimum_non_synthetic_datasets} minimum"
            )

        registered_providers = [
            item for item in self.providers if item.status == "registered"
        ]
        if len(registered_providers) < self.minimum_provider_count:
            gaps.append(
                f"providers: registered {len(registered_providers)}/{self.minimum_provider_count} minimum"
            )

        missing_hashes = [
            name for name, digest in self.freeze_hashes.items() if digest is None
        ]
        if missing_hashes:
            gaps.append(f"freeze_hashes: missing {', '.join(missing_hashes)}")
        if self.private_commitment_sha256 is None:
            gaps.append("private_holdout: external commitment missing")
        if self.golden_review_status != "completed":
            gaps.append("external_review: golden review incomplete")
        if self.statistical_crosscheck_status != "completed":
            gaps.append("external_review: statistical cross-check incomplete")
        return tuple(gaps)

    def public_summary(self) -> dict[str, Any]:
        registered_datasets = sum(
            item.status == "registered" for item in self.datasets
        )
        registered_providers = sum(
            item.status == "registered" for item in self.providers
        )
        gaps = self.readiness_gaps()
        return {
            "schema_version": EVAL_V2_SCHEMA_VERSION,
            "campaign_id": self.campaign_id,
            "campaign_status": self.status,
            "evidence_status": "not_run",
            "ready_for_freeze": not gaps,
            "target_task_count": sum(
                item.target_task_count for item in self.splits.values()
            ),
            "registered_task_count": sum(
                item.registered_task_count for item in self.splits.values()
            ),
            "split_target_counts": {
                name: self.splits[name].target_task_count for name in EVAL_V2_SPLITS
            },
            "split_registered_counts": {
                name: self.splits[name].registered_task_count
                for name in EVAL_V2_SPLITS
            },
            "dataset_slots": len(self.datasets),
            "registered_datasets": registered_datasets,
            "provider_slots": len(self.providers),
            "registered_providers": registered_providers,
            "repetitions_per_provider": self.repetitions_per_provider,
            "private_holdout_content_in_repository": False,
            "private_holdout_commitment_present": (
                self.private_commitment_sha256 is not None
            ),
            "readiness_gaps": list(gaps),
        }


def load_eval_v2_campaign(path: str | Path) -> EvalV2Campaign:
    source = Path(path)
    try:
        size = source.stat().st_size
        if size > _MAX_CAMPAIGN_BYTES:
            raise EvalV2ContractError(
                "eval_v2_file_too_large",
                f"campaign manifest 超过 {_MAX_CAMPAIGN_BYTES} bytes。",
            )
        text = source.read_text(encoding="utf-8")
    except EvalV2ContractError:
        raise
    except (OSError, UnicodeError) as exc:
        raise EvalV2ContractError(
            "eval_v2_file_unreadable", "无法读取 Eval v2 campaign manifest。"
        ) from exc

    try:
        payload = json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except EvalV2ContractError:
        raise
    except json.JSONDecodeError as exc:
        raise EvalV2ContractError(
            "eval_v2_invalid_json", f"campaign manifest JSON 无效：{exc.msg}。"
        ) from exc
    return EvalV2Campaign.from_dict(payload)


def validate_eval_v2_campaign(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    campaign = load_eval_v2_campaign(source)
    summary = campaign.public_summary()
    summary.update(
        {
            "status": "valid",
            "campaign_sha256": _sha256_file(source),
            "golden_isolation": (
                "private task content, goldens, identifiers, and locator are not stored in the repository"
            ),
            "network_calls": 0,
        }
    )
    return summary


def _parse_private_policy(payload: Mapping[str, Any]) -> str | None:
    value = _strict_object(
        payload, "private_holdout_policy", _PRIVATE_POLICY_FIELDS
    )
    _require_all_fields(value, _PRIVATE_POLICY_FIELDS, "private_holdout_policy")
    forbidden_true = (
        "content_in_repository",
        "goldens_in_repository",
        "locator_in_repository",
    )
    if any(_boolean(value[name], f"private_holdout_policy.{name}") for name in forbidden_true):
        raise EvalV2ContractError(
            "eval_v2_private_content_in_repo",
            "private holdout 的题面、golden、ID 与 locator 均不得进入仓库。",
        )
    if not _boolean(
        value["external_custodian_required"],
        "private_holdout_policy.external_custodian_required",
    ) or not _boolean(
        value["single_campaign_per_freeze"],
        "private_holdout_policy.single_campaign_per_freeze",
    ):
        raise EvalV2ContractError(
            "eval_v2_invalid_private_policy",
            "private holdout 必须由外部 custodian 保管，且每次 freeze 只运行一个 campaign。",
        )
    if value["result_release_policy"] != "sanitized_aggregate_only":
        raise EvalV2ContractError(
            "eval_v2_invalid_private_policy",
            "private 结果只能发布脱敏聚合证据。",
        )
    return _optional_sha256(
        value["commitment_sha256"], "private_holdout_policy.commitment_sha256"
    )


def _parse_external_review_policy(payload: Mapping[str, Any]) -> tuple[str, str]:
    value = _strict_object(
        payload, "external_review_policy", _EXTERNAL_REVIEW_FIELDS
    )
    _require_all_fields(value, _EXTERNAL_REVIEW_FIELDS, "external_review_policy")
    if not _boolean(
        value["holdout_custodian_required"],
        "external_review_policy.holdout_custodian_required",
    ) or not _boolean(
        value["independent_statistical_implementation_required"],
        "external_review_policy.independent_statistical_implementation_required",
    ) or not _boolean(
        value["reviewer_conflict_declaration_required"],
        "external_review_policy.reviewer_conflict_declaration_required",
    ):
        raise EvalV2ContractError(
            "eval_v2_invalid_review_policy",
            "必须要求外部 custodian、独立统计实现和 reviewer conflict declaration。",
        )
    reviewers = _positive_int(
        value["minimum_domain_expert_reviewers"],
        "external_review_policy.minimum_domain_expert_reviewers",
    )
    if reviewers < 2:
        raise EvalV2ContractError(
            "eval_v2_invalid_review_policy", "至少需要 2 位领域专家复核。"
        )
    implementations = set(
        _unique_strings(
            value["accepted_reference_implementations"],
            "external_review_policy.accepted_reference_implementations",
        )
    )
    if not implementations.intersection({"R", "SAS"}):
        raise EvalV2ContractError(
            "eval_v2_invalid_review_policy",
            "独立统计交叉检查至少接受 R 或 SAS 之一。",
        )
    golden_status = _choice(
        value["golden_review_status"],
        "external_review_policy.golden_review_status",
        {"planned", "completed"},
    )
    crosscheck_status = _choice(
        value["statistical_crosscheck_status"],
        "external_review_policy.statistical_crosscheck_status",
        {"planned", "completed"},
    )
    return golden_status, crosscheck_status


def _parse_reporting_policy(payload: Mapping[str, Any]) -> tuple[str, ...]:
    value = _strict_object(payload, "reporting_policy", _REPORTING_FIELDS)
    _require_all_fields(value, _REPORTING_FIELDS, "reporting_policy")
    metrics = tuple(
        _unique_strings(value["required_metrics"], "reporting_policy.required_metrics")
    )
    missing = sorted(EVAL_V2_REQUIRED_METRICS - set(metrics))
    if missing:
        raise EvalV2ContractError(
            "eval_v2_missing_metric", f"缺少必报指标：{', '.join(missing)}。"
        )
    boolean_fields = _REPORTING_FIELDS - {"required_metrics"}
    for name in boolean_fields:
        if not _boolean(value[name], f"reporting_policy.{name}"):
            raise EvalV2ContractError(
                "eval_v2_invalid_reporting_policy",
                f"reporting_policy.{name} 必须为 true。",
            )
    return metrics


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
    result = [_nonempty_string(item, label) for item in _sequence(value, label)]
    if len(result) != len(set(result)):
        raise EvalV2ContractError(
            "eval_v2_duplicate_value", f"{label} 不允许重复值。"
        )
    return result


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
            "eval_v2_invalid_logical_id",
            f"{label} 必须是最长 64 字符的安全逻辑 ID。",
        )
    return normalized


def _choice(value: Any, label: str, choices: set[str]) -> str:
    normalized = _nonempty_string(value, label)
    if normalized not in choices:
        raise EvalV2ContractError(
            "eval_v2_invalid_choice",
            f"{label} 必须是：{', '.join(sorted(choices))}。",
        )
    return normalized


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EvalV2ContractError(
            "eval_v2_invalid_number", f"{label} 必须是正整数。"
        )
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvalV2ContractError(
            "eval_v2_invalid_number", f"{label} 必须是非负整数。"
        )
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise EvalV2ContractError(
            "eval_v2_invalid_type", f"{label} 必须是布尔值。"
        )
    return value


def _optional_sha256(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise EvalV2ContractError(
            "eval_v2_invalid_sha256", f"{label} 必须是小写 SHA-256 或 null。"
        )
    return value


def _require_unique_ids(values: Sequence[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise EvalV2ContractError(
            "eval_v2_duplicate_id", f"{label} 不允许重复。"
        )


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


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    """Reserved for future result manifests; rejects non-JSON and non-finite values."""

    if depth > 32:
        raise EvalV2ContractError("eval_v2_max_depth", "JSON 嵌套超过 32 层。")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EvalV2ContractError(
                "eval_v2_non_finite_number", "JSON 不允许 NaN 或 Infinity。"
            )
        return
    if isinstance(value, Mapping):
        for item in value.values():
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    raise EvalV2ContractError(
        "eval_v2_invalid_type", f"包含非 JSON 类型 {type(value).__name__}。"
    )
