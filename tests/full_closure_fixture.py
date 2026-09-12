"""Full synthetic A/B input graph, using no runtime factory or verifier stub."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime,timedelta

from researchops_external_closure.primitives import canonical_json_bytes
from tests import test_external_closure_evaluator as helpers
from tests.test_external_closure_documents import USER_BINDING_DOMAIN,RUNTIME_PLAN_DOMAIN


def _hash(domain,*parts):
    return helpers._domain_hash(domain,*parts)


def shift(value,*,hours=2):
    if isinstance(value,dict):
        return {key:shift(child,hours=hours) for key,child in value.items()}
    if isinstance(value,list):
        return [shift(child,hours=hours) for child in value]
    if isinstance(value,str):
        if value.startswith(("2026-09-04T","2026-09-05T")):
            base=datetime.strptime(value[:19],"%Y-%m-%dT%H:%M:%S")+timedelta(days=1,hours=hours)
            return base.strftime("%Y-%m-%dT%H:%M:%S")+value[19:]
        if value=="2026-09-04":
            return "2026-09-05"
    return value


def full_campaign_fixture(admission_setup):
    fixture,semantic=helpers._signed_complete_fixture()
    # Trust is unchanged: the SAME externally anchored freeze_authority must
    # sign the first-live review and this campaign's pre-receipt graph.
    trust=copy.deepcopy(fixture.documents["trust_manifest"])
    trust_observation=copy.deepcopy(fixture.observation["trust_manifest_observation"])
    fixture.documents={name:shift(value) for name,value in fixture.documents.items()}
    fixture.documents["trust_manifest"]=trust
    fixture.observation=shift(fixture.observation)
    fixture.observation["trust_manifest_observation"]=trust_observation
    assert canonical_json_bytes(trust)==canonical_json_bytes(admission_setup.trust)
    docs=fixture.documents
    candidate=docs["candidate_freeze_receipt"]
    binding=copy.deepcopy(admission_setup.binding)
    candidate["candidate_commit"]=binding["execution_commit"]
    candidate["candidate_tree"]=binding["execution_tree"]
    for name in ("source_integrity_commitment_sha256","runtime_registry_commitment_sha256","implementation_commitment_sha256",
                 "runner_config_commitment_sha256","closure_evidence_contract_commitment_sha256","dependency_lock_sha256"):
        candidate[name]=binding[name]
    fixture._finalize_document(candidate,("freeze_authority",))
    prior=docs["candidate_frozen_ledger_entry"]
    candidate_entry=fixture._ledger_entry(entry_type="candidate_frozen",sequence=7,previous_head=prior["previous_head_sha256"],
        occurred_at="2026-09-05T02:00:04Z",candidate_hash=candidate["document_sha256"],envelope_hash=None,grant_hash=None,consumption_hash=None)
    docs["candidate_frozen_ledger_entry"]=candidate_entry
    seen=docs["seen_case_exclusion_manifest"]
    seen["candidate_freeze_receipt_sha256"]=candidate["document_sha256"]
    fixture._finalize_document(seen,("freeze_authority","task_custodian"))
    envelope=docs["preregistration_envelope"]
    envelope["valid_until_utc"]="2026-09-05T23:00:00Z"
    binding.update(candidate_freeze_receipt_sha256=candidate["document_sha256"],candidate_freeze_ledger_entry_sha256=candidate_entry["entry_sha256"])
    envelope["execution_binding"]=binding
    envelope["candidate_frozen_at_utc"]=candidate["frozen_at_utc"]
    envelope["task_custody"]["seen_task_exclusion_manifest_sha256"]=seen["document_sha256"]
    envelope["external_trust"].update(ledger_base_sequence=7,ledger_base_head_sha256=candidate_entry["resulting_head_sha256"])
    runtime=envelope["runtime_plan"]
    budget=runtime["budget_policy"]
    budget_body=dict(budget);budget_body.pop("budget_policy_commitment_sha256")
    budget["budget_policy_commitment_sha256"]=_hash(RUNTIME_PLAN_DOMAIN,b"budget_policy",canonical_json_bytes(budget_body))
    runtime_body=dict(runtime);runtime_body.pop("external_plan_binding_sha256")
    runtime["external_plan_binding_sha256"]=_hash(RUNTIME_PLAN_DOMAIN,canonical_json_bytes(runtime_body))
    fixture._finalize_document(envelope,("freeze_authority","task_custodian"))
    prereg=fixture._ledger_entry(entry_type="preregistration_frozen",sequence=8,previous_head=candidate_entry["resulting_head_sha256"],
        occurred_at="2026-09-05T02:00:09Z",candidate_hash=candidate["document_sha256"],envelope_hash=envelope["document_sha256"],grant_hash=None,consumption_hash=None)
    docs["preregistration_frozen_ledger_entry"]=prereg
    grant=docs["authorization_grant"]
    grant.update(candidate_commit=binding["execution_commit"],candidate_tree=binding["execution_tree"],
        source_integrity_commitment_sha256=binding["source_integrity_commitment_sha256"],runtime_registry_commitment_sha256=binding["runtime_registry_commitment_sha256"],
        preregistration_envelope_sha256=envelope["document_sha256"],preregistration_freeze_entry_sha256=prereg["entry_sha256"],
        ledger_sequence=8,ledger_head_sha256=prereg["resulting_head_sha256"],external_plan_binding_sha256=runtime["external_plan_binding_sha256"],
        denominator_plan_commitment_sha256=runtime["denominator_plan_commitment_sha256"],budget_policy_commitment_sha256=budget["budget_policy_commitment_sha256"])
    for name in ("pricing_snapshot_date","pricing_source_snapshot_sha256","pricing_retrieved_at_utc","official_pricing_attestation_current"):
        grant[name]=budget[name]
    grant_body=dict(grant)
    for name in ("explicit_user_authorization_binding_sha256","document_sha256","signatures"):
        grant_body.pop(name)
    grant["explicit_user_authorization_binding_sha256"]=_hash(USER_BINDING_DOMAIN,canonical_json_bytes(grant_body))
    fixture._finalize_document(grant,("freeze_authority","task_custodian"))
    consumed=docs["consumption_receipt"]
    consumed.update(candidate_commit=binding["execution_commit"],preregistration_envelope_sha256=envelope["document_sha256"],
        preregistration_freeze_entry_sha256=prereg["entry_sha256"],authorization_grant_sha256=grant["document_sha256"],
        denominator_plan_commitment_sha256=runtime["denominator_plan_commitment_sha256"],external_plan_binding_sha256=runtime["external_plan_binding_sha256"])
    fixture._finalize_document(consumed)
    consumed_entry=fixture._ledger_entry(entry_type="authorization_consumed",sequence=9,previous_head=prereg["resulting_head_sha256"],
        occurred_at="2026-09-05T02:00:15Z",candidate_hash=candidate["document_sha256"],envelope_hash=envelope["document_sha256"],
        grant_hash=grant["document_sha256"],consumption_hash=consumed["document_sha256"])
    docs["authorization_consumed_ledger_entry"]=consumed_entry
    observation=fixture.observation
    observation["candidate_freeze_anchor"]=helpers._anchor(candidate_entry,"2026-09-05T02:00:05Z")
    observation["preregistration_freeze_anchor"]=helpers._anchor(prereg,"2026-09-05T02:00:10Z")
    observation["authorization_consumed_anchor"]=helpers._anchor(consumed_entry,"2026-09-05T02:00:16Z")
    observation["preregistration_envelope_observation"]["document_sha256"]=envelope["document_sha256"]
    observation["user_authorization_observation"]["authorization_binding_sha256"]=grant["explicit_user_authorization_binding_sha256"]
    observation["manifest_observation"]["observed_at_utc"]="2026-09-05T02:03:30Z"
    # Place the actual synthetic campaign rows inside the 600-second budget.
    for group in ("run_rows","event_rows","model_call_rows"):
        semantic[group]=shift(semantic[group],hours=-1)
    helpers._rebind_semantic_fixture(semantic,envelope_id=envelope["envelope_id"],
        campaign_id=runtime["campaign_topology"]["campaign_id"],runtime_plan=runtime,
        authorization_grant_sha256=grant["document_sha256"],model_id=binding["model_id"])
    postrun=helpers._postrun(fixture)
    postrun.update(completed_at_utc="2026-09-05T02:03:00Z",receipt_issued_at_utc="2026-09-05T02:04:00Z",
                   task_released_at_utc="2026-09-05T02:00:17Z",provider_key_loaded_at_utc="2026-09-05T02:00:18Z")
    review_observation=json_copy(admission_setup.observation)
    review_observation.update(candidate_freeze_receipt_sha256=candidate["document_sha256"],candidate_frozen_at_utc=candidate["frozen_at_utc"])
    admission=replace(admission_setup.inputs,review_observation=canonical_json_bytes(review_observation),
        expected_candidate_freeze_receipt_sha256=candidate["document_sha256"],expected_candidate_frozen_at_utc=candidate["frozen_at_utc"])
    return fixture,semantic,postrun,admission


def json_copy(value):
    return copy.deepcopy(value)
