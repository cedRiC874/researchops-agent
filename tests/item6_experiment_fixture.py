"""Isolated real source/claim/Adapter/ledger fixture; only OS locator and HTTP are replaced."""
from pathlib import Path
from datetime import timedelta
import asyncio
import copy
import functools
from contextlib import ExitStack
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import io
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BASE = "5605396e165fc5140eba54340d5a9c93f67540dc"
PYTHON = sys.executable
_temporary = []
RESULTS = []
NEW_TESTS = (
    "tests.test_item6_experiment_authority", "tests.test_item6_experiment_session",
    "tests.test_item6_experiment_budget", "tests.test_item6_experiment_artifacts",
    "tests.test_item6_experiment_integration", "tests.test_internal_source_integrity_v3")
RELATED_TESTS = (
    "tests.test_phase6_providers", "tests.test_phase6_provider_completion_capture",
    "tests.test_provider_completion_surface_mapping", "tests.test_completion_telemetry_ledger",
    "tests.test_completion_timed_ledger", "tests.test_internal_source_integrity_v2",
    "tests.test_deepseek_completion_first_live_validation", "tests.test_kimi_k3_handshake",
    "tests.test_phase6_depth60")
OVERLAY = (
    "src/researchops/model_providers.py", "src/researchops_completion_telemetry/surface_mapping.py",
    "src/researchops_internal_telemetry/source_integrity_v3.py", "scripts/build_internal_source_integrity_v3.py",
    "scripts/verify_pre_v6_integrity.py", ".github/workflows/ci.yml", ".github/workflows/item6-experiment-bridge-offline.yml",
    "tests/internal_source_v2_historical_support.py", "tests/test_internal_source_integrity_v2.py",
    "tests/test_internal_source_integrity_v3.py", "tests/test_deepseek_completion_first_live_validation.py",
    "tests/test_kimi_k3_handshake.py", "tests/test_phase6_depth60.py", "tests/item6_experiment_fixture.py",
    "tests/test_item6_experiment_authority.py", "tests/test_item6_experiment_session.py", "tests/test_item6_experiment_budget.py",
    "tests/test_item6_experiment_artifacts.py", "tests/test_item6_experiment_integration.py")


def environment(root):
    env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT") if k in os.environ}
    env.update(PYTHONPATH=str(root / "src") + os.pathsep + str(root), PYTHONDONTWRITEBYTECODE="1",
               PYTHONUTF8="1", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    return env


def command(args, root):
    result = subprocess.run(args, cwd=root, env=environment(root), capture_output=True, text=True, encoding="utf-8", timeout=180)
    if result.returncode:
        raise RuntimeError("fixture_command_failed:" + str(args[:2]) + ":" + result.stderr[-2000:])
    return result.stdout


def allocate(prefix):
    # Retain fixture sources/stores after failures; never delete a claim to retry.
    path = Path(tempfile.mkdtemp(prefix=prefix))
    _temporary.append(path)
    return path


@functools.lru_cache(maxsize=1)
def seed():
    root = allocate("i6-seed-")
    command(["git", "clone", "--shared", "--no-checkout", str(ROOT), str(root)], ROOT)
    command(["git", "checkout", "--detach", BASE], root)
    names = list(OVERLAY)
    for folder in ("src/researchops_item6_experiment_v1", "evals/item6_experiment_bridge_v1", "evals/provider_completion_internal_source_v3"):
        names.extend(p.relative_to(ROOT).as_posix() for p in (ROOT / folder).rglob("*") if p.is_file()
                     and "__pycache__" not in p.parts and not p.name.startswith("source_manifest_v3.json"))
    for name in names:
        target = root / name; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    command([PYTHON, "-B", "-m", "tests.item6_experiment_fixture", "freeze-source"], root)
    command(["git", "add", "--", *names, "evals/provider_completion_internal_source_v3/source_manifest_v3.json"], root)
    command(["git", "-c", "user.name=Item6 Fixture", "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=" + str(root / "no-hooks"),
             "commit", "--no-gpg-sign", "-m", "Synthetic offline Item6 fixture; never published"], root)
    commit = command(["git", "rev-parse", "HEAD"], root).strip()
    bundles = ROOT / "output/item6-bridge-validation/fixture-bundles"
    bundles.mkdir(parents=True, exist_ok=True)
    command(["git", "bundle", "create", str(bundles / (commit + ".bundle")), "HEAD", "^" + BASE], root)
    return root


def run_case(mode="normal"):
    root = allocate("i6-case-")
    command(["git", "clone", "--shared", str(seed()), str(root)], ROOT)
    env = environment(root)
    result = subprocess.run([PYTHON, "-B", "-m", "tests.item6_experiment_fixture", "exercise", mode],
        cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=900)
    rows = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if not rows: raise AssertionError("fixture_missing_result:" + result.stderr[-1500:])
    value = json.loads(rows[-1]); value["process_exit_code"] = result.returncode
    value["fixture_root"] = str(root)
    value["stderr"] = result.stderr
    RESULTS.append(value)
    directory = ROOT / "output/item6-bridge-validation/probes"
    directory.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2)
    if "offline-fixture-key-item6" in encoded:
        encoded = json.dumps({"diagnostic_copy_redacted": True, "payload": json.loads(encoded.replace("offline-fixture-key-item6", "<synthetic-key-redacted>"))}, ensure_ascii=False, indent=2)
    (directory / (mode + "-" + uuid.uuid4().hex + ".json")).write_text(encoded, encoding="utf-8")
    return value


@functools.lru_cache(maxsize=3)
def unfinished_case(mode):
    assert mode in {"cross_case_handle", "missing_usage", "cross_case_handle_after_one"}
    return run_case(mode)


def verify_unfinished(result):
    """Independent process, real failed-evidence reader; never treats it as a normal seal."""
    root = Path(result["fixture_root"])
    run = result["run"]
    assert run["archive_kind"] == "unfinished_failure"
    program = ("import json,sys; from researchops_item6_experiment_v1.failed_archive import verify_failed_archive; "
               "print(json.dumps(verify_failed_archive(sys.argv[1], expected_failure_sha256=sys.argv[2])))")
    child = subprocess.run([PYTHON, "-B", "-c", program, str(root / run["archive_namespace"]), run["failure_receipt_sha256"]],
        cwd=root, env=environment(root), capture_output=True, text=True, encoding="utf-8", timeout=900)
    assert child.returncode == 0, "unfinished_readback_exit:" + str(child.returncode)
    value = json.loads(child.stdout)
    value["actual_exit_code"] = child.returncode
    RESULTS.append(dict(kind="unfinished_independent_readback", result=value))
    return value


@functools.lru_cache(maxsize=1)
def normal(): return run_case("normal")


def verify_result(result):
    root = Path(result["fixture_root"])
    run = result["run"]
    process = subprocess.run([PYTHON, "-B", "-m", "researchops_item6_experiment_v1", "verify",
        "--archive", run["archive_namespace"], "--seal-sha256", run["seal_sha256"], "--finalization-sha256", run["finalization_sha256"]],
        cwd=root, env=environment(root), capture_output=True, text=True, encoding="utf-8", timeout=120)
    value = json.loads(process.stdout.splitlines()[-1]); value["actual_exit_code"] = process.returncode
    RESULTS.append({"kind": "independent_readback", "result": value})
    return value


def concurrent_claims():
    root = allocate("i6-race-")
    command(["git", "clone", "--shared", str(seed()), str(root)], ROOT)
    known = allocate("item6-test-store-")
    config = root / "output/claim-race"
    config.mkdir(parents=True)
    command([PYTHON, "-B", "-m", "tests.item6_experiment_fixture", "prepare-claim-fixture", str(known), str(config)], root)
    processes = [subprocess.Popen([PYTHON, "-B", "-m", "tests.item6_experiment_fixture", "claim-only", str(known), str(config), str(index)],
        cwd=root, env=environment(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8") for index in range(2)]
    deadline = time.monotonic() + 180
    while not all((config / ("ready-" + str(index))).exists() for index in range(2)):
        if time.monotonic() > deadline: raise AssertionError("claim race readiness timeout")
        if any(process.poll() is not None for process in processes): raise AssertionError("claim race child exited before barrier")
        time.sleep(0.05)
    (config / "go").write_text("go", encoding="utf-8")
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=180)
        value = json.loads(stdout.splitlines()[-1]); value["actual_exit_code"] = process.returncode
        results.append(value)
    rows = list((known / "ResearchOpsAgent/completion-claims-v1/claims").glob("*.json"))
    result = dict(kind="concurrent_claims", contenders=results, claim_files=len(rows), real_store_touched=False, provider_calls=0)
    RESULTS.append(result)
    return result


def claim_fixture_child(command_name, known, directory, index=None):
    from researchops_completion_timing import local_claim
    from researchops_item6_experiment_v1 import authority, contract as c
    with patch.object(local_claim, "_windows_local_app_data", return_value=known):
        if command_name == "prepare-claim-fixture":
            status = local_claim.provision_local_claim_store()
            freeze = c.build_freeze(mode="offline_test", environment_id=status["execution_environment_id"], pricing=dict(
                kind="synthetic_fixture", input_per_million="1", output_per_million="2", cost_limit="10",
                input_accounting="synthetic_bytes", provider_bill_hard_cap=False, evidence_sha256=None))
            instant = c.now()
            candidate = dict(scope=c.SCOPE, mode="offline_test", authorization_id="race-fixture-"+uuid.uuid4().hex,
                freeze_sha256=c.commit("freeze", freeze), environment_id=status["execution_environment_id"],
                not_before_utc=(instant-timedelta(seconds=5)).isoformat().replace("+00:00","Z"),
                expires_at_utc=(instant+timedelta(hours=1)).isoformat().replace("+00:00","Z"))
            approved = c.commit("approval-candidate",candidate)
            approval = dict(schema_version="item6-experiment-approval/1.0", candidate=candidate,
                observation=dict(kind="synthetic_approval_fixture", approved_digest=approved,
                    observed_at_utc=instant.isoformat().replace("+00:00","Z"), message_reference="synthetic-race-only"))
            (directory/"inputs.json").write_bytes(c.raw(dict(freeze=freeze,approval=approval,approved_digest=approved)))
            return 0
        data = c.decode((directory/"inputs.json").read_bytes())
        prepared = authority.validate_experiment(freeze_bytes=c.raw(data["freeze"]), approval_bytes=c.raw(data["approval"]),
            approved_digest=data["approved_digest"], expected_mode="offline_test")
        (directory/("ready-"+index)).write_text("ready",encoding="utf-8")
        deadline = time.monotonic()+120
        while not (directory/"go").exists():
            if time.monotonic()>deadline: raise AssertionError("claim barrier timeout")
            time.sleep(0.01)
        try:
            owner = authority._claim_experiment(prepared)
            value = dict(owner_created=True,run_id=owner.run_id,dispatches=0)
            owner.stop("item6_fixture_claim_only"); owner.close()
            print(json.dumps(value)); return 0
        except BaseException as error:
            print(json.dumps(dict(owner_created=False,error=c.safe_error(error),dispatches=0,claim_may_exist=bool(getattr(error,"claim_may_exist",False)))))
            return 2


def suite_child(run_id, names):
    """Executed in an actual synthetic committed checkout, never a mock source verifier."""
    from researchops_internal_telemetry import source_integrity_v3 as source
    from services.agent_workflow_comparison_v1.controlled_publication_v1.public_artifacts import project
    from researchops_external_closure.io import scan_public_artifact_bytes
    before = source.verify_source(ROOT)
    directory = ROOT / "output/item6-bridge-validation" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    public = directory / "public"; public.mkdir()
    def guard(event, args):
        if event in {"socket.connect", "socket.sendto", "socket.sendmsg"}:
            address = args[1] if event != "socket.sendmsg" else args[-1]
            if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
                raise PermissionError("item6 offline suite forbids external sockets")
        if event == "socket.getaddrinfo" and args[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise PermissionError("item6 offline suite forbids external DNS")
    sys.addaudithook(guard)
    class Progress(io.StringIO):
        def write(self, value):
            result = super().write(value)
            sys.stdout.write(value); sys.stdout.flush()
            return result
    stream = Progress()
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    planned_tests = suite.countTestCases()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, failfast=True).run(suite)
    after = source.verify_source(ROOT)
    code = 0 if result.wasSuccessful() and before == after and result.testsRun == planned_tests else 1
    report = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors), skips=len(result.skipped),
        intended_exit_code=code, scope=list(names), source_before=before, source_after=after, inputs_stable=before == after,
        synthetic_commit=command(["git", "rev-parse", "HEAD"], ROOT).strip(), test_commit_not_project_publication=True,
        source_commitment_sha256=before["commitment_sha256"], provider_calls=0, real_store_touched=False,
        formal_comparison_result=False, log=stream.getvalue())
    report.update(planned_tests=planned_tests, test_plan_complete=result.testsRun == planned_tests, failfast=True)
    (directory / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    derived = project(report)
    try: scan_public_artifact_bytes((json.dumps(derived, ensure_ascii=False).encode(),))
    except Exception:
        derived["log"] = "Raw diagnostic log retained locally; not exported because of privacy-pattern detection."
        derived["failure_test_ids"] = [str(test) for test, _ in result.failures + result.errors]
    (public / "validation.json").write_text(json.dumps(derived, ensure_ascii=False, indent=2), encoding="utf-8")
    fixture_results = getattr(sys.modules.get("tests.item6_experiment_fixture"), "RESULTS", RESULTS)
    (directory / "fixture-results.json").write_text(json.dumps(fixture_results, ensure_ascii=False, indent=2).replace("offline-fixture-key-item6", "<synthetic-key-redacted>"), encoding="utf-8")
    print(json.dumps({k:report[k] for k in ("tests", "failures", "errors", "skips", "intended_exit_code", "inputs_stable")}), flush=True)
    return code


def suite_parent(run_id, *, remaining=False):
    from researchops_internal_telemetry import source_integrity_v3 as source
    import re
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", run_id) is None: raise ValueError("isolated_run_id")
    target = ROOT / "output/item6-bridge-validation" / run_id
    target.mkdir(parents=True, exist_ok=False)
    before = {name: source.digest((ROOT / name).read_bytes()) for name in source.source_files(ROOT)}
    fixture = seed()
    with (target / "process.log").open("x", encoding="utf-8") as log:
        process = subprocess.run([PYTHON, "-B", "-m", "tests.item6_experiment_fixture", "suite-remaining-child" if remaining else "suite-child", run_id],
            cwd=fixture, env=environment(fixture), stdout=log, stderr=subprocess.STDOUT, timeout=6000)
    source_dir = fixture / "output/item6-bridge-validation" / run_id
    if source_dir.exists():
        for path in source_dir.iterdir():
            if path.is_dir(): shutil.copytree(path, target / path.name)
            else: shutil.copyfile(path, target / path.name)
    after = {name: source.digest((ROOT / name).read_bytes()) for name in source.source_files(ROOT)}
    receipt = dict(actual_exit_code=process.returncode, source_before=before, source_after=after,
                   inputs_stable=before == after, synthetic_fixture_root=str(fixture), project_git_published=False)
    (target / "process-result.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"actual_exit_code": process.returncode, "inputs_stable": before == after}), flush=True)
    return process.returncode if before == after else 1


async def exercise(mode):
    logging.disable(logging.CRITICAL)
    from agents import set_tracing_disabled
    set_tracing_disabled(True)
    from researchops_completion_timing import local_claim
    from researchops_item6_experiment_v1 import contract as c, runner, session, authority, observations
    from services.agent_workflow_comparison_v1.deepseek_flash_v1.wire import ResponsesFixture
    import httpx2
    known = allocate("item6-test-store-")
    scripts = json.loads((ROOT / "services/agent_workflow_comparison_v1/controlled_comparison_v1/fixtures/development/mock_responses.json").read_text(encoding="utf-8"))
    calls, hits, responders = [], [], {}
    os.environ["DEEPSEEK_API_KEY"] = "offline-fixture-key-item6"
    faults = {"http401": {"http_status": 401}, "http429": {"http_status": 429}, "http503": {"http_status": 503},
              "timeout": {"timeout": True}, "cancel": {"cancel": True}, "missing_usage": {"missing_usage": True},
              "usage_boolean": {"usage_boolean": True}, "usage_overrun": {"usage_overrun": True},
              "cache_overrun": {"cache_overrun": True}, "truncated": {"truncated": True},
              "privacy": {"privacy_text": True}, "reasoning": {"reasoning_output": True},
              "duplicate_call": {"duplicate_call_id": True}, "bad_tool": {"unknown_tool": True},
              "bad_arguments": {"invalid_arguments": True}, "oversize": {"oversized_response": True}}
    if mode == "after_unknown_retry": faults[mode] = {"timeout": True}
    if mode == "free_expression":
        scripts["IC-01"]["steps"][-1]["text"] = "The source contains a small mass; see the aggregate package."
    text_variants = {"null_text": None, "empty_text": "", "whitespace_text": " \t\n　",
                     "path_text": "C:/Users/example/private.txt", "email_text": "sample@example.invalid",
                     "traceback_text": "Traceback: synthetic frame", "authorization_text": "Authorization: synthetic"}
    if mode in text_variants:
        scripts["IC-01"]["steps"][-1]["text"] = text_variants[mode]
    async def handler(request):
        body = json.loads(request.content)
        task = json.loads(body["input"][0]["content"])
        tid = task["task_id"]
        calls.append({"task_id": tid, "model": body["model"], "bytes": len(request.content)})
        if tid not in responders: responders[tid] = ResponsesFixture(scripts[tid], faults.get(mode, {}) if tid == "IC-01" else {})
        if mode in faults and tid == "IC-01": hits.append(mode)
        if mode in text_variants and tid == "IC-01": hits.append(mode)
        response = await responders[tid].handle(request)
        if tid == "IC-01" and mode in {"function_incomplete", "function_status_missing", "function_status_null"}:
            data = response.json()
            for item in data["output"]:
                if item["type"] == "function_call":
                    if mode == "function_status_missing": item.pop("status", None)
                    else: item["status"] = "incomplete" if mode == "function_incomplete" else None
                    hits.append(mode)
            response = httpx2.Response(200, json=data)
        if tid == "IC-01" and mode == "duplicate_call":
            data = response.json()
            if data["output"] and data["output"][0]["type"] == "function_call":
                data["output"].append(copy.deepcopy(data["output"][0]))
                hits.append("duplicate_in_single_response")
                response = httpx2.Response(200, json=data)
        if tid == "IC-01" and mode in {"missing_status", "null_status", "unknown_status"}:
            hits.append(mode); data = response.json()
            if mode == "missing_status": data.pop("status")
            else: data["status"] = None if mode == "null_status" else "future_state"
            response = httpx2.Response(200, json=data)
        if tid == "IC-01" and mode == "source_drift":
            hits.append(mode)
            path = ROOT / "src/researchops_item6_experiment_v1/budget.py"
            path.write_bytes(path.read_bytes() + b"\n# isolated live source mutation\n")
        return response
    real_network_attempts = []
    def no_network(*a, **kw):
        real_network_attempts.append("blocked"); raise AssertionError("real network forbidden")
    with patch.object(local_claim, "_windows_local_app_data", return_value=known), \
         patch.object(session, "_native_transport", side_effect=lambda mode: httpx2.MockTransport(handler)), \
         patch("socket.socket.connect", side_effect=no_network), patch("socket.getaddrinfo", side_effect=no_network), \
         patch("socket.socket.sendto", side_effect=no_network):
        status = local_claim.provision_local_claim_store()
        pricing = dict(kind="synthetic_fixture", input_per_million="1", output_per_million="2", cost_limit="10",
                       input_accounting="synthetic_bytes", provider_bill_hard_cap=False, evidence_sha256=None)
        freeze = c.build_freeze(mode="offline_test", environment_id=status["execution_environment_id"], pricing=pricing)
        if mode == "manifest_hash_mismatch":
            freeze["source_manifest_sha256"] = "1" * 64
            hits.append(mode)
        instant = c.now()
        candidate = dict(scope=c.SCOPE, mode="offline_test", authorization_id="item6-fixture-" + uuid.uuid4().hex,
            freeze_sha256=c.commit("freeze", freeze), environment_id=status["execution_environment_id"],
            not_before_utc=(instant-timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
            expires_at_utc=(instant+timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        if mode == "expired": candidate["expires_at_utc"] = (instant-timedelta(seconds=1)).isoformat().replace("+00:00", "Z"); hits.append(mode)
        approved = c.commit("approval-candidate", candidate)
        approval = dict(schema_version="item6-experiment-approval/1.0", candidate=candidate,
            observation=dict(kind="synthetic_approval_fixture", approved_digest=approved,
                observed_at_utc=instant.isoformat().replace("+00:00", "Z"), message_reference="synthetic-fixture-only"))
        kwargs = dict(freeze_bytes=c.raw(freeze), approval_bytes=c.raw(approval), approved_digest=approved)
        if mode == "noncanonical_documents":
            hits.append(mode)
            kwargs["freeze_bytes"] = json.dumps(freeze, ensure_ascii=False, indent=2).encode()
        result = None
        error = None
        claim_may_exist = False
        admission_observations = None
        partial_observations = None
        primary_stop_reason = None
        factories = []
        post_checks = {}
        extra = ExitStack()
        if mode in {"factory_freeze_drift", "budget_policy_drift"}:
            original_open = session.ExperimentFactory.open_case
            def mutate_policy(self, case):
                hits.append(mode)
                if mode == "factory_freeze_drift": self.freeze["budget"]["output_per_request"] += 1
                else: self.budget.limits["output_per_request"] += 1
                return original_open(self, case)
            extra.enter_context(patch.object(session.ExperimentFactory, "open_case", new=mutate_policy))
        if mode in text_variants or mode in {"tool_missing_facts", "expiry_after_scoring", "expiry_after_export", "expiry_after_terminal"}:
            original_open_case = session.ExperimentFactory.open_case
            def stop_after_boundary_case(self, case):
                if self.case_index == 2:
                    hits.append("declared_stop_after_boundary_case")
                    raise c.ExperimentError("fixture_boundary_case_stop")
                return original_open_case(self, case)
            extra.enter_context(patch.object(session.ExperimentFactory, "open_case", new=stop_after_boundary_case))
        if mode in {"after_unknown_retry", "factory_clone"}:
            original_init = session.ExperimentFactory.__init__
            def observe_factory(self, *args):
                original_init(self, *args); factories.append(self)
                if mode == "factory_clone":
                    hits.append(mode)
                    self.owner.assert_factory(copy.copy(self))
            extra.enter_context(patch.object(session.ExperimentFactory, "__init__", new=observe_factory))
        if mode in {"cross_case_handle", "cross_case_handle_after_one"}:
            from dataclasses import replace
            original_finalize = session.ExperimentSession._finalize
            def foreign_handle(self, handle, *args, **kw):
                if mode == "cross_case_handle_after_one" and len(self.event_commitment()["started"]) == 1:
                    return original_finalize(self, handle, *args, **kw)
                hits.append(mode)
                wrong = next(x for x in self.factory.freeze["model_case_handles"].values() if x != self.case_id)
                return original_finalize(self, replace(handle, case_id=wrong), *args, **kw)
            extra.enter_context(patch.object(session.ExperimentSession, "_finalize", new=foreign_handle))
        if mode in {"claim_write", "claim_fsync", "claim_close"}:
            original_write = local_claim._write_once
            def damaged_write(path, data):
                if path.parent.name != "claims": return original_write(path, data)
                target = {"claim_write": "write", "claim_fsync": "fsync", "claim_close": "close"}[mode]
                real_os = local_claim.os
                original_operation = getattr(real_os, target)
                observation = dict(target=target, call_count=0, claim_parent_matched=True,
                    module_os_isolated=False, module_os_restored=False, process_os_unchanged=False)
                post_checks["claim_fault"] = observation
                def fail_operation(*args):
                    observation["call_count"] += 1
                    if mode not in hits: hits.append(mode)
                    if mode == "claim_close": original_operation(*args)
                    elif mode == "claim_write": original_operation(args[0], args[1][:8])
                    raise OSError("synthetic claim operation failure")
                class FaultOS:
                    def __getattr__(self, name): return getattr(real_os, name)
                fault_os = FaultOS()
                setattr(fault_os, target, fail_operation)
                try:
                    with patch.object(local_claim, "os", fault_os):
                        observation["module_os_isolated"] = local_claim.os is fault_os and os is real_os
                        return original_write(path, data)
                finally:
                    observation["module_os_restored"] = local_claim.os is real_os
                    observation["process_os_unchanged"] = getattr(os, target) is original_operation
            extra.enter_context(patch.object(local_claim, "_write_once", side_effect=damaged_write))
        if mode in {"audit_start", "audit_intent"}:
            if mode == "audit_start":
                original_write = session.ExperimentSession._write
                def fail_start(self, event_type, payload, **kw):
                    if event_type == "model_request_started":
                        hits.append(mode); raise OSError("synthetic audit failure")
                    return original_write(self, event_type, payload, **kw)
                extra.enter_context(patch.object(session.ExperimentSession, "_write", new=fail_start))
            else:
                original_append = session.AuditLedger.append_event
                def fail_intent(self, run_id, event_type, payload, **kw):
                    if event_type == "item6_send_intent_v1":
                        hits.append(mode); raise OSError("synthetic intent failure")
                    return original_append(self, run_id, event_type, payload, **kw)
                extra.enter_context(patch.object(session.AuditLedger, "append_event", new=fail_intent))
        if mode in {"audit_tool_start", "audit_tool_finish"}:
            original_tool_append = session.AuditLedger.append_event
            original_observed_read = observations.Case._read_tool
            target_event = "item6_tool_started_v1" if mode == "audit_tool_start" else "item6_tool_finished_v1"
            def fail_tool_audit(self, run_id, event_type, payload, **kw):
                if event_type == target_event and payload.get("case_run_id", "").endswith("-agent-IC-01"):
                    hits.append(mode)
                    raise OSError("synthetic tool audit failure")
                return original_tool_append(self, run_id, event_type, payload, **kw)
            def observe_tool_read(self, tool, arguments):
                if self.path == "agent" and self.task["task_id"] == "IC-01": hits.append("agent_tool_read_observed")
                return original_observed_read(self, tool, arguments)
            extra.enter_context(patch.object(session.AuditLedger, "append_event", new=fail_tool_audit))
            extra.enter_context(patch.object(observations.Case, "_read_tool", new=observe_tool_read))
        if mode in {"tool_failure", "tool_timeout", "tool_missing_facts"}:
            original_read = observations.Case._read_tool
            def fail_tool(self, tool, arguments):
                if self.task["task_id"] == "IC-01" and self.path == "agent":
                    hits.append(mode)
                    if mode == "tool_timeout": time.sleep(self.factory.freeze["budget"]["tool_seconds"] + 0.1)
                    elif mode == "tool_failure": raise OSError("synthetic read failure")
                    else:
                        damaged = original_read(self, tool, arguments)
                        damaged.pop("facts")
                        return damaged
                return original_read(self, tool, arguments)
            extra.enter_context(patch.object(observations.Case, "_read_tool", new=fail_tool))
        if mode == "wrong_origin":
            hits.append(mode)
            extra.enter_context(patch.object(runner.DeepSeekProvider, "base_url", "https://gateway.invalid"))
        if mode == "offline_native_delegate":
            hits.append(mode)
            extra.enter_context(patch.object(session, "_native_transport", side_effect=lambda mode: httpx2.AsyncHTTPTransport()))
        if mode == "expiry_after_source":
            original_source = c.check_source
            original_before = session.ExperimentFactory.before_send
            armed = [False]
            def before(self, *args):
                armed[0] = True
                return original_before(self, *args)
            def checked_then_expired(freeze):
                answer = original_source(freeze)
                if armed[0]:
                    hits.append(mode)
                    extra.enter_context(patch.object(c, "now", return_value=instant+timedelta(hours=2)))
                    armed[0] = False
                return answer
            extra.enter_context(patch.object(session.ExperimentFactory, "before_send", new=before))
            extra.enter_context(patch.object(c, "check_source", side_effect=checked_then_expired))
        if mode in {"expiry_after_scoring", "expiry_after_export", "expiry_after_seal", "expiry_after_terminal"}:
            from researchops_item6_experiment_v1 import finalization
            def expire():
                hits.append(mode)
                extra.enter_context(patch.object(c, "now", return_value=instant+timedelta(hours=2)))
            if mode == "expiry_after_scoring":
                original_score = runner.score_business
                def score_then_expire(*args):
                    result = original_score(*args); expire(); return result
                extra.enter_context(patch.object(runner, "score_business", side_effect=score_then_expire))
            elif mode == "expiry_after_export":
                original_export = session.AuditLedger.export_run
                def export_then_expire(self, *args, **kw):
                    result = original_export(self, *args, **kw); expire(); return result
                extra.enter_context(patch.object(session.AuditLedger, "export_run", new=export_then_expire))
            elif mode == "expiry_after_seal":
                original_archive = runner.write_archive
                def seal_then_expire(*args):
                    result = original_archive(*args); expire(); return result
                extra.enter_context(patch.object(runner, "write_archive", side_effect=seal_then_expire))
            else:
                original_check = finalization.FinalizationDeadline.check
                def terminal_then_expire(self, stage):
                    if stage == "terminal_after_write": expire()
                    return original_check(self, stage)
                extra.enter_context(patch.object(finalization.FinalizationDeadline, "check", new=terminal_then_expire))
        failed_archive = None
        try:
            if mode == "offline_to_live":
                hits.append(mode); result = await runner.run_live(**kwargs)
            elif mode == "forged_prepared":
                hits.append(mode); authority._claim_experiment({"receipt": "success"})
            else:
                result = await runner._run_offline_test(**kwargs)
                if mode == "after_unknown_retry":
                    before_retry = len(calls)
                    try: factories[0].sessions[0].begin_attempt()
                    except BaseException as exc: post_checks["retry_error"] = c.safe_error(exc)
                    else: raise AssertionError("unknown outcome retry accepted")
                    post_checks["calls_before_retry"] = before_retry
                    post_checks["calls_after_retry"] = len(calls)
                if mode == "duplicate":
                    hits.append(mode)
                    try: await runner._run_offline_test(**kwargs)
                    except BaseException as exc: error = c.safe_error(exc)
                    else: raise AssertionError("duplicate accepted")
        except BaseException as exc:
            error = c.safe_error(exc)
            claim_may_exist = bool(getattr(exc, "claim_may_exist", False))
            admission_observations = getattr(exc, "admission_observations", None)
            partial_observations = getattr(exc, "partial_observations", None)
            primary_stop_reason = getattr(exc, "primary_stop_reason", None)
            failed_archive = getattr(exc, "archive_namespace", None)
        finally:
            extra.close()
        files = list((known / "ResearchOpsAgent/completion-claims-v1/claims").glob("*.json"))
        value = dict(mode=mode, run=result, error=error, calls=calls, hits=hits, claim_files=len(files), claim_may_exist=claim_may_exist, post_checks=post_checks,
            admission_observations=admission_observations, partial_observations=partial_observations, primary_stop_reason=primary_stop_reason,
            real_store_touched=False, provider_calls=0, network_attempts=len(real_network_attempts),
            synthetic_approval=True, synthetic_commit=True)
        if result is not None:
            if "archive_kind" in result:
                assert result["archive_kind"] == "unfinished_failure"
                failure = c.decode((ROOT / result["archive_namespace"] / "failed-experiment.json").read_bytes(), c.policy()["archive_bytes"])
                artifact = failure["artifact"]
                value["attempt_failures"] = failure["attempt_failures"]
            else:
                artifact = c.decode((ROOT / result["archive_namespace"] / "experiment.json").read_bytes(), c.policy()["archive_bytes"])
            value["artifact"] = artifact
        if failed_archive is not None:
            directory = ROOT / failed_archive
            value["failed_archive_namespace"] = failed_archive
            value["finalization_failure"] = c.decode((directory / "finalization-failure.json").read_bytes())
            value["candidate_status"] = c.decode((directory / "experiment.json").read_bytes(), c.policy()["archive_bytes"])["status"] if (directory / "experiment.json").exists() else None
        return value


if __name__ == "__main__":
    if sys.argv[1] in {"prepare-claim-fixture", "claim-only"}:
        raise SystemExit(claim_fixture_child(sys.argv[1],Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4] if len(sys.argv)>4 else None))
    elif sys.argv[1] == "suite":
        raise SystemExit(suite_parent(sys.argv[2]))
    elif sys.argv[1] == "suite-remaining":
        raise SystemExit(suite_parent(sys.argv[2], remaining=True))
    elif sys.argv[1] == "suite-child":
        raise SystemExit(suite_child(sys.argv[2], NEW_TESTS + RELATED_TESTS))
    elif sys.argv[1] == "suite-remaining-child":
        raise SystemExit(suite_child(sys.argv[2], ("tests.test_internal_source_integrity_v3",) + RELATED_TESTS))
    elif sys.argv[1] == "freeze-source":
        from researchops_internal_telemetry import source_integrity_v3 as v3
        report = v3.write_manifest_exclusive(ROOT)
        manifest = json.loads((ROOT / v3.MANIFEST).read_bytes())
        workflow = ROOT / ".github/workflows/ci.yml"
        text = workflow.read_text(encoding="utf-8").replace("ITEM6_V3_COMMITMENT", manifest["commitment_sha256"]).replace("ITEM6_V3_MANIFEST_SHA256", report["sha256"])
        workflow.write_text(text, encoding="utf-8", newline="\n")
        print(json.dumps(report))
    else:
        try:
            result = asyncio.run(exercise(sys.argv[2])); print(json.dumps(result, ensure_ascii=True))
            raise SystemExit(0 if result["error"] is None and result["run"] and result["run"]["actual_exit_code"] == 0 else 2)
        except SystemExit: raise
        except BaseException as error:
            print(json.dumps({"fixture_error": type(error).__name__, "code": getattr(error, "code", None)}))
            raise SystemExit(3)
