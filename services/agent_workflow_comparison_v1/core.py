"""Read-only business components and lossless observations; no scoring code."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
VERSION = "workflow-agent-comparison-v1.0"
BASELINE = "551b9e252670be871ff75b0638b033b07d3c6f08"
REQUESTS = {"检查数据", "推荐方法", "发布结果", "伪造数据"}
STATES = {"completed", "clarification", "refusal", "waiting_approval", "failed", "unknown"}
CONFIG = {"version": VERSION, "max_tool_calls": 2, "max_model_requests": 3,
          "retry": 0, "resume": False, "fallback": False,
          "permissions": "read_only_synthetic_csv", "network": "deny",
          "tools": {"inspect_dataset": ["dataset_id"],
                    "recommend_statistical_method": ["dataset_id"]}}
DATASETS = {"sample": "sample.csv", "invalid": "invalid.csv"}


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def deny_network():
    """Permanent for this new local process; no change to other processes/OS."""
    def guard(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto",
                     "socket.sendmsg", "subprocess.Popen", "os.system"}:
            raise PermissionError("comparison offline process: network/child execution denied")
    sys.addaudithook(guard)


def public_task(task):
    if not isinstance(task, dict) or set(task) != {"task_id", "request", "dataset_id", "design"}:
        raise ValueError("task requires only task_id/request/dataset_id/design; no gold or labels")
    if not isinstance(task["task_id"], str) or not task["task_id"]:
        raise ValueError("task_id required")
    if not isinstance(task["request"], str):
        raise ValueError("request must be text")
    if task["dataset_id"] is not None and task["dataset_id"] not in DATASETS:
        raise ValueError("dataset outside synthetic allowlist")
    if task["design"] is not None:
        # Existing strict domain contract; no intent/method/answer labels accepted.
        from researchops.contracts import ResearchDesign
        ResearchDesign.from_dict(deepcopy(task["design"]))
    return deepcopy(task)


def context(task):
    task = public_task(task)
    return {"task": task, "config": deepcopy(CONFIG),
            "datasets": {k: file_hash(ROOT / "fixtures" / v) for k, v in DATASETS.items()},
            "instruction": "只支持检查数据或按显式设计推荐方法；缺条件澄清，禁止伪造和写入。"
                           "结果引用本次工具 evidence_id。不允许推断设计或实际发布。"}


def text_state(text):
    if text is None:
        return "unobserved"
    if not isinstance(text, str):
        raise ValueError("final_output must be string or null")
    return "empty" if text == "" else "whitespace" if text.isspace() else "nonempty"


def metric(value, availability, source, unit):
    return dict(value=value, availability=availability, source=source, unit=unit)


class Session:
    """Only this collector establishes actual tool events, never model self-report."""
    def __init__(self, task, path_kind, run_id):
        if path_kind not in {"fixed_workflow", "agent"}:
            raise ValueError("invalid path_kind")
        self.visible = context(task)
        self.task = self.visible["task"]
        self.profile = None
        self.record = {
            "schema_version": VERSION, "source_kind": "synthetic_fixture",
            "task_id": task["task_id"], "path_kind": path_kind, "run_id": run_id,
            "binding": {"baseline_commit": BASELINE, "input_sha256": digest(self.visible),
                        "config_sha256": digest(CONFIG), "data_sha256": self.visible["datasets"],
                        "component_sha256": {name: file_hash(REPO / "src/researchops" / name)
                            for name in ("contracts.py", "data_quality.py", "method_selection.py")}},
            "execution_state": "observed", "status": "unknown", "status_source": "collector",
            "completion": "unknown", "final_output": None, "text_observation": "unobserved",
            "plan": [], "events": [], "events_complete": True,
            "allowed_evidence": [], "approval_interruptions": [],
            "side_effects": [], "side_effects_complete": True,
            "side_effect_scope": "only calls through this read-only backend; excludes harness report files",
            "delivered_artifacts": [], "missing_observations": [], "known_failures": [],
            "model_requests": [], "model_response_origin": "not_applicable",
            "metrics": {}, "comparability": {"engineering_inputs_equal": None,
                "effectiveness_comparable": False,
                "reasons": ["scripted model is not evidence of planning ability",
                            "scorer integration commit not supplied"]},
        }

    def plan(self, tool, arguments, source):
        entry = {"plan_id": f"P{len(self.record['plan']) + 1}", "tool": tool,
                 "arguments": deepcopy(arguments), "source": source,
                 "execution": "not_executed", "call_id": None}
        self.record["plan"].append(entry)
        return entry

    def execute(self, tool, arguments):
        r = self.record
        call_id = f"C{len(r['events']) + 1}"
        event = {"call_id": call_id, "tool": tool, "arguments": deepcopy(arguments),
                 "status": "failed", "produced_artifacts": [], "result": None,
                 "result_origin": "actual_local_tool_on_synthetic_data"}
        r["events"].append(event)
        pending = next((p for p in r["plan"] if p["execution"] == "not_executed"
                        and p["tool"] == tool and p["arguments"] == arguments), None)
        if pending:
            pending.update(execution="executed", call_id=call_id)
        try:
            if len(r["events"]) > CONFIG["max_tool_calls"]:
                raise ValueError("tool_budget_exceeded")
            if r["known_failures"]:
                raise ValueError("stopped_after_failure")
            if tool not in CONFIG["tools"] or set(arguments) != {"dataset_id"}:
                raise ValueError("tool_or_arguments_not_allowed")
            if self.task["request"] not in {"检查数据", "推荐方法"}:
                raise ValueError("request_outside_read_scope")
            if self.task["request"] == "推荐方法" and self.task["design"] is None:
                raise ValueError("design_missing_no_tools")
            dataset_id = arguments["dataset_id"]
            if dataset_id is None or dataset_id != self.task["dataset_id"]:
                raise ValueError("dataset_scope_mismatch")
            path = ROOT / "fixtures" / DATASETS[dataset_id]
            if file_hash(path) != self.visible["datasets"][dataset_id]:
                raise ValueError("dataset_changed")
            if tool == "inspect_dataset":
                from researchops.data_quality import CsvProfiler
                self.profile = CsvProfiler().profile(path)
                result = {"dataset_id": dataset_id, "row_count": self.profile.row_count,
                          "column_count": self.profile.column_count,
                          "rows_with_missing": self.profile.rows_with_missing,
                          "warning_codes": [w.code for w in self.profile.warnings]}
            else:
                if self.profile is None or self.task["design"] is None:
                    raise ValueError("inspection_and_explicit_design_required")
                from researchops.contracts import ResearchDesign
                from researchops.method_selection import StatisticalMethodSelector
                recommendation = StatisticalMethodSelector().recommend(
                    self.profile, ResearchDesign.from_dict(deepcopy(self.task["design"])))
                result = {"dataset_id": dataset_id,
                          "method_code": recommendation.primary_method.code,
                          "rule_version": recommendation.rule_version,
                          "warnings": recommendation.warnings,
                          "assumptions_to_check": recommendation.assumptions_to_check,
                          "required_diagnostics": recommendation.required_diagnostics}
            evidence_id = f"E{len(r['allowed_evidence']) + 1}"
            result["evidence_id"] = evidence_id
            event.update(status="succeeded", produced_artifacts=[evidence_id], result=result)
            r["allowed_evidence"].append({"artifact_id": evidence_id, "run_id": r["run_id"],
                "call_id": call_id, "projection": deepcopy(result),
                "projection_sha256": digest(result), "availability": "observed",
                "storage": "in_memory_in_this_record", "facts": [],
                "mapping_limit": "counts/methods are outside scorer v1.1 fact vocabulary"})
        except Exception as exc:
            # Safe synthetic error: no raw exception, paths, rows or guessed success.
            event["result"] = {"error": type(exc).__name__, "code": str(exc)
                if type(exc) is ValueError else "existing_component_rejected_input"}
            r["known_failures"].append({"code": "tool_failed", "call_id": call_id})
        return deepcopy(event["result"])

    def finish(self, text, status, *, origin="fixed_rule", completion="complete"):
        if status not in STATES:
            raise ValueError("invalid terminal state")
        r = self.record
        r.update(final_output=text, text_observation=text_state(text), status=status,
                 status_source=origin, completion=completion)
        if completion in {"timeout", "truncated"}:
            r["known_failures"].append({"code": completion})
        if r["known_failures"]:
            r.update(status="failed", status_source="collector_known_failure")
        if text is None:
            r["missing_observations"].append("final_output")
        agent = r["path_kind"] == "agent"
        r["metrics"] = {
            "model_request_count": metric(len(r["model_requests"]), "observed",
                "local_fake_model_invocations" if agent else "structural_no_model_path", "request"),
            "provider_request_count": metric(0, "observed", "offline_network_denied", "request"),
            "tool_call_count": metric(len(r["events"]), "observed", "collector", "call"),
            "tokens": metric(None, "unavailable" if agent else "not_applicable",
                             "SDK placeholder usage deliberately discarded" if agent else "no_model", "token"),
            "api_cost": metric(None, "not_applicable", "no_provider_call", "currency_unspecified"),
            "api_latency": metric(None, "not_applicable", "no_provider_call", "second"),
            "human_time": metric(None, "unavailable", "not_recorded", "second"),
            "local_elapsed": metric(None, "unavailable", "recorded_only_by_cli_wall_clock", "second"),
        }
        return deepcopy(r)


def fixed_run(task, run_id):
    s = Session(task, "fixed_workflow", run_id)
    t = s.task
    if t["request"] == "伪造数据":
        return s.finish("不能伪造数据。", "refusal")
    if t["request"] == "发布结果":
        return s.finish("暂停：本离线范围不支持发布，尚未创建审批请求。", "waiting_approval")
    if t["request"] not in {"检查数据", "推荐方法"}:
        return s.finish("不支持此请求；只支持检查数据或推荐方法。", "refusal")
    if t["dataset_id"] is None or (t["request"] == "推荐方法" and t["design"] is None):
        return s.finish("请提供数据集及推荐方法所需的明确研究设计。", "clarification")
    args = {"dataset_id": t["dataset_id"]}
    s.plan("inspect_dataset", args, "fixed_rule")
    if t["request"] == "推荐方法":
        s.plan("recommend_statistical_method", args, "fixed_rule")
    p = s.execute("inspect_dataset", args)
    if s.record["known_failures"]:
        return s.finish("数据检查失败，停止后续工具。", "failed")
    text = f"数据集包含 {p['row_count']} 行、{p['column_count']} 列；含缺失的行数 {p['rows_with_missing']} [E1]。"
    if t["request"] == "推荐方法":
        p = s.execute("recommend_statistical_method", args)
        if s.record["known_failures"]:
            return s.finish("方法推荐工具失败，需检查输入；未生成方法结论。", "failed")
        text += f"\n候选方法 {p['method_code']} [E2]；尚未执行分析或验证统计假设。"
    return s.finish(text, "completed")


def apply_observation_fixture(record, fault):
    """Collector fault injection, never supplied to either path's decision logic."""
    r = deepcopy(record)
    if fault == "missing_final_output":
        r.update(final_output=None, text_observation="unobserved")
        r["missing_observations"].append("final_output")
    elif fault != "none":
        raise ValueError("unknown observation fixture")
    r["observation_fixture"] = {"source": "synthetic_fixture", "fault": fault}
    return r


def not_executed(task, path_kind, run_id):
    s = Session(task, path_kind, run_id)
    r = s.finish(None, "failed", completion="unknown")
    r.update(execution_state="not_executed", known_failures=[{"code": "not_executed"}])
    return r


def coverage(planned_ids, records):
    if not planned_ids or len(set(planned_ids)) != len(planned_ids):
        raise ValueError("unique nonempty planned list required")
    by_id = {}
    for r in records:
        if r["task_id"] not in planned_ids or r["task_id"] in by_id:
            raise ValueError("unexpected/duplicate observation")
        by_id[r["task_id"]] = r
    rows = [{"task_id": tid, "observation": deepcopy(by_id.get(tid))} for tid in planned_ids]
    counts = {"planned": len(rows), "observed": 0, "not_executed": 0, "observation_missing": 0}
    for row in rows:
        r = row["observation"]
        counts["observation_missing" if r is None else r["execution_state"]] += 1
    return {"counts": counts, "rows": rows, "score_status": "not_scored"}
