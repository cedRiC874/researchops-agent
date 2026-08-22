from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from researchops.eval_v2_contracts import EvalV2ContractError
from researchops.eval_v2_dataset_verify import verify_eval_v2_dataset_downloads


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "evals" / "v2" / "external_datasets.json"


def make_zip(entry_name: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(entry_name, content)
    return buffer.getvalue()


class EvalV2DatasetDownloadVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temp_path = Path(self.temporary_directory.name)
        self.payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def build_fixture(self) -> tuple[Path, dict[str, bytes]]:
        payload = copy.deepcopy(self.payload)
        penguins = b"a,b\n1,\n2,3\n"
        parkinsons_asset = b"subject#,x\n1,2\n1,3\n"
        heart_asset = b"1,?\n2,3\n"
        parkinsons_zip = make_zip("parkinsons_updrs.data", parkinsons_asset)
        heart_zip = make_zip("processed.cleveland.data", heart_asset)
        downloads = [penguins, parkinsons_zip, heart_zip]
        selected = [penguins, parkinsons_asset, heart_asset]
        structures = [
            (True, 2, 2, 1, [""]),
            (True, 2, 2, 0, ["?"]),
            (False, 2, 2, 1, ["?"]),
        ]
        mapping: dict[str, bytes] = {}
        for index, dataset in enumerate(payload["datasets"]):
            url = f"https://datasets.example.test/{index}"
            dataset["source"]["download_url"] = url
            dataset["source"]["selected_asset_bytes"] = len(selected[index])
            dataset["source"]["selected_asset_sha256"] = hashlib.sha256(
                selected[index]
            ).hexdigest()
            if index == 0:
                dataset["source"]["archive_bytes"] = None
                dataset["source"]["archive_sha256"] = None
            else:
                dataset["source"]["archive_bytes"] = len(downloads[index])
                dataset["source"]["archive_sha256"] = hashlib.sha256(
                    downloads[index]
                ).hexdigest()
            has_header, rows, columns, missing, tokens = structures[index]
            dataset["structure"]["has_header"] = has_header
            dataset["structure"]["row_count"] = rows
            dataset["structure"]["column_count"] = columns
            dataset["structure"]["missing_cell_count"] = missing
            dataset["structure"]["missing_tokens"] = tokens
            mapping[url] = downloads[index]
        path = self.temp_path / "datasets.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path, mapping

    def test_download_is_opt_in_and_writes_no_files(self) -> None:
        called = False

        def forbidden_fetcher(_: str) -> bytes:
            nonlocal called
            called = True
            raise AssertionError("fetcher should not be called")

        result = verify_eval_v2_dataset_downloads(
            MANIFEST_PATH,
            confirm_download=False,
            fetcher=forbidden_fetcher,
        )

        self.assertEqual(result["status"], "not_run")
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["files_written"], 0)
        self.assertFalse(called)

    def test_in_memory_download_verification_checks_all_three_assets(self) -> None:
        path, mapping = self.build_fixture()

        result = verify_eval_v2_dataset_downloads(
            path,
            confirm_download=True,
            fetcher=lambda url: mapping[url],
        )

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["verified_count"], 3)
        self.assertEqual(result["network_calls"], 3)
        self.assertEqual(result["files_written"], 0)
        self.assertFalse(result["external_review_status_changed"])

    def test_hash_mismatch_fails_closed(self) -> None:
        path, mapping = self.build_fixture()
        first_url = next(iter(mapping))
        mapping[first_url] = mapping[first_url] + b"tampered"

        with self.assertRaises(EvalV2ContractError) as context:
            verify_eval_v2_dataset_downloads(
                path,
                confirm_download=True,
                fetcher=lambda url: mapping[url],
            )

        self.assertEqual(
            context.exception.code, "eval_v2_dataset_verification_failed"
        )


if __name__ == "__main__":
    unittest.main()
