from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import argparse
import json
from services.agent_workflow_comparison_v1.controlled_comparison_v1 import runtime
from services.agent_workflow_comparison_v1.controlled_comparison_v1.budget import StopRun


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, '{"error":"invalid_arguments"}\n')


def main(argv=None):
    parser = SafeParser(description="Offline engineering only; no real Provider path")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        result, path, sealed = runtime.run_to_new_directory(args.run_id)
        code = 0 if result["budget"]["stop_reason"] is None and all(x["error"] is None for x in result["scores"].values()) else 2
        print(json.dumps({"output": str(path), "stop_reason": result["budget"]["stop_reason"],
                          "seal_sha256": sealed["manifest_sha256"],
                          "formal_comparison_result": False, "actual_exit_code": code}, ensure_ascii=False))
        return code
    except (ValueError, OSError, StopRun) as exc:
        print(json.dumps({"error": getattr(exc, "code", type(exc).__name__)}, ensure_ascii=False))
        return 2
    finally:
        runtime.LOOP.close()


if __name__ == "__main__":
    raise SystemExit(main())
