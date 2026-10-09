"""运行离线 unittest，并从实际 TestResult 输出计数与用例结果。"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path


class MeasuredResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed_count = 0
        self.cases = {}

    def mark(self, test, status):
        matched = re.search(r"test_case_(\d{2})_", test.id())
        if matched:
            self.cases["C" + matched.group(1)] = status

    def addSuccess(self, test):
        self.passed_count += 1
        self.mark(test, "passed")
        super().addSuccess(test)

    def addFailure(self, test, err):
        self.mark(test, "failed")
        super().addFailure(test, err)

    def addError(self, test, err):
        self.mark(test, "error")
        super().addError(test, err)

    def addSkip(self, test, reason):
        self.mark(test, "skipped")
        super().addSkip(test, reason)


def main():
    root = Path(__file__).resolve().parents[3]
    for location in (root, root / "src", root / "services/mcp_gateway_v1/src"):
        sys.path.insert(0, str(location))
    from services.mcp_gateway_v1.tests.offline import prohibit_network
    sys.addaudithook(prohibit_network)
    suite = unittest.defaultTestLoader.discover(
        str(root / "services/mcp_gateway_v1/tests"), top_level_dir=str(root)
    )
    result = unittest.TextTestRunner(verbosity=2, resultclass=MeasuredResult).run(suite)
    exit_code = 0 if result.wasSuccessful() else 1
    print(json.dumps({
        "schema_version": "mcp-gateway-tests/1.0",
        "tests_run": result.testsRun,
        "passed": result.passed_count,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "exit_code": exit_code,
        "cases": dict(sorted(result.cases.items())),
    }, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
