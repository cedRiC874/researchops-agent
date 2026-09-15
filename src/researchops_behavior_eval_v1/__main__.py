"""Explicit offline JSON IO wrapper. No online or existing runner entry point."""

import argparse
import json
import sys
from pathlib import Path

from .core import ContractError, _strict_json, score_plan


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline internal behavior evaluator v1")
    parser.add_argument("--input", required=True, type=Path, help="Explicit plan JSON")
    parser.add_argument("--output", required=True, type=Path, help="New report file (must not exist)")
    args = parser.parse_args(argv)
    try:
        plan = _strict_json(args.input.read_text(encoding="utf-8"))
        report = score_plan(plan)
        # Validate and serialize before creating output; exclusive create preserves history.
        payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        with args.output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
    except (ContractError, ValueError, OSError, RecursionError) as exc:
        print(json.dumps({"error": getattr(exc, "code", "offline_io_or_json_error"),
                          "location": getattr(exc, "path", None), "message": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    # 0 means a report was produced, NOT that every observed task passed.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
