from pathlib import Path
import json
import shutil
import subprocess
import unittest
import uuid
from researchops_item6_experiment_v1 import contract as c
from . import item6_experiment_fixture as f


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
                self.assertEqual(result["process_exit_code"],2)
                self.assertIn(mode,result["hits"])
                self.assertIsNone(result["run"])
                self.assertEqual(result["error"],"item6_finalization_deadline")
                failure=result["finalization_failure"]
                self.assertEqual(failure["status"],"failed")
                self.assertEqual(failure["stage"],stage)
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
