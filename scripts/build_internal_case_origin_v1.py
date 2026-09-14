"""Create-once internal authorship/exact-exclusion record; no runtime permission."""
from researchops_internal_telemetry.contract import ROOT, raw, digest
from researchops_internal_telemetry.task_origin import ORIGIN_PATH, build_origin_record
from researchops_completion_timing.first_live_publish import _write_exclusive

if __name__=="__main__":
    payload=raw(build_origin_record())
    _write_exclusive(ROOT/ORIGIN_PATH,payload,[])
    print(raw(dict(origin_record_sha256=digest(payload),provider_calls=0)).decode())
