from copy import deepcopy
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re

from ..core import coverage, digest, file_hash, metric, text_state

ROOT = Path(__file__).resolve().parent
VERSION = "aggregate-read-comparison-v1.0"
TOOL = "read_aggregate_fixture"
BUNDLES = {"alpha": "alpha.json", "beta": "beta.json", "gamma": "damaged.json"}
CONFIG = {"version": VERSION, "tool": TOOL, "arguments": ["bundle_id"],
          "max_tool_executions": 1, "max_model_requests": 3,
          "retry": 0, "resume": False, "fallback": False,
          "permissions": "read_only_allowlisted_synthetic_aggregates", "network": "denied"}
INSTRUCTION = ("按用户指定设计及目录关联读取聚合结果，按指定对象、指标、单位说明并引用本次 evidence_id。"
               "不猜测缺失设计，缺设计先澄清；拒绝伪造。只允许读取合成聚合包，无发布、计算分析或审批权限。")


def load_json(path):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("duplicate_json_key")
            result[k] = v
        return result
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))


def public_context(task):
    fields = {"task_id", "request", "design_id", "subject", "metric", "output_unit"}
    if not isinstance(task, dict) or set(task) != fields:
        raise ValueError("public_task_fields_invalid_no_gold_allowed")
    if any(not isinstance(task[k], str) or not task[k] for k in fields - {"design_id"}):
        raise ValueError("public_task_string_required")
    if task["request"] not in {"read_aggregate", "fabricate"}:
        raise ValueError("unsupported_request")
    if task["metric"] not in {"mass", "mean", "difference"} or task["output_unit"] not in {"mg", "g"}:
        raise ValueError("unsupported_metric_or_unit")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", task["subject"]):
        raise ValueError("invalid_subject")
    catalog = load_json(ROOT / "fixtures/catalog.json")
    if set(catalog) != {"source_kind", "designs"} or catalog["source_kind"] != "synthetic_fixture":
        raise ValueError("catalog_structure_invalid")
    for entry in catalog["designs"].values():
        if set(entry) != {"label", "bundle_id"} or entry["bundle_id"] not in BUNDLES:
            raise ValueError("catalog_entry_invalid")
    design = task["design_id"]
    if design is not None and (not isinstance(design, str) or design not in catalog["designs"]):
        raise ValueError("unknown_design")
    return {"task": deepcopy(task), "catalog": catalog, "config": deepcopy(CONFIG),
            "instruction": INSTRUCTION,
            "bundle_sha256": {k: file_hash(ROOT / "fixtures" / v) for k, v in BUNDLES.items()}}


def validate_bundle(bundle, bundle_id):
    """Validate the tool's data format, not any answer or scorer judgment."""
    if set(bundle) != {"source_kind", "bundle_id", "source_record_id", "facts"} or (
        bundle["source_kind"] != "synthetic_fixture" or bundle["bundle_id"] != bundle_id
        or not isinstance(bundle["source_record_id"], str) or not bundle["source_record_id"]
        or not isinstance(bundle["facts"], list)
    ):
        raise ValueError("bundle_structure_invalid")
    seen = set()
    for f in bundle["facts"]:
        if not isinstance(f, dict) or set(f) != {"subject", "metric", "value", "unit"}:
            raise ValueError("fact_structure_invalid")
        if any(not isinstance(v, str) for v in f.values()):
            raise ValueError("fact_string_required")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", f["subject"]) or (
            f["metric"] not in {"mass", "mean", "difference"} or f["unit"] not in {"mg", "g"}
        ):
            raise ValueError("fact_vocabulary_invalid")
        if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", f["value"]):
            raise ValueError("fact_decimal_string_invalid")
        number = Decimal(f["value"])
        if not number.is_finite() or len(f["value"]) > 64 or abs(number.adjusted()) > 100:
            raise ValueError("fact_number_invalid")
        identity = (f["subject"], f["metric"])
        if identity in seen:
            raise ValueError("duplicate_fact")
        seen.add(identity)


def convert(value, source_unit, target_unit):
    if source_unit not in {"mg", "g"} or target_unit not in {"mg", "g"}:
        raise ValueError("unsupported_conversion")
    with localcontext() as ctx:
        ctx.prec = 512
        ctx.Emax, ctx.Emin = 999999, -999999
        number = Decimal(value)
        if not number.is_finite():
            raise ValueError("nonfinite_number")
        # Shift the decimal exponent, retaining the source coefficient/scale.
        exponent = {"mg": 0, "g": 3}
        return format(number.scaleb(exponent[source_unit] - exponent[target_unit]), "f")


class Session:
    def __init__(self, task, path_kind, run_id):
        if path_kind not in {"agent", "fixed_workflow"} or not isinstance(run_id, str) or not run_id:
            raise ValueError("run_identity_invalid")
        self.visible = public_context(task)
        self.task = self.visible["task"]
        self.record = {"schema_version": VERSION, "source_kind": "synthetic_fixture",
            "task_id": task["task_id"], "path_kind": path_kind, "run_id": run_id,
            "binding": {"input_sha256": digest(self.visible), "config_sha256": digest(CONFIG),
                        "bundle_sha256": self.visible["bundle_sha256"]},
            "execution_state": "observed", "status": "unknown", "status_source": "collector",
            "completion": "unknown", "final_output": None, "text_observation": "unobserved",
            "plan": [], "events": [], "events_complete": True, "allowed_evidence": [],
            "approval_interruptions": [], "side_effects": [], "side_effects_complete": True,
            "side_effect_scope": "this allowlisted read-only tool; excludes harness output files",
            "delivered_artifacts": [], "known_failures": [], "missing_observations": [],
            "model_requests": [], "model_response_origin": "not_applicable",
            "comparability": {"shared_business_context_sha256": digest(self.visible),
                "effectiveness_comparable": False,
                "limitations": ["scripted_model_response_not_planning_evidence",
                    "fixed_template_format_advantage_free_expression_may_be_unknown",
                    "no_confirmed_scorer_integration_commit"]}, "score_status": "not_scored"}

    def plan(self, tool, arguments, source):
        self.record["plan"].append({"plan_id": f"P{len(self.record['plan']) + 1}",
            "tool": tool, "arguments": deepcopy(arguments), "source": source,
            "execution": "not_executed", "call_id": None})

    def execute(self, bundle_id):
        r = self.record
        call_id = f"C{len(r['events']) + 1}"
        args = {"bundle_id": bundle_id}
        event = {"call_id": call_id, "tool": TOOL, "arguments": args, "status": "failed",
                 "result": None, "produced_artifacts": [],
                 "result_origin": "actual_local_read_of_synthetic_fixture"}
        r["events"].append(event)
        for p in r["plan"]:
            if p["execution"] == "not_executed" and p["tool"] == TOOL and p["arguments"] == args:
                p.update(execution="executed", call_id=call_id)
                break
        try:
            if len(r["events"]) > CONFIG["max_tool_executions"] or r["known_failures"]:
                raise ValueError("execution_budget_or_stop_boundary")
            if self.task["request"] != "read_aggregate" or self.task["design_id"] is None:
                raise ValueError("no_read_scope_before_clarification_or_refusal")
            if not isinstance(bundle_id, str) or bundle_id not in BUNDLES:
                raise ValueError("bundle_not_allowlisted")
            path = (ROOT / "fixtures" / BUNDLES[bundle_id]).resolve()
            if not path.is_relative_to((ROOT / "fixtures").resolve()):
                raise ValueError("bundle_path_escape")
            if file_hash(path) != self.visible["bundle_sha256"][bundle_id]:
                raise ValueError("bundle_changed_after_binding")
            bundle = load_json(path)
            validate_bundle(bundle, bundle_id)
            artifact = f"E{len(r['allowed_evidence']) + 1}"
            result = {**deepcopy(bundle), "evidence_id": artifact}
            event.update(status="succeeded", result=result, produced_artifacts=[artifact])
            r["allowed_evidence"].append({"artifact_id": artifact, "run_id": r["run_id"],
                "call_id": call_id, "facts": deepcopy(bundle["facts"]),
                "source_bundle_id": bundle_id, "source_record_id": bundle["source_record_id"],
                "source_sha256": self.visible["bundle_sha256"][bundle_id],
                "projection_sha256": digest(bundle["facts"]), "availability": "observed"})
        except Exception as exc:
            event["result"] = {"error": "aggregate_read_failed", "type": type(exc).__name__}
            r["known_failures"].append({"code": "tool_failed", "call_id": call_id,
                                        "attribution": "not_a_model_capability_verdict"})
        return deepcopy(event["result"])

    def finish(self, text, status, *, origin="fixed_rule", completion="complete"):
        if status not in {"completed", "clarification", "refusal", "failed", "unknown"}:
            raise ValueError("unsupported_terminal_state")
        if completion not in {"complete", "unknown", "timeout", "truncated", "suspected_truncation"}:
            raise ValueError("invalid_completion")
        r = self.record
        r.update(final_output=text, text_observation=text_state(text), status=status,
                 status_source=origin, completion=completion)
        if completion in {"timeout", "truncated"}:
            r["known_failures"].append({"code": completion})
        if r["known_failures"]:
            r.update(status="failed", status_source="collector_known_failure")
        if text is None:
            r["missing_observations"].append("final_output")
        is_agent = r["path_kind"] == "agent"
        r["metrics"] = {
            "model_request_count": metric(len(r["model_requests"]), "observed",
                "local_fake_model_invocations" if is_agent else "structural_no_model_path", "request"),
            "provider_request_count": metric(0, "observed", "offline_execution", "request"),
            "tool_attempt_count": metric(len(r["events"]), "observed", "collector", "attempt"),
            "tokens": metric(None, "unavailable" if is_agent else "not_applicable",
                             "SDK_placeholder_usage_discarded" if is_agent else "no_model", "token"),
            "api_cost": metric(None, "not_applicable", "no_provider_call", "unspecified_currency"),
            "api_latency": metric(None, "not_applicable", "no_provider_call", "second"),
            "human_time": metric(None, "unavailable", "not_recorded", "second"),
            "local_elapsed": metric(None, "unavailable", "CLI_only", "second")}
        return deepcopy(r)


def fixed_run(task, run_id):
    s = Session(task, "fixed_workflow", run_id)
    if task["request"] == "fabricate":
        return s.finish("不能伪造数据。", "refusal")
    if task["design_id"] is None:
        return s.finish("请指定分析设计。", "clarification")
    bundle_id = s.visible["catalog"]["designs"][task["design_id"]]["bundle_id"]
    s.plan(TOOL, {"bundle_id": bundle_id}, "fixed_rule")
    result = s.execute(bundle_id)
    if s.record["known_failures"]:
        return s.finish("聚合证据读取失败，已停止，未补造结果。", "failed")
    fact = next((f for f in result["facts"] if f["subject"] == task["subject"]
                 and f["metric"] == task["metric"]), None)
    if fact is None:
        s.record["known_failures"].append({"code": "requested_fact_not_available"})
        return s.finish("未取得指定对象和指标的聚合结果，已停止。", "failed")
    value = convert(fact["value"], fact["unit"], task["output_unit"])
    word = {"mass": "质量", "mean": "均值", "difference": "差值"}[fact["metric"]]
    return s.finish(f"{fact['subject']}的{word}为{value} {task['output_unit']} [{result['evidence_id']}]。", "completed")


def not_executed(task, path_kind, run_id):
    s = Session(task, path_kind, run_id)
    s.record["known_failures"].append({"code": "not_executed"})
    r = s.finish(None, "failed", completion="unknown")
    r["execution_state"] = "not_executed"
    return r
