from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from .audit import AuditLedger
from .tool_runtime import (
    ControlledToolExecutor,
    ToolRegistry,
    TransientToolError,
    build_project_tool_registry,
)


def start_phase4_demo(
    *,
    project_root: str | Path,
    audit_database: str | Path,
    audit_export: str | Path,
    release_name: str = "demo-release",
) -> dict[str, Any]:
    """Run safe planning, inject one transient failure, then pause before a write."""

    root = Path(project_root).resolve()
    database_path = Path(audit_database).resolve()
    export_path = Path(audit_export).resolve()
    ledger = AuditLedger(database_path)
    run_id = ledger.start_run(
        mode="offline_agent_demo",
        request_summary={
            "objective": "read aggregate evidence and propose a controlled release",
            "bundle_id": "phase3",
        },
        dataset_sha256=_phase3_dataset_hash(root),
    )
    registry = _demo_registry(root)
    executor = ControlledToolExecutor(ledger, registry, sleeper=lambda _: None)

    summary = executor.propose(
        run_id,
        "read_aggregate_evidence",
        {"bundle_id": "phase3"},
    )
    pending = executor.propose(
        run_id,
        "publish_aggregate_results",
        {"bundle_id": "phase3", "release_name": release_name},
    )
    ledger.set_run_status(run_id, "waiting_approval")
    _write_export(ledger, run_id, export_path)
    verification = ledger.verify_chain(run_id)
    return {
        "status": "waiting_approval",
        "run_id": run_id,
        "safe_tool_call": summary.to_dict(),
        "pending_tool_call": pending.to_dict(),
        "simulated_transient_failures": 1,
        "audit_chain": verification.to_dict(),
    }


def decide_phase4_call(
    *,
    project_root: str | Path,
    audit_database: str | Path,
    call_id: str,
    decision: str,
    approver: str,
    reason: str | None = None,
    audit_export: str | Path | None = None,
) -> dict[str, Any]:
    ledger = AuditLedger(audit_database)
    registry = build_project_tool_registry(project_root)
    executor = ControlledToolExecutor(ledger, registry)
    outcome = executor.decide(
        call_id,
        decision=decision,
        approver=approver,
        reason=reason,
    )
    if decision == "approve":
        ledger.set_run_status(outcome.run_id, "running")
    else:
        ledger.set_run_status(outcome.run_id, "cancelled", terminal_error_code="tool_approval_rejected")
    if audit_export is not None:
        _write_export(ledger, outcome.run_id, audit_export)
    return {
        **outcome.to_dict(),
        "audit_chain": ledger.verify_chain(outcome.run_id).to_dict(),
    }


def resume_phase4_call(
    *,
    project_root: str | Path,
    audit_database: str | Path,
    call_id: str,
    audit_export: str | Path | None = None,
) -> dict[str, Any]:
    ledger = AuditLedger(audit_database)
    registry = build_project_tool_registry(project_root)
    executor = ControlledToolExecutor(ledger, registry)
    outcome = executor.execute(call_id)
    ledger.set_run_status(outcome.run_id, "completed")
    if audit_export is not None:
        _write_export(ledger, outcome.run_id, audit_export)
    return {
        **outcome.to_dict(),
        "audit_chain": ledger.verify_chain(outcome.run_id).to_dict(),
    }


def export_audit_run(
    *,
    audit_database: str | Path,
    run_id: str,
    output_path: str | Path,
) -> dict[str, Any]:
    ledger = AuditLedger(audit_database)
    _write_export(ledger, run_id, output_path)
    return ledger.verify_chain(run_id).to_dict()


def _demo_registry(project_root: Path) -> ToolRegistry:
    base = build_project_tool_registry(project_root)
    registry = ToolRegistry()
    remaining_failures = {"count": 1}
    for spec in base.specs():
        if spec.name != "read_aggregate_evidence":
            registry.register(spec)
            continue
        original_handler = spec.handler

        def flaky_handler(arguments, context, *, _handler=original_handler):
            if remaining_failures["count"] > 0:
                remaining_failures["count"] -= 1
                raise TransientToolError(
                    "artifact_temporarily_locked",
                    "聚合证据暂时被占用；允许按重试策略再次读取。",
                )
            return _handler(arguments, context)

        registry.register(replace(spec, handler=flaky_handler))
    return registry


def _phase3_dataset_hash(project_root: Path) -> str:
    payload = json.loads(
        (project_root / "artifacts" / "phase3" / "analysis_bundle.json").read_text(
            encoding="utf-8"
        )
    )
    return str(payload["dataset"]["sha256"])


def _write_export(ledger: AuditLedger, run_id: str, output_path: str | Path) -> None:
    destination = Path(output_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    payload = json.dumps(
        ledger.export_run(run_id),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    temporary.write_text(payload + "\n", encoding="utf-8")
    os.replace(temporary, destination)
