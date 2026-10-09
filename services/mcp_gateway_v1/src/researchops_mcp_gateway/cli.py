"""本地运维入口；审批命令不会被注册为 MCP 工具。"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from researchops.audit import AuditError
from researchops.tool_runtime import RiskLevel, ToolRuntimeError

from .gateway import Gateway
from .safety import error_payload


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
    parser.add_argument("--upstreams", type=Path, help="本地运维提供的上游服务器 JSON 配置")
    parser.add_argument("--manifest-file", type=Path, help="固定清单；默认位于 state-dir/manifest.json")
    parser.add_argument("--publish-pattern", action="append", help="发布名称的拒绝正则，可重复；省略时使用默认模式")
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
    manifest = commands.add_parser("manifest", help="本地固定、解除固定及查看上游工具清单")
    changes = manifest.add_subparsers(dest="action", required=True)
    changes.add_parser("show")
    pin = changes.add_parser("pin")
    pin.add_argument("--server", required=True, help="上游配置中的 server_id")
    source = pin.add_mutually_exclusive_group(required=True)
    source.add_argument("--definition", type=Path, help="由操作者复核的工具定义 JSON")
    source.add_argument("--tool", help="从已配置上游读取并固定指定工具")
    pin.add_argument("--risk", choices=[risk.value for risk in RiskLevel], help="省略风险时默认拒绝")
    unpin = changes.add_parser("unpin")
    unpin.add_argument("--server", required=True)
    unpin.add_argument("--tool", required=True)
    return parser


def make_gateway(args: argparse.Namespace) -> Gateway:
    proxy = None
    if args.upstreams:
        from .manifest import ManifestStore
        from .proxy import ProxyManager
        from .upstream import load_upstreams

        proxy = ProxyManager(ManifestStore(args.manifest_file or args.state_dir / "manifest.json"),
                             load_upstreams(args.upstreams))
    return Gateway(
        args.project_root,
        args.state_dir,
        source_snapshot_root=args.source_root,
        run_ttl_seconds=args.run_ttl,
        max_calls_per_run=args.max_calls,
        publish_patterns=args.publish_pattern,
        proxy=proxy,
    )


def manage_manifest(args: argparse.Namespace) -> dict:
    from .manifest import ManifestStore

    manifest = ManifestStore(args.manifest_file or args.state_dir / "manifest.json")
    if args.action == "show":
        return manifest.show()
    if args.action == "unpin":
        manifest.unpin(args.server, args.tool)
        return {"status": "unpinned", "server_id": args.server, "name": args.tool}
    if args.definition:
        if args.definition.stat().st_size > 1_048_576:
            raise ToolRuntimeError("gateway_manifest_invalid", "工具定义文件过大。")
        definition = json.loads(args.definition.read_text(encoding="utf-8"))
    else:
        if not args.upstreams:
            raise ToolRuntimeError("gateway_configuration_invalid", "固定上游工具需要本地上游配置。")
        from .upstream import load_upstreams

        clients = load_upstreams(args.upstreams)
        if args.server not in clients:
            raise ToolRuntimeError("gateway_configuration_invalid", "未配置该上游服务器。")
        matches = [tool for tool in clients[args.server].list_tools() if tool.get("name") == args.tool]
        if len(matches) != 1:
            raise ToolRuntimeError("gateway_manifest_invalid", "无法唯一确定待固定工具。")
        definition = matches[0]
    entry = manifest.pin(args.server, definition, risk=args.risk)
    return {"status": "pinned", "tool": entry}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "manifest":
            print(json.dumps(manage_manifest(args), ensure_ascii=False, sort_keys=True))
            return 0
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
        result = error_payload(exc.code)
    except (OSError, ValueError, re.error):
        result = {"status": "error", "error_code": "gateway_configuration_invalid", "message": "本地配置无法加载。"}
    except Exception:
        result = error_payload("gateway_internal_error")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
