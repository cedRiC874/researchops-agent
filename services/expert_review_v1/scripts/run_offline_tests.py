"""Run unmodified unittest suites with external socket connections denied."""
from __future__ import annotations

import argparse
import json
import sys
import time
import unittest
from pathlib import Path


def _deny_external_sockets(event, args):
    if event in {"socket.connect", "socket.sendto", "socket.sendmsg"}:
        address = args[-1] if event == "socket.sendmsg" else args[1]
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError("Offline verification forbids external socket connections")
    if event == "socket.getaddrinfo" and args[0] not in {"127.0.0.1", "::1", "localhost", None}:
        raise RuntimeError("Offline verification forbids external DNS resolution")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scope", choices=("root", "service"))
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    sys.addaudithook(_deny_external_sockets)
    root = Path(__file__).resolve().parents[3]
    # Service integration tests reuse the repository's mock HTTP fixture.
    sys.path.insert(0, str(root))
    loader = unittest.TestLoader()
    if args.scope == "root":
        suite = loader.discover(str(root / "tests"), top_level_dir=str(root))
    else:
        suite = loader.discover(str(root / "services/expert_review_v1/tests"))
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "scope": args.scope, "tests_run": result.testsRun,
        "passed": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped) - len(result.expectedFailures) - len(result.unexpectedSuccesses),
        "failures": len(result.failures), "errors": len(result.errors),
        "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "successful": result.wasSuccessful(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "external_socket_connections_denied": True,
    }
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
