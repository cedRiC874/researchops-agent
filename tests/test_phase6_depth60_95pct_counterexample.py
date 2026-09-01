from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import unicodedata
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_phase6_depth60_95pct_counterexample.py"
SPEC = importlib.util.spec_from_file_location(
    "verify_phase6_depth60_95pct_counterexample", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
counterexample = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = counterexample
SPEC.loader.exec_module(counterexample)


EXPECTED_TASK_IDS = [
    "P6-DEV-026",
    "P6-DEV-028",
    "P6-DEV-030",
    "P6-DEV-032",
    "P6-DEV-044",
]
EXPECTED_LINES = [26, 28, 30, 32, 44]
EXPECTED_PUBLISHED_ARTIFACT_SHA256 = (
    "36cad26b69002e9d55aa9d086e7b1b2d2fefa4893ccf18dbf6c99a1f91b278fc"
)
CURRENT_PYTHON_PATCH = (
    f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
)
CANONICAL_RUNTIME_AVAILABLE = (
    CURRENT_PYTHON_PATCH == counterexample.PINNED_PYTHON_PATCH
    and unicodedata.unidata_version
    == counterexample.PINNED_UNICODE_DATABASE_VERSION
)


@unittest.skipUnless(
    CANONICAL_RUNTIME_AVAILABLE,
    "counterexample replay is covered by the canonical Python 3.12.13 CI job",
)
class Phase6Depth6095PercentCounterexampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.proof = counterexample.build_phase6_depth60_95pct_counterexample(ROOT)

    def test_generator_is_deterministic_and_retracts_the_false_claims(self) -> None:
        self.assertEqual(
            self.proof,
            counterexample.build_phase6_depth60_95pct_counterexample(ROOT),
        )
        self.assertEqual(self.proof["status"], "counterexample_verified")
        self.assertEqual(
            self.proof["schema_version"],
            "phase6-depth60-95-percent-lexical-counterexample/1.1",
        )
        self.assertEqual(self.proof["verdict"], "satisfiable_counterexample")
        self.assertFalse(self.proof["formal_unsatisfiable_claim"])
        self.assertFalse(self.proof["strict_ceiling_55_claim"])
        self.assertTrue(self.proof["natural_format_conflict"])
        self.assertTrue(self.proof["lexical_escape_accepted"])
        self.assertTrue(self.proof["60_reachability_not_proven"])
        self.assertEqual(
            self.proof["runtime_binding"],
            {
                "python_requirement": "==3.12.13",
                "sys_version": sys.version,
                "exact_python_patch_pinned": True,
                "unicodedata_unidata_version": unicodedata.unidata_version,
                "unicode_database_version_pinned": True,
                "actual_scorer_replayed_on_current_runtime": True,
                "semantic_probes": {
                    "nfkc_fullwidth_95_percent": "95%",
                    "natural_number_matches": ["95"],
                    "lexical_escape_number_matches": [],
                },
            },
        )
        self.assertTrue(
            self.proof["claim_boundary"]
            ["counterexample_invalidates_five_task_basis_for_strict_55_ceiling"]
        )
        self.assertEqual(
            self.proof["proof_scope"],
            {
                "domain": "score_phase6_run input contract",
                "synthetic_scorer_domain_records": True,
                "provider_output_replayed": False,
                "online_runner_reachability_proven": False,
                "model_reachability_proven": False,
            },
        )

    def test_runtime_drift_fails_before_scorer_replay(self) -> None:
        cases = (
            ("sys_version", "3.12.12 (different patch)"),
            ("unicodedata_unidata_version", "0.0.0"),
            ("exact_python_patch_pinned", False),
            ("unicode_database_version_pinned", False),
        )
        for field, value in cases:
            with (
                self.subTest(field=field),
                tempfile.TemporaryDirectory() as directory,
            ):
                drifted = json.loads(json.dumps(self.proof))
                drifted["runtime_binding"][field] = value
                proof_path = Path(directory) / "proof.json"
                proof_path.write_bytes(counterexample._canonical_json_bytes(drifted))
                with patch.object(
                    counterexample,
                    "build_phase6_depth60_95pct_counterexample",
                    side_effect=AssertionError("scorer replay must not start"),
                ):
                    with self.assertRaisesRegex(
                        counterexample.CounterexampleProofError,
                        rf"\$\.runtime_binding\.{field}: value_mismatch",
                    ):
                        counterexample.verify_phase6_depth60_95pct_counterexample(
                            ROOT, proof_path
                        )

        for field, mutation in (
            ("sys_version", "missing"),
            ("unicodedata_unidata_version", "type"),
        ):
            with (
                self.subTest(field=field, mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                drifted = json.loads(json.dumps(self.proof))
                if mutation == "missing":
                    del drifted["runtime_binding"][field]
                    reason = "missing"
                else:
                    drifted["runtime_binding"][field] = 15
                    reason = "type_mismatch"
                proof_path = Path(directory) / "proof.json"
                proof_path.write_bytes(counterexample._canonical_json_bytes(drifted))
                with patch.object(
                    counterexample,
                    "build_phase6_depth60_95pct_counterexample",
                    side_effect=AssertionError("scorer replay must not start"),
                ):
                    with self.assertRaisesRegex(
                        counterexample.CounterexampleProofError,
                        rf"\$\.runtime_binding\.{field}: {reason}",
                    ):
                        counterexample.verify_phase6_depth60_95pct_counterexample(
                            ROOT, proof_path
                        )

    def test_same_patch_different_build_string_replays_deterministically(self) -> None:
        portable = json.loads(json.dumps(self.proof))
        portable["runtime_binding"]["sys_version"] = (
            "3.12.13 (different platform build provenance)"
        )
        with tempfile.TemporaryDirectory() as directory:
            proof_path = Path(directory) / "proof.json"
            proof_path.write_bytes(counterexample._canonical_json_bytes(portable))
            summary = counterexample.verify_phase6_depth60_95pct_counterexample(
                ROOT, proof_path
            )
        self.assertEqual(summary["status"], "valid")
        self.assertEqual(summary["mismatch_count"], 0)

    def test_exact_five_locked_tasks_and_complete_traces_are_exercised(self) -> None:
        tasks = self.proof["tasks"]
        self.assertEqual([task["task_id"] for task in tasks], EXPECTED_TASK_IDS)
        self.assertEqual([task["corpus_line"] for task in tasks], EXPECTED_LINES)
        for task in tasks:
            self.assertEqual(task["required_literal"], "95%")
            self.assertEqual(task["structured_required_phrases"], [])
            for witness_name in ("natural_witness", "lexical_escape_witness"):
                witness = task[witness_name]
                self.assertTrue(witness["synthetic_not_provider_output"])
                self.assertEqual(
                    witness["synthetic_output"].splitlines(),
                    witness["synthetic_output_lines"],
                )
                self.assertTrue(witness["synthetic_output_lines"])
                trace = witness["trace"]
                self.assertTrue(trace["synthetic_trace_not_provider_output"])
                self.assertEqual(trace["record_status"], "completed")
                self.assertTrue(trace["completion_integrity"])
                self.assertEqual(trace["approval_interruption_count"], 0)
                self.assertEqual(
                    len(trace["tool_calls"]), task["required_tool_call_count"]
                )
                self.assertEqual(
                    trace["tool_observation_count"], task["required_tool_call_count"]
                )
                self.assertEqual(
                    len(trace["tool_observations"]), task["required_tool_call_count"]
                )
                self.assertEqual(
                    trace["numeric_claim_line_count"],
                    task["required_numeric_claim_count"],
                )
                self.assertEqual(
                    len(trace["numeric_claim_lines"]),
                    task["required_numeric_claim_count"],
                )
                self.assertTrue(trace["numeric_claim_lines_all_match_actual_grammar"])

    def test_natural_and_letter_prefixed_witnesses_use_the_actual_scorer(self) -> None:
        for task in self.proof["tasks"]:
            with self.subTest(task_id=task["task_id"]):
                natural = task["natural_witness"]
                self.assertTrue(natural["contains_required_literal"])
                self.assertEqual(
                    natural["number_in_percent_token_matches"],
                    [{"text": "95", "start": 0, "end": 2}],
                )
                self.assertFalse(natural["structured_assertions_match"])
                self.assertFalse(natural["score"]["checks"]["required_phrases"])
                self.assertFalse(natural["score"]["checks"]["guardrail"])
                self.assertFalse(natural["score"]["task_pass"])
                self.assertEqual(
                    natural["score"]["failure_reasons"], ["required_phrases"]
                )
                self.assertEqual(natural["rejection_reasons"], ["required_phrases"])
                self.assertEqual(
                    natural["structured_rejection_reasons"],
                    ["number_in_text_match_in_non_claim_prose"],
                )
                for name, passed in natural["score"]["checks"].items():
                    if name not in {"required_phrases", "guardrail"}:
                        self.assertTrue(passed, name)

                escaped = task["lexical_escape_witness"]
                self.assertTrue(escaped["contains_required_literal"])
                self.assertEqual(escaped["number_in_percent_token_matches"], [])
                self.assertTrue(escaped["structured_assertions_match"])
                self.assertTrue(escaped["score"]["checks"]["required_phrases"])
                self.assertTrue(escaped["score"]["task_pass"])
                self.assertEqual(escaped["score"]["failure_reasons"], [])
                self.assertEqual(escaped["rejection_reasons"], [])
                self.assertEqual(escaped["structured_rejection_reasons"], [])
                self.assertTrue(all(escaped["score"]["checks"].values()))

    def test_source_bytes_hashes_and_ast_lines_are_bound(self) -> None:
        commitments = self.proof["source_bindings"]["commitments"]
        expected = {
            "evals/phase6_agent_tasks.jsonl": (
                69499,
                "af945e5bd780b39e7639102dab75c446350a81e9e9464fe156e41d2a45cebc2c",
            ),
            "src/researchops/phase6_eval.py": (
                82883,
                "9b89bf65b59a25bcc27dc306f3134ac58e3c7e500877fa0e1205c10105bfe34c",
            ),
            "src/researchops/phase6_agent.py": (
                62587,
                "bdb938e1314dfc74201a0003b289daa4aa536abf673ff924f28d16a6469d9df1",
            ),
        }
        for relative, (size, digest) in expected.items():
            payload = (ROOT / relative).read_bytes()
            self.assertEqual(commitments[relative]["bytes"], size)
            self.assertEqual(commitments[relative]["bytes"], len(payload))
            self.assertEqual(commitments[relative]["sha256"], digest)
            self.assertEqual(
                commitments[relative]["sha256"], hashlib.sha256(payload).hexdigest()
            )
        verifier_payload = SCRIPT.read_bytes()
        self.assertEqual(
            commitments["scripts/verify_phase6_depth60_95pct_counterexample.py"],
            {
                "bytes": len(verifier_payload),
                "sha256": hashlib.sha256(verifier_payload).hexdigest(),
            },
        )

        source = (ROOT / "src/researchops/phase6_eval.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        top_level_lines: dict[str, int] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                top_level_lines[node.name] = node.lineno
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        top_level_lines[target.id] = node.lineno
        locations = self.proof["source_bindings"]["scorer_ast_symbols"]
        for symbol, location in locations.items():
            self.assertEqual(location["line"], top_level_lines[symbol])

    def test_emit_is_canonical_and_verify_compares_every_value(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(counterexample.main(["--emit"]), 0)
        emitted = stdout.getvalue().encode("utf-8")
        self.assertEqual(emitted, counterexample._canonical_json_bytes(self.proof))
        self.assertEqual(json.loads(emitted), self.proof)

        with tempfile.TemporaryDirectory() as directory:
            proof_path = Path(directory) / "proof.json"
            proof_path.write_bytes(emitted)
            summary = counterexample.verify_phase6_depth60_95pct_counterexample(
                ROOT, proof_path
            )
            self.assertEqual(summary["status"], "valid")
            self.assertEqual(summary["mismatch_count"], 0)
            self.assertGreater(summary["compared_value_count"], 100)

            for arguments in (
                ["--verify", "--proof", str(proof_path)],
                ["--proof", str(proof_path)],
            ):
                cli_stdout = io.StringIO()
                with contextlib.redirect_stdout(cli_stdout):
                    self.assertEqual(counterexample.main(arguments), 0)
                self.assertEqual(json.loads(cli_stdout.getvalue())["status"], "valid")

            drifted = json.loads(emitted)
            drifted["natural_format_conflict"] = False
            proof_path.write_bytes(counterexample._canonical_json_bytes(drifted))
            with self.assertRaises(counterexample.CounterexampleProofError) as caught:
                counterexample.verify_phase6_depth60_95pct_counterexample(
                    ROOT, proof_path
                )
            self.assertIn("$.natural_format_conflict", str(caught.exception))
            self.assertNotIn('"expected"', str(caught.exception))
            self.assertNotIn('"actual"', str(caught.exception))

    def test_published_artifact_default_path_is_verified(self) -> None:
        summary = counterexample.verify_phase6_depth60_95pct_counterexample(ROOT)
        self.assertEqual(summary["status"], "valid")
        self.assertEqual(summary["mismatch_count"], 0)
        self.assertEqual(
            summary["artifact_sha256"], EXPECTED_PUBLISHED_ARTIFACT_SHA256
        )

    def test_fresh_process_import_and_build_use_no_provider_or_model_runtime(self) -> None:
        bootstrap = f'''
import json, os, runpy, socket, sys
def is_provider_secret_name(key):
    upper = str(key).upper()
    return any(token in upper for token in ("API_KEY", "TOKEN", "SECRET", "AUTH", "PASSWORD"))
class ProviderSecretGuard(dict):
    def __getitem__(self, key):
        if is_provider_secret_name(key):
            raise AssertionError("provider secret environment value read: " + str(key))
        return super().__getitem__(key)
    def get(self, key, default=None):
        if is_provider_secret_name(key):
            raise AssertionError("provider secret environment value read: " + str(key))
        return super().get(key, default)
os.environ = ProviderSecretGuard(dict(os.environ))
os.getenv = lambda key, default=None: (_ for _ in ()).throw(AssertionError("provider secret getenv")) if is_provider_secret_name(key) else default
OriginalSocket = socket.socket
class ForbiddenNetworkSocket(OriginalSocket):
    def connect(self, *args, **kwargs):
        raise AssertionError("network connect")
    def connect_ex(self, *args, **kwargs):
        raise AssertionError("network connect_ex")
    def sendto(self, *args, **kwargs):
        raise AssertionError("network sendto")
socket.socket = ForbiddenNetworkSocket
socket.create_connection = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("connect"))
namespace = runpy.run_path({str(SCRIPT)!r}, run_name="counterexample_import_probe")
proof = namespace["build_phase6_depth60_95pct_counterexample"]({str(ROOT)!r})
if "agents" in sys.modules:
    raise AssertionError("model SDK was imported")
print(json.dumps({{"status": proof["status"], "task_count": proof["target_task_count"]}}))
'''
        with tempfile.TemporaryDirectory() as directory:
            child_env = {
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "MPLCONFIGDIR": directory,
                "HOME": directory,
                "TEMP": directory,
                "TMP": directory,
            }
            if os.name == "nt":
                child_env["WINDIR"] = os.environ.get("WINDIR", "C:\\Windows")
                child_env["SYSTEMROOT"] = os.environ.get(
                    "SYSTEMROOT", child_env["WINDIR"]
                )
            completed = subprocess.run(
                [sys.executable, "-B", "-c", bootstrap],
                cwd=ROOT,
                env=child_env,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {"status": "counterexample_verified", "task_count": 5},
        )

    def test_generator_has_zero_network_model_environment_or_key_access(self) -> None:
        class ForbiddenEnvironment(dict[str, str]):
            def __getitem__(self, key: str) -> str:
                raise AssertionError(f"environment read is forbidden: {key}")

            def get(self, key: str, default=None):
                del default
                raise AssertionError(f"environment read is forbidden: {key}")

        with patch.object(
            socket, "socket", side_effect=AssertionError("network is forbidden")
        ), patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network is forbidden"),
        ), patch.object(
            os, "getenv", side_effect=AssertionError("key read is forbidden")
        ), patch.object(
            os, "environ", ForbiddenEnvironment()
        ):
            proof = counterexample.build_phase6_depth60_95pct_counterexample(ROOT)
        self.assertEqual(
            proof["execution_boundary"],
            {
                "network_calls": 0,
                "model_calls": 0,
                "api_key_reads": 0,
                "provider_secret_environment_value_reads": 0,
            },
        )


class CounterexampleRuntimeRoutingTests(unittest.TestCase):
    def test_ci_routes_counterexample_replay_to_the_canonical_runtime(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn('python-version: "3.12.10"', workflow)
        self.assertIn('python-version: "3.12.13"', workflow)
        self.assertIn("depth60-counterexample-runtime-replay:", workflow)
        canonical_job = workflow.split(
            "depth60-counterexample-runtime-replay:", maxsplit=1
        )[1]
        self.assertIn("grep -v '^pywin32==' requirements.lock", canonical_job)
        self.assertIn(
            "python -m pip install -r /tmp/researchops-linux.lock",
            canonical_job,
        )
        self.assertNotIn("pip install -r requirements.lock", canonical_job)


if __name__ == "__main__":
    unittest.main()
