"""No test/live override, credential argument, store path or transport argument."""
import argparse
import asyncio
import json
from pathlib import Path

from . import contract as c


class Parser(argparse.ArgumentParser):
    def error(self, message): self.exit(2, '{"error":"item6_invalid_arguments"}\n')


def main():
    parser = Parser()
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--output", required=True)
    run = sub.add_parser("run")
    run.add_argument("--freeze", required=True); run.add_argument("--approval", required=True)
    run.add_argument("--approved-digest", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--archive", required=True); verify.add_argument("--seal-sha256", required=True)
    verify.add_argument("--finalization-sha256", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            # A draft cannot be submitted to run: all real approval/pricing/environment fields remain absent.
            manifest = c.source.verify_source(c.ROOT)
            value = dict(schema_version="item6-unapproved-draft/1.0", source_commitment_sha256=manifest["commitment_sha256"],
                         policy=c.policy(), execution_commit=None, pricing=None, environment_id=None, approval=None,
                         online_authorized=False, scorer_commit=c.SCORER)
            target = Path(args.output).resolve()
            allowed = (c.ROOT / c.OUTPUT).resolve()
            c.require(target.parent == allowed and target.suffix == ".json", "draft_output_scope")
            for part in (c.ROOT / c.OUTPUT, c.ROOT / "output"):
                if part.exists(): c.require(part.is_dir() and not part.is_symlink() and not part.is_junction(), "draft_output_scope")
            allowed.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream: stream.write(c.raw(value))
            print(json.dumps({"draft_created": True, "online_authorized": False})); return 0
        if args.command == "verify":
            from .observations import verify_archive
            result = verify_archive(Path(args.archive), expected_seal_sha256=args.seal_sha256,
                                    expected_finalization_sha256=args.finalization_sha256)
            print(json.dumps(result)); return 0
        from .runner import run_live
        result = asyncio.run(run_live(freeze_bytes=c.read_regular_file_no_follow(Path(args.freeze), max_bytes=1048576),
            approval_bytes=c.read_regular_file_no_follow(Path(args.approval), max_bytes=16384), approved_digest=args.approved_digest))
        print(json.dumps(result)); return result["actual_exit_code"]
    except BaseException as error:
        print(json.dumps({"error": c.safe_error(error), "claim_may_exist": bool(getattr(error, "claim_may_exist", False)),
                          "primary_stop_reason": getattr(error, "primary_stop_reason", None),
                          "admission_observations": getattr(error, "admission_observations", None),
                          "partial_observations": getattr(error, "partial_observations", None), "online_authorized": False}))
        return 2


if __name__ == "__main__": raise SystemExit(main())
