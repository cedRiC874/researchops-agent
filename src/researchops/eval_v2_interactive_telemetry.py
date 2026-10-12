"""Local interactive completion receipts, never campaign evaluation evidence."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from researchops_completion_telemetry import surface_mapping as mapping
from researchops_completion_telemetry.capture import (
    RuntimeDenominatorTracker, verify_runtime_denominator_plan,
)
from .audit import AuditError, AuditLedger, COMPLETION_TELEMETRY_UNMAPPED_EVENT, sha256_json, verify_audit_chain_rows
from .completion_telemetry_ledger import LedgerCompletionTelemetrySession
from .eval_v2_contracts import EvalV2ContractError

MODULE_VERSION = "eval-v2-interactive-telemetry/1.1"
_READABLE_MODULE_VERSIONS = frozenset({"eval-v2-interactive-telemetry/1.0", MODULE_VERSION})
RUNTIME_SCOPE = "eval_v2_interactive_v1"
INCOMPLETE_CODE = "eval_v2_interactive_telemetry_incomplete"
_DIRECTORY = "interactive-telemetry"
_CLAIM_TOKEN = object()
_MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})
_INTEGRITY_REASONS = frozenset({"ok", "receipt_missing_unsealed", "receipt_missing_sealed",
                              "chain_invalid", "hash_mismatch", "unreadable", "other_integrity_failure"})
_TERMINALS = {
    "model_response_telemetry_recorded": "response_accepted",
    COMPLETION_TELEMETRY_UNMAPPED_EVENT: "response_accepted",
    "model_response_telemetry_rejected": "response_rejected",
    "model_request_http_error": "http_error",
    "model_request_no_response": "no_response",
    "model_request_cancelled": "cancelled",
    "model_request_outcome_unknown": "outcome_unknown",
}


class InteractiveTelemetryError(EvalV2ContractError):
    def __init__(self, code, message, *, case_reserved=False, telemetry_summary=None):
        super().__init__(code, message)
        self._case_reserved = case_reserved is True
        self._telemetry_summary_json = (
            json.dumps(telemetry_summary, ensure_ascii=False, allow_nan=False)
            if self._case_reserved and telemetry_summary is not None else None
        )

    @property
    def message(self) -> str:
        return str(self)

    @property
    def case_reserved(self) -> bool:
        return self._case_reserved

    @property
    def telemetry_summary(self) -> dict[str, Any] | None:
        return json.loads(self._telemetry_summary_json) if self._telemetry_summary_json is not None else None


def _error(code: str, message: str, *, case_reserved=False, telemetry_summary=None) -> InteractiveTelemetryError:
    return InteractiveTelemetryError(code, message, case_reserved=case_reserved, telemetry_summary=telemetry_summary)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _verify_loaded_sources(root: Path, *, expert_review: bool) -> None:
    import sys
    expected = {
        "researchops": root / "src/researchops",
        "researchops_completion_telemetry": root / "src/researchops_completion_telemetry",
    }
    if expert_review:
        expected["researchops_expert_review"] = root / "services/expert_review_v1/src/researchops_expert_review"
    try:
        for name, directory in expected.items():
            package = sys.modules.get(name)
            origin = package.__file__ if package is not None else importlib.util.find_spec(name).origin
            if not _same_path(Path(origin).parent, directory):
                raise ValueError("source")
        # Also reject mixed packages whose already-loaded children came from an old install.
        for name, module in tuple(sys.modules.items()):
            parent = name.split(".", 1)[0]
            if parent in expected and module is not None and getattr(module, "__file__", None):
                source = Path(module.__file__).resolve()
                directory = expected[parent].resolve()
                if os.path.commonpath([os.path.normcase(str(source)), os.path.normcase(str(directory))]) != os.path.normcase(str(directory)):
                    raise ValueError("source")
    except Exception:
        raise _error("eval_v2_interactive_source_required", "必须从当前仓库 src 加载模块；请设置 PYTHONPATH=src（现场提问另加 services/expert_review_v1/src），重新启动进程。") from None


def _verify_clean_commit(root: Path) -> str:
    def git(*arguments: str) -> str:
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *arguments], capture_output=True, check=False)
        if result.returncode:
            raise _error("eval_v2_interactive_git_required", "无法核对当前仓库提交；请在完整 Git 工作区运行。")
        return result.stdout.decode("utf-8", errors="strict")
    try:
        if not _same_path(Path(git("rev-parse", "--show-toplevel").strip()), root):
            raise ValueError("root")
        commit = git("rev-parse", "HEAD").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("commit")
        dirty = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "src", "evals", "services/expert_review_v1/src")
    except InteractiveTelemetryError:
        raise
    except Exception:
        raise _error("eval_v2_interactive_git_required", "无法核对当前仓库提交；请在完整 Git 工作区运行。") from None
    if dirty:
        paths = []
        records = iter(dirty.split("\0"))
        for entry in records:
            if not entry:
                continue
            paths.append(entry[3:])
            if "R" in entry[:2] or "C" in entry[:2]:
                next(records, None)
        shown = ", ".join(json.dumps(path, ensure_ascii=False) for path in paths[:10])
        raise _error("eval_v2_interactive_dirty_tree", f"代码目录有未提交改动：{shown}。请提交需要的修改，或在保存修改后恢复干净工作区；未跟踪文件也须处理，无跳过选项。")
    return commit


def preflight_interactive_environment(*, root, provider, model_id, expert_review=False):
    """Read-only environment admission; never grants a binding or reads a Key."""
    if (getattr(provider, "provider_id", None) != "deepseek" or not isinstance(model_id, str) or model_id not in _MODELS
            or getattr(provider, "api_surface", None) != "responses"
            or getattr(provider, "transport_id", None) != "openai_compatible_responses"):
        raise _error("eval_v2_interactive_provider_denied", "交互遥测仅允许 DeepSeek Responses 及现有 allowlist 模型。")
    root = Path(root).resolve()
    _verify_loaded_sources(root, expert_review=expert_review)
    commit = _verify_clean_commit(root)
    return {"git_commit": commit, "provider": provider.provider_id, "model": model_id, "module_version": MODULE_VERSION}


class EvalV2InteractiveTelemetrySession(LedgerCompletionTelemetrySession):
    __slots__ = ("_owner", "_model_id", "_sdk_reconciliation")

    def __init__(self, *args, _owner, **kwargs):
        if type(_owner) is not _InteractiveCase or _owner._token is not _CLAIM_TOKEN:
            raise _error("eval_v2_interactive_claim_required", "需要已完成准入检查的交互 case。")
        self._owner = _owner
        self._model_id = _owner.model_id
        from .phase6_agent import AgentSdkReconciliation
        self._sdk_reconciliation = AgentSdkReconciliation("unavailable", None, None, ())
        super().__init__(*args, **kwargs)

    @property
    def model_id(self):
        return self._model_id

    def _required_runtime_authority_scope(self):
        return RUNTIME_SCOPE

    def _is_exact_supported_session_type(self):
        return type(self) is EvalV2InteractiveTelemetrySession

    def assert_provider_telemetry_authority(self):
        super().assert_provider_telemetry_authority()
        if self._owner._finished or self._owner._failed or self._session._tracker._sealed or self.case_id in self._session._tracker._case_reconciliations:
            raise AuditError("audit_completion_session_failed", "交互 case 已封存或失败。")

    def begin_attempt(self):
        self.assert_provider_telemetry_authority()
        return super().begin_attempt()

    def _record_sdk_result(self, result):
        from .phase6_agent import _sdk_reconciliation_from_object
        self._sdk_reconciliation = _sdk_reconciliation_from_object(result, source="run_result")

    def _record_sdk_exception(self, exc):
        from .phase6_agent import _sdk_reconciliation_from_exception
        self._sdk_reconciliation = _sdk_reconciliation_from_exception(exc)
        self._owner._failed = True

    def _finalize(self, handle, terminal_kind, **kwargs):
        terminal = super()._finalize(handle, terminal_kind, **kwargs)
        if terminal_kind != "response_accepted":
            self._owner._failed = True
        else:
            try:
                state = self._binding.resolve_mapping(terminal._capture.mapping_projection())[0]
            except Exception:
                self._owner._failed = True
                raise
            if state != "completed":
                self._owner._failed = True
        return terminal


class _InteractiveCase:
    def __init__(self, token, *, root, model_id, commit):
        if token is not _CLAIM_TOKEN:
            raise TypeError("private interactive claim required")
        self._token = token
        self.root = root
        self.model_id = model_id
        self.commit = commit
        self._binding_taken = False
        self._finished = False
        self._failed = False

    def finish(self, *, completion_status: str) -> dict[str, Any]:
        if self._finished:
            return dict(self.summary)
        self._finished = True
        summary = _empty_summary(self.metadata)
        writing = False
        try:
            from .phase6_runner import _seal_runtime_telemetry_case, _finalize_runtime_completion_telemetry
            _seal_runtime_telemetry_case(self.tracker, self.session.case_id, self.session._sdk_reconciliation)
            telemetry = _finalize_runtime_completion_telemetry(
                self.tracker, ledger=self.ledger,
                audit_index=[{"run_id": self.run_id, "task_id": self.session.case_id,
                              "completion_telemetry_event_commitment": self.session.event_commitment()}],
                ledger_failure_observed=self.session.failed,
            )
            artifact = telemetry["runtime_denominator"]
            rows, run = _read_rows(self.database)
            summary = _summarize_rows(self.metadata, rows)
            summary["closure_passed"] = telemetry["closure"]["claim_allowed"] is True
            summary["write_failed"] = self.session.failed
            summary["completion_status"] = "complete" if completion_status == "complete" else "failed"
            summary["normal_result"] = summary["closure_passed"] and completion_status == "complete" and not self._failed
            payload = {"metadata": self.metadata, "artifact": artifact,
                       "event_commitment": self.session.event_commitment(), "summary": summary}
            writing = True
            self.ledger.append_event(self.run_id, "interactive_telemetry_sealed", {"receipt_sha256": sha256_json(payload)})
            self.ledger.set_run_status(self.run_id, "completed" if summary["normal_result"] else "failed", terminal_error_code=None if summary["normal_result"] else INCOMPLETE_CODE)
            rows, run = _read_rows(self.database)
            receipt = {"payload": payload, "ledger_sha256": sha256_json({"run": run, "events": rows}),
                       "ledger_file_sha256": _sealed_database_sha256(self.database)}
            with self.database.with_suffix(".json").open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            self._failed = True
            try:
                rows, _ = _read_rows(self.database)
                summary = _summarize_rows(self.metadata, rows)
            except Exception:
                pass
            summary.update(closure_passed=False, normal_result=False,
                           write_failed=self.session.failed or writing, completion_status="failed")
        self.summary = summary
        return dict(summary)


def _binding_for_claim(owner):
    if type(owner) is not _InteractiveCase or owner._token is not _CLAIM_TOKEN or owner._binding_taken or owner._finished:
        raise _error("eval_v2_interactive_claim_required", "交互授权无效或已经使用。")
    owner._binding_taken = True
    offline = mapping.load_and_select_surface_mapping(owner.root, "deepseek", "responses", "openai_compatible_responses", purpose="offline_validation")
    selection = mapping.VerifiedSurfaceSelection._create(
        mapping._SELECTION_TOKEN, purpose=RUNTIME_SCOPE,
        telemetry_schema_sha256=offline.telemetry_schema_sha256, mapping=offline.mapping_snapshot(),
        entry=dict(adapter_version=offline.adapter_version, mapping_version=offline.mapping_version,
                   output_counter_comparability=offline.output_counter_comparability,
                   output_counter_path=offline.output_counter_path, runtime_binding_allowed=True),
    )
    return mapping.VerifiedRuntimeCompletionBinding._create(mapping._RUNTIME_BINDING_TOKEN, selection)


def _prepare_interactive_case(*, root, session_dir, case_id, provider, model_id, max_turns, expert_review=False):
    """Called only after the entrypoint's confirmation/authorization/dedup gates."""
    preflight = preflight_interactive_environment(root=root, provider=provider, model_id=model_id, expert_review=expert_review)
    root = Path(root).resolve()
    commit = preflight["git_commit"]
    if not isinstance(case_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", case_id):
        raise _error("eval_v2_interactive_case_invalid", "交互 case 标识无效。")
    owner = _InteractiveCase(_CLAIM_TOKEN, root=root, model_id=model_id, commit=commit)
    binding = _binding_for_claim(owner)
    snapshot = binding.runtime_snapshot()
    if provider.adapter_version != snapshot["adapter_version"]:
        raise _error("eval_v2_interactive_provider_denied", "Provider adapter 版本与已验证映射不一致。")
    plan = {key: snapshot[key] for key in ("provider_id", "api_surface", "transport_id", "adapter_version", "telemetry_schema_sha256", "mapping_schema_version", "mapping_version", "mapping_sha256")}
    plan.update(schema_version="provider-completion-runtime-denominator-plan/1.0", case_ids=[case_id],
                case_ids_sha256=sha256_json([case_id]), max_turns_per_case=max_turns,
                total_model_request_cap=max_turns, agents_sdk_retries=0, http_client_retries=0,
                denominator_algorithm="transport-response-finalization-v1", exact_response_count_preregistered=False)
    commitment = sha256_json(plan)
    reserved = False
    try:
        verified = verify_runtime_denominator_plan(binding, plan, preregistration_commitment=commitment)
        owner.tracker = RuntimeDenominatorTracker(verified)
        directory = Path(session_dir).resolve() / _DIRECTORY
        directory.mkdir(exist_ok=True)
        owner.database = directory / (sha256_json(case_id) + ".sqlite3")
        # Exclusive creation permanently reserves this case even if setup or the process fails.
        owner.run_id = "RUN-INTERACTIVE-" + uuid.uuid4().hex
        owner.metadata = dict(case_id=case_id, run_id=owner.run_id, model_id=model_id,
                              authority_scope=RUNTIME_SCOPE, git_commit=commit, module_version=MODULE_VERSION,
                              plan_commitment_sha256=commitment, plan=plan, binding=snapshot,
                              evaluation_evidence=False)
        try:
            with owner.database.open("xb"):
                reserved = True
        except FileExistsError:
            existing = owner.database.is_file()
            raise _error("eval_v2_interactive_case_already_run", "本题已登记运行，失败或中断后也不能重跑。",
                         case_reserved=existing,
                         telemetry_summary=_reservation_summary(owner, write_failed=False, existing=True) if existing else None) from None
        owner.ledger = AuditLedger(owner.database)
        owner.ledger.start_run(mode=RUNTIME_SCOPE, run_id=owner.run_id, request_summary=owner.metadata)
        owner.ledger.append_event(owner.run_id, "interactive_plan_committed", owner.metadata)
        owner.session = EvalV2InteractiveTelemetrySession(owner.tracker.bind_case(case_id), ledger=owner.ledger,
                                                        run_id=owner.run_id, runtime_plan_binding=verified, _owner=owner)
        return owner
    except InteractiveTelemetryError as exc:
        if not reserved or exc.case_reserved:
            raise
        raise _error(INCOMPLETE_CODE, "交互遥测计划无法完整持久化；未读取密钥，未打开 Provider。",
                     case_reserved=True, telemetry_summary=_reservation_summary(owner, write_failed=True)) from None
    except Exception:
        raise _error(INCOMPLETE_CODE, "交互遥测计划无法完整持久化；未读取密钥，未打开 Provider。",
                     case_reserved=reserved,
                     telemetry_summary=_reservation_summary(owner, write_failed=True) if reserved else None) from None


def _reservation_summary(owner, *, write_failed, existing=False):
    metadata = {"case_id": owner.metadata["case_id"], "module_version": MODULE_VERSION} if existing else owner.metadata
    summary = _empty_summary(metadata)
    if existing:
        try:
            rows, run = _read_rows(owner.database)
            plans = [json.loads(row["safe_payload_json"]) for row in rows if row["event_type"] == "interactive_plan_committed"]
            if len(plans) != 1 or plans[0]["case_id"] != metadata["case_id"] or not verify_audit_chain_rows(run["run_id"], rows).valid:
                raise ValueError("reserved metadata")
            projected = _verification_output(dict(_summarize_rows(plans[0], rows), case_ordinal=1))
            summary.update({key: projected[key] for key in summary if key in projected})
            if re.fullmatch(r"RUN-INTERACTIVE-[0-9a-f]{32}", run["run_id"]):
                summary["run_id"] = run["run_id"]
        except Exception:
            pass
    summary.update(write_failed=write_failed, closure_passed=False, normal_result=False, completion_status="failed")
    return summary


def _read_rows(database):
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        runs = connection.execute("SELECT * FROM runs").fetchall()
        if len(runs) != 1:
            raise ValueError("run count")
        rows = [dict(row) for row in connection.execute("SELECT * FROM audit_events ORDER BY sequence")]
        return rows, dict(runs[0])


def _sealed_database_sha256(database):
    # A closed owned ledger is checkpointed; an outstanding WAL is not covered
    # by the main-file commitment and must never be accepted as sealed evidence.
    wal = Path(str(database) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("uncheckpointed ledger")
    return hashlib.sha256(Path(database).read_bytes()).hexdigest()


def _empty_summary(metadata):
    return {key: metadata.get(key) for key in ("case_id", "run_id", "plan_commitment_sha256", "git_commit", "module_version")} | dict(
        attempts=0, terminal_states=[], usage=dict(input_tokens=None, output_tokens=None, total_tokens=None),
        all_terminal=False, write_failed=False, closure_passed=False, normal_result=False, completion_status="failed")


def _summarize_rows(metadata, rows):
    summary = _empty_summary(metadata)
    starts, terminals, records = [], [], []
    for row in rows:
        event = row["event_type"]
        payload = json.loads(row["safe_payload_json"])
        if event == "model_request_started":
            starts.append(payload["attempt_index"])
        elif event in _TERMINALS:
            terminals.append(payload["attempt_index"])
            record = payload.get("completion_record")
            summary["terminal_states"].append(record["normalized_completion_state"] if record else _TERMINALS[event])
            records.append(record)
    summary["attempts"] = len(starts)
    summary["all_terminal"] = bool(starts) and sorted(starts) == sorted(terminals) and len(set(starts)) == len(starts)
    for field in summary["usage"]:
        values = [record.get("usage", {}).get("normalized", {}).get(field) if record else None for record in records]
        summary["usage"][field] = sum(values) if values and len(values) == len(starts) and all(type(value) is int for value in values) else None
    return summary


def verify_session(session_dir) -> tuple[list[dict[str, Any]], bool]:
    """Read-only replay: never constructs runtime bindings, sessions, or clients."""
    from researchops_completion_telemetry.persisted_evidence import (
        create_persisted_live_evidence_validation_context, summarize_persisted_live_runtime_denominator_artifact,
    )
    root = Path(__file__).resolve().parents[2]
    results = []
    directory = Path(session_dir).resolve() / _DIRECTORY
    databases = sorted({*directory.glob("*.sqlite3"), *(path.with_suffix(".sqlite3") for path in directory.glob("*.json"))})
    for ordinal, database in enumerate(databases, 1):
        summary = _empty_summary({})
        summary["case_ordinal"] = ordinal
        reason = "unreadable"
        try:
            receipt_path = database.with_suffix(".json")
            receipt = None
            file_digest = None
            if receipt_path.exists():
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                reason = "hash_mismatch"
                try:
                    file_digest = _sealed_database_sha256(database)
                except OSError:
                    reason = "unreadable"
                    raise
            reason = "unreadable"
            rows, run = _read_rows(database)
            reason = "chain_invalid"
            if not verify_audit_chain_rows(run["run_id"], rows).valid:
                raise ValueError("chain")
            reason = "other_integrity_failure"
            plan_rows = [row for row in rows if row["event_type"] == "interactive_plan_committed"]
            if len(plan_rows) != 1:
                raise ValueError("plan")
            metadata = json.loads(plan_rows[0]["safe_payload_json"])
            summary.update(_summarize_rows(metadata, rows))
            if receipt is None:
                reason = "receipt_missing_sealed" if any(row["event_type"] == "interactive_telemetry_sealed" for row in rows) else "receipt_missing_unsealed"
                raise ValueError("missing receipt")
            reason = "hash_mismatch"
            if receipt["ledger_file_sha256"] != file_digest:
                raise ValueError("ledger file")
            if receipt["ledger_sha256"] != sha256_json({"run": run, "events": rows}):
                raise ValueError("receipt")
            payload = receipt["payload"]
            sealed = [json.loads(row["safe_payload_json"]) for row in rows if row["event_type"] == "interactive_telemetry_sealed"]
            if sealed != [{"receipt_sha256": sha256_json(payload)}] or payload["metadata"] != metadata:
                raise ValueError("seal")
            reason = "other_integrity_failure"
            if (metadata["authority_scope"] != RUNTIME_SCOPE or metadata["evaluation_evidence"] is not False
                    or metadata["model_id"] not in _MODELS or run["run_id"] != metadata["run_id"]
                    or metadata.get("module_version") not in _READABLE_MODULE_VERSIONS
                    or metadata["plan"]["case_ids"] != [metadata["case_id"]]
                    or run["request_sha256"] != sha256_json(metadata)):
                raise ValueError("identity")
            offline = mapping.load_and_select_surface_mapping(root, "deepseek", "responses", "openai_compatible_responses", purpose="offline_validation")
            context = create_persisted_live_evidence_validation_context(offline, expected_binding=metadata["binding"],
                expected_denominator_plan=metadata["plan"], expected_denominator_plan_commitment_sha256=metadata["plan_commitment_sha256"])
            validated = summarize_persisted_live_runtime_denominator_artifact(payload["artifact"], context=context)
            artifact = payload["artifact"]
            starts = [json.loads(row["safe_payload_json"]) for row in rows if row["event_type"] == "model_request_started"]
            terminals = [json.loads(row["safe_payload_json"]) for row in rows if row["event_type"] in _TERMINALS]
            if len(starts) != validated.attempts_started or len(terminals) != validated.attempts_terminal:
                raise ValueError("counts")
            for attempt, start, terminal in zip(artifact["attempts"], starts, terminals, strict=True):
                if any(start[key] != attempt[key] or terminal[key] != attempt[key] for key in ("case_id", "attempt_index", "case_attempt_index")):
                    raise ValueError("attempt")
                if any(terminal[key] != attempt[key] for key in ("terminal_kind", "response_index", "error_code")):
                    raise ValueError("terminal")
                if start["binding"] != metadata["binding"] or terminal["binding"] != metadata["binding"]:
                    raise ValueError("binding")
                if attempt["terminal_kind"] == "response_accepted" and terminal["completion_record"] != artifact["records"][attempt["response_index"]]:
                    raise ValueError("record")
            for key in ("attempts", "terminal_states", "usage", "all_terminal"):
                if summary[key] != payload["summary"][key]:
                    raise ValueError("summary")
            summary.update(payload["summary"])
            summary["ledger_intact"] = True
            summary["integrity_reason"] = "ok"
            summary["closure_passed"] = validated.inner_closure_claim_allowed and summary["all_terminal"] and not summary["write_failed"]
            summary["case_closed"] = summary["closure_passed"]
            summary["verified"] = summary["closure_passed"]
        except Exception:
            summary.update(verified=False, ledger_intact=False, integrity_reason=reason, case_closed=False, closure_passed=False, normal_result=False, error_code=INCOMPLETE_CODE)
        # Output only fixed identifiers and aggregate facts; no stored question/answer/path.
        summary["case_ordinal"] = ordinal
        results.append(_verification_output(summary))
    return results, bool(results) and all(item.get("verified") is True for item in results)


def _verification_output(summary):
    """Never echo arbitrary data from a corrupt ledger, even on failure."""
    output = {"case_ordinal": summary["case_ordinal"]}
    for name in ("git_commit", "plan_commitment_sha256"):
        value = summary.get(name)
        length = 40 if name == "git_commit" else 64
        output[name] = value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value) else None
    version = summary.get("module_version")
    output["module_version"] = version if isinstance(version, str) and version in _READABLE_MODULE_VERSIONS else None
    attempts = summary.get("attempts")
    output["attempts"] = attempts if type(attempts) is int and attempts >= 0 else None
    states = {"completed", "incomplete_length", "incomplete_content_filter", "incomplete_other", "error", "unmapped", "not_provided", "not_persisted", *_TERMINALS.values()}
    output["terminal_states"] = [value if isinstance(value, str) and value in states else "invalid" for value in summary.get("terminal_states", [])]
    output["usage"] = {name: value if type(value) is int and value >= 0 else None for name, value in ((name, summary.get("usage", {}).get(name)) for name in ("input_tokens", "output_tokens", "total_tokens"))}
    for name in ("all_terminal", "write_failed", "closure_passed", "normal_result", "verified", "ledger_intact", "case_closed"):
        output[name] = summary.get(name) is True
    reason = summary.get("integrity_reason")
    output["integrity_reason"] = reason if isinstance(reason, str) and reason in _INTEGRITY_REASONS else "other_integrity_failure"
    output["completion_status"] = "complete" if summary.get("completion_status") == "complete" else "failed"
    if not output["verified"]:
        output["error_code"] = INCOMPLETE_CODE
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读核对交互试用账本；不联网、不读取密钥。")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--session-dir", required=True)
    preflight = sub.add_parser("preflight", help="只读检查交互运行环境，不读取密钥或创建会话")
    preflight.add_argument("--provider", default="deepseek")
    preflight.add_argument("--model", default="deepseek-v4-flash")
    preflight.add_argument("--expert-review", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            from .model_providers import get_provider, ProviderConfigurationError
            try:
                provider = get_provider(args.provider)
            except ProviderConfigurationError:
                raise _error("eval_v2_interactive_provider_denied", "交互预检仅接受 DeepSeek 及现有 allowlist 模型。") from None
            result = preflight_interactive_environment(root=Path(__file__).resolve().parents[2],
                provider=provider, model_id=args.model, expert_review=args.expert_review)
            print(json.dumps({"status": "ok", "message": "交互运行环境预检通过；未读取密钥，未联网，未创建会话。", **result}, ensure_ascii=False, sort_keys=True))
            return 0
        cases, complete = verify_session(args.session_dir)
        success = complete and all(case["case_closed"] and case["normal_result"] and case["completion_status"] == "complete" for case in cases)
        intact = bool(cases) and all(case["ledger_intact"] for case in cases)
        print(json.dumps({"cases": cases, "complete": success, "evaluation_evidence": False}, ensure_ascii=False, sort_keys=True))
        return 0 if success and intact else 1 if intact else 2
    except InteractiveTelemetryError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"error_code": INCOMPLETE_CODE, "complete": False,
                          "message": "只读核对或预检失败；未读取密钥，未发起在线请求。"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
