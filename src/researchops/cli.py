from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .analysis_tools import AnalysisExecutionError
from .audit import AuditError, AuditLedger
from .contracts import ResearchDesign
from .data_quality import CsvValidationError, profile_csv
from .eval_contracts import EvalContractError
from .eval_runner import (
    EvaluationRunError,
    run_offline_evaluation,
    validate_eval_suite,
)
from .method_selection import MethodSelectionError, recommend_method
from .model_providers import ProviderConfigurationError
from .offline_agent import (
    decide_phase4_call,
    resume_phase4_call,
    start_phase4_demo,
)
from .openai_agent import OpenAIAgentIntegrationError, sdk_status
from .phase6_agent import Phase6AgentError
from .phase6_eval import Phase6ContractError
from .phase6_runner import (
    Phase6RunError,
    phase6_status,
    run_phase6_online_evaluation,
    validate_phase6_suite,
)
from .tool_runtime import ToolRuntimeError
from .workflow import run_phase3_analysis, validate_cli_output_directory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ResearchOps CSV 数据质量工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="检查 CSV 结构和缺失值")
    inspect_parser.add_argument("csv_path", type=Path)
    inspect_parser.add_argument("--output", type=Path, help="将 JSON 结果保存到文件")

    method_parser = subparsers.add_parser(
        "recommend-method",
        help="根据 CSV 概要和显式研究设计推荐统计方法",
    )
    method_parser.add_argument("csv_path", type=Path)
    method_parser.add_argument("design_path", type=Path)
    method_parser.add_argument("--output", type=Path, help="将 JSON 结果保存到文件")

    analyze_parser = subparsers.add_parser(
        "analyze",
        help="执行受控的 ANCOVA、Welch 检验和聚合效应图",
    )
    analyze_parser.add_argument("csv_path", type=Path)
    analyze_parser.add_argument("design_path", type=Path)
    analyze_parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="位于项目 artifacts 目录下的新建子目录",
    )

    phase4_parser = subparsers.add_parser(
        "phase4-start",
        help="运行离线 Agent，记录一次瞬时错误与重试，并在发布前暂停审批",
    )
    phase4_parser.add_argument(
        "--audit-db", type=Path, default=Path("artifacts/phase4/audit.sqlite3")
    )
    phase4_parser.add_argument(
        "--audit-export", type=Path, default=Path("artifacts/phase4/audit_export.json")
    )
    phase4_parser.add_argument("--release-name", default="demo-release")

    decide_parser = subparsers.add_parser(
        "phase4-decide",
        help="人工批准或拒绝一个等待中的受控工具调用",
    )
    decide_parser.add_argument("call_id")
    decide_parser.add_argument("--decision", choices=("approve", "reject"), required=True)
    decide_parser.add_argument("--approver", required=True)
    decide_parser.add_argument("--reason")
    decide_parser.add_argument(
        "--audit-db", type=Path, default=Path("artifacts/phase4/audit.sqlite3")
    )
    decide_parser.add_argument(
        "--audit-export", type=Path, default=Path("artifacts/phase4/audit_export.json")
    )

    resume_parser = subparsers.add_parser(
        "phase4-resume",
        help="在本地再次校验审批范围后恢复受控工具调用",
    )
    resume_parser.add_argument("call_id")
    resume_parser.add_argument(
        "--audit-db", type=Path, default=Path("artifacts/phase4/audit.sqlite3")
    )
    resume_parser.add_argument(
        "--audit-export", type=Path, default=Path("artifacts/phase4/audit_export.json")
    )

    verify_parser = subparsers.add_parser(
        "audit-verify", help="验证指定运行的追加事件 SHA-256 链"
    )
    verify_parser.add_argument("run_id")
    verify_parser.add_argument(
        "--audit-db", type=Path, default=Path("artifacts/phase4/audit.sqlite3")
    )

    subparsers.add_parser(
        "agent-sdk-status", help="检查官方 Agents SDK 和 API Key 是否就绪（不发起网络调用）"
    )

    eval_validate_parser = subparsers.add_parser(
        "eval-validate", help="严格验证第五阶段 50 项 JSONL 评测集"
    )
    eval_validate_parser.add_argument(
        "--tasks", type=Path, default=Path("evals/tasks.jsonl")
    )

    eval_run_parser = subparsers.add_parser(
        "eval-run", help="运行完全离线的 50 项组件与控制面评测"
    )
    eval_run_parser.add_argument(
        "--tasks", type=Path, default=Path("evals/tasks.jsonl")
    )
    eval_run_parser.add_argument(
        "--output-dir", type=Path, default=Path("artifacts/phase5")
    )

    phase6_status_parser = subparsers.add_parser(
        "phase6-status",
        help="检查第六阶段在线评测就绪状态（不导入 Runner、不联网）",
    )
    phase6_status_parser.add_argument(
        "--provider", choices=("openai", "deepseek"), default="openai"
    )

    phase6_validate_parser = subparsers.add_parser(
        "phase6-validate", help="严格验证第六阶段 Agent 行为评测集与 split 清单"
    )
    phase6_validate_parser.add_argument(
        "--tasks", type=Path, default=Path("evals/phase6_agent_tasks.jsonl")
    )
    phase6_validate_parser.add_argument(
        "--splits", type=Path, default=Path("evals/phase6_splits.json")
    )

    phase6_run_parser = subparsers.add_parser(
        "phase6-run-online",
        help="显式确认后，顺序运行真实 OpenAI Agents SDK 行为评测",
    )
    phase6_run_parser.add_argument(
        "--tasks", type=Path, default=Path("evals/phase6_agent_tasks.jsonl")
    )
    phase6_run_parser.add_argument(
        "--splits", type=Path, default=Path("evals/phase6_splits.json")
    )
    phase6_run_parser.add_argument("--output-dir", type=Path, required=True)
    phase6_run_parser.add_argument(
        "--provider", choices=("openai", "deepseek"), required=True
    )
    phase6_run_parser.add_argument("--model", required=True)
    phase6_run_parser.add_argument(
        "--split", choices=("development", "holdout"), required=True
    )
    phase6_run_parser.add_argument(
        "--max-cases", type=_positive_int, required=True
    )
    phase6_run_parser.add_argument("--max-turns", type=_positive_int, default=8)
    phase6_run_parser.add_argument(
        "--case-timeout-seconds", type=_positive_float, default=120.0
    )
    phase6_run_parser.add_argument("--confirm-online", action="store_true")
    phase6_run_parser.add_argument(
        "--input-price-per-million-usd", type=_nonnegative_float
    )
    phase6_run_parser.add_argument(
        "--output-price-per-million-usd", type=_nonnegative_float
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "inspect":
            profile = profile_csv(args.csv_path)
            result = profile.to_dict()
        elif args.command == "recommend-method":
            profile = profile_csv(args.csv_path)
            design = _load_design(args.design_path)
            result = recommend_method(profile, design).to_dict()
        elif args.command == "analyze":
            design = _load_design(args.design_path)
            output_directory = validate_cli_output_directory(args.output_dir)
            result = run_phase3_analysis(args.csv_path, design, output_directory).to_dict()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "phase4-start":
            project_root = Path(__file__).resolve().parents[2]
            audit_db = _validate_artifact_file(args.audit_db)
            audit_export = _validate_artifact_file(args.audit_export)
            result = start_phase4_demo(
                project_root=project_root,
                audit_database=audit_db,
                audit_export=audit_export,
                release_name=args.release_name,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "phase4-decide":
            project_root = Path(__file__).resolve().parents[2]
            result = decide_phase4_call(
                project_root=project_root,
                audit_database=_validate_artifact_file(args.audit_db),
                call_id=args.call_id,
                decision=args.decision,
                approver=args.approver,
                reason=args.reason,
                audit_export=_validate_artifact_file(args.audit_export),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "phase4-resume":
            project_root = Path(__file__).resolve().parents[2]
            result = resume_phase4_call(
                project_root=project_root,
                audit_database=_validate_artifact_file(args.audit_db),
                call_id=args.call_id,
                audit_export=_validate_artifact_file(args.audit_export),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "audit-verify":
            result = AuditLedger(_validate_artifact_file(args.audit_db)).verify_chain(args.run_id).to_dict()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["valid"] else 4
        elif args.command == "agent-sdk-status":
            print(json.dumps(sdk_status(), ensure_ascii=False, indent=2))
            return 0
        elif args.command == "eval-validate":
            result = validate_eval_suite(args.tasks)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "eval-run":
            project_root = Path(__file__).resolve().parents[2]
            result = run_offline_evaluation(
                project_root=project_root,
                tasks_path=args.tasks,
                output_directory=args.output_dir,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["report"]["failed_count"] == 0 else 5
        elif args.command == "phase6-status":
            print(
                json.dumps(
                    phase6_status(provider=args.provider),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        elif args.command == "phase6-validate":
            result = validate_phase6_suite(args.tasks, args.splits)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "phase6-run-online":
            project_root = Path(__file__).resolve().parents[2]
            result = asyncio.run(
                run_phase6_online_evaluation(
                    project_root=project_root,
                    tasks_path=args.tasks,
                    split_manifest_path=args.splits,
                    output_directory=args.output_dir,
                    provider=args.provider,
                    model=args.model,
                    split=args.split,
                    max_cases=args.max_cases,
                    max_turns=args.max_turns,
                    case_timeout_seconds=args.case_timeout_seconds,
                    confirm_online=args.confirm_online,
                    input_price_per_million_usd=args.input_price_per_million_usd,
                    output_price_per_million_usd=args.output_price_per_million_usd,
                )
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            report = result["report"]
            return 0 if (
                report["harness_error_count"] == 0
                and report["passed"] == report["included"]
            ) else 5
        else:
            return 2
    except (
        ToolRuntimeError,
        OpenAIAgentIntegrationError,
        AuditError,
        EvalContractError,
        EvaluationRunError,
        ProviderConfigurationError,
        Phase6AgentError,
        Phase6ContractError,
        Phase6RunError,
    ) as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2))
        return 4
    except AnalysisExecutionError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2))
        return 3
    except MethodSelectionError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2))
        return 2
    except (CsvValidationError, OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")

    print(payload)
    return 0


def _load_design(path: Path) -> ResearchDesign:
    design_payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(design_payload, dict):
        raise ValueError("研究设计 JSON 顶层必须是对象。")
    return ResearchDesign.from_dict(design_payload)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正数")
    return parsed


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是非负数") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是非负数")
    return parsed


def _validate_artifact_file(path: Path) -> Path:
    project_root = Path(__file__).resolve().parents[2]
    artifacts_root = (project_root / "artifacts").resolve()
    resolved = path.resolve()
    if resolved == artifacts_root or not resolved.is_relative_to(artifacts_root):
        raise ValueError("第四阶段文件必须位于项目 artifacts 目录内。")
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
