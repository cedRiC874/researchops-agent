"""Offline timing-contract authoring validator; NOT a runtime or A/B verifier."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from researchops_external_closure.io import read_regular_file_no_follow
from researchops_completion_timing.contract import (
    CONTRACT_BYTES, CONTRACT_SHA256, DOMAINS, RELATIVE, SELF_FIELDS,
    TimingContractError, _EVENTS, commitment, digest, load_contract,
    runtime_plan_core_commitment, validate_documents,
)


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify-contract", "validate"))
    parser.add_argument("--project-root", type=Path, default=ROOT)
    for name in ("plan", "evidence", "events", "publication"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--manifest-sha256")
    args = parser.parse_args(arguments)
    try:
        contract, schemas = load_contract(args.project_root)
        if args.command == "verify-contract":
            result = {"status": "frozen_contract_valid", "contract_sha256": CONTRACT_SHA256,
                      "schema_count": len(schemas), "runtime_integrated": False,
                      "current_closure_claim_allowed": False, "provider_calls": 0, "real_key_loads": 0}
        else:
            result = validate_documents(args.project_root,
                plan_bytes=read_regular_file_no_follow(args.plan, max_bytes=contract["limits"]["max_plan_bytes"]),
                evidence_bytes=read_regular_file_no_follow(args.evidence, max_bytes=contract["limits"]["max_evidence_bytes"]),
                terminal_projection_bytes=read_regular_file_no_follow(args.events, max_bytes=contract["limits"]["max_evidence_bytes"]),
                publication_bytes=None if args.publication is None else read_regular_file_no_follow(args.publication, max_bytes=contract["limits"]["max_publication_bytes"]),
                expected_manifest_file_sha256=args.manifest_sha256)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except Exception as error:
        code = error.code if type(error) is TimingContractError else "timing_schema_invalid"
        print(json.dumps({"status": "rejected", "error_code": code, "current_closure_claim_allowed": False,
                          "provider_calls": 0, "real_key_loads": 0}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
