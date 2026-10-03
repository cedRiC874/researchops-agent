from pathlib import Path
from copy import deepcopy
from contextlib import closing
import json
import sqlite3
import shutil
import subprocess
import unittest
import uuid
from researchops_item6_experiment_v1 import contract as c
from . import item6_experiment_fixture as f


FINALIZATION_EXPIRY_DIAGNOSTICS = []


def _observe_finalization_expiry(result, mode, stage, sealed, verifier=None):
    """Value-free test observation; never changes the result or an assertion."""
    def number(value):
        return value if type(value) is int and -1 <= value <= 4096 else None

    def enum(value, allowed):
        return value if type(value) is str and value in allowed else None

    failure = result.get("finalization_failure")
    failure = failure if type(failure) is dict else {}
    partial = result.get("partial_observations")
    partial_count = (sum(len(value) for value in partial.values())
                     if type(partial) is dict and all(type(value) is dict for value in partial.values()) else None)
    calls, hits = result.get("calls"), result.get("hits")
    row = dict(schema="item6-finalization-expiry-test-observation/1", mode=mode,
        expected_stage=stage, expected_seal_present=sealed,
        process_exit_code=number(result.get("process_exit_code")), expected_process_exit_code=2,
        injection_hit=mode in hits if type(hits) is list else None,
        run_is_null=result.get("run") is None,
        error_code=enum(result.get("error"), ("item6_finalization_deadline",)),
        error_code_allowlisted=result.get("error") == "item6_finalization_deadline",
        failure_status=enum(failure.get("status"), ("failed", "completed")),
        failure_stage=enum(failure.get("stage"), ("start", "scoring_after", "export_after", "seal_after", "terminal_after_write")),
        seal_present=failure.get("seal_present") if type(failure.get("seal_present")) is bool else None,
        partial_observation_count=number(partial_count), expected_partial_observation_count=32,
        calls_count=number(len(calls)) if type(calls) is list else None,
        candidate_status=enum(result.get("candidate_status"), ("failed", "completed")),
        exception_text_recorded=False, path_values_recorded=False, raw_output_recorded=False)
    if verifier is not None:
        row["verifier_exit_code"] = number(verifier.returncode)
        row["expected_verifier_exit_code"] = 2
        try:
            document = json.loads(verifier.stdout.splitlines()[-1])
        except (ValueError, IndexError):
            document = None
        row["verifier_output_is_json_object"] = type(document) is dict
        value = document.get("error") if type(document) is dict else None
        row["verifier_error_code"] = enum(value, ("item6_finalization_failed",))
        row["verifier_error_code_allowlisted"] = value == "item6_finalization_failed"
    # This named test-only observation is not a shared-fixture marker. Keep
    # that existing marker allowlist strict and reuse its scanner/size bound.
    from researchops_external_closure.io import scan_public_artifact_bytes
    encoded = json.dumps(row, ensure_ascii=True, sort_keys=True)
    if len(encoded.encode()) > 16384:
        raise ValueError("finalization_diagnostic_size")
    scan_public_artifact_bytes((encoded.encode(),))
    print("ITEM6_FINALIZATION_DIAGNOSTIC " + encoded, flush=True)
    FINALIZATION_EXPIRY_DIAGNOSTICS.append(row)


class ArtifactTests(unittest.TestCase):
    def _assert_rehashed_diagnostic_rejected(self, change, code, *, mode="cross_case_handle"):
        from researchops_item6_experiment_v1.failed_archive import verify_failed_archive
        result = f.unfinished_case(mode)
        directory = Path(result["fixture_root"]) / result["run"]["archive_namespace"]
        clone = f.allocate("item6-attempt-link-")
        for name in ("failed-experiment.json", "audit.sqlite3", "failure-receipt.json"):
            shutil.copyfile(directory / name, clone / name)
        document = c.decode((clone / "failed-experiment.json").read_bytes(), c.policy()["archive_bytes"])
        self.assertEqual(document["schema_version"], "item6-unfinished-failure/1.1")
        change(document)
        data = c.raw(document)
        (clone / "failed-experiment.json").write_bytes(data)
        receipt = c.decode((clone / "failure-receipt.json").read_bytes())
        receipt["files"]["failed-experiment.json"] = dict(bytes=len(data), sha256=c.digest(data))
        receipt_bytes = c.raw(receipt)
        (clone / "failure-receipt.json").write_bytes(receipt_bytes)
        digest = c.digest(receipt_bytes)
        # Both external hashes really match the mutated bytes; rejection must be semantic.
        self.assertEqual(c.digest((clone / "failure-receipt.json").read_bytes()), digest)
        self.assertEqual(receipt["files"]["failed-experiment.json"]["sha256"], c.digest((clone / "failed-experiment.json").read_bytes()))
        with self.assertRaises(c.ExperimentError) as rejected:
            verify_failed_archive(clone, expected_failure_sha256=digest)
        self.assertEqual(rejected.exception.code, "item6_" + code)
        f.RESULTS.append(dict(kind="rehashed_attempt_link_rejection",mode=mode,
            expected_code="item6_"+code,actual_code=rejected.exception.code,outer_hashes_match=True))

    def test_failed_diagnostic_wrong_planned_case_is_rejected(self):
        def other_case(doc):
            diagnostic = doc["attempt_failures"][0]
            diagnostic["case_id"] = next(case for case in doc["artifact"]["denominator_plan"]["case_ids"]
                                         if case != diagnostic["case_id"])
        for mode in ("cross_case_handle", "missing_usage"):
            with self.subTest(mode=mode):
                self._assert_rehashed_diagnostic_rejected(other_case, "failed_archive_attempt_binding", mode=mode)

    def test_failed_diagnostic_wrong_terminal_count_is_rejected(self):
        for mode, actual, wrong in (("cross_case_handle", 0, 1), ("cross_case_handle_after_one", 1, 0)):
            with self.subTest(mode=mode):
                def change(doc):
                    self.assertEqual(doc["attempt_failures"][0]["persisted_terminal_count"], actual)
                    doc["attempt_failures"][0]["persisted_terminal_count"] = wrong
                self._assert_rehashed_diagnostic_rejected(change, "failed_archive_terminal_count", mode=mode)

    def test_failed_diagnostic_wrong_attempt_or_start_hash_is_rejected(self):
        for key, value in (("attempt_index", 1), ("request_start_event_hash", "a" * 64)):
            with self.subTest(key=key):
                self._assert_rehashed_diagnostic_rejected(
                    lambda doc: doc["attempt_failures"][0].__setitem__(key, value), "failed_archive_attempt_binding")
        self._assert_rehashed_diagnostic_rejected(
            lambda doc: doc.__setitem__("schema_version", "item6-unfinished-failure/1.0"), "failed_archive_state")

    def test_failed_diagnostic_binds_second_request_and_nonzero_terminals(self):
        result = f.unfinished_case("cross_case_handle_after_one")
        self.assertEqual(result["process_exit_code"], 2)
        self.assertEqual(len(result["calls"]), 2)
        artifact = result["artifact"]
        records = result["attempt_failures"]
        self.assertEqual(len(records), 1)
        row = records[0]
        starts = [e for e in artifact["audit"]["events"] if e["event_type"] == "model_request_started"]
        terminals = [e for e in artifact["audit"]["events"] if "terminal_kind" in e["safe_payload"]]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(terminals), 1)
        self.assertEqual(row["attempt_index"], 1)
        self.assertEqual(row["request_start_event_hash"], starts[-1]["event_hash"])
        self.assertEqual(row["case_id"], starts[-1]["safe_payload"]["case_id"])
        self.assertEqual(row["persisted_terminal_count"], 1)
        self.assertEqual(row["code"], "audit_completion_attempt_handle_invalid")
        self.assertEqual(len(artifact["phase"]["attempts"]), 1)
        self.assertIsNotNone(artifact["phase"]["active_attempt"])
        self.assertFalse(artifact["phase"]["closed"])
        self.assertEqual([e["safe_payload"]["attempt_index"] for e in terminals], [0])
        for path in ("agent", "fixed_workflow"):
            self.assertEqual(len(artifact["business"][path]), 16)
            self.assertEqual(artifact["scores"][path]["raw_report"]["counts"]["planned"], 16)
        checked = f.verify_unfinished(result)
        self.assertTrue(checked["failure_archive_verified"])
        self.assertFalse(checked["execution_completed"])

    def test_failed_evidence_is_distinct_and_rejects_tamper_or_completion_claims(self):
        from researchops_external_closure.errors import ExternalClosurePrimitiveError
        from researchops_item6_experiment_v1.failed_archive import verify_failed_archive
        from researchops_item6_experiment_v1.observations import verify_archive
        result = f.unfinished_case("cross_case_handle")
        directory = Path(result["fixture_root"]) / result["run"]["archive_namespace"]
        receipt_hash = result["run"]["failure_receipt_sha256"]
        self.assertFalse((directory / "sealed.json").exists())
        self.assertFalse((directory / "finalization.json").exists())
        with self.assertRaises(ExternalClosurePrimitiveError) as rejected:
            verify_archive(directory, expected_seal_sha256=receipt_hash, expected_finalization_sha256=receipt_hash)
        self.assertEqual(rejected.exception.code, "external_closure_file_read_failed")
        with self.assertRaisesRegex(c.ExperimentError, "failed_archive_receipt"):
            verify_failed_archive(directory, expected_failure_sha256="a" * 64)
        # All mutation is in new disposable copies, never in the original evidence.
        for mode in ("hash", "completed", "closed", "diagnostic", "extra", "missing"):
            with self.subTest(mode=mode):
                clone = f.allocate("item6-failed-readback-")
                for name in ("failed-experiment.json", "audit.sqlite3", "failure-receipt.json"):
                    shutil.copyfile(directory / name, clone / name)
                payload = c.decode((clone / "failed-experiment.json").read_bytes(), c.policy()["archive_bytes"])
                if mode == "completed": payload["artifact"]["status"] = "completed"
                elif mode == "closed": payload["artifact"]["phase"]["closed"] = True
                elif mode == "diagnostic": payload["attempt_failures"][0]["code"] = "unrecognized_code"
                elif mode == "extra": (clone / "sealed.json").write_bytes(b"{}")
                elif mode == "missing": (clone / "failure-receipt.json").unlink()
                if mode not in {"extra", "missing"}:
                    blob = c.raw(payload) + (b" " if mode == "hash" else b"")
                    (clone / "failed-experiment.json").write_bytes(blob)
                    if mode != "hash":
                        receipt = c.decode((clone / "failure-receipt.json").read_bytes())
                        receipt["files"]["failed-experiment.json"] = dict(bytes=len(blob), sha256=c.digest(blob))
                        raw = c.raw(receipt); (clone / "failure-receipt.json").write_bytes(raw)
                        expected = c.digest(raw)
                    else: expected = receipt_hash
                else: expected = receipt_hash
                with self.assertRaises((c.ExperimentError, OSError)):
                    verify_failed_archive(clone, expected_failure_sha256=expected)

    def test_shared_clock_duplicate_terminal_remains_rejected(self):
        from researchops_completion_timing.clock import _TimingClock, TimingCaptureError
        clock = _TimingClock(request_timeout_ns=10**9, phase_timeout_ns=10**10)
        clock.task_released(); clock.key_loaded()
        handle = clock.begin_attempt()
        clock.transport_terminal(handle)
        clock.finish_attempt(handle, terminal_kind="no_response", raw_cleanup_status="not_applicable")
        before = clock.snapshot()
        with self.assertRaises(TimingCaptureError):
            clock.finish_attempt(handle, terminal_kind="no_response", raw_cleanup_status="not_applicable")
        after = clock.snapshot()
        self.assertEqual(before["attempts"], after["attempts"])
        self.assertIsNone(after["active_attempt"])
        self.assertEqual(len(after["attempts"]), 1)

    def test_finalization_monotonic_deadline_component(self):
        from researchops_item6_experiment_v1.finalization import FinalizationDeadline
        from unittest.mock import patch
        from types import SimpleNamespace
        from datetime import timedelta
        # Negative checkpoint unit test only: no owner/claim/authorization is manufactured.
        guard=object.__new__(FinalizationDeadline)
        guard.last_utc=c.now();guard.not_before=guard.last_utc-timedelta(seconds=1)
        guard.expires=guard.last_utc+timedelta(seconds=10);guard.deadline_ns=100
        with patch("researchops_item6_experiment_v1.finalization.time.monotonic_ns",return_value=100):
            with self.assertRaisesRegex(c.ExperimentError,"finalization_deadline"): guard.check("seal_after")

    def test_actual_finalization_expiry_never_returns_completed(self):
        for mode,stage,sealed in (("expiry_after_scoring","scoring_after",False),
                                   ("expiry_after_export","export_after",False),
                                   ("expiry_after_seal","seal_after",True),
                                   ("expiry_after_terminal","terminal_after_write",True)):
            with self.subTest(mode=mode):
                result=f.run_case(mode)
                _observe_finalization_expiry(result, mode, stage, sealed)
                self.assertEqual(result["process_exit_code"],2)
                self.assertIn(mode,result["hits"])
                self.assertIsNone(result["run"])
                self.assertEqual(result["error"],"item6_finalization_deadline")
                failure=result["finalization_failure"]
                self.assertEqual(failure["status"],"failed")
                self.assertEqual(failure["stage"],stage)
                if mode == "expiry_after_export":
                    fault = result["post_checks"]["finalization_export_fault"]
                    self.assertTrue(fault["original_exports_preserved"])
                    self.assertGreater(fault["nonmatching_exports"], 0)
                    self.assertEqual(fault["target_exports"], 1)
                    self.assertTrue(fault["expiry_injected"])
                    self.assertEqual(fault["matched_stage"], "scoring_after")
                self.assertEqual(failure["seal_present"],sealed)
                self.assertEqual(sum(len(v) for v in result["partial_observations"].values()),32)
                root=Path(result["fixture_root"])
                directory=root/result["failed_archive_namespace"]
                self.assertTrue((directory/"finalization-failure.json").is_file())
                if mode=="expiry_after_seal":
                    self.assertEqual(result["candidate_status"],"completed")
                    self.assertEqual(len(result["calls"]),30)
                process=subprocess.run([f.PYTHON,"-B","-m","researchops_item6_experiment_v1","verify",
                    "--archive",str(directory),"--seal-sha256","1"*64,"--finalization-sha256","1"*64],
                    cwd=root,env=f.environment(root),capture_output=True,text=True,encoding="utf-8",timeout=120)
                _observe_finalization_expiry(result, mode, stage, sealed, verifier=process)
                self.assertEqual(process.returncode,2)
                self.assertEqual(json.loads(process.stdout.splitlines()[-1])["error"],"item6_finalization_failed")

    def test_finalization_receipt_is_required_and_bound(self):
        normal=f.normal();root=Path(normal["fixture_root"])
        original=root/normal["run"]["archive_namespace"]
        target=original.parent/("terminal-tamper-"+uuid.uuid4().hex)
        shutil.copytree(original,target)
        doc=json.loads((target/"finalization.json").read_bytes());doc["status"]="failed"
        (target/"finalization.json").write_bytes(c.raw(doc))
        process=subprocess.run([f.PYTHON,"-B","-m","researchops_item6_experiment_v1","verify",
            "--archive",str(target),"--seal-sha256",normal["run"]["seal_sha256"],
            "--finalization-sha256",normal["run"]["finalization_sha256"]],
            cwd=root,env=f.environment(root),capture_output=True,text=True,encoding="utf-8",timeout=120)
        self.assertEqual(process.returncode,2)
        self.assertEqual(json.loads(process.stdout.splitlines()[-1])["error"],"item6_finalization_hash")

    def test_extra_archive_file_and_wrong_seal_are_rejected(self):
        normal = f.normal(); root = Path(normal["fixture_root"])
        original = root / normal["run"]["archive_namespace"]
        target = original.parent / ("extra-" + uuid.uuid4().hex)
        self.assertTrue(target.resolve().is_relative_to(root.resolve()))
        shutil.copytree(original, target)
        (target / "unexpected.txt").write_text("synthetic", encoding="utf-8")
        process = subprocess.run([f.PYTHON, "-B", "-m", "researchops_item6_experiment_v1", "verify",
            "--archive", str(target), "--seal-sha256", normal["run"]["seal_sha256"], "--finalization-sha256", normal["run"]["finalization_sha256"]], cwd=root,
            env=f.environment(root), capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout.splitlines()[-1])["error"], "item6_archive_unexpected_files")
        process = subprocess.run([f.PYTHON, "-B", "-m", "researchops_item6_experiment_v1", "verify",
            "--archive", str(original), "--seal-sha256", "1" * 64, "--finalization-sha256", normal["run"]["finalization_sha256"]], cwd=root,
            env=f.environment(root), capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout.splitlines()[-1])["error"], "item6_archive_seal")

    def test_business_field_size_limit_and_invalid_ancestor(self):
        from researchops_item6_experiment_v1.observations import safe_business, safe_directory
        from unittest.mock import patch
        with self.assertRaisesRegex(c.ExperimentError, "business_size"):
            safe_business("x" * c.policy()["business_field_bytes"])
        hits = []
        original = Path.is_junction
        def reparse(path):
            if path == f.ROOT.parent:
                hits.append("ancestor_reparse"); return True
            return original(path)
        with patch.object(Path, "is_junction", new=reparse), self.assertRaisesRegex(c.ExperimentError, "archive_path"):
            safe_directory(f.ROOT)
        self.assertEqual(hits, ["ancestor_reparse"])

    def test_null_empty_and_whitespace_are_distinct_actual_sdk_observations(self):
        for mode, text, state in (("null_text", None, "unobserved"), ("empty_text", "", "empty"), ("whitespace_text", " \t\n　", "whitespace")):
            with self.subTest(mode=mode):
                result = f.run_case(mode)
                self.assertEqual(result["process_exit_code"], 2)
                row = result["artifact"]["business"]["agent"]["IC-01"]
                self.assertEqual(row["final_output"], text)
                self.assertEqual(row["text_observation"], state)
                self.assertIn(mode, result["hits"])
                self.assertEqual(result["artifact"]["scores"]["agent"]["raw_report"]["counts"]["planned"], 16)

    def test_paths_email_headers_and_traceback_are_rejected_before_body_storage(self):
        for mode in ("path_text", "email_text", "traceback_text", "authorization_text"):
            with self.subTest(mode=mode):
                result = f.run_case(mode)
                self.assertEqual(result["process_exit_code"], 2)
                self.assertIsNone(result["artifact"]["business"]["agent"]["IC-01"]["final_output"])
                self.assertIn(mode, result["hits"])

    def test_privacy_sentinel_is_not_persisted(self):
        result = f.run_case("privacy")
        self.assertEqual(result["process_exit_code"], 2)
        self.assertNotIn("offline-fixture-key-item6", json.dumps(result))
        self.assertIn("privacy", result["hits"])

    def test_semantic_tamper_with_recomputed_outer_hash_is_rejected(self):
        normal = f.normal(); root = Path(normal["fixture_root"])
        original = root / normal["run"]["archive_namespace"]
        for fault in ("budget", "answer", "dispatch_timing", "tool_arguments", "tool_status"):
            target = original.parent / ("tamper-" + uuid.uuid4().hex)
            self.assertTrue(target.resolve().is_relative_to(root.resolve()))
            shutil.copytree(original, target)
            doc = json.loads((target / "experiment.json").read_bytes())
            if fault == "budget": doc["budget"]["known_cost_subtotal"] = "999"
            elif fault == "answer": doc["business"]["agent"]["IC-01"]["final_output"] = "Spruce的质量为999 mg [E1]。"
            elif fault == "dispatch_timing":
                doc["budget"]["requests"][0]["dispatches"] = 0; doc["dispatches"] -= 1
            elif fault == "tool_arguments": doc["business"]["agent"]["IC-01"]["events"][0]["arguments"]["bundle_id"] = "aggregate-02"
            else: doc["business"]["agent"]["IC-01"]["events"][0]["status"] = "failed"
            payload = c.raw(doc); (target / "experiment.json").write_bytes(payload)
            seal = json.loads((target / "sealed.json").read_bytes())
            seal["files"]["experiment.json"] = dict(bytes=len(payload), sha256=c.digest(payload))
            sealed = c.raw(seal); (target / "sealed.json").write_bytes(sealed)
            process = subprocess.run([f.PYTHON, "-B", "-m", "researchops_item6_experiment_v1", "verify",
                "--archive", str(target), "--seal-sha256", c.digest(sealed), "--finalization-sha256", normal["run"]["finalization_sha256"]], cwd=root,
                env=f.environment(root), capture_output=True, text=True, encoding="utf-8", timeout=120)
            result = json.loads(process.stdout.splitlines()[-1])
            self.assertEqual(process.returncode, 2)
            expected = {"budget": "item6_budget_readback", "answer": "item6_score_readback",
                        "dispatch_timing": "item6_dispatch_timing_mismatch", "tool_arguments": "item6_tool_event_binding",
                        "tool_status": "item6_tool_event_binding"}
            self.assertEqual(result["error"], expected[fault])

    def test_archive_directory_enumeration_is_bounded_and_closed(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from researchops_item6_experiment_v1 import observations
        rows = []
        for expected in ({"experiment.json", "audit.sqlite3"}, {"experiment.json", "audit.sqlite3", "sealed.json"}, {"experiment.json", "audit.sqlite3", "sealed.json", "finalization.json"}):
            class Entries:
                def __init__(self): self.count = 0; self.closed = False
                def __enter__(self): return self
                def __exit__(self, *args): self.closed = True
                def __iter__(self):
                    for name in sorted(expected) + ["unexpected.txt"]:
                        self.count += 1
                        yield SimpleNamespace(name=name)
                    raise AssertionError("archive enumeration continued past the rejection boundary")
            entries = Entries()
            with patch.object(observations.os, "scandir", return_value=entries):
                with self.assertRaisesRegex(c.ExperimentError, "item6_archive_unexpected_files"):
                    observations._check_archive_entries(Path("unused-negative-fixture"), expected)
            self.assertEqual(entries.count, len(expected) + 1)
            self.assertTrue(entries.closed)
            rows.append(dict(allowed_entries=len(expected), entries_read=entries.count, closed=True))
        f.RESULTS.append(dict(kind="archive_enumeration_boundaries", rows=rows, injected_iterator_hit=True))

    def test_partial_body_and_seal_writes_are_not_complete_archives(self):
        from unittest.mock import patch
        from researchops_item6_experiment_v1.observations import write_archive
        normal = f.normal()
        original = Path(normal["fixture_root"]) / normal["run"]["archive_namespace"]
        actual_open = Path.open
        checks = []
        for name in ("experiment.json", "sealed.json"):
            directory = f.allocate("i6-partial-archive-")
            shutil.copyfile(original / "audit.sqlite3", directory / "audit.sqlite3")
            hits = []
            class PartialWrite:
                def __init__(self, stream): self.stream = stream
                def __enter__(self): self.stream.__enter__(); return self
                def __exit__(self, *args): return self.stream.__exit__(*args)
                def write(self, data):
                    self.stream.write(data[:8])
                    hits.append(name)
                    raise OSError("synthetic partial archive write")
            def open_with_failure(path, *args, **kwargs):
                stream = actual_open(path, *args, **kwargs)
                return PartialWrite(stream) if path == directory / name and args and args[0] == "xb" else stream
            with patch.object(Path, "open", new=open_with_failure), self.assertRaises(OSError):
                write_archive(directory, normal["artifact"])
            self.assertEqual(hits, [name])
            self.assertEqual((directory / name).stat().st_size, 8)
            if name == "experiment.json": self.assertFalse((directory / "sealed.json").exists())
            else: self.assertNotEqual((directory / name).read_bytes(), (original / name).read_bytes())
            checks.append(dict(failed_file=name, hits=hits, partial_bytes=8, complete_seal=False, directory=str(directory)))
        f.RESULTS.append(dict(kind="partial_archive_writes", checks=checks, provider_calls=0, real_store_touched=False))


class CaseIsolationArtifactTamperTests(unittest.TestCase):
    """Mutate isolated copies, rebuild cryptographic envelopes, then demand semantics."""

    @staticmethod
    def _replace_refs(value, replacements):
        if type(value) is str:
            seen = set()
            while value in replacements and value not in seen:
                seen.add(value)
                value = replacements[value]
            return value
        if type(value) is list:
            return [CaseIsolationArtifactTamperTests._replace_refs(item, replacements) for item in value]
        if type(value) is dict:
            return {key: CaseIsolationArtifactTamperTests._replace_refs(item, replacements)
                    for key, item in value.items()}
        return value

    def _rebuild_archive(self, directory, document, fixture_root):
        from researchops.audit import ZERO_HASH, _event_hash, canonical_json, verify_audit_chain_rows
        from researchops_item6_experiment_v1 import case_isolation as isolation
        replacements = {}
        events = document["audit"]["events"]
        records = {row["run_id"]: row for group in document["business"].values() for row in group.values()}
        previous = ZERO_HASH
        for sequence, event in enumerate(events, 1):
            old_hash = event["event_hash"]
            payload = self._replace_refs(event["safe_payload"], replacements)
            record = records.get(payload.get("case_run_id"))
            if event["event_type"] == isolation.REJECT:
                initial_keys = ("schema_version", "code", "reason_kind", "plan_index", "tool",
                                "arguments_sha256", "native_call_id_sha256", "response_links")
                original_id = payload["rejection_id"]
                identity = isolation.common(document["authority"]["freeze"], document["run_id"], record)
                changed_id = c.commit("case-rejection-v1", {**identity, **{key: payload[key] for key in initial_keys}})
                replacements[original_id] = changed_id
                payload["rejection_id"] = changed_id
            if event["event_type"] == isolation.CLOSE:
                # This projection deliberately excludes event/self references.
                payload["observation_sha256"] = c.digest(c.raw(isolation.projection(record)))
                value = payload["reconciliation"]
                payload["reconciliation_sha256"] = c.digest(c.raw(value)) if value is not None else None
            event.update(event_id=sequence, sequence=sequence, safe_payload=payload, prev_hash=previous)
            event["event_hash"] = _event_hash(run_id=event["run_id"], sequence=sequence,
                event_type=event["event_type"], occurred_at_utc=event["occurred_at_utc"],
                actor_kind=event["actor_kind"], safe_payload_json=canonical_json(payload), prev_hash=previous)
            replacements[old_hash] = event["event_hash"]
            previous = event["event_hash"]
        document = self._replace_refs(document, replacements)
        events = document["audit"]["events"]
        rows = [{**{key: value for key, value in event.items() if key != "safe_payload"},
                 "safe_payload_json": canonical_json(event["safe_payload"])} for event in events]
        verification = verify_audit_chain_rows(document["run_id"], rows)
        self.assertTrue(verification.valid, verification.error_code)
        document["audit"]["chain_verification"] = verification.to_dict()

        # Preserve the unmodified copied DB, then construct another DB from its
        # schema/data. Never disable append-only triggers on a production DB or
        # bypass them with DELETE/UPDATE. Install the identical triggers last,
        # as during initial database creation, and prove they still reject writes.
        columns = ("event_id", "run_id", "sequence", "event_type", "occurred_at_utc",
                   "actor_kind", "safe_payload_json", "prev_hash", "event_hash")
        database = directory / "audit.sqlite3"
        self.assertTrue(directory.resolve().is_relative_to(fixture_root.resolve()))
        self.assertTrue(directory.name.startswith("isolation-tamper-"))
        self.assertFalse(database.is_symlink() or database.is_junction())
        # A read-only WAL database may create -shm/-wal beside itself. Keep
        # that reader and all of its sidecars outside the four-file archive.
        original_bytes = database.read_bytes()
        original_hash = c.digest(original_bytes)
        read_stage = directory.parent / ("sqlite-read-stage-" + directory.name)
        self.assertTrue(read_stage.resolve().is_relative_to(fixture_root.resolve()))
        read_stage.mkdir(exist_ok=False)
        staged_database = read_stage / "audit.sqlite3"
        with staged_database.open("xb") as stream:
            stream.write(original_bytes)
        self.assertEqual(c.digest(staged_database.read_bytes()), original_hash)
        schema_query = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
        with closing(sqlite3.connect(staged_database.as_uri() + "?mode=ro", uri=True)) as source:
            source_schema = source.execute(schema_query).fetchall()
            dump = list(source.iterdump())
        self.assertEqual(c.digest(staged_database.read_bytes()), original_hash)
        self.assertEqual(c.digest(database.read_bytes()), original_hash)
        preserved = directory.parent / (directory.name + "-original-audit.sqlite3")
        self.assertFalse(preserved.exists())
        self.assertTrue(preserved.resolve().is_relative_to(fixture_root.resolve()))
        database.rename(preserved)
        triggers = [line for line in dump if line.lstrip().upper().startswith("CREATE TRIGGER")]
        self.assertTrue(triggers)
        self.assertFalse(database.exists())
        with closing(sqlite3.connect(database)) as connection, connection:
            for line in dump:
                if line in {"BEGIN TRANSACTION;", "COMMIT;"} or line in triggers or line.startswith('INSERT INTO "audit_events"'):
                    continue
                connection.execute(line)
            connection.executemany(
                "INSERT INTO audit_events (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")",
                [tuple(row[column] for column in columns) for row in rows])
            for line in triggers: connection.execute(line)
            connection.commit()
            self.assertEqual(connection.execute(schema_query).fetchall(), source_schema)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "audit_events are append-only"):
                connection.execute("UPDATE audit_events SET event_hash=event_hash WHERE run_id=?", (document["run_id"],))
            connection.rollback()
            connection.row_factory = sqlite3.Row
            actual = [dict(row) for row in connection.execute(
                "SELECT * FROM audit_events WHERE run_id=? ORDER BY sequence", (document["run_id"],))]
        self.assertEqual(actual, rows)
        self.assertEqual(c.digest(preserved.read_bytes()), original_hash)

        body = c.raw(document)
        (directory / "experiment.json").write_bytes(body)
        receipt = c.decode((directory / "sealed.json").read_bytes())
        for name in ("experiment.json", "audit.sqlite3"):
            payload = (directory / name).read_bytes()
            receipt["files"][name] = dict(bytes=len(payload), sha256=c.digest(payload))
        seal = c.raw(receipt)
        (directory / "sealed.json").write_bytes(seal)
        terminal = c.decode((directory / "finalization.json").read_bytes())
        terminal["seal_sha256"] = c.digest(seal)
        finalization = c.raw(terminal)
        (directory / "finalization.json").write_bytes(finalization)
        self.assertEqual(terminal["seal_sha256"], c.digest((directory / "sealed.json").read_bytes()))
        for name, expected in receipt["files"].items():
            payload = (directory / name).read_bytes()
            self.assertEqual(expected, dict(bytes=len(payload), sha256=c.digest(payload)))
        archive_names = {path.name for path in directory.iterdir()}
        self.assertEqual(archive_names, {"experiment.json", "audit.sqlite3", "sealed.json", "finalization.json"})
        f.RESULTS.append(dict(kind="rehashed_sqlite_preparation", reader_outside_archive=True,
            archive_files=sorted(archive_names), reader_files=sorted(path.name for path in read_stage.iterdir()),
            original_database_sha256=original_hash, reader_database_sha256=c.digest(staged_database.read_bytes()),
            preserved_database_sha256=c.digest(preserved.read_bytes()), append_only_trigger_rejection_verified=True,
            auxiliary_files_retained=True, provider_calls=0))
        return document, c.digest(seal), c.digest(finalization), len(rows)

    @staticmethod
    def _new_event(template, event_type, payload):
        result = deepcopy(template)
        result.update(event_type=event_type, actor_kind="system", safe_payload=payload,
                      event_hash=c.digest(uuid.uuid4().hex.encode()))
        return result

    def _mutate(self, document, fault):
        from researchops_item6_experiment_v1 import case_isolation as isolation
        events = document["audit"]["events"]
        if fault == "identity":
            return dict(fault=fault, hit_count=1)
        if fault == "post_stop_start":
            self.assertEqual(document["status"], "failed")
            candidate = next(item for item in document["authority"]["freeze"]["business_plan"]
                             if document["business"][item["path_kind"]][item["task_id"]]["execution_state"] == "not_executed")
            row = document["business"][candidate["path_kind"]][candidate["task_id"]]
            self.assertIsNone(row["case_lifecycle"]["started_event_hash"])
            self.assertFalse(any(event["event_type"] == isolation.START
                                 and event["safe_payload"].get("case_run_id") == row["run_id"] for event in events))
            self.assertTrue(any(event["event_type"] == isolation.SEALED for event in events))
            events.append(self._new_event(events[-1], isolation.START,
                isolation.common(document["authority"]["freeze"], document["run_id"], row)))
            return dict(fault=fault, hit_count=1, task_id=row["task_id"], execution_state="not_executed")
        rejected = next(row for row in document["business"]["agent"].values()
                        if row["case_rejection"] is not None and row["case_rejection"]["state"] == "isolated")
        case_id = rejected["run_id"]
        rejection = rejected["case_rejection"]
        if fault == "audit_tools":
            self.assertEqual(rejected["events"], [])
            self.assertEqual(document["budget"]["tools"].get(rejected["task_id"] + ":agent", 0), 0)
            close_index = next(i for i, event in enumerate(events)
                               if event["event_type"] == isolation.CLOSE and event["safe_payload"].get("case_run_id") == case_id)
            common = dict(mode=document["mode"], case_run_id=case_id, call_id="TAMPER-C1", tool="inspect_sources")
            start = self._new_event(events[close_index], "item6_tool_started_v1",
                                   {**common, "arguments_sha256": rejection["arguments_sha256"]})
            finish = self._new_event(events[close_index], "item6_tool_finished_v1",
                                    {**common, "status": "succeeded", "result_sha256": c.digest(c.raw({})),
                                     "produced_artifacts": []})
            events[close_index:close_index] = [start, finish]
            return dict(fault=fault, hit_count=2, business_events=0, budget_tools=0)
        if fault == "close_before_terminal":
            row = next(row for row in document["business"]["agent"].values()
                       if row["case_rejection"] is None and row["case_lifecycle"]["closed_event_hash"] is not None)
            handle = document["authority"]["freeze"]["model_case_handles"][row["task_id"]]
            terminal = next(event for event in reversed(events)
                            if "terminal_kind" in event["safe_payload"] and event["safe_payload"].get("case_id") == handle)
            closed = next(event for event in events if event["event_hash"] == row["case_lifecycle"]["closed_event_hash"])
            events.remove(closed)
            # Match a legitimate timestamp; the rejection must concern semantic order.
            closed["occurred_at_utc"] = terminal["occurred_at_utc"]
            events.insert(events.index(terminal), closed)
            return dict(fault=fault, hit_count=1, task_id=row["task_id"])
        if fault == "response_link":
            changed_index = (rejection["response_links"][0]["response_index"] + 1) % 48
            rejected_event = next(event for event in events if event["event_hash"] == rejection["rejected_event_hash"])
            rejection["response_links"][0]["response_index"] = changed_index
            rejected_event["safe_payload"]["response_links"][0]["response_index"] = changed_index
            return dict(fault=fault, hit_count=1, changed_response_index=changed_index)
        if fault == "missing_seal":
            seal = next(event for event in events if event["event_hash"] == rejection["sealed_event_hash"])
            events.remove(seal)
            rejection.update(state="rejected", sealed_event_hash=None)
            # Make all non-cryptographic summary fields internally consistent so
            # the exact missing lifecycle closure is the reason for rejection.
            document.update(isolation.result_fields(document["business"], document["scores"], document["authority"]["stopped"]))
            return dict(fault=fault, hit_count=1, deleted_seal=True)
        if fault == "collection_all_pass":
            self.assertGreater(document["collection_summary"]["failed_after_start"], 0)
            self.assertFalse(document["all_business_passed"])
            document["collection_summary"].update(operational_complete=32, failed_after_start=0, isolated_case_rejections=0)
            document["collection_status"] = "complete"
            document["all_business_passed"] = True
            return dict(fault=fault, hit_count=1, forged_all_business_passed=True)
        self.fail("unsupported bounded artifact mutation")

    def _assert_rehashed_isolation(self, fault, expected_code=None):
        mode = "isolation_audit_after_seal" if fault == "post_stop_start" else "isolation_design"
        original_result = f.isolation_case(mode)
        self.assertEqual(original_result["process_exit_code"], 2 if fault == "post_stop_start" else 3)
        root = Path(original_result["fixture_root"])
        original = root / original_result["run"]["archive_namespace"]
        names = ("experiment.json", "audit.sqlite3", "sealed.json", "finalization.json")
        before = {name: c.digest((original / name).read_bytes()) for name in names}
        target = original.parent / ("isolation-tamper-" + uuid.uuid4().hex)
        self.assertTrue(target.resolve().is_relative_to(root.resolve()))
        shutil.copytree(original, target)
        document = c.decode((target / "experiment.json").read_bytes(), c.policy()["archive_bytes"])
        mutation = self._mutate(document, fault)
        self.assertGreater(mutation["hit_count"], 0)
        document, seal_sha256, final_sha256, event_count = self._rebuild_archive(target, document, root)
        process = subprocess.run([f.PYTHON, "-B", "-m", "researchops_item6_experiment_v1", "verify",
            "--archive", str(target), "--seal-sha256", seal_sha256, "--finalization-sha256", final_sha256],
            cwd=root, env=f.environment(root), capture_output=True, text=True, encoding="utf-8", timeout=120)
        result = json.loads(process.stdout.splitlines()[-1])
        if expected_code is None:
            self.assertEqual(process.returncode, 0, result.get("error"))
            self.assertTrue(result["archive_verified"])
            self.assertEqual(result["collection_status"], "complete_with_case_rejections")
            self.assertFalse(result["all_business_passed"])
        else:
            self.assertEqual(process.returncode, 2)
            self.assertEqual(result["error"], "item6_" + expected_code)
        self.assertEqual(before, {name: c.digest((original / name).read_bytes()) for name in names})
        f.RESULTS.append(dict(kind="rehashed_case_isolation_semantic_check", fault=fault, mode=mode,
            mutation=mutation, actual_exit_code=process.returncode,
            expected_code=None if expected_code is None else "item6_" + expected_code,
            actual_code=result.get("error"), audit_chain_valid=True, audit_sqlite_projection_matched=True,
            event_count=event_count, seal_sha256=seal_sha256, finalization_sha256=final_sha256,
            original_archive_unchanged=True, provider_calls=0, real_store_touched=False))

    def test_fully_rehashed_isolation_archive_without_mutation_still_verifies(self):
        self._assert_rehashed_isolation("identity")

    def test_isolated_case_rejects_extra_audit_tool_events_after_full_rehash(self):
        self._assert_rehashed_isolation("audit_tools", "isolation_audit_tool_executed")

    def test_agent_close_before_response_terminal_rejected_after_full_rehash(self):
        self._assert_rehashed_isolation("close_before_terminal", "isolation_case_close")

    def test_not_executed_suffix_start_after_stop_rejected_after_full_rehash(self):
        self._assert_rehashed_isolation("post_stop_start", "isolation_events_after_stop")

    def test_wrong_response_link_rejected_with_coherent_rejection_identity(self):
        self._assert_rehashed_isolation("response_link", "isolation_response_links")

    def test_missing_isolation_seal_rejected_after_full_rehash(self):
        self._assert_rehashed_isolation("missing_seal", "isolation_unsealed")

    def test_collection_all_pass_forgery_rejected_after_full_rehash(self):
        self._assert_rehashed_isolation("collection_all_pass", "isolation_collection_projection")


class FinalizationExportInjectionUnitTests(unittest.TestCase):
    def frame_fixture(self):
        from types import SimpleNamespace
        from researchops_item6_experiment_v1 import runner, finalization
        ledger = object()
        owner = SimpleNamespace(run_id="synthetic-finalization-run")
        factory = SimpleNamespace(ledger=ledger, owner=owner)
        # Pure selector model only, not an Owner or a live admission proof.
        deadline = object.__new__(finalization.FinalizationDeadline)
        deadline.owner, deadline.stage = owner, "scoring_after"
        frame = SimpleNamespace(f_code=runner._run_owned.__code__, f_globals=runner._run_owned.__globals__,
            f_locals=dict(deadline=deadline, owner=owner, factory=factory, ledger=ledger))
        return frame, ledger, (owner.run_id,), runner._run_owned, finalization.FinalizationDeadline

    def matches(self, values, args=None, kw=None):
        frame, ledger, original_args, run, deadline_type = values
        return f.finalization_export_matches(frame, ledger, original_args if args is None else args,
            {} if kw is None else kw, run, deadline_type)

    def test_exact_finalization_identity_matches(self):
        self.assertTrue(self.matches(self.frame_fixture()))

    def test_business_export_caller_and_earlier_stage_do_not_match(self):
        from researchops_item6_experiment_v1 import case_isolation, runner
        values = self.frame_fixture()
        values[0].f_code = case_isolation._append.__code__
        self.assertFalse(self.matches(values))
        values = self.frame_fixture()
        values[0].f_code = runner._run.__code__
        self.assertFalse(self.matches(values))
        values = self.frame_fixture()
        values[0].f_locals["deadline"].stage = "start"
        self.assertFalse(self.matches(values))

    def test_foreign_globals_or_deadline_type_do_not_match(self):
        from types import SimpleNamespace
        values = self.frame_fixture()
        values[0].f_globals = {}
        self.assertFalse(self.matches(values))
        values = self.frame_fixture()
        values[0].f_locals["deadline"] = SimpleNamespace(stage="scoring_after", owner=values[0].f_locals["owner"])
        self.assertFalse(self.matches(values))

    def test_wrong_ledger_run_or_arguments_do_not_match(self):
        values = self.frame_fixture()
        values[0].f_locals["ledger"] = object()
        self.assertFalse(self.matches(values))
        self.assertFalse(self.matches(self.frame_fixture(), args=("foreign-run",)))
        self.assertFalse(self.matches(self.frame_fixture(), args=()))
        self.assertFalse(self.matches(self.frame_fixture(), kw={"extra": True}))

    def test_foreign_owner_or_factory_does_not_match(self):
        from types import SimpleNamespace
        values = self.frame_fixture()
        values[0].f_locals["owner"] = SimpleNamespace(run_id=values[2][0])
        self.assertFalse(self.matches(values))
        values = self.frame_fixture()
        values[0].f_locals["factory"].ledger = object()
        self.assertFalse(self.matches(values))
        values = self.frame_fixture()
        values[0].f_locals["factory"].owner = object()
        self.assertFalse(self.matches(values))

    def test_earlier_export_keeps_original_call_and_return_without_expiry(self):
        values = self.frame_fixture()
        calls, expired = [], []
        document = object()
        def original(ledger, *args, **kw):
            calls.append((ledger, args, kw))
            return document
        observation = dict(target_exports=0, nonmatching_exports=0, expiry_injected=False, matched_stage=None)
        wrapped = f.finalization_export_expiry(original, lambda: expired.append(True), values[3], values[4], observation)
        self.assertIs(wrapped(values[1], *values[2]), document)
        self.assertEqual(calls, [(values[1], values[2], {})])
        self.assertEqual(expired, [])
        self.assertEqual(observation["nonmatching_exports"], 1)
        self.assertEqual(observation["target_exports"], 0)
        self.assertFalse(observation["expiry_injected"])
