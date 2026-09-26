"""Exclusive offline v3 build. Never updates v1/v2 or grants runtime admission."""
from pathlib import Path
import argparse
import json
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from researchops_internal_telemetry import source_integrity_v3 as source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-base", required=True)
    args = parser.parse_args()
    source.require(args.expected_base == source.HISTORICAL_COMMIT, "expected_base")
    source.v2._git(ROOT, "merge-base", "--is-ancestor", args.expected_base, "HEAD")
    print(json.dumps(source.write_manifest_exclusive(ROOT)))
    return 0


if __name__ == "__main__": raise SystemExit(main())
