from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from researchops.eval_v2_contracts import EvalV2ContractError
from researchops.eval_v2_dataset_prep import (
    EvalV2LogicalDatasetRegistry,
    prepare_eval_v2_datasets,
)
from researchops.eval_v2_inspect_backend import EvalV2InspectDatasetBackend


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "evals" / "v2" / "external_datasets.json"
PARKINSONS_HEADER = (
    "subject#,age,sex,test_time,motor_UPDRS,total_UPDRS,Jitter(%),"
    "Jitter(Abs),Jitter:RAP,Jitter:PPQ5,Jitter:DDP,Shimmer,Shimmer(dB),"
    "Shimmer:APQ3,Shimmer:APQ5,Shimmer:APQ11,Shimmer:DDA,NHR,HNR,RPDE,DFA,PPE"
)


def make_zip(entry_name: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(entry_name, content)
    return buffer.getvalue()


class EvalV2DatasetPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "artifacts").mkdir()

    def build_fixture(self) -> tuple[Path, dict[str, bytes]]:
        payload = copy.deepcopy(
            json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        )
        penguins = (
            "species,island,bill_length_mm,bill_depth_mm,flipper_length_mm,"
            "body_mass_g,sex,year\nAdelie,Torgersen,39.1,18.7,181,3750,male,2007\n"
            "Adelie,Torgersen,39.5,,186,3800,female,2007\n"
        ).encode()
        parkinsons_asset = (
            PARKINSONS_HEADER
            + "\n1,72,0,5,20,30,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16"
            + "\n1,72,0,6,21,31,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16\n"
        ).encode()
        heart_asset = (
            "63,1,1,145,233,1,2,150,0,2.3,3,0,6,0\n"
            "67,1,4,160,286,0,2,108,1,1.5,2,3,3,2\n"
        ).encode()
        parkinsons_zip = make_zip("parkinsons_updrs.data", parkinsons_asset)
        heart_zip = make_zip("processed.cleveland.data", heart_asset)
        downloads = [penguins, parkinsons_zip, heart_zip]
        selected = [penguins, parkinsons_asset, heart_asset]
        structures = [
            (True, 2, 8, 1, ["", "NA"]),
            (True, 2, 22, 0, ["?"]),
            (False, 2, 14, 0, ["?"]),
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
        path = self.root / "external_datasets.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path, mapping

    def test_preparation_is_opt_in(self) -> None:
        result = prepare_eval_v2_datasets(
            project_root=self.root,
            dataset_manifest_path=MANIFEST_PATH,
            output_directory=self.root / "artifacts" / "prepared",
            confirm_download=False,
            fetcher=lambda _: (_ for _ in ()).throw(AssertionError("no fetch")),
        )

        self.assertEqual(result["status"], "not_run")
        self.assertEqual(result["files_written"], 0)
        self.assertFalse((self.root / "artifacts" / "prepared").exists())

    def test_prepares_atomic_outputs_and_loads_registry(self) -> None:
        manifest_path, mapping = self.build_fixture()
        output = self.root / "artifacts" / "prepared"

        result = prepare_eval_v2_datasets(
            project_root=self.root,
            dataset_manifest_path=manifest_path,
            output_directory=output,
            confirm_download=True,
            fetcher=lambda url: mapping[url],
        )
        registry = EvalV2LogicalDatasetRegistry.load(
            output / "logical_dataset_registry.json"
        )

        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["dataset_count"], 3)
        self.assertFalse(result["raw_downloads_persisted"])
        self.assertFalse(result["model_row_access"])
        self.assertEqual(len(registry.dataset_ids), 3)
        self.assertNotIn(str(self.root), json.dumps(registry.public_catalog()))
        self.assertNotIn("prepared_sha256", json.dumps(registry.public_catalog()))

        parkinsons = registry.resolve("uci_parkinsons_telemonitoring_189")
        with parkinsons.path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0][0], "subject_key")
        self.assertTrue(rows[1][0].startswith("SUBJ-"))
        self.assertNotIn("subject#", rows[0])

        heart = registry.resolve("uci_heart_disease_cleveland_45")
        with heart.path.open(encoding="utf-8", newline="") as handle:
            heart_header = next(csv.reader(handle))
        self.assertEqual(heart_header[-1], "heart_disease_class")

    def test_registry_rejects_unknown_id_and_tampered_file(self) -> None:
        manifest_path, mapping = self.build_fixture()
        output = self.root / "artifacts" / "prepared"
        prepare_eval_v2_datasets(
            project_root=self.root,
            dataset_manifest_path=manifest_path,
            output_directory=output,
            confirm_download=True,
            fetcher=lambda url: mapping[url],
        )
        registry = EvalV2LogicalDatasetRegistry.load(
            output / "logical_dataset_registry.json"
        )
        with self.assertRaises(EvalV2ContractError) as unknown:
            registry.resolve("unknown_dataset")
        self.assertEqual(unknown.exception.code, "eval_v2_dataset_not_authorized")

        path = registry.resolve("palmer_penguins_v0_1_0").path
        path.write_bytes(path.read_bytes() + b"tampered")
        with self.assertRaises(EvalV2ContractError) as tampered:
            registry.resolve("palmer_penguins_v0_1_0")
        self.assertEqual(
            tampered.exception.code, "eval_v2_prepared_dataset_tampered"
        )

    def test_inspect_backend_returns_only_allowlisted_aggregates(self) -> None:
        manifest_path, mapping = self.build_fixture()
        output = self.root / "artifacts" / "prepared"
        prepare_eval_v2_datasets(
            project_root=self.root,
            dataset_manifest_path=manifest_path,
            output_directory=output,
            confirm_download=True,
            fetcher=lambda url: mapping[url],
        )
        backend = EvalV2InspectDatasetBackend.from_registry_path(
            output / "logical_dataset_registry.json"
        )

        result = backend.inspect_dataset("uci_parkinsons_telemonitoring_189")
        serialized = json.dumps(result, ensure_ascii=False)

        self.assertEqual(result["profile"]["row_count"], 2)
        self.assertFalse(result["privacy"]["row_level_values_exposed"])
        self.assertFalse(result["privacy"]["filesystem_path_exposed"])
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn('"sample_values":', serialized)
        self.assertNotIn("prepared_sha256", serialized)
        self.assertNotIn("SUBJ-", serialized)
        subject_column = next(
            column
            for column in result["profile"]["columns"]
            if column["name"] == "subject_key"
        )
        self.assertTrue(subject_column["possible_identifier"])

    def test_output_boundary_and_no_overwrite(self) -> None:
        manifest_path, mapping = self.build_fixture()
        with self.assertRaises(EvalV2ContractError) as boundary:
            prepare_eval_v2_datasets(
                project_root=self.root,
                dataset_manifest_path=manifest_path,
                output_directory=self.root / "outside",
                confirm_download=True,
                fetcher=lambda url: mapping[url],
            )
        self.assertEqual(
            boundary.exception.code, "eval_v2_output_path_not_allowed"
        )

        output = self.root / "artifacts" / "prepared"
        output.mkdir()
        with self.assertRaises(EvalV2ContractError) as overwrite:
            prepare_eval_v2_datasets(
                project_root=self.root,
                dataset_manifest_path=manifest_path,
                output_directory=output,
                confirm_download=True,
                fetcher=lambda url: mapping[url],
            )
        self.assertEqual(overwrite.exception.code, "eval_v2_output_exists")


if __name__ == "__main__":
    unittest.main()
