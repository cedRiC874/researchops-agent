"""Explicit immutable first-live versions; no context/global mutation or I/O."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True,slots=True)
class FirstLiveExecutionProfile:
    version: int
    implementation_path: str
    implementation_bytes: int
    implementation_sha256: str
    implementation_commitment_sha256: str
    source_plan_id: str
    source_plan_path: str


_V2=FirstLiveExecutionProfile(2,
    "evals/provider_completion_first_live_validation_v1/deepseek_responses_adapter_validation_implementation_v2.json",9273,
    "97667a65a8b88da9ce3ea21136c4060529408a83c8c862bbb98eb3e0c4d8bc9c",
    "187bb6b537f2bdffb4bf77581550ea0ca81d02fb52d44d110b22a450ae0dce10",
    "phase6-deepseek-depth60-v5","evals/phase6_deepseek_depth60_plan_v5.json")
_V3=FirstLiveExecutionProfile(3,
    "evals/provider_completion_first_live_validation_v1/deepseek_responses_adapter_validation_implementation_v3.json",3721,
    "6e11120ad2463ae4aaeed076b4e490e98e977fb21c8e71fce91e9bb4bc9118eb",
    "b320f096ac7afdfd68516e3d2a5be526b99cb99e6a557ee414d8f390900bd6a7",
    "phase6-deepseek-depth60-v7","evals/phase6_deepseek_depth60_plan_v7.json")


def execution_profile(version: int=2) -> FirstLiveExecutionProfile:
    if type(version) is not int or version not in (2,3):
        raise ValueError("first_live_execution_version_unsupported")
    return _V2 if version==2 else _V3


__all__=["FirstLiveExecutionProfile","execution_profile"]
