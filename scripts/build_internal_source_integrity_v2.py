"""Create a new offline-only v2 manifest after the authorized final base is fixed."""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from researchops_internal_telemetry.source_integrity_v2 import _git, require, write_manifest_exclusive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-offline-source-only", action="store_true", required=True)
    parser.add_argument("--expected-base-commit", required=True)
    args = parser.parse_args()
    require(bool(re.fullmatch("[0-9a-f]{40}", args.expected_base_commit)), "base_commit_format")
    require(_git(ROOT, "rev-parse", "HEAD").decode("ascii").strip() == args.expected_base_commit, "base_commit_mismatch")
    print(json.dumps(write_manifest_exclusive(ROOT), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
