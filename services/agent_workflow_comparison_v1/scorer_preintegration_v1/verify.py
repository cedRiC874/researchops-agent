"""Portable public verification; historical workstation preservation is separate."""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
sys.path.insert(0, str(REPO))

import argparse
import hashlib
import io
import json
import traceback
import unittest


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inputs():
    carried = json.loads((ROOT / "carryover.json").read_text(encoding="utf-8"))["carried_files"]
    fixed = json.loads((ROOT / "upstream.json").read_text(encoding="utf-8"))["fixed_files_sha256"]
    paths = [REPO / p for p in {*carried, *fixed}]
    paths += [p for p in ROOT.iterdir() if p.is_file() and p.suffix in {".py", ".json", ".md"}]
    return {p.relative_to(REPO).as_posix(): sha(p) for p in sorted(set(paths))}


def preserved():
    carry = json.loads((ROOT / "carryover.json").read_text(encoding="utf-8"))
    return {"historical_origin_check": "not_performed_in_this_context",
            "historical_origin_record_sha256": carry["historical_origin_record_sha256"],
            "carried_files_unchanged": all(sha(REPO / p) == h for p, h in carry["carried_files"].items())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("new", "joint"), required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    target = Path(args.report).resolve()
    if not target.is_relative_to(ROOT / "outputs") or target.suffix != ".json":
        raise ValueError("report must be inside the new outputs directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8", newline="\n") as out:
        before = inputs()
        stream = io.StringIO()
        counts = dict(tests=0, failures=0, errors=0, skips=0)
        bootstrap = None
        try:
            from services.agent_workflow_comparison_v1.scorer_preintegration_v1 import bootstrap, test_preintegration, test_publication
            suite = unittest.defaultTestLoader.loadTestsFromModule(test_preintegration)
            suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(test_publication))
            if args.scope == "joint":
                from services.agent_workflow_comparison_v1.aggregate_read_v1 import test_aggregate
                suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(test_aggregate))
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
            counts = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors), skips=len(result.skipped))
            code = 0 if result.wasSuccessful() else 1
        except Exception:
            stream.write(traceback.format_exc())
            code = 2
            counts["errors"] += 1
        after = inputs()
        preservation = preserved()
        if before != after or not all(v for k, v in preservation.items() if k.endswith("unchanged")):
            code = 1
        report = {"scope": args.scope, **counts, "actual_exit_code": code,
                  "business_path_case_count": 16, "predeclared_fault_case_count": 24,
                  "sha256_before": before, "sha256_after": after, "inputs_unchanged": before == after,
                  "preservation": preservation, "log": stream.getvalue(),
                  "identity": bootstrap.IDENTITY if bootstrap else None,
                  "environment": bootstrap.ENVIRONMENT if bootstrap else None,
                  "preparation_record": json.loads((ROOT / "outputs/preparation.json").read_text(encoding="utf-8"))
                      if (ROOT / "outputs/preparation.json").is_file() else None,
                  "formal_integration_accepted": False}
        json.dump(report, out, ensure_ascii=False, indent=2)
        out.write("\n")
        print(stream.getvalue())
        print(json.dumps({**counts, "actual_exit_code": code, "inputs_unchanged": before == after, **preservation}))
        if bootstrap:
            bootstrap.aggregate_run.LOOP.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
