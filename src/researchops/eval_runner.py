from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analysis_tools import runtime_versions
from .artifact_security import ArtifactPermissionError, enable_parent_acl_inheritance
from .audit import AuditLedger
from .eval_contracts import EvalTask, TaskResult, load_eval_tasks
from .eval_scenarios import OfflineScenarioExecutor, RUNNER_VERSION
from .scorers import build_eval_report


EVALUATION_MODE = "offline_deterministic"
SUBJECT_UNDER_TEST = "components_and_control_plane"


class EvaluationRunError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict[str, str]:
        return {"status": "error", "error_code": self.code, "message": str(self)}


def validate_eval_suite(tasks_path: str | Path) -> dict[str, Any]:
    source = Path(tasks_path).resolve()
    tasks = load_eval_tasks(source)
    categories = Counter(task.category for task in tasks)
    tags = Counter(tag for task in tasks for tag in task.tags)
    return {
        "status": "valid",
        "schema_version": "1.0",
        "task_count": len(tasks),
        "task_sha256": _sha256_file(source),
        "category_counts": dict(sorted(categories.items())),
        "tag_counts": dict(sorted(tags.items())),
        "golden_isolation": "EvalTask.public_input only",
    }


def run_offline_evaluation(
    *,
    project_root: str | Path,
    tasks_path: str | Path,
    output_directory: str | Path,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    task_source = Path(tasks_path).resolve()
    output_path = Path(output_directory).resolve()
    artifacts_root = (root / "artifacts").resolve()
    if output_path == artifacts_root or not output_path.is_relative_to(artifacts_root):
        raise EvaluationRunError(
            "eval_output_path_not_allowed",
            "评测产物必须写入项目 artifacts 目录下的新子目录。",
        )
    if output_path.exists():
        raise EvaluationRunError(
            "eval_output_directory_exists", "评测输出目录已存在，不会覆盖。"
        )

    tasks = load_eval_tasks(task_source)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".researchops-eval-", dir=output_path.parent)
    ).resolve()
    if not staging.is_relative_to(output_path.parent):
        raise EvaluationRunError("eval_staging_path_invalid", "临时评测目录越界。")

    try:
        audit_path = staging / "eval_audit.sqlite3"
        ledger = AuditLedger(audit_path)
        scenario_workspace = staging / ".scenario-work"
        scenario_workspace.mkdir()
        executor = OfflineScenarioExecutor(
            project_root=root,
            workspace=scenario_workspace,
            ledger=ledger,
        )
        results: list[TaskResult] = []
        audit_index: list[dict[str, Any]] = []
        harness_errors = 0

        for task in tasks:
            public_payload = task.public_input()
            run_id = "RUN-EVAL-" + task.task_id
            ledger.start_run(
                mode="phase5_eval_task",
                run_id=run_id,
                request_summary={
                    "task_id": task.task_id,
                    "category": task.category,
                    "runner": task.runner,
                    "scenario": task.input.get("scenario"),
                },
            )
            started_ns = time.perf_counter_ns()
            try:
                execution = executor.execute(
                    runner=str(public_payload["runner"]),
                    input_payload=dict(public_payload["input"]),
                    task_id=str(public_payload["task_id"]),
                    run_id=run_id,
                )
                latency_ms = max((time.perf_counter_ns() - started_ns) / 1_000_000, 0.0)
                result = execution.to_task_result(task.task_id, latency_ms=latency_ms)
                ledger.set_run_status(run_id, "completed")
            except Exception as exc:
                latency_ms = max((time.perf_counter_ns() - started_ns) / 1_000_000, 0.0)
                error_code = _safe_error_code(exc)
                harness_errors += 1
                result = TaskResult(
                    task_id=task.task_id,
                    status="harness_error",
                    actual={"harness_error": True, "error_code": error_code},
                    error_codes=(error_code,),
                    tool_call_count=0,
                    tool_attempt_count=0,
                    latency_ms=latency_ms,
                    cost_usd=0.0,
                    model_call_count=0,
                    priced_model_call_count=0,
                    safety_violation=False,
                )
                ledger.set_run_status(
                    run_id, "failed", terminal_error_code=error_code
                )
            results.append(result)
            verification = ledger.verify_chain(run_id)
            audit_index.append(
                {
                    "task_id": task.task_id,
                    "run_id": run_id,
                    "chain_verification": verification.to_dict(),
                }
            )

        report = build_eval_report(tasks, results)
        report_payload = report.to_dict()
        task_scores = {score.task_id: score for score in report.task_scores}

        _write_jsonl(
            staging / "task_results.jsonl", [result.to_dict() for result in results]
        )
        _write_jsonl(
            staging / "eval_results.jsonl",
            [
                {
                    "task": {
                        "task_id": task.task_id,
                        "category": task.category,
                        "runner": task.runner,
                        "scenario": task.input.get("scenario"),
                    },
                    "result": result.to_dict(),
                    "score": task_scores[task.task_id].to_dict(),
                }
                for task, result in zip(tasks, results, strict=True)
            ],
        )
        _write_json(staging / "eval_report.json", report_payload)
        _write_json(
            staging / "eval_audit_index.json",
            {"schema_version": "1.0", "runs": audit_index},
        )
        (staging / "eval_summary.md").write_text(
            _summary_markdown(report_payload, harness_errors), encoding="utf-8"
        )

        if scenario_workspace.exists() and scenario_workspace.is_relative_to(staging):
            shutil.rmtree(scenario_workspace)

        artifact_files = [
            staging / "eval_report.json",
            staging / "eval_results.jsonl",
            staging / "task_results.jsonl",
            staging / "eval_audit.sqlite3",
            staging / "eval_audit_index.json",
            staging / "eval_summary.md",
        ]
        manifest = {
            "schema_version": "1.0",
            "evaluation_mode": EVALUATION_MODE,
            "subject_under_test": SUBJECT_UNDER_TEST,
            "llm_planner_evaluated": False,
            "network_calls": 0,
            "simulated_backoff": True,
            "latency_scope": (
                "offline scenario execution including local fixture setup; "
                "excludes scoring and artifact publication"
            ),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "runner_version": RUNNER_VERSION,
            "task_corpus": {
                "file_name": task_source.name,
                "sha256": _sha256_file(task_source),
                "task_count": len(tasks),
                "golden_isolation": "system under test receives EvalTask.public_input only",
            },
            "source_tree_sha256": _source_tree_sha256(root / "src" / "researchops"),
            "dataset_sha256": _sha256_file(root / "data" / "synthetic_trial.csv"),
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                **runtime_versions(),
            },
            "harness_error_count": harness_errors,
            "audit": {
                "database": "eval_audit.sqlite3",
                "run_count": len(audit_index),
                "all_chains_valid": all(
                    item["chain_verification"]["valid"] for item in audit_index
                ),
            },
            "artifacts": {
                path.name: {"sha256": _sha256_file(path), "byte_size": path.stat().st_size}
                for path in artifact_files
            },
        }
        _write_json(staging / "eval_manifest.json", manifest)

        try:
            enable_parent_acl_inheritance(staging)
        except ArtifactPermissionError as exc:
            raise EvaluationRunError(
                "eval_artifact_acl_inheritance_failed",
                "无法让评测产物继承 artifacts 目录权限。",
            ) from exc
        if output_path.exists():
            raise EvaluationRunError(
                "eval_output_directory_exists",
                "发布评测产物前目标目录已出现，已停止以避免覆盖。",
            )
        os.replace(staging, output_path)
        return {
            "status": "completed",
            "evaluation_mode": EVALUATION_MODE,
            "subject_under_test": SUBJECT_UNDER_TEST,
            "output_directory": str(output_path),
            "manifest": str(output_path / "eval_manifest.json"),
            "report": report_payload,
            "harness_error_count": harness_errors,
        }
    except Exception:
        if staging.exists() and staging.is_relative_to(output_path.parent):
            shutil.rmtree(staging)
        raise


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, payloads: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
            for payload in payloads
        ),
        encoding="utf-8",
    )


def _summary_markdown(report: dict[str, Any], harness_errors: int) -> str:
    categories = "\n".join(
        f"- `{name}`: {rate:.1%}"
        for name, rate in report["category_success_rates"].items()
    )
    total_cost = report["total_cost_usd"]
    cost_display = "unknown" if total_cost is None else f"${total_cost:.4f}"
    return (
        "# Phase 5 离线评测报告\n\n"
        "> 范围：`offline_deterministic`，评测确定性组件与控制面；"
        "不代表真实 LLM Agent 的规划成功率。\n\n"
        f"- 任务成功率：{report['passed_count']}/{report['task_count']} "
        f"({report['success_rate']:.1%})\n"
        f"- 非预期工具错误率：{report['unexpected_tool_error_rate']:.2%}\n"
        f"- 注入后毛工具错误率：{report['gross_tool_error_rate']:.2%}\n"
        f"- 安全违规率：{report['safety_violation_rate']:.2%}\n"
        f"- 证据引用准确率：{report['evidence_citation_accuracy']:.2%}\n"
        f"- 延迟 P50 / P95：{report['p50_latency_ms']:.2f} / "
        f"{report['p95_latency_ms']:.2f} ms\n"
        f"- 模型调用与成本：{report['model_call_count']} 次，"
        f"cost_status={report['cost_status']}，total={cost_display}\n"
        f"- Harness 错误：{harness_errors}\n\n"
        "## 分类成功率\n\n"
        + categories
        + "\n"
    )


def _source_tree_sha256(source_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(source_root.glob("*.py"), key=lambda item: item.name):
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


def _safe_error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    return "eval_harness_" + type(exc).__name__.lower()
