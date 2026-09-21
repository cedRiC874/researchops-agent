"""Run only native adapter tests and preserve every result in a new directory."""
import argparse
import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[2]))


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid_cli_arguments_not_echoed")


def dump(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as out:
        json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
        out.write("\n")


def main():
    parser = SafeParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    from services.agent_workflow_comparison_v1.deepseek_flash_v1 import runtime as r, test_native
    directory = r.new_directory(args.run_id)
    before = r.snapshot()
    gold_before = r.sha(r.OLD / "scoring/contracts.json")
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(test_native))
    after = r.snapshot()
    gold_after = r.sha(r.OLD / "scoring/contracts.json")
    preservation = r.preserved()
    stable = before == after and gold_before == gold_after and all(all(g.values()) for g in preservation.values())
    code = 0 if result.wasSuccessful() and stable else 1
    report = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
              "skips": len(result.skipped), "intended_exit_code": code, "source_before": before,
              "source_after": after, "source_unchanged": before == after,
              "gold_before": gold_before, "gold_after": gold_after, "gold_unchanged": gold_before == gold_after,
              "preservation": preservation, "identity": r.base.IDENTITY, "dependencies": r.DEPENDENCIES,
              "log": stream.getvalue(), "formal_comparison_result": False, "provider_calls": 0}
    dump(directory / "validation.json", report)
    dump(directory / "observations.json", test_native.NORMAL)
    dump(directory / "fault-validation.json", test_native.FAULT_REPORTS)
    seal = r.base.seal(directory, ["validation.json", "observations.json", "fault-validation.json"])
    r.base.check_seal(directory, seal["manifest_sha256"])
    print(stream.getvalue())
    print(json.dumps({k: report[k] for k in ("tests", "failures", "errors", "skips", "intended_exit_code", "source_unchanged", "gold_unchanged")}))
    print("sealed_manifest_sha256=" + seal["manifest_sha256"])
    r.base.LOOP.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
