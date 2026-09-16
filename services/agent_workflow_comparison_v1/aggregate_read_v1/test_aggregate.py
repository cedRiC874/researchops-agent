from copy import deepcopy
from decimal import localcontext
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

from . import run
from .core import (CONFIG, ROOT, TOOL, Session, convert, coverage, digest, fixed_run,
                   load_json, not_executed, public_context, text_state, validate_bundle)
from .paths import agent_run
from .adapter import envelope, project, transfer_stub
from .variants import apply_variant


class AggregateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tasks = load_json(ROOT / "fixtures/tasks.json")["tasks"]
        cls.scripts = load_json(ROOT / "fixtures/scripts.json")
        cls.specs = load_json(ROOT / "fixtures/variants.json")["variants"]
        cls.fixed = {t["task_id"]: fixed_run(t, "f-" + t["task_id"]) for t in cls.tasks}
        cls.agent = {t["task_id"]: run.LOOP.run_until_complete(agent_run(t, cls.scripts[t["task_id"]],
                     "a-" + t["task_id"])) for t in cls.tasks}

    def test_eight_new_scenarios(self):
        self.assertEqual([t["task_id"] for t in self.tasks], [f"AGR-{i:02}" for i in range(1, 9)])
        self.assertEqual(set(self.scripts), {t["task_id"] for t in self.tasks})

    def test_fixed_business_branches(self):
        self.assertEqual([r["status"] for r in self.fixed.values()],
            ["completed"] * 5 + ["clarification", "refusal", "failed"])
        self.assertEqual(self.fixed["AGR-01"]["final_output"], "A的质量为37.50 mg [E1]。")
        self.assertEqual(self.fixed["AGR-05"]["events"][0]["arguments"], {"bundle_id": "beta"})

    def test_same_inputs_catalog_and_actual_tool_results(self):
        for t in self.tasks:
            a, f = self.agent[t["task_id"]], self.fixed[t["task_id"]]
            self.assertEqual(a["binding"]["input_sha256"], digest(public_context(t)))
            self.assertEqual(a["binding"]["input_sha256"], f["binding"]["input_sha256"])
            self.assertEqual(a["events"], f["events"])
            self.assertEqual(a["binding"]["bundle_sha256"], f["binding"]["bundle_sha256"])

    def test_actual_sdk_loop_not_stubbed_runner(self):
        r = self.agent["AGR-01"]
        self.assertEqual(r["status"], "completed")
        self.assertEqual(len(r["model_requests"]), 2)
        self.assertEqual(r["events"][0]["status"], "succeeded")
        self.assertEqual(r["events"][0]["result"]["facts"][0]["value"], "37.50")
        self.assertEqual(r["model_response_origin"], "scripted_model_response")

    def test_decimal_conversion_exact_and_context_independent(self):
        with localcontext() as ctx:
            ctx.prec = 2
            self.assertEqual(convert("37.50", "mg", "g"), "0.03750")
            self.assertEqual(convert("-0.00375", "g", "mg"), "-3.75")
            self.assertEqual(convert("1e-20", "g", "mg"), "0.00000000000000001")
        with self.assertRaises(ValueError):
            convert("1", "mm", "mg")

    def test_unit_request_changes_text_not_source_projection(self):
        self.assertEqual(self.fixed["AGR-02"]["final_output"], "A的质量为0.03750 g [E1]。")
        f = self.fixed["AGR-02"]["allowed_evidence"][0]["facts"][0]
        self.assertEqual((f["value"], f["unit"]), ("37.50", "mg"))

    def test_both_directions_are_separate_source_records(self):
        self.assertEqual(self.fixed["AGR-03"]["final_output"], "A-B的差值为-3.75 mg [E1]。")
        self.assertEqual(self.fixed["AGR-04"]["final_output"], "B-A的差值为3.75 mg [E1]。")
        facts = self.fixed["AGR-04"]["allowed_evidence"][0]["facts"]
        self.assertIn({"subject": "B-A", "metric": "difference", "value": "3.75", "unit": "mg"}, facts)

    def test_no_silent_reverse_derivation(self):
        t = {**self.tasks[4], "subject": "A-B", "metric": "difference"}
        r = fixed_run(t, "missing-direction")
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["known_failures"][-1]["code"], "requested_fact_not_available")

    def test_missing_design_and_fabrication_have_zero_tools(self):
        for outputs in (self.fixed, self.agent):
            for tid in ("AGR-06", "AGR-07"):
                self.assertEqual(outputs[tid]["events"], [])
                self.assertEqual(outputs[tid]["allowed_evidence"], [])
                self.assertEqual(outputs[tid]["approval_interruptions"], [])
        for t in self.tasks[5:7]:
            s = Session(t, "agent", "denied")
            s.execute("alpha")
            self.assertEqual(s.record["events"][0]["status"], "failed")
            self.assertEqual(s.record["allowed_evidence"], [])

    def test_corrupt_file_is_actual_tool_failure_without_model_verdict(self):
        with self.assertRaises(json.JSONDecodeError):
            load_json(ROOT / "fixtures/damaged.json")
        for output in (self.fixed, self.agent):
            r = output["AGR-08"]
            self.assertEqual(r["events"][0]["result"]["type"], "JSONDecodeError")
            self.assertEqual(r["status"], "failed")
            self.assertEqual(r["allowed_evidence"], [])
            self.assertEqual(r["known_failures"][0]["attribution"], "not_a_model_capability_verdict")
            self.assertEqual(r["score_status"], "not_scored")

    def test_real_evidence_identity_content_and_deep_copies(self):
        s = Session(self.tasks[0], "agent", "this-run")
        result = s.execute("alpha")
        result["facts"][0]["value"] = "999"
        r = s.record
        evidence = r["allowed_evidence"][0]
        event = r["events"][0]
        self.assertEqual(evidence["run_id"], "this-run")
        self.assertNotEqual(evidence["source_record_id"], evidence["run_id"])
        self.assertEqual(evidence["call_id"], event["call_id"])
        self.assertIn(evidence["artifact_id"], event["produced_artifacts"])
        self.assertEqual(evidence["facts"], event["result"]["facts"])
        self.assertEqual(evidence["projection_sha256"], digest(evidence["facts"]))
        self.assertEqual(evidence["facts"][0]["value"], "37.50")

    def test_unallowlisted_path_does_not_read(self):
        for bid in ("../alpha.json", "https://example.invalid", "unknown"):
            s = Session(self.tasks[0], "agent", "r")
            with patch.object(Path, "read_text", side_effect=AssertionError("must not read")):
                s.execute(bid)
            self.assertEqual(s.record["events"][0]["status"], "failed")

    def test_bundle_byte_change_after_binding_is_rejected(self):
        s = Session(self.tasks[0], "agent", "r")
        with patch("services.agent_workflow_comparison_v1.aggregate_read_v1.core.file_hash", return_value="changed"):
            s.execute("alpha")
        self.assertEqual(s.record["events"][0]["status"], "failed")

    def test_other_allowed_bundle_is_observed_without_oracle_correction(self):
        script = deepcopy(self.scripts["AGR-01"])
        script["steps"][0]["arguments"]["bundle_id"] = "beta"
        r = run.LOOP.run_until_complete(agent_run(self.tasks[0], script, "other-allowed"))
        self.assertEqual(r["events"][0]["status"], "succeeded")
        self.assertEqual(r["allowed_evidence"][0]["facts"][0]["subject"], "B")
        self.assertEqual(r["final_output"], script["steps"][-1]["text"])
        self.assertEqual(r["score_status"], "not_scored")

    def test_fake_cannot_create_publish_or_approval_event(self):
        script = deepcopy(self.scripts["AGR-01"])
        script["steps"][0]["tool"] = "publish_aggregate_results"
        r = run.LOOP.run_until_complete(agent_run(self.tasks[0], script, "denied"))
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["events"], [])
        self.assertEqual(r["plan"][0]["execution"], "not_executed")
        self.assertEqual(r["approval_interruptions"], [])

    def test_execution_budget_and_no_retry_after_error(self):
        s = Session(self.tasks[7], "agent", "r")
        s.execute("gamma")
        with patch.object(Path, "read_text", side_effect=AssertionError("must stop")):
            s.execute("alpha")
        self.assertEqual([e["status"] for e in s.record["events"]], ["failed", "failed"])
        self.assertEqual(s.record["allowed_evidence"], [])

    def test_model_request_budget(self):
        script = deepcopy(self.scripts["AGR-01"])
        script["steps"] = [script["steps"][0]] * 4
        r = run.LOOP.run_until_complete(agent_run(self.tasks[0], script, "r"))
        self.assertEqual(r["status"], "failed")
        self.assertLessEqual(len(r["model_requests"]), CONFIG["max_model_requests"])

    def test_input_gold_labels_rejected_in_both_paths(self):
        for label in ("gold", "expected_method", "contract", "correct_bundle", "expected_answer"):
            t = {**self.tasks[0], label: "secret-canary"}
            with self.assertRaises(ValueError):
                fixed_run(t, "denied")
            with self.assertRaises(ValueError):
                run.LOOP.run_until_complete(agent_run(t, self.scripts["AGR-01"], "denied"))

    def test_decisions_do_not_read_scripts_scenarios_variants_or_scorer(self):
        old_open = Path.open
        def guarded(p, *a, **kw):
            if any(s in str(p) for s in ("scripts.json", "scenarios.json", "variants.json", "behavior_eval", "gold")):
                raise AssertionError("hidden input read")
            return old_open(p, *a, **kw)
        with patch.object(Path, "open", guarded):
            fixed_run(self.tasks[0], "f")
            r = run.LOOP.run_until_complete(agent_run(self.tasks[0], self.scripts["AGR-01"], "a"))
        self.assertEqual(r["status"], "completed")

    def test_no_key_or_real_store_access(self):
        getitem, original_open = type(os.environ).__getitem__, Path.open
        def env_get(env, key):
            if "KEY" in key.upper() or "SECRET" in key.upper():
                raise AssertionError("secret access")
            return getitem(env, key)
        def file_open(p, *a, **kw):
            if "claim" in str(p).lower() or str(p).endswith(".env"):
                raise AssertionError("real store access")
            return original_open(p, *a, **kw)
        with patch.object(type(os.environ), "__getitem__", env_get), patch.object(Path, "open", file_open):
            fixed_run(self.tasks[0], "f")
            r = run.LOOP.run_until_complete(agent_run(self.tasks[0], self.scripts["AGR-01"], "a"))
        self.assertEqual(r["status"], "completed")

    def test_source_schema_rejects_extra_gold_nonfinite_and_duplicate_facts(self):
        bundle = load_json(ROOT / "fixtures/alpha.json")
        variants = [{**bundle, "gold": "no"}]
        duplicate = deepcopy(bundle); duplicate["facts"].append(duplicate["facts"][0]); variants.append(duplicate)
        for value in ("NaN", "Infinity", "1e101", "1_000", " 1", "１"):
            bad = deepcopy(bundle); bad["facts"][0]["value"] = value; variants.append(bad)
        for bad in variants:
            with self.assertRaises(ValueError):
                validate_bundle(bad, "alpha")

    def test_core_deterministic_and_inputs_unchanged(self):
        before = deepcopy((self.tasks, self.scripts))
        for t in self.tasks:
            self.assertEqual(fixed_run(t, "same"), fixed_run(t, "same"))
            a = run.LOOP.run_until_complete(agent_run(t, self.scripts[t["task_id"]], "same"))
            b = run.LOOP.run_until_complete(agent_run(t, self.scripts[t["task_id"]], "same"))
            self.assertEqual(a, b)
        self.assertEqual((self.tasks, self.scripts), before)

    def test_raw_text_null_empty_whitespace_lossless(self):
        for text, state in [(None, "unobserved"), ("", "empty"), (" \t\n　", "whitespace"), ("\u200b", "nonempty")]:
            r = Session(self.tasks[0], "fixed_workflow", "r").finish(text, "unknown", completion="unknown")
            self.assertEqual(r["text_observation"], state)
            self.assertEqual(project(r)["observation"]["final_output"], text)

    def test_sdk_raw_empty_and_whitespace(self):
        for text in ("", " \t\n　"):
            script = deepcopy(self.scripts["AGR-06"]); script["steps"][0]["text"] = text
            r = run.LOOP.run_until_complete(agent_run(self.tasks[5], script, "r"))
            self.assertEqual(r["final_output"], text)

    def test_free_expression_is_not_rewritten_or_scored(self):
        r = self.agent["AGR-05"]
        self.assertEqual(project(r)["observation"]["final_output"], self.scripts["AGR-05"]["steps"][-1]["text"])
        self.assertNotEqual(r["final_output"], self.fixed["AGR-05"]["final_output"])
        self.assertFalse(r["comparability"]["effectiveness_comparable"])
        self.assertEqual(r["score_status"], "not_scored")

    def test_known_failure_retained_with_null_and_timeout(self):
        s = Session(self.tasks[7], "agent", "r"); s.execute("gamma")
        r = s.finish(None, "unknown", completion="timeout")
        self.assertEqual(r["status"], "failed")
        self.assertEqual([f["code"] for f in r["known_failures"]], ["tool_failed", "timeout"])
        self.assertEqual(project(r)["observation"]["completion"], "timeout")

    def test_fault_variants_preserve_invalid_material_without_judging(self):
        for spec in self.specs:
            base = self.agent[spec["base_task_id"]]; before = deepcopy(base)
            v = apply_variant(base, spec)
            self.assertEqual(base, before)
            self.assertEqual(v["origin"], "collector_fault_fixture")
            self.assertEqual(v["score_status"], "not_scored")
            projected = project(v["observation"])
            if spec["kind"] == "text":
                self.assertEqual(projected["observation"]["final_output"], spec["value"])
            if spec["kind"] == "other_run_evidence":
                self.assertEqual(projected["allowed_evidence"][0]["run_id"], "synthetic-other-run")
            if spec["kind"] == "missing_evidence_content":
                self.assertIsNone(projected["allowed_evidence"][0]["facts"])
            if spec["kind"] == "not_produced":
                self.assertEqual(projected["observation"]["events"][0]["produced_artifacts"], [])

    def test_fixed_denominator_for_missing_and_unexecuted(self):
        ids = [t["task_id"] for t in self.tasks]
        not_run = not_executed(self.tasks[1], "agent", "not-run")
        report = coverage(ids, [self.agent["AGR-01"], not_run])
        self.assertEqual(report["counts"], dict(planned=8, observed=1, not_executed=1, observation_missing=6))
        packet = envelope(report)
        self.assertEqual(len(packet["rows"]), 8)
        self.assertIsNone(packet["rows"][2]["observation"])
        self.assertEqual(project(not_run)["observation"]["events"], [])
        self.assertEqual(transfer_stub(packet)["received_rows"], 8)
        with self.assertRaises(ValueError):
            coverage(ids, [not_run, not_run])

    def test_mapping_uses_actual_facts_and_no_scorer_contract(self):
        r = self.agent["AGR-01"]
        p = project(r)
        self.assertEqual(p["allowed_evidence"][0]["facts"], r["events"][0]["result"]["facts"])
        self.assertEqual(p["mapping_blockers"], ["no_confirmed_scorer_integration_commit"])
        self.assertNotIn("contract", p)
        self.assertNotIn("atol", json.dumps(p))

    def test_stub_error_handling_without_scores(self):
        packet = envelope(coverage(["AGR-01"], [self.fixed["AGR-01"]]))
        before = deepcopy(packet)
        receipt = transfer_stub(packet)
        self.assertEqual(packet, before)
        self.assertEqual(receipt["score_status"], "not_scored")
        self.assertNotIn("pass", receipt)
        for key, value in [("actual_scorer_connected", True), ("planned", 2), ("target_measurement_revision", "expanded")]:
            with self.assertRaises(ValueError):
                transfer_stub({**packet, key: value})
        del packet["rows"][0]["observation"]["final_output"]
        with self.assertRaises(ValueError):
            transfer_stub(packet)

    def test_metrics_no_synthetic_usage_cost_or_time(self):
        a, f = self.agent["AGR-01"], self.fixed["AGR-01"]
        self.assertEqual(f["metrics"]["model_request_count"]["value"], 0)
        self.assertEqual(a["metrics"]["model_request_count"]["value"], 2)
        self.assertEqual(f["metrics"]["tokens"]["availability"], "not_applicable")
        self.assertEqual(a["metrics"]["tokens"]["availability"], "unavailable")
        for r in (a, f):
            self.assertEqual(r["metrics"]["provider_request_count"]["value"], 0)
            for name in ("tokens", "api_cost", "api_latency", "human_time", "local_elapsed"):
                self.assertIsNone(r["metrics"][name]["value"])

    def test_network_and_child_process_actually_denied(self):
        for host in ("127.0.0.1", "192.0.2.1"):
            with socket.socket() as sock:
                with self.assertRaises(PermissionError):
                    sock.connect((host, 9))
        with self.assertRaises(PermissionError):
            socket.getaddrinfo("example.invalid", 443)
        with socket.socket(type=socket.SOCK_DGRAM) as sock:
            with self.assertRaises(PermissionError):
                sock.sendto(b"offline-test", ("192.0.2.1", 9))
        with self.assertRaises(PermissionError):
            subprocess.Popen([sys.executable, "-c", "pass"])

    def test_cli_cannot_overwrite_old_evidence_or_escape(self):
        with self.assertRaises(ValueError):
            run.output_path(ROOT.parent / "example-delivery.json")
        p = ROOT / "fixtures/tasks.json"; before = p.read_bytes()
        with patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(run.main(["--output", str(p)]), 2)
        self.assertEqual(p.read_bytes(), before)
