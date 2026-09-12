from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .eval_v2_contracts import EvalV2ContractError
from .eval_v2_runner import (
    COMPLETION_FAILURE_SOURCES,
    COMPLETION_TELEMETRY_CONTRACT_VERSION,
)


ARTIFACT_SCHEMA_VERSION = "2.1"
_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "credentials",
        "direct_identifiers",
        "final_output",
        "incomplete_details",
        "provider_output_body",
        "provider_response_body",
        "provider_status_raw_value",
        "raw_data",
        "raw_rows",
        "records",
        "sample_values",
        "traceback",
    }
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"[A-Za-z]:[\\/]")
_SECRET_PREFIX = re.compile(r"\b(?:sk-|Bearer\s+)[A-Za-z0-9._-]+", re.IGNORECASE)


def aggregate_eval_v2_repetitions(
    reports: Sequence[Mapping[str, Any]],
    *,
    minimum_provider_count: int = 1,
) -> dict[str, Any]:
    if isinstance(minimum_provider_count, bool) or minimum_provider_count < 1:
        raise EvalV2ContractError(
            "eval_v2_provider_count_invalid", "minimum_provider_count 必须是正整数。"
        )
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    common_task_ids: frozenset[str] | None = None
    for report in reports:
        _validate_report_for_aggregation(report)
        provider = report["provider"]
        identity = (
            provider["provider_id"],
            provider["model_id"],
            provider["transport_id"],
        )
        groups.setdefault(identity, []).append(report)
        ordered_task_ids = tuple(score["task_id"] for score in report["task_scores"])
        if len(ordered_task_ids) != len(set(ordered_task_ids)):
            raise EvalV2ContractError(
                "eval_v2_repetition_scope_mismatch",
                "Repetition task scope 包含重复 task_id。",
            )
        task_ids = frozenset(ordered_task_ids)
        if common_task_ids is None:
            common_task_ids = task_ids
        elif common_task_ids != task_ids:
            raise EvalV2ContractError(
                "eval_v2_repetition_scope_mismatch",
                "所有 Provider/repetition 必须使用相同 task-ID 集合；顺序可以预承诺随机化。",
            )
    if len(groups) < minimum_provider_count:
        raise EvalV2ContractError(
            "eval_v2_provider_count_mismatch",
            "聚合报告中的 Provider 数低于要求。",
        )

    provider_results: list[dict[str, Any]] = []
    for identity, provider_reports in sorted(groups.items()):
        ordered = sorted(provider_reports, key=lambda item: item["repetition_index"])
        indices = [item["repetition_index"] for item in ordered]
        if indices != [1, 2, 3]:
            raise EvalV2ContractError(
                "eval_v2_repetition_count_mismatch",
                "每个 Provider 必须且只能包含 repetition 1、2、3。",
            )
        score_maps = [
            {score["task_id"]: score for score in report["task_scores"]}
            for report in ordered
        ]
        stable_count = 0
        all_pass_count = 0
        for task_id in sorted(common_task_ids or ()):
            task_triplet = [score_map[task_id] for score_map in score_maps]
            statuses = [bool(item["passed"]) for item in task_triplet]
            stable_count += len(set(statuses)) == 1
            all_pass_count += all(statuses)
        success_rates = [float(item["success_rate"]) for item in ordered]
        task_count = int(ordered[0]["task_count"])
        provider_results.append(
            {
                "provider_id": identity[0],
                "model_id": identity[1],
                "transport_id": identity[2],
                "repetition_count": 3,
                "success_rates": success_rates,
                "mean_success_rate": sum(success_rates) / 3,
                "min_success_rate": min(success_rates),
                "max_success_rate": max(success_rates),
                "task_stability_rate": stable_count / task_count,
                "all_repetitions_pass_rate": all_pass_count / task_count,
                "total_model_calls": sum(
                    int(item["model_call_count"]) for item in ordered
                ),
                "usage_complete_all_repetitions": all(
                    bool(item["usage_complete"]) for item in ordered
                ),
                "task_order_sha256_by_repetition": [
                    item.get("task_order_sha256") for item in ordered
                ],
                "completion_telemetry": _aggregate_completion_telemetry_reports(
                    ordered
                ),
            }
        )
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "status": "aggregated",
        "provider_count": len(provider_results),
        "repetitions_per_provider": 3,
        "task_count_per_repetition": len(common_task_ids or ()),
        "task_alignment": "by_task_id",
        "model_quality_claim_allowed": False,
        "completion_telemetry": _aggregate_completion_telemetry_reports(reports),
        "providers": provider_results,
    }


def write_eval_v2_artifacts(
    *,
    project_root: str | Path,
    output_directory: str | Path,
    report: Mapping[str, Any],
    repetition_aggregation: Mapping[str, Any] | None = None,
    run_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    output = _validate_output_directory(root, Path(output_directory))
    if output.exists():
        raise EvalV2ContractError(
            "eval_v2_artifact_output_exists", "Eval v2 artifact 目录已存在；不会覆盖。"
        )
    _assert_sanitized(report)
    if report.get("model_quality_claim_allowed") is not False:
        raise EvalV2ContractError(
            "eval_v2_artifact_claim_invalid",
            "未冻结 Eval v2 report 必须禁止模型质量声明。",
        )
    if repetition_aggregation is not None:
        _assert_sanitized(repetition_aggregation)
    metadata = dict(run_metadata or {})
    _assert_sanitized(metadata)

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".eval-v2-artifacts-", dir=output.parent)
    ).resolve()
    try:
        report_path = staging / "eval_v2_report.json"
        summary_path = staging / "eval_v2_summary.md"
        _write_json(report_path, report)
        summary_path.write_text(_render_summary(report), encoding="utf-8")
        files = [report_path, summary_path]
        if repetition_aggregation is not None:
            aggregation_path = staging / "eval_v2_repetitions.json"
            _write_json(aggregation_path, repetition_aggregation)
            files.append(aggregation_path)
        manifest = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "status": "published_atomic_local",
            "evidence_status": report.get("evidence_status"),
            "model_quality_claim_allowed": False,
            "source_tree_sha256": _source_tree_sha256(root / "src" / "researchops"),
            "run_metadata": metadata,
            "artifacts": {
                path.name: {
                    "sha256": _sha256_file(path),
                    "byte_size": path.stat().st_size,
                }
                for path in files
            },
        }
        manifest_path = staging / "eval_v2_manifest.json"
        _write_json(manifest_path, manifest)
        _verify_staged_artifacts(staging, manifest)
        staging.replace(output)
        return {
            "status": "published_atomic_local",
            "output_directory": output.relative_to(root).as_posix(),
            "artifact_count": len(files) + 1,
            "model_quality_claim_allowed": False,
        }
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _validate_report_for_aggregation(report: Mapping[str, Any]) -> None:
    required = {
        "provider",
        "repetition_index",
        "task_count",
        "success_rate",
        "model_call_count",
        "usage_complete",
        "task_scores",
        "model_quality_claim_allowed",
    }
    if not isinstance(report, Mapping) or not required.issubset(report):
        raise EvalV2ContractError(
            "eval_v2_repetition_report_invalid", "repetition report 字段不完整。"
        )
    provider = report["provider"]
    if not isinstance(provider, Mapping) or not all(
        isinstance(provider.get(name), str) and provider.get(name)
        for name in ("provider_id", "model_id", "transport_id")
    ):
        raise EvalV2ContractError(
            "eval_v2_repetition_report_invalid", "repetition provider 身份无效。"
        )
    if report["repetition_index"] not in {1, 2, 3}:
        raise EvalV2ContractError(
            "eval_v2_repetition_report_invalid", "repetition_index 无效。"
        )
    scores = report["task_scores"]
    if not isinstance(scores, list) or len(scores) != report["task_count"]:
        raise EvalV2ContractError(
            "eval_v2_repetition_report_invalid", "task_scores 与 task_count 不一致。"
        )
    if report["model_quality_claim_allowed"] is not False:
        raise EvalV2ContractError(
            "eval_v2_repetition_report_invalid", "未冻结 repetition 不得允许质量声明。"
        )
    telemetry = report.get("completion_telemetry")
    if telemetry is not None:
        _parse_completion_telemetry(telemetry)


def _aggregate_completion_telemetry_reports(
    reports: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    source_counts = {source: 0 for source in COMPLETION_FAILURE_SOURCES}
    eligible_failure_count = 0
    classified_failure_count = 0
    legacy_unknown_count = 0
    structured_report_count = 0
    for report in reports:
        telemetry = report.get("completion_telemetry")
        if telemetry is None:
            continue
        parsed = _parse_completion_telemetry(telemetry)
        structured_report_count += 1
        eligible_failure_count += parsed["eligible_failure_count"]
        classified_failure_count += parsed["classified_failure_count"]
        legacy_unknown_count += parsed["legacy_unknown_count"]
        for source, count in parsed["source_counts"].items():
            source_counts[source] += count

    report_count = len(reports)
    structured_report_coverage = (
        structured_report_count / report_count if report_count else 1.0
    )
    coverage_complete = (
        structured_report_count == report_count and legacy_unknown_count == 0
    )
    coverage_status = (
        "partial"
        if structured_report_count != report_count
        else (
            "no_applicable_failures"
            if eligible_failure_count == 0
            else ("complete" if coverage_complete else "partial")
        )
    )
    aggregated = {
        "contract_version": COMPLETION_TELEMETRY_CONTRACT_VERSION,
        "diagnostic_only": True,
        "counts_scope": "structured_reports_only",
        "report_count": report_count,
        "structured_report_count": structured_report_count,
        "structured_report_coverage": structured_report_coverage,
        "eligible_failure_count": eligible_failure_count,
        "classified_failure_count": classified_failure_count,
        "legacy_unknown_count": legacy_unknown_count,
        "classified_failure_coverage": (
            classified_failure_count / eligible_failure_count
            if eligible_failure_count
            else None
        ),
        "coverage_status": coverage_status,
        "coverage_complete": coverage_complete,
        "failure_source_counts": [
            {"source": source, "case_count": source_counts[source]}
            for source in COMPLETION_FAILURE_SOURCES
            if source_counts[source]
        ],
    }
    _parse_completion_telemetry(aggregated)
    return aggregated


def _parse_completion_telemetry(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or value.get("contract_version")
        != COMPLETION_TELEMETRY_CONTRACT_VERSION
        or value.get("diagnostic_only") is not True
    ):
        raise EvalV2ContractError(
            "eval_v2_repetition_report_invalid",
            "completion telemetry contract version 无效。",
        )
    count_names = (
        "eligible_failure_count",
        "classified_failure_count",
        "legacy_unknown_count",
    )
    counts: dict[str, int] = {}
    for name in count_names:
        candidate = value.get(name)
        if (
            isinstance(candidate, bool)
            or not isinstance(candidate, int)
            or candidate < 0
        ):
            raise EvalV2ContractError(
                "eval_v2_repetition_report_invalid",
                "completion telemetry counts 无效。",
            )
        counts[name] = candidate
    if (
        counts["classified_failure_count"] + counts["legacy_unknown_count"]
        != counts["eligible_failure_count"]
    ):
        raise EvalV2ContractError(
            "eval_v2_repetition_report_invalid",
            "completion telemetry coverage counts 不一致。",
        )

    aggregate_report_count: int | None = None
    aggregate_structured_count: int | None = None
    if "report_count" in value or "structured_report_count" in value:
        if value.get("counts_scope") != "structured_reports_only":
            raise EvalV2ContractError(
                "eval_v2_repetition_report_invalid",
                "aggregated completion telemetry scope 无效。",
            )
        aggregate_report_count = value.get("report_count")
        aggregate_structured_count = value.get("structured_report_count")
        if (
            isinstance(aggregate_report_count, bool)
            or not isinstance(aggregate_report_count, int)
            or aggregate_report_count < 0
            or isinstance(aggregate_structured_count, bool)
            or not isinstance(aggregate_structured_count, int)
            or aggregate_structured_count < 0
            or aggregate_structured_count > aggregate_report_count
        ):
            raise EvalV2ContractError(
                "eval_v2_repetition_report_invalid",
                "aggregated completion telemetry report counts 无效。",
            )
        expected_report_coverage = (
            aggregate_structured_count / aggregate_report_count
            if aggregate_report_count
            else 1.0
        )
        observed_report_coverage = value.get("structured_report_coverage")
        if (
            isinstance(observed_report_coverage, bool)
            or not isinstance(observed_report_coverage, (int, float))
            or not math.isclose(
                float(observed_report_coverage), expected_report_coverage
            )
        ):
            raise EvalV2ContractError(
                "eval_v2_repetition_report_invalid",
                "aggregated completion telemetry report coverage 无效。",
            )

    raw_source_counts = value.get("failure_source_counts")
    if not isinstance(raw_source_counts, list):
        raise EvalV2ContractError(
            "eval_v2_repetition_report_invalid",
            "completion telemetry source counts 无效。",
        )
    source_counts = {source: 0 for source in COMPLETION_FAILURE_SOURCES}
    seen: set[str] = set()
    last_source_index = -1
    for item in raw_source_counts:
        if not isinstance(item, Mapping):
            raise EvalV2ContractError(
                "eval_v2_repetition_report_invalid",
                "completion telemetry source count entry 无效。",
            )
        source = item.get("source")
        count = item.get("case_count")
        if (
            source not in source_counts
            or source in seen
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
        ):
            raise EvalV2ContractError(
                "eval_v2_repetition_report_invalid",
                "completion telemetry source allowlist/count 无效。",
            )
        source_index = COMPLETION_FAILURE_SOURCES.index(source)
        if source_index <= last_source_index:
            raise EvalV2ContractError(
                "eval_v2_repetition_report_invalid",
                "completion telemetry source counts 顺序无效。",
            )
        last_source_index = source_index
        seen.add(source)
        source_counts[source] = count
    if sum(source_counts.values()) != counts["classified_failure_count"]:
        raise EvalV2ContractError(
            "eval_v2_repetition_report_invalid",
            "completion telemetry classified/source counts 不一致。",
        )

    expected_failure_coverage = (
        counts["classified_failure_count"] / counts["eligible_failure_count"]
        if counts["eligible_failure_count"]
        else None
    )
    observed_failure_coverage = value.get("classified_failure_coverage")
    if expected_failure_coverage is None:
        failure_coverage_valid = observed_failure_coverage is None
    else:
        failure_coverage_valid = (
            not isinstance(observed_failure_coverage, bool)
            and isinstance(observed_failure_coverage, (int, float))
            and math.isclose(
                float(observed_failure_coverage), expected_failure_coverage
            )
        )
    if not failure_coverage_valid:
        raise EvalV2ContractError(
            "eval_v2_repetition_report_invalid",
            "completion telemetry classified failure coverage 无效。",
        )

    expected_complete = counts["legacy_unknown_count"] == 0
    if aggregate_report_count is not None and aggregate_structured_count is not None:
        expected_complete = (
            expected_complete
            and aggregate_structured_count == aggregate_report_count
        )
    expected_status = (
        "partial"
        if aggregate_report_count is not None
        and aggregate_structured_count != aggregate_report_count
        else (
            "no_applicable_failures"
            if counts["eligible_failure_count"] == 0
            else ("complete" if expected_complete else "partial")
        )
    )
    if (
        value.get("coverage_complete") is not expected_complete
        or value.get("coverage_status") != expected_status
    ):
        raise EvalV2ContractError(
            "eval_v2_repetition_report_invalid",
            "completion telemetry coverage status 无效。",
        )
    return {
        **counts,
        "source_counts": source_counts,
    }


def _validate_output_directory(project_root: Path, output_directory: Path) -> Path:
    artifacts_root = (project_root / "artifacts").resolve()
    resolved = output_directory.resolve()
    if resolved == artifacts_root or not resolved.is_relative_to(artifacts_root):
        raise EvalV2ContractError(
            "eval_v2_artifact_path_not_allowed",
            "Eval v2 artifacts 必须位于项目 artifacts 的独立子目录。",
        )
    return resolved


def _assert_sanitized(value: Any) -> None:
    if isinstance(value, Mapping):
        forbidden = set(value).intersection(_FORBIDDEN_KEYS)
        if forbidden:
            raise EvalV2ContractError(
                "eval_v2_artifact_sensitive_field",
                "Eval v2 artifact 包含禁止字段。",
            )
        for item in value.values():
            _assert_sanitized(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _assert_sanitized(item)
    elif isinstance(value, str):
        if _WINDOWS_ABSOLUTE_PATH.search(value) or _SECRET_PREFIX.search(value):
            raise EvalV2ContractError(
                "eval_v2_artifact_sensitive_value",
                "Eval v2 artifact 包含路径或凭据形态。",
            )


def _render_summary(report: Mapping[str, Any]) -> str:
    provider = report.get("provider", {})
    completion = report.get("completion_telemetry")
    completion_line = (
        "- Completion telemetry: `legacy/unavailable`"
        if not isinstance(completion, Mapping)
        else (
            "- Completion telemetry: "
            f"{completion.get('classified_failure_count')}/"
            f"{completion.get('eligible_failure_count')} classified; "
            f"coverage complete=`{str(bool(completion.get('coverage_complete'))).lower()}`"
        )
    )
    return "\n".join(
        [
            "# Eval v2 run summary",
            "",
            f"- Evidence status: `{report.get('evidence_status')}`",
            f"- Evaluation mode: `{report.get('evaluation_mode')}`",
            f"- Provider/model: `{provider.get('provider_id')}` / `{provider.get('model_id')}`",
            f"- Repetition: {report.get('repetition_index')}/3",
            f"- Tasks: {report.get('passed')}/{report.get('task_count')} passed",
            f"- Safety violations: {report.get('safety_violation_count')}",
            completion_line,
            "- Model quality claim allowed: `false`",
            "",
            "This is an unfrozen local harness artifact, not a published model-quality result.",
            "",
        ]
    )


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _verify_staged_artifacts(staging: Path, manifest: Mapping[str, Any]) -> None:
    for name, expected in manifest["artifacts"].items():
        path = staging / name
        if (
            not path.is_file()
            or path.stat().st_size != expected["byte_size"]
            or _sha256_file(path) != expected["sha256"]
        ):
            raise EvalV2ContractError(
                "eval_v2_artifact_verification_failed",
                "staging artifact hash/size 不匹配。",
            )


def _source_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.py"), key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
