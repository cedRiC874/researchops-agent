"""Text observation v1.1 verification; byte-bound inputs and exclusive reports."""

import argparse
import hashlib
import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def fingerprints():
    paths = [*ROOT.glob("src/researchops_behavior_eval_v1/*.py"),
             *ROOT.glob("tests/test_behavior_eval_v1*.py"),
             ROOT / "evals/internal_behavior_eval_v1/scorer_cases.json",
             ROOT / "evals/internal_behavior_eval_v1/CONTRACT.md",
             ROOT / "evals/internal_behavior_eval_v1/README.md",
             ROOT / "evals/internal_behavior_eval_v1/text_observation_v1_1/REVISION.md",
             ROOT / "evals/internal_behavior_eval_v1/text_observation_v1_1/example-plan.json",
             Path(__file__).resolve()]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", required=True, choices=("regression", "related", "full"))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    # Reserve a new output before running; never overwrite a previous run's evidence.
    with args.report.open("x", encoding="utf-8", newline="\n") as output:
        before = fingerprints()
        started = datetime.now(timezone.utc).isoformat()
        pattern = "test_behavior_eval_v1_text_observation.py" if args.scope == "regression" else "test_behavior_eval_v1*.py"
        loader = unittest.TestLoader()
        if args.scope == "related":
            # Establish the same package discovery context; do not add tests/ to sys.path.
            discovered = loader.discover(str(ROOT / "tests"), pattern="test_behavior_eval_v1.py", top_level_dir=str(ROOT))
            suite = discovered if loader.errors else loader.loadTestsFromNames([
                "tests.test_behavior_eval_v1.InvariantTests." + name for name in (
                    "test_json_empty_answer_is_delivery_failure",
                    "test_core_no_io_or_runtime_dependency",
                    "test_deterministic_no_mutation_no_aliasing",
                    "test_optional_extra_is_checked",
                    "test_parser_does_not_read_gold")])
        else:
            suite = loader.discover(str(ROOT / "tests"), pattern=pattern, top_level_dir=str(ROOT))
        def ids(node):
            return [item for child in node for item in ids(child)] if isinstance(node, unittest.TestSuite) else [node.id()]
        case_ids = ids(suite)
        if len(case_ids) != len(set(case_ids)):
            raise RuntimeError("duplicate_test_ids")
        log = io.StringIO()
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        after = fingerprints()
        same = before == after
        exit_code = 0 if result.wasSuccessful() and same else 1
        record = {"scope": args.scope, "source_kind": "synthetic_fixture", "started_at_utc": started,
                  "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                  "skips": len(result.skipped), "exit_code": exit_code, "python": sys.version,
                  "test_ids": case_ids, "unique_test_id_count": len(set(case_ids)),
                  "discovery_top_level": "repository_root", "root_full_regression": False,
                  "binding": {"algorithm": "sha256", "before": before, "after": after, "unchanged": same,
                              "reports_included": False},
                  "failure_details": [{"test": str(test), "traceback": trace} for test, trace in result.failures],
                  "error_details": [{"test": str(test), "traceback": trace} for test, trace in result.errors],
                  "skip_details": [{"test": str(test), "reason": reason} for test, reason in result.skipped],
                  "test_log": log.getvalue()}
        json.dump(record, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps({k: record[k] for k in ("scope", "tests", "failures", "errors", "skips", "exit_code")}, ensure_ascii=False))
    print("Bound files unchanged:", same)
    if not result.wasSuccessful():
        print(log.getvalue())
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
