"""Isolated subprocess harness: real Internal entry, fake Key/store, MockTransport.

Only the OS store locator and HTTP delegate are substituted. Fault modes mutate
that delegate's response or isolated source bytes, never admission return values.
"""
from __future__ import annotations
import asyncio
import copy
from datetime import timedelta
import json
import logging
import os
from pathlib import Path
import socket
import shutil
import sys
import uuid
from unittest.mock import patch

import httpx2
from agents import set_tracing_disabled
from researchops_completion_timing import local_claim
from researchops_internal_telemetry import admission, contract as c, runtime, run, source
from researchops_internal_telemetry.verify import verify_archive


def response_body():
    return dict(id="resp_internal_synthetic",object="response",created_at=1,model="deepseek-v4-flash",status="completed",
        error=None,incomplete_details=None,output=[dict(id="msg_internal",type="message",status="completed",role="assistant",
            content=[dict(type="output_text",text="synthetic response never persisted",annotations=[])])],
        parallel_tool_calls=False,tools=[],usage=dict(input_tokens=5,input_tokens_details=dict(cached_tokens=0),output_tokens=2,
            output_tokens_details=dict(reasoning_tokens=0),total_tokens=7))


async def exercise(mode, known):
    set_tracing_disabled(True)
    logging.disable(logging.CRITICAL)
    os.environ["DEEPSEEK_API_KEY"]="FIXTURE-INTERNAL-KEY-NOT-REAL"
    instant=c.now()
    pricing=dict(schema_version="provider-completion-internal-pricing/1.0",status="user_reviewed_official_snapshot",
        provider_id="deepseek",requested_model="deepseek-v4-flash",input_price_per_million_cny="2.000000",output_price_per_million_cny="8.000000",
        cache_discount_assumed=False,provider_invoice_hard_cap=False,evidence_date_utc=instant.isoformat().replace("+00:00","Z"),official_evidence_sha256="1"*64)
    freeze=admission.build_freeze(execution_commit=source.git(c.ROOT,"rev-parse","HEAD").decode().strip(),pricing=pricing,
        review_record=dict(kind="internal_review",preparer="synthetic_test",reviewer="synthetic_test",same_person=True,developer_known=True,
            old_task_exclusion_record_sha256=c.digest(c.read(c.ROOT/c.DIRECTORY/"case_origin_v1.json"))))
    candidate=dict(schema_version="provider-completion-internal-authorization-candidate/1.0",scope=c.SCOPE,
        authorization_id="internal-offline-fixture-"+uuid.uuid4().hex,freeze_commitment_sha256=c.commitment("freeze",freeze),
        execution_environment_id=c.ENVIRONMENT_ID,not_before_utc=(instant-timedelta(seconds=5)).isoformat().replace("+00:00","Z"),
        expires_at_utc=(instant+timedelta(hours=2)).isoformat().replace("+00:00","Z"),output_namespace="output/internal-telemetry-v1")
    if mode=="expired":candidate["expires_at_utc"]=(instant-timedelta(seconds=1)).isoformat().replace("+00:00","Z")
    approved=c.commitment("authorization-candidate",candidate)
    authorization=dict(schema_version="provider-completion-internal-authorization/1.0",candidate=candidate,
        approval_observation=dict(kind="explicit_user_approval",approved_digest=approved,observed_at_utc=instant.isoformat().replace("+00:00","Z"),message_reference="synthetic-test-only"))
    calls=[]
    def handler(request):
        calls.append(dict(method=request.method,url=str(request.url),cap=json.loads(request.content)["max_output_tokens"]))
        value=response_body()
        if mode=="length":value.update(status="incomplete",incomplete_details=dict(reason="max_output_tokens"),output=[])
        if mode=="missing":value.pop("status")
        if mode=="unknown":value["status"]="future_native_status"
        if mode=="usage":value["usage"]=None
        if mode=="http":return httpx2.Response(429,json={"error":{"message":"synthetic error"}})
        if mode=="timeout":raise httpx2.ReadTimeout("synthetic timeout",request=request)
        if mode=="cancel":raise asyncio.CancelledError()
        if mode=="privacy":value["incomplete_details"]={"reason":"sk-FAKESECRET0123456789"}
        if mode=="source_drift":
            path=c.ROOT/"src/researchops_internal_telemetry/__init__.py"
            path.write_bytes(path.read_bytes()+b"\n# isolated fault injection\n")
        return httpx2.Response(200,json=value,headers={"x-request-id":"synthetic-only"})
    # Production gate is exercised unchanged; no positive verdicts are mocked.
    with patch.object(local_claim,"_windows_local_app_data",return_value=known), \
         patch.object(runtime,"_native_transport",side_effect=lambda:httpx2.MockTransport(handler)), \
         patch("socket.socket.connect",side_effect=AssertionError("real network forbidden")), \
         patch("socket.getaddrinfo",side_effect=AssertionError("real network forbidden")):
        if not (known/"ResearchOpsAgent/completion-claims-v1").exists():
            with patch.object(local_claim.secrets,"token_hex",return_value=c.ENVIRONMENT_ID[7:].lower()):
                local_claim.provision_local_claim_store()
        result=await run.run_internal_v1(freeze_bytes=c.raw(freeze),authorization_bytes=c.raw(authorization),approved_digest=approved,confirm_online=True)
        if mode=="duplicate":
            repeated=await run.run_internal_v1(freeze_bytes=c.raw(freeze),authorization_bytes=c.raw(authorization),approved_digest=approved,confirm_online=True)
            result["repeated_result"]=repeated
    base=c.ROOT/"output/internal-telemetry-v1"/c.digest(candidate["authorization_id"].encode())
    return dict(result=result,calls=calls,approved_digest=approved,archive=str(base),
        source_commitment_sha256=freeze["source_commitment_sha256"],mode=mode,
        synthetic_approval=True,real_provider_calls=0,real_store_access=False)


if __name__=="__main__":
    try:
        if sys.argv[1] == "verify":
            archive=Path(sys.argv[2])
            bundle_path=archive.parent/(archive.name+".bundle.json")
            checked=verify_archive(archive,bundle_bytes=bundle_path.read_bytes(),expected_bundle_commitment=sys.argv[3],
                expected_approved_digest=sys.argv[4],observed_exit_code=int(sys.argv[5]))
            print(json.dumps(checked));raise SystemExit(0)
        if sys.argv[1] in ("tamper","tamper_semantic"):
            archive=Path(sys.argv[2]);copy_path=archive.parent/(archive.name+"."+sys.argv[1])
            shutil.copytree(archive,copy_path)
            path=copy_path/"completion_telemetry.json"
            value=json.loads(path.read_bytes());value["records"][0]["normalized_completion_state"]="incomplete_length"
            path.write_bytes(c.raw(value))
            bundle_bytes=(archive.parent/(archive.name+".bundle.json")).read_bytes()
            expected_bundle=sys.argv[3]
            if sys.argv[1] == "tamper_semantic":
                # Rebuild all container hashes deliberately. Semantic replay,
                # rather than the original outer checksum, must reject this.
                manifest=json.loads((copy_path/"manifest.json").read_bytes())
                for item in manifest["files"]:
                    blob=(copy_path/item["path"]).read_bytes()
                    item.update(bytes=len(blob),sha256=c.digest(blob))
                (copy_path/"manifest.json").write_bytes(c.raw(manifest))
                publication=json.loads((copy_path/"publication.json").read_bytes())
                publication.update(manifest_sha256=c.digest(c.raw(manifest)),manifest_commitment_sha256=c.commitment("manifest",manifest))
                (copy_path/"publication.json").write_bytes(c.raw(publication))
                outer=json.loads(bundle_bytes)
                for item in outer["files"]:
                    blob=(copy_path/item["path"]).read_bytes()
                    item.update(bytes=len(blob),sha256=c.digest(blob))
                body={key:outer[key] for key in ("schema_version","files")}
                expected_bundle=c.commitment("bundle",body)
                outer["commitment_sha256"]=expected_bundle;bundle_bytes=c.raw(outer)
            try:
                verify_archive(copy_path,bundle_bytes=bundle_bytes,expected_bundle_commitment=expected_bundle,
                    expected_approved_digest=sys.argv[4],observed_exit_code=0)
            except Exception as error:
                print(json.dumps(dict(rejected=True,code=getattr(error,"code",None),mutation_applied=True)));raise SystemExit(0)
            print(json.dumps(dict(rejected=False)));raise SystemExit(1)
        outcome=asyncio.run(exercise(sys.argv[1],Path(sys.argv[2])))
        print(json.dumps(outcome,ensure_ascii=True))
    except SystemExit:
        raise
    except BaseException as error:
        print(json.dumps(dict(probe_error_type=type(error).__name__,probe_error_code=getattr(error,"code",None))))
        raise
