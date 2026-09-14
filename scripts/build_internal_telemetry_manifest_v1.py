"""Create-once Internal source manifest. Never rewrites v11 or an existing file."""
from pathlib import Path

from researchops_internal_telemetry.source import MANIFEST, build_manifest
from researchops_internal_telemetry.contract import ROOT, raw
from researchops_completion_timing.first_live_publish import _write_exclusive


def main():
    result = build_manifest(ROOT)
    _write_exclusive(ROOT / MANIFEST, raw(result), [])
    print(raw(dict(source_commitment_sha256=result["commitment_sha256"], file_count=len(result["files"]))).decode())


if __name__ == "__main__":
    main()
