from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import researchops.phase6_eval as phase6_eval_module  # noqa: E402
from researchops.phase6_agent import (  # noqa: E402
    AgentRunRecord,
    AgentToolCall,
    AgentToolObservation,
    AgentUsage,
)
from researchops.phase6_eval import (  # noqa: E402
    _NUMBER_IN_TEXT,
    _STRUCTURED_CLAIM,
    _contains_phrase,
    _depth60_structured_required_phrases,
    _structured_assertions_match,
    load_phase6_tasks,
    score_phase6_run,
)


CORPUS_RELATIVE_PATH = Path("evals/phase6_agent_tasks.jsonl")
SCORER_RELATIVE_PATH = Path("src/researchops/phase6_eval.py")
AGENT_RELATIVE_PATH = Path("src/researchops/phase6_agent.py")
VERIFIER_RELATIVE_PATH = Path("scripts/verify_phase6_depth60_95pct_counterexample.py")
PROOF_RELATIVE_PATH = Path(
    "docs/evidence/phase6-deepseek-depth60-interpretation-audit-v1/"
    "95_percent_lexical_counterexample.json"
)
TARGET_TASK_IDS = (
    "P6-DEV-026",
    "P6-DEV-028",
    "P6-DEV-030",
    "P6-DEV-032",
    "P6-DEV-044",
)
PINNED_PYTHON_PATCH = "3.12.13"
PINNED_UNICODE_DATABASE_VERSION = "15.0.0"
REQUIRED_LITERAL = "95%"
LEXICAL_ESCAPE = "x95%"
AST_SYMBOLS = (
    "_NUMBER_IN_TEXT",
    "_STRUCTURED_CLAIM",
    "_contains_phrase",
    "_depth60_structured_required_phrases",
    "_structured_assertions_match",
    "load_phase6_tasks",
    "score_phase6_run",
)


class CounterexampleProofError(RuntimeError):
    """The deterministic counterexample could not be constructed or verified."""


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _source_commitment(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    return {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _ast_symbol_locations(source_path: Path) -> dict[str, dict[str, Any]]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    found: dict[str, dict[str, Any]] = {}
    for node in tree.body:
        names: list[tuple[str, str]] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append((node.name, type(node).__name__))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.append((target.id, type(node).__name__))
        for name, kind in names:
            if name in AST_SYMBOLS:
                found[name] = {
                    "kind": kind,
                    "line": node.lineno,
                    "end_line": node.end_lineno,
                }
    missing = sorted(set(AST_SYMBOLS) - set(found))
    if missing:
        raise CounterexampleProofError(f"missing scorer AST symbols: {missing}")
    return {name: found[name] for name in AST_SYMBOLS}


def _corpus_line_numbers(corpus_path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for line_number, line in enumerate(
        corpus_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        task_id = json.loads(line)["task_id"]
        if task_id in TARGET_TASK_IDS:
            result[task_id] = line_number
    if set(result) != set(TARGET_TASK_IDS):
        raise CounterexampleProofError("the five locked target rows were not found")
    return result


def _claim_line(claim: Any) -> str:
    return (
        f"[CLAIM metric={claim.metric_name} value={repr(claim.value)} "
        f"evidence_id={claim.evidence_id}]"
    )


def _build_output(task: Any, percent_token: str) -> tuple[str, tuple[str, ...]]:
    structured = _depth60_structured_required_phrases(
        task.task_id, task.expected.required_phrases
    )
    prose_phrases = [
        percent_token if phrase == REQUIRED_LITERAL else phrase
        for phrase in task.expected.required_phrases
        if phrase not in structured
    ]
    assertion_lines = [f"[ASSERT {phrase}]" for phrase in structured]
    claim_lines = [_claim_line(claim) for claim in task.expected.numeric_claims]
    output = "\n".join((*assertion_lines, *claim_lines, " ".join(prose_phrases)))
    return output, tuple(claim_lines)


def _build_record(task: Any, output: str) -> AgentRunRecord:
    calls: list[AgentToolCall] = []
    observations: list[AgentToolObservation] = []
    for index, expected_call in enumerate(task.expected.tool_arguments):
        call_id = f"counterexample-call-{index + 1}"
        calls.append(
            AgentToolCall(
                call_id=call_id,
                name=expected_call.tool_name,
                arguments=dict(expected_call.arguments),
                status="succeeded",
            )
        )
        evidence_ids = (
            tuple(task.expected.required_evidence_ids)
            if expected_call.tool_name == "read_aggregate_evidence"
            else ()
        )
        observations.append(
            AgentToolObservation(
                call_id=call_id,
                name=expected_call.tool_name,
                status="succeeded",
                evidence_ids=evidence_ids,
                error_code=None,
                output_sha256=hashlib.sha256(
                    f"{task.task_id}:{index}:{expected_call.tool_name}".encode("utf-8")
                ).hexdigest(),
            )
        )
    return AgentRunRecord(
        status="completed",
        model="deterministic-counterexample",
        final_output=output,
        tool_calls=tuple(calls),
        usage=AgentUsage(
            requests=1,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            cached_input_tokens=0,
            complete=True,
        ),
        latency_ms=0.0,
        cost_usd=0.0,
        approval_interruptions=(),
        tracing_disabled=True,
        tool_observations=tuple(observations),
        provider="offline-deterministic",
        transport="none",
        completion_integrity=True,
        completion_error_code=None,
    )


def _score_projection(score: Any) -> dict[str, Any]:
    serialized = score.to_dict()
    return {
        "task_pass": serialized["task_pass"],
        "failure_reasons": serialized["failure_reasons"],
        "checks": serialized["checks"],
        "counts": serialized["counts"],
        "privacy_canary_detected": serialized["privacy_canary_detected"],
    }


def _witness(task: Any, percent_token: str, kind: str) -> dict[str, Any]:
    output, claim_lines = _build_output(task, percent_token)
    record = _build_record(task, output)
    score = score_phase6_run(task, record, safety_violation=False)
    structured = _depth60_structured_required_phrases(
        task.task_id, task.expected.required_phrases
    )
    normalized_token = unicodedata.normalize("NFKC", percent_token).casefold()
    numeric_matches = [
        {
            "text": match.group(0),
            "start": match.start(),
            "end": match.end(),
        }
        for match in _NUMBER_IN_TEXT.finditer(normalized_token)
    ]
    score_projection = _score_projection(score)
    return {
        "kind": kind,
        "synthetic_not_provider_output": True,
        "synthetic_output": output,
        "synthetic_output_lines": output.splitlines(),
        "percent_token": percent_token,
        "normalized_percent_token": normalized_token,
        "contains_required_literal": _contains_phrase(output, REQUIRED_LITERAL),
        "number_in_percent_token_matches": numeric_matches,
        "structured_assertions_match": _structured_assertions_match(
            task.task_id,
            output,
            structured,
            task.expected.forbidden_assertions,
        ),
        "rejection_reasons": list(score_projection["failure_reasons"]),
        "structured_rejection_reasons": (
            ["number_in_text_match_in_non_claim_prose"] if numeric_matches else []
        ),
        "trace": {
            "synthetic_trace_not_provider_output": True,
            "record_status": record.status,
            "completion_integrity": record.completion_integrity,
            "approval_interruption_count": len(record.approval_interruptions),
            "usage": record.usage.to_dict(),
            "tool_calls": [
                {
                    "call_index": index,
                    "call_id": call.call_id,
                    "tool_name": call.name,
                    "status": call.status,
                    "arguments": dict(call.arguments),
                    "arguments_sha256": hashlib.sha256(
                        _canonical_json_bytes(call.arguments)
                    ).hexdigest(),
                }
                for index, call in enumerate(record.tool_calls)
            ],
            "tool_observations": [
                observation.to_dict() for observation in record.tool_observations
            ],
            "tool_observation_count": len(record.tool_observations),
            "numeric_claim_lines": list(claim_lines),
            "numeric_claim_line_count": len(claim_lines),
            "numeric_claim_lines_all_match_actual_grammar": all(
                _STRUCTURED_CLAIM.fullmatch(line) is not None for line in claim_lines
            ),
            "final_output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        },
        "score": score_projection,
    }


def build_phase6_depth60_95pct_counterexample(
    project_root: str | Path = PROJECT_ROOT,
    *,
    recorded_sys_version: str | None = None,
) -> dict[str, Any]:
    """Build the offline proof that the five contracts have a lexical escape."""

    _validate_current_runtime_compatibility()
    root = Path(project_root).resolve()
    corpus_path = root / CORPUS_RELATIVE_PATH
    scorer_path = root / SCORER_RELATIVE_PATH
    agent_path = root / AGENT_RELATIVE_PATH
    verifier_path = root / VERIFIER_RELATIVE_PATH
    imported_scorer_path = Path(phase6_eval_module.__file__).resolve()
    if imported_scorer_path != scorer_path.resolve():
        raise CounterexampleProofError(
            "imported scorer does not match bound source file"
        )
    tasks = {task.task_id: task for task in load_phase6_tasks(corpus_path)}
    lines = _corpus_line_numbers(corpus_path)
    selected: list[dict[str, Any]] = []
    for task_id in TARGET_TASK_IDS:
        task = tasks[task_id]
        structured = _depth60_structured_required_phrases(
            task.task_id, task.expected.required_phrases
        )
        if REQUIRED_LITERAL not in task.expected.required_phrases:
            raise CounterexampleProofError(f"{task_id} no longer requires 95%")
        natural = _witness(task, REQUIRED_LITERAL, "natural_prose_95_percent")
        escaped = _witness(task, LEXICAL_ESCAPE, "letter_prefixed_lexical_escape")
        if not (
            natural["contains_required_literal"]
            and natural["number_in_percent_token_matches"]
            and not natural["structured_assertions_match"]
            and natural["score"]["checks"]["required_phrases"] is False
            and natural["score"]["task_pass"] is False
        ):
            raise CounterexampleProofError(
                f"natural conflict was not reproduced: {task_id}"
            )
        if not (
            escaped["contains_required_literal"]
            and not escaped["number_in_percent_token_matches"]
            and escaped["structured_assertions_match"]
            and escaped["score"]["checks"]["required_phrases"]
            and escaped["score"]["task_pass"]
        ):
            raise CounterexampleProofError(
                f"lexical escape was not accepted: {task_id}"
            )
        selected.append(
            {
                "task_id": task_id,
                "corpus_line": lines[task_id],
                "required_literal": REQUIRED_LITERAL,
                "structured_required_phrases": list(structured),
                "required_numeric_claim_count": len(task.expected.numeric_claims),
                "required_tool_call_count": len(task.expected.tool_arguments),
                "natural_witness": natural,
                "lexical_escape_witness": escaped,
            }
        )

    source_commitments = {
        CORPUS_RELATIVE_PATH.as_posix(): _source_commitment(corpus_path),
        SCORER_RELATIVE_PATH.as_posix(): _source_commitment(scorer_path),
        AGENT_RELATIVE_PATH.as_posix(): _source_commitment(agent_path),
        VERIFIER_RELATIVE_PATH.as_posix(): _source_commitment(verifier_path),
    }
    return {
        "schema_version": "phase6-depth60-95-percent-lexical-counterexample/1.1",
        "status": "counterexample_verified",
        "verdict": "satisfiable_counterexample",
        "formal_unsatisfiable_claim": False,
        "strict_ceiling_55_claim": False,
        "natural_format_conflict": True,
        "lexical_escape_accepted": True,
        "60_reachability_not_proven": True,
        "target_task_ids": list(TARGET_TASK_IDS),
        "target_task_count": len(TARGET_TASK_IDS),
        "matcher_mechanism": {
            "required_phrase_normalization": "NFKC + casefold + whitespace collapse",
            "required_phrase_operation": "substring",
            "number_in_text_pattern": _NUMBER_IN_TEXT.pattern,
            "structured_claim_pattern": _STRUCTURED_CLAIM.pattern,
            "natural_token": {
                "value": REQUIRED_LITERAL,
                "contains_required_literal": _contains_phrase(
                    REQUIRED_LITERAL, REQUIRED_LITERAL
                ),
                "number_in_text_matches": [
                    match.group(0)
                    for match in _NUMBER_IN_TEXT.finditer(REQUIRED_LITERAL)
                ],
            },
            "lexical_escape_token": {
                "value": LEXICAL_ESCAPE,
                "contains_required_literal": _contains_phrase(
                    LEXICAL_ESCAPE, REQUIRED_LITERAL
                ),
                "number_in_text_matches": [
                    match.group(0) for match in _NUMBER_IN_TEXT.finditer(LEXICAL_ESCAPE)
                ],
                "boundary_explanation": (
                    "the ASCII letter immediately before 9 defeats the regex negative "
                    "lookbehind while substring matching still sees 95%"
                ),
            },
        },
        "source_bindings": {
            "commitments": source_commitments,
            "scorer_ast_symbols": _ast_symbol_locations(scorer_path),
            "imported_private_symbols_from_bound_scorer": True,
        },
        "runtime_binding": {
            "python_requirement": f"=={PINNED_PYTHON_PATCH}",
            "sys_version": recorded_sys_version or sys.version,
            "exact_python_patch_pinned": True,
            "unicodedata_unidata_version": PINNED_UNICODE_DATABASE_VERSION,
            "unicode_database_version_pinned": True,
            "actual_scorer_replayed_on_current_runtime": True,
            "semantic_probes": {
                "nfkc_fullwidth_95_percent": unicodedata.normalize(
                    "NFKC", "９５％"
                ),
                "natural_number_matches": [
                    match.group(0)
                    for match in _NUMBER_IN_TEXT.finditer(REQUIRED_LITERAL)
                ],
                "lexical_escape_number_matches": [
                    match.group(0)
                    for match in _NUMBER_IN_TEXT.finditer(LEXICAL_ESCAPE)
                ],
            },
        },
        "proof_scope": {
            "domain": "score_phase6_run input contract",
            "synthetic_scorer_domain_records": True,
            "provider_output_replayed": False,
            "online_runner_reachability_proven": False,
            "model_reachability_proven": False,
        },
        "tasks": selected,
        "claim_boundary": {
            "five_natural_renderings_fail_only_required_phrases": all(
                task["natural_witness"]["score"]["failure_reasons"]
                == ["required_phrases"]
                for task in selected
            ),
            "five_lexical_counterexamples_pass_full_task_contract": all(
                task["lexical_escape_witness"]["score"]["task_pass"]
                for task in selected
            ),
            "counterexample_refutes_formal_unsatisfiability": True,
            "counterexample_invalidates_five_task_basis_for_strict_55_ceiling": True,
            "counterexample_proves_all_60_tasks_reachable": False,
        },
        "execution_boundary": {
            "network_calls": 0,
            "model_calls": 0,
            "api_key_reads": 0,
            "provider_secret_environment_value_reads": 0,
        },
    }


# Short alias for callers that do not need the phase-qualified name.
build_counterexample = build_phase6_depth60_95pct_counterexample


def _python_patch_from_sys_version(value: str) -> str:
    patch = value.split(maxsplit=1)[0]
    components = patch.split(".")
    if len(components) != 3 or any(not item.isdigit() for item in components):
        raise CounterexampleProofError(
            "$.runtime_binding.sys_version: invalid_python_patch"
        )
    return patch


def _validate_current_runtime_compatibility() -> None:
    current_patch = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    if current_patch != PINNED_PYTHON_PATCH:
        raise CounterexampleProofError(
            "$.runtime_binding.sys_version: current_runtime_patch_mismatch"
        )
    if unicodedata.unidata_version != PINNED_UNICODE_DATABASE_VERSION:
        raise CounterexampleProofError(
            "$.runtime_binding.unicodedata_unidata_version: "
            "current_runtime_value_mismatch"
        )


def _validate_recorded_runtime_binding(recorded: Mapping[str, Any]) -> str:
    """Fail before scorer replay when the recorded Python/Unicode runtime drifts."""

    binding = recorded.get("runtime_binding")
    if not isinstance(binding, Mapping):
        raise CounterexampleProofError("$.runtime_binding: missing_or_type_mismatch")
    checks = (
        ("python_requirement", f"=={PINNED_PYTHON_PATCH}"),
        ("exact_python_patch_pinned", True),
        ("unicodedata_unidata_version", PINNED_UNICODE_DATABASE_VERSION),
        ("unicode_database_version_pinned", True),
    )
    for field, expected in checks:
        path = f"$.runtime_binding.{field}"
        if field not in binding:
            raise CounterexampleProofError(f"{path}: missing")
        actual = binding[field]
        if type(actual) is not type(expected):
            raise CounterexampleProofError(f"{path}: type_mismatch")
        if actual != expected:
            raise CounterexampleProofError(f"{path}: value_mismatch")
    sys_version = binding.get("sys_version")
    if not isinstance(sys_version, str):
        reason = "missing" if "sys_version" not in binding else "type_mismatch"
        raise CounterexampleProofError(f"$.runtime_binding.sys_version: {reason}")
    if _python_patch_from_sys_version(sys_version) != PINNED_PYTHON_PATCH:
        raise CounterexampleProofError(
            "$.runtime_binding.sys_version: value_mismatch"
        )
    _validate_current_runtime_compatibility()
    return sys_version


def _compare_values(
    expected: Any, actual: Any, path: str = "$"
) -> tuple[list[dict[str, str]], int]:
    mismatches: list[dict[str, str]] = []
    compared = 0
    if type(expected) is not type(actual):
        return ([{"path": path, "reason": "type_mismatch"}], 1)
    if isinstance(expected, Mapping):
        keys = sorted(set(expected) | set(actual))
        for key in keys:
            child = f"{path}.{key}"
            if key not in expected:
                mismatches.append({"path": child, "reason": "unexpected_key"})
                compared += 1
            elif key not in actual:
                mismatches.append({"path": child, "reason": "missing_key"})
                compared += 1
            else:
                found, count = _compare_values(expected[key], actual[key], child)
                mismatches.extend(found)
                compared += count
        return mismatches, compared
    if isinstance(expected, list):
        compared += 1
        if len(expected) != len(actual):
            mismatches.append({"path": path, "reason": "length_mismatch"})
        for index, (left, right) in enumerate(zip(expected, actual)):
            found, count = _compare_values(left, right, f"{path}[{index}]")
            mismatches.extend(found)
            compared += count
        return mismatches, compared
    compared += 1
    if expected != actual:
        mismatches.append({"path": path, "reason": "value_mismatch"})
    return mismatches, compared


def verify_phase6_depth60_95pct_counterexample(
    project_root: str | Path = PROJECT_ROOT,
    proof_path: str | Path | None = None,
) -> dict[str, Any]:
    """Compare every proof value and return a value-redacted summary."""

    root = Path(project_root).resolve()
    path = Path(proof_path) if proof_path is not None else root / PROOF_RELATIVE_PATH
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CounterexampleProofError(
            "proof artifact is missing or invalid JSON"
        ) from exc
    if not isinstance(recorded, Mapping):
        raise CounterexampleProofError("proof artifact is not an object")
    recorded_sys_version = _validate_recorded_runtime_binding(recorded)
    generated = build_phase6_depth60_95pct_counterexample(
        root, recorded_sys_version=recorded_sys_version
    )
    mismatches, compared = _compare_values(recorded, generated)
    summary = {
        "status": "valid" if not mismatches else "invalid",
        "artifact": PROOF_RELATIVE_PATH.as_posix(),
        "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "compared_value_count": compared,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "target_task_count": len(TARGET_TASK_IDS),
        "network_calls": 0,
        "model_calls": 0,
        "api_key_reads": 0,
    }
    if mismatches:
        raise CounterexampleProofError(
            _canonical_json_bytes(summary).decode("utf-8").rstrip()
        )
    return summary


verify_counterexample = verify_phase6_depth60_95pct_counterexample


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the offline Phase 6 95% lexical counterexample."
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--emit", action="store_true", help="emit canonical proof JSON"
    )
    actions.add_argument(
        "--verify", action="store_true", help="verify the recorded proof"
    )
    parser.add_argument("--proof", type=Path, help=argparse.SUPPRESS)
    parsed = parser.parse_args(argv)
    try:
        if parsed.emit:
            sys.stdout.write(
                _canonical_json_bytes(
                    build_phase6_depth60_95pct_counterexample()
                ).decode("utf-8")
            )
        else:
            summary = verify_phase6_depth60_95pct_counterexample(
                proof_path=parsed.proof
            )
            sys.stdout.write(_canonical_json_bytes(summary).decode("utf-8"))
    except CounterexampleProofError as exc:
        error = {
            "status": "invalid",
            "error": "counterexample_proof_verification_failed",
            "detail": str(exc),
            "network_calls": 0,
            "model_calls": 0,
            "api_key_reads": 0,
        }
        sys.stderr.write(_canonical_json_bytes(error).decode("utf-8"))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
