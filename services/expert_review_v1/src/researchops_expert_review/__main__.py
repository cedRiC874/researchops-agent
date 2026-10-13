from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import core
from .core import ExpertReview, ExpertReviewError, completion_summary, create_session, preview


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="现场提问：复用 Eval v2，无自动评分。")
    commands = parser.add_subparsers(dest="command", required=True)
    session = commands.add_parser("session-create", help="在 artifacts 下创建全新会话")
    session.add_argument("--output-dir", required=True)
    inspection = commands.add_parser("preview", help="仅在本机展示检查工具返回值")
    inspection.add_argument("--registry", required=True)
    inspection.add_argument("--dataset-id", required=True)
    inspection.add_argument("--local-head", type=int, choices=range(6), default=0)
    for name in ("ask", "web"):
        command = commands.add_parser(name)
        command.add_argument("--session-dir", required=True)
        command.add_argument("--registry", required=True)
        command.add_argument("--provider", choices=["deepseek"], required=True)
        command.add_argument("--model", required=True)
        if name == "ask":
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument("--question-file")
            source.add_argument("--from-task")
            command.add_argument("--dataset-id")
            command.add_argument("--confirm-online", action="store_true")
        else:
            command.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(__file__).resolve().parents[4]
    try:
        if args.command == "session-create":
            print(create_session(project_root=root, output_directory=args.output_dir)["message"])
            return 0
        if args.command == "preview":
            print(preview(registry_path=args.registry, dataset_id=args.dataset_id, local_head=args.local_head))
            return 0
        if args.command == "ask":
            if not args.confirm_online:
                raise ExpertReviewError("online_confirmation_required", "缺少 --confirm-online；拒绝运行，未读取密钥。")
            if bool(args.question_file) != bool(args.dataset_id):
                raise ExpertReviewError("question_source_invalid", "现场问题必须同时提供数据集和问题文件；示例题的数据集由题目决定。")
        review = ExpertReview(project_root=root, session_directory=args.session_dir,
                              registry_path=args.registry, provider_id=args.provider, model_id=args.model)
        if args.command == "web":
            from .web import serve

            serve(review, port=args.port)
            return 0
        question = Path(args.question_file).read_text(encoding="utf-8-sig") if args.question_file else None
        record = review.ask(confirm_online=True, dataset_id=args.dataset_id,
                            question=question, from_task=args.from_task)
        print("状态：" + record["status"])
        print("English\n" + record["answers"]["english"])
        print("中文\n" + record["answers"]["chinese"])
        if record.get("output_redacted") is True:
            print(core.output_redaction_message(record))
        print(f"工具调用次数：{record['tool_call_count']}")
        print(completion_summary(record))
        print(record["notice"])
        return 1 if record["status"] in {"拒绝", "出错"} else 0
    except ExpertReviewError as exc:
        print(f"拒绝 / 出错：{exc.message}（{exc.code}）", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, ValueError):
        print("出错：无法读取或写入本机文件，请检查参数和权限。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断；已提交的问题不能重跑。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
