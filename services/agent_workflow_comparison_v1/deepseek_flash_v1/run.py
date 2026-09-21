"""CLI for a new isolated full-plan mock run. No online switch exists."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from services.agent_workflow_comparison_v1.deepseek_flash_v1.verify import SafeParser, dump


def main():
    parser = SafeParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    from services.agent_workflow_comparison_v1.deepseek_flash_v1 import runtime as r
    directory = r.new_directory(args.run_id)
    result = r.base.LOOP.run_until_complete(r.execute_batch(args.run_id))
    dump(directory / "observations.json", result)
    sealed = r.base.seal(directory, ["observations.json"])
    r.base.check_seal(directory, sealed["manifest_sha256"])
    code = 0 if result["source_stable"] and result["budget"]["stop_reason"] is None and result["actual_scorer_connected"] else 1
    print("offline_report_generated; intended_exit_code=" + str(code))
    r.base.LOOP.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
