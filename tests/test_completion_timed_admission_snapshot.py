"""Candidate-local consistency must not replace the observed first-live snapshot."""
from __future__ import annotations

import json
import unittest

from researchops_completion_timing import admission
from researchops_completion_timing.contract import TimingContractError
from researchops_external_closure import execution_components_v3 as source
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import execution_v3_fixture as source_fixture
from tests.timed_admission_fixture import TimedAdmissionFixture
from tests.test_external_closure_execution_binding import write_file_tree


class TimedAdmissionSnapshotTests(unittest.TestCase):
    def test_self_consistent_candidate_cannot_substitute_another_first_live_snapshot(self):
        fixture = TimedAdmissionFixture(); self.addCleanup(fixture.close)
        first_paths = set(source.PROFILE_PATHS["first_live"])
        files = {name: payload for name, payload in fixture.files.items() if name not in first_paths}
        # Source Python, lock and pyproject stay byte-identical. Only the static
        # contract bytes change; this requires different first-live proof.
        # The legacy campaign already uses all 256 selected paths. Mutate one
        # existing static document so setup reaches the snapshot-link gate,
        # without raising the old limit or dropping any covered source file.
        changed = "evals/provider_completion_campaign_run_v1/contract_v1.json"
        static_document = json.loads(files[changed])
        static_document["synthetic_snapshot_substitution_test"] = True
        files[changed] = raw(static_document)
        self.assertEqual(set(files), set(fixture.files) - first_paths)
        for name in files:
            if name.startswith("src/") or name in {"requirements.lock", "pyproject.toml"}:
                self.assertEqual(files[name], fixture.files[name])
        paths = tuple(sorted(files))
        different_first = source.build_profile_documents(files, available_paths=paths, profile="first_live")
        self.assertNotEqual(different_first.plan, fixture.source_documents.plan)
        candidate, paths = source_fixture.campaign_files(files, paths, different_first)
        for name in ("evals/provider_completion_runtime_registry_v3/runtime_registry_v3.json",
                     "evals/provider_completion_runtime_registry_v3/registry_admission_bundle_v1.json"):
            candidate[name] = fixture.campaign_files[name]
        different_campaign = source.build_profile_documents(candidate, available_paths=paths, profile="campaign")
        self.assertEqual(len(json.loads(different_campaign.manifest)["file_inventory"]), 256)
        manifest_path, plan_path = source.PROFILE_PATHS["campaign"]
        tree = write_file_tree(fixture.repository, candidate | {manifest_path: different_campaign.manifest,
            plan_path: different_campaign.plan, fixture.path: raw(fixture.archive.bundle)})
        commit = fixture.repository.commit(tree, fixture.campaign_commit)
        binding = dict(fixture.binding, execution_commit=commit, execution_tree=tree,
            source_integrity_commitment_sha256=json.loads(different_campaign.plan)["plan_commitment_sha256"],
            implementation_commitment_sha256=json.loads(different_campaign.manifest)["commitment_sha256"])
        with self.assertRaisesRegex(TimingContractError, "timed_admission_first_live_snapshot_mismatch"):
            admission.verify_timed_admission_links(fixture.repository.root, execution_binding=binding, inputs=fixture.inputs())


if __name__ == "__main__":
    unittest.main()
