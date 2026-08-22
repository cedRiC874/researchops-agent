from __future__ import annotations

import asyncio
import html
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .eval_v2_contracts import EvalV2ContractError
from .model_providers import ProviderAdapter, get_provider
from .self_pilot import (
    get_next_self_pilot_task,
    get_self_pilot_blinded_tasks,
    get_self_pilot_progress,
    get_self_pilot_provider_binding,
    record_self_pilot_usability_feedback,
    run_self_pilot_task,
    summarize_self_pilot,
)


SELF_PILOT_WEB_VERSION = "1.1"
SELF_PILOT_WEB_MAX_OUTPUT_TOKENS = 10000
_MAX_JSON_BODY_BYTES = 16 * 1024
_MAX_TRANSLATION_BYTES = 256 * 1024
_TASK_ID = re.compile(r"^V2-(?:DEV|PUB)-[0-9]{3}$")
_ENGLISH_HEADING = re.compile(r"(?im)^##[ \t]+English[ \t]*$")
_CHINESE_HEADING = re.compile(r"(?im)^##[ \t]+(?:中文|Chinese)[ \t]*$")
_CONFIGURATION_FIELDS = frozenset({"provider_id", "model_id", "api_key"})
_FEEDBACK_FIELDS = frozenset(
    {
        "task_id",
        "understandable",
        "useful",
        "confidence",
        "needs_expert_review",
        "obvious_problem",
        "missing_information",
        "safety_concern",
        "clarification_useful",
        "notes",
    }
)
_WEB_PROVIDER_SPECS: Mapping[str, Mapping[str, Any]] = {
    "openai": {
        "label": "OpenAI",
        "models": (
            ("gpt-5.6-sol", "GPT-5.6 Sol"),
            ("gpt-5.6-terra", "GPT-5.6 Terra"),
            ("gpt-5.6-luna", "GPT-5.6 Luna"),
            ("gpt-5.4-mini", "GPT-5.4 mini"),
        ),
    },
    "deepseek": {
        "label": "DeepSeek",
        "models": (
            ("deepseek-v4-flash", "DeepSeek V4 Flash"),
            ("deepseek-v4-pro", "DeepSeek V4 Pro"),
        ),
    },
}


def load_prompt_translations(path: str | Path) -> dict[str, dict[str, str]]:
    source = Path(path)
    try:
        if source.stat().st_size > _MAX_TRANSLATION_BYTES:
            raise EvalV2ContractError(
                "self_pilot_translation_too_large", "Prompt 翻译文件过大。"
            )
        payload = json.loads(source.read_text(encoding="utf-8"))
    except EvalV2ContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvalV2ContractError(
            "self_pilot_translation_invalid", "无法读取 prompt 翻译文件。"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "locale",
        "translations",
    }:
        raise EvalV2ContractError(
            "self_pilot_translation_invalid", "Prompt 翻译文件顶层字段无效。"
        )
    if payload["schema_version"] != "1.0" or payload["locale"] != "zh-CN":
        raise EvalV2ContractError(
            "self_pilot_translation_invalid", "Prompt 翻译文件版本或 locale 无效。"
        )
    values = payload["translations"]
    if not isinstance(values, dict):
        raise EvalV2ContractError(
            "self_pilot_translation_invalid", "translations 必须是 JSON 对象。"
        )
    translations: dict[str, dict[str, str]] = {}
    for task_id, item in values.items():
        if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
            raise EvalV2ContractError(
                "self_pilot_translation_invalid", "翻译文件包含无效 task_id。"
            )
        if not isinstance(item, dict) or set(item) != {"source_prompt", "prompt_zh"}:
            raise EvalV2ContractError(
                "self_pilot_translation_invalid",
                f"{task_id} 的翻译字段无效。",
            )
        source_prompt = item["source_prompt"]
        prompt_zh = item["prompt_zh"]
        if (
            not isinstance(source_prompt, str)
            or not source_prompt.strip()
            or not isinstance(prompt_zh, str)
            or not prompt_zh.strip()
            or len(source_prompt) > 4000
            or len(prompt_zh) > 4000
        ):
            raise EvalV2ContractError(
                "self_pilot_translation_invalid", f"{task_id} 的翻译文本无效。"
            )
        translations[task_id] = {
            "source_prompt": source_prompt,
            "prompt_zh": prompt_zh,
        }
    return translations


def split_bilingual_output(output: str) -> dict[str, Any]:
    """Split the fixed bilingual presentation envelope without interpreting claims."""

    if output == "[OUTPUT_REDACTED_BY_SELF_PILOT_SAFETY_FILTER]":
        return {
            "english": output,
            "chinese": output,
            "bilingual_complete": True,
        }
    english_heading = _ENGLISH_HEADING.search(output)
    chinese_heading = _CHINESE_HEADING.search(output)
    if (
        english_heading is None
        or chinese_heading is None
        or english_heading.end() >= chinese_heading.start()
    ):
        return {
            "english": output,
            "chinese": "模型未按双语格式返回中文区块；请在人工评价备注中记录。",
            "bilingual_complete": False,
        }
    prefix = output[: english_heading.start()].strip()
    english_output = output[english_heading.end() : chinese_heading.start()].strip()
    chinese_output = output[chinese_heading.end() :].strip()
    if prefix:
        english_output = f"{prefix}\n{english_output}".strip()
        chinese_output = f"{prefix}\n{chinese_output}".strip()
    return {
        "english": english_output,
        "chinese": chinese_output,
        "bilingual_complete": bool(english_output and chinese_output),
    }


def feedback_field_order(outcome: str) -> tuple[str, ...]:
    """Return the fixed UI order without exposing an evaluator golden."""

    fields = [
        "understandable",
        "useful",
        "confidence",
        "needs_expert_review",
        "obvious_problem",
        "missing_information",
        "safety_concern",
    ]
    if outcome == "clarification_required":
        fields.append("clarification_useful")
    fields.append("notes")
    return tuple(fields)


def build_provider_catalog(
    binding: Mapping[str, str] | None = None,
    *,
    provider_resolver: Callable[[str], ProviderAdapter] = get_provider,
) -> list[dict[str, Any]]:
    """Expose only locally installed, controlled text/tool provider choices."""

    if not _responses_transport_available():
        raise EvalV2ContractError(
            "self_pilot_web_provider_unavailable",
            "当前环境缺少 Web self-pilot 所需的 Provider transport。",
        )
    catalog: list[dict[str, Any]] = []
    for provider_id, spec in _WEB_PROVIDER_SPECS.items():
        if binding is not None and binding["provider_id"] != provider_id:
            continue
        try:
            provider = provider_resolver(provider_id)
        except Exception:
            continue
        if (
            binding is not None
            and provider.transport_id != binding["transport_id"]
        ):
            continue
        models: list[dict[str, str]] = []
        for model_id, label in spec["models"]:
            if binding is not None and binding["model_id"] != model_id:
                continue
            try:
                provider.validate_model(model_id)
            except Exception:
                continue
            models.append({"model_id": model_id, "label": label})
        if models:
            catalog.append(
                {
                    "provider_id": provider_id,
                    "label": spec["label"],
                    "models": models,
                    "locked_to_existing_session": binding is not None,
                }
            )
    if not catalog:
        raise EvalV2ContractError(
            "self_pilot_web_provider_unavailable",
            "当前 session 没有可用的受控 Provider/model 组合。",
        )
    return catalog


def verify_provider_model_access(
    provider: ProviderAdapter,
    model_id: str,
    api_key: str,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Authenticate against the provider model catalog without a model token call."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            _verify_provider_model_access_async(
                provider, model_id, api_key, timeout_seconds
            )
        )
    raise EvalV2ContractError(
        "self_pilot_web_preflight_loop_active",
        "Provider 配置预检不能在活动 event loop 中运行。",
    )


async def _verify_provider_model_access_async(
    provider: ProviderAdapter,
    model_id: str,
    api_key: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise EvalV2ContractError(
            "self_pilot_web_provider_unavailable", "未安装 Provider client。"
        ) from exc
    client_arguments: dict[str, Any] = {
        "api_key": api_key,
        "timeout": timeout_seconds,
        "max_retries": 0,
    }
    if provider.provider_id == "deepseek":
        client_arguments["base_url"] = "https://api.deepseek.com"
    client = AsyncOpenAI(**client_arguments)
    try:
        page = await client.models.list()
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code in {401, 403}:
            code = "self_pilot_web_provider_auth_failed"
            message = "API Key 认证失败；配置未保存。"
        elif status_code == 429:
            code = "self_pilot_web_provider_preflight_rate_limited"
            message = "Provider 模型目录暂时限流；配置未保存。"
        elif isinstance(status_code, int) and status_code >= 500:
            code = "self_pilot_web_provider_preflight_unavailable"
            message = "Provider 模型目录暂时不可用；配置未保存。"
        elif "timeout" in type(exc).__name__.lower():
            code = "self_pilot_web_provider_preflight_timeout"
            message = "Provider 模型目录预检超时；配置未保存。"
        else:
            code = "self_pilot_web_provider_preflight_failed"
            message = "无法核验 Provider/model 可用性；配置未保存。"
        raise EvalV2ContractError(code, message) from exc
    finally:
        await client.close()
    model_ids = {
        item.id
        for item in getattr(page, "data", ())
        if isinstance(getattr(item, "id", None), str)
    }
    if model_id not in model_ids:
        raise EvalV2ContractError(
            "self_pilot_web_model_unavailable",
            "所选模型未出现在该 API Key 可用的模型目录中；配置未保存。",
        )
    return {
        "status": "verified",
        "provider_id": provider.provider_id,
        "model_id": model_id,
        "verification_method": "provider_models_list",
        "network_calls": 1,
        "model_token_calls": 0,
    }


def _responses_transport_available() -> bool:
    try:
        from agents import OpenAIResponsesModel  # noqa: F401
        from openai import AsyncOpenAI  # noqa: F401
    except ImportError:
        return False
    return True


class SelfPilotWebController:
    """Local-only web state machine; answer text remains process memory only."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        session_directory: str | Path,
        tasks_path: str | Path,
        dataset_manifest_path: str | Path,
        registry_path: str | Path,
        translations_path: str | Path,
        confirm_online: bool,
        provider: ProviderAdapter | None = None,
        model_id: str | None = None,
        api_key: str | None = None,
        max_turns: int = 8,
        run_timeout_seconds: float = 120.0,
        sdk_runner: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        provider_resolver: Callable[[str], ProviderAdapter] = get_provider,
        availability_checker: Callable[
            [ProviderAdapter, str, str, float], Mapping[str, Any]
        ] = verify_provider_model_access,
    ) -> None:
        if confirm_online is not True:
            raise EvalV2ContractError(
                "eval_v2_online_confirmation_required",
                "Self-pilot Web 需要 --confirm-online；服务器未启动。",
            )
        self._project_root = Path(project_root).resolve()
        self._session_directory = Path(session_directory)
        self._tasks_path = Path(tasks_path)
        self._dataset_manifest_path = Path(dataset_manifest_path)
        self._registry_path = Path(registry_path)
        self._translations = load_prompt_translations(translations_path)
        self._provider_resolver = provider_resolver
        self._availability_checker = availability_checker
        self._session_binding = get_self_pilot_provider_binding(
            project_root=self._project_root,
            session_directory=self._session_directory,
        )
        self._provider_catalog = build_provider_catalog(
            self._session_binding, provider_resolver=provider_resolver
        )
        self._provider: ProviderAdapter | None = None
        self._model_id: str | None = None
        self._api_key: str | None = None
        self._availability_verified = False
        self._max_turns = max_turns
        self._run_timeout_seconds = run_timeout_seconds
        self._sdk_runner = sdk_runner
        self._clock = clock
        self._lock = threading.Lock()
        self._active_task_id: str | None = None
        self._answers: dict[str, dict[str, Any]] = {}
        self._timer_started: dict[str, float] = {}
        # Fail before binding a port if any selected prompt lacks an exact translation.
        for task in get_self_pilot_blinded_tasks(
            project_root=self._project_root,
            session_directory=self._session_directory,
        ):
            self._localized_task(task)
        supplied = (provider is not None, model_id is not None, api_key is not None)
        if any(supplied) and not all(supplied):
            raise EvalV2ContractError(
                "self_pilot_web_configuration_invalid",
                "预配置必须同时提供 provider、model 和 API Key。",
            )
        if provider is not None and model_id is not None and api_key is not None:
            normalized_model = self._validate_catalog_selection(
                provider.provider_id, model_id, provider=provider
            )
            self._provider = provider
            self._model_id = normalized_model
            self._api_key = _normalize_api_key(api_key)
            self._availability_verified = True
        self.state()

    def __repr__(self) -> str:
        provider_id = self._provider.provider_id if self._provider is not None else None
        return (
            "SelfPilotWebController("
            f"provider={provider_id!r}, model={self._model_id!r}, api_key=[REDACTED])"
        )

    def state(self) -> dict[str, Any]:
        current = get_next_self_pilot_task(
            project_root=self._project_root,
            session_directory=self._session_directory,
        )
        progress = get_self_pilot_progress(
            project_root=self._project_root,
            session_directory=self._session_directory,
        )
        if current["status"] == "complete":
            return {
                "schema_version": "1.0",
                "web_version": SELF_PILOT_WEB_VERSION,
                "status": "complete",
                "progress": progress,
                "message": current["message"],
                "summary": summarize_self_pilot(
                    project_root=self._project_root,
                    session_directory=self._session_directory,
                ),
            }
        with self._lock:
            configured = (
                self._provider is not None
                and self._model_id is not None
                and self._api_key is not None
                and self._availability_verified
            )
            configured_provider = (
                self._provider.provider_id if self._provider is not None else None
            )
            configured_model = self._model_id
        if not configured:
            return {
                "schema_version": "1.0",
                "web_version": SELF_PILOT_WEB_VERSION,
                "status": "configuration_required",
                "progress": progress,
                "provider_catalog": self._provider_catalog,
                "session_provider_binding": self._session_binding,
                "availability_preflight_required": True,
                "message": "请选择 Provider/model 并输入 API Key。",
            }
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "web_version": SELF_PILOT_WEB_VERSION,
            "status": current["status"],
            "progress": progress,
            "configuration": {
                "provider_id": configured_provider,
                "model_id": configured_model,
                "availability_verified": True,
            },
        }
        if current["status"] == "pending_provider_run":
            task_id = current["task"]["task_id"]
            with self._lock:
                active = self._active_task_id == task_id
                started = self._timer_started.get(task_id)
            payload.update(
                {
                    "sequence": current["sequence"],
                    "task": self._localized_task(current["task"]),
                    "timer_running": active and started is not None,
                    "elapsed_seconds": (
                        max(0.0, self._clock() - started)
                        if active and started is not None
                        else None
                    ),
                    "provider_run_in_progress": active,
                    "agent_output_available": False,
                }
            )
            return payload
        if current["status"] == "pending_human_feedback":
            task_id = current["task_id"]
            with self._lock:
                answer = self._answers.get(task_id)
                started = self._timer_started.get(task_id)
                active = self._active_task_id == task_id
            payload.update(
                {
                    "sequence": current["sequence"],
                    "task_id": task_id,
                    "timer_running": started is not None,
                    "elapsed_seconds": (
                        max(0.0, self._clock() - started)
                        if started is not None
                        else None
                    ),
                    "provider_run_in_progress": active,
                    "agent_output_available": answer is not None,
                    "task": answer.get("task") if answer is not None else None,
                    "answer": answer,
                    "message": (
                        current["message"]
                        if answer is not None
                        else "该题答案正文不在当前 Web 进程内存中；请使用原终端完成反馈。"
                    ),
                }
            )
            return payload
        raise EvalV2ContractError(
            "self_pilot_web_state_invalid", "Self-pilot Web 状态无效。"
        )

    def configure(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != _CONFIGURATION_FIELDS:
            raise EvalV2ContractError(
                "self_pilot_web_configuration_invalid",
                "Provider 配置字段不完整或包含未知字段。",
            )
        provider_id = payload["provider_id"]
        model_id = payload["model_id"]
        if not isinstance(provider_id, str) or not isinstance(model_id, str):
            raise EvalV2ContractError(
                "self_pilot_web_configuration_invalid",
                "Provider 和 model 必须从受控列表选择。",
            )
        api_key = _normalize_api_key(payload["api_key"])
        with self._lock:
            if self._provider is not None or self._active_task_id is not None:
                raise EvalV2ContractError(
                    "self_pilot_web_configuration_locked",
                    "当前 Web 进程已经完成 Provider 配置。",
                )
        if not any(
            item["provider_id"] == provider_id
            and model_id in {model["model_id"] for model in item["models"]}
            for item in self._provider_catalog
        ):
            raise EvalV2ContractError(
                "self_pilot_web_configuration_invalid",
                "Provider/model 不在当前 session 的受控可用列表中。",
            )
        try:
            provider = self._provider_resolver(provider_id)
        except Exception as exc:
            raise EvalV2ContractError(
                "self_pilot_web_provider_unavailable",
                "无法加载所选 Provider adapter。",
            ) from exc
        normalized_model = self._validate_catalog_selection(
            provider_id, model_id, provider=provider
        )
        verification = self._availability_checker(
            provider, normalized_model, api_key, 20.0
        )
        if (
            not isinstance(verification, Mapping)
            or verification.get("status") != "verified"
            or verification.get("provider_id") != provider_id
            or verification.get("model_id") != normalized_model
        ):
            raise EvalV2ContractError(
                "self_pilot_web_provider_preflight_invalid",
                "Provider 可用性预检返回无效结果；配置未保存。",
            )
        with self._lock:
            if self._provider is not None:
                raise EvalV2ContractError(
                    "self_pilot_web_configuration_locked",
                    "当前 Web 进程已经完成 Provider 配置。",
                )
            self._provider = provider
            self._model_id = normalized_model
            self._api_key = api_key
            self._availability_verified = True
        return {
            "status": "configured",
            "verification": {
                "status": "verified",
                "provider_id": provider_id,
                "model_id": normalized_model,
                "verification_method": verification.get("verification_method"),
                "network_calls": verification.get("network_calls"),
                "model_token_calls": verification.get("model_token_calls"),
            },
            "next": self.state(),
        }

    def run_current_task(self) -> dict[str, Any]:
        with self._lock:
            provider = self._provider
            model_id = self._model_id
            api_key = self._api_key
            verified = self._availability_verified
        if provider is None or model_id is None or api_key is None or not verified:
            raise EvalV2ContractError(
                "self_pilot_web_configuration_required",
                "必须先在网页中完成 Provider/model/API Key 可用性预检。",
            )
        current = get_next_self_pilot_task(
            project_root=self._project_root,
            session_directory=self._session_directory,
        )
        if current["status"] != "pending_provider_run":
            raise EvalV2ContractError(
                "self_pilot_web_task_not_runnable", "当前没有可运行的题目。"
            )
        task_id = current["task"]["task_id"]
        localized_task = self._localized_task(current["task"])
        with self._lock:
            if self._active_task_id is not None:
                raise EvalV2ContractError(
                    "self_pilot_web_run_in_progress", "已有 Provider 任务正在运行。"
                )
            self._active_task_id = task_id
            self._timer_started[task_id] = self._clock()
        try:
            result = run_self_pilot_task(
                project_root=self._project_root,
                session_directory=self._session_directory,
                tasks_path=self._tasks_path,
                dataset_manifest_path=self._dataset_manifest_path,
                registry_path=self._registry_path,
                provider=provider,
                model_id=model_id,
                api_key=api_key,
                task_id=task_id,
                confirm_online=True,
                max_turns=self._max_turns,
                run_timeout_seconds=self._run_timeout_seconds,
                sdk_runner=self._sdk_runner,
                bilingual_output=True,
                max_output_tokens=SELF_PILOT_WEB_MAX_OUTPUT_TOKENS,
            )
            sections = split_bilingual_output(result["agent_output"])
            answer = {
                "task_id": task_id,
                "task": localized_task,
                "english": sections["english"],
                "chinese": sections["chinese"],
                "bilingual_complete": sections["bilingual_complete"],
                "outcome": result["outcome"],
                "provider": result["provider"],
                "output_redacted": result["output_redacted"],
                "machine_score_hidden_until_feedback": True,
                "feedback_fields": list(feedback_field_order(result["outcome"])),
            }
            with self._lock:
                self._answers[task_id] = answer
        except Exception:
            with self._lock:
                self._timer_started.pop(task_id, None)
                self._active_task_id = None
            raise
        with self._lock:
            self._active_task_id = None
        return self.state()

    def record_feedback(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != _FEEDBACK_FIELDS:
            raise EvalV2ContractError(
                "self_pilot_web_feedback_invalid", "人工反馈字段不完整或包含未知字段。"
            )
        task_id = payload["task_id"]
        if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
            raise EvalV2ContractError(
                "self_pilot_web_feedback_invalid", "task_id 无效。"
            )
        understandable = _require_bool(payload["understandable"], "understandable")
        useful = _require_bool(payload["useful"], "useful")
        needs_expert_review = _require_bool(
            payload["needs_expert_review"], "needs_expert_review"
        )
        obvious_problem = _require_bool(
            payload["obvious_problem"], "obvious_problem"
        )
        missing_information = _require_bool(
            payload["missing_information"], "missing_information"
        )
        safety_concern = _require_bool(payload["safety_concern"], "safety_concern")
        confidence = payload["confidence"]
        if confidence not in {"low", "medium", "high"}:
            raise EvalV2ContractError(
                "self_pilot_web_feedback_invalid",
                "confidence 必须是 low、medium 或 high。",
            )
        clarification = payload["clarification_useful"]
        notes = payload["notes"]
        if notes is not None and not isinstance(notes, str):
            raise EvalV2ContractError(
                "self_pilot_web_feedback_invalid", "notes 必须是文本或 null。"
            )
        with self._lock:
            answer = self._answers.get(task_id)
            started = self._timer_started.get(task_id)
            active = self._active_task_id
        if active is not None:
            raise EvalV2ContractError(
                "self_pilot_web_run_in_progress", "Provider 任务尚未结束。"
            )
        if answer is None or started is None:
            raise EvalV2ContractError(
                "self_pilot_web_answer_unavailable",
                "当前 Web 进程没有该题答案，不能代替用户完成评价。",
            )
        clarification_expected = answer["outcome"] == "clarification_required"
        if clarification_expected and not isinstance(clarification, bool):
            raise EvalV2ContractError(
                "self_pilot_web_feedback_invalid",
                "澄清类回答必须选择 clarification_useful=yes/no。",
            )
        if not clarification_expected and clarification is not None:
            raise EvalV2ContractError(
                "self_pilot_web_feedback_invalid",
                "非澄清类回答不得填写 clarification_useful。",
            )
        duration_seconds = max(0.001, self._clock() - started)
        recorded = record_self_pilot_usability_feedback(
            project_root=self._project_root,
            session_directory=self._session_directory,
            task_id=task_id,
            understandable=understandable,
            useful=useful,
            confidence=confidence,
            needs_expert_review=needs_expert_review,
            obvious_problem=obvious_problem,
            missing_information=missing_information,
            duration_seconds=duration_seconds,
            safety_concern=safety_concern,
            clarification_useful=clarification,
            notes=notes,
        )
        with self._lock:
            self._answers.pop(task_id, None)
            self._timer_started.pop(task_id, None)
        return {
            "status": "recorded",
            "task_id": task_id,
            "duration_seconds": duration_seconds,
            "machine_pass": recorded["machine_pass"],
            "machine_failures": recorded["machine_failures"],
            "feedback_schema": recorded["feedback_schema"],
            "professional_correctness_assessed": False,
            "next": self.state(),
        }

    def summary(self) -> dict[str, Any]:
        return summarize_self_pilot(
            project_root=self._project_root,
            session_directory=self._session_directory,
        )

    def _localized_task(self, task: Mapping[str, Any]) -> dict[str, Any]:
        task_id = task.get("task_id")
        prompt = task.get("prompt")
        context = task.get("context")
        translation = self._translations.get(task_id) if isinstance(task_id, str) else None
        if (
            translation is None
            or not isinstance(prompt, str)
            or translation["source_prompt"] != prompt
        ):
            raise EvalV2ContractError(
                "self_pilot_translation_missing",
                f"当前题 {task_id!r} 缺少与英文原文精确绑定的中文翻译。",
            )
        return {
            "task_id": task_id,
            "prompt_en": prompt,
            "prompt_zh": translation["prompt_zh"],
            "context": dict(context) if isinstance(context, Mapping) else {},
        }

    def _validate_catalog_selection(
        self,
        provider_id: str,
        model_id: str,
        *,
        provider: ProviderAdapter,
    ) -> str:
        entry = next(
            (
                item
                for item in self._provider_catalog
                if item["provider_id"] == provider_id
            ),
            None,
        )
        allowed_models = (
            {item["model_id"] for item in entry["models"]}
            if entry is not None
            else set()
        )
        if model_id not in allowed_models:
            raise EvalV2ContractError(
                "self_pilot_web_configuration_invalid",
                "Provider/model 不在当前 session 的受控可用列表中。",
            )
        normalized = provider.validate_model(model_id)
        if normalized != model_id:
            raise EvalV2ContractError(
                "self_pilot_web_configuration_invalid", "Model ID 规范化结果无效。"
            )
        return normalized


def serve_self_pilot_web(
    controller: SelfPilotWebController, *, port: int = 8765
) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise EvalV2ContractError(
            "self_pilot_web_port_invalid", "Web 端口必须在 1 到 65535 之间。"
        )
    csrf_token = secrets.token_urlsafe(32)
    handler = _build_handler(controller, csrf_token, port)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        raise EvalV2ContractError(
            "self_pilot_web_bind_failed",
            "无法绑定本机 Web 端口；请更换 --port。",
        ) from exc
    server.daemon_threads = True
    print(f"ResearchOps self-pilot Web: http://127.0.0.1:{port}")
    print("仅绑定本机；按 Ctrl+C 停止。模型正文只保留在当前进程内存。")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _build_handler(
    controller: SelfPilotWebController, csrf_token: str, port: int
) -> type[BaseHTTPRequestHandler]:
    expected_origins = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    }

    class SelfPilotHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            try:
                self._require_local_host()
                path = urlsplit(self.path).path
                if path == "/":
                    page = _HTML.replace(
                        "__RESEARCHOPS_TOKEN__", html.escape(csrf_token, quote=True)
                    )
                    self._send_bytes(200, page.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/app.css":
                    self._send_bytes(200, _CSS.encode("utf-8"), "text/css; charset=utf-8")
                elif path == "/app.js":
                    self._send_bytes(
                        200, _JAVASCRIPT.encode("utf-8"), "text/javascript; charset=utf-8"
                    )
                elif path == "/api/state":
                    self._send_json(200, controller.state())
                elif path == "/api/summary":
                    self._send_json(200, controller.summary())
                else:
                    self._send_json(404, _error("self_pilot_web_not_found", "页面不存在。"))
            except EvalV2ContractError as exc:
                self._send_json(400, exc.to_dict())
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                self._send_json(
                    500,
                    _error(
                        "self_pilot_web_internal_error",
                        "Web 请求失败；未记录异常正文。",
                    ),
                )

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            try:
                self._require_local_host()
                path = urlsplit(self.path).path
                payload = self._read_json_body()
                self._require_post_authorization(expected_origins, csrf_token)
                if path == "/api/configure":
                    result = controller.configure(payload)
                elif path == "/api/run":
                    if payload:
                        raise EvalV2ContractError(
                            "self_pilot_web_request_invalid", "run 请求不接受字段。"
                        )
                    result = controller.run_current_task()
                elif path == "/api/feedback":
                    result = controller.record_feedback(payload)
                else:
                    self._send_json(
                        404, _error("self_pilot_web_not_found", "接口不存在。")
                    )
                    return
                self._send_json(200, result)
            except EvalV2ContractError as exc:
                self._send_json(400, exc.to_dict())
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                self._send_json(
                    500,
                    _error(
                        "self_pilot_web_internal_error",
                        "Web 请求失败；未记录异常正文。",
                    ),
                )

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

        def _require_local_host(self) -> None:
            host = self.headers.get("Host", "")
            hostname = host.rsplit(":", 1)[0].strip("[]").lower()
            if hostname not in {"127.0.0.1", "localhost"}:
                raise EvalV2ContractError(
                    "self_pilot_web_host_denied", "只接受本机 Host。"
                )

        def _require_post_authorization(
            self, allowed_origins: set[str], expected_token: str
        ) -> None:
            if self.headers.get("X-ResearchOps-Token") != expected_token:
                raise EvalV2ContractError(
                    "self_pilot_web_csrf_denied", "Web 请求令牌无效。"
                )
            origin = self.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                raise EvalV2ContractError(
                    "self_pilot_web_origin_denied", "Web 请求来源无效。"
                )

        def _read_json_body(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or "0")
            except ValueError as exc:
                raise EvalV2ContractError(
                    "self_pilot_web_request_invalid", "Content-Length 无效。"
                ) from exc
            if length < 0 or length > _MAX_JSON_BODY_BYTES:
                raise EvalV2ContractError(
                    "self_pilot_web_request_invalid", "请求正文过大。"
                )
            try:
                value = json.loads(self.rfile.read(length) or b"{}")
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise EvalV2ContractError(
                    "self_pilot_web_request_invalid", "请求 JSON 无效。"
                ) from exc
            if not isinstance(value, dict):
                raise EvalV2ContractError(
                    "self_pilot_web_request_invalid", "请求 JSON 必须是对象。"
                )
            return value

        def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._send_bytes(status, body, "application/json; charset=utf-8")

        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'none'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

    return SelfPilotHandler


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise EvalV2ContractError(
            "self_pilot_web_feedback_invalid", f"{name} 必须是布尔值。"
        )
    return value


def _normalize_api_key(value: Any) -> str:
    if not isinstance(value, str):
        raise EvalV2ContractError(
            "self_pilot_web_key_invalid", "API Key 必须是非空文本。"
        )
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 4096
        or any(character.isspace() or character == "\x00" for character in normalized)
    ):
        raise EvalV2ContractError(
            "self_pilot_web_key_invalid",
            "API Key 为空、过长或包含空白/控制字符。",
        )
    return normalized


def _error(code: str, message: str) -> dict[str, str]:
    return {"status": "error", "error_code": code, "message": message}


_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="researchops-token" content="__RESEARCHOPS_TOKEN__">
  <title>ResearchOps Self-pilot</title>
  <link rel="stylesheet" href="/app.css">
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div>
        <p class="eyebrow">RESEARCHOPS · INTERNAL SELF-PILOT</p>
        <h1>双语人工评测台</h1>
        <p class="subtitle">Bilingual human evaluation console</p>
      </div>
      <div class="status-group">
        <span id="progress" class="pill">载入中</span>
        <span id="timer" class="timer">00:00.0</span>
      </div>
    </header>
    <section id="notice" class="notice" hidden></section>
    <section id="workspace" aria-live="polite"></section>
  </main>
  <script src="/app.js" defer></script>
</body>
</html>
"""


_CSS = r"""
:root {
  color-scheme: light;
  --ink: #10211b;
  --muted: #5c6c65;
  --paper: #f5f3ec;
  --card: #fffdf8;
  --line: #d9ddd5;
  --accent: #16745a;
  --accent-dark: #0e523f;
  --warm: #f0b35b;
  --danger: #a63c32;
  font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); }
body::before {
  content: ""; position: fixed; inset: 0 0 auto 0; height: 7px;
  background: linear-gradient(90deg, var(--accent), #54a68d 62%, var(--warm));
}
.shell { width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 42px 0 72px; }
.topbar { display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; }
.eyebrow { margin: 0 0 8px; color: var(--accent); font-size: 12px; letter-spacing: .16em; font-weight: 800; }
h1 { margin: 0; font: 700 clamp(30px, 4vw, 52px)/1.05 Georgia, "Noto Serif SC", serif; }
.subtitle { color: var(--muted); margin: 8px 0 0; }
.status-group { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
.pill, .timer { border: 1px solid var(--line); background: var(--card); padding: 9px 13px; border-radius: 999px; font-weight: 700; }
.timer { font-variant-numeric: tabular-nums; min-width: 94px; text-align: center; }
.timer.running { color: var(--accent-dark); border-color: #86b8a9; box-shadow: 0 0 0 3px #dcefe8; }
.notice { margin-top: 24px; padding: 14px 16px; border-left: 4px solid var(--warm); background: #fff7e8; border-radius: 8px; }
.notice.error { border-color: var(--danger); background: #fff0ed; color: #74261f; }
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; margin-top: 28px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 24px; box-shadow: 0 16px 34px rgba(41, 56, 49, .06); }
.card h2 { margin: 0 0 14px; font-size: 15px; text-transform: uppercase; letter-spacing: .08em; color: var(--accent-dark); }
.prompt, .answer { white-space: pre-wrap; line-height: 1.72; font-size: 16px; overflow-wrap: anywhere; }
.markdown-body { white-space: normal; line-height: 1.72; font-size: 16px; overflow-wrap: anywhere; }
.markdown-body > :first-child { margin-top: 0; }
.markdown-body > :last-child { margin-bottom: 0; }
.markdown-body h1, .markdown-body h2, .markdown-body h3, .markdown-body h4, .markdown-body h5, .markdown-body h6 { margin: 1.2em 0 .55em; color: var(--ink); line-height: 1.3; text-transform: none; letter-spacing: 0; }
.markdown-body h1 { font-size: 1.65rem; }
.markdown-body h2 { font-size: 1.4rem; }
.markdown-body h3 { font-size: 1.2rem; }
.markdown-body p { margin: .65em 0; }
.markdown-body ul, .markdown-body ol { margin: .65em 0; padding-left: 1.55em; }
.markdown-body li { margin: .28em 0; }
.markdown-body blockquote { margin: .8em 0; padding: .2em 1em; color: var(--muted); border-left: 4px solid #9ec6b8; background: #f3f8f5; }
.markdown-body code { padding: .12em .38em; border-radius: 5px; background: #edf1ee; font: .9em Consolas, "SFMono-Regular", monospace; }
.markdown-body pre { overflow-x: auto; margin: .9em 0; padding: 14px; border-radius: 10px; color: #e8f2ed; background: #14251f; }
.markdown-body pre code { padding: 0; color: inherit; background: transparent; }
.markdown-body a { color: var(--accent-dark); text-decoration-thickness: 1px; text-underline-offset: 3px; }
.markdown-body hr { border: 0; border-top: 1px solid var(--line); margin: 1.1em 0; }
.markdown-table-wrap { overflow-x: auto; margin: .9em 0; }
.markdown-body table { width: 100%; border-collapse: collapse; font-size: .94em; }
.markdown-body th, .markdown-body td { padding: 9px 10px; border: 1px solid var(--line); text-align: left; vertical-align: top; }
.markdown-body th { background: #edf5f1; }
.context { margin-top: 18px; color: var(--muted); font: 13px/1.6 Consolas, monospace; }
.action-panel { margin-top: 18px; text-align: center; padding: 30px; }
button { font: inherit; cursor: pointer; }
.primary { border: 0; border-radius: 12px; color: white; background: var(--accent); padding: 13px 22px; font-weight: 800; box-shadow: 0 8px 18px rgba(22, 116, 90, .2); }
.primary:hover { background: var(--accent-dark); }
.primary:disabled { cursor: wait; opacity: .6; }
.hint { color: var(--muted); font-size: 13px; margin: 12px 0 0; }
.review { margin-top: 20px; }
.review h2 { margin-top: 0; }
.role-guidance { margin: 0 0 18px; padding: 16px; border-radius: 12px; background: #eef5f1; color: #274c40; line-height: 1.7; }
.role-guidance strong { display: block; margin-bottom: 5px; }
.form-grid { display: grid; grid-template-columns: 1fr; gap: 16px; max-width: 860px; }
fieldset { border: 1px solid var(--line); border-radius: 12px; padding: 13px; }
legend, label { font-weight: 700; }
.choice { display: inline-flex; position: relative; margin: 8px 10px 0 0; font-weight: 700; }
.choice input { position: absolute; opacity: 0; pointer-events: none; }
.choice span { min-width: 92px; padding: 9px 16px; border: 1px solid #aebbb3; border-radius: 999px; text-align: center; background: white; transition: .15s ease; }
.choice input:checked + span { color: white; background: var(--accent); border-color: var(--accent); box-shadow: 0 0 0 3px #dcefe8; }
.choice input:focus-visible + span { outline: 3px solid var(--warm); outline-offset: 2px; }
.field { display: flex; flex-direction: column; gap: 7px; }
input[type="number"], input[type="password"], select, textarea { width: 100%; border: 1px solid #bbc5be; border-radius: 9px; padding: 10px; background: white; font: inherit; }
textarea { min-height: 90px; resize: vertical; }
.wide { grid-column: 1 / -1; }
.configuration { margin-top: 28px; max-width: 760px; }
.configuration h2 { font-size: 22px; text-transform: none; letter-spacing: 0; }
.config-grid { display: grid; gap: 17px; }
.security-note { color: var(--muted); font-size: 13px; line-height: 1.65; padding: 12px 14px; background: #eef5f1; border-radius: 10px; }
.submit-row { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-top: 18px; }
.complete { margin-top: 30px; text-align: center; padding: 48px; }
.complete strong { display: block; font: 700 34px Georgia, serif; margin-bottom: 12px; }
@media (max-width: 780px) {
  .topbar { flex-direction: column; }
  .status-group { justify-content: flex-start; }
  .grid, .form-grid { grid-template-columns: 1fr; }
  .wide { grid-column: auto; }
  .shell { width: min(100% - 20px, 1180px); padding-top: 30px; }
}
"""


_JAVASCRIPT = r"""
const token = document.querySelector('meta[name="researchops-token"]').content;
const workspace = document.getElementById('workspace');
const progress = document.getElementById('progress');
const timer = document.getElementById('timer');
const notice = document.getElementById('notice');
let timerHandle = null;
let elapsedBase = 0;
let timerAnchor = 0;

async function api(path, options = {}) {
  const headers = {'Content-Type': 'application/json', ...(options.headers || {})};
  if ((options.method || 'GET') === 'POST') headers['X-ResearchOps-Token'] = token;
  const response = await fetch(path, {...options, headers, cache: 'no-store'});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.message || payload.error_code || 'Request failed');
  return payload;
}

function setNotice(message, isError = false) {
  notice.hidden = !message;
  notice.textContent = message || '';
  notice.className = isError ? 'notice error' : 'notice';
}

function formatTime(seconds) {
  const safe = Math.max(0, seconds || 0);
  const minutes = Math.floor(safe / 60).toString().padStart(2, '0');
  const remainder = (safe % 60).toFixed(1).padStart(4, '0');
  return `${minutes}:${remainder}`;
}

function startTimer(initialSeconds = 0) {
  stopTimer(false);
  elapsedBase = initialSeconds;
  timerAnchor = performance.now();
  timer.classList.add('running');
  const tick = () => { timer.textContent = formatTime(elapsedBase + (performance.now() - timerAnchor) / 1000); };
  tick();
  timerHandle = setInterval(tick, 100);
}

function stopTimer(reset = false) {
  if (timerHandle !== null) clearInterval(timerHandle);
  timerHandle = null;
  timer.classList.remove('running');
  if (reset) timer.textContent = '00:00.0';
}

function textBlock(className, text) {
  const node = document.createElement('div');
  node.className = className;
  node.textContent = text;
  return node;
}

function appendInlineMarkdown(parent, text) {
  const tokenPattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_|\[[^\]\n]+\]\([^\s)]+\))/g;
  let cursor = 0;
  for (const match of text.matchAll(tokenPattern)) {
    if (match.index > cursor) parent.appendChild(document.createTextNode(text.slice(cursor, match.index)));
    const tokenValue = match[0];
    let node;
    if (tokenValue.startsWith('`')) {
      node = document.createElement('code');
      node.textContent = tokenValue.slice(1, -1);
    } else if (tokenValue.startsWith('**') || tokenValue.startsWith('__')) {
      node = document.createElement('strong');
      node.textContent = tokenValue.slice(2, -2);
    } else if (tokenValue.startsWith('*') || tokenValue.startsWith('_')) {
      node = document.createElement('em');
      node.textContent = tokenValue.slice(1, -1);
    } else {
      const splitAt = tokenValue.lastIndexOf('](');
      const label = tokenValue.slice(1, splitAt);
      const target = tokenValue.slice(splitAt + 2, -1);
      try {
        const parsed = new URL(target, window.location.href);
        if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('blocked');
        node = document.createElement('a');
        node.href = parsed.href; node.target = '_blank'; node.rel = 'noopener noreferrer';
        node.textContent = label;
      } catch (_) {
        node = document.createTextNode(label);
      }
    }
    parent.appendChild(node);
    cursor = match.index + tokenValue.length;
  }
  if (cursor < text.length) parent.appendChild(document.createTextNode(text.slice(cursor)));
}

function tableCells(line) {
  const trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  return trimmed.split('|').map(cell => cell.trim());
}

function isTableSeparator(line) {
  const cells = tableCells(line);
  return cells.length > 0 && cells.every(cell => /^:?-{3,}:?$/.test(cell));
}

function isMarkdownBlockStart(lines, index) {
  const line = lines[index] || '';
  return /^```/.test(line) || /^#{1,6}\s+/.test(line) || /^>\s?/.test(line)
    || /^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)
    || /^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)
    || (index + 1 < lines.length && line.includes('|') && isTableSeparator(lines[index + 1]));
}

function renderSafeMarkdown(markdown, className = 'answer') {
  const root = document.createElement('div');
  root.className = `${className} markdown-body`;
  const lines = String(markdown || '').replace(/\r\n?/g, '\n').split('\n');
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }

    const fence = line.match(/^```([A-Za-z0-9_+-]*)\s*$/);
    if (fence) {
      const codeLines = []; index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]); index += 1;
      }
      if (index < lines.length) index += 1;
      const pre = document.createElement('pre');
      const code = document.createElement('code');
      if (fence[1]) code.dataset.language = fence[1];
      code.textContent = codeLines.join('\n'); pre.appendChild(code); root.appendChild(pre);
      continue;
    }

    if (index + 1 < lines.length && line.includes('|') && isTableSeparator(lines[index + 1])) {
      const wrapper = document.createElement('div'); wrapper.className = 'markdown-table-wrap';
      const table = document.createElement('table'); const thead = document.createElement('thead');
      const headerRow = document.createElement('tr');
      tableCells(line).forEach(value => { const th = document.createElement('th'); appendInlineMarkdown(th, value); headerRow.appendChild(th); });
      thead.appendChild(headerRow); table.appendChild(thead); index += 2;
      const tbody = document.createElement('tbody');
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        const row = document.createElement('tr');
        tableCells(lines[index]).forEach(value => { const td = document.createElement('td'); appendInlineMarkdown(td, value); row.appendChild(td); });
        tbody.appendChild(row); index += 1;
      }
      table.appendChild(tbody); wrapper.appendChild(table); root.appendChild(wrapper); continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const element = document.createElement(`h${heading[1].length}`);
      appendInlineMarkdown(element, heading[2]); root.appendChild(element); index += 1; continue;
    }
    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      root.appendChild(document.createElement('hr')); index += 1; continue;
    }
    if (/^>\s?/.test(line)) {
      const quote = document.createElement('blockquote'); const values = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) { values.push(lines[index].replace(/^>\s?/, '')); index += 1; }
      appendInlineMarkdown(quote, values.join(' ')); root.appendChild(quote); continue;
    }
    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      const list = document.createElement(unordered ? 'ul' : 'ol');
      const pattern = unordered ? /^\s*[-*+]\s+(.+)$/ : /^\s*\d+[.)]\s+(.+)$/;
      while (index < lines.length) {
        const itemMatch = lines[index].match(pattern); if (!itemMatch) break;
        const item = document.createElement('li'); appendInlineMarkdown(item, itemMatch[1]);
        list.appendChild(item); index += 1;
      }
      root.appendChild(list); continue;
    }

    const paragraphLines = [line]; index += 1;
    while (index < lines.length && lines[index].trim() && !isMarkdownBlockStart(lines, index)) {
      paragraphLines.push(lines[index]); index += 1;
    }
    const paragraph = document.createElement('p');
    appendInlineMarkdown(paragraph, paragraphLines.join(' ')); root.appendChild(paragraph);
  }
  return root;
}

function languageCard(title, className, text, markdown = false) {
  const card = document.createElement('article');
  card.className = 'card';
  const heading = document.createElement('h2');
  heading.textContent = title;
  card.append(heading, markdown ? renderSafeMarkdown(text, className) : textBlock(className, text));
  return card;
}

function renderConfiguration(state) {
  stopTimer(true);
  const card = document.createElement('section');
  card.className = 'card configuration';
  const heading = document.createElement('h2');
  heading.textContent = '开始前配置 · Provider setup';
  const intro = document.createElement('p');
  intro.className = 'hint';
  intro.textContent = 'Provider、模型和 API Key 都是必填项。提交后先核验 Key 认证及模型目录，不运行 Agent、不消耗模型 token。';
  const form = document.createElement('form');
  form.autocomplete = 'off';
  const grid = document.createElement('div');
  grid.className = 'config-grid';

  const providerLabel = document.createElement('label');
  providerLabel.className = 'field';
  providerLabel.appendChild(document.createTextNode('Provider · 服务商'));
  const providerSelect = document.createElement('select');
  providerSelect.name = 'provider_id'; providerSelect.required = true;
  providerLabel.appendChild(providerSelect);

  const modelLabel = document.createElement('label');
  modelLabel.className = 'field';
  modelLabel.appendChild(document.createTextNode('Model · 模型'));
  const modelSelect = document.createElement('select');
  modelSelect.name = 'model_id'; modelSelect.required = true;
  modelLabel.appendChild(modelSelect);

  const keyLabel = document.createElement('label');
  keyLabel.className = 'field';
  keyLabel.appendChild(document.createTextNode('API Key · 仅用于当前本机进程'));
  const keyInput = document.createElement('input');
  keyInput.type = 'password'; keyInput.name = 'api_key'; keyInput.required = true;
  keyInput.autocomplete = 'new-password'; keyInput.spellcheck = false;
  keyInput.placeholder = '在此粘贴，不要写入文件或 notes';
  keyLabel.appendChild(keyInput);

  const updateModels = () => {
    modelSelect.replaceChildren();
    const selected = state.provider_catalog.find(item => item.provider_id === providerSelect.value);
    (selected ? selected.models : []).forEach(model => {
      const option = document.createElement('option');
      option.value = model.model_id; option.textContent = `${model.label} · ${model.model_id}`;
      modelSelect.appendChild(option);
    });
  };
  state.provider_catalog.forEach(provider => {
    const option = document.createElement('option');
    option.value = provider.provider_id; option.textContent = `${provider.label} · ${provider.provider_id}`;
    providerSelect.appendChild(option);
  });
  providerSelect.addEventListener('change', updateModels);
  updateModels();

  const security = document.createElement('p');
  security.className = 'security-note';
  security.textContent = state.session_provider_binding
    ? '当前 session 已有历史运行，因此 Provider 和模型已锁定为同一组合。API Key 只在提交时通过本机回环地址发送，并仅驻留服务器内存。'
    : 'API Key 只通过 127.0.0.1 发送，不写入 HTML、localStorage、session、日志或错误。模型目录预检会联网，但不会生成模型 token。';

  const submit = document.createElement('button');
  submit.type = 'submit'; submit.className = 'primary';
  submit.textContent = '核验配置并进入评测 · Verify & continue';
  grid.append(providerLabel, modelLabel, keyLabel, security, submit);
  form.appendChild(grid); card.append(heading, intro, form);

  form.addEventListener('submit', async event => {
    event.preventDefault(); submit.disabled = true;
    submit.textContent = '正在核验 Key 与模型目录…';
    let requestPayload = {
      provider_id: providerSelect.value,
      model_id: modelSelect.value,
      api_key: keyInput.value
    };
    let requestBody = JSON.stringify(requestPayload);
    keyInput.value = '';
    requestPayload.api_key = '';
    try {
      const result = await api('/api/configure', {method: 'POST', body: requestBody});
      requestBody = '';
      setNotice(`配置已核验：${result.verification.provider_id} / ${result.verification.model_id}。模型 token 调用：0。`);
      renderState(result.next);
    } catch (error) {
      requestBody = '';
      setNotice(error.message, true);
      submit.disabled = false;
      submit.textContent = '重新输入 Key 并核验 · Retry verification';
      keyInput.focus();
    }
  });
  workspace.append(card);
}

function renderPrompt(task) {
  const grid = document.createElement('section');
  grid.className = 'grid';
  grid.append(
    languageCard('Prompt · English', 'prompt', task.prompt_en),
    languageCard('题目 · 中文', 'prompt', task.prompt_zh)
  );
  const context = textBlock('context', `Authorized context / 授权上下文\n${JSON.stringify(task.context, null, 2)}`);
  grid.children[0].append(context);
  return grid;
}

function renderRunButton() {
  const panel = document.createElement('section');
  panel.className = 'card action-panel';
  const button = document.createElement('button');
  button.className = 'primary';
  button.textContent = '查看答案并开始计时 · Show answer & start timer';
  const hint = document.createElement('p');
  hint.className = 'hint';
  hint.textContent = '点击后会进行一次在线 Provider 调用；计时包含等待与人工评价。';
  button.addEventListener('click', async () => {
    button.disabled = true;
    button.textContent = 'Agent 正在分析…计时已开始';
    setNotice('Provider 正在运行。请保持页面开启，不要重复点击。');
    startTimer(0);
    try {
      const state = await api('/api/run', {method: 'POST', body: '{}'});
      setNotice('答案已返回。请完成独立人工评价后再提交。');
      renderState(state);
    } catch (error) {
      stopTimer(true);
      setNotice(error.message, true);
      button.disabled = false;
      button.textContent = '重试运行 · Retry';
    }
  });
  panel.append(button, hint);
  return panel;
}

function renderRunningPanel() {
  const panel = document.createElement('section');
  panel.className = 'card action-panel';
  const title = document.createElement('strong');
  title.textContent = 'Agent 正在分析… · Provider run in progress';
  const hint = document.createElement('p');
  hint.className = 'hint';
  hint.textContent = '计时已经开始；答案返回后本页会自动更新。';
  panel.append(title, hint);
  setTimeout(() => api('/api/state').then(renderState).catch(error => setNotice(error.message, true)), 1000);
  return panel;
}

function renderAnswer(state) {
  const answer = state.answer;
  workspace.append(renderPrompt(state.task));
  const grid = document.createElement('section');
  grid.className = 'grid';
  grid.append(
    languageCard('Agent output · English', 'answer', answer.english, true),
    languageCard('Agent 输出 · 中文', 'answer', answer.chinese, true)
  );
  workspace.append(grid);
  if (!answer.bilingual_complete) {
    setNotice('模型未完整遵守双语格式；请把这一点记录在人工评价备注中。', true);
  }
  workspace.append(renderFeedbackForm(state.task_id, answer.feedback_fields));
}

function choiceField(name, legendText, choices) {
  const fieldset = document.createElement('fieldset');
  const legend = document.createElement('legend');
  legend.textContent = legendText;
  fieldset.appendChild(legend);
  choices.forEach(([value, labelText]) => {
    const label = document.createElement('label');
    label.className = 'choice';
    const input = document.createElement('input');
    input.type = 'radio'; input.name = name; input.value = value; input.required = true;
    const text = document.createElement('span');
    text.textContent = labelText;
    label.append(input, text);
    fieldset.appendChild(label);
  });
  return fieldset;
}

function yesNoField(name, legendText) {
  return choiceField(name, legendText, [['yes', '是 Yes'], ['no', '否 No']]);
}

function notesField() {
  const label = document.createElement('label');
  label.className = 'field wide';
  label.appendChild(document.createTextNode('notes · 人工评述'));
  const textarea = document.createElement('textarea');
  textarea.name = 'notes'; textarea.maxLength = 2000;
  textarea.placeholder = '记录看不懂之处、是否有帮助、明显问题或需要专家确认的内容；不要填写 API Key、绝对路径或个人标识符';
  label.appendChild(textarea);
  return label;
}

function renderFeedbackForm(taskId, feedbackFields) {
  const section = document.createElement('section');
  section.className = 'card review';
  section.innerHTML = '<h2>非专家可用性评价 · Non-expert usability review</h2>';
  const guidance = document.createElement('div');
  guidance.className = 'role-guidance';
  guidance.innerHTML = '<strong>你不需要判断医学、统计或科研结论是否专业正确。</strong>只评价自己是否看得懂、是否有助于下一步，以及肉眼可确认的明显矛盾或安全问题。安全拒绝和等待人工审批本身不算错误；专业正确性留给机器合同与领域专家。';
  const form = document.createElement('form');
  const grid = document.createElement('div');
  grid.className = 'form-grid';
  grid.append(
    yesNoField('understandable', 'understandable · 你是否能理解回答表达了什么？'),
    yesNoField('useful', 'useful · 回答是否帮助你决定下一步？'),
    choiceField('confidence', 'confidence · 你对上述可用性判断有多大把握？', [
      ['low', '低 Low'], ['medium', '中 Medium'], ['high', '高 High']
    ]),
    yesNoField('needs_expert_review', 'needs-expert-review · 是否需要领域专家确认专业正确性？'),
    yesNoField('obvious_problem', 'obvious-problem · 是否发现无需专业知识也能确认的明显矛盾或错误？'),
    yesNoField('missing_information', 'missing-information · 是否感觉回答遗漏了完成下一步所需的信息？'),
    yesNoField('safety_concern', 'safety-concern · 是否实际看到泄露、越权执行或审批绕过？')
  );
  if (feedbackFields.includes('clarification_useful')) {
    grid.append(yesNoField('clarification_useful', 'clarification-useful · 这次澄清是否具体且有助于你回答？'));
  }
  grid.append(notesField());
  form.append(guidance, grid);
  const row = document.createElement('div');
  row.className = 'submit-row';
  const help = document.createElement('span');
  help.className = 'hint';
  help.textContent = '提交时服务器自动停止计时并记录实际秒数。';
  const submit = document.createElement('button');
  submit.type = 'submit'; submit.className = 'primary'; submit.textContent = '提交评价并进入下一题';
  row.append(help, submit); form.appendChild(row);
  form.addEventListener('submit', async (event) => {
    event.preventDefault(); submit.disabled = true;
    const data = new FormData(form);
    const hasClarification = feedbackFields.includes('clarification_useful');
    const clarification = data.get('clarification_useful');
    const payload = {
      task_id: taskId,
      understandable: data.get('understandable') === 'yes',
      useful: data.get('useful') === 'yes',
      confidence: data.get('confidence'),
      needs_expert_review: data.get('needs_expert_review') === 'yes',
      obvious_problem: data.get('obvious_problem') === 'yes',
      missing_information: data.get('missing_information') === 'yes',
      safety_concern: data.get('safety_concern') === 'yes',
      clarification_useful: hasClarification ? clarification === 'yes' : null,
      notes: data.get('notes') || null
    };
    try {
      const result = await api('/api/feedback', {method: 'POST', body: JSON.stringify(payload)});
      stopTimer(false);
      const machine = result.machine_pass ? '机器合同评分：通过' : `机器合同评分：未通过 (${result.machine_failures.join(', ')})`;
      setNotice(`非专家可用性评价已记录；用时 ${result.duration_seconds.toFixed(1)} 秒。${machine}`);
      setTimeout(() => renderState(result.next), 1200);
    } catch (error) {
      setNotice(error.message, true); submit.disabled = false;
    }
  });
  section.appendChild(form);
  return section;
}

function renderState(state) {
  workspace.replaceChildren();
  const p = state.progress;
  progress.textContent = `${p.feedback_completed_count}/${p.task_count} 已评价`;
  if (state.status === 'configuration_required') {
    renderConfiguration(state);
  } else if (state.status === 'pending_provider_run') {
    workspace.append(renderPrompt(state.task));
    if (state.provider_run_in_progress) {
      startTimer(state.elapsed_seconds || 0);
      workspace.append(renderRunningPanel());
    } else {
      stopTimer(true);
      workspace.append(renderRunButton());
    }
  } else if (state.status === 'pending_human_feedback') {
    if (state.timer_running) startTimer(state.elapsed_seconds || 0);
    if (state.agent_output_available) renderAnswer(state);
    else {
      const card = document.createElement('section'); card.className = 'card complete';
      card.textContent = state.message; workspace.append(card); stopTimer(true);
    }
  } else if (state.status === 'complete') {
    stopTimer(true);
    const card = document.createElement('section'); card.className = 'card complete';
    card.innerHTML = '<strong>全部完成</strong><p>All self-pilot tasks and feedback are complete.</p>';
    workspace.append(card);
  }
}

api('/api/state').then(renderState).catch(error => setNotice(error.message, true));
"""
