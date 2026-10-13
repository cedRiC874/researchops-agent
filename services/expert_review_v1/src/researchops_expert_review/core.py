from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from researchops import model_providers, self_pilot, self_pilot_web
from researchops.eval_v2_dataset_prep import EvalV2LogicalDatasetRegistry
from researchops.eval_v2_inspect_backend import EvalV2InspectDatasetBackend
from researchops.eval_v2_provider_executor import EvalV2ProviderExecutor
from researchops.eval_v2_runner import EvalV2ToolGateway
from researchops.eval_v2_interactive_telemetry import (
    InteractiveTelemetryError, _prepare_interactive_case, preflight_interactive_environment,
)


FORMAT_NOTICE = "个人信息检查只是格式匹配，不是完备检测；请勿输入个人信息或敏感内容。"
ONLINE_LIMITATION = "未满足代码来源、干净提交和遥测准入时，尚未开放真实在线运行；通过检查后仅允许 DeepSeek 本机交互试用，记录不是评测证据。"
_RUN_ID = re.compile(r"^EXPERT-[a-f0-9]{32}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_ASCII_PATH_PART = r"[A-Za-z0-9_.-]+"
_RELATIVE_PATH_START = r"(?<![A-Za-z0-9_./\\-])"
_PATTERNS = {
    "identity_number": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "phone_number": re.compile(r"(?<!\d)1\d{10}(?!\d)"),
    "email": re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    "file_path": re.compile(
        r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]"
        r"|\\\\[^\s\\/]+\\[^\s\\/]+"
        + r"|(?ai:file://)"
        # ASCII segments and an ASCII lookbehind preserve units and Chinese prose.
        # A second slash in an HTTP URL is not the start of a local absolute path.
        + r"|(?<![A-Za-z0-9/])/" + _ASCII_PATH_PART + r"(?:/" + _ASCII_PATH_PART + r")+"
        + r"|" + _RELATIVE_PATH_START + r"(?:~[\\/]|\.{1,2}[\\/])"
        + r"|" + _RELATIVE_PATH_START
        + r"(?ai:artifacts|data|evals|src|services|output|docs|tests|scripts|\.venv|\.git)[\\/]"
        + r"|" + _RELATIVE_PATH_START + r"(?:[\w.-]+[\\/])+[\w.-]+\."
        + r"(?ai:csv|tsv|json|jsonl|parquet|sqlite|sqlite3|db|xlsx|xls|txt|md|py|ps1|yml|yaml|toml|log|zip|data)"
        + r"(?=$|[^\w./\\-]|\.(?=$|\s))"
    ),
    "api_key": re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9._-]+", re.IGNORECASE),
    "subject_id": re.compile(r"(?<![A-Za-z0-9_])SUBJ-[A-Za-z0-9_-]+", re.IGNORECASE),
}
_OUTPUT_REDACTION_LABELS = {
    "file_path": "疑似本机路径",
    "api_key": "疑似 API Key",
    "subject_id": "疑似受试者编号",
    "self_pilot_filter": "原 self-pilot 安全规则",
}


class ExpertReviewError(ValueError):
    """Only fixed, safe user-facing messages cross the CLI/HTTP boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def question_sha256(question: str) -> str:
    return hashlib.sha256(" ".join(question.split()).encode("utf-8")).hexdigest()


def completion_summary(record: dict[str, Any]) -> str:
    telemetry = record.get("completion_telemetry")
    if not isinstance(telemetry, dict):
        return "模型调用摘要：尚无已登记的调用。"
    usage = telemetry.get("usage") or {}
    value = lambda name: "未知" if usage.get(name) is None else str(usage[name])
    complete = (telemetry.get("closure_passed") is True
                and telemetry.get("normal_result") is True
                and record.get("completion_status") == "complete")
    return (
        f"模型调用次数：{telemetry['attempts']}；是否完整：{'是' if complete else '否'}；"
        f"token 输入：{value('input_tokens')}，输出：{value('output_tokens')}，合计：{value('total_tokens')}。"
    )


def input_rejection_types(question: str) -> list[str]:
    hits = [name for name, pattern in _PATTERNS.items() if pattern.search(question)]
    if len(question) > 2000:
        hits.append("too_long")
    if not question.strip():
        hits.append("empty_question")
    return hits


def filter_output(output: str, *, api_key: str | None = None) -> tuple[str, list[str]]:
    safe_output, inherited_redaction = self_pilot._safe_display_output(output)
    hits = {
        name for name in ("file_path", "api_key", "subject_id")
        if _PATTERNS[name].search(output)
    }
    if api_key and api_key in output:
        hits.add("api_key")
    if inherited_redaction:
        hits.add("self_pilot_filter")
    if hits:
        safe_output = "[OUTPUT_REDACTED_BY_SELF_PILOT_SAFETY_FILTER]"
    return safe_output, [name for name in _OUTPUT_REDACTION_LABELS if name in hits]


def output_redaction_message(record: dict[str, Any]) -> str:
    if record.get("output_redacted") is not True:
        return ""
    types = record.get("output_redaction_types", [])
    if not isinstance(types, list):
        types = []
    labels = [label for name, label in _OUTPUT_REDACTION_LABELS.items() if name in types]
    message = "回答被安全过滤器整段屏蔽"
    return message + ("（命中：" + "、".join(labels) + "）" if labels else "。")


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


def _session_path(root: Path, value: str | Path) -> Path:
    try:
        return self_pilot._validate_session_directory(root, _resolve(root, value))
    except Exception:
        raise ExpertReviewError("session_path_not_allowed", "会话必须位于项目 artifacts 的独立子目录。") from None


def _write_json(path: Path, payload: Any) -> None:
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(4) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def create_session(*, project_root: str | Path, output_directory: str | Path) -> dict[str, Any]:
    root = Path(project_root).resolve()
    session = _session_path(root, output_directory)
    try:
        session.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ExpertReviewError("session_exists", "会话目录已存在；不会覆盖。") from None
    payload = {
        "schema_version": "expert-review/1.1",
        "session_id": "EXPERT-SESSION-" + secrets.token_hex(16),
        "created_at_utc": _now(),
        "automatic_scoring": False,
        "records": [],
    }
    _write_json(session / "index.json", payload)
    return {"status": "created", "session_id": payload["session_id"], "message": "会话已创建；无自动评分。"}


def preview(*, registry_path: str | Path, dataset_id: str, local_head: int = 0) -> str:
    if isinstance(local_head, bool) or not isinstance(local_head, int) or not 0 <= local_head <= 5:
        raise ExpertReviewError("local_head_invalid", "本机预览行数必须为 0 到 5。")
    try:
        registry = EvalV2LogicalDatasetRegistry.load(registry_path)
        backend = EvalV2InspectDatasetBackend(registry)
        # This is the complete tool result, without a second projection.
        output = json.dumps(backend.inspect_dataset(dataset_id), ensure_ascii=False, indent=2)
        if local_head:
            with registry.resolve(dataset_id).path.open(encoding="utf-8-sig") as stream:
                lines = [line.rstrip("\r\n") for _, line in zip(range(local_head), stream)]
            output += f"\n\n本机文件前 {local_head} 行（只在本机显示，不会发送）\n" + "\n".join(lines)
        return output
    except ExpertReviewError:
        raise
    except Exception:
        raise ExpertReviewError("preview_failed", "无法预览，请检查登记表、数据集和准备产物。") from None


class ExpertReview:
    def __init__(
        self, *, project_root: str | Path, session_directory: str | Path,
        registry_path: str | Path, provider_id: str = "deepseek",
        model_id: str = "deepseek-v4-flash", sdk_runner: Any = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.session = _session_path(self.project_root, session_directory)
        self.provider_id = provider_id
        self.model_id = model_id
        self._sdk_runner = sdk_runner
        if provider_id != "deepseek":
            raise ExpertReviewError("provider_invalid", "此次试用仅支持 DeepSeek。")
        try:
            self._provider = model_providers.get_provider(provider_id)
            self.model_id = self._provider.validate_model(model_id)
            self._registry = EvalV2LogicalDatasetRegistry.load(_resolve(self.project_root, registry_path))
            self._backend = EvalV2InspectDatasetBackend(self._registry)
        except Exception:
            raise ExpertReviewError("configuration_invalid", "模型或登记表配置无效。") from None
        self._load_index()

    def _load_index(self) -> dict[str, Any]:
        try:
            path = self.session / "index.json"
            if path.is_symlink():
                raise ValueError
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload["schema_version"] not in {"expert-review/1.0", "expert-review/1.1"} or not isinstance(payload["records"], list):
                raise ValueError
            return payload
        except Exception:
            raise ExpertReviewError("session_invalid", "会话无效，请使用已创建的现场提问会话。") from None

    def _records(self) -> list[dict[str, Any]]:
        records = []
        try:
            for path in sorted(self.session.glob("EXPERT-*.json")):
                if path.is_symlink() or not _RUN_ID.fullmatch(path.stem):
                    raise ValueError
                value = json.loads(path.read_text(encoding="utf-8"))
                if value["run_id"] != path.stem or not _HASH.fullmatch(value["question_sha256"]):
                    raise ValueError
                records.append(value)
            records.sort(key=lambda row: row["sequence"])
            return records
        except Exception:
            raise ExpertReviewError("session_invalid", "会话记录损坏；拒绝继续运行。") from None

    @contextmanager
    def _lock(self):
        path = self.session / ".session.lock"
        try:
            with path.open("x", encoding="ascii"):
                pass
        except FileExistsError:
            raise ExpertReviewError("session_busy", "会话正在执行或上次被中断；不能重复提交。") from None
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    def _save(self, record: dict[str, Any]) -> None:
        # Public example text may remain in the in-memory return value for callers;
        # no submitted question text is persisted in the session.
        _write_json(self.session / (record["run_id"] + ".json"), {**record, "question": None})
        index = self._load_index()
        index["schema_version"] = "expert-review/1.1"
        index["records"] = [
            {name: row[name] for name in ("sequence", "run_id", "dataset_id", "status", "question_sha256")}
            for row in self._records()
        ]
        _write_json(self.session / "index.json", index)

    def catalog(self) -> list[dict[str, Any]]:
        try:
            return self._backend.public_catalog()
        except Exception:
            raise ExpertReviewError("registry_invalid", "登记表或准备产物校验失败。") from None

    def _public_tasks(self) -> list[dict[str, Any]]:
        try:
            source = self.project_root / "evals/v2/public_tasks.jsonl"
            result = []
            with source.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    task = json.loads(line)
                    result.append({name: task[name] for name in ("task_id", "dataset_id", "prompt", "context")})
            return result
        except Exception:
            raise ExpertReviewError("tasks_invalid", "无法读取公开示例题。") from None

    def examples(self, dataset_id: str) -> list[dict[str, Any]]:
        self._authorize(dataset_id)
        return [
            {key: task[key] for key in ("task_id", "prompt", "context")}
            for task in self._public_tasks() if task["dataset_id"] == dataset_id
        ]

    def history(self) -> list[dict[str, Any]]:
        return [{key: record[key] for key in ("sequence", "dataset_id", "status", "run_id")} for record in self._records()]

    def _authorize(self, dataset_id: str) -> None:
        try:
            self._registry.resolve(dataset_id)
        except Exception:
            raise ExpertReviewError("dataset_not_authorized", "数据集不在登记表中或准备产物校验失败。") from None

    def preflight(self) -> dict[str, Any]:
        """Check the environment without reserving a question or reading a key."""
        try:
            return preflight_interactive_environment(
                root=self.project_root, provider=self._provider,
                model_id=self.model_id, expert_review=True,
            )
        except InteractiveTelemetryError as exc:
            raise ExpertReviewError(exc.code, exc.message) from None

    def ask(
        self, *, confirm_online: bool, dataset_id: str | None = None,
        question: str | None = None, from_task: str | None = None,
    ) -> dict[str, Any]:
        if confirm_online is not True:
            raise ExpertReviewError("online_confirmation_required", "必须先确认会把问题文本和汇总统计发送给 DeepSeek；未读取密钥。")
        if from_task is not None:
            if dataset_id is not None or question is not None:
                raise ExpertReviewError("question_source_invalid", "示例题和现场输入不能同时使用。")
            task = next((task for task in self._public_tasks() if task["task_id"] == from_task), None)
            if task is None:
                raise ExpertReviewError("task_unknown", "示例题号不存在。")
            dataset_id, question, context = task["dataset_id"], task["prompt"], task["context"]
            if context.get("dataset_id") != dataset_id:
                raise ExpertReviewError("task_scope_invalid", "示例题的数据集授权不一致。")
        else:
            context = {"dataset_id": dataset_id}
        if not isinstance(dataset_id, str) or not isinstance(question, str):
            raise ExpertReviewError("question_source_invalid", "请选择数据集并输入问题，或选择示例题。")
        self._authorize(dataset_id)
        digest = question_sha256(question)
        hits = input_rejection_types(question)
        if any(record["question_sha256"] == digest for record in self._records()):
            raise ExpertReviewError("question_already_run", "该问题已在本会话中提交；不允许覆盖或重跑。")
        self.preflight()
        with self._lock():
            # Recheck under the write lock after the read-only preflight.
            records = self._records()
            if any(record["question_sha256"] == digest for record in records):
                raise ExpertReviewError("question_already_run", "该问题已在本会话中提交；不允许覆盖或重跑。")
            record = {
                "run_id": "EXPERT-" + secrets.token_hex(16),
                "sequence": len(records) + 1,
                "created_at_utc": _now(), "completed_at_utc": None,
                "dataset_id": dataset_id,
                "question": question if from_task is not None and not hits else None,
                "question_sha256": digest,
                "source": from_task if from_task is not None else "现场提问",
                "provider": self.provider_id, "model": self.model_id,
                "preclassification": None, "outcome": "controlled_failure", "status": "出错",
                "output_redacted": False, "output_redaction_types": [],
                "answers": {"english": "", "chinese": "执行未完成或已中断；本题已占用，不能重跑。", "bilingual_complete": False},
                "tool_call_count": 0, "elapsed_seconds": None,
                "token_usage": {"input_tokens": None, "output_tokens": None},
                "completion_status": None, "completion_telemetry": None,
                "input_rejection_types": hits, "automatic_scoring": False,
                "notice": "无自动评分", "error_code": None,
            }
            if hits:
                record.update(outcome="refused", status="拒绝", completed_at_utc=_now(), elapsed_seconds=0.0,
                              error_code="input_format_rejected")
                record["answers"] = {
                    "english": "Rejected locally by format matching. Rewrite the question without sensitive information. This check is not exhaustive.",
                    "chinese": "问题命中格式检查，请去掉相关内容后改写。" + FORMAT_NOTICE,
                    "bilingual_complete": True,
                }
                self._save(record)
                return record
            # Reserve durably before looking up the key or attempting execution.
            self._save(record)
            started = time.monotonic()
            task_scope = SimpleNamespace(task_id="ADHOC-" + secrets.token_hex(4), context=context)
            gateway = EvalV2ToolGateway(task_scope, self._backend)
            public_input = {"task_id": task_scope.task_id, "prompt": question, "context": context}
            api_key = None
            telemetry = None
            try:
                defaults = inspect.signature(self_pilot.run_self_pilot_task).parameters
                telemetry = _prepare_interactive_case(
                    root=self.project_root, session_dir=self.session, case_id=record["run_id"],
                    provider=self._provider, model_id=self.model_id,
                    max_turns=defaults["max_turns"].default, expert_review=True,
                )
                api_key = os.environ.get(self._provider.api_key_env)
                if not api_key or not api_key.strip():
                    raise ExpertReviewError("provider_key_missing", "当前进程未配置 DeepSeek 密钥；本题不能重跑。")
                api_key = api_key.strip()
                executor = EvalV2ProviderExecutor(
                    provider=self._provider, model_id=self.model_id, api_key=api_key,
                    confirm_online=True, sdk_runner=self._sdk_runner,
                    bilingual_output=True,
                    max_output_tokens=self_pilot_web.SELF_PILOT_WEB_MAX_OUTPUT_TOKENS,
                    max_turns=defaults["max_turns"].default,
                    run_timeout_seconds=defaults["run_timeout_seconds"].default,
                    completion_telemetry_session=telemetry.session,
                )
                result = executor.execute(public_input, gateway)
                record["completion_status"] = getattr(result, "completion_status", None)
                record["completion_telemetry"] = telemetry.finish(completion_status=record["completion_status"])
                telemetry = None
                if (not record["completion_telemetry"]["closure_passed"]
                        or record["completion_telemetry"].get("normal_result") is not True
                        or record["completion_status"] != "complete"):
                    raise ExpertReviewError(
                        "eval_v2_interactive_telemetry_incomplete",
                        "本题完成遥测未闭合或模型未完整完成；回答不展示，本题不能重跑。",
                    )
                safe_output, redaction_types = filter_output(result.final_output, api_key=api_key)
                record.update(
                    outcome=result.outcome,
                    status={"completed": "完成", "clarification_required": "先提问澄清", "refused": "拒绝", "waiting_approval": "完成"}.get(result.outcome, "出错"),
                    output_redacted=bool(redaction_types), output_redaction_types=redaction_types,
                    answers=self_pilot_web.split_bilingual_output(safe_output),
                    token_usage={"input_tokens": result.input_tokens, "output_tokens": result.output_tokens},
                )
                if result.outcome == "waiting_approval":
                    record["notice"] += "；本次执行已结束，发布仍待审批，未实际发布。"
                policy = getattr(result, "preclassification", None)
                if isinstance(policy, str) and re.fullmatch(r"[a-z_]{1,64}", policy):
                    record["preclassification"] = policy
            except (Exception, asyncio.CancelledError, KeyboardInterrupt) as exc:
                code = getattr(exc, "code", None)
                if code == "eval_v2_completion_telemetry_session_required":
                    record["error_code"] = code
                    message = ONLINE_LIMITATION
                elif isinstance(exc, (ExpertReviewError, InteractiveTelemetryError)):
                    record["error_code"] = exc.code
                    message = exc.message
                    if isinstance(exc, InteractiveTelemetryError) and exc.case_reserved:
                        record["completion_telemetry"] = exc.telemetry_summary
                        record["completion_status"] = "provider_failed"
                else:
                    record["error_code"] = "eval_v2_interactive_telemetry_incomplete" if telemetry is not None else "execution_failed"
                    message = "执行失败；未保存底层错误正文，本题不能重跑。"
                record["answers"] = {"english": "Execution did not complete. This question cannot be rerun in this session.", "chinese": message, "bilingual_complete": True}
            finally:
                api_key = None
                if telemetry is not None:
                    record["completion_telemetry"] = telemetry.finish(
                        completion_status=record["completion_status"] or "controlled_failure"
                    )
                record["tool_call_count"] = len(gateway.tool_calls)
                record["elapsed_seconds"] = round(time.monotonic() - started, 6)
                record["completed_at_utc"] = _now()
                self._save(record)
            return record
