from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import researchops.phase6_eval as phase6_eval_module  # noqa: E402
from researchops.phase6_eval import (  # noqa: E402
    _ASSIGNMENT_LABEL,
    _EVIDENCE_ID_IN_TEXT,
    _NUMBER_IN_TEXT,
    _STRUCTURED_ASSERTION_LINE,
    _STRUCTURED_CLAIM,
    _contains_phrase,
    _depth60_structured_required_phrases,
    _structured_assertion_labels,
    _structured_assertion_values,
    _structured_assertions_match,
    load_phase6_tasks,
)
from researchops.phase6_source_bundle import (  # noqa: E402
    phase6_depth60_source_bundle_sha256,
)


CORPUS_RELATIVE_PATH = Path("evals/phase6_agent_tasks.jsonl")
SCORER_RELATIVE_PATH = Path("src/researchops/phase6_eval.py")
RUNNER_RELATIVE_PATH = Path("src/researchops/phase6_runner.py")
SOURCE_BUNDLE_RELATIVE_PATH = Path("src/researchops/phase6_source_bundle.py")
SCRIPT_RELATIVE_PATH = Path("scripts/analyze_phase6_depth60_rejections.py")
PUBLIC_COMMITMENTS_RELATIVE_PATH = Path(
    "docs/evidence/phase6-deepseek-depth60-v1/artifact_commitments.json"
)
PUBLIC_SUMMARY_RELATIVE_PATH = Path(
    "docs/evidence/phase6-deepseek-depth60-v1/public_summary.json"
)
INTERPRETATION_PROJECTION_RELATIVE_PATH = Path(
    "docs/evidence/phase6-deepseek-depth60-interpretation-audit-v1/"
    "diagnostic_projection.json"
)
ARTIFACT_RELATIVE_PATH = Path(
    "docs/evidence/phase6-deepseek-depth60-interpretation-audit-v1/"
    "rejection_reason_histogram.json"
)
LOCKED_RESULTS_NAME = "phase6_results.jsonl"
LOCKED_AUDIT_DB_NAME = "phase6_audit.sqlite3"
LOCKED_FAILED_COUNT = 40
LOCKED_SOLE_REQUIRED_COUNT = 34

PLAIN_REQUIRED_PHRASE_MISSING = "plain_required_phrase_missing"
MALFORMED_ASSERT_LINE = "malformed_assert_line"
MISSING_EXPECTED_ASSERTION = "missing_expected_assertion"
UNEXPECTED_ASSERTION = "unexpected_assertion"
DUPLICATE_ASSERTION = "duplicate_assertion"
ASSIGNMENT_LABEL_REPEATED_IN_PROSE = "assignment_label_repeated_in_prose"
NUMERIC_LITERAL_IN_NON_CLAIM_PROSE = "numeric_literal_in_non_claim_prose"
ENUM_VALUE_REPEATED_IN_PROSE = "enum_value_repeated_in_prose"

REJECTION_REASON_ORDER = (
    PLAIN_REQUIRED_PHRASE_MISSING,
    MALFORMED_ASSERT_LINE,
    MISSING_EXPECTED_ASSERTION,
    UNEXPECTED_ASSERTION,
    DUPLICATE_ASSERTION,
    ASSIGNMENT_LABEL_REPEATED_IN_PROSE,
    NUMERIC_LITERAL_IN_NON_CLAIM_PROSE,
    ENUM_VALUE_REPEATED_IN_PROSE,
)
STRUCTURED_REJECTION_REASONS = frozenset(REJECTION_REASON_ORDER[1:])

PHRASE_ONLY_ALL_LITERALS_PRESENT_FORMAT_REJECTED = (
    "phrase_only_all_literals_present_format_rejected"
)
PHRASE_ONLY_ONE_OR_MORE_LITERALS_MISSING = (
    "phrase_only_one_or_more_literals_missing"
)
MIXED_ALL_LITERALS_PRESENT_FORMAT_REJECTED = (
    "mixed_all_literals_present_format_rejected"
)
MIXED_ONE_OR_MORE_LITERALS_MISSING = "mixed_one_or_more_literals_missing"
OBJECTIVE_BUCKET_ORDER = (
    PHRASE_ONLY_ALL_LITERALS_PRESENT_FORMAT_REJECTED,
    PHRASE_ONLY_ONE_OR_MORE_LITERALS_MISSING,
    MIXED_ALL_LITERALS_PRESENT_FORMAT_REJECTED,
    MIXED_ONE_OR_MORE_LITERALS_MISSING,
)
PRIMARY_BUCKET_ORDER = OBJECTIVE_BUCKET_ORDER
FIRST_GATE_PLAIN = "nonstructured_required_phrase_short_circuit"
FIRST_GATE_MALFORMED = "malformed_assert_line"
FIRST_GATE_INVENTORY = "assertion_inventory_rejected"
FIRST_GATE_ASSIGNMENT = "assignment_label_repeated_in_prose"
FIRST_GATE_NUMERIC = "numeric_literal_in_non_claim_prose"
FIRST_GATE_ENUM = "enum_value_repeated_in_prose"
FIRST_FAILED_GATE_ORDER = (
    FIRST_GATE_PLAIN,
    FIRST_GATE_MALFORMED,
    FIRST_GATE_INVENTORY,
    FIRST_GATE_ASSIGNMENT,
    FIRST_GATE_NUMERIC,
    FIRST_GATE_ENUM,
)

AST_SYMBOLS = (
    "_NUMBER_IN_TEXT",
    "_STRUCTURED_CLAIM",
    "_STRUCTURED_ASSERTION_LINE",
    "_ASSIGNMENT_LABEL",
    "_contains_phrase",
    "_depth60_structured_required_phrases",
    "_structured_assertion_labels",
    "_structured_assertion_values",
    "_structured_assertions_match",
    "load_phase6_tasks",
)
RUNNER_AST_SYMBOLS = (
    "_safe_record",
    "_safe_text",
    "_optional_text_sha256",
)


class RejectionAnalysisError(RuntimeError):
    """The locked rejection analysis could not be built or verified."""


FORBIDDEN_PUBLICATION_KEYS = frozenset(
    {
        "final_output",
        "raw_output",
        "raw_model_output",
        "prompt",
        "response_body",
        "request_headers",
        "response_headers",
        "api_key",
        "authorization_id",
        "authorization_binding",
    }
)


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
    return {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def _ast_symbol_locations(
    source_path: Path, symbols: Sequence[str] = AST_SYMBOLS
) -> dict[str, dict[str, Any]]:
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
            if name in symbols:
                found[name] = {
                    "kind": kind,
                    "line": node.lineno,
                    "end_line": node.end_lineno,
                }
    missing = sorted(set(symbols) - set(found))
    if missing:
        raise RejectionAnalysisError(f"missing scorer AST symbols: {missing}")
    return {name: found[name] for name in symbols}


def _locked_omitted_commitment(root: Path, artifact_name: str) -> dict[str, Any]:
    path = root / PUBLIC_COMMITMENTS_RELATIVE_PATH
    try:
        public = json.loads(path.read_text(encoding="utf-8"))
        commitment = public["opaque_commitments"][artifact_name]
        byte_count = commitment["bytes"]
        digest = commitment["sha256"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RejectionAnalysisError(
            "locked public results commitment is missing or invalid"
        ) from exc
    if (
        not isinstance(byte_count, int)
        or byte_count <= 0
        or not isinstance(digest, str)
        or re.fullmatch(r"[a-f0-9]{64}", digest) is None
    ):
        raise RejectionAnalysisError("locked public results commitment is malformed")
    return {
        "published_name": artifact_name,
        "bytes": byte_count,
        "sha256": digest,
    }


def _locked_results_commitment(root: Path) -> dict[str, Any]:
    return _locked_omitted_commitment(root, LOCKED_RESULTS_NAME)


def _locked_audit_db_commitment(root: Path) -> dict[str, Any]:
    return _locked_omitted_commitment(root, LOCKED_AUDIT_DB_NAME)


def _locked_source_bundle_sha256(root: Path) -> str:
    try:
        public = json.loads(
            (root / PUBLIC_SUMMARY_RELATIVE_PATH).read_text(encoding="utf-8")
        )
        expected = public["plan"]["source_bundle_sha256"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RejectionAnalysisError(
            "locked source-bundle commitment is missing or invalid"
        ) from exc
    if not isinstance(expected, str) or re.fullmatch(r"[a-f0-9]{64}", expected) is None:
        raise RejectionAnalysisError("locked source-bundle commitment is malformed")
    actual = phase6_depth60_source_bundle_sha256(root)
    if actual != expected:
        raise RejectionAnalysisError("locked source-bundle commitment mismatch")
    return expected


def _published_artifact_commitment(root: Path) -> dict[str, Any]:
    projection_path = root / INTERPRETATION_PROJECTION_RELATIVE_PATH
    try:
        projection = json.loads(projection_path.read_text(encoding="utf-8"))
        binding = projection["rejection_reason_histogram"]
        artifact = binding["artifact"]
        byte_count = binding["bytes"]
        digest = binding["sha256"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RejectionAnalysisError(
            "published rejection histogram commitment is missing or invalid"
        ) from exc
    if (
        artifact != ARTIFACT_RELATIVE_PATH.as_posix()
        or not isinstance(byte_count, int)
        or byte_count <= 0
        or not isinstance(digest, str)
        or re.fullmatch(r"[a-f0-9]{64}", digest) is None
    ):
        raise RejectionAnalysisError(
            "published rejection histogram commitment is malformed"
        )
    return {"artifact": artifact, "bytes": byte_count, "sha256": digest}


def _expected_matcher_binding() -> dict[str, Any]:
    return {
        "actual_private_matcher_imported": True,
        "required_phrase_matcher": "_contains_phrase",
        "structured_matcher": "_structured_assertions_match",
        "number_in_text_pattern": _NUMBER_IN_TEXT.pattern,
        "structured_claim_pattern": _STRUCTURED_CLAIM.pattern,
        "structured_assertion_line_pattern": _STRUCTURED_ASSERTION_LINE.pattern,
        "assignment_label_pattern": _ASSIGNMENT_LABEL.pattern,
        "diagnostic_reason_order": list(REJECTION_REASON_ORDER),
        "first_failed_gate_order": list(FIRST_FAILED_GATE_ORDER),
        "structured_rejection_reasons_semantics": (
            "post_hoc_non_short_circuit_trigger_set_not_native_scorer_emissions"
        ),
        "first_failed_gate_semantics": (
            "locked_short_circuit_order_applied_to_the_persisted_projection_output"
        ),
        "all_required_literals_present_is_semantic_correctness": False,
    }


def _ordered_reasons(reasons: set[str]) -> list[str]:
    unknown = reasons - set(REJECTION_REASON_ORDER)
    if unknown:
        raise RejectionAnalysisError(f"unknown structured rejection reasons: {sorted(unknown)}")
    return [reason for reason in REJECTION_REASON_ORDER if reason in reasons]


def diagnose_required_phrase_rejection(
    task_id: str,
    text: str,
    required_phrases: Sequence[str],
    forbidden_assertions: Sequence[str],
) -> dict[str, Any]:
    """Explain every branch that can make the locked required-phrase check fail.

    The boolean decision is delegated to the imported scorer private functions.
    This function mirrors the scorer's branch predicates and accumulates them so
    one output may carry multiple diagnostic trigger labels.
    """

    if not isinstance(text, str):
        raise RejectionAnalysisError("final output must be a string")
    expected = tuple(required_phrases)
    forbidden = tuple(forbidden_assertions)
    structured = _depth60_structured_required_phrases(task_id, expected)
    structured_set = set(structured)
    plain = tuple(phrase for phrase in expected if phrase not in structured_set)
    phrase_matches = [
        {
            "phrase": phrase,
            "match": _contains_phrase(text, phrase),
            "structured": phrase in structured_set,
        }
        for phrase in expected
    ]

    reasons: set[str] = set()
    if any(not _contains_phrase(text, phrase) for phrase in plain):
        reasons.add(PLAIN_REQUIRED_PHRASE_MISSING)

    observed: list[str] = []
    prose_lines: list[str] = []
    for line in text.splitlines():
        if "[assert" in line.casefold():
            match = _STRUCTURED_ASSERTION_LINE.fullmatch(line)
            if match is None:
                reasons.add(MALFORMED_ASSERT_LINE)
            else:
                observed.append(match.group("phrase"))
        elif _STRUCTURED_CLAIM.fullmatch(line.strip()) is None:
            prose_lines.append(line)

    observed_set = set(observed)
    if any(phrase not in observed_set for phrase in structured):
        reasons.add(MISSING_EXPECTED_ASSERTION)
    if any(phrase not in structured_set for phrase in observed):
        reasons.add(UNEXPECTED_ASSERTION)
    if len(observed) != len(observed_set):
        reasons.add(DUPLICATE_ASSERTION)

    prose = _EVIDENCE_ID_IN_TEXT.sub("", "\n".join(prose_lines))
    labels = _structured_assertion_labels(structured)
    if any(
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(label)}\s*=",
            prose,
            re.IGNORECASE,
        )
        for label in labels
    ):
        reasons.add(ASSIGNMENT_LABEL_REPEATED_IN_PROSE)

    normalized_prose = unicodedata.normalize("NFKC", prose).casefold()
    if _NUMBER_IN_TEXT.search(normalized_prose) is not None:
        reasons.add(NUMERIC_LITERAL_IN_NON_CLAIM_PROSE)

    values = _structured_assertion_values((*structured, *forbidden))
    for value in values:
        if re.fullmatch(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)", value):
            continue
        variants = {
            value.casefold(),
            value.casefold().replace("_", "-"),
            value.casefold().replace("_", " "),
        }
        if any(
            re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(variant)}(?![A-Za-z0-9_])",
                normalized_prose,
            )
            for variant in variants
        ):
            reasons.add(ENUM_VALUE_REPEATED_IN_PROSE)
            break

    structured_matches = _structured_assertions_match(
        task_id, text, structured, forbidden
    )
    structured_labels = reasons & STRUCTURED_REJECTION_REASONS
    if structured_matches == bool(structured_labels):
        raise RejectionAnalysisError(
            "structured matcher and rejection branch diagnostics disagree"
        )
    required_phrases_match = all(item["match"] for item in phrase_matches)
    nonstructured_phrases_match = all(
        _contains_phrase(text, phrase) for phrase in plain
    )
    scorer_required_phrase_check = nonstructured_phrases_match and structured_matches
    ordered = _ordered_reasons(reasons)
    if not scorer_required_phrase_check and not ordered:
        raise RejectionAnalysisError(
            "required-phrase check rejected output without a diagnostic reason"
        )
    return {
        "expected_phrase_count": len(expected),
        "structured_expected_phrase_count": len(structured),
        "required_phrase_matches": phrase_matches,
        "all_required_literals_present": required_phrases_match,
        "all_nonstructured_required_phrases_present": nonstructured_phrases_match,
        "structured_assertions_match": structured_matches,
        "scorer_required_phrase_check": scorer_required_phrase_check,
        "structured_rejection_reasons": ordered,
    }


def _load_results(path: Path) -> tuple[bytes, list[tuple[bytes, dict[str, Any]]]]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise RejectionAnalysisError("explicit results artifact cannot be read") from exc
    entries: list[tuple[bytes, dict[str, Any]]] = []
    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        if not raw_line.strip():
            raise RejectionAnalysisError(f"blank results row at line {line_number}")
        try:
            row = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RejectionAnalysisError(
                f"invalid results JSON at line {line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise RejectionAnalysisError(
                f"results row at line {line_number} is not an object"
            )
        entries.append((raw_line, row))
    if not entries:
        raise RejectionAnalysisError("results artifact has no rows")
    return payload, entries


def _load_scored_output_hashes(
    audit_db_path: Path, root: Path
) -> tuple[dict[str, str | None], dict[str, Any]]:
    try:
        payload = audit_db_path.read_bytes()
    except OSError as exc:
        raise RejectionAnalysisError("explicit audit database cannot be read") from exc
    observed = {
        "published_name": LOCKED_AUDIT_DB_NAME,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    if observed != _locked_audit_db_commitment(root):
        raise RejectionAnalysisError(
            "explicit audit database does not match the locked public commitment"
        )
    try:
        connection = sqlite3.connect(f"file:{audit_db_path.resolve()}?mode=ro", uri=True)
        request_rows = connection.execute(
            "SELECT run_id, safe_payload_json FROM audit_events "
            "WHERE event_type = 'agent_request_dispatched'"
        ).fetchall()
        trace_rows = connection.execute(
            "SELECT run_id, safe_payload_json FROM audit_events "
            "WHERE event_type = 'agent_trace_recorded'"
        ).fetchall()
    except sqlite3.Error as exc:
        raise RejectionAnalysisError("locked audit database query failed") from exc
    finally:
        if "connection" in locals():
            connection.close()
    run_to_task: dict[str, str] = {}
    for run_id, payload_json in request_rows:
        try:
            task_id = json.loads(payload_json)["task_id"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RejectionAnalysisError("invalid request audit event") from exc
        if not isinstance(run_id, str) or not isinstance(task_id, str) or run_id in run_to_task:
            raise RejectionAnalysisError("duplicate or invalid request audit event")
        run_to_task[run_id] = task_id
    result: dict[str, str | None] = {}
    for run_id, payload_json in trace_rows:
        try:
            digest = json.loads(payload_json)["final_output_sha256"]
            task_id = run_to_task[run_id]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RejectionAnalysisError("invalid trace audit event") from exc
        if task_id in result or (
            digest is not None
            and (
                not isinstance(digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", digest) is None
            )
        ):
            raise RejectionAnalysisError("duplicate or invalid scored output hash")
        result[task_id] = digest
    if len(result) != 60:
        raise RejectionAnalysisError("locked audit database does not bind 60 scored outputs")
    return result, observed


def _first_failed_gate(diagnostic_triggers: Sequence[str]) -> str:
    reasons = set(diagnostic_triggers)
    if PLAIN_REQUIRED_PHRASE_MISSING in reasons:
        return FIRST_GATE_PLAIN
    if MALFORMED_ASSERT_LINE in reasons:
        return FIRST_GATE_MALFORMED
    if reasons & {
        MISSING_EXPECTED_ASSERTION,
        UNEXPECTED_ASSERTION,
        DUPLICATE_ASSERTION,
    }:
        return FIRST_GATE_INVENTORY
    if ASSIGNMENT_LABEL_REPEATED_IN_PROSE in reasons:
        return FIRST_GATE_ASSIGNMENT
    if NUMERIC_LITERAL_IN_NON_CLAIM_PROSE in reasons:
        return FIRST_GATE_NUMERIC
    if ENUM_VALUE_REPEATED_IN_PROSE in reasons:
        return FIRST_GATE_ENUM
    raise RejectionAnalysisError("failed required-phrase row has no first failed gate")


def _primary_bucket(
    original_failure_reasons: Sequence[str], all_required_literals_present: bool
) -> str:
    required_only = list(original_failure_reasons) == ["required_phrases"]
    if required_only and all_required_literals_present:
        return PHRASE_ONLY_ALL_LITERALS_PRESENT_FORMAT_REJECTED
    if required_only:
        return PHRASE_ONLY_ONE_OR_MORE_LITERALS_MISSING
    if all_required_literals_present:
        return MIXED_ALL_LITERALS_PRESENT_FORMAT_REJECTED
    return MIXED_ONE_OR_MORE_LITERALS_MISSING


def _reason_set_counts(
    rows: Sequence[Mapping[str, Any]], key: str, order: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, ...]] = Counter()
    order_index = {reason: index for index, reason in enumerate(order or ())}
    for row in rows:
        values = row[key]
        reason_set = tuple(
            sorted(
                set(values),
                key=lambda reason: (order_index.get(reason, len(order_index)), reason),
            )
        )
        counts[reason_set] += 1
    return [
        {"reasons": list(reason_set), "count": counts[reason_set]}
        for reason_set in sorted(counts, key=lambda item: (len(item), item))
    ]


def _cohort_histogram(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    primary_counts = Counter(row["primary_bucket"] for row in rows)
    trigger_counts = Counter(
        reason
        for row in rows
        for reason in row["structured_rejection_reasons"]
    )
    original_counts = Counter(
        reason for row in rows for reason in row["original_failure_reasons"]
    )
    first_gate_counts = Counter(row["first_failed_gate"] for row in rows)
    all_literal_count = sum(
        row["all_required_literals_present"] is True for row in rows
    )
    required_only = [
        row
        for row in rows
        if row["original_failure_reasons"] == ["required_phrases"]
    ]
    mixed_failures = [
        row
        for row in rows
        if row["original_failure_reasons"] != ["required_phrases"]
    ]

    def literal_presence_split(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        present = sum(row["all_required_literals_present"] is True for row in items)
        return {
            "all_required_literals_present": present,
            "one_or_more_required_literals_missing": len(items) - present,
        }

    return {
        "denominator": len(rows),
        "primary_bucket_interpretation": {
            PHRASE_ONLY_ALL_LITERALS_PRESENT_FORMAT_REJECTED: (
                "required_phrases was the only failed locked check and every required "
                "literal matched, but the structured contract rejected the output; "
                "this is not a semantic-correctness judgment"
            ),
            PHRASE_ONLY_ONE_OR_MORE_LITERALS_MISSING: (
                "required_phrases was the only failed locked check and at least one "
                "required literal did not match"
            ),
            MIXED_ALL_LITERALS_PRESENT_FORMAT_REJECTED: (
                "every required literal matched and the structured contract rejected "
                "the output, but at least one other locked check also failed"
            ),
            MIXED_ONE_OR_MORE_LITERALS_MISSING: (
                "at least one required literal and at least one other locked check failed"
            ),
        },
        "primary_bucket_counts": {
            bucket: primary_counts[bucket] for bucket in PRIMARY_BUCKET_ORDER
        },
        "all_literals_present_structured_rejected_vs_other": {
            "all_required_literals_present_but_structured_rejected": all_literal_count,
            "other": len(rows) - all_literal_count,
        },
        "failure_scope_by_literal_presence_2x2": {
            "required_phrases_only": literal_presence_split(required_only),
            "mixed_failure_reasons": literal_presence_split(mixed_failures),
        },
        "first_failed_gate_counts": {
            gate: first_gate_counts[gate]
            for gate in FIRST_FAILED_GATE_ORDER
            if first_gate_counts[gate]
        },
        "non_short_circuit_diagnostic_trigger_counts": {
            reason: trigger_counts[reason]
            for reason in REJECTION_REASON_ORDER
            if trigger_counts[reason]
        },
        "exact_non_short_circuit_diagnostic_trigger_set_counts": _reason_set_counts(
            rows, "structured_rejection_reasons", REJECTION_REASON_ORDER
        ),
        "original_failure_reason_counts": {
            reason: original_counts[reason] for reason in sorted(original_counts)
        },
        "exact_original_failure_reason_set_counts": _reason_set_counts(
            rows, "original_failure_reasons"
        ),
    }


def _build_histograms(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    sole = [
        row
        for row in rows
        if row["original_failure_reasons"] == ["required_phrases"]
    ]
    return {
        "all_failed_40": _cohort_histogram(rows),
        "required_phrases_only_34": _cohort_histogram(sole),
    }


def _delivery_aware_interpretation(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    empty_digest = hashlib.sha256(b"").hexdigest()
    byte_empty_rows = [row for row in rows if row["output_blank"]]
    content_bearing_rows = [row for row in rows if not row["output_blank"]]
    byte_empty_task_ids = [row["task_id"] for row in byte_empty_rows]
    if byte_empty_task_ids != ["P6-DEV-043", "P6-DEV-060"]:
        raise RejectionAnalysisError("locked byte-empty task set drifted")
    if any(
        row["projection_output_sha256"] != empty_digest
        or row["scored_output_sha256"] != empty_digest
        or row["projection_matches_scored_output"] is not True
        for row in byte_empty_rows
    ):
        raise RejectionAnalysisError("byte-empty scorer-input binding drifted")

    content_histogram = _cohort_histogram(content_bearing_rows)
    plain_gate_rows = [
        row for row in rows if row["first_failed_gate"] == FIRST_GATE_PLAIN
    ]
    mixed_missing_rows = [
        row
        for row in rows
        if row["primary_bucket"] == MIXED_ONE_OR_MORE_LITERALS_MISSING
    ]
    missing_assertion_rows = [
        row
        for row in rows
        if MISSING_EXPECTED_ASSERTION in row["structured_rejection_reasons"]
    ]
    return {
        "interpretation_scope": "post_hoc_delivery_aware_not_official_rescore",
        "first_failed_gate_is_causal_attribution": False,
        "reported_findings_are_exhaustive_or_mutually_exclusive_causal_partition": False,
        "mechanical_required_phrases_failure_count": len(rows),
        "byte_empty_delivery_failure_count": len(byte_empty_rows),
        "byte_empty_delivery_failure_task_ids": byte_empty_task_ids,
        "byte_empty_output_sha256": empty_digest,
        "byte_empty_required_phrases_failure_is_textually_discriminating": False,
        "content_bearing_failed_task_count": len(content_bearing_rows),
        "content_bearing_required_phrases_failure_count": len(
            content_bearing_rows
        ),
        "content_bearing_scored_output_byte_exact_count": sum(
            row["projection_matches_scored_output"] for row in content_bearing_rows
        ),
        "content_bearing_sanitized_projection_only_task_ids": [
            row["task_id"]
            for row in content_bearing_rows
            if not row["projection_matches_scored_output"]
        ],
        "content_bearing_first_failed_gate_counts": content_histogram[
            "first_failed_gate_counts"
        ],
        "content_bearing_non_short_circuit_diagnostic_trigger_counts": (
            content_histogram["non_short_circuit_diagnostic_trigger_counts"]
        ),
        "content_bearing_failure_scope_by_literal_presence_2x2": (
            content_histogram["failure_scope_by_literal_presence_2x2"]
        ),
        "content_bearing_primary_bucket_counts": content_histogram[
            "primary_bucket_counts"
        ],
        "plain_required_phrase_first_gate_decomposition": {
            "mechanical_count": len(plain_gate_rows),
            "content_bearing_literal_omission_count": sum(
                not row["output_blank"] for row in plain_gate_rows
            ),
            "content_bearing_task_ids": [
                row["task_id"] for row in plain_gate_rows if not row["output_blank"]
            ],
            "byte_empty_delivery_failure_count": sum(
                row["output_blank"] for row in plain_gate_rows
            ),
            "byte_empty_task_ids": [
                row["task_id"] for row in plain_gate_rows if row["output_blank"]
            ],
        },
        "mixed_missing_literal_cell_decomposition": {
            "mechanical_count": len(mixed_missing_rows),
            "content_bearing_mixed_failure_count": sum(
                not row["output_blank"] for row in mixed_missing_rows
            ),
            "content_bearing_task_ids": [
                row["task_id"]
                for row in mixed_missing_rows
                if not row["output_blank"]
            ],
            "byte_empty_delivery_failure_count": sum(
                row["output_blank"] for row in mixed_missing_rows
            ),
            "byte_empty_task_ids": [
                row["task_id"] for row in mixed_missing_rows if row["output_blank"]
            ],
        },
        "missing_expected_assertion_trigger_decomposition": {
            "mechanical_count": len(missing_assertion_rows),
            "content_bearing_count": sum(
                not row["output_blank"] for row in missing_assertion_rows
            ),
            "byte_empty_delivery_failure_count": sum(
                row["output_blank"] for row in missing_assertion_rows
            ),
            "byte_empty_task_ids": [
                row["task_id"]
                for row in missing_assertion_rows
                if row["output_blank"]
            ],
            "byte_exact_content_bearing_count": sum(
                not row["output_blank"]
                and row["projection_matches_scored_output"]
                for row in missing_assertion_rows
            ),
            "sanitized_only_content_bearing_task_ids": [
                row["task_id"]
                for row in missing_assertion_rows
                if not row["output_blank"]
                and not row["projection_matches_scored_output"]
            ],
        },
    }


def _build_histogram_views(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    byte_exact_rows = [
        row for row in rows if row["projection_matches_scored_output"]
    ]
    unresolved_rows = [
        row for row in rows if not row["projection_matches_scored_output"]
    ]
    byte_exact_sole = [
        row
        for row in byte_exact_rows
        if row["original_failure_reasons"] == ["required_phrases"]
    ]
    return {
        "sanitized_projection": _build_histograms(rows),
        "delivery_aware_interpretation": _delivery_aware_interpretation(rows),
        "scored_output_byte_exact_projection": {
            "all_failed_39": _cohort_histogram(byte_exact_rows),
            "required_phrases_only_33": _cohort_histogram(byte_exact_sole),
            "indeterminate_against_actual_scorer_input_count": len(unresolved_rows),
            "indeterminate_task_ids": [row["task_id"] for row in unresolved_rows],
        },
    }


def build_phase6_depth60_rejection_histogram(
    results_path: str | Path,
    audit_db_path: str | Path,
    project_root: str | Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Build the sanitized rejection histogram from the explicit locked raw JSONL."""

    root = Path(project_root).resolve()
    corpus_path = root / CORPUS_RELATIVE_PATH
    scorer_path = root / SCORER_RELATIVE_PATH
    runner_path = root / RUNNER_RELATIVE_PATH
    source_bundle_path = root / SOURCE_BUNDLE_RELATIVE_PATH
    script_path = root / SCRIPT_RELATIVE_PATH
    commitments_path = root / PUBLIC_COMMITMENTS_RELATIVE_PATH
    public_summary_path = root / PUBLIC_SUMMARY_RELATIVE_PATH
    imported_scorer_path = Path(phase6_eval_module.__file__).resolve()
    if imported_scorer_path != scorer_path.resolve():
        raise RejectionAnalysisError("imported scorer does not match bound source file")

    locked_results = _locked_results_commitment(root)
    results_payload, entries = _load_results(Path(results_path))
    observed_results = {
        "published_name": LOCKED_RESULTS_NAME,
        "bytes": len(results_payload),
        "sha256": hashlib.sha256(results_payload).hexdigest(),
    }
    if observed_results != locked_results:
        raise RejectionAnalysisError(
            "explicit results artifact does not match the locked public commitment"
        )
    scored_output_hashes, observed_audit_db = _load_scored_output_hashes(
        Path(audit_db_path), root
    )

    tasks = {task.task_id: task for task in load_phase6_tasks(corpus_path)}
    seen_task_ids: set[str] = set()
    sanitized_rows: list[dict[str, Any]] = []
    for raw_line, result in entries:
        task_id = result.get("task_id")
        if not isinstance(task_id, str) or task_id not in tasks:
            raise RejectionAnalysisError("results row has an unknown task_id")
        if task_id in seen_task_ids:
            raise RejectionAnalysisError(f"duplicate results task_id: {task_id}")
        seen_task_ids.add(task_id)
        score = result.get("score")
        if not isinstance(score, dict) or not isinstance(score.get("task_pass"), bool):
            raise RejectionAnalysisError(f"invalid score object: {task_id}")
        if score["task_pass"]:
            continue
        checks = score.get("checks")
        if not isinstance(checks, dict) or not isinstance(
            checks.get("required_phrases"), bool
        ):
            raise RejectionAnalysisError(f"invalid required_phrases check: {task_id}")
        raw_reasons = score.get("failure_reasons")
        if (
            not isinstance(raw_reasons, list)
            or not raw_reasons
            or any(not isinstance(reason, str) for reason in raw_reasons)
        ):
            raise RejectionAnalysisError(f"invalid failure reasons: {task_id}")
        original_failure_reasons = list(raw_reasons)
        if "required_phrases" not in original_failure_reasons:
            raise RejectionAnalysisError(
                f"locked failed task lacks required_phrases rejection: {task_id}"
            )
        observation = result.get("observation")
        if not isinstance(observation, dict):
            raise RejectionAnalysisError(f"missing observation: {task_id}")
        final_output_value = observation.get("final_output")
        if final_output_value is None:
            final_output = ""
        elif isinstance(final_output_value, str):
            final_output = final_output_value
        else:
            raise RejectionAnalysisError(f"invalid final output: {task_id}")
        completion_integrity = observation.get("completion_integrity")
        if not isinstance(completion_integrity, bool):
            raise RejectionAnalysisError(
                f"invalid completion_integrity: {task_id}"
            )

        task = tasks[task_id]
        diagnostic = diagnose_required_phrase_rejection(
            task_id,
            final_output,
            task.expected.required_phrases,
            task.expected.forbidden_assertions,
        )
        if diagnostic["scorer_required_phrase_check"]:
            raise RejectionAnalysisError(
                f"recorded rejection is not reproduced by locked matcher: {task_id}"
            )
        if diagnostic["scorer_required_phrase_check"] != checks["required_phrases"]:
            raise RejectionAnalysisError(
                f"locked required_phrases boolean was not reproduced: {task_id}"
            )
        output_blank = not final_output.strip()
        bucket = _primary_bucket(
            original_failure_reasons,
            diagnostic["all_required_literals_present"],
        )
        projection_output_sha256 = hashlib.sha256(
            final_output.encode("utf-8")
        ).hexdigest()
        scored_output_sha256 = scored_output_hashes[task_id]
        if scored_output_sha256 is None:
            raise RejectionAnalysisError(
                f"failed task lacks scored output hash binding: {task_id}"
            )
        projection_matches_scored = projection_output_sha256 == scored_output_sha256
        diagnostic_basis = (
            "scored_output_byte_exact"
            if projection_matches_scored
            else "sanitized_projection_only_unresolved_against_actual_scorer_input"
        )
        first_failed_gate = _first_failed_gate(
            diagnostic["structured_rejection_reasons"]
        )
        sanitized_rows.append(
            {
                "task_id": task_id,
                "original_failure_reasons": original_failure_reasons,
                "result_row_sha256": hashlib.sha256(raw_line).hexdigest(),
                "projection_output_sha256": projection_output_sha256,
                "scored_output_sha256": scored_output_sha256,
                "projection_matches_scored_output": projection_matches_scored,
                "diagnostic_basis": diagnostic_basis,
                "output_blank": output_blank,
                "completion_integrity": completion_integrity,
                "expected_phrase_count": diagnostic["expected_phrase_count"],
                "structured_expected_phrase_count": diagnostic[
                    "structured_expected_phrase_count"
                ],
                "required_phrase_matches": [
                    item["match"]
                    for item in diagnostic["required_phrase_matches"]
                ],
                "all_required_literals_present": diagnostic[
                    "all_required_literals_present"
                ],
                "structured_assertions_match": diagnostic[
                    "structured_assertions_match"
                ],
                "structured_rejection_reasons": diagnostic[
                    "structured_rejection_reasons"
                ],
                "first_failed_gate": first_failed_gate,
                "primary_bucket": bucket,
            }
        )

    if len(sanitized_rows) != LOCKED_FAILED_COUNT:
        raise RejectionAnalysisError(
            f"locked failed task count drifted: {len(sanitized_rows)}"
        )
    sole_count = sum(
        row["original_failure_reasons"] == ["required_phrases"]
        for row in sanitized_rows
    )
    if sole_count != LOCKED_SOLE_REQUIRED_COUNT:
        raise RejectionAnalysisError(
            f"locked sole-required failure count drifted: {sole_count}"
        )

    byte_exact_rows = [
        row for row in sanitized_rows if row["projection_matches_scored_output"]
    ]
    unresolved_rows = [
        row for row in sanitized_rows if not row["projection_matches_scored_output"]
    ]
    if len(byte_exact_rows) != 39 or [
        row["task_id"] for row in unresolved_rows
    ] != ["P6-DEV-057"]:
        raise RejectionAnalysisError("safe/scored output hash coverage drifted")
    histogram_views = _build_histogram_views(sanitized_rows)
    return {
        "schema_version": "phase6-depth60-rejection-reason-histogram/1.1",
        "status": "post_hoc_rejection_diagnostic",
        "scope": {
            "failed_task_count": len(sanitized_rows),
            "required_phrases_only_failure_count": sole_count,
            "scored_output_byte_exact_count": len(byte_exact_rows),
            "sanitized_projection_only_count": len(unresolved_rows),
            "sanitized_projection_only_task_ids": [
                row["task_id"] for row in unresolved_rows
            ],
            "byte_empty_delivery_failure_count": 2,
            "byte_empty_delivery_failure_task_ids": [
                "P6-DEV-043",
                "P6-DEV-060",
            ],
            "content_bearing_failed_task_count": 38,
            "official_rescore": False,
            "locked_result_changed": False,
        },
        "matcher_binding": _expected_matcher_binding(),
        "source_bindings": {
            "commitments": {
                CORPUS_RELATIVE_PATH.as_posix(): _source_commitment(corpus_path),
                SCORER_RELATIVE_PATH.as_posix(): _source_commitment(scorer_path),
                RUNNER_RELATIVE_PATH.as_posix(): _source_commitment(runner_path),
                SOURCE_BUNDLE_RELATIVE_PATH.as_posix(): _source_commitment(
                    source_bundle_path
                ),
                SCRIPT_RELATIVE_PATH.as_posix(): _source_commitment(script_path),
                PUBLIC_COMMITMENTS_RELATIVE_PATH.as_posix(): _source_commitment(
                    commitments_path
                ),
                PUBLIC_SUMMARY_RELATIVE_PATH.as_posix(): _source_commitment(
                    public_summary_path
                ),
            },
            "locked_source_bundle_sha256": _locked_source_bundle_sha256(root),
            "omitted_results_artifact": locked_results,
            "omitted_audit_database": observed_audit_db,
            "scorer_ast_symbols": _ast_symbol_locations(scorer_path),
            "runner_ast_symbols": _ast_symbol_locations(
                runner_path, RUNNER_AST_SYMBOLS
            ),
            "imported_private_symbols_from_bound_scorer": True,
        },
        "publication": {
            "raw_model_outputs_omitted": True,
            "raw_result_rows_omitted": True,
            "scored_output_bytes_omitted": True,
            "per_task_sanitized_diagnostics_included": True,
            "derivation_fully_recomputable_only_with_committed_sha256_matching_omitted_artifact": True,
            "omitted_artifact_published_names": [
                LOCKED_RESULTS_NAME,
                LOCKED_AUDIT_DB_NAME,
            ],
            "sanitized_projection_is_not_actual_scorer_input_when_hashes_differ": True,
        },
        "rows": sanitized_rows,
        "histograms": histogram_views,
        "execution_boundary": {
            "network_calls": 0,
            "model_calls": 0,
            "api_key_reads": 0,
            "provider_secret_environment_value_reads": 0,
        },
    }


build_rejection_histogram = build_phase6_depth60_rejection_histogram


def _compare_values(
    expected: Any, actual: Any, path: str = "$"
) -> tuple[list[dict[str, str]], int]:
    mismatches: list[dict[str, str]] = []
    compared = 0
    if type(expected) is not type(actual):
        return ([{"path": path, "reason": "type_mismatch"}], 1)
    if isinstance(expected, Mapping):
        for key in sorted(set(expected) | set(actual)):
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


def _expect_exact_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    if set(value) != expected:
        raise RejectionAnalysisError(f"sanitized artifact keys drifted at {path}")


def _reject_forbidden_publication_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and key.casefold() in FORBIDDEN_PUBLICATION_KEYS:
                raise RejectionAnalysisError(
                    f"forbidden publication field at {path}.{key}"
                )
            _reject_forbidden_publication_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_publication_keys(child, f"{path}[{index}]")


def _validate_sanitized_artifact(
    artifact: Mapping[str, Any], root: Path
) -> dict[str, int]:
    """Validate committed sources plus all facts derivable without raw outputs."""

    _reject_forbidden_publication_keys(artifact)
    _expect_exact_keys(
        artifact,
        {
            "schema_version",
            "status",
            "scope",
            "matcher_binding",
            "source_bindings",
            "publication",
            "rows",
            "histograms",
            "execution_boundary",
        },
        "$",
    )
    if artifact.get("schema_version") != "phase6-depth60-rejection-reason-histogram/1.1":
        raise RejectionAnalysisError("unsupported rejection histogram schema")
    if artifact.get("status") != "post_hoc_rejection_diagnostic":
        raise RejectionAnalysisError("unexpected rejection histogram status")
    publication = artifact.get("publication")
    if not isinstance(publication, dict) or publication != {
        "raw_model_outputs_omitted": True,
        "raw_result_rows_omitted": True,
        "scored_output_bytes_omitted": True,
        "per_task_sanitized_diagnostics_included": True,
        "derivation_fully_recomputable_only_with_committed_sha256_matching_omitted_artifact": True,
        "omitted_artifact_published_names": [
            LOCKED_RESULTS_NAME,
            LOCKED_AUDIT_DB_NAME,
        ],
        "sanitized_projection_is_not_actual_scorer_input_when_hashes_differ": True,
    }:
        raise RejectionAnalysisError("publication boundary is missing or drifted")

    bindings = artifact.get("source_bindings")
    if not isinstance(bindings, dict):
        raise RejectionAnalysisError("source bindings are missing")
    _expect_exact_keys(
        bindings,
        {
            "commitments",
            "locked_source_bundle_sha256",
            "omitted_results_artifact",
            "omitted_audit_database",
            "scorer_ast_symbols",
            "runner_ast_symbols",
            "imported_private_symbols_from_bound_scorer",
        },
        "$.source_bindings",
    )
    commitments = bindings.get("commitments")
    if not isinstance(commitments, dict):
        raise RejectionAnalysisError("source commitments are missing")
    expected_commitments = {
        relative.as_posix(): _source_commitment(root / relative)
        for relative in (
            CORPUS_RELATIVE_PATH,
            SCORER_RELATIVE_PATH,
            RUNNER_RELATIVE_PATH,
            SOURCE_BUNDLE_RELATIVE_PATH,
            SCRIPT_RELATIVE_PATH,
            PUBLIC_COMMITMENTS_RELATIVE_PATH,
            PUBLIC_SUMMARY_RELATIVE_PATH,
        )
    }
    if commitments != expected_commitments:
        raise RejectionAnalysisError("source commitment mismatch")
    if bindings.get("omitted_results_artifact") != _locked_results_commitment(root):
        raise RejectionAnalysisError("omitted results commitment mismatch")
    if bindings.get("locked_source_bundle_sha256") != _locked_source_bundle_sha256(
        root
    ):
        raise RejectionAnalysisError("locked source-bundle binding mismatch")
    if bindings.get("omitted_audit_database") != _locked_audit_db_commitment(root):
        raise RejectionAnalysisError("omitted audit database commitment mismatch")
    if bindings.get("scorer_ast_symbols") != _ast_symbol_locations(
        root / SCORER_RELATIVE_PATH
    ):
        raise RejectionAnalysisError("scorer AST binding mismatch")
    if bindings.get("runner_ast_symbols") != _ast_symbol_locations(
        root / RUNNER_RELATIVE_PATH, RUNNER_AST_SYMBOLS
    ):
        raise RejectionAnalysisError("runner AST binding mismatch")
    if bindings.get("imported_private_symbols_from_bound_scorer") is not True:
        raise RejectionAnalysisError("private scorer import binding is false")
    if Path(phase6_eval_module.__file__).resolve() != (
        root / SCORER_RELATIVE_PATH
    ).resolve():
        raise RejectionAnalysisError("runtime imported a different scorer")
    if artifact.get("matcher_binding") != _expected_matcher_binding():
        raise RejectionAnalysisError("matcher binding is missing or drifted")

    tasks = {task.task_id: task for task in load_phase6_tasks(root / CORPUS_RELATIVE_PATH)}
    rows = artifact.get("rows")
    if not isinstance(rows, list) or len(rows) != LOCKED_FAILED_COUNT:
        raise RejectionAnalysisError("sanitized failed row count mismatch")
    row_keys = {
        "task_id",
        "original_failure_reasons",
        "result_row_sha256",
        "projection_output_sha256",
        "scored_output_sha256",
        "projection_matches_scored_output",
        "diagnostic_basis",
        "output_blank",
        "completion_integrity",
        "expected_phrase_count",
        "structured_expected_phrase_count",
        "required_phrase_matches",
        "all_required_literals_present",
        "structured_assertions_match",
        "structured_rejection_reasons",
        "first_failed_gate",
        "primary_bucket",
    }
    seen: set[str] = set()
    compared = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RejectionAnalysisError(f"sanitized row {index} is not an object")
        _expect_exact_keys(row, row_keys, f"$.rows[{index}]")
        task_id = row["task_id"]
        if not isinstance(task_id, str) or task_id not in tasks or task_id in seen:
            raise RejectionAnalysisError(f"invalid sanitized task_id at row {index}")
        seen.add(task_id)
        task = tasks[task_id]
        reasons = row["original_failure_reasons"]
        if (
            not isinstance(reasons, list)
            or not reasons
            or "required_phrases" not in reasons
            or any(not isinstance(reason, str) for reason in reasons)
        ):
            raise RejectionAnalysisError(f"invalid original failure reasons: {task_id}")
        if any(
            not isinstance(row[key], str)
            or re.fullmatch(r"[a-f0-9]{64}", row[key]) is None
            for key in (
                "result_row_sha256",
                "projection_output_sha256",
                "scored_output_sha256",
            )
        ):
            raise RejectionAnalysisError(f"invalid row hash: {task_id}")
        projection_matches_scored = (
            row["projection_output_sha256"] == row["scored_output_sha256"]
        )
        if row["projection_matches_scored_output"] is not projection_matches_scored:
            raise RejectionAnalysisError(f"output hash comparison mismatch: {task_id}")
        expected_basis = (
            "scored_output_byte_exact"
            if projection_matches_scored
            else "sanitized_projection_only_unresolved_against_actual_scorer_input"
        )
        if row["diagnostic_basis"] != expected_basis:
            raise RejectionAnalysisError(f"diagnostic basis mismatch: {task_id}")
        if not isinstance(row["output_blank"], bool) or not isinstance(
            row["completion_integrity"], bool
        ):
            raise RejectionAnalysisError(f"invalid completion flags: {task_id}")

        expected_phrases = list(task.expected.required_phrases)
        structured = set(
            _depth60_structured_required_phrases(task_id, expected_phrases)
        )
        phrase_matches = row["required_phrase_matches"]
        if (
            not isinstance(phrase_matches, list)
            or len(phrase_matches) != len(expected_phrases)
            or any(not isinstance(matched, bool) for matched in phrase_matches)
        ):
            raise RejectionAnalysisError(f"invalid phrase matches: {task_id}")
        if row["expected_phrase_count"] != len(expected_phrases) or row[
            "structured_expected_phrase_count"
        ] != len(structured):
            raise RejectionAnalysisError(f"expected phrase count mismatch: {task_id}")
        all_present = bool(phrase_matches) and all(phrase_matches)
        if row["all_required_literals_present"] is not all_present:
            raise RejectionAnalysisError(f"all-literals flag mismatch: {task_id}")
        if not isinstance(row["structured_assertions_match"], bool):
            raise RejectionAnalysisError(f"invalid structured match flag: {task_id}")
        diagnostic_reasons = row["structured_rejection_reasons"]
        if (
            not isinstance(diagnostic_reasons, list)
            or diagnostic_reasons != _ordered_reasons(set(diagnostic_reasons))
        ):
            raise RejectionAnalysisError(f"invalid diagnostic reasons: {task_id}")
        structured_reason_present = any(
            reason in STRUCTURED_REJECTION_REASONS
            for reason in diagnostic_reasons
        )
        if row["structured_assertions_match"] == structured_reason_present:
            raise RejectionAnalysisError(
                f"structured rejection has no exact branch reason: {task_id}"
            )
        plain_unmatched = any(
            phrase not in structured and not matched
            for phrase, matched in zip(expected_phrases, phrase_matches)
        )
        if (PLAIN_REQUIRED_PHRASE_MISSING in diagnostic_reasons) != plain_unmatched:
            raise RejectionAnalysisError(f"plain phrase reason mismatch: {task_id}")
        if row["first_failed_gate"] != _first_failed_gate(diagnostic_reasons):
            raise RejectionAnalysisError(f"first failed gate mismatch: {task_id}")
        if all_present and row["structured_assertions_match"]:
            raise RejectionAnalysisError(
                f"sanitized row does not reproduce required rejection: {task_id}"
            )
        expected_bucket = _primary_bucket(reasons, all_present)
        if row["primary_bucket"] != expected_bucket:
            raise RejectionAnalysisError(f"primary bucket mismatch: {task_id}")
        compared += 20 + len(phrase_matches)

    sole_count = sum(
        row["original_failure_reasons"] == ["required_phrases"] for row in rows
    )
    scope = artifact.get("scope")
    byte_exact_count = sum(row["projection_matches_scored_output"] for row in rows)
    unresolved_task_ids = [
        row["task_id"] for row in rows if not row["projection_matches_scored_output"]
    ]
    if scope != {
        "failed_task_count": LOCKED_FAILED_COUNT,
        "required_phrases_only_failure_count": LOCKED_SOLE_REQUIRED_COUNT,
        "scored_output_byte_exact_count": 39,
        "sanitized_projection_only_count": 1,
        "sanitized_projection_only_task_ids": ["P6-DEV-057"],
        "byte_empty_delivery_failure_count": 2,
        "byte_empty_delivery_failure_task_ids": ["P6-DEV-043", "P6-DEV-060"],
        "content_bearing_failed_task_count": 38,
        "official_rescore": False,
        "locked_result_changed": False,
    } or sole_count != LOCKED_SOLE_REQUIRED_COUNT or byte_exact_count != 39 or unresolved_task_ids != ["P6-DEV-057"]:
        raise RejectionAnalysisError("scope denominators mismatch")
    expected_histograms = _build_histogram_views(rows)
    histogram_mismatches, histogram_compared = _compare_values(
        expected_histograms, artifact.get("histograms")
    )
    if histogram_mismatches:
        raise RejectionAnalysisError("histogram does not match sanitized rows")
    if artifact.get("execution_boundary") != {
        "network_calls": 0,
        "model_calls": 0,
        "api_key_reads": 0,
        "provider_secret_environment_value_reads": 0,
    }:
        raise RejectionAnalysisError("execution boundary drifted")
    return {
        "sanitized_row_value_check_count": compared,
        "derived_histogram_value_check_count": histogram_compared,
        "default_verification_compared_value_count": compared
        + histogram_compared,
    }


def verify_phase6_depth60_rejection_histogram(
    project_root: str | Path = PROJECT_ROOT,
    artifact_path: str | Path | None = None,
    results_path: str | Path | None = None,
    audit_db_path: str | Path | None = None,
    expected_artifact_sha256: str | None = None,
) -> dict[str, Any]:
    """Verify publication data, optionally replaying the omitted safe results and audit DB."""

    root = Path(project_root).resolve()
    path = Path(artifact_path) if artifact_path is not None else root / ARTIFACT_RELATIVE_PATH
    try:
        artifact_bytes = path.read_bytes()
        recorded = json.loads(artifact_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RejectionAnalysisError(
            "rejection histogram artifact is missing or invalid JSON"
        ) from exc
    if not isinstance(recorded, dict):
        raise RejectionAnalysisError("rejection histogram artifact is not an object")
    if artifact_bytes != _canonical_json_bytes(recorded):
        raise RejectionAnalysisError("rejection histogram artifact is not canonical JSON")
    published_commitment = _published_artifact_commitment(root)
    observed_commitment = {
        "artifact": ARTIFACT_RELATIVE_PATH.as_posix(),
        "bytes": len(artifact_bytes),
        "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
    }
    if observed_commitment != published_commitment:
        raise RejectionAnalysisError(
            "rejection histogram does not match its published commitment"
        )
    out_of_band_verified = False
    if expected_artifact_sha256 is not None:
        if re.fullmatch(r"[a-f0-9]{64}", expected_artifact_sha256) is None:
            raise RejectionAnalysisError("out-of-band expected artifact SHA-256 is malformed")
        if observed_commitment["sha256"] != expected_artifact_sha256:
            raise RejectionAnalysisError(
                "rejection histogram does not match the out-of-band expected SHA-256"
            )
        out_of_band_verified = True

    internal_checks = _validate_sanitized_artifact(recorded, root)
    mismatches: list[dict[str, str]] = []
    full_compared = 0
    mode = "committed_sanitized_projection_without_omitted_artifacts"
    if (results_path is None) != (audit_db_path is None):
        raise RejectionAnalysisError(
            "full recomputation requires both --results and --audit-db"
        )
    if results_path is not None and audit_db_path is not None:
        generated = build_phase6_depth60_rejection_histogram(
            results_path, audit_db_path, root
        )
        mismatches, full_compared = _compare_values(recorded, generated)
        mode = (
            "full_recomputation_from_sha256_matching_sanitized_results_"
            "and_audit_database"
        )
    summary = {
        "status": "valid" if not mismatches else "invalid",
        "verification_mode": mode,
        "artifact": ARTIFACT_RELATIVE_PATH.as_posix(),
        "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        "published_commitment_verified": True,
        "out_of_band_expected_sha256_verified": out_of_band_verified,
        **internal_checks,
        "full_recomputation_compared_value_count": full_compared,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "failed_task_count": LOCKED_FAILED_COUNT,
        "required_phrases_only_failure_count": LOCKED_SOLE_REQUIRED_COUNT,
        "omitted_results_artifact_loaded": results_path is not None,
        "audit_database_loaded": audit_db_path is not None,
        "actual_scorer_input_bytes_loaded": False,
        "scored_output_byte_exact_count": 39,
        "sanitized_projection_only_count": 1,
        "sanitized_projection_only_task_ids": ["P6-DEV-057"],
        "byte_empty_delivery_failure_count": 2,
        "byte_empty_delivery_failure_task_ids": ["P6-DEV-043", "P6-DEV-060"],
        "content_bearing_failed_task_count": 38,
        "network_calls": 0,
        "model_calls": 0,
        "api_key_reads": 0,
    }
    if mismatches:
        raise RejectionAnalysisError(
            _canonical_json_bytes(summary).decode("utf-8").rstrip()
        )
    return summary


verify_rejection_histogram = verify_phase6_depth60_rejection_histogram


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build or verify the offline Phase 6 depth-60 rejection histogram."
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--emit", action="store_true", help="emit canonical JSON")
    actions.add_argument("--verify", action="store_true", help="verify publication")
    parser.add_argument(
        "--results",
        type=Path,
        help="explicit path to the omitted locked phase6_results.jsonl",
    )
    parser.add_argument(
        "--audit-db",
        type=Path,
        help="explicit path to the omitted locked phase6_audit.sqlite3",
    )
    parser.add_argument(
        "--expected-artifact-sha256",
        help="optional out-of-band expected SHA-256 for coordinated-change detection",
    )
    parser.add_argument("--artifact", type=Path, help=argparse.SUPPRESS)
    parsed = parser.parse_args(argv)
    try:
        if parsed.emit:
            if parsed.results is None or parsed.audit_db is None:
                raise RejectionAnalysisError(
                    "--emit requires explicit --results and --audit-db"
                )
            sys.stdout.write(
                _canonical_json_bytes(
                    build_phase6_depth60_rejection_histogram(
                        parsed.results, parsed.audit_db
                    )
                ).decode("utf-8")
            )
        else:
            summary = verify_phase6_depth60_rejection_histogram(
                artifact_path=parsed.artifact,
                results_path=parsed.results,
                audit_db_path=parsed.audit_db,
                expected_artifact_sha256=parsed.expected_artifact_sha256,
            )
            sys.stdout.write(_canonical_json_bytes(summary).decode("utf-8"))
    except RejectionAnalysisError as exc:
        error = {
            "status": "invalid",
            "error": "phase6_depth60_rejection_analysis_failed",
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
