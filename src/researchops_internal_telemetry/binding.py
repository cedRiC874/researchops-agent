"""Private Internal authority factory; never promote an external registry entry."""
from researchops_completion_telemetry import surface_mapping as mapping
from .admission import _ClaimedInternal
from .contract import ROOT, RUNTIME_SCOPE, require


def _binding_for_claim(owner):
    require(type(owner) is _ClaimedInternal and owner._taken and not owner._mapping_taken, "mapping_owner_required")
    owner.check(source_check=True)
    owner._mapping_taken = True
    offline = mapping.load_and_select_surface_mapping(ROOT, "deepseek", "responses", "openai_compatible_responses", purpose="offline_validation")
    selection = mapping.VerifiedSurfaceSelection._create(mapping._SELECTION_TOKEN, purpose=RUNTIME_SCOPE,
        telemetry_schema_sha256=offline.telemetry_schema_sha256, mapping=offline.mapping_snapshot(),
        entry=dict(adapter_version=offline.adapter_version, mapping_version=offline.mapping_version,
            output_counter_comparability=offline.output_counter_comparability, output_counter_path=offline.output_counter_path,
            runtime_binding_allowed=True))
    return mapping.VerifiedRuntimeCompletionBinding._create(mapping._RUNTIME_BINDING_TOKEN, selection)
