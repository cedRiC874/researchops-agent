from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .eval_v2_contracts import EvalV2ContractError


ARTIFACT_SCHEMA_VERSION = "2.0"
_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "final_output",
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
