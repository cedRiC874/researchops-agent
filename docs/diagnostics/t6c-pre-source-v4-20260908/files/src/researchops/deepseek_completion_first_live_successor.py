"""First-live implementation v3: explicit profile, existing bounded Adapter engine.

Public APIs do not accept Key, clock, Git, transport or verifier injection.
The normative v3 contract is not an online grant. All calls remain subject to
the independent one-shot binding and the complete pre-consumption run gates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import date,datetime,timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import deepseek_completion_first_live_validation as engine
from .first_live_execution_profiles import execution_profile


def validate_successor_contract(project_root: str | Path) -> dict[str,Any]:
    root=Path(project_root).resolve()
    profile=execution_profile(3)
    engine.validate_deepseek_first_live_contract(root)
    engine._validate_execution_profile_implementation(root,_execution_version=3)
    source_valid=False
    source_commitment=None
    if (root/profile.source_plan_path).is_file():
        from researchops_external_closure.execution_current_v2 import verify_current_profile
        checked=verify_current_profile(root,profile="first_live")
        source_commitment=checked.source_integrity_commitment_sha256
        source_valid=True
    return {"status":"offline_controls_valid_requires_fresh_authorization" if source_valid else "offline_source_snapshot_required",
            "execution_version":3,"contract_commitment_sha256":engine.CONTRACT_COMMITMENT_SHA256,
            "implementation_contract_sha256":profile.implementation_sha256,
            "implementation_commitment_sha256":profile.implementation_commitment_sha256,
            "source_integrity_plan_id":profile.source_plan_id,"source_integrity_commitment_sha256":source_commitment,
            "source_profile_valid":source_valid,"online_execution_authorized":False,"runtime_admission_verified":False,
            "provider_key_loaded":False,"network_calls":0,"model_calls":0}


def calculate_successor_authorization_binding(
    *,project_root: str | Path,authorization_id: str,authorization_expires_at_utc: str,
    expected_contract_commitment_sha256: str,expected_source_integrity_commitment_sha256: str,
    expected_execution_commit: str,pricing_snapshot_date: str,pricing_source_url: str,
    input_price_per_million_cny: str | Decimal,output_price_per_million_cny: str | Decimal,
) -> dict[str,Any]:
    root=Path(project_root).resolve()
    profile=execution_profile(3)
    engine.validate_deepseek_first_live_contract(root)
    engine._validate_execution_profile_implementation(root,_execution_version=3)
    if expected_contract_commitment_sha256!=engine.CONTRACT_COMMITMENT_SHA256:
        raise engine._error("deepseek_first_live_contract_not_authorized",not_run=True)
    if type(authorization_id) is not str or engine._AUTHORIZATION_ID.fullmatch(authorization_id) is None:
        raise engine._error("deepseek_first_live_authorization_invalid",not_run=True)
    if type(expected_source_integrity_commitment_sha256) is not str or engine._SHA256.fullmatch(expected_source_integrity_commitment_sha256) is None:
        raise engine._error("deepseek_first_live_source_commitment_invalid",not_run=True)
    if type(expected_execution_commit) is not str or engine._GIT_SHA.fullmatch(expected_execution_commit) is None:
        raise engine._error("deepseek_first_live_execution_commit_invalid",not_run=True)
    expiry=engine._timestamp(engine._parse_utc(authorization_expires_at_utc))
    try:
        date.fromisoformat(pricing_snapshot_date)
    except (TypeError,ValueError):
        raise engine._error("deepseek_first_live_pricing_invalid",not_run=True) from None
    if pricing_source_url!=engine._PRICING_SOURCE_URL:
        raise engine._error("deepseek_first_live_pricing_source_invalid",not_run=True)
    input_price=engine._price(input_price_per_million_cny)
    output_price=engine._price(output_price_per_million_cny)
    engine._validate_locked_cost_reservation(input_price,output_price,not_run=True)
    identifier=engine._sha256(authorization_id.encode())
    binding=engine._authorization_binding(authorization_id_sha256=identifier,expires_at_utc=expiry,
        execution_commit=expected_execution_commit,source_integrity_commitment=expected_source_integrity_commitment_sha256,
        pricing_snapshot_date=pricing_snapshot_date,pricing_source_url=pricing_source_url,input_price=input_price,output_price=output_price,
        _execution_version=3)
    return {"schema_version":"deepseek-first-live-authorization-binding/2.0","status":"offline_binding_calculated_not_authorized",
            "authorization_id_sha256":identifier,"authorization_binding_sha256":binding,
            "contract_commitment_sha256":engine.CONTRACT_COMMITMENT_SHA256,"implementation_commitment_sha256":profile.implementation_commitment_sha256,
            "source_integrity_commitment_sha256":expected_source_integrity_commitment_sha256,"execution_commit":expected_execution_commit,
            "authorization_expires_at_utc":expiry,"provider_key_loaded":False,"network_calls":0,"model_calls":0,"authorizes_online_execution":False}


async def run_successor_validation(
    *,project_root: str | Path,authorization_id: str | None,authorization_expires_at_utc: str | None,
    expected_contract_commitment_sha256: str | None,expected_source_integrity_commitment_sha256: str | None,
    expected_execution_commit: str | None,expected_authorization_binding_sha256: str | None,
    pricing_snapshot_date: str | None,pricing_source_url: str | None,
    input_price_per_million_cny: str | Decimal | None,output_price_per_million_cny: str | Decimal | None,
    confirm_online: bool,accept_locked_caps: bool,attest_pricing_current: bool,
) -> dict[str,Any]:
    return await engine._run_deepseek_first_live_validation_impl(project_root=project_root,authorization_id=authorization_id,
        authorization_expires_at_utc=authorization_expires_at_utc,expected_contract_commitment_sha256=expected_contract_commitment_sha256,
        expected_source_integrity_commitment_sha256=expected_source_integrity_commitment_sha256,expected_execution_commit=expected_execution_commit,
        expected_authorization_binding_sha256=expected_authorization_binding_sha256,pricing_snapshot_date=pricing_snapshot_date,
        pricing_source_url=pricing_source_url,input_price_per_million_cny=input_price_per_million_cny,output_price_per_million_cny=output_price_per_million_cny,
        confirm_online=confirm_online,accept_locked_caps=accept_locked_caps,attest_pricing_current=attest_pricing_current,
        _key_loader=lambda:os.environ.get("DEEPSEEK_API_KEY"),_clock=lambda:datetime.now(timezone.utc),
        _git_state_loader=engine._git_state,_artifact_root=None,_monotonic=engine.time.monotonic,_execution_version=3)


def main(arguments=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("validate","bind-authorization","run"))
    parser.add_argument("--project-root",type=Path,default=Path.cwd())
    for name in ("authorization-id","authorization-expires-at-utc","expected-contract-commitment-sha256",
                 "expected-source-integrity-commitment-sha256","expected-execution-commit","expected-authorization-binding-sha256",
                 "pricing-snapshot-date","pricing-source-url","input-price-per-million-cny","output-price-per-million-cny"):
        parser.add_argument("--"+name)
    parser.add_argument("--confirm-online",action="store_true")
    parser.add_argument("--accept-locked-caps",action="store_true")
    parser.add_argument("--attest-pricing-current",action="store_true")
    values=vars(parser.parse_args(arguments))
    command=values.pop("command")
    try:
        if command=="validate":
            result=validate_successor_contract(values["project_root"])
        elif command=="bind-authorization":
            for name in ("expected_authorization_binding_sha256","confirm_online","accept_locked_caps","attest_pricing_current"):
                values.pop(name)
            result=calculate_successor_authorization_binding(**values)
        else:
            result=asyncio.run(run_successor_validation(**values))
        print(json.dumps(result,ensure_ascii=True,indent=2))
        return 0 if result.get("status") not in ("not_run","failed") else 4
    except (Exception,asyncio.CancelledError) as error:
        code=getattr(error,"code",None)
        if type(code) is not str or engine._SAFE_ERROR.fullmatch(code) is None:
            code="deepseek_first_live_successor_gate_failed"
        # An exception after entering run may follow consumption or a send.
        # Never rewrite uncertain counters as zero or claim it was not run.
        uncertain=command=="run"
        print(json.dumps({"status":"failed" if uncertain else "not_run","error_code":code,
                          "outcome_unknown":uncertain,"authorization_consumed":None if uncertain else False,
                          "provider_key_loaded":None if uncertain else False,"network_calls":None if uncertain else 0,
                          "model_calls":None if uncertain else 0,"online_execution_authorized":False}))
        return 4


if __name__=="__main__":
    raise SystemExit(main())
