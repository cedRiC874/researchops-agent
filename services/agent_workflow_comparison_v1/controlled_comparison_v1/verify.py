from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[2]))
import argparse
import io
import json
import unittest
from services.agent_workflow_comparison_v1.controlled_comparison_v1.binding import source_snapshot, sha


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    target = Path(args.report).resolve()
    if not target.is_relative_to(ROOT / "outputs") or target.suffix != ".json":
        raise ValueError("isolated_report_required")
    target.parent.mkdir(parents=True, exist_ok=True)
    before = source_snapshot()
    gold_before = sha(ROOT / "scoring/contracts.json")
    with target.open("x", encoding="utf-8", newline="\n") as out:
        from services.agent_workflow_comparison_v1.controlled_comparison_v1 import test_controlled, runtime
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(test_controlled))
        after = source_snapshot()
        gold_after = sha(ROOT / "scoring/contracts.json")
        code = 0 if result.wasSuccessful() and before == after and gold_before == gold_after else 1
        report = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors), "skips": len(result.skipped),
                  "actual_exit_code": code, "scope": "only new controlled comparison offline engineering tests",
                  "source_before": before, "source_after": after, "source_unchanged": before == after,
                  "gold_before": gold_before, "gold_after": gold_after, "gold_unchanged": gold_before == gold_after,
                  "log": stream.getvalue(), "identity": runtime.IDENTITY, "formal_comparison_result": False,
                  "provider_calls": 0, "new_execution_commit": None}
        json.dump(report, out, ensure_ascii=False, indent=2)
        out.write("\n")
        print(stream.getvalue())
        print(json.dumps({k: report[k] for k in ("tests", "failures", "errors", "skips", "actual_exit_code", "source_unchanged", "gold_unchanged")}))
        runtime.LOOP.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
