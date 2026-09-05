from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


CANONICAL_EVIDENCE_IDS = {
    "Windows": "E-36034128278C",
    "Linux": "E-14EBFFCA843E",
}
TASK_CORPUS_NAMES = {
    "Windows": "tasks.jsonl",
    "Linux": "tasks.linux-x86_64.jsonl",
}
QUALITY_PROFILES = {
    "Windows": "phase5-ci-v1",
    "Linux": "phase5-linux-x86-ci-v1",
}
SUPPORTED_SYSTEMS = frozenset(("Windows", "Linux"))
SUPPORTED_MACHINES = frozenset(("amd64", "x86_64"))
NUMERICAL_ENVIRONMENT = {
    "OPENBLAS_CORETYPE": "NEHALEM",
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "NPY_DISABLE_CPU_FEATURES": "X86_V3,X86_V4",
}
PROVIDER_CREDENTIAL_VARIABLES = (
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "MOONSHOT_API_KEY",
)


class DemoError(RuntimeError):
    pass


def _section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def _require_supported_platform() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    python_bits = 64 if sys.maxsize > 2**32 else 32
    if (
        system not in SUPPORTED_SYSTEMS
        or machine not in SUPPORTED_MACHINES
        or python_bits != 64
    ):
        raise DemoError(
            "the strict frozen-evidence demo supports only Windows x86-64 "
            "and Linux x86-64; macOS and ARM do not yet have a compatible "
            "numerical evidence baseline "
            f"(detected: system={system}, machine={machine}, python={python_bits}-bit)"
        )
    return system


def _run_python_step(
    *,
    title: str,
    arguments: Sequence[str],
    repo_root: Path,
    environment: Mapping[str, str],
    show_output: bool = False,
) -> str:
    _section(title)
    completed = subprocess.run(
        [sys.executable, *arguments],
        cwd=repo_root,
        env=dict(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = completed.stdout.rstrip()
    if completed.returncode != 0:
        if output:
            print(output)
        raise DemoError(
            f"step [{title}] failed with Python exit code {completed.returncode}; "
            "the output directory will not be reused or overwritten"
        )
    if show_output and output:
        print(output)
    print("passed")
    return output


def _resolve_output(repo_root: Path, value: str | None) -> Path:
    artifacts_root = (repo_root / "artifacts").resolve()
    if value is None:
        suffix = uuid.uuid4().hex[:8]
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S%f")[:-3]
        output = artifacts_root / f"portfolio_demo_{timestamp}_{suffix}"
    else:
        candidate = Path(value)
        output = (
            candidate.resolve()
            if candidate.is_absolute()
            else (repo_root / candidate).resolve()
        )
    if output == artifacts_root or not output.is_relative_to(artifacts_root):
        raise DemoError("output must be a new child directory under artifacts")
    if output.exists():
        raise DemoError(f"output already exists; refusing to overwrite: {output}")
    return output


def _require_files(repo_root: Path) -> None:
    required = (
        repo_root / "pyproject.toml",
        repo_root / "data/synthetic_trial.csv",
        repo_root / "data/synthetic_trial_design.json",
        repo_root / "evals/tasks.jsonl",
        repo_root / "evals/tasks.linux-x86_64.jsonl",
        repo_root / "evals/phase6_agent_tasks.jsonl",
        repo_root / "evals/phase6_splits.json",
        repo_root / "scripts/verify_phase5_artifacts.py",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise DemoError("required demo files are missing: " + ", ".join(missing))
    if sys.version_info < (3, 12):
        raise DemoError("Python 3.12 or newer is required by the locked dependencies")


def _offline_environment(repo_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(NUMERICAL_ENVIRONMENT)
    environment["PYTHONPATH"] = str(repo_root / "src")
    for name in PROVIDER_CREDENTIAL_VARIABLES:
        environment.pop(name, None)
    return environment


def _verify_numerical_identity(
    repo_root: Path, environment: Mapping[str, str], *, system: str
) -> None:
    expected_evidence_id = CANONICAL_EVIDENCE_IDS[system]
    probe_environment = dict(environment)
    probe_environment["OPENBLAS_VERBOSE"] = "2"
    output = _run_python_step(
        title="Pin and verify Nehalem/x86-v2 numerical baseline",
        arguments=(
            "-c",
            "import numpy as np; np.linalg.svd(np.eye(4))",
        ),
        repo_root=repo_root,
        environment=probe_environment,
        show_output=True,
    )
    if re.search(r"(?im)^Core:\s*Nehalem\s*$", output) is None:
        raise DemoError(
            "the pinned Nehalem OpenBLAS kernel was not activated; "
            "refusing to generate incomparable evidence IDs"
        )

    identity_program = (
        "import json,pandas as pd; "
        "from pathlib import Path; "
        "from researchops.analysis_tools import run_ancova; "
        "from researchops.contracts import ResearchDesign; "
        "from researchops.data_quality import profile_csv; "
        "p=Path('data/synthetic_trial.csv'); "
        "f=pd.read_csv(p,encoding='utf-8-sig',low_memory=False); "
        "d=ResearchDesign.from_dict(json.loads(Path('data/synthetic_trial_design.json').read_text(encoding='utf-8'))); "
        "got=run_ancova(f,profile_csv(p),d).evidence_id; "
        "print(got); "
        f"raise SystemExit(0 if got=='{expected_evidence_id}' else 3)"
    )
    _run_python_step(
        title=f"Verify canonical {system} x86-64 ANCOVA evidence identity",
        arguments=("-c", identity_program),
        repo_root=repo_root,
        environment=environment,
        show_output=True,
    )


def _print_report(output: Path) -> None:
    report_path = output / "eval_report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise DemoError("the generated evaluation report is unreadable") from exc

    _section("Demo result")
    fields = (
        ("Tasks", report["task_count"]),
        ("Passed", report["passed_count"]),
        ("Failed", report["failed_count"]),
        ("Success rate", f"{float(report['success_rate']):.2%}"),
        (
            "Unexpected tool error rate",
            f"{float(report['unexpected_tool_error_rate']):.2%}",
        ),
        ("Safety violation rate", f"{float(report['safety_violation_rate']):.2%}"),
        (
            "Evidence citation accuracy",
            f"{float(report['evidence_citation_accuracy']):.2%}",
        ),
        ("Latency P50 ms", f"{float(report['p50_latency_ms']):.2f}"),
        ("Latency P95 ms", f"{float(report['p95_latency_ms']):.2f}"),
        ("Cost status", report["cost_status"]),
    )
    width = max(len(label) for label, _ in fields)
    for label, value in fields:
        print(f"{label:<{width}} : {value}")

    print("\nKey artifacts:")
    for name, label in (
        ("eval_summary.md", "summary"),
        ("eval_report.json", "metrics"),
        ("eval_manifest.json", "reproducibility manifest"),
        ("eval_results.jsonl", "per-task results"),
        ("eval_audit.sqlite3", "audit database"),
        ("eval_audit_index.json", "audit-chain index"),
    ):
        path = output / name
        if not path.is_file():
            raise DemoError(f"expected artifact is missing: {path}")
        print(f"- {label}: {path}")


def run_demo(output_directory: str | None) -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    system = _require_supported_platform()
    _require_files(repo_root)
    task_corpus = repo_root / "evals" / TASK_CORPUS_NAMES[system]
    quality_profile = QUALITY_PROFILES[system]
    output = _resolve_output(repo_root, output_directory)
    environment = _offline_environment(repo_root)

    print("ResearchOps Agent offline portfolio demo")
    print(f"Repository: {repo_root}")
    print(f"Output:     {output}")
    print(f"Corpus:     {task_corpus.name}")
    print(f"Profile:    {quality_profile}")
    print("Mode:       offline deterministic 50-task control-plane evaluation")
    print("Credentials: removed from child-process environment")

    _run_python_step(
        title="1/4 Validate Python environment",
        arguments=(
            "-c",
            "import sys; print('Python ' + sys.version.split()[0]); "
            "raise SystemExit(0 if sys.version_info >= (3, 12) else 2)",
        ),
        repo_root=repo_root,
        environment=environment,
        show_output=True,
    )
    _verify_numerical_identity(repo_root, environment, system=system)
    _run_python_step(
        title="2/4 Validate offline evaluation corpus",
        arguments=(
            "-m",
            "researchops.cli",
            "eval-validate",
            "--tasks",
            str(task_corpus),
        ),
        repo_root=repo_root,
        environment=environment,
        show_output=True,
    )
    _run_python_step(
        title="Additional check: Phase 6 behavior corpus and split",
        arguments=("-m", "researchops.cli", "phase6-validate"),
        repo_root=repo_root,
        environment=environment,
        show_output=True,
    )
    _run_python_step(
        title="3/4 Run frozen 50-task offline evaluation",
        arguments=(
            "-m",
            "researchops.cli",
            "eval-run",
            "--tasks",
            str(task_corpus),
            "--output-dir",
            str(output),
        ),
        repo_root=repo_root,
        environment=environment,
    )
    _run_python_step(
        title="4/4 Verify hashes, audit chains and redaction",
        arguments=(
            str(repo_root / "scripts/verify_phase5_artifacts.py"),
            str(output),
            "--quality-profile",
            quality_profile,
        ),
        repo_root=repo_root,
        environment=environment,
        show_output=True,
    )
    _print_report(output)
    print("\nOffline demo completed.")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the fully offline ResearchOps Agent portfolio demo without "
            "loading Provider credentials."
        )
    )
    parser.add_argument(
        "--output-dir",
        help="new output directory under artifacts (default: unique generated path)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        run_demo(arguments.output_dir)
    except (DemoError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"portfolio demo failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
