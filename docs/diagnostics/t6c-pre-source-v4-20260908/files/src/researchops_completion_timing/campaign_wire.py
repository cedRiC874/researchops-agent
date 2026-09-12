"""Fixed private input rendering and comparison; no dispatch or runtime grant."""
from researchops_external_closure.io import read_regular_file_no_follow
from researchops_external_closure.primitives import decode_strict_json_object
from .contract import digest, fail
from .campaign_opening import _OpenedCampaignTasks

PROFILE_PATH = 'evals/provider_completion_campaign_wire_v1/contract_v1.json'
PROFILE_SHA256 = '8f4760755b079c287b42903864d07b99da088c332e538ceb3c1f5fd6e09c79cb'


def _profile(root):
    value = read_regular_file_no_follow(root / PROFILE_PATH, max_bytes=4096)
    if digest(value) != PROFILE_SHA256: fail('campaign_wire_contract_invalid')
    return decode_strict_json_object(value, max_bytes=4096)


def _render_case_input(root, opening, case_index):
    if type(opening) is not _OpenedCampaignTasks or type(case_index) is not int or not 0 <= case_index < len(opening._cases):
        fail('campaign_wire_case_invalid')
    profile = _profile(root)
    return opening._cases[case_index]['user_input'] + profile['canary_prefix'] + opening._canary_b64 + profile['canary_suffix']


def _verify_wire_case_input(root, opening, case_index, wire_input):
    expected = [{'role': 'user', 'content': _render_case_input(root, opening, case_index)}]
    if type(wire_input) is not list or wire_input != expected:
        fail('campaign_wire_input_mismatch')
    return dict(case_handle=opening._cases[case_index]['case_handle'],
        execution_input_commitment_sha256=opening.execution_input_commitments[case_index],
        input_and_canary_match=True, full_request_validated=False, runtime_authority_granted=False)
