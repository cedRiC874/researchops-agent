from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Sequence

from .kimi_k3_handshake import (
    KimiK3HandshakeError,
    run_kimi_k3_handshake,
    validate_kimi_k3_handshake,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Frozen Kimi K3 controlled synthetic handshake"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "validate",
        help="validate the frozen plan and contracts without loading a Key",
    )
    run_parser = subparsers.add_parser(
        "run",
        help="consume one new authorization and run the frozen handshake",
    )
    run_parser.add_argument("--confirm-online", action="store_true")
    run_parser.add_argument("--accept-locked-caps", action="store_true")
    run_parser.add_argument(
        "--attest-terms-and-pricing-unchanged", action="store_true"
    )
    run_parser.add_argument("--authorization-id")
    run_parser.add_argument("--authorization-expires-at-utc")
    run_parser.add_argument("--expected-plan-commitment")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(__file__).resolve().parents[2]
    try:
        if args.command == "validate":
            result = validate_kimi_k3_handshake(project_root)
            exit_code = 0
        else:
            result = asyncio.run(
                run_kimi_k3_handshake(
                    project_root=project_root,
                    authorization_id=args.authorization_id,
                    authorization_expires_at_utc=(
                        args.authorization_expires_at_utc
                    ),
                    expected_plan_commitment_sha256=(
                        args.expected_plan_commitment
                    ),
                    confirm_online=args.confirm_online,
                    accept_locked_caps=args.accept_locked_caps,
                    attest_terms_and_pricing_unchanged=(
                        args.attest_terms_and_pricing_unchanged
                    ),
                    _key_loader=lambda: os.environ.get("MOONSHOT_API_KEY"),
                )
            )
            exit_code = 0 if result["status"] == "success" else (
                4 if result["status"] == "not_run" else 5
            )
    except KimiK3HandshakeError as exc:
        result = {"status": "invalid", "error_code": exc.code}
        exit_code = 4
    except (OSError, ValueError, json.JSONDecodeError):
        result = {
            "status": "invalid",
            "error_code": "kimi_k3_handshake_local_contract_invalid",
        }
        exit_code = 4
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
