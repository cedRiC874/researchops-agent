"""Join current v11/v12 bytes to fixed historical Git identity, never a permit."""
from dataclasses import dataclass,field
import re

from .execution_binding_v4 import verify_historical_timed_profile
from .execution_current_v4 import verify_current_timed_profile
from .execution_current_v3 import _root
from .execution_local_v3 import _head
from .errors import ExternalClosurePrimitiveError


def _fail(code): raise ExternalClosurePrimitiveError('execution_local_v4_'+code) from None


@dataclass(frozen=True,slots=True)
class LocalTimedExecutionIdentity:
    profile: str
    execution_commit: str
    execution_tree: str
    source_integrity_commitment_sha256: str
    source_manifest_commitment_sha256: str
    source_recipe_version: int=field(default=4,init=False)
    current_and_historical_source_matched: bool=field(default=True,init=False)
    head_matched_at_both_observations: bool=field(default=True,init=False)
    repository_globally_clean_verified: bool=field(default=False,init=False)
    installed_environment_verified: bool=field(default=False,init=False)
    fresh_authorization_verified: bool=field(default=False,init=False)
    winning_claim_verified: bool=field(default=False,init=False)
    runtime_authority_granted: bool=field(default=False,init=False)
    closure_claim_allowed: bool=field(default=False,init=False)


def verify_local_timed_execution_identity(project_root,*,profile,expected_commit,expected_tree,
        expected_source_integrity_commitment_sha256,expected_source_manifest_commitment_sha256):
    if type(profile) is not str or profile not in {'first_live','campaign'}: _fail('expectation_invalid')
    for value,length in ((expected_commit,40),(expected_tree,40),
                         (expected_source_integrity_commitment_sha256,64),(expected_source_manifest_commitment_sha256,64)):
        if type(value) is not str or re.fullmatch(r'[0-9a-f]{'+str(length)+'}',value) is None or value=='0'*length:
            _fail('expectation_invalid')
    root=_root(project_root)
    if _head(root)!=expected_commit: _fail('head_mismatch')
    historical=verify_historical_timed_profile(root,commit=expected_commit,tree=expected_tree,profile=profile)
    current=verify_current_timed_profile(root,profile=profile)
    for checked in (historical.components,current):
        if (checked.profile!=profile or checked.source_integrity_commitment_sha256!=expected_source_integrity_commitment_sha256
            or checked.implementation_commitment_sha256!=expected_source_manifest_commitment_sha256):
            _fail('commitment_mismatch')
    if historical.components.component_hashes!=current.component_hashes: _fail('component_mismatch')
    if _head(root)!=expected_commit: _fail('head_changed')
    return LocalTimedExecutionIdentity(profile,expected_commit,expected_tree,
        expected_source_integrity_commitment_sha256,expected_source_manifest_commitment_sha256)
