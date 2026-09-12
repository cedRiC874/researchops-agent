"""Real temporary Git/SQLite plus ephemeral signatures; not actual admission."""
from __future__ import annotations

import base64
import copy
import json

from researchops_completion_timing import first_live_review as review
from researchops_completion_timing import first_live_identity as identity
from researchops_completion_timing import first_live_artifacts as archive
from researchops_completion_timing.admission import TimedAdmissionInputs
from researchops_external_closure import execution_components_v3 as source
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import execution_v3_fixture as source_fixture
from tests.execution_v2_fixture import registry_documents, h
from tests.test_external_closure_documents import SyntheticPreReceipt
from tests.timed_first_live_evidence_fixture import TimedEvidenceFixture
from tests.test_external_closure_execution_binding import write_file_tree


class TimedAdmissionFixture(TimedEvidenceFixture):
    def __init__(self, *, implementation_version=4, current_campaign_runtime=False):
        super().__init__(implementation_version=implementation_version, current_campaign_runtime=current_campaign_runtime)
        try:
            identity_module, source_module = identity, source
            if implementation_version == 5:
                from researchops_completion_timing import first_live_identity_v5 as identity_module
                from researchops_external_closure import execution_components_v4 as source_module
            self.signers = SyntheticPreReceipt()
            self.trust = copy.deepcopy(self.signers.documents["trust_manifest"])
            self.trust["expires_at_utc"] = "2026-09-08T00:00:00Z"
            for role in self.trust["roles"]:
                role["expires_at_utc"] = self.trust["expires_at_utc"]
            self.signers._finalize_document(self.trust, roles=("freeze_authority", "task_custodian", "ledger_witness"))
            components = json.loads(self.source_documents.manifest)["component_hashes"]
            protocol = json.loads(self.files[identity_module.IMPLEMENTATION_PATH])["execution_profile"]
            self.subject = {name: protocol[name] for name in ("provider_id", "model_id", "api_origin", "api_surface", "transport_id", "adapter_version")}
            self.subject.update(first_live_execution_commit=self.source_commit, first_live_execution_tree=self.source_tree,
                first_live_source_integrity_commitment_sha256=json.loads(self.source_documents.plan)["plan_commitment_sha256"],
                first_live_implementation_commitment_sha256=identity_module.IMPLEMENTATION_COMMITMENT_SHA256,
                source_byte_inventory_sha256=components["source_byte_inventory_sha256"], dependency_lock_sha256=components["dependency_lock_sha256"],
                pyproject_sha256=components["pyproject_sha256"], selected_mapping_sha256=components["mapping_sha256"])
            self.review = dict(schema_version="provider-completion-timed-first-live-review/2.0",
                document_type="completion_telemetry_timed_first_live_review", review_id="PCEREVIEW-" + "A" * 32,
                reviewed_at_utc="2026-09-06T01:00:10Z", first_live_completed_at_utc="2026-09-06T01:00:02Z",
                evidence_observed_at_utc="2026-09-06T01:00:05Z", evidence_commit=self.reviewed_commit, evidence_tree=self.reviewed_tree,
                first_live_evidence_commitment_sha256=self.archive.bundle["bundle_commitment_sha256"], subject=copy.deepcopy(self.subject),
                decision="approved", reason_code="passed", review_scope="two_response_timing_control_archive_and_source_linkage_only",
                reviewer_independence_claimed=False, online_execution_authorized=False, provider_registration_authorized=False,
                first_live_artifact_contract_sha256=archive.PROFILE_SHA256, document_sha256="0" * 64, signatures=[])
            self.observation = dict(schema_version="provider-completion-first-live-review-observation/1.0",
                document_type="completion_telemetry_first_live_review_observation", review_document_sha256="0" * 64,
                review_observed_at_utc="2026-09-06T01:00:11Z", review_observation_source_commitment_sha256=h("timed review observer"),
                trust_manifest_sha256=self.trust["document_sha256"], trust_observed_at_utc="2026-09-04T00:00:02Z",
                trust_observation_source_commitment_sha256=h("timed trust observer"), candidate_freeze_receipt_sha256=h("timed candidate freeze"),
                candidate_frozen_at_utc="2026-09-06T01:01:00Z", expectations_supplied_out_of_band=True)
            self.sign_review()
            _, registry_bundle, registry_raw, carrier_raw = registry_documents(self.subject,
                self.archive.bundle["bundle_commitment_sha256"], self.review["document_sha256"], self.reviewed_commit, self.reviewed_tree)
            if implementation_version == 4:
                files, paths = source_fixture.campaign_files(self.files, tuple(sorted(self.files)), self.source_documents)
            else:
                files = dict(self.files)
                files.update({'evals/provider_completion_runtime_registry_v3/runtime_registry_v3.json': registry_raw,
                              'evals/provider_completion_runtime_registry_v3/registry_admission_bundle_v1.json': carrier_raw})
                paths = tuple(sorted(files))
            files["evals/provider_completion_runtime_registry_v3/runtime_registry_v3.json"] = registry_raw
            files["evals/provider_completion_runtime_registry_v3/registry_admission_bundle_v1.json"] = carrier_raw
            campaign = source_module.build_profile_documents(files, available_paths=paths, profile="campaign")
            manifest_path, plan_path = source_module.PROFILE_PATHS["campaign"]
            self.campaign_files = files | {manifest_path: campaign.manifest, plan_path: campaign.plan, self.path: raw(self.archive.bundle)}
            self.campaign_tree = write_file_tree(self.repository, self.campaign_files)
            self.campaign_commit = self.repository.commit(self.campaign_tree, self.reviewed_commit)
            manifest, plan = json.loads(campaign.manifest), json.loads(campaign.plan)
            self.binding = dict(repository="cedRiC874/researchops-agent", candidate_freeze_receipt_sha256=h("timed candidate freeze"),
                candidate_freeze_ledger_entry_sha256=h("timed candidate witness"), execution_commit=self.campaign_commit,
                execution_tree=self.campaign_tree, source_integrity_commitment_sha256=plan["plan_commitment_sha256"],
                implementation_commitment_sha256=manifest["commitment_sha256"],
                first_live_evidence_commitment_sha256=self.archive.bundle["bundle_commitment_sha256"], first_live_validation_status="reviewed_success",
                runtime_registry_commitment_sha256=registry_bundle["commitment_sha256"], runtime_registry_binding_allowed=True,
                closure_evidence_contract_commitment_sha256="a96b39a1f4bafa3bcf512fd88a5e7b0ca3987ca2bc9804642a1d1259cbf8cad0",
                **{name: manifest["component_hashes"][name] for name in ("runner_config_commitment_sha256", "dependency_lock_sha256",
                    "sanitizer_sha256", "closure_verifier_sha256", "telemetry_schema_sha256", "mapping_sha256")},
                **{name: self.subject[name] for name in ("provider_id", "model_id", "api_origin", "api_surface", "transport_id", "adapter_version")})
        except BaseException:
            self.close()
            raise

    def sign_review(self):
        digest = review.review_document_sha256(self.review)
        key_id = self.signers.key_ids["freeze_authority"]
        signature = self.signers.keys["freeze_authority"].sign(review.review_signature_message(key_id, digest))
        self.review["document_sha256"] = digest
        self.review["signatures"] = [{"role": "freeze_authority", "key_id": key_id, "algorithm": "ed25519",
            "signed_sha256": digest, "signature_b64": base64.b64encode(signature).decode()}]
        self.observation["review_document_sha256"] = digest

    def inputs(self):
        return TimedAdmissionInputs(self.archive.directory, raw(self.archive.bundle), raw(self.review), raw(self.trust), raw(self.observation),
            self.review["document_sha256"], self.trust["document_sha256"], self.archive.data["auth"]["authorization_binding_sha256"],
            h("timed candidate freeze"), "2026-09-06T01:01:00Z", "2026-09-06T01:00:02Z", self.archive.data["auth"]["authorized_at_utc"])
