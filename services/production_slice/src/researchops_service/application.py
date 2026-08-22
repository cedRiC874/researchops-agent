from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

from opentelemetry import trace
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind

from .domain import (
    IdempotencyConflict,
    InspectionJob,
    InvalidIdempotencyKey,
    InvalidLogicalId,
    JobNotFound,
    JobStatus,
    ObjectOutcomeUnknown,
    ResultNotReady,
    StoredObject,
    SubmitResult,
    TransientDependencyError,
    UnsafeAggregatePayload,
)
from .ports import AggregateInspector, JobRepository, ObjectStore, WorkQueue


_LOGICAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
_FORBIDDEN_AGGREGATE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "csv",
        "file_path",
        "path",
        "raw_data",
        "raw_rows",
        "records",
        "sample_values",
        "subject_key",
    }
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def actor_hash(api_token: str) -> str:
    return hashlib.sha256(api_token.encode("utf-8")).hexdigest()


def idempotency_digest(secret: bytes, actor: str, key: str) -> str:
    if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
        raise InvalidIdempotencyKey("Idempotency-Key 格式无效。")
    return hmac.new(secret, f"{actor}:{key}".encode("utf-8"), hashlib.sha256).hexdigest()


def request_sha256(dataset_id: str) -> str:
    if not isinstance(dataset_id, str) or _LOGICAL_ID.fullmatch(dataset_id) is None:
        raise InvalidLogicalId("dataset_id 必须是受控逻辑 ID。")
    canonical = json.dumps(
        {"dataset_id": dataset_id}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class InspectionApplication:
    def __init__(
        self,
        *,
        repository: JobRepository,
        object_store: ObjectStore,
        hmac_key: bytes,
        max_attempts: int = 3,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not hmac_key:
            raise ValueError("hmac_key 不能为空。")
        self._repository = repository
        self._object_store = object_store
        self._hmac_key = hmac_key
        self._max_attempts = max_attempts
        self._clock = clock

    def submit(
        self,
        *,
        actor: str,
        raw_idempotency_key: str,
        dataset_id: str,
        traceparent: str | None = None,
    ) -> SubmitResult:
        request_hash = request_sha256(dataset_id)
        digest = idempotency_digest(self._hmac_key, actor, raw_idempotency_key)
        if traceparent is not None and _TRACEPARENT.fullmatch(traceparent) is None:
            traceparent = None
        result = self._repository.create_or_get(
            actor_hash=actor,
            idempotency_digest=digest,
            request_sha256=request_hash,
            dataset_id=dataset_id,
            max_attempts=self._max_attempts,
            traceparent=traceparent,
            now=self._clock(),
        )
        if result.job.request_sha256 != request_hash:
            raise IdempotencyConflict(
                "同一 Idempotency-Key 已绑定不同请求。"
            )
        return result

    def get(self, job_id: str) -> InspectionJob:
        job = self._repository.get(job_id)
        if job is None:
            raise JobNotFound("job 不存在。")
        return job

    def get_result(self, job_id: str) -> Mapping[str, Any]:
        job = self.get(job_id)
        if (
            job.status is not JobStatus.SUCCEEDED
            or job.expected_object_key is None
            or job.artifact_sha256 is None
        ):
            raise ResultNotReady("job 结果尚未就绪。")
        payload = self._object_store.get_json(
            object_key=job.expected_object_key,
            expected_sha256=job.artifact_sha256,
        )
        if hashlib.sha256(payload).hexdigest() != job.artifact_sha256:
            raise ObjectOutcomeUnknown("对象内容 hash 不匹配。")
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise UnsafeAggregatePayload("结果顶层必须是 object。")
        _validate_result_artifact(value, job)
        return value


class InspectionWorker:
    def __init__(
        self,
        *,
        queue: WorkQueue,
        inspector: AggregateInspector,
        object_store: ObjectStore,
        worker_id: str,
        lease_seconds: int = 60,
        retry_delay_seconds: int = 5,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._queue = queue
        self._inspector = inspector
        self._object_store = object_store
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._clock = clock

    def process_one(self) -> InspectionJob | None:
        job = self._queue.claim_next(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            now=self._clock(),
        )
        if job is None:
            return None
        context = (
            extract({"traceparent": job.traceparent})
            if job.traceparent is not None
            else None
        )
        tracer = trace.get_tracer("researchops.production_slice.worker", "0.1.0")
        with tracer.start_as_current_span(
            "inspection.execute",
            context=context,
            kind=SpanKind.CONSUMER,
            attributes={
                "researchops.task_type": "inspect_dataset",
                "researchops.attempt_count": job.attempt_count,
            },
        ):
            return self._process_claimed(job)

    def _process_claimed(self, job: InspectionJob) -> InspectionJob:
        try:
            profile = self._inspector.inspect_dataset(job.dataset_id)
            _validate_inspection_profile(
                profile, expected_dataset_id=job.dataset_id
            )
            artifact = {
                "schema_version": "1.0",
                "job_id": job.job_id,
                "dataset_id": job.dataset_id,
                "result_type": "aggregate_dataset_profile",
                "profile": profile,
                "privacy": {
                    "row_level_data_exposed": False,
                    "filesystem_path_exposed": False,
                },
            }
            payload = json.dumps(
                artifact,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            digest = hashlib.sha256(payload).hexdigest()
            object_key = f"jobs/{job.job_id}/results/{digest}.json"
            publishing = self._queue.begin_publishing(
                job=job,
                worker_id=self._worker_id,
                object_key=object_key,
                sha256=digest,
                byte_size=len(payload),
                now=self._clock(),
            )
        except TransientDependencyError as exc:
            return self._queue.fail_or_retry(
                job=job,
                worker_id=self._worker_id,
                error_code=exc.code,
                retryable=True,
                retry_at=self._clock()
                + timedelta(seconds=self._retry_delay_seconds),
                now=self._clock(),
            )
        except Exception:
            return self._queue.fail_or_retry(
                job=job,
                worker_id=self._worker_id,
                error_code="inspection_failed",
                retryable=False,
                retry_at=self._clock(),
                now=self._clock(),
            )

        try:
            stored = self._object_store.put_json(
                object_key=object_key,
                payload=payload,
                sha256=digest,
            )
            if stored != StoredObject(object_key, digest, len(payload)):
                raise ObjectOutcomeUnknown("对象存储回执不匹配。")
            return self._queue.complete(
                job=publishing,
                worker_id=self._worker_id,
                stored=stored,
                now=self._clock(),
            )
        except Exception:
            return self._queue.mark_outcome_unknown(
                job=publishing,
                worker_id=self._worker_id,
                error_code="object_write_outcome_unknown",
                retry_at=self._clock()
                + timedelta(seconds=self._retry_delay_seconds),
                now=self._clock(),
            )

    def reconcile_one(self) -> InspectionJob | None:
        job = self._queue.claim_unknown(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            now=self._clock(),
        )
        if job is None:
            return None
        context = (
            extract({"traceparent": job.traceparent})
            if job.traceparent is not None
            else None
        )
        tracer = trace.get_tracer("researchops.production_slice.worker", "0.1.0")
        with tracer.start_as_current_span(
            "inspection.reconcile",
            context=context,
            kind=SpanKind.CONSUMER,
            attributes={"researchops.task_type": "inspect_dataset"},
        ):
            return self._reconcile_claimed(job)

    def _reconcile_claimed(self, job: InspectionJob) -> InspectionJob:
        if job.expected_object_key is None or job.expected_sha256 is None:
            return self._queue.release_unknown(
                job=job,
                worker_id=self._worker_id,
                error_code="reconcile_intent_missing",
                retry_at=self._clock()
                + timedelta(seconds=self._retry_delay_seconds),
                now=self._clock(),
            )
        try:
            stored = self._object_store.head_json(object_key=job.expected_object_key)
        except Exception:
            return self._queue.release_unknown(
                job=job,
                worker_id=self._worker_id,
                error_code="reconcile_dependency_unavailable",
                retry_at=self._clock()
                + timedelta(seconds=self._retry_delay_seconds),
                now=self._clock(),
            )
        if stored is None:
            return self._queue.reconcile_absent(
                job=job,
                worker_id=self._worker_id,
                retry_at=self._clock()
                + timedelta(seconds=self._retry_delay_seconds),
                now=self._clock(),
            )
        if (
            stored.sha256 != job.expected_sha256
            or stored.byte_size != job.expected_bytes
        ):
            return self._queue.release_unknown(
                job=job,
                worker_id=self._worker_id,
                error_code="reconcile_object_mismatch",
                retry_at=self._clock()
                + timedelta(seconds=self._retry_delay_seconds),
                now=self._clock(),
            )
        return self._queue.reconcile_succeeded(
            job=job,
            worker_id=self._worker_id,
            stored=stored,
            now=self._clock(),
        )


def _assert_aggregate_only(value: Any) -> None:
    if isinstance(value, Mapping):
        forbidden = set(value).intersection(_FORBIDDEN_AGGREGATE_KEYS)
        if forbidden:
            raise UnsafeAggregatePayload("聚合结果包含禁止字段。")
        for item in value.values():
            _assert_aggregate_only(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_aggregate_only(item)


def _validate_result_artifact(
    value: Mapping[str, Any], job: InspectionJob
) -> None:
    _require_exact_keys(
        value,
        {"schema_version", "job_id", "dataset_id", "result_type", "profile", "privacy"},
    )
    if (
        value["schema_version"] != "1.0"
        or value["job_id"] != job.job_id
        or value["dataset_id"] != job.dataset_id
        or value["result_type"] != "aggregate_dataset_profile"
    ):
        raise UnsafeAggregatePayload("结果 artifact binding 无效。")
    privacy = _mapping(value["privacy"])
    _require_exact_keys(
        privacy, {"row_level_data_exposed", "filesystem_path_exposed"}
    )
    if privacy != {
        "row_level_data_exposed": False,
        "filesystem_path_exposed": False,
    }:
        raise UnsafeAggregatePayload("结果 privacy contract 无效。")
    _validate_inspection_profile(
        _mapping(value["profile"]), expected_dataset_id=job.dataset_id
    )


def _validate_inspection_profile(
    value: Mapping[str, Any], *, expected_dataset_id: str
) -> None:
    _assert_aggregate_only(value)
    _require_exact_keys(
        value,
        {"schema_version", "tool_name", "tool_version", "dataset", "profile", "privacy"},
    )
    if value["schema_version"] != "2.0" or value["tool_name"] != "inspect_dataset":
        raise UnsafeAggregatePayload("Inspect result contract 无效。")
    _nonempty_string(value["tool_version"])

    dataset = _mapping(value["dataset"])
    _require_exact_keys(
        dataset,
        {
            "dataset_id",
            "row_count",
            "column_count",
            "domain",
            "repeated_subjects",
            "analysis_boundaries",
            "model_access",
        },
    )
    _nonempty_string(dataset["dataset_id"])
    if dataset["dataset_id"] != expected_dataset_id:
        raise UnsafeAggregatePayload("Inspect dataset binding 不一致。")
    _nonnegative_int(dataset["row_count"])
    _nonnegative_int(dataset["column_count"])
    _nonempty_string(dataset["domain"])
    if not isinstance(dataset["repeated_subjects"], bool):
        raise UnsafeAggregatePayload("repeated_subjects 类型无效。")
    _string_list(dataset["analysis_boundaries"])
    if dataset["model_access"] != "aggregate_tools_only":
        raise UnsafeAggregatePayload("model_access 无效。")

    profile = _mapping(value["profile"])
    _require_exact_keys(
        profile,
        {
            "row_count",
            "column_count",
            "duplicate_row_count",
            "rows_with_missing",
            "complete_row_count",
            "columns",
            "missing_patterns",
            "warnings",
        },
    )
    for key in (
        "row_count",
        "column_count",
        "duplicate_row_count",
        "rows_with_missing",
        "complete_row_count",
    ):
        _nonnegative_int(profile[key])
    if (
        profile["row_count"] != dataset["row_count"]
        or profile["column_count"] != dataset["column_count"]
    ):
        raise UnsafeAggregatePayload("Dataset/profile dimensions 不一致。")
    columns = _list(profile["columns"])
    if len(columns) != profile["column_count"]:
        raise UnsafeAggregatePayload("Column profile 数量不一致。")
    for raw_column in columns:
        column = _mapping(raw_column)
        _require_exact_keys(
            column,
            {
                "name",
                "semantic_type",
                "non_null_count",
                "null_count",
                "missing_rate",
                "unique_count",
                "unique_rate",
                "possible_identifier",
            },
        )
        _nonempty_string(column["name"])
        _nonempty_string(column["semantic_type"])
        for key in ("non_null_count", "null_count", "unique_count"):
            _nonnegative_int(column[key])
        for key in ("missing_rate", "unique_rate"):
            _rate(column[key])
        if not isinstance(column["possible_identifier"], bool):
            raise UnsafeAggregatePayload("possible_identifier 类型无效。")
    for raw_pattern in _list(profile["missing_patterns"]):
        pattern = _mapping(raw_pattern)
        _require_exact_keys(pattern, {"missing_columns", "row_count", "row_rate"})
        _string_list(pattern["missing_columns"])
        _nonnegative_int(pattern["row_count"])
        _rate(pattern["row_rate"])
    for raw_warning in _list(profile["warnings"]):
        warning = _mapping(raw_warning)
        _require_exact_keys(warning, {"code", "severity", "column"})
        _nonempty_string(warning["code"])
        _nonempty_string(warning["severity"])
        if warning["column"] is not None:
            _nonempty_string(warning["column"])

    privacy = _mapping(value["privacy"])
    _require_exact_keys(
        privacy,
        {
            "row_level_values_exposed",
            "sample_values_exposed",
            "filesystem_path_exposed",
            "model_access",
        },
    )
    if privacy != {
        "row_level_values_exposed": False,
        "sample_values_exposed": False,
        "filesystem_path_exposed": False,
        "model_access": "aggregate_tools_only",
    }:
        raise UnsafeAggregatePayload("Inspect privacy contract 无效。")


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UnsafeAggregatePayload("期望 object。")
    return value


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise UnsafeAggregatePayload("期望 array。")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise UnsafeAggregatePayload("Aggregate schema 字段集合无效。")


def _nonempty_string(value: Any) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise UnsafeAggregatePayload("字符串字段无效。")


def _nonnegative_int(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UnsafeAggregatePayload("计数字段无效。")


def _rate(value: Any) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise UnsafeAggregatePayload("比例字段无效。")


def _string_list(value: Any) -> None:
    items = _list(value)
    for item in items:
        _nonempty_string(item)
