from __future__ import annotations

import base64
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_external_closure.admission_contract import (
    CONTRACT_COMMITMENT, CONTRACT_PATH, CONTRACT_SHA256, load_admission_link_contract,
)
from researchops_external_closure.admission_review import (
    review_document_sha256, review_signature_message, verify_review_link,
)
from researchops_external_closure.admission_source import compare_source_bytes
from researchops_external_closure.admission_source_git import verify_historical_source_equality
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.primitives import canonical_json_bytes
from tests.test_external_closure_documents import SyntheticPreReceipt
from tests.test_external_closure_execution_binding import write_file_tree
from tests.test_external_closure_git_objects import Repository


ROOT = Path(__file__).resolve().parents[1]


def h(label):
    return hashlib.sha256(label.encode()).hexdigest()


def source_files():
    return {
        "src/researchops/__init__.py": b"",
        "src/researchops/model_providers.py": b"VERSION = 'same'\n",
        "src/new_package/member.py": b"value = 1\n",
        "requirements.lock": b"example==1\n",
        "pyproject.toml": b"[project]\nname='synthetic'\n",
    }


class AdmissionContractTests(unittest.TestCase):
    def test_pinned_contract_and_all_schemas_load(self):
        contract = load_admission_link_contract(ROOT)
        self.assertEqual(len(contract.schema_bytes), 5)
        self.assertEqual(hashlib.sha256(contract.contract_bytes).hexdigest(), CONTRACT_SHA256)
        domain = contract.document()["byte_protocol"]["domain_values"]["contract"]
        self.assertEqual(hashlib.sha256(domain.encode()+b"\0"+canonical_json_bytes(contract.document())).hexdigest(), CONTRACT_COMMITMENT)
        self.assertEqual(contract.document()["mapping_rules"]["allowed_metadata_change_paths"], [])
        with self.assertRaises(TypeError):
            contract.schema_bytes["unexpected"] = b"{}"

    def test_contract_and_schema_tamper_are_rejected(self):
        contract = load_admission_link_contract(ROOT)
        document = contract.document()
        relative = [CONTRACT_PATH, document["predecessors"]["trust_schema"]["path"]]
        relative += [str(Path(CONTRACT_PATH).parent / "schemas" / item["name"]) for item in document["schema_bindings"]]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in relative:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
            self.assertEqual(len(load_admission_link_contract(root).schema_bytes), 5)
            for name in (relative[0], relative[-1]):
                target = root / name
                original = target.read_bytes()
                target.write_bytes(original+b"\n")
                with self.assertRaises(ExternalClosurePrimitiveError):
                    load_admission_link_contract(root)
                target.write_bytes(original)


class ReviewLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = load_admission_link_contract(ROOT)

    def setUp(self):
        self.fixture = SyntheticPreReceipt()
        self.trust = self.fixture.documents["trust_manifest"]
        self.subject = {
            "provider_id":"deepseek", "model_id":"deepseek-v4-flash", "api_origin":"https://api.deepseek.com",
            "api_surface":"responses", "transport_id":"openai_compatible_responses", "adapter_version":"deepseek-responses-adapter/1.0",
            "first_live_execution_commit":"1"*40, "first_live_execution_tree":"2"*40,
            **{name:h(name) for name in ("first_live_source_integrity_commitment_sha256", "first_live_implementation_commitment_sha256",
               "source_byte_inventory_sha256", "dependency_lock_sha256", "pyproject_sha256", "selected_mapping_sha256")},
        }
        self.review = {
            "schema_version":"provider-completion-first-live-review/1.0", "document_type":"completion_telemetry_first_live_review",
            "review_id":"PCEREVIEW-"+"A"*32, "reviewed_at_utc":"2026-09-04T00:00:12Z",
            "first_live_completed_at_utc":"2026-09-04T00:00:10Z", "evidence_observed_at_utc":"2026-09-04T00:00:11Z",
            "evidence_commit":"3"*40, "evidence_tree":"4"*40, "first_live_evidence_commitment_sha256":h("evidence"),
            "subject":copy.deepcopy(self.subject), "decision":"approved", "reason_code":"passed",
            "review_scope":"two_response_adapter_completion_shapes_only", "reviewer_independence_claimed":False,
            "online_execution_authorized":False, "provider_registration_authorized":False,
            "document_sha256":"0"*64, "signatures":[],
        }
        self.observation = {
            "schema_version":"provider-completion-first-live-review-observation/1.0",
            "document_type":"completion_telemetry_first_live_review_observation",
            "review_document_sha256":"0"*64, "review_observed_at_utc":"2026-09-04T00:00:13Z",
            "review_observation_source_commitment_sha256":h("review observer"),
            "trust_manifest_sha256":self.trust["document_sha256"], "trust_observed_at_utc":"2026-09-04T00:00:02Z",
            "trust_observation_source_commitment_sha256":h("trust observer"),
            "candidate_freeze_receipt_sha256":h("candidate"), "candidate_frozen_at_utc":"2026-09-04T00:00:14Z",
            "expectations_supplied_out_of_band":True,
        }
        self.sign()

    def sign(self, role="freeze_authority"):
        digest = review_document_sha256(self.contract, self.review)
        key_id = self.fixture.key_ids[role]
        signature = self.fixture.keys[role].sign(review_signature_message(self.contract, key_id, digest))
        self.review["document_sha256"] = digest
        self.review["signatures"] = [{"role":"freeze_authority", "key_id":key_id, "algorithm":"ed25519",
                                      "signed_sha256":digest,"signature_b64":base64.b64encode(signature).decode()}]
        self.expected_review = digest
        self.observation["review_document_sha256"] = digest

    def run_review(self, **overrides):
        arguments = {
            "review_bytes":canonical_json_bytes(self.review), "trust_bytes":canonical_json_bytes(self.trust),
            "observation_bytes":canonical_json_bytes(self.observation), "expected_review_document_sha256":self.expected_review,
            "expected_trust_manifest_sha256":self.trust["document_sha256"],
            "expected_candidate_freeze_receipt_sha256":h("candidate"), "expected_candidate_frozen_at_utc":"2026-09-04T00:00:14Z",
            "expected_subject":self.subject, "expected_first_live_evidence_commitment_sha256":h("evidence"),
            "expected_evidence_commit":"3"*40, "expected_evidence_tree":"4"*40,
        }
        return verify_review_link(self.contract, **(arguments | overrides))

    def test_real_synthetic_signature_does_not_become_evidence_or_admission(self):
        result = self.run_review()
        self.assertTrue(result.review_signature_verified)
        self.assertEqual(result.decision, "approved")
        self.assertFalse(result.first_live_artifact_integrity_verified)
        self.assertFalse(result.git_source_completeness_verified)
        self.assertFalse(result.registry_admission_verified)
        self.assertFalse(result.runtime_authority_granted)
        self.assertFalse(result.closure_claim_allowed)

    def test_valid_rejected_review_is_not_rewritten_as_approved(self):
        self.review.update(decision="rejected", reason_code="privacy_not_verified")
        self.sign()
        self.assertEqual(self.run_review().decision, "rejected")
        self.assertFalse(self.run_review().closure_claim_allowed)

    def test_external_anchor_and_fixed_evidence_commit_are_required(self):
        for field, value in (("expected_review_document_sha256",h("wrong")),("expected_trust_manifest_sha256",h("wrong")),
                             ("expected_candidate_freeze_receipt_sha256",h("wrong")),("expected_evidence_commit","5"*40),
                             ("expected_evidence_tree","6"*40)):
            with self.subTest(field=field), self.assertRaises(ExternalClosurePrimitiveError):
                self.run_review(**{field:value})

    def test_self_consistent_resigned_subject_drift_is_rejected(self):
        self.review["subject"]["source_byte_inventory_sha256"] = h("different source")
        self.sign()
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "subject_mismatch"):
            self.run_review()

    def test_wrong_signer_missing_signature_and_revoked_key_reject(self):
        self.sign("task_custodian")
        with self.assertRaises(ExternalClosurePrimitiveError):
            self.run_review()
        self.sign()
        self.review["signatures"] = []
        with self.assertRaises(ExternalClosurePrimitiveError):
            self.run_review()
        self.sign()
        self.trust["roles"][0]["revoked"] = True
        with self.assertRaises(ExternalClosurePrimitiveError):
            self.run_review()

    def test_submicrosecond_ordering_and_expired_role_reject(self):
        self.review["reviewed_at_utc"] = "2026-09-04T00:00:13.000000001Z"
        self.sign()
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"timeline_invalid"):
            self.run_review()
        self.review["reviewed_at_utc"] = "2026-09-06T00:00:00Z"
        self.sign()
        self.observation["review_observed_at_utc"] = "2026-09-06T00:00:01Z"
        self.observation["candidate_frozen_at_utc"] = "2026-09-06T00:00:02Z"
        with self.assertRaises(ExternalClosurePrimitiveError):
            self.run_review(expected_candidate_frozen_at_utc="2026-09-06T00:00:02Z")

    def test_fake_key_in_schema_valid_adapter_field_is_not_returned(self):
        self.review["subject"]["adapter_version"] = "sk-offlinecanary12345"
        self.sign()
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"sensitive_content_detected") as caught:
            self.run_review()
        self.assertNotIn("offlinecanary",str(caught.exception))

    def test_verification_has_no_file_network_or_process_io(self):
        with patch("builtins.open",side_effect=AssertionError("file IO")), patch("pathlib.Path.read_bytes",side_effect=AssertionError("file IO")), patch("subprocess.run",side_effect=AssertionError("process IO")), patch("socket.socket",side_effect=AssertionError("network IO")):
            self.assertEqual(self.run_review().decision,"approved")

    def test_unicode_escaped_fake_key_is_scanned_after_json_decode(self):
        self.review["subject"]["adapter_version"]="sk-offlinecanary12345"
        self.sign()
        escaped=canonical_json_bytes(self.review).replace(b"sk-offlinecanary12345",b"\\u0073k-offlinecanary12345")
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"sensitive_content_detected"):
            self.run_review(review_bytes=escaped)


class SourceEqualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = load_admission_link_contract(ROOT)

    def compare(self, first, second):
        return compare_source_bytes(self.contract,first_live_files=first,first_live_tree_paths=tuple(first),
                                    campaign_files=second,campaign_tree_paths=tuple(second))

    def test_byte_identity_is_not_execution_evidence(self):
        result = self.compare(source_files(),source_files())
        self.assertEqual(result.source_file_count,3)
        self.assertFalse(result.git_completeness_verified)
        self.assertFalse(result.runtime_authority_granted)
        self.assertFalse(result.installed_environment_verified)

    def test_every_source_and_lock_change_or_new_module_is_detected(self):
        first=source_files()
        for name in first:
            changed=dict(first)
            changed[name]+=b"\n"
            with self.subTest(name=name),self.assertRaisesRegex(ExternalClosurePrimitiveError,"byte_mismatch"):
                self.compare(first,changed)
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"inventory_mismatch"):
            self.compare(first,first|{"src/new_package/another.py":b""})

    def test_missing_required_path_and_mutable_bytes_are_rejected(self):
        files=source_files()
        with self.assertRaises(ExternalClosurePrimitiveError):
            self.compare({k:v for k,v in files.items() if k!="pyproject.toml"},files)
        changed=files|{"requirements.lock":bytearray(b"example==1\n")}
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,"bytes_required"):
            self.compare(changed,changed)

    def test_real_git_coverage_ignores_dirty_worktree_but_rejects_committed_source_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=Repository(Path(directory)/"repo")
            files=source_files()
            tree=write_file_tree(repo,files)
            first=repo.commit(tree)
            new_tree=write_file_tree(repo,files|{"README.md":b"different documentation"})
            second=repo.commit(new_tree,first)
            (repo.root/"unrelated.txt").write_bytes(b"dirty worktree")
            result=verify_historical_source_equality(repo.root,self.contract,first_live_commit=first,first_live_tree=tree,
                                                     campaign_commit=second,campaign_tree=new_tree)
            self.assertTrue(result.git_completeness_verified)
            self.assertFalse(result.runtime_authority_granted)
            bad_tree=write_file_tree(repo,files|{"src/new_package/hidden.py":b"new source"})
            bad=repo.commit(bad_tree,second)
            with self.assertRaisesRegex(ExternalClosurePrimitiveError,"inventory_mismatch"):
                verify_historical_source_equality(repo.root,self.contract,first_live_commit=first,first_live_tree=tree,
                                                   campaign_commit=bad,campaign_tree=bad_tree)
            with self.assertRaises(ExternalClosurePrimitiveError):
                verify_historical_source_equality(repo.root,self.contract,first_live_commit="f"*40,first_live_tree=tree,
                                                   campaign_commit=second,campaign_tree=new_tree)


if __name__ == "__main__":
    unittest.main()
