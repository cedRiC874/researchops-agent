"""本地运维入口；审批命令不会被注册为 MCP 工具。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from researchops.audit import AuditError
from researchops.tool_runtime import ToolRuntimeError

from .gateway import Gateway


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数。")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ResearchOps MCP 网关与本地人工审批")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parents[2] / ".state")
    parser.add_argument("--run-ttl", type=positive_int, default=3600)
    parser.add_argument("--max-calls", type=positive_int, default=100)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="通过标准输入输出启动 MCP 服务")
    approvals = commands.add_parser("approvals", help="只能由本地操作者使用的审批入口")
    actions = approvals.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="列出等待人工复核的调用")
    for decision in ("approve", "reject"):
        action = actions.add_parser(decision)
        action.add_argument("call_id")
        action.add_argument("--approver", required=True, help="人工审批人身份，不从环境读取")
        if decision == "approve":
            action.add_argument("--ttl", type=positive_int, default=900)
    return parser


def make_gateway(args: argparse.Namespace) -> Gateway:
    return Gateway(
        args.project_root,
        args.state_dir,
        source_snapshot_root=args.source_root,
        run_ttl_seconds=args.run_ttl,
        max_calls_per_run=args.max_calls,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        gateway = make_gateway(args)
        if args.command == "serve":
            from .sdk_adapter import run_stdio

            run_stdio(gateway)
            return 0
        if args.action == "list":
            result = {"status": "ok", "approvals": gateway.list_pending_approvals()}
        else:
            result = gateway.decide(
                args.call_id,
                decision=args.action,
                approver=args.approver,
                ttl=getattr(args, "ttl", 900),
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ToolRuntimeError, AuditError) as exc:
        result = {"status": "error", "error_code": exc.code, "message": "操作被拒绝，请核对本地参数与审批状态。"}
    except (OSError, ValueError):
        result = {"status": "error", "error_code": "gateway_configuration_invalid", "message": "本地配置无法加载。"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
