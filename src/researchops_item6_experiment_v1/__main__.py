"""No test/live override, credential argument, store path or transport argument."""
import argparse
import asyncio
import json
import re
from pathlib import Path

from . import contract as c


class Parser(argparse.ArgumentParser):
    def error(self, message): self.exit(2, '{"error":"item6_invalid_arguments"}\n')


def _observation_summary(value, *, admission=False):
    """Counts only, not a denominator/identity verifier or an evidence archive."""
    result = dict(state="unavailable" if value is None else "invalid", planned=32,
        available_rows=None, observed=None, not_executed=None, unknown_execution_state=None,
        observed_failed=None, record_body_recorded=False, identity_verified=False,
        complete_collection_claim=False)
    if admission:
        if (type(value) is not dict or type(value.get("planned")) is not int
                or value["planned"] != 32 or value.get("runtime_authority_granted") is not False
                or type(value.get("records")) is not list or len(value["records"]) > 32):
            return result
        rows = value["records"]
    else:
        if type(value) is not dict or set(value) != {"fixed_workflow", "agent"}:
            return result
        paths = (value["fixed_workflow"], value["agent"])
        if any(type(path) is not dict or len(path) > 16 for path in paths):
            return result
        rows = [row for path in paths for row in path.values()]
    if any(type(row) is not dict for row in rows):
        return result
    observed = not_executed = failed = unknown = 0
    for row in rows:
        state = row.get("execution_state")
        if type(state) is str and state == "observed":
            observed += 1
            if type(row.get("status")) is str and row["status"] == "failed": failed += 1
        elif type(state) is str and state == "not_executed":
            not_executed += 1
        else:
            unknown += 1
    result.update(state="available", available_rows=len(rows), observed=observed,
        not_executed=not_executed, unknown_execution_state=unknown, observed_failed=failed)
    return result


def failure_projection(error):
    """item6-cli-failure/2.0: bounded value-free failure output, exit remains 2."""
    attributes = vars(error)
    primary = attributes.get("primary_stop_reason")
    if type(primary) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,127}", primary) is None:
        primary = None
    claim = attributes.get("claim_may_exist")
    result = dict(schema_version="item6-cli-failure/2.0", error=c.safe_error(error),
        claim_may_exist=claim if type(claim) is bool else None, primary_stop_reason=primary,
        admission_observations=_observation_summary(attributes.get("admission_observations"), admission=True),
        partial_observations=_observation_summary(attributes.get("partial_observations")), online_authorized=False)
    c.require(len(c.raw(result)) <= 4096, "cli_failure_size")
    c.scan_public_artifact_bytes((c.raw(result),))
    return result


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
            c.case_rejection_policy()
            value = dict(schema_version="item6-unapproved-draft/1.0", source_commitment_sha256=manifest["commitment_sha256"],
                         case_rejection_policy_revision=c.REJECTION_REVISION,
                         case_rejection_policy_sha256=c.digest(c.read(c.REJECTION_POLICY_PATH, 8192)),
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
        try:
            output = failure_projection(error)
        except BaseException:
            # Even a broken projection/scanner must not expose the first error.
            output = dict(schema_version="item6-cli-failure/2.0",
                error="item6_cli_failure_projection_unavailable", claim_may_exist=None,
                primary_stop_reason=None, admission_observations=_observation_summary(None),
                partial_observations=_observation_summary(None), online_authorized=False)
        print(json.dumps(output))
        return 2


if __name__ == "__main__": raise SystemExit(main())
