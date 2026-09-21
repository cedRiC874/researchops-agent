"""One bounded joint offline run; no provider credentials or live mode."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import argparse
import io
import json
import re
import unittest
from services.agent_workflow_comparison_v1.controlled_publication_v1 import binding as b
from services.agent_workflow_comparison_v1.controlled_publication_v1.public_artifacts import write_json, derive, project


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, '{"error":"invalid_arguments"}\n')


def main():
    parser = Parser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--require-committed", action="store_true")
    args = parser.parse_args()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", args.run_id) is None:
        raise ValueError("isolated_run_id_required")
    parent = b.ROOT / "outputs"
    parent.mkdir(exist_ok=True)
    directory = parent / args.run_id
    directory.mkdir(exist_ok=False)
    public = directory / "public"
    public.mkdir()
    try:
        identity = b.verify_identity(require_committed=args.require_committed)
        before = b.verify_files()
        from services.agent_workflow_comparison_v1.controlled_comparison_v1 import runtime, test_controlled
        from services.agent_workflow_comparison_v1.deepseek_flash_v1 import test_native
        from services.agent_workflow_comparison_v1.controlled_publication_v1 import test_publication
        stream = io.StringIO()
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(module)
                                   for module in (test_controlled, test_native, test_publication))
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        after = b.verify_files()
        stable = before == after
        code = 0 if result.wasSuccessful() and stable else 1
        report = {"kind": "item6_offline_publication_joint", "identity": identity,
                  "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                  "skips": len(result.skipped), "intended_exit_code": code,
                  "source_before": before, "source_after": after, "source_unchanged": stable,
                  "historical_origin_check": "not_performed_in_this_context", "carried_source_files_verified": True,
                  "formal_comparison_result": False, "provider_calls": 0, "log": stream.getvalue()}
        write_json(directory / "validation.json", report)
        write_json(directory / "observations.json", test_native.NORMAL)
        write_json(directory / "fault-validation.json", test_native.FAULT_REPORTS)
        mappings = [derive(directory / name, public / name) for name in
                    ("validation.json", "observations.json", "fault-validation.json")]
        write_json(public / "derivation-map.json", {"artifacts": mappings, "formal_comparison_result": False})
        sealed = runtime.seal(directory, ["validation.json", "observations.json", "fault-validation.json"])
        runtime.check_seal(directory, sealed["manifest_sha256"])
        runtime.LOOP.close()
        print(project(stream.getvalue()))
        print(json.dumps({k: report[k] for k in ("tests", "failures", "errors", "skips", "intended_exit_code", "source_unchanged")}))
        return code
    except Exception as exc:
        write_json(public / "entry-failure.json", {"kind": "offline_entry_failure", "exception_type": type(exc).__name__,
            "safe_message": project(str(exc)) if type(exc) is ValueError else "entry_rejected",
            "intended_exit_code": 2, "formal_comparison_result": False, "online_authorized": False})
        print('{"error":"offline_entry_failure"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
