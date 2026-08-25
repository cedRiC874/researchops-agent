from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import statistics
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .eval_v2_contracts import EvalV2ContractError
from .eval_v2_inspect_backend import EvalV2InspectDatasetBackend
from .eval_v2_provider_executor import EvalV2ProviderExecutor
from .eval_v2_public import (
    EvalV2PublicTask,
    load_eval_v2_dataset_manifest,
    load_eval_v2_public_tasks,
)
from .eval_v2_runner import EvalV2TaskExecutor, run_eval_v2_evaluation
from .model_providers import (
    ANTHROPIC_GENERIC_ONLINE_DISABLED_CODE,
    ProviderAdapter,
)


SELF_PILOT_SCHEMA_VERSION = "1.1"
_SUPPORTED_SELF_PILOT_SCHEMA_VERSIONS = frozenset({"1.0", "1.1"})
_SESSION_INSTANCE_ID = re.compile(r"^SELF-PILOT-RUN-[A-F0-9]{32}$")
_PILOT_PACK_ID = re.compile(r"^PILOT-PACK-[A-F0-9]{12}$")
DEFAULT_TASK_COUNT = 12
_SAFE_SCENARIOS = frozenset(
    {
        "standard_analysis",
        "clarification_required",
        "safe_refusal",
        "unauthorized_resource",
        "prompt_injection",
        "duplicate_tool_call",
        "approval_pause",
    }
)
_DEFAULT_SCENARIO_PLAN = (
    "standard_analysis",
    "standard_analysis",
    "standard_analysis",
    "clarification_required",
    "clarification_required",
    "safe_refusal",
    "safe_refusal",
    "unauthorized_resource",
    "prompt_injection",
    "duplicate_tool_call",
    "approval_pause",
    "standard_analysis",
)
_WINDOWS_PATH = re.compile(r"[A-Za-z]:[\\/]")
_UNIX_PATH = re.compile(r"(?:^|\s)/(?:Users|home|tmp|var|etc)/")
_SECRET = re.compile(r"\b(?:sk-|Bearer\s+)[A-Za-z0-9._-]+", re.IGNORECASE)
_SUBJECT_KEY = re.compile(r"\bSUBJ-[A-F0-9]{8,}\b")
_USABILITY_CONFIDENCE = frozenset({"low", "medium", "high"})


def create_self_pilot_session(
    *,
    project_root: str | Path,
    output_directory: str | Path,
    tasks_path: str | Path,
    dataset_manifest_path: str | Path,
    task_count: int = DEFAULT_TASK_COUNT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    output = _validate_session_directory(root, Path(output_directory))
    if output.exists():
        raise EvalV2ContractError(
            "self_pilot_output_exists", "Self-pilot session 目录已存在；不会覆盖。"
        )
    if isinstance(task_count, bool) or not isinstance(task_count, int) or not 1 <= task_count <= 30:
        raise EvalV2ContractError(
            "self_pilot_task_count_invalid", "task_count 必须在 1 到 30 之间。"
        )
    task_source = Path(tasks_path).resolve()
    manifest = load_eval_v2_dataset_manifest(dataset_manifest_path)
    tasks = load_eval_v2_public_tasks(task_source, manifest)
    selected = _select_pilot_tasks(tasks, task_count)
    corpus_sha256 = _sha256_file(task_source)
    selected_ids = [task.task_id for task in selected]
    pack_digest = hashlib.sha256(
        (corpus_sha256 + "\0" + "\0".join(selected_ids)).encode("utf-8")
    ).hexdigest()[:12].upper()
    pilot_pack_id = "PILOT-PACK-" + pack_digest
    session_instance_id = "SELF-PILOT-RUN-" + secrets.token_hex(16).upper()
    session_id = session_instance_id

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".self-pilot-", dir=output.parent)
    ).resolve()
    try:
        blinded = {
            "schema_version": SELF_PILOT_SCHEMA_VERSION,
            "session_id": session_id,
            "session_instance_id": session_instance_id,
            "pilot_pack_id": pilot_pack_id,
            "task_count": len(selected),
            "goldens_included": False,
            "tasks": [
                {"sequence": index, **task.public_input()}
                for index, task in enumerate(selected, start=1)
            ],
        }
        state = {
            "schema_version": SELF_PILOT_SCHEMA_VERSION,
            "session_id": session_id,
            "session_instance_id": session_instance_id,
            "pilot_pack_id": pilot_pack_id,
            "session_type": "internal_self_pilot",
            "external_pilot": False,
            "created_at_utc": _now_utc(),
            "public_corpus_sha256": corpus_sha256,
            "blinded_tasks_sha256": None,
            "selected_task_ids": selected_ids,
            "records": [
                {
                    "task_id": task_id,
                    "provider_run": None,
                    "human_feedback": None,
                }
                for task_id in selected_ids
            ],
        }
        blinded_path = staging / "pilot_tasks.json"
        _write_json(blinded_path, blinded)
        state["blinded_tasks_sha256"] = _sha256_file(blinded_path)
        _write_json(staging / "pilot_state.json", state)
        (staging / "README.md").write_text(
            _render_session_readme(
                session_instance_id, pilot_pack_id, len(selected)
            ),
            encoding="utf-8",
        )
        _assert_blinded_pack(staging / "pilot_tasks.json")
        staging.replace(output)
        return {
            "status": "created",
            "session_id": session_id,
            "session_instance_id": session_instance_id,
            "pilot_pack_id": pilot_pack_id,
            "task_count": len(selected),
            "output_directory": output.relative_to(root).as_posix(),
            "goldens_included": False,
            "next_command": (
                "python -m researchops.cli self-pilot-next "
                f"--session-dir {output.relative_to(root).as_posix()}"
            ),
        }
    except Exception:
        if staging.exists():
            import shutil

            shutil.rmtree(staging)
        raise


def get_next_self_pilot_task(
    *, project_root: str | Path, session_directory: str | Path
) -> dict[str, Any]:
    session = _load_session(project_root, session_directory)
    blinded = _load_json(session / "pilot_tasks.json")
    state = _load_json(session / "pilot_state.json")
    records = {item["task_id"]: item for item in state["records"]}
    for task in blinded["tasks"]:
        record = records[task["task_id"]]
        if record["provider_run"] is None:
            return {
                "status": "pending_provider_run",
                "sequence": task["sequence"],
                "task": {
                    "task_id": task["task_id"],
                    "prompt": task["prompt"],
                    "context": task["context"],
                },
            }
        if record["human_feedback"] is None:
            return {
                "status": "pending_human_feedback",
                "sequence": task["sequence"],
                "task_id": task["task_id"],
                "message": "请先记录该题人工反馈，再查看下一题。",
            }
    return {"status": "complete", "message": "所有 self-pilot 任务和反馈均已完成。"}


def get_self_pilot_progress(
    *, project_root: str | Path, session_directory: str | Path
) -> dict[str, int]:
    """Return non-sensitive progress counters without writing a summary artifact."""

    session = _load_session(project_root, session_directory)
    state = _load_json(session / "pilot_state.json")
    records = list(state["records"])
    return {
        "task_count": len(records),
        "provider_run_count": sum(item["provider_run"] is not None for item in records),
        "feedback_completed_count": sum(
            item["human_feedback"] is not None for item in records
        ),
    }


def get_self_pilot_blinded_tasks(
    *, project_root: str | Path, session_directory: str | Path
) -> tuple[dict[str, Any], ...]:
    """Return defensive copies of the session's public inputs, never goldens."""

    session = _load_session(project_root, session_directory)
    blinded = _load_json(session / "pilot_tasks.json")
    return tuple(
        {
            "sequence": item["sequence"],
            "task_id": item["task_id"],
            "prompt": item["prompt"],
            "context": dict(item["context"]),
        }
        for item in blinded["tasks"]
    )


def get_self_pilot_provider_binding(
    *, project_root: str | Path, session_directory: str | Path
) -> dict[str, str] | None:
    """Return the one provider/model identity already used by this session."""

    session = _load_session(project_root, session_directory)
    state = _load_json(session / "pilot_state.json")
    bindings = {
        (
            item["provider_run"]["provider_id"],
            item["provider_run"]["model_id"],
            item["provider_run"]["transport_id"],
        )
        for item in state["records"]
        if item["provider_run"] is not None
    }
    if not bindings:
        return None
    if len(bindings) != 1:
        raise EvalV2ContractError(
            "self_pilot_provider_binding_mixed",
            "当前 session 已混用多个 Provider/model，不能由 Web 继续。",
        )
    provider_id, model_id, transport_id = next(iter(bindings))
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "transport_id": transport_id,
    }


def run_self_pilot_task(
    *,
    project_root: str | Path,
    session_directory: str | Path,
    tasks_path: str | Path,
    dataset_manifest_path: str | Path,
    registry_path: str | Path,
    provider: ProviderAdapter,
    model_id: str,
    api_key: str,
    task_id: str | None,
    confirm_online: bool,
    max_turns: int = 8,
    run_timeout_seconds: float = 120.0,
    sdk_runner: Any | None = None,
    bilingual_output: bool = False,
    max_output_tokens: int = 2000,
) -> dict[str, Any]:
    if provider.provider_id == "anthropic":
        raise EvalV2ContractError(
            ANTHROPIC_GENERIC_ONLINE_DISABLED_CODE,
            "Generic self-pilot Anthropic 入口未获受控 pilot 授权；Models preflight receipt 不授权运行。",
        )
    session = _load_session(project_root, session_directory)
    state_path = session / "pilot_state.json"
    state = _load_json(state_path)
    task_source = Path(tasks_path).resolve()
    if _sha256_file(task_source) != state["public_corpus_sha256"]:
        raise EvalV2ContractError(
            "self_pilot_corpus_changed", "Public corpus 已变化；当前 session 必须重新创建。"
        )
    manifest = load_eval_v2_dataset_manifest(dataset_manifest_path)
    all_tasks = load_eval_v2_public_tasks(task_source, manifest)
    tasks_by_id = {task.task_id: task for task in all_tasks}
    selected_ids = list(state["selected_task_ids"])
    selected_task_id = task_id or _next_unrun_task_id(state)
    if selected_task_id is None:
        raise EvalV2ContractError(
            "self_pilot_no_pending_task", "没有待运行的 self-pilot 任务。"
        )
    if selected_task_id not in selected_ids or selected_task_id not in tasks_by_id:
        raise EvalV2ContractError(
            "self_pilot_task_not_selected", "task_id 不属于当前 self-pilot session。"
        )
    record = _record_for(state, selected_task_id)
    if record["provider_run"] is not None:
        raise EvalV2ContractError(
            "self_pilot_task_already_run", "该题已运行；self-pilot 不允许覆盖或重跑。"
        )
    backend = EvalV2InspectDatasetBackend.from_registry_path(registry_path)
    executor = EvalV2ProviderExecutor(
        provider=provider,
        model_id=model_id,
        api_key=api_key,
        confirm_online=confirm_online,
        sdk_runner=sdk_runner,
        max_turns=max_turns,
        run_timeout_seconds=run_timeout_seconds,
        bilingual_output=bilingual_output,
        max_output_tokens=max_output_tokens,
    )
    capture = _CapturingExecutor(executor)
    report = run_eval_v2_evaluation(
        [tasks_by_id[selected_task_id]],
        executor=capture,
        inspect_backend=backend,
        evaluation_mode="provider_online",
        repetition_index=1,
    )
    result = capture.result
    if result is None:
        raise EvalV2ContractError(
            "self_pilot_provider_result_missing", "Provider executor 未返回结果。"
        )
    safe_output, output_redacted = _safe_display_output(result.final_output)
    task_score = report["task_scores"][0]
    machine_pass = bool(task_score["passed"]) and not output_redacted
    record["provider_run"] = {
        "completed_at_utc": _now_utc(),
        "provider_id": result.provider_id,
        "model_id": result.model_id,
        "transport_id": result.transport_id,
        "outcome": result.outcome,
        "machine_pass": machine_pass,
        "machine_failures": (
            list(task_score["failures"])
            + (["pilot_output_safety_filter"] if output_redacted else [])
        ),
        "latency_ms": report["p50_latency_ms"],
        "model_call_count": result.model_call_count,
        "model_requested_tool_call_count": result.model_requested_tool_call_count,
        "model_requested_tool_call_count_source": (
            result.model_requested_tool_call_count_source
        ),
        "deduplicated_tool_call_count": result.deduplicated_tool_call_count,
        "gateway_dispatched_tool_call_count": (
            result.gateway_dispatched_tool_call_count
        ),
        "backend_executed_tool_call_count": (
            result.backend_executed_tool_call_count
        ),
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "response_sha256": hashlib.sha256(
            result.final_output.encode("utf-8")
        ).hexdigest(),
        "output_redacted": output_redacted,
    }
    _atomic_write_json(state_path, state)
    return {
        "status": "ran",
        "task_id": selected_task_id,
        "agent_output": safe_output,
        "outcome": result.outcome,
        "provider": {
            "provider_id": result.provider_id,
            "model_id": result.model_id,
            "transport_id": result.transport_id,
        },
        "output_redacted": output_redacted,
        "machine_score_hidden_until_feedback": True,
        "next_command": (
            "python -m researchops.cli self-pilot-record "
            f"--session-dir {session.relative_to(Path(project_root).resolve()).as_posix()} "
            f"--task-id {selected_task_id} ..."
        ),
    }


def record_self_pilot_feedback(
    *,
    project_root: str | Path,
    session_directory: str | Path,
    task_id: str,
    accepted: bool,
    first_pass: bool,
    manual_revisions: int,
    duration_seconds: float,
    critical_error: bool,
    safety_concern: bool,
    clarification_useful: bool | None,
    notes: str | None = None,
) -> dict[str, Any]:
    session = _load_session(project_root, session_directory)
    state_path = session / "pilot_state.json"
    state = _load_json(state_path)
    if task_id not in state["selected_task_ids"]:
        raise EvalV2ContractError(
            "self_pilot_task_not_selected", "task_id 不属于当前 self-pilot session。"
        )
    record = _record_for(state, task_id)
    if record["provider_run"] is None:
        raise EvalV2ContractError(
            "self_pilot_provider_run_missing", "必须先运行 Provider，再记录人工反馈。"
        )
    if record["human_feedback"] is not None:
        raise EvalV2ContractError(
            "self_pilot_feedback_exists", "该题反馈已存在；不会覆盖。"
        )
    for name, value in (
        ("accepted", accepted),
        ("first_pass", first_pass),
        ("critical_error", critical_error),
        ("safety_concern", safety_concern),
    ):
        if not isinstance(value, bool):
            raise EvalV2ContractError(
                "self_pilot_feedback_invalid", f"{name} 必须是布尔值。"
            )
    if clarification_useful is not None and not isinstance(
        clarification_useful, bool
    ):
        raise EvalV2ContractError(
            "self_pilot_feedback_invalid",
            "clarification_useful 必须是布尔值或 null。",
        )
    if (
        isinstance(manual_revisions, bool)
        or not isinstance(manual_revisions, int)
        or manual_revisions < 0
    ):
        raise EvalV2ContractError(
            "self_pilot_feedback_invalid", "manual_revisions 必须是非负整数。"
        )
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not math.isfinite(float(duration_seconds))
        or duration_seconds <= 0
    ):
        raise EvalV2ContractError(
            "self_pilot_feedback_invalid", "duration_seconds 必须是正数。"
        )
    normalized_notes = None
    if notes is not None:
        if not isinstance(notes, str) or len(notes) > 2000:
            raise EvalV2ContractError(
                "self_pilot_feedback_invalid", "notes 必须是不超过 2000 字符的文本。"
            )
        normalized_notes = notes.strip() or None
        if normalized_notes and (
            _SECRET.search(normalized_notes) or _WINDOWS_PATH.search(normalized_notes)
            or _UNIX_PATH.search(normalized_notes)
            or _SUBJECT_KEY.search(normalized_notes)
        ):
            raise EvalV2ContractError(
                "self_pilot_feedback_sensitive", "notes 不得包含凭据或绝对路径。"
            )
    record["human_feedback"] = {
        "feedback_schema": "legacy_acceptance_v1",
        "evaluator_role": "unspecified",
        "recorded_at_utc": _now_utc(),
        "accepted": accepted,
        "first_pass": first_pass,
        "manual_revisions": manual_revisions,
        "duration_seconds": float(duration_seconds),
        "critical_error": critical_error,
        "safety_concern": safety_concern,
        "clarification_useful": clarification_useful,
        "notes": normalized_notes,
    }
    _atomic_write_json(state_path, state)
    return {
        "status": "recorded",
        "task_id": task_id,
        "machine_pass": record["provider_run"]["machine_pass"],
        "machine_failures": list(record["provider_run"]["machine_failures"]),
        "human_accepted": accepted,
        "next": get_next_self_pilot_task(
            project_root=project_root, session_directory=session
        ),
    }


def record_self_pilot_usability_feedback(
    *,
    project_root: str | Path,
    session_directory: str | Path,
    task_id: str,
    understandable: bool,
    useful: bool,
    confidence: str,
    needs_expert_review: bool,
    obvious_problem: bool,
    missing_information: bool,
    safety_concern: bool,
    clarification_useful: bool | None,
    duration_seconds: float,
    notes: str | None = None,
) -> dict[str, Any]:
    """Record non-expert usability feedback without claiming domain correctness."""

    session = _load_session(project_root, session_directory)
    state_path = session / "pilot_state.json"
    state = _load_json(state_path)
    if task_id not in state["selected_task_ids"]:
        raise EvalV2ContractError(
            "self_pilot_task_not_selected", "task_id 不属于当前 self-pilot session。"
        )
    record = _record_for(state, task_id)
    if record["provider_run"] is None:
        raise EvalV2ContractError(
            "self_pilot_provider_run_missing", "必须先运行 Provider，再记录人工反馈。"
        )
    if record["human_feedback"] is not None:
        raise EvalV2ContractError(
            "self_pilot_feedback_exists", "该题反馈已存在；不会覆盖。"
        )
    for name, value in (
        ("understandable", understandable),
        ("useful", useful),
        ("needs_expert_review", needs_expert_review),
        ("obvious_problem", obvious_problem),
        ("missing_information", missing_information),
        ("safety_concern", safety_concern),
    ):
        if not isinstance(value, bool):
            raise EvalV2ContractError(
                "self_pilot_feedback_invalid", f"{name} 必须是布尔值。"
            )
    if confidence not in _USABILITY_CONFIDENCE:
        raise EvalV2ContractError(
            "self_pilot_feedback_invalid",
            "confidence 必须是 low、medium 或 high。",
        )
    if clarification_useful is not None and not isinstance(
        clarification_useful, bool
    ):
        raise EvalV2ContractError(
            "self_pilot_feedback_invalid",
            "clarification_useful 必须是布尔值或 null。",
        )
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not math.isfinite(float(duration_seconds))
        or duration_seconds <= 0
    ):
        raise EvalV2ContractError(
            "self_pilot_feedback_invalid", "duration_seconds 必须是正数。"
        )
    normalized_notes = _normalize_feedback_notes(notes)
    record["human_feedback"] = {
        "feedback_schema": "non_expert_usability_v2",
        "evaluator_role": "non_domain_expert",
        "professional_correctness_assessed": False,
        "recorded_at_utc": _now_utc(),
        "understandable": understandable,
        "useful": useful,
        "confidence": confidence,
        "needs_expert_review": needs_expert_review,
        "obvious_problem": obvious_problem,
        "missing_information": missing_information,
        "safety_concern": safety_concern,
        "clarification_useful": clarification_useful,
        "duration_seconds": float(duration_seconds),
        "notes": normalized_notes,
    }
    _atomic_write_json(state_path, state)
    return {
        "status": "recorded",
        "task_id": task_id,
        "feedback_schema": "non_expert_usability_v2",
        "machine_pass": record["provider_run"]["machine_pass"],
        "machine_failures": list(record["provider_run"]["machine_failures"]),
        "understandable": understandable,
        "useful": useful,
        "needs_expert_review": needs_expert_review,
        "missing_information": missing_information,
        "professional_correctness_assessed": False,
        "next": get_next_self_pilot_task(
            project_root=project_root, session_directory=session
        ),
    }


def summarize_self_pilot(
    *, project_root: str | Path, session_directory: str | Path
) -> dict[str, Any]:
    session = _load_session(project_root, session_directory)
    state = _load_json(session / "pilot_state.json")
    records = list(state["records"])
    provider_records = [item["provider_run"] for item in records if item["provider_run"]]
    feedback_records = [item["human_feedback"] for item in records if item["human_feedback"]]
    usability_records = [
        item
        for item in feedback_records
        if item.get("feedback_schema") in {
            "non_expert_usability_v1",
            "non_expert_usability_v2",
        }
    ]
    legacy_records = [item for item in feedback_records if item not in usability_records]
    clarification_records = [
        item for item in usability_records if item.get("clarification_useful") is not None
    ]
    missing_information_records = [
        item for item in usability_records if "missing_information" in item
    ]
    durations = [float(item["duration_seconds"]) for item in feedback_records]
    schema_counts = Counter(
        item.get("feedback_schema", "legacy_acceptance_v1_unversioned")
        for item in feedback_records
    )
    confidence_counts = {
        level: sum(item["confidence"] == level for item in usability_records)
        for level in ("low", "medium", "high")
    }
    identity = _session_identity(state)
    summary = {
        "schema_version": state["schema_version"],
        "summary_schema_version": "1.1",
        "feedback_contract": "role_aware_v1",
        "session_id": state["session_id"],
        "session_instance_id": identity["session_instance_id"],
        "session_instance_id_source": identity["session_instance_id_source"],
        "pilot_pack_id": identity["pilot_pack_id"],
        "session_type": "internal_self_pilot",
        "external_pilot": False,
        "status": (
            "complete" if len(feedback_records) == len(records) else "in_progress"
        ),
        "task_count": len(records),
        "provider_run_count": len(provider_records),
        "feedback_completed_count": len(feedback_records),
        "machine_pass_count": sum(item["machine_pass"] for item in provider_records),
        "machine_pass_rate": _rate(
            sum(item["machine_pass"] for item in provider_records),
            len(provider_records),
        ),
        "feedback_schema_counts": dict(sorted(schema_counts.items())),
        "legacy_feedback_count": len(legacy_records),
        "non_expert_usability_feedback_count": len(usability_records),
        "human_accepted_count": sum(item["accepted"] for item in legacy_records),
        "human_acceptance_rate": _rate(
            sum(item["accepted"] for item in legacy_records),
            len(legacy_records),
        ),
        "first_pass_count": sum(item["first_pass"] for item in legacy_records),
        "first_pass_rate": _rate(
            sum(item["first_pass"] for item in legacy_records),
            len(legacy_records),
        ),
        "manual_revision_count": sum(
            int(item["manual_revisions"]) for item in legacy_records
        ),
        "critical_error_count": sum(item["critical_error"] for item in legacy_records),
        "understandable_count": sum(
            item["understandable"] for item in usability_records
        ),
        "understandable_rate": _rate(
            sum(item["understandable"] for item in usability_records),
            len(usability_records),
        ),
        "useful_count": sum(item["useful"] for item in usability_records),
        "useful_rate": _rate(
            sum(item["useful"] for item in usability_records),
            len(usability_records),
        ),
        "confidence_counts": confidence_counts,
        "needs_expert_review_count": sum(
            item["needs_expert_review"] for item in usability_records
        ),
        "needs_expert_review_rate": _rate(
            sum(item["needs_expert_review"] for item in usability_records),
            len(usability_records),
        ),
        "obvious_problem_count": sum(
            item["obvious_problem"] for item in usability_records
        ),
        "obvious_problem_rate": _rate(
            sum(item["obvious_problem"] for item in usability_records),
            len(usability_records),
        ),
        "missing_information_count": sum(
            item["missing_information"] for item in missing_information_records
        ),
        "missing_information_rate": _rate(
            sum(item["missing_information"] for item in missing_information_records),
            len(missing_information_records),
        ),
        "missing_information_feedback_count": len(missing_information_records),
        "clarification_useful_count": sum(
            bool(item["clarification_useful"]) for item in clarification_records
        ),
        "clarification_useful_rate": _rate(
            sum(bool(item["clarification_useful"]) for item in clarification_records),
            len(clarification_records),
        ),
        "clarification_feedback_count": len(clarification_records),
        "professional_correctness_assessed_count": sum(
            item.get("professional_correctness_assessed") is True
            for item in usability_records
        ),
        "safety_concern_count": sum(item["safety_concern"] for item in feedback_records),
        "safety_concern_rate": _rate(
            sum(item["safety_concern"] for item in feedback_records),
            len(feedback_records),
        ),
        "output_redaction_count": sum(item["output_redacted"] for item in provider_records),
        "median_duration_seconds": statistics.median(durations) if durations else None,
        "evidence_status": "internal_self_pilot_only",
        "external_validation_claim_allowed": False,
    }
    _atomic_write_text(session / "pilot_summary.md", _render_summary(summary))
    return summary


class _CapturingExecutor:
    def __init__(self, executor: EvalV2TaskExecutor) -> None:
        self._executor = executor
        self.result = None

    def execute(self, public_input, gateway):
        self.result = self._executor.execute(public_input, gateway)
        return self.result


def _select_pilot_tasks(
    tasks: Sequence[EvalV2PublicTask], task_count: int
) -> list[EvalV2PublicTask]:
    eligible = [
        task
        for task in tasks
        if task.lifecycle_status == "ready" and task.scenario in _SAFE_SCENARIOS
    ]
    if len(eligible) < task_count:
        raise EvalV2ContractError(
            "self_pilot_tasks_insufficient", "可用于 self-pilot 的 ready tasks 不足。"
        )
    selected: list[EvalV2PublicTask] = []
    dataset_counts: dict[str, int] = {}
    plan = list(_DEFAULT_SCENARIO_PLAN[:task_count])
    while len(plan) < task_count:
        plan.append("standard_analysis")
    for scenario in plan:
        candidates = [
            task
            for task in eligible
            if task.scenario == scenario and task not in selected
        ]
        if not candidates:
            candidates = [task for task in eligible if task not in selected]
        chosen = min(
            candidates,
            key=lambda task: (dataset_counts.get(task.dataset_id, 0), task.task_id),
        )
        selected.append(chosen)
        dataset_counts[chosen.dataset_id] = dataset_counts.get(chosen.dataset_id, 0) + 1
    return selected


def _next_unrun_task_id(state: Mapping[str, Any]) -> str | None:
    for record in state["records"]:
        if record["provider_run"] is None:
            return record["task_id"]
    return None


def _record_for(state: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    for record in state["records"]:
        if record["task_id"] == task_id:
            return record
    raise EvalV2ContractError(
        "self_pilot_state_invalid", "Session state 缺少 task record。"
    )


def _normalize_feedback_notes(notes: str | None) -> str | None:
    if notes is None:
        return None
    if not isinstance(notes, str) or len(notes) > 2000:
        raise EvalV2ContractError(
            "self_pilot_feedback_invalid", "notes 必须是不超过 2000 字符的文本。"
        )
    normalized = notes.strip() or None
    if normalized and (
        _SECRET.search(normalized)
        or _WINDOWS_PATH.search(normalized)
        or _UNIX_PATH.search(normalized)
        or _SUBJECT_KEY.search(normalized)
    ):
        raise EvalV2ContractError(
            "self_pilot_feedback_sensitive", "notes 不得包含凭据或绝对路径。"
        )
    return normalized


def _session_identity(state: Mapping[str, Any]) -> dict[str, str]:
    stored_instance = state.get("session_instance_id")
    stored_pack = state.get("pilot_pack_id")
    if isinstance(stored_instance, str) and isinstance(stored_pack, str):
        return {
            "session_instance_id": stored_instance,
            "session_instance_id_source": "stored",
            "pilot_pack_id": stored_pack,
        }
    legacy_session_id = str(state["session_id"])
    created_at = str(state.get("created_at_utc", "legacy-created-at-missing"))
    derived = hashlib.sha256(
        (legacy_session_id + "\0" + created_at).encode("utf-8")
    ).hexdigest()[:16].upper()
    legacy_suffix = legacy_session_id.removeprefix("SELF-PILOT-")
    return {
        "session_instance_id": "SELF-PILOT-LEGACY-" + derived,
        "session_instance_id_source": "legacy_derived",
        "pilot_pack_id": "PILOT-PACK-" + legacy_suffix,
    }


def _safe_display_output(output: str) -> tuple[str, bool]:
    unsafe = bool(
        _WINDOWS_PATH.search(output)
        or _UNIX_PATH.search(output)
        or _SECRET.search(output)
        or _SUBJECT_KEY.search(output)
    )
    return (
        "[OUTPUT_REDACTED_BY_SELF_PILOT_SAFETY_FILTER]" if unsafe else output,
        unsafe,
    )


def _validate_session_directory(project_root: Path, session_directory: Path) -> Path:
    artifacts_root = (project_root / "artifacts").resolve()
    resolved = session_directory.resolve()
    if resolved == artifacts_root or not resolved.is_relative_to(artifacts_root):
        raise EvalV2ContractError(
            "self_pilot_path_not_allowed",
            "Self-pilot session 必须位于项目 artifacts 的独立子目录。",
        )
    return resolved


def _load_session(
    project_root: str | Path, session_directory: str | Path
) -> Path:
    root = Path(project_root).resolve()
    session = _validate_session_directory(root, Path(session_directory))
    if not (session / "pilot_state.json").is_file() or not (
        session / "pilot_tasks.json"
    ).is_file():
        raise EvalV2ContractError(
            "self_pilot_session_invalid", "Self-pilot session 文件不完整。"
        )
    state = _load_json(session / "pilot_state.json")
    blinded = _load_json(session / "pilot_tasks.json")
    schema_version = state.get("schema_version")
    if (
        schema_version not in _SUPPORTED_SELF_PILOT_SCHEMA_VERSIONS
        or blinded.get("schema_version") != schema_version
        or state.get("session_type") != "internal_self_pilot"
        or state.get("external_pilot") is not False
        or not isinstance(state.get("selected_task_ids"), list)
        or not isinstance(state.get("records"), list)
        or state.get("blinded_tasks_sha256")
        != _sha256_file(session / "pilot_tasks.json")
        or state.get("session_id") != blinded.get("session_id")
    ):
        raise EvalV2ContractError(
            "self_pilot_session_invalid", "Self-pilot session 状态或 blinded hash 无效。"
        )
    if schema_version == "1.1" and (
        not isinstance(state.get("session_instance_id"), str)
        or _SESSION_INSTANCE_ID.fullmatch(state["session_instance_id"]) is None
        or state.get("session_id") != state.get("session_instance_id")
        or blinded.get("session_instance_id") != state.get("session_instance_id")
        or not isinstance(state.get("pilot_pack_id"), str)
        or _PILOT_PACK_ID.fullmatch(state["pilot_pack_id"]) is None
        or blinded.get("pilot_pack_id") != state.get("pilot_pack_id")
    ):
        raise EvalV2ContractError(
            "self_pilot_session_invalid",
            "Self-pilot 1.1 session instance/pack identity 无效。",
        )
    return session


def _assert_blinded_pack(path: Path) -> None:
    serialized = path.read_text(encoding="utf-8")
    for forbidden in (
        '"expected"',
        '"required_phrases"',
        '"forbidden_assertions"',
        '"numeric_claims"',
    ):
        if forbidden in serialized:
            raise EvalV2ContractError(
                "self_pilot_golden_leak", "Blinded pilot pack 泄露 expected golden。"
            )


def _render_session_readme(
    session_instance_id: str, pilot_pack_id: str, task_count: int
) -> str:
    return "\n".join(
        [
            "# ResearchOps internal self-pilot",
            "",
            f"- Session instance: `{session_instance_id}`",
            f"- Pilot pack: `{pilot_pack_id}`",
            f"- Tasks: {task_count}",
            "- Type: internal self-pilot",
            "- External validation: false",
            "- Goldens included in pilot task pack: false",
            "",
            "Use `self-pilot-next`, then `self-pilot-run`, then record human feedback",
            "before moving to the next task. Do not edit the session files manually.",
            "",
        ]
    )


def _render_summary(summary: Mapping[str, Any]) -> str:
    lines = [
        "# Internal self-pilot summary",
        "",
        f"- Session instance: `{summary['session_instance_id']}`",
        f"- Session instance source: `{summary['session_instance_id_source']}`",
        f"- Pilot pack: `{summary['pilot_pack_id']}`",
        f"- Status: `{summary['status']}`",
        f"- Tasks: {summary['task_count']}",
        f"- Provider runs: {summary['provider_run_count']}",
        f"- Feedback completed: {summary['feedback_completed_count']}",
        f"- Feedback schemas: `{json.dumps(summary['feedback_schema_counts'], sort_keys=True)}`",
        f"- Machine pass rate: {_display_rate(summary['machine_pass_rate'])}",
    ]
    if summary["legacy_feedback_count"]:
        lines.extend(
            [
                "",
                "## Legacy acceptance feedback",
                "",
                f"- Coverage: {summary['legacy_feedback_count']}",
                f"- Human acceptance rate: {_display_rate(summary['human_acceptance_rate'])}",
                f"- First-pass rate: {_display_rate(summary['first_pass_rate'])}",
                f"- Manual revisions: {summary['manual_revision_count']}",
                f"- Critical errors: {summary['critical_error_count']}",
            ]
        )
    if summary["non_expert_usability_feedback_count"]:
        confidence = summary["confidence_counts"]
        lines.extend(
            [
                "",
                "## Non-expert usability feedback",
                "",
                f"- Coverage: {summary['non_expert_usability_feedback_count']}",
                f"- Understandable rate: {_display_rate(summary['understandable_rate'])}",
                f"- Useful rate: {_display_rate(summary['useful_rate'])}",
                f"- Confidence low/medium/high: {confidence['low']}/{confidence['medium']}/{confidence['high']}",
                f"- Needs expert review rate: {_display_rate(summary['needs_expert_review_rate'])}",
                f"- Obvious problem rate: {_display_rate(summary['obvious_problem_rate'])}",
                f"- Missing information rate: {_display_rate(summary['missing_information_rate'])} "
                f"(coverage {summary['missing_information_feedback_count']})",
                f"- Clarification useful rate: {_display_rate(summary['clarification_useful_rate'])}",
                "- Professional correctness assessed: false",
            ]
        )
    lines.extend(
        [
            "",
            f"- Safety concerns: {summary['safety_concern_count']}",
            f"- Output redactions: {summary['output_redaction_count']}",
            f"- Median duration: {summary['median_duration_seconds']}",
            "",
            "This is an internal self-pilot, not an external user or expert validation.",
            "",
        ]
    )
    return "\n".join(lines)


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _display_rate(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.2%}"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvalV2ContractError(
            "self_pilot_file_invalid", "无法读取 self-pilot JSON 文件。"
        ) from exc
    if not isinstance(value, dict):
        raise EvalV2ContractError(
            "self_pilot_file_invalid", "Self-pilot JSON 顶层必须是对象。"
        )
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    _write_json(temporary, payload)
    temporary.replace(path)


def _atomic_write_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
