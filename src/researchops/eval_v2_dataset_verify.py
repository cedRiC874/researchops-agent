from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .eval_v2_contracts import EvalV2ContractError
from .eval_v2_public import VerifiedDataset, load_eval_v2_dataset_manifest


ByteFetcher = Callable[[str], bytes]


@dataclass(frozen=True)
class VerifiedDatasetDownload:
    dataset_id: str
    download_byte_count: int
    selected_bytes: bytes


def verify_eval_v2_dataset_downloads(
    manifest_path: str | Path,
    *,
    confirm_download: bool,
    timeout_seconds: float = 30.0,
    fetcher: ByteFetcher | None = None,
) -> dict[str, object]:
    """Re-download public assets in memory and verify frozen metadata.

    No bytes are written to disk. Network access is opt-in so offline CI cannot
    accidentally depend on mutable external services.
    """

    manifest = load_eval_v2_dataset_manifest(manifest_path)
    if not confirm_download:
        return {
            "status": "not_run",
            "reason_code": "explicit_download_confirmation_required",
            "dataset_count": len(manifest.datasets),
            "network_calls": 0,
            "files_written": 0,
        }

    active_fetcher = fetcher or (
        lambda url: _fetch_bytes(url, timeout_seconds=timeout_seconds)
    )
    results: list[dict[str, object]] = []
    downloaded_bytes = 0
    for dataset in manifest.datasets:
        verified = download_verified_dataset(
            dataset,
            timeout_seconds=timeout_seconds,
            fetcher=active_fetcher,
        )
        downloaded_bytes += verified.download_byte_count
        selected = verified.selected_bytes
        structure = _summarize_csv(dataset, selected)
        results.append(
            {
                "dataset_id": dataset.dataset_id,
                "download_bytes": verified.download_byte_count,
                "selected_asset_bytes": len(selected),
                "selected_asset_sha256": _sha256(selected),
                "row_count": structure["row_count"],
                "column_count": structure["column_count"],
                "missing_cell_count": structure["missing_cell_count"],
                "verified": True,
            }
        )
    return {
        "status": "verified",
        "dataset_count": len(results),
        "verified_count": len(results),
        "network_calls": len(results),
        "downloaded_bytes": downloaded_bytes,
        "files_written": 0,
        "external_review_status_changed": False,
        "datasets": results,
    }


def download_verified_dataset(
    dataset: VerifiedDataset,
    *,
    timeout_seconds: float = 30.0,
    fetcher: ByteFetcher | None = None,
) -> VerifiedDatasetDownload:
    active_fetcher = fetcher or (
        lambda url: _fetch_bytes(url, timeout_seconds=timeout_seconds)
    )
    downloaded = active_fetcher(dataset.download_url)
    selected = _select_asset(dataset, downloaded)
    _verify_bytes(dataset, downloaded, selected)
    structure = _summarize_csv(dataset, selected)
    _verify_structure(dataset, structure)
    return VerifiedDatasetDownload(
        dataset_id=dataset.dataset_id,
        download_byte_count=len(downloaded),
        selected_bytes=selected,
    )


def _fetch_bytes(url: str, *, timeout_seconds: float) -> bytes:
    request = Request(
        url,
        headers={"User-Agent": "ResearchOps-Agent-EvalV2/0.2.0"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return response.read()
    except HTTPError as exc:
        raise EvalV2ContractError(
            "eval_v2_dataset_http_error",
            f"公开数据下载返回 HTTP {exc.code}；未记录响应正文。",
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise EvalV2ContractError(
            "eval_v2_dataset_download_failed",
            "公开数据下载失败；未记录底层响应正文。",
        ) from exc


def _select_asset(dataset: VerifiedDataset, downloaded: bytes) -> bytes:
    if dataset.archive_sha256 is None:
        return downloaded
    try:
        with zipfile.ZipFile(io.BytesIO(downloaded)) as archive:
            with archive.open(dataset.selected_asset) as handle:
                return handle.read()
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise EvalV2ContractError(
            "eval_v2_dataset_archive_invalid",
            f"dataset {dataset.dataset_id} 缺少已登记 selected asset。",
        ) from exc


def _verify_bytes(
    dataset: VerifiedDataset, downloaded: bytes, selected: bytes
) -> None:
    failures: list[str] = []
    if dataset.archive_sha256 is not None:
        if len(downloaded) != dataset.archive_bytes:
            failures.append("archive_bytes")
        if _sha256(downloaded) != dataset.archive_sha256:
            failures.append("archive_sha256")
    if len(selected) != dataset.selected_asset_bytes:
        failures.append("selected_asset_bytes")
    if _sha256(selected) != dataset.selected_asset_sha256:
        failures.append("selected_asset_sha256")
    if failures:
        raise EvalV2ContractError(
            "eval_v2_dataset_verification_failed",
            f"dataset {dataset.dataset_id} 资产不匹配：{', '.join(failures)}。",
        )


def _summarize_csv(
    dataset: VerifiedDataset, selected: bytes
) -> dict[str, int]:
    try:
        text = selected.decode("utf-8-sig")
    except UnicodeError as exc:
        raise EvalV2ContractError(
            "eval_v2_dataset_encoding_invalid",
            f"dataset {dataset.dataset_id} 不是已登记的 UTF-8/ASCII CSV。",
        ) from exc
    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    if not rows:
        raise EvalV2ContractError(
            "eval_v2_dataset_empty", f"dataset {dataset.dataset_id} 为空。"
        )
    data_rows = rows[1:] if dataset.has_header else rows
    column_counts = {len(row) for row in rows}
    if len(column_counts) != 1:
        raise EvalV2ContractError(
            "eval_v2_dataset_ragged_rows",
            f"dataset {dataset.dataset_id} 的 CSV 行列数不一致。",
        )
    missing_tokens = set(dataset.missing_tokens)
    missing_cells = sum(
        cell.strip() in missing_tokens for row in data_rows for cell in row
    )
    return {
        "row_count": len(data_rows),
        "column_count": next(iter(column_counts)),
        "missing_cell_count": missing_cells,
    }


def _verify_structure(
    dataset: VerifiedDataset, structure: dict[str, int]
) -> None:
    expected = {
        "row_count": dataset.row_count,
        "column_count": dataset.column_count,
        "missing_cell_count": dataset.missing_cell_count,
    }
    failures = [
        name for name, value in expected.items() if structure[name] != value
    ]
    if failures:
        raise EvalV2ContractError(
            "eval_v2_dataset_verification_failed",
            f"dataset {dataset.dataset_id} 结构不匹配：{', '.join(failures)}。",
        )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
