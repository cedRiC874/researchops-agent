"""Temporary Git plus bound synthetic eight-file archive; never a live fixture."""
from __future__ import annotations

import copy
import json
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from researchops.audit import AuditLedger
from researchops_completion_timing import first_live, first_live_control as control, local_claim
from researchops_completion_timing.contract import digest
from researchops_external_closure import execution_components_v3 as source
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import execution_v3_fixture as source_fixture
from tests import test_completion_first_live_artifacts as artifact_fixture
from tests import test_completion_first_live_timing as timing_fixture
from tests.test_external_closure_git_objects import Repository
from tests.test_external_closure_execution_binding import write_file_tree


def _with_current_project_python(files, project):
    """Current-runtime fixtures must not silently mix current callers with old callees.

    Historical contract/archive bytes remain seeds. All Python source is taken
    before either synthetic first-live or campaign source commitment is built.
    The caller's dictionary and the project filesystem are not modified.
    """
    current = {path.relative_to(project).as_posix(): path.read_bytes()
               for path in (project / 'src').rglob('*.py')}
    if not current:
        raise ValueError('current_runtime_python_source_missing')
    return {name: payload for name, payload in files.items()
            if not (name.startswith('src/') and name.endswith('.py'))} | current


class TimedEvidenceFixture:
    def __init__(self, *, implementation_version=4, current_startup_runtime=False, current_campaign_runtime=False):
        if type(implementation_version) is not int or implementation_version not in (4, 5):
            raise ValueError('synthetic evidence version invalid')
        if type(current_startup_runtime) is not bool or type(current_campaign_runtime) is not bool:
            raise ValueError('synthetic runtime selection invalid')
        self.stack = ExitStack()
        try:
            self.temporary = self.stack.enter_context(tempfile.TemporaryDirectory())
            # Launch copied source from the same canonical spelling as module ROOTs.
            # Windows TEMP may use an 8.3 alias; do not relax production origin checks.
            self.root = Path(self.temporary).resolve(strict=True)
            self.repository = Repository(self.root / "repo")
            source_module = source
            if implementation_version == 4:
                # This is a historical v4/v9 artifact test, not current-source
                # runtime admission. Preserve the frozen 256-file limit.
                files, paths = source_fixture.archived_first_live_files()
                if current_startup_runtime:
                    project = Path(__file__).resolve().parents[1]
                    for name in ('first_live_start.py', 'first_live_runtime.py', 'first_live_publish.py',
                                 'first_live_failure.py', 'first_live_failure_artifacts.py', 'first_live_run.py'):
                        path = 'src/researchops_completion_timing/' + name
                        files[path] = (project / path).read_bytes()
                if current_campaign_runtime:
                    project = Path(__file__).resolve().parents[1]
                    for name in ('campaign_admission.py', 'campaign_start.py', 'campaign_tasks.py', 'campaign_budget.py',
                                 'campaign_runtime.py', 'campaign_phase.py', 'campaign_artifacts.py', 'campaign_publish.py', 'campaign_run.py'):
                        path = 'src/researchops_completion_timing/' + name
                        files[path] = (project / path).read_bytes()
            else:
                from researchops_external_closure import execution_components_v4 as source_module
                from tests.test_execution_readers_v4 import fixture_files
                from tests.test_completion_first_live_identity_v5 import protocol_files
                files, _ = fixture_files()
                files.update(protocol_files())
                project = Path(__file__).resolve().parents[1]
                for name in ('first_live_identity_v5.py', 'first_live_evidence.py', 'admission.py', 'first_live_start.py',
                             'first_live_runtime.py', 'first_live_publish.py', 'first_live_failure.py', 'first_live_failure_artifacts.py',
                             'first_live_run.py', 'first_live_run_v5.py', 'campaign_admission.py', 'campaign_start.py',
                             'campaign_tasks.py', 'campaign_budget.py', 'campaign_runtime.py', 'campaign_phase.py',
                             'campaign_artifacts.py', 'campaign_publish.py', 'campaign_run.py', 'campaign_run_v5.py'):
                    path = 'src/researchops_completion_timing/' + name
                    files[path] = (project / path).read_bytes()
                # Current verifier bytes and the explicit timed closure schemas
                # belong in this synthetic v5 source tree, not a silent v3 replay.
                for path in (project / 'src/researchops_external_closure').glob('*.py'):
                    files[path.relative_to(project).as_posix()] = path.read_bytes()
                for path in (project / 'evals/provider_completion_timed_closure_v1').glob('*.json'):
                    files[path.relative_to(project).as_posix()] = path.read_bytes()
                if current_campaign_runtime:
                    files = _with_current_project_python(files, project)
                paths = tuple(sorted(files))
            self.source_documents = source_module.build_profile_documents(files, available_paths=paths, profile="first_live")
            manifest, plan = source_module.PROFILE_PATHS["first_live"]
            self.files = files | {manifest: self.source_documents.manifest, plan: self.source_documents.plan}
            self.source_tree = write_file_tree(self.repository, self.files)
            self.source_commit = self.repository.commit(self.source_tree)
            for name, payload in self.files.items():
                path = self.repository.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
            self.archive = artifact_fixture.FirstLiveArtifactTests()
            self.archive.setUp(); self.stack.callback(self.archive.doCleanups)
            self.template = copy.deepcopy(self.archive.data)
            self.counter = 0
            self.bind_archive()
        except BaseException:
            self.stack.close()
            raise

    def close(self):
        self.stack.close()

    def bind_archive(self, *, source_commitment=None):
        self.counter += 1
        directory = self.root / ("archive-" + str(self.counter)); directory.mkdir()
        data = copy.deepcopy(self.template)
        binding = data["plan"]["binding"]
        binding.update(execution_commit=self.source_commit, execution_tree=self.source_tree,
            source_integrity_commitment_sha256=source_commitment or json.loads(self.source_documents.plan)["plan_commitment_sha256"])
        binding["validation_run_id"] = first_live.validation_run_id(binding)
        plan = data["plan"]; plan["plan_commitment_sha256"] = first_live.commitment("plan", plan)
        auth = data["auth"]; auth["timing_plan_commitment_sha256"] = plan["plan_commitment_sha256"]
        auth["authorization_binding_sha256"] = control.authorization_binding(auth)
        proof = control.verify_first_live_authorization(self.repository.root, plan_bytes=raw(plan), authorization_bytes=raw(auth),
            expected_authorization_binding_sha256=auth["authorization_binding_sha256"], verification_time_utc=auth["authorized_at_utc"])
        intent, request = control.build_first_live_claim_intent(self.repository.root, proof,
            clock_domain_id=data["evidence"]["clock_domain_id"], intended_at_utc=auth["authorized_at_utc"])
        receipt = dict(json.loads(request), schema_version="provider-completion-local-claim/1.0", status="reserved",
            claimed_at_utc="2026-09-06T01:00:01Z", claim_contract_sha256=local_claim.CONTRACT_SHA256,
            store_scope="single_host_fixed_store", provider_actions_authorized=False)
        data.update(intent=json.loads(intent), receipt=receipt)
        data["evidence"]["binding"] = dict(binding, authorization_binding_sha256=auth["authorization_binding_sha256"],
            local_claim_receipt_sha256=digest(raw(receipt)))
        timing_fixture.FirstLiveTimingTests().seal(data)
        self.archive.directory = directory
        self.archive.data = self.archive.helper.data = data
        self.archive.helper.database = directory / "audit.sqlite3"
        with patch.object(AuditLedger, "_now", return_value="2026-09-06T01:00:02+00:00"):
            self.archive.helper.build_database()
        self.archive.reseal()
        self.path = "docs/evidence/provider-completion-first-live-v1/" + binding["authorization_id_sha256"] + "/bundle.json"
        self.reviewed_tree = write_file_tree(self.repository, self.files | {self.path: raw(self.archive.bundle)})
        self.reviewed_commit = self.repository.commit(self.reviewed_tree, self.source_commit)
        current = self.repository.root / self.path
        current.parent.mkdir(parents=True, exist_ok=True); current.write_bytes(b"wrong current worktree bundle")

    def arguments(self):
        return dict(artifact_directory=self.archive.directory, bundle_bytes=raw(self.archive.bundle),
            expected_bundle_commitment_sha256=self.archive.bundle["bundle_commitment_sha256"],
            expected_authorization_binding_sha256=self.archive.data["auth"]["authorization_binding_sha256"],
            verification_time_utc=self.archive.data["auth"]["authorized_at_utc"],
            reviewed_evidence_commit=self.reviewed_commit, reviewed_evidence_tree=self.reviewed_tree)
