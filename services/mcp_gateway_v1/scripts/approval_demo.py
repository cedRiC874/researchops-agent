"""隔离的人工审批演示；prepare 和 finish 都不会代替操作者批准。"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = Path(__file__).resolve().parents[1]
for location in (PROJECT_ROOT / "src", SERVICE_ROOT / "src"):
    if str(location) not in sys.path:
        sys.path.insert(0, str(location))

from researchops.tool_runtime import ToolRuntimeError
from researchops_mcp_gateway.gateway import Gateway
from researchops_mcp_gateway.schemas import CALL_ID, RUN_ID

HANDLES_SCHEMA = "mcp-approval-demo/1.0"
RELEASE_NAME = "demo-reviewed-release"
DEFAULT_DEMO_DIRECTORY = SERVICE_ROOT / ".state/demo"


def _safe_error(code):
    return {"status": "error", "error_code": code}


def prepare(demo_dir: str | Path = DEFAULT_DEMO_DIRECTORY):
    """新目录中复制合成材料，返回读取统计和待审批句柄。"""
    root = Path(demo_dir).resolve()
    # 拒绝覆盖已存在目录，保留上一次演示及任何用户文件。
    root.mkdir(parents=True, exist_ok=False)
    for relative in ("data", "artifacts/phase3"):
        shutil.copytree(PROJECT_ROOT / relative, root / relative)
    gateway = Gateway(root, root / "state")
    begun = gateway.call_tool("begin_run", {})
    if begun["isError"]:
        return _safe_error(begun["structuredContent"]["error_code"])
    run_id = begun["structuredContent"]["run_id"]
    inspection = gateway.call_tool("inspect_dataset", {"run_id": run_id, "dataset_id": "synthetic_trial"})
    if inspection["isError"]:
        return _safe_error(inspection["structuredContent"]["error_code"])
    pending = gateway.call_tool("publish_aggregate_results", {
        "run_id": run_id, "bundle_id": "phase3", "release_name": RELEASE_NAME})
    if pending["isError"]:
        return _safe_error(pending["structuredContent"]["error_code"])
    data = pending["structuredContent"]
    if data.get("status") != "awaiting_approval":
        return _safe_error("demo_expected_pending_approval")
    handles = {"schema_version": HANDLES_SCHEMA, "run_id": run_id,
               "call_id": data["call_id"], "release_name": RELEASE_NAME}
    with (root / "handles.json").open("x", encoding="utf-8") as stream:
        json.dump(handles, stream, ensure_ascii=False, indent=2)
    return {"status": data["status"], "run_id": run_id, "call_id": data["call_id"],
            "row_count": inspection["structuredContent"]["row_count"], "release_name": RELEASE_NAME}


def _read_handles(root):
    handles = json.loads((root / "handles.json").read_text(encoding="utf-8"))
    if (not isinstance(handles, dict) or set(handles) != {"schema_version", "run_id", "call_id", "release_name"}
            or handles["schema_version"] != HANDLES_SCHEMA or handles["release_name"] != RELEASE_NAME
            or not isinstance(handles["run_id"], str) or not re.fullmatch(RUN_ID["pattern"], handles["run_id"])
            or not isinstance(handles["call_id"], str) or not re.fullmatch(CALL_ID["pattern"], handles["call_id"])):
        raise ValueError("demo_handles_invalid")
    return handles


def finish(demo_dir: str | Path = DEFAULT_DEMO_DIRECTORY):
    """只用保存的句柄执行，再验证额外参数被公开工具 schema 拒绝。"""
    root = Path(demo_dir).resolve()
    handles = _read_handles(root)
    gateway = Gateway(root, root / "state")
    arguments = {"run_id": handles["run_id"], "call_id": handles["call_id"]}
    executed = gateway.call_tool("execute_approved", arguments)
    common = {**arguments, "release_name": RELEASE_NAME}
    if executed["isError"]:
        return {**common, **_safe_error(executed["structuredContent"]["error_code"])}
    tampered = gateway.call_tool("execute_approved", {
        **arguments, "arguments": {"bundle_id": "phase3", "release_name": "tampered-release"}})
    tamper_code = tampered.get("structuredContent", {}).get("error_code")
    if not tampered.get("isError") or tamper_code != "tool_arguments_invalid":
        return {**common, **_safe_error("demo_tamper_not_rejected")}
    return {**common, "status": "succeeded", "execute_is_error": executed["isError"],
            "tamper_is_error": tampered["isError"], "tamper_error_code": tamper_code}


def main(argv=None):
    parser = argparse.ArgumentParser(description="合成数据审批演示：先 prepare，再在本地 CLI 人工批准，最后 finish。")
    parser.add_argument("command", choices=["prepare", "finish"], help="准备待审批调用，或执行已批准调用")
    parser.add_argument("--demo-dir", type=Path, default=DEFAULT_DEMO_DIRECTORY, help="隔离演示目录；prepare 必须使用新目录")
    args = parser.parse_args(argv)
    try:
        result = prepare(args.demo_dir) if args.command == "prepare" else finish(args.demo_dir)
    except FileExistsError:
        result = _safe_error("demo_directory_exists")
    except ToolRuntimeError as exc:
        result = _safe_error(exc.code)
    except Exception:
        result = _safe_error("demo_configuration_invalid")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
