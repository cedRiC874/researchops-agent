from __future__ import annotations

import csv
import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .contracts import ColumnProfile, DatasetProfile, MissingPattern, QualityWarning


IDENTIFIER_NAME_PATTERN = re.compile(
    r"(^id$|_id$|^id_|identifier|patient|participant|subject|email|phone|mobile|name|address)",
    re.IGNORECASE,
)
FORMULA_PREFIXES = ("=", "+", "-", "@")


class CsvValidationError(ValueError):
    """Raised when an input file violates the CSV safety contract."""


@dataclass(frozen=True)
class CsvSafetyConfig:
    max_file_size_bytes: int = 20 * 1024 * 1024
    max_rows: int = 1_000_000
    high_missingness_threshold: float = 0.20
    identifier_unique_rate_threshold: float = 0.90
    max_sample_values: int = 3
    max_missing_patterns: int = 10


class CsvProfiler:
    def __init__(self, config: CsvSafetyConfig | None = None) -> None:
        self.config = config or CsvSafetyConfig()

    def profile(self, csv_path: str | Path) -> DatasetProfile:
        path = Path(csv_path).resolve()
        file_size = self._validate_file(path)
        header = self._read_and_validate_header(path)

        try:
            frame = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
        except UnicodeDecodeError as exc:
            raise CsvValidationError("CSV 必须使用 UTF-8 或 UTF-8-SIG 编码。") from exc
        except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
            raise CsvValidationError(f"CSV 解析失败：{exc}") from exc

        if frame.empty:
            raise CsvValidationError("CSV 没有可分析的数据行。")
        if len(frame) > self.config.max_rows:
            raise CsvValidationError(
                f"CSV 行数 {len(frame):,} 超过限制 {self.config.max_rows:,}。"
            )
        if list(frame.columns) != header:
            raise CsvValidationError("CSV 表头在解析时发生了非预期变化。")

        columns = [self._profile_column(frame[name], len(frame)) for name in frame.columns]
        duplicate_rows = int(frame.duplicated().sum())
        rows_with_missing = int(frame.isna().any(axis=1).sum())
        warnings = self._build_warnings(frame, columns, duplicate_rows)

        return DatasetProfile(
            source_name=path.name,
            file_size_bytes=file_size,
            sha256=_sha256(path),
            row_count=int(len(frame)),
            column_count=int(len(frame.columns)),
            duplicate_row_count=duplicate_rows,
            rows_with_missing=rows_with_missing,
            complete_row_count=int(len(frame) - rows_with_missing),
            columns=columns,
            missing_patterns=self._missing_patterns(frame),
            warnings=warnings,
        )

    def _validate_file(self, path: Path) -> int:
        if path.suffix.lower() != ".csv":
            raise CsvValidationError("只接受 .csv 文件。")
        if not path.is_file():
            raise CsvValidationError(f"CSV 文件不存在：{path}")
        file_size = path.stat().st_size
        if file_size == 0:
            raise CsvValidationError("CSV 文件为空。")
        if file_size > self.config.max_file_size_bytes:
            raise CsvValidationError(
                f"CSV 大小 {file_size:,} 字节超过限制 "
                f"{self.config.max_file_size_bytes:,} 字节。"
            )
        return file_size

    @staticmethod
    def _read_and_validate_header(path: Path) -> list[str]:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                header = next(csv.reader(handle))
        except UnicodeDecodeError as exc:
            raise CsvValidationError("CSV 必须使用 UTF-8 或 UTF-8-SIG 编码。") from exc
        except StopIteration as exc:
            raise CsvValidationError("CSV 文件为空。") from exc

        cleaned = [name.strip() for name in header]
        if not cleaned or any(not name for name in cleaned):
            raise CsvValidationError("CSV 表头不能包含空列名。")
        if len(cleaned) != len(set(cleaned)):
            raise CsvValidationError("CSV 表头包含重复列名。")
        if cleaned != header:
            raise CsvValidationError("CSV 列名不能包含前导或尾随空格。")
        return cleaned

    def _profile_column(self, series: pd.Series, total_rows: int) -> ColumnProfile:
        non_null = series.dropna()
        non_null_count = int(non_null.size)
        unique_count = int(non_null.nunique(dropna=True))
        unique_rate = unique_count / non_null_count if non_null_count else 0.0
        semantic_type = _infer_semantic_type(series)
        looks_like_identifier = bool(IDENTIFIER_NAME_PATTERN.search(str(series.name))) or (
            semantic_type == "text"
            and non_null_count >= 10
            and unique_rate >= self.config.identifier_unique_rate_threshold
        )
        if looks_like_identifier and non_null_count:
            sample_values = ["[REDACTED]"]
        else:
            sample_values = [
                _json_safe(value)
                for value in non_null.drop_duplicates().head(self.config.max_sample_values)
            ]

        return ColumnProfile(
            name=str(series.name),
            pandas_dtype=str(series.dtype),
            semantic_type=semantic_type,
            non_null_count=non_null_count,
            null_count=int(total_rows - non_null_count),
            missing_rate=round((total_rows - non_null_count) / total_rows, 6),
            unique_count=unique_count,
            unique_rate=round(unique_rate, 6),
            sample_values=sample_values,
        )

    def _missing_patterns(self, frame: pd.DataFrame) -> list[MissingPattern]:
        counts: Counter[tuple[str, ...]] = Counter()
        for mask in frame.isna().itertuples(index=False, name=None):
            missing_columns = tuple(
                str(column) for column, is_missing in zip(frame.columns, mask) if is_missing
            )
            if missing_columns:
                counts[missing_columns] += 1

        return [
            MissingPattern(
                missing_columns=list(columns),
                row_count=count,
                row_rate=round(count / len(frame), 6),
            )
            for columns, count in counts.most_common(self.config.max_missing_patterns)
        ]

    def _build_warnings(
        self,
        frame: pd.DataFrame,
        columns: list[ColumnProfile],
        duplicate_rows: int,
    ) -> list[QualityWarning]:
        warnings: list[QualityWarning] = []
        if duplicate_rows:
            warnings.append(
                QualityWarning(
                    code="duplicate_rows",
                    severity="warning",
                    message=f"发现 {duplicate_rows} 行完全重复记录。",
                )
            )

        for profile in columns:
            if profile.missing_rate >= self.config.high_missingness_threshold:
                warnings.append(
                    QualityWarning(
                        code="high_missingness",
                        severity="warning",
                        column=profile.name,
                        message=(
                            f"列 {profile.name} 的缺失率为 "
                            f"{profile.missing_rate:.1%}，达到高缺失阈值。"
                        ),
                    )
                )

            if profile.non_null_count and profile.unique_count == 1:
                warnings.append(
                    QualityWarning(
                        code="constant_column",
                        severity="info",
                        column=profile.name,
                        message=f"列 {profile.name} 只包含一个非空值。",
                    )
                )

            name_suggests_identifier = bool(IDENTIFIER_NAME_PATTERN.search(profile.name))
            high_cardinality_text = (
                profile.semantic_type == "text"
                and profile.non_null_count >= 10
                and profile.unique_rate >= self.config.identifier_unique_rate_threshold
            )
            if name_suggests_identifier or high_cardinality_text:
                warnings.append(
                    QualityWarning(
                        code="possible_identifier",
                        severity="warning",
                        column=profile.name,
                        message=(
                            f"列 {profile.name} 的名称类似行级标识符，"
                            "不应发送给模型或出现在报告中。"
                        ),
                    )
                )

            formula_count = _formula_like_count(frame[profile.name])
            if formula_count:
                warnings.append(
                    QualityWarning(
                        code="formula_injection_risk",
                        severity="warning",
                        column=profile.name,
                        message=(
                            f"列 {profile.name} 包含 {formula_count} 个公式样式文本，"
                            "导出到电子表格前需要转义。"
                        ),
                    )
                )

        return warnings


def profile_csv(
    csv_path: str | Path,
    config: CsvSafetyConfig | None = None,
) -> DatasetProfile:
    return CsvProfiler(config).profile(csv_path)


def _infer_semantic_type(series: pd.Series) -> str:
    non_null = series.dropna()
    if non_null.empty:
        return "empty"
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_numeric_dtype(series):
        unique_count = int(non_null.nunique())
        if unique_count == 2:
            return "binary_numeric"
        if pd.api.types.is_integer_dtype(series) and unique_count <= 20:
            return "discrete_numeric"
        return "continuous_numeric"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"

    text = non_null.astype(str).str.strip()
    if not text.empty:
        parsed_dates = pd.to_datetime(text, errors="coerce", format="mixed")
        if float(parsed_dates.notna().mean()) >= 0.90:
            return "datetime"
    unique_count = int(non_null.nunique())
    if unique_count <= min(20, max(2, math.ceil(len(non_null) * 0.10))):
        return "categorical"
    return "text"


def _formula_like_count(series: pd.Series) -> int:
    if pd.api.types.is_numeric_dtype(series):
        return 0
    text = series.dropna().astype(str).str.lstrip()
    return int(text.str.startswith(FORMULA_PREFIXES).sum())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_safe(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
