"""Business observations and independent artifact replay, separate from telemetry metadata."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import time

from researchops.audit import verify_audit_chain_rows
from researchops_completion_telemetry.surface_mapping import load_and_select_surface_mapping
from researchops_completion_telemetry.persisted_evidence import (
    create_persisted_live_evidence_validation_context, validate_persisted_live_runtime_denominator_artifact)
from researchops_completion_timing.ledger_event import validate_segment_link
from researchops_external_closure.artifact_inputs import _read_database_rows
from services.agent_workflow_comparison_v1.controlled_comparison_v1.paths import public_task, refusal, needs_design, INSTRUCTION
from . import contract as c
from . import case_isolation as isolation
from . import privacy_diagnostics as privacy


def safe_business(value, *, key=None, diagnostic=None):
    data = c.raw(value)
    c.require(len(data) <= c.policy()["business_field_bytes"], "business_size")
    from researchops_external_closure.errors import ExternalClosurePrimitiveError
    try:
        c.scan_public_artifact_bytes((data,), sensitive_canaries=(() if key is None else (key.encode(),)))
    except ExternalClosurePrimitiveError as error:
        if type(error) is ExternalClosurePrimitiveError and error.code == "external_closure_sensitive_content_detected":
            privacy.notify(diagnostic, "P00", error)
        raise
    text = data.decode()
    rule = privacy.business_rule(text)
    if rule is not None:
        error = c.ExperimentError("business_privacy")
        privacy.notify(diagnostic, rule, error)
        raise error


def safe_directory(path):
    path = Path(path).absolute()
    c.require(".." not in path.parts, "archive_path")
    for part in (path, *path.parents):
        c.require(part.is_dir() and not part.is_symlink() and not part.is_junction(), "archive_path")
    return path


class Case:
    def __init__(self, factory, task, path):
        self.factory, self.task, self.path = factory, public_task(task), path
        self.started = time.monotonic()
        self.catalog_observed = False
        self.text = None; self.status = "unknown"; self.completion = "unknown"
        self.replay = [{"role": "user", "content": json.dumps(self.task, ensure_ascii=False)}]
        self.sdk_raw_count = self.sdk_requests = 0
        self.sdk_usage_indices = {}
        self.sdk_observation_complete = True
        self.lock = asyncio.Lock()
        self.record = dict(task_id=task["task_id"], path_kind=path,
            run_id=f"{factory.owner.run_id}-{path}-{task['task_id']}", execution_state="not_executed",
            mode=factory.owner.mode, source_kind="internal/developer-known", task_input=deepcopy(self.task),
            model_response_origin="synthetic_fixture/scripted_model_response" if path == "agent" and factory.owner.mode == "offline_test" else "not_applicable" if path == "fixed_workflow" else "provider_response",
            input_sha256=c.digest(c.raw({"task": self.task, "instruction": INSTRUCTION})),
            plan=[], events=[], events_complete=True, allowed_evidence=[], known_failures=[],
            approval_interruptions=[], side_effects=[], side_effects_complete=True,
            side_effect_scope="two frozen read-only tools", delivered_artifacts=[],
            human_help={"during_run": [], "observations_complete": True}, native_call_ids=[],
            formal_experiment_result=False,
            case_lifecycle=dict(started_event_hash=None, closed_event_hash=None), case_rejection=None,
            privacy_diagnostic=None)

    def check_deadline(self):
        c.require(time.monotonic() - self.started < self.factory.freeze["budget"]["task_seconds"], "task_deadline")

    def plan(self, tool, arguments, source):
        self.record["plan"].append(dict(tool=tool, arguments=deepcopy(arguments), source=source,
                                        execution="not_executed", call_id=None))

    def _read_tool(self, tool, arguments):
        data = c.decode(c.read(c.EVIDENCE))
        catalog = data["catalog"].get(self.task["scope_id"], [])
        if tool == "inspect_sources":
            c.require(arguments == {"scope_id": self.task["scope_id"]}, "tool_scope")
            return {"sources": deepcopy(catalog), "facts": []}
        c.require(tool == "read_aggregate" and set(arguments) == {"bundle_id"}
                  and arguments["bundle_id"] in {x["bundle_id"] for x in catalog}, "tool_scope")
        c.require(self.task["bundle_id"] is not None or self.catalog_observed, "catalog_precondition")
        return deepcopy(data["bundles"][arguments["bundle_id"]])

    async def call(self, tool, arguments):
        async with self.lock:
            self.factory.owner.check(source_check=True); self.check_deadline()
            self.factory.owner.assert_case(self.factory, self)
            c.require(self.factory.current is self and self.record["execution_state"] == "observed", "tool_case")
            isolation.work_allowed(self)
            if refusal(self.task) or needs_design(self.task):
                error = c.ExperimentError("tool_before_design_or_refusal")
                isolation.register_rejection(self, error, tool, arguments)
                raise error
            self.factory.budget.tool(self.task["task_id"] + ":" + self.path)
            event = dict(call_id=f"C{len(self.record['events'])+1}", tool=tool, arguments=deepcopy(arguments),
                         status="failed", produced_artifacts=[], result=None)
            self.record["events"].append(event)
            previous_complete = self.record["events_complete"]
            self.record["events_complete"] = False
            base = dict(mode=self.factory.owner.mode, case_run_id=self.record["run_id"], call_id=event["call_id"], tool=tool)
            try:
                self.factory.ledger.append_event(self.factory.owner.run_id, "item6_tool_started_v1", {**base, "arguments_sha256": c.digest(c.raw(arguments))})
            except BaseException:
                code = "item6_tool_start_audit_failed"
                self.record["known_failures"].append({"code": code, "call_id": event["call_id"]})
                self.factory.owner.stop(code)
                raise c.ExperimentError("tool_start_audit_failed") from None
            for proposed in self.record["plan"]:
                if proposed["execution"] == "not_executed" and proposed["tool"] == tool and proposed["arguments"] == arguments:
                    proposed.update(execution="executed", call_id=event["call_id"]); break
            pending_evidence = None
            try:
                result = await asyncio.wait_for(asyncio.to_thread(self._read_tool, tool, arguments), self.factory.freeze["budget"]["tool_seconds"])
                evidence_id = f"E{len(self.record['allowed_evidence'])+1}"
                result["evidence_id"] = evidence_id
                safe_business(result, key=self.factory.canary)
                pending_evidence = dict(artifact_id=evidence_id, run_id=self.record["run_id"],
                    call_id=event["call_id"], facts=deepcopy(result["facts"]), content_sha256=c.digest(c.raw(result)))
                event.update(status="succeeded", result=deepcopy(result), produced_artifacts=[evidence_id])
            except TimeoutError:
                code = "item6_tool_timeout"
                event["result"] = {"error": code}
                self.completion = "timeout"
                self.record["known_failures"].append({"code": code, "call_id": event["call_id"]})
                self.factory.owner.stop(code)
                raise
            except (OSError, KeyError):
                result = {"error": "read_failed"}; event["result"] = result
                self.record["known_failures"].append({"code": "tool_failed", "call_id": event["call_id"], "attribution": "source_or_tool_not_model_verdict"})
            except BaseException as error:
                code = c.safe_error(error)
                event["result"] = {"error": code}
                self.record["known_failures"].append({"code": code, "call_id": event["call_id"]})
                self.factory.owner.stop(code)
                raise
            try:
                self.factory.ledger.append_event(self.factory.owner.run_id, "item6_tool_finished_v1",
                    {**base, "status": event["status"], "result_sha256": c.digest(c.raw(result)), "produced_artifacts": event["produced_artifacts"]})
            except BaseException:
                code = "item6_tool_finish_audit_failed"
                event.update(status="failed", produced_artifacts=[])
                self.record["known_failures"].append({"code": code, "call_id": event["call_id"]})
                self.factory.owner.stop(code)
                raise c.ExperimentError("tool_finish_audit_failed") from None
            if pending_evidence is not None:
                self.record["allowed_evidence"].append(pending_evidence)
                if tool == "inspect_sources": self.catalog_observed = True
            self.record["events_complete"] = previous_complete
            return deepcopy(result)

    def finish(self, text=None, status=None, completion=None):
        if status is not None: self.text, self.status = text, status
        if completion is not None: self.completion = completion
        safe_business(self.text, key=self.factory.canary)
        self.record.update(final_output=self.text, completion=self.completion,
            status="failed" if self.record["known_failures"] or self.factory.owner.stopped else self.status,
            text_observation="unobserved" if self.text is None else "empty" if self.text == "" else "whitespace" if self.text.isspace() else "nonempty",
            elapsed_seconds=time.monotonic()-self.started, clock_source="local_monotonic",
            model_request_count=sum(r["dispatches"] for r in self.factory.budget.requests if r["case_id"] == self.factory.freeze["model_case_handles"][self.task["task_id"]]) if self.path == "agent" else 0,
            usage_origin="synthetic_fixture" if self.factory.owner.mode == "offline_test" and self.path == "agent" else "not_applicable" if self.path == "fixed_workflow" else "provider_observed",
            actual_api_cost=None, human_review_seconds=None)
        return deepcopy(self.record)


def score_business(freeze, records):
    import researchops_behavior_eval_v1 as scorer
    c.require(Path(scorer.__file__).resolve() == c.ROOT / "src/researchops_behavior_eval_v1/__init__.py", "scorer_import")
    c.require(c.digest(c.read(c.GOLD)) == freeze["scoring_contract_sha256"], "scoring_contract_drift")
    contracts = c.decode(c.read(c.GOLD))["contracts"]
    result = {}
    for path in ("fixed_workflow", "agent"):
        rows = []
        for task_id in freeze["model_case_ids"]:
            r = records[path][task_id]
            obs = {k: deepcopy(r[k]) for k in ("task_id", "run_id", "execution_state", "final_output", "completion", "events_complete", "approval_interruptions", "side_effects", "side_effects_complete", "delivered_artifacts")}
            obs["events"] = [{k: deepcopy(e[k]) for k in ("call_id", "tool", "arguments", "status", "produced_artifacts")} for e in r["events"]]
            evidence = [{k: deepcopy(e[k]) for k in ("artifact_id", "run_id", "call_id", "facts")} for e in r["allowed_evidence"]]
            rows.append(dict(contract=contracts[task_id], observation=obs, allowed_evidence=evidence))
        plan = dict(schema_version="internal-behavior-eval-v1.0", source_kind="internal/developer-known", tasks=rows)
        try: result[path] = dict(input=plan, raw_report=scorer.score_plan(plan), error=None)
        except scorer.ContractError as error:
            result[path] = dict(input=plan, raw_report=None, error=dict(code=error.code, path=error.path))
    return result


def _check_archive_entries(directory, expected):
    # Path.iterdir may materialize every name first. Scandir stops at the first
    # unexpected/duplicate entry and always closes, including rejection paths.
    seen = set()
    with os.scandir(directory) as entries:
        for entry in entries:
            c.require(entry.name in expected and entry.name not in seen, "archive_unexpected_files")
            seen.add(entry.name)
    c.require(seen == expected, "archive_unexpected_files")


def write_archive(directory, document):
    c.schema(document, "artifact")
    blob = c.raw(document)
    c.require(len(blob) <= c.policy()["archive_bytes"], "archive_size")
    c.scan_public_artifact_bytes((blob,))
    with (directory / "experiment.json").open("xb") as stream: stream.write(blob)
    db = c.read_regular_file_no_follow(directory / "audit.sqlite3", max_bytes=c.policy()["archive_bytes"])
    c.require(len(blob) + len(db) <= c.policy()["archive_bytes"], "archive_size")
    c.scan_public_artifact_bytes((db,))
    _check_archive_entries(directory, {"experiment.json", "audit.sqlite3"})
    receipt = dict(schema_version=c.ARCHIVE_VERSION, files={"experiment.json": dict(bytes=len(blob), sha256=c.digest(blob)),
                   "audit.sqlite3": dict(bytes=len(db), sha256=c.digest(db))},
                   mode=document["mode"], run_id=document["run_id"])
    sealed = c.raw(receipt)
    with (directory / "sealed.json").open("xb") as stream: stream.write(sealed)
    return c.digest(sealed)


def verify_archive(directory, *, expected_seal_sha256, expected_finalization_sha256):
    directory = safe_directory(directory)
    c.require(not (directory / "finalization-failure.json").exists(), "finalization_failed")
    receipt_bytes = c.read_regular_file_no_follow(directory / "sealed.json", max_bytes=8192)
    c.require(c.digest(receipt_bytes) == expected_seal_sha256, "archive_seal")
    receipt = c.decode(receipt_bytes, 8192)
    c.exact(receipt, "schema_version files mode run_id")
    c.require(receipt["schema_version"] == c.ARCHIVE_VERSION and set(receipt["files"]) == {"experiment.json", "audit.sqlite3"}, "archive_files")
    _check_archive_entries(directory, {"experiment.json", "audit.sqlite3", "sealed.json", "finalization.json"})
    data = c.read_regular_file_no_follow(directory / "experiment.json", max_bytes=c.policy()["archive_bytes"])
    c.require(receipt["files"]["experiment.json"] == {"bytes": len(data), "sha256": c.digest(data)}, "archive_hash")
    doc = c.schema(c.decode(data, c.policy()["archive_bytes"]), "artifact")
    db = c.read_regular_file_no_follow(directory / "audit.sqlite3", max_bytes=c.policy()["archive_bytes"])
    c.require(receipt["files"]["audit.sqlite3"] == {"bytes": len(db), "sha256": c.digest(db)}
              and len(data) + len(db) <= c.policy()["archive_bytes"], "database_hash")
    c.require(doc["run_id"] == receipt["run_id"] and doc["mode"] == receipt["mode"], "archive_identity")
    freeze, events = _verify_document(directory, doc, db)
    candidate = doc["authority"]["approval"]["candidate"]
    final_bytes = c.read_regular_file_no_follow(directory / "finalization.json", max_bytes=8192)
    c.require(c.digest(final_bytes) == expected_finalization_sha256, "finalization_hash")
    final = c.decode(final_bytes, 8192)
    c.exact(final, "schema_version run_id mode status seal_sha256 started_at_utc sealed_at_utc elapsed_ns remaining_at_start_ns clock_source phase_scope")
    c.require(final["schema_version"] == "item6-finalization/1.0" and final["run_id"] == doc["run_id"]
              and final["mode"] == doc["mode"] and final["status"] == doc["status"]
              and final["seal_sha256"] == expected_seal_sha256, "finalization_binding")
    c.require(c.utc(candidate["not_before_utc"]) <= c.utc(final["started_at_utc"]) <= c.utc(final["sealed_at_utc"])
              < c.utc(candidate["expires_at_utc"])
              and all(c.utc(e["occurred_at_utc"]) <= c.utc(final["sealed_at_utc"]) for e in events), "finalization_window")
    c.require(type(final["elapsed_ns"]) is int and type(final["remaining_at_start_ns"]) is int
              and 0 <= final["elapsed_ns"] < final["remaining_at_start_ns"] <= freeze["budget"]["batch_seconds"] * 10**9
              and final["clock_source"] == "local_monotonic_finalization"
              and final["phase_scope"] == "request_and_cleanup_before_archive", "finalization_timing")
    return dict(archive_verified=True, execution_completed=doc["status"] == "completed", mode=doc["mode"],
                business_denominator=32, provider_calls=0 if doc["mode"] == "offline_test" else doc["dispatches"],
                runtime_authority_granted=False, formal_comparison_result=False,
                collection_status=doc["collection_status"], collection_summary=doc["collection_summary"],
                all_business_passed=doc["all_business_passed"],
                unacknowledged_case_events=isolation.unacknowledged_events(doc),
                unacknowledged_privacy_events=privacy.unacknowledged_events(doc))


def _verify_document(directory, doc, db):
    """Same source, claim, audit, denominator, budget, evidence and FCC checks for both archive kinds."""
    freeze = c.validate_freeze(doc["authority"]["freeze"])
    # Verify the producer's actual immutable Git objects, not its current working tree or self-reported hashes.
    commit = freeze["execution_commit"]
    c.require(type(commit) is str, "archive_execution_commit")
    c.source.v2._git(c.ROOT, "merge-base", "--is-ancestor", c.BASE, commit)
    c.require(c.source.v2._git(c.ROOT, "rev-parse", commit + "^{tree}").decode().strip() == freeze["execution_tree"], "archive_execution_tree")
    manifest_bytes = c.source.committed_blobs(c.ROOT, commit, [c.source.MANIFEST])[0]
    c.require(c.digest(manifest_bytes) == freeze["source_manifest_sha256"], "archive_source_manifest")
    source_manifest = c.source.decode(manifest_bytes)
    c.source.validate_schema(c.ROOT, source_manifest)
    body = {k: v for k, v in source_manifest.items() if k != "commitment_sha256"}
    c.require(c.source.digest(c.source.DOMAIN + c.source.canonical(body)) == source_manifest["commitment_sha256"] == freeze["source_commitment_sha256"], "archive_source_commitment")
    rows = source_manifest["files"]
    for row, blob in zip(rows, c.source.committed_blobs(c.ROOT, commit, [r["path"] for r in rows])):
        c.require(len(blob) == row["bytes"] and c.digest(blob) == row["sha256"], "archive_source_blob")
    for name, blob in zip(c.source.SEPARATE_GIT_BINDINGS, c.source.committed_blobs(c.ROOT, commit, c.source.SEPARATE_GIT_BINDINGS)):
        c.require(c.digest(blob) == freeze["workflow_sha256"][name], "archive_workflow_binding")
    c.validate_approval(doc["authority"]["approval"], freeze, doc["authority"]["approved_digest"], at=c.utc(doc["authority"]["claim_receipt"]["claimed_at_utc"]))
    c.require(doc["mode"] == freeze["mode"], "archive_mode")
    c.require(doc["authority"]["run_id"] == doc["run_id"] and doc["authority"]["mode"] == doc["mode"], "authority_identity")
    claim, request = doc["authority"]["claim_receipt"], doc["authority"]["claim_request"]
    from researchops_completion_timing import local_claim
    c.require(claim["claim_contract_sha256"] == local_claim.CONTRACT_SHA256 and claim["provider_actions_authorized"] is False
              and all(claim[k] == v for k, v in request.items() if k != "schema_version"), "claim_binding")
    approval = doc["authority"]["approval"]; candidate = approval["candidate"]
    c.exact(request, " ".join(local_claim._profile()["request_fields"]))
    c.exact(claim, " ".join(local_claim._profile()["request_fields"]) + " status claimed_at_utc claim_contract_sha256 store_scope provider_actions_authorized")
    intent = dict(scope=c.SCOPE, mode=freeze["mode"], freeze_sha256=c.commit("freeze", freeze),
                  approval_sha256=c.digest(c.raw(approval)), clock_domain_id=doc["authority"]["clock_domain_id"])
    c.require(request["authorization_id_sha256"] == c.commit("authorization-id", {"mode": freeze["mode"], "id": candidate["authorization_id"]})
              and request["execution_commit"] == freeze["execution_commit"]
              and request["timing_plan_commitment_sha256"] == c.commit("timing-plan", freeze)
              and request["execution_environment_id"] == freeze["environment_id"]
              and request["authorization_grant_sha256"] == c.digest(c.raw(approval))
              and request["consumption_entry_sha256"] == c.commit("claim-intent", intent)
              and request["clock_domain_id"] == doc["authority"]["clock_domain_id"]
              and claim["status"] == "reserved" and claim["store_scope"] == "single_host_fixed_store", "claim_scope")
    c.require(doc["run_id"] == "ITEM6-" + request["authorization_id_sha256"][:32].upper(), "run_binding")
    events = doc["audit"]["events"]
    rows = [{**{k: v for k, v in e.items() if k != "safe_payload"}, "safe_payload_json": c.raw(e["safe_payload"]).decode()} for e in events]
    c.require(verify_audit_chain_rows(doc["run_id"], rows).valid, "audit_chain")
    c.require(all(c.utc(candidate["not_before_utc"]) <= c.utc(e["occurred_at_utc"]) < c.utc(candidate["expires_at_utc"]) for e in events), "event_approval_window")
    runs, db_events, model_rows, *tool_counts = _read_database_rows(directory / "audit.sqlite3", expected_payload=db)
    c.require(list(db_events) == rows and len(runs) == 1 and runs[0]["run_id"] == doc["run_id"]
              and runs[0]["mode"] == c.SCOPE + "." + doc["mode"] and runs[0]["status"] == doc["status"]
              and not model_rows and tool_counts == [0, 0, 0], "database_projection")
    selected = load_and_select_surface_mapping(c.ROOT, "deepseek", "responses", "openai_compatible_responses", purpose="offline_validation")
    context = create_persisted_live_evidence_validation_context(selected, expected_binding=doc["binding"],
        expected_denominator_plan=doc["denominator_plan"], expected_denominator_plan_commitment_sha256=c.digest(c.raw(doc["denominator_plan"])))
    if doc["denominator"] is not None:
        validate_persisted_live_runtime_denominator_artifact(doc["denominator"], context=context)
    if doc["status"] == "completed":
        c.require(doc["authority"]["stopped"] is None and not doc["cleanup_errors"]
                  and doc["phase"]["closed"] is True and doc["phase"]["halted"] is False
                  and doc["phase"]["clock_failed"] is False and doc["phase"]["active_attempt"] is None
                  and doc["denominator"] is not None and not doc["denominator"]["not_finalized_case_ids"]
                  and all(row["closure_eligible"] for row in doc["denominator"]["cases"]), "incomplete_execution_claimed_complete")
        c.require(all(row["execution_state"] == "observed" for observations in doc["business"].values() for row in observations.values()), "unexecuted_claimed_complete")
    c.require(doc["denominator_plan"]["case_ids"] == [freeze["model_case_handles"][tid] for tid in freeze["model_case_ids"]], "model_case_plan")
    starts = [e for e in events if e["event_type"] == "model_request_started"]
    terminals = [e for e in events if "terminal_kind" in e["safe_payload"]]
    sends = [e for e in events if e["event_type"] == "item6_send_intent_v1"]
    c.require(len(starts) == len(doc["budget"]["requests"]) and len(terminals) == len(doc["segments"]), "request_event_denominator")
    from .budget import Budget
    replay_budget = Budget(freeze)
    timing_binding = dict(plan_commitment_sha256=c.commit("timing-plan", freeze), execution_binding_sha256=c.commit("freeze", freeze), clock_domain_id=doc["authority"]["clock_domain_id"])
    segment_by_index = {s["attempt_index"]: s for s in doc["segments"]}
    last_end = doc["phase"]["phase"]["key_loaded_ns"]
    for index, start in enumerate(starts):
        payload = start["safe_payload"]
        c.require(payload["attempt_index"] == index and payload["binding"] == doc["binding"], "attempt_start_binding")
        replay_budget.reserve(payload["case_id"])
        send = [e for e in sends if e["safe_payload"]["attempt_index"] == index]
        terminal = [e for e in terminals if e["safe_payload"]["attempt_index"] == index]
        c.require(len(send) <= 1 and len(terminal) <= 1, "attempt_duplicates")
        if send:
            p = send[0]["safe_payload"]
            c.require(freeze["model_case_handles"][p["task_id"]] == p["model_case_id"] == payload["case_id"] and p["mode"] == doc["mode"]
                      and p["source_sha256"] == freeze["source_commitment_sha256"]
                      and p["policy_sha256"] == c.digest(c.raw(freeze["policy"])), "send_intent_binding")
            replay_budget.wire(p["request_bytes"])
        expected_row = doc["budget"]["requests"][index]
        if expected_row["dispatches"]:
            c.require(bool(send), "dispatch_without_intent"); replay_budget.dispatch()
        if terminal:
            p = terminal[0]["safe_payload"]; segment = segment_by_index[index]
            c.require((segment["send_started_ns"] is not None) == bool(expected_row["dispatches"]), "dispatch_timing_mismatch")
            c.require(p["case_id"] == payload["case_id"] and start["sequence"] < terminal[0]["sequence"], "terminal_binding")
            validate_segment_link(p, event_type=terminal[0]["event_type"], segment_bytes=c.raw(segment), expected_timing_binding=timing_binding)
            c.require(segment["attempt_started_ns"] >= last_end, "timing_order")
            end = segment["lifetime_ended_ns"]
            if end is not None:
                c.require(end >= segment["attempt_started_ns"], "timing_order"); last_end = end
            offsets = [segment[name] for name in ("attempt_started_ns", "send_started_ns", "transport_terminal_ns", "raw_cleanup_terminal_ns", "lifetime_ended_ns")]
            provided = [value for value in offsets if value is not None]
            c.require(all(type(v) is int and v >= 0 for v in provided) and provided == sorted(provided), "timing_order")
            if doc["status"] == "completed":
                c.require(all(value is not None for value in offsets) and segment["raw_cleanup_status"] == "completed"
                          and offsets[2] - offsets[1] <= freeze["budget"]["request_seconds"] * 10**9,
                          "completed_timing_invalid")
            if "completion_record" in p:
                try: replay_budget.settle(p["completion_record"])
                except c.ExperimentError:
                    c.require(doc["status"] == "failed", "unreported_budget_failure")
            else: replay_budget.unknown(p["terminal_kind"])
        else:
            c.require(doc["status"] == "failed", "missing_terminal")
            replay_budget.unknown(expected_row["outcome"])
    c.require(replay_budget.requests == doc["budget"]["requests"] and sum(r["dispatches"] for r in replay_budget.requests) == doc["dispatches"], "budget_request_projection")
    expected_tasks = {t["task_id"]: t for t in c.decode(c.read(c.TASKS))["tasks"]}
    for path, observations in doc["business"].items():
        c.require(path in {"agent", "fixed_workflow"} and set(observations) == set(freeze["model_case_ids"]), "business_denominator")
        for record in observations.values():
            c.require(record["task_input"] == expected_tasks[record["task_id"]] and record["mode"] == doc["mode"], "task_projection")
            safe_business(record["final_output"])
            for event in record["events"]:
                replay_budget.tool(record["task_id"] + ":" + path)
                started = [e for e in events if e["event_type"] == "item6_tool_started_v1" and e["safe_payload"]["case_run_id"] == record["run_id"] and e["safe_payload"]["call_id"] == event["call_id"]]
                finished = [e for e in events if e["event_type"] == "item6_tool_finished_v1" and e["safe_payload"]["case_run_id"] == record["run_id"] and e["safe_payload"]["call_id"] == event["call_id"]]
                c.require(len(started) == len(finished) == 1 and started[0]["sequence"] < finished[0]["sequence"]
                    and started[0]["safe_payload"]["tool"] == finished[0]["safe_payload"]["tool"] == event["tool"]
                    and started[0]["safe_payload"]["arguments_sha256"] == c.digest(c.raw(event["arguments"]))
                    and finished[0]["safe_payload"]["status"] == event["status"]
                    and finished[0]["safe_payload"]["produced_artifacts"] == event["produced_artifacts"]
                    and finished[0]["safe_payload"]["result_sha256"] == c.digest(c.raw(event["result"])), "tool_event_binding")
            for evidence in record["allowed_evidence"]:
                matching = [e for e in record["events"] if e["call_id"] == evidence["call_id"] and e["status"] == "succeeded" and evidence["artifact_id"] in e["produced_artifacts"]]
                c.require(evidence["run_id"] == record["run_id"] and len(matching) == 1 and matching[0]["result"]["facts"] == evidence["facts"], "evidence_binding")
    c.require(replay_budget.snapshot() == doc["budget"], "budget_readback")
    c.require(doc["scores"] == score_business(freeze, doc["business"]), "score_readback")
    isolation.verify_document(doc, events)
    privacy.verify_document(doc, events)
    return freeze, events
