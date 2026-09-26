"""Bounded, strict experiment documents; no Key or claim access."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re

from researchops_internal_telemetry import source_integrity_v3 as source
from researchops_external_closure.io import read_regular_file_no_follow, scan_public_artifact_bytes

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = "evals/item6_experiment_bridge_v1"
SCOPE = "item6_controlled_experiment_v1"
BASE = source.HISTORICAL_COMMIT
SCORER = "fcc2026c60943de6016495ad244291689a9d491d"
SERVICE = "services/agent_workflow_comparison_v1/controlled_comparison_v1"
TASKS = SERVICE + "/tasks/frozen/tasks.json"
EVIDENCE = SERVICE + "/tasks/frozen/evidence.json"
GOLD = SERVICE + "/scoring/contracts.json"
OUTPUT = "output/item6-experiment-bridge-v1"
SHA = re.compile(r"(?!0{64}$)[0-9a-f]{64}")
SCORER_FILES = ("src/researchops_behavior_eval_v1/__init__.py", "src/researchops_behavior_eval_v1/core.py",
                "src/researchops_behavior_eval_v1/__main__.py", "evals/internal_behavior_eval_v1/CONTRACT.md",
                "evals/internal_behavior_eval_v1/text_observation_v1_1/REVISION.md")


class ExperimentError(ValueError):
    def __init__(self, code):
        self.code = "item6_" + code
        super().__init__(self.code)


def require(value, code):
    if not value: raise ExperimentError(code)


def raw(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data): return hashlib.sha256(data).hexdigest()
def commit(kind, value): return digest(b"researchops.item6.experiment.v1\0" + kind.encode() + b"\0" + raw(value))
def now(): return datetime.now(timezone.utc)


def utc(value):
    require(type(value) is str and value.endswith("Z") and len(value) <= 27, "utc")
    try: result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError: raise ExperimentError("utc") from None
    require(result.tzinfo is not None, "utc")
    return result


def decode(data, maximum=1048576):
    require(type(data) is bytes and len(data) <= maximum, "document_bytes")
    def unique(pairs):
        value = {}
        for k, v in pairs:
            require(k not in value, "duplicate_key"); value[k] = v
        return value
    try: value = json.loads(data, object_pairs_hook=unique, parse_constant=lambda _: require(False, "nonfinite"))
    except (UnicodeError, json.JSONDecodeError): raise ExperimentError("json") from None
    require(type(value) is dict, "document_type")
    return value


def read(name, maximum=1048576):
    source.v2._relative(name)
    return source.v2._regular(ROOT, name, maximum)


def policy(): return decode(read(DIRECTORY + "/policy_v1.json"))


def model_handles(mode, tasks_sha256, task_ids):
    return {task_id: "PCECASE-" + commit("model-case", {"scope": SCOPE, "mode": mode,
            "tasks_sha256": tasks_sha256, "task_id": task_id})[:32].upper() for task_id in task_ids}


def schema(value, name):
    from jsonschema import Draft202012Validator
    document = decode(read(DIRECTORY + "/" + name + ".schema.json"))
    pending = [document]
    while pending:
        item = pending.pop()
        if type(item) is dict:
            require(not any(k in item for k in ("$ref", "$dynamicRef", "$recursiveRef")), "schema_reference")
            pending.extend(item.values())
        elif type(item) is list: pending.extend(item)
    Draft202012Validator.check_schema(document)
    require(not list(Draft202012Validator(document).iter_errors(value)), name + "_schema")
    return deepcopy(value)


def exact(value, names):
    require(type(value) is dict and set(value) == set(names.split()), "fields")


def check_source(freeze):
    require(digest(read(source.MANIFEST, source.v2.LIMITS["file_bytes"])) == freeze["source_manifest_sha256"], "source_manifest_drift")
    checked = source.verify_source(ROOT, expected=freeze["source_commitment_sha256"])
    require(digest(read(TASKS)) == freeze["tasks_sha256"] and digest(read(EVIDENCE)) == freeze["evidence_sha256"]
            and digest(read(GOLD)) == freeze["scoring_contract_sha256"], "input_drift")
    require(policy() == freeze["policy"], "policy_drift")
    for name, expected in freeze["workflow_sha256"].items():
        require(name in source.SEPARATE_GIT_BINDINGS and digest(read(name)) == expected, "workflow_drift")
    require(set(freeze["workflow_sha256"]) == set(source.SEPARATE_GIT_BINDINGS), "workflow_scope")
    return checked


def check_execution(freeze):
    require(type(freeze["execution_commit"]) is str and re.fullmatch(r"[0-9a-f]{40}", freeze["execution_commit"]), "execution_uncommitted")
    head = source.v2._git(ROOT, "rev-parse", "HEAD").decode().strip()
    require(head == freeze["execution_commit"], "execution_head_drift")
    require(source.v2._git(ROOT, "rev-parse", head + "^{tree}").decode().strip() == freeze["execution_tree"], "execution_tree_drift")
    source.v2._git(ROOT, "merge-base", "--is-ancestor", BASE, head)
    selected = source.verify_source(ROOT, expected=freeze["source_commitment_sha256"])
    names = [r["path"] for r in selected["files"]] + [source.MANIFEST, *source.SEPARATE_GIT_BINDINGS]
    # Actual committed blobs, never a caller's boolean about dirty state.
    for name, blob in zip(names, source.committed_blobs(ROOT, head, names)):
        require(blob == read(name, source.v2.LIMITS["file_bytes"]), "uncommitted_source")
    require(freeze["scorer_commit"] == SCORER, "scorer_identity")
    for name in SCORER_FILES:
        require(source.v2._git(ROOT, "show", SCORER + ":" + name) == read(name), "scorer_drift")
    locked = dict(line.split("==", 1) for line in read("requirements.lock").decode().splitlines() if "==" in line and not line.startswith("#"))
    for name in ("openai-agents", "openai", "httpx2", "pydantic", "pydantic_core", "jsonschema"):
        require(importlib.metadata.version(name) == locked[name], "dependency_version")


def validate_freeze(value):
    value = schema(value, "freeze")
    require(value["scope"] == SCOPE and value["policy"] == policy(), "freeze_policy")
    require(value["mode"] in ("offline_test", "live"), "mode")
    tasks = decode(read(TASKS))["tasks"]
    order = [{"task_id": t["task_id"], "path_kind": p} for i, t in enumerate(tasks)
             for p in (("fixed_workflow", "agent") if i % 2 == 0 else ("agent", "fixed_workflow"))]
    require(value["business_plan"] == order and value["model_case_ids"] == [t["task_id"] for t in tasks], "plan")
    require(value["model_case_handles"] == model_handles(value["mode"], value["tasks_sha256"], value["model_case_ids"]), "model_case_handles")
    require(len(tasks) == 16 and len(order) == 32, "budget_plan")
    require(set(value["budget"]) == set(value["policy"]["limits"]) and all(
        type(v) is int and 0 < v <= value["policy"]["limits"][k] for k, v in value["budget"].items()), "budget_plan")
    pricing = value["pricing"]
    require(pricing["kind"] == ("synthetic_fixture" if value["mode"] == "offline_test" else "user_reviewed_official_snapshot"), "pricing_mode")
    for key in ("input_per_million", "output_per_million", "cost_limit"):
        require(type(pricing[key]) is str and re.fullmatch(r"[0-9]+(?:\.[0-9]{1,8})?", pricing[key]), "pricing_number")
        require(Decimal(pricing[key]).is_finite() and Decimal(pricing[key]) > 0, "pricing_number")
    if value["mode"] == "live":
        require(pricing["input_accounting"] == "observed_stop" and pricing["provider_bill_hard_cap"] is False,
                "native_input_bound_unverified")
        require(type(pricing["evidence_sha256"]) is str and SHA.fullmatch(pricing["evidence_sha256"]), "pricing_evidence")
    else:
        require(pricing["input_accounting"] == "synthetic_bytes" and pricing["evidence_sha256"] is None, "synthetic_pricing")
    return value


def validate_approval(value, freeze, approved_digest, *, at=None):
    value = schema(value, "approval")
    candidate = value["candidate"]
    require(type(approved_digest) is str and SHA.fullmatch(approved_digest)
            and commit("approval-candidate", candidate) == approved_digest == value["observation"]["approved_digest"], "approval_digest")
    require(candidate["mode"] == freeze["mode"] and candidate["scope"] == SCOPE
            and candidate["freeze_sha256"] == commit("freeze", freeze)
            and candidate["environment_id"] == freeze["environment_id"], "approval_binding")
    expected_kind = "synthetic_approval_fixture" if freeze["mode"] == "offline_test" else "explicit_user_confirmation"
    require(value["observation"]["kind"] == expected_kind, "approval_mode")
    instant = at or now()
    require(utc(value["observation"]["observed_at_utc"]) <= instant, "future_approval")
    require(utc(candidate["not_before_utc"]) <= instant < utc(candidate["expires_at_utc"]), "approval_window")
    return value


def safe_error(error):
    code = getattr(error, "code", None)
    if type(error).__module__.startswith(("researchops_item6_experiment_v1", "researchops_completion_", "researchops_internal_telemetry", "researchops.audit", "researchops.model_providers")):
        if type(code) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code): return code
    return "item6_execution_failed"


def build_freeze(*, mode, environment_id, pricing, budget=None):
    """Explicit candidate only: cannot manufacture an approval or consume a claim."""
    checked = source.verify_source(ROOT)
    tasks = decode(read(TASKS))["tasks"]
    head = source.v2._git(ROOT, "rev-parse", "HEAD").decode().strip()
    tracked = set(source.v2._git(ROOT, "ls-tree", "-r", "--name-only", head).decode().splitlines())
    names = [row["path"] for row in checked["files"]] + [source.MANIFEST, *source.SEPARATE_GIT_BINDINGS]
    committed = set(names) <= tracked and all(blob == read(name, source.v2.LIMITS["file_bytes"])
        for name, blob in zip(names, source.committed_blobs(ROOT, head, names)))
    candidate = dict(schema_version="item6-experiment-freeze/1.0", scope=SCOPE, mode=mode,
        execution_commit=head if committed else None,
        execution_tree=source.v2._git(ROOT, "rev-parse", head + "^{tree}").decode().strip() if committed else None,
        source_commitment_sha256=checked["commitment_sha256"], source_manifest_sha256=digest(read(source.MANIFEST)),
        workflow_sha256={name: digest(read(name)) for name in source.SEPARATE_GIT_BINDINGS}, scorer_commit=SCORER,
        tasks_sha256=digest(read(TASKS)), evidence_sha256=digest(read(EVIDENCE)), scoring_contract_sha256=digest(read(GOLD)),
        environment_id=environment_id, policy=policy(), budget=deepcopy(budget or policy()["limits"]), pricing=deepcopy(pricing),
        business_plan=[dict(task_id=t["task_id"], path_kind=p) for i, t in enumerate(tasks)
                       for p in (("fixed_workflow", "agent") if i % 2 == 0 else ("agent", "fixed_workflow"))],
        model_case_ids=[t["task_id"] for t in tasks],
        model_case_handles=model_handles(mode, digest(read(TASKS)), [t["task_id"] for t in tasks]))
    return validate_freeze(candidate)
