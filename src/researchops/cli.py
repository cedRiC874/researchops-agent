from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from .analysis_tools import AnalysisExecutionError
from .anthropic_preflight import run_anthropic_models_preflight
from .audit import AuditError, AuditLedger
from .contracts import ResearchDesign
from .data_quality import CsvValidationError, profile_csv
from .eval_contracts import EvalContractError
from .eval_runner import (
    EvaluationRunError,
    run_offline_evaluation,
    validate_eval_suite,
)
from .eval_v2_contracts import EvalV2ContractError
from .eval_v2_dataset_prep import (
    EvalV2LogicalDatasetRegistry,
    prepare_eval_v2_datasets,
)
from .eval_v2_dataset_verify import verify_eval_v2_dataset_downloads
from .eval_v2_inspect_backend import EvalV2InspectDatasetBackend
from .eval_v2_freeze import (
    validate_eval_v2_dependency_environment,
    validate_public_regression_candidate,
)
from .eval_v2_public_runner import run_public_regression_online
from .eval_v2_public import validate_eval_v2_suite
from .kimi_controlled_pilot import verify_kimi_controlled_pilot_artifacts
from .kimi_controlled_pilot_v2 import (
    verify_kimi_controlled_pilot_v2_artifacts,
)
from .kimi_controlled_pilot_v3 import (
    EXPECTED_CANDIDATE_ID as KIMI_V8_EXPECTED_CANDIDATE_ID,
    verify_kimi_controlled_pilot_v3_artifacts,
)
from .kimi_preflight import run_kimi_models_preflight
from .method_selection import MethodSelectionError, recommend_method
from .model_providers import (
    ANTHROPIC_GENERIC_ONLINE_DISABLED_CODE,
    SUPPORTED_PROVIDER_IDS,
    ProviderConfigurationError,
    get_provider,
)
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
from .phase6_depth60 import (
    run_phase6_depth60_online,
    validate_phase6_depth60_plan,
)
from .self_pilot import (
    create_self_pilot_session,
    get_next_self_pilot_task,
    record_self_pilot_feedback,
    run_self_pilot_task,
    summarize_self_pilot,
)
from .self_pilot_web import SelfPilotWebController, serve_self_pilot_web
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
        "--provider", choices=SUPPORTED_PROVIDER_IDS, default="openai"
    )

    anthropic_preflight_parser = subparsers.add_parser(
        "anthropic-models-preflight",
        help="对 exact allowlisted Anthropic model 执行一次固定 Models API metadata 预检",
    )
    anthropic_preflight_parser.add_argument("--model", required=True)
    anthropic_preflight_parser.add_argument(
        "--confirm-online", action="store_true"
    )

    kimi_preflight_parser = subparsers.add_parser(
        "kimi-models-preflight",
        help="对 Kimi 中国区 exact allowlisted model 执行一次固定 Models API metadata 预检",
    )
    kimi_preflight_parser.add_argument("--model", required=True)
    kimi_preflight_parser.add_argument("--confirm-online", action="store_true")

    kimi_pilot_parser = subparsers.add_parser(
        "kimi-controlled-synthetic-pilot",
        help="历史 v6 入口已永久禁用，仅返回不授权 tombstone",
    )
    kimi_pilot_parser.add_argument("--confirm-online", action="store_true")
    kimi_pilot_parser.add_argument("--accept-locked-caps", action="store_true")
    kimi_pilot_parser.add_argument("--authorization-id")
    kimi_pilot_parser.add_argument("--authorization-expires-at-utc")
    kimi_pilot_parser.add_argument("--terms-retrieved-at-utc")
    kimi_pilot_parser.add_argument("--terms-service-sha256")
    kimi_pilot_parser.add_argument("--terms-privacy-sha256")
    kimi_pilot_parser.add_argument("--terms-payment-sha256")
    kimi_pilot_parser.add_argument(
        "--attest-no-material-terms-delta", action="store_true"
    )
    kimi_pilot_parser.add_argument("--pricing-retrieved-at-utc")
    kimi_pilot_parser.add_argument("--pricing-source-sha256")
    kimi_pilot_parser.add_argument("--pricing-source-bytes", type=int)
    kimi_pilot_parser.add_argument(
        "--attest-kimi-k3-pricing-unchanged", action="store_true"
    )

    kimi_pilot_verify_parser = subparsers.add_parser(
        "kimi-controlled-pilot-verify",
        help="只读验证历史 Candidate v6 / Pilot v1 artifact chain",
    )
    kimi_pilot_verify_parser.add_argument("--authorization-id", required=True)

    kimi_pilot_v7_parser = subparsers.add_parser(
        "kimi-controlled-synthetic-pilot-v7",
        help="历史 v7 入口已永久禁用，仅返回不授权 tombstone",
    )
    kimi_pilot_v7_parser.add_argument("--confirm-online", action="store_true")
    kimi_pilot_v7_parser.add_argument(
        "--accept-successor-v7-locked-caps", action="store_true"
    )
    kimi_pilot_v7_parser.add_argument("--authorized-candidate-commitment")
    kimi_pilot_v7_parser.add_argument("--authorization-id")
    kimi_pilot_v7_parser.add_argument("--authorization-expires-at-utc")
    kimi_pilot_v7_parser.add_argument("--terms-retrieved-at-utc")
    kimi_pilot_v7_parser.add_argument("--terms-service-sha256")
    kimi_pilot_v7_parser.add_argument("--terms-privacy-sha256")
    kimi_pilot_v7_parser.add_argument("--terms-payment-sha256")
    kimi_pilot_v7_parser.add_argument(
        "--attest-no-material-terms-delta", action="store_true"
    )
    kimi_pilot_v7_parser.add_argument("--pricing-retrieved-at-utc")
    kimi_pilot_v7_parser.add_argument("--pricing-source-sha256")
    kimi_pilot_v7_parser.add_argument("--pricing-source-bytes", type=int)
    kimi_pilot_v7_parser.add_argument(
        "--attest-kimi-k3-pricing-unchanged", action="store_true"
    )

    kimi_pilot_v8_parser = subparsers.add_parser(
        "kimi-controlled-synthetic-pilot-v8",
        help="Candidate v8 为 diagnostic-only；在线入口固定不授权",
    )
    kimi_pilot_v8_parser.add_argument("--confirm-online", action="store_true")

    kimi_pilot_v7_verify_parser = subparsers.add_parser(
        "kimi-controlled-pilot-v7-verify",
        help="只读验证历史 Candidate v7 / Pilot v2 artifact chain",
    )
    kimi_pilot_v7_verify_parser.add_argument(
        "--authorization-id", required=True
    )

    kimi_pilot_v8_verify_parser = subparsers.add_parser(
        "kimi-controlled-pilot-v8-verify",
        help="只读验证 Candidate v8 / Pilot v3 artifact chain",
    )
    kimi_pilot_v8_verify_parser.add_argument(
        "--authorization-id", required=True
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
        help="显式确认后，顺序运行真实 Agents SDK Provider 行为评测",
    )
    phase6_run_parser.add_argument(
        "--tasks", type=Path, default=Path("evals/phase6_agent_tasks.jsonl")
    )
    phase6_run_parser.add_argument(
        "--splits", type=Path, default=Path("evals/phase6_splits.json")
    )
    phase6_run_parser.add_argument("--output-dir", type=Path, required=True)
    phase6_run_parser.add_argument(
        "--provider", choices=SUPPORTED_PROVIDER_IDS, required=True
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
    phase6_run_parser.add_argument("--deepseek-pricing-snapshot-date")
    phase6_run_parser.add_argument("--deepseek-pricing-source-url")
    phase6_run_parser.add_argument(
        "--local-observed-cost-stop-cny", type=_positive_float
    )
    phase6_run_parser.add_argument(
        "--max-total-input-tokens", type=_positive_int
    )
    phase6_run_parser.add_argument(
        "--max-total-output-tokens", type=_positive_int
    )
    phase6_run_parser.add_argument(
        "--max-total-requests", type=_positive_int
    )
    phase6_run_parser.add_argument(
        "--total-timeout-seconds", type=_positive_float
    )

    phase6_depth60_parser = subparsers.add_parser(
        "phase6-run-deepseek-depth60-online",
        help="使用冻结单次计划运行 60 题 DeepSeek Phase 6 development 评测",
    )
    phase6_depth60_parser.add_argument(
        "--plan",
        type=Path,
        default=Path("evals/phase6_deepseek_depth60_plan.json"),
    )
    phase6_depth60_parser.add_argument(
        "--output-dir", type=Path, required=True
    )
    phase6_depth60_parser.add_argument("--authorization-id", required=True)
    phase6_depth60_parser.add_argument(
        "--expected-plan-commitment", required=True
    )
    phase6_depth60_parser.add_argument(
        "--authorization-expires-at-utc", required=True
    )
    phase6_depth60_parser.add_argument("--confirm-online", action="store_true")

    phase6_depth60_validate_parser = subparsers.add_parser(
        "phase6-validate-deepseek-depth60",
        help="离线验证 60 题 DeepSeek 单次计划、组件 hash 与授权边界",
    )
    phase6_depth60_validate_parser.add_argument(
        "--plan",
        type=Path,
        default=Path("evals/phase6_deepseek_depth60_plan.json"),
    )

    eval_v2_validate_parser = subparsers.add_parser(
        "eval-v2-validate",
        help="离线验证 Eval v2 campaign 设计、冻结门槛与 private holdout 隔离",
    )
    eval_v2_validate_parser.add_argument(
        "--campaign", type=Path, default=Path("evals/v2/campaign.json")
    )
    eval_v2_validate_parser.add_argument(
        "--datasets",
        type=Path,
        default=Path("evals/v2/external_datasets.json"),
    )
    eval_v2_validate_parser.add_argument(
        "--tasks", type=Path, default=Path("evals/v2/public_tasks.jsonl")
    )
    eval_v2_validate_parser.add_argument(
        "--task-schema",
        type=Path,
        default=Path("evals/v2/public_task_schema.json"),
    )
    eval_v2_validate_parser.add_argument(
        "--internal-review",
        type=Path,
        default=Path("evals/v2/internal_review.json"),
    )

    eval_v2_verify_parser = subparsers.add_parser(
        "eval-v2-verify-datasets",
        help="显式确认后在内存中重新下载并核对 Eval v2 公开数据资产",
    )
    eval_v2_verify_parser.add_argument(
        "--datasets",
        type=Path,
        default=Path("evals/v2/external_datasets.json"),
    )
    eval_v2_verify_parser.add_argument(
        "--timeout-seconds", type=_positive_float, default=30.0
    )
    eval_v2_verify_parser.add_argument("--confirm-download", action="store_true")

    eval_v2_prepare_parser = subparsers.add_parser(
        "eval-v2-prepare-datasets",
        help="显式确认后下载、核验并原子生成 Eval v2 受控数据产物",
    )
    eval_v2_prepare_parser.add_argument(
        "--datasets",
        type=Path,
        default=Path("evals/v2/external_datasets.json"),
    )
    eval_v2_prepare_parser.add_argument("--output-dir", type=Path, required=True)
    eval_v2_prepare_parser.add_argument(
        "--timeout-seconds", type=_positive_float, default=30.0
    )
    eval_v2_prepare_parser.add_argument("--confirm-download", action="store_true")

    eval_v2_registry_parser = subparsers.add_parser(
        "eval-v2-registry-status",
        help="校验准备产物 hash，并输出不含路径的逻辑数据集目录",
    )
    eval_v2_registry_parser.add_argument("--registry", type=Path, required=True)

    eval_v2_inspect_parser = subparsers.add_parser(
        "eval-v2-inspect",
        help="通过逻辑 ID 返回 Eval v2 准备数据的白名单聚合 profile",
    )
    eval_v2_inspect_parser.add_argument("dataset_id")
    eval_v2_inspect_parser.add_argument("--registry", type=Path, required=True)

    eval_v2_freeze_parser = subparsers.add_parser(
        "eval-v2-verify-public-freeze",
        help="离线复核 public-regression candidate 的组件 hash、顺序和通道隔离",
    )
    eval_v2_freeze_parser.add_argument(
        "--candidate",
        type=Path,
        default=Path("evals/v2/public_regression_candidate_v8.json"),
    )
    eval_v2_freeze_parser.add_argument(
        "--verify-environment", action="store_true"
    )

    subparsers.add_parser(
        "eval-v2-verify-environment",
        help="独立验证 requirements.lock 与当前已安装环境；不验证或授权 candidate",
    )

    eval_v2_public_run_parser = subparsers.add_parser(
        "eval-v2-run-public-online",
        help="按锁定顺序、分通道并在预算护栏内运行 Eval v2 public regression",
    )
    eval_v2_public_run_parser.add_argument(
        "--candidate",
        type=Path,
        default=Path("evals/v2/public_regression_candidate_v5.json"),
    )
    eval_v2_public_run_parser.add_argument("--registry", type=Path, required=True)
    eval_v2_public_run_parser.add_argument("--output-dir", type=Path, required=True)
    eval_v2_public_run_parser.add_argument(
        "--budget-cny", type=_positive_float, required=True
    )
    eval_v2_public_run_parser.add_argument(
        "--max-total-input-tokens", type=_positive_int, default=1_000_000
    )
    eval_v2_public_run_parser.add_argument(
        "--max-total-output-tokens", type=_positive_int, default=333_333
    )
    eval_v2_public_run_parser.add_argument(
        "--max-model-calls", type=_positive_int, default=744
    )
    eval_v2_public_run_parser.add_argument("--resume", action="store_true")
    eval_v2_public_run_parser.add_argument("--confirm-online", action="store_true")

    pilot_create_parser = subparsers.add_parser(
        "self-pilot-create",
        help="创建不含 golden 的内部 self-pilot 任务包",
    )
    pilot_create_parser.add_argument("--output-dir", type=Path, required=True)
    pilot_create_parser.add_argument(
        "--tasks", type=Path, default=Path("evals/v2/public_tasks.jsonl")
    )
    pilot_create_parser.add_argument(
        "--datasets",
        type=Path,
        default=Path("evals/v2/external_datasets.json"),
    )
    pilot_create_parser.add_argument(
        "--task-count", type=_positive_int, default=12
    )

    pilot_next_parser = subparsers.add_parser(
        "self-pilot-next", help="显示下一道待运行或待反馈的 self-pilot 任务"
    )
    pilot_next_parser.add_argument("--session-dir", type=Path, required=True)

    pilot_run_parser = subparsers.add_parser(
        "self-pilot-run", help="显式确认后运行一道 self-pilot Provider 任务"
    )
    pilot_run_parser.add_argument("--session-dir", type=Path, required=True)
    pilot_run_parser.add_argument("--registry", type=Path, required=True)
    pilot_run_parser.add_argument(
        "--tasks", type=Path, default=Path("evals/v2/public_tasks.jsonl")
    )
    pilot_run_parser.add_argument(
        "--datasets",
        type=Path,
        default=Path("evals/v2/external_datasets.json"),
    )
    pilot_run_parser.add_argument(
        "--provider", choices=SUPPORTED_PROVIDER_IDS, required=True
    )
    pilot_run_parser.add_argument("--model", required=True)
    pilot_run_parser.add_argument("--task-id")
    pilot_run_parser.add_argument("--max-turns", type=_positive_int, default=8)
    pilot_run_parser.add_argument(
        "--timeout-seconds", type=_positive_float, default=120.0
    )
    pilot_run_parser.add_argument("--confirm-online", action="store_true")

    pilot_record_parser = subparsers.add_parser(
        "self-pilot-record", help="记录 self-pilot 人工接受度、时间和修改情况"
    )
    pilot_record_parser.add_argument("--session-dir", type=Path, required=True)
    pilot_record_parser.add_argument("--task-id", required=True)
    pilot_record_parser.add_argument(
        "--accepted", choices=("yes", "no"), required=True
    )
    pilot_record_parser.add_argument(
        "--first-pass", choices=("yes", "no"), required=True
    )
    pilot_record_parser.add_argument(
        "--manual-revisions", type=_nonnegative_int, required=True
    )
    pilot_record_parser.add_argument(
        "--duration-seconds", type=_positive_float, required=True
    )
    pilot_record_parser.add_argument(
        "--critical-error", choices=("yes", "no"), required=True
    )
    pilot_record_parser.add_argument(
        "--safety-concern", choices=("yes", "no"), required=True
    )
    pilot_record_parser.add_argument(
        "--clarification-useful", choices=("yes", "no", "na"), default="na"
    )
    pilot_record_parser.add_argument("--notes")

    pilot_web_parser = subparsers.add_parser(
        "self-pilot-web",
        help="启动仅绑定本机的双语 self-pilot 人工评测网页",
    )
    pilot_web_parser.add_argument("--session-dir", type=Path, required=True)
    pilot_web_parser.add_argument("--registry", type=Path, required=True)
    pilot_web_parser.add_argument(
        "--tasks", type=Path, default=Path("evals/v2/public_tasks.jsonl")
    )
    pilot_web_parser.add_argument(
        "--datasets",
        type=Path,
        default=Path("evals/v2/external_datasets.json"),
    )
    pilot_web_parser.add_argument(
        "--translations",
        type=Path,
        default=Path("evals/v2/self_pilot_translations.zh-CN.json"),
    )
    pilot_web_parser.add_argument("--port", type=_tcp_port, default=8765)
    pilot_web_parser.add_argument("--max-turns", type=_positive_int, default=8)
    pilot_web_parser.add_argument(
        "--timeout-seconds", type=_positive_float, default=120.0
    )
    pilot_web_parser.add_argument("--confirm-online", action="store_true")

    pilot_summary_parser = subparsers.add_parser(
        "self-pilot-summary", help="生成并显示内部 self-pilot Markdown 汇总"
    )
    pilot_summary_parser.add_argument("--session-dir", type=Path, required=True)
    return parser


def _kimi_pilot_cli_not_run(
    *,
    command: str,
    error_code: str,
    candidate_id: str | None,
    contract_id: str,
) -> dict[str, object]:
    """Return a non-artifact, zero-call tombstone receipt."""

    return {
        "schema_version": "kimi-controlled-pilot-cli-gate/1.0",
        "status": "not_run",
        "command": command,
        "error_code": error_code,
        "contract_id": contract_id,
        "candidate_id": candidate_id,
        "candidate_commitment_sha256": None,
        "chat_contract_sha256": None,
        "pilot_contract_sha256": None,
        "scenario_count": 3,
        "scenarios_completed": 0,
        "model_request_count": 0,
        "model_request_limit": 8,
        "network_attempts": 0,
        "network_calls": 0,
        "requested_tool_call_count": 0,
        "deduplicated_tool_call_count": 0,
        "executed_tool_call_count": 0,
        "expected_invalid_request_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "usage_observed_request_count": 0,
        "usage_complete": False,
        "pricing_attestation": None,
        "local_estimated_reservation_limit_cny": "5.000000",
        "local_cost_claim_scope": "local_conservative_hard_stop_only",
        "actual_billed_cost_cny": None,
        "outcome_unknown": False,
        "candidate_result_created": False,
        "key_loader_passed_to_runner": False,
        "authorizes_retry": False,
        "authorizes_resume": False,
        "authorizes_chat": False,
        "authorizes_tools": False,
        "authorizes_model_quality_claim": False,
        "authorizes_provider_registration": False,
        "authorizes_private_evaluation": False,
        "authorizes_non_synthetic_data": False,
    }


_KIMI_V8_CANDIDATE_RELATIVE_PATH = Path(
    "evals/v2/public_regression_candidate_v8.json"
)


def _kimi_v8_cli_not_run(
    *,
    error_code: str,
    candidate_commitment_sha256: str | None = None,
    chat_contract_sha256: str | None = None,
    pilot_contract_sha256: str | None = None,
) -> dict[str, object]:
    result = _kimi_pilot_cli_not_run(
        command="kimi-controlled-synthetic-pilot-v8",
        error_code=error_code,
        candidate_id=KIMI_V8_EXPECTED_CANDIDATE_ID,
        contract_id="kimi-controlled-synthetic-pilot-v3",
    )
    result["candidate_commitment_sha256"] = candidate_commitment_sha256
    result["chat_contract_sha256"] = chat_contract_sha256
    result["pilot_contract_sha256"] = pilot_contract_sha256
    return result


def _load_deepseek_api_key() -> str:
    """Load the DeepSeek Key only after Candidate execution scope is authorized."""

    return os.environ.get("DEEPSEEK_API_KEY", "")


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
        elif args.command == "anthropic-models-preflight":
            result = asyncio.run(
                run_anthropic_models_preflight(
                    provider_id="anthropic",
                    model_id=args.model,
                    api_key=None,
                    confirm_online=args.confirm_online,
                    _key_loader=lambda: os.environ.get("ANTHROPIC_API_KEY"),
                )
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "verified" else 4
        elif args.command == "kimi-models-preflight":
            result = asyncio.run(
                run_kimi_models_preflight(
                    provider_id="moonshot_kimi",
                    model_id=args.model,
                    api_key=None,
                    confirm_online=args.confirm_online,
                    _key_loader=lambda: os.environ.get("MOONSHOT_API_KEY"),
                )
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "verified" else 4
        elif args.command == "kimi-controlled-synthetic-pilot":
            result = _kimi_pilot_cli_not_run(
                command="kimi-controlled-synthetic-pilot",
                error_code="kimi_pilot_v6_online_permanently_disabled",
                candidate_id=None,
                contract_id="kimi-controlled-synthetic-pilot-v1",
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 4
        elif args.command == "kimi-controlled-pilot-verify":
            project_root = Path(__file__).resolve().parents[2]
            result = verify_kimi_controlled_pilot_artifacts(
                project_root,
                authorization_id=args.authorization_id,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "kimi-controlled-synthetic-pilot-v7":
            result = _kimi_pilot_cli_not_run(
                command="kimi-controlled-synthetic-pilot-v7",
                error_code="kimi_pilot_v7_online_permanently_disabled",
                candidate_id=(
                    "eval-v2-public-regression-deepseek-kimi-controlled-chat-v7"
                ),
                contract_id="kimi-controlled-synthetic-pilot-v2",
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 4
        elif args.command == "kimi-controlled-synthetic-pilot-v8":
            if not args.confirm_online:
                result = _kimi_v8_cli_not_run(
                    error_code="kimi_pilot_v8_confirmation_required"
                )
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 4
            project_root = Path(__file__).resolve().parents[2]
            candidate_path = project_root / _KIMI_V8_CANDIDATE_RELATIVE_PATH
            try:
                candidate_summary = validate_public_regression_candidate(
                    project_root=project_root,
                    candidate_path=candidate_path,
                    verify_environment=False,
                )
            except (EvalV2ContractError, OSError, ValueError, json.JSONDecodeError):
                result = _kimi_v8_cli_not_run(
                    error_code="kimi_pilot_v8_candidate_invalid"
                )
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 4
            candidate_id = candidate_summary.get("candidate_id")
            candidate_commitment = candidate_summary.get(
                "candidate_commitment_sha256"
            )
            if (
                candidate_summary.get("status") != "valid"
                or candidate_id != KIMI_V8_EXPECTED_CANDIDATE_ID
                or not isinstance(candidate_commitment, str)
                or candidate_summary.get("diagnostic_snapshot_only") is not True
                or candidate_summary.get("controlled_synthetic_pilot_online_authorized")
                is not False
            ):
                result = _kimi_v8_cli_not_run(
                    error_code="kimi_pilot_v8_candidate_invalid"
                )
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 4
            result = _kimi_v8_cli_not_run(
                error_code="kimi_pilot_v8_online_not_authorized",
                candidate_commitment_sha256=candidate_commitment,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 4
        elif args.command == "kimi-controlled-pilot-v7-verify":
            project_root = Path(__file__).resolve().parents[2]
            result = verify_kimi_controlled_pilot_v2_artifacts(
                project_root,
                authorization_id=args.authorization_id,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "kimi-controlled-pilot-v8-verify":
            project_root = Path(__file__).resolve().parents[2]
            result = verify_kimi_controlled_pilot_v3_artifacts(
                project_root,
                authorization_id=args.authorization_id,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
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
                    deepseek_pricing_snapshot_date=(
                        args.deepseek_pricing_snapshot_date
                    ),
                    deepseek_pricing_source_url=args.deepseek_pricing_source_url,
                    local_observed_cost_stop_cny=(
                        args.local_observed_cost_stop_cny
                    ),
                    total_input_tokens_cap=args.max_total_input_tokens,
                    total_output_tokens_cap=args.max_total_output_tokens,
                    total_requests_cap=args.max_total_requests,
                    total_timeout_seconds=args.total_timeout_seconds,
                )
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            report = result["report"]
            return 0 if (
                report["harness_error_count"] == 0
                and report["passed"] == report["included"]
                and report.get("run_status") == "completed"
                and report.get("not_started_case_count") == 0
            ) else 5
        elif args.command == "phase6-run-deepseek-depth60-online":
            project_root = Path(__file__).resolve().parents[2]
            result = asyncio.run(
                run_phase6_depth60_online(
                    project_root=project_root,
                    plan_path=args.plan,
                    output_directory=args.output_dir,
                    authorization_id=args.authorization_id,
                    expected_plan_commitment=args.expected_plan_commitment,
                    authorization_expires_at_utc=(
                        args.authorization_expires_at_utc
                    ),
                    confirm_online=args.confirm_online,
                )
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            report = result["report"]
            return 0 if (
                report.get("run_status") == "completed"
                and report.get("attempted_case_count") == 60
                and report.get("not_started_case_count") == 0
                and report.get("harness_error_count") == 0
            ) else 5
        elif args.command == "phase6-validate-deepseek-depth60":
            project_root = Path(__file__).resolve().parents[2]
            result = validate_phase6_depth60_plan(project_root, args.plan)
            result.pop("plan", None)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "eval-v2-validate":
            result = validate_eval_v2_suite(
                campaign_path=args.campaign,
                dataset_manifest_path=args.datasets,
                public_tasks_path=args.tasks,
                task_schema_path=args.task_schema,
                internal_review_path=args.internal_review,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "eval-v2-verify-datasets":
            result = verify_eval_v2_dataset_downloads(
                args.datasets,
                confirm_download=args.confirm_download,
                timeout_seconds=args.timeout_seconds,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "verified" else 4
        elif args.command == "eval-v2-prepare-datasets":
            project_root = Path(__file__).resolve().parents[2]
            result = prepare_eval_v2_datasets(
                project_root=project_root,
                dataset_manifest_path=args.datasets,
                output_directory=args.output_dir,
                confirm_download=args.confirm_download,
                timeout_seconds=args.timeout_seconds,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "prepared" else 4
        elif args.command == "eval-v2-registry-status":
            registry = EvalV2LogicalDatasetRegistry.load(args.registry)
            result = {
                "status": "valid",
                "dataset_count": len(registry.dataset_ids),
                "datasets": registry.public_catalog(),
                "filesystem_paths_exposed": False,
                "model_row_access": False,
                "network_calls": 0,
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "eval-v2-inspect":
            backend = EvalV2InspectDatasetBackend.from_registry_path(args.registry)
            result = backend.inspect_dataset(args.dataset_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "eval-v2-verify-public-freeze":
            project_root = Path(__file__).resolve().parents[2]
            result = validate_public_regression_candidate(
                project_root=project_root,
                candidate_path=args.candidate,
                verify_environment=args.verify_environment,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "eval-v2-verify-environment":
            project_root = Path(__file__).resolve().parents[2]
            result = validate_eval_v2_dependency_environment(project_root)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "eval-v2-run-public-online":
            project_root = Path(__file__).resolve().parents[2]
            candidate_scope = validate_public_regression_candidate(
                project_root=project_root,
                candidate_path=args.candidate,
                verify_environment=False,
            )
            if candidate_scope.get("historical_snapshot_only") is not False:
                raise EvalV2ContractError(
                    "eval_v2_historical_candidate_execution_forbidden",
                    "Historical candidate 只允许离线复核，不能授权 public-regression 在线执行。",
                )
            if candidate_scope.get("public_regression_online_authorized") is False:
                raise EvalV2ContractError(
                    "eval_v2_candidate_online_execution_forbidden",
                    "Diagnostic-only candidate 不能授权 public-regression 在线执行。",
                )
            result = run_public_regression_online(
                project_root=project_root,
                candidate_path=args.candidate,
                registry_path=args.registry,
                output_directory=args.output_dir,
                api_key=_load_deepseek_api_key(),
                budget_cny=args.budget_cny,
                confirm_online=args.confirm_online,
                resume=args.resume,
                max_total_input_tokens=args.max_total_input_tokens,
                max_total_output_tokens=args.max_total_output_tokens,
                max_model_calls=args.max_model_calls,
                progress_callback=lambda event: print(
                    json.dumps(event, ensure_ascii=False), flush=True
                ),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "complete" else 5
        elif args.command == "self-pilot-create":
            project_root = Path(__file__).resolve().parents[2]
            result = create_self_pilot_session(
                project_root=project_root,
                output_directory=args.output_dir,
                tasks_path=args.tasks,
                dataset_manifest_path=args.datasets,
                task_count=args.task_count,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "self-pilot-next":
            project_root = Path(__file__).resolve().parents[2]
            result = get_next_self_pilot_task(
                project_root=project_root,
                session_directory=args.session_dir,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "self-pilot-run":
            if not args.confirm_online:
                raise EvalV2ContractError(
                    "eval_v2_online_confirmation_required",
                    "Self-pilot Provider 运行需要 --confirm-online。",
                )
            if args.provider == "anthropic":
                raise EvalV2ContractError(
                    ANTHROPIC_GENERIC_ONLINE_DISABLED_CODE,
                    "Generic self-pilot Anthropic 入口未获受控 pilot 授权；Models preflight receipt 不授权运行。",
                )
            project_root = Path(__file__).resolve().parents[2]
            provider = get_provider(args.provider)
            api_key = os.environ.get(provider.api_key_env, "")
            result = run_self_pilot_task(
                project_root=project_root,
                session_directory=args.session_dir,
                tasks_path=args.tasks,
                dataset_manifest_path=args.datasets,
                registry_path=args.registry,
                provider=provider,
                model_id=args.model,
                api_key=api_key,
                task_id=args.task_id,
                confirm_online=True,
                max_turns=args.max_turns,
                run_timeout_seconds=args.timeout_seconds,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "self-pilot-record":
            project_root = Path(__file__).resolve().parents[2]
            clarification = (
                None
                if args.clarification_useful == "na"
                else args.clarification_useful == "yes"
            )
            result = record_self_pilot_feedback(
                project_root=project_root,
                session_directory=args.session_dir,
                task_id=args.task_id,
                accepted=args.accepted == "yes",
                first_pass=args.first_pass == "yes",
                manual_revisions=args.manual_revisions,
                duration_seconds=args.duration_seconds,
                critical_error=args.critical_error == "yes",
                safety_concern=args.safety_concern == "yes",
                clarification_useful=clarification,
                notes=args.notes,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "self-pilot-web":
            project_root = Path(__file__).resolve().parents[2]
            controller = SelfPilotWebController(
                project_root=project_root,
                session_directory=args.session_dir,
                tasks_path=args.tasks,
                dataset_manifest_path=args.datasets,
                registry_path=args.registry,
                translations_path=args.translations,
                confirm_online=args.confirm_online,
                max_turns=args.max_turns,
                run_timeout_seconds=args.timeout_seconds,
            )
            serve_self_pilot_web(controller, port=args.port)
            return 0
        elif args.command == "self-pilot-summary":
            project_root = Path(__file__).resolve().parents[2]
            result = summarize_self_pilot(
                project_root=project_root,
                session_directory=args.session_dir,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        else:
            return 2
    except (
        ToolRuntimeError,
        OpenAIAgentIntegrationError,
        AuditError,
        EvalContractError,
        EvalV2ContractError,
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


def _tcp_port(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 65535:
        raise argparse.ArgumentTypeError("端口必须在 1 到 65535 之间")
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


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是非负整数") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
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
