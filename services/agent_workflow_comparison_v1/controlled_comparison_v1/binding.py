import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
EXECUTION_BASE = "dfb568d26f0745e397f6cc06dc020ad1ee870de2"
SCORER = "fcc2026c60943de6016495ad244291689a9d491d"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def source_snapshot():
    files = [p for p in ROOT.rglob("*") if p.is_file() and not {"outputs", "scoring"}.intersection(p.relative_to(ROOT).parts)
             and "__pycache__" not in p.parts and p.name != "delivery-manifest.json" and p.suffix in {".py", ".json", ".md"}]
    files += [REPO / n for n in ("requirements.lock", "requirements.linux.lock", "pyproject.toml",
        "src/researchops_behavior_eval_v1/__init__.py", "src/researchops_behavior_eval_v1/core.py",
        "src/researchops_behavior_eval_v1/__main__.py", "evals/internal_behavior_eval_v1/CONTRACT.md",
        "evals/internal_behavior_eval_v1/text_observation_v1_1/REVISION.md")]
    files += [REPO / "services/agent_workflow_comparison_v1" / n for n in (
        "__init__.py", "core.py", "aggregate_read_v1/__init__.py", "aggregate_read_v1/core.py")]
    from ..controlled_publication_v1.binding import runtime_snapshot
    return {**{p.relative_to(REPO).as_posix(): sha(p) for p in sorted(set(files))}, **runtime_snapshot()}


def verify_snapshot(snapshot):
    if source_snapshot() != snapshot:
        from .budget import StopRun
        raise StopRun("source_drift")


def reserve_output(path, run_id, snapshot):
    path = Path(path).resolve()
    if path.parent != (ROOT / "outputs").resolve() or path.name != run_id or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", run_id) is None:
        raise ValueError("isolated_output_required")
    # Atomic mkdir + exclusive owner record; no real claim store or fake production approval.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(parents=False, exist_ok=False)
    with (path / "offline-owner.json").open("x", encoding="utf-8") as f:
        json.dump({"kind": "offline_test_ownership", "run_id": run_id,
                   "source_snapshot_sha256": digest(snapshot), "online_authorized": False}, f)
    return path


def seal(path, names):
    if any(Path(n).name != n for n in names):
        raise ValueError("artifact_path_invalid")
    record = {"kind": "offline_artifact_manifest", "files": {n: sha(path / n) for n in names}}
    with (path / "sealed.json").open("x", encoding="utf-8") as f:
        json.dump(record, f, sort_keys=True)
    return {**record, "manifest_sha256": sha(path / "sealed.json")}


def check_seal(path, expected_sha256):
    if sha(path / "sealed.json") != expected_sha256:
        raise ValueError("seal_manifest_tampering")
    record = json.loads((path / "sealed.json").read_text(encoding="utf-8"))
    if any(Path(n).name != n or sha(path / n) != value for n, value in record["files"].items()):
        raise ValueError("artifact_tampering")
    return True
