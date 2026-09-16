"""Verify fixed Git blobs and actual import locations before invoking the scorer."""
import hashlib
import importlib
import inspect
import json
import re
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
COMMIT = "fcc2026c60943de6016495ad244291689a9d491d"
TREE = "cb4fd9421d6f25da121aefa51bb30244900f0a40"
SCHEMA = "internal-behavior-eval-v1.0"
REVISION = "internal-behavior-eval-v1.1"
REQUIRED_FIXED_FILES = frozenset({
    "src/researchops_behavior_eval_v1/__init__.py",
    "src/researchops_behavior_eval_v1/core.py",
    "src/researchops_behavior_eval_v1/__main__.py",
    "evals/internal_behavior_eval_v1/CONTRACT.md",
    "evals/internal_behavior_eval_v1/text_observation_v1_1/REVISION.md",
    "pyproject.toml", "requirements.lock", "requirements.linux.lock",
    "evals/provider_completion_internal_source_v2/source_manifest_v2.json",
})


class IdentityError(ValueError):
    pass


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(*args):
    return subprocess.check_output(["git", "-C", str(REPO), *args], stderr=subprocess.PIPE)


def validate_fixed_files(files):
    if type(files) is not dict or set(files) != REQUIRED_FIXED_FILES:
        raise IdentityError("required_fixed_file_set_mismatch")
    if any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in files.values()):
        raise IdentityError("fixed_file_digest_invalid")


def read_upstream():
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise IdentityError("duplicate_upstream_field")
            result[key] = value
        return result
    return json.loads((ROOT / "upstream.json").read_text(encoding="utf-8"), object_pairs_hook=unique)


def prepare():
    """Only before the process offline fence; no network Git commands."""
    upstream = read_upstream()
    validate_fixed_files(upstream.get("fixed_files_sha256"))
    if upstream.get("scorer_commit") != COMMIT or upstream.get("scorer_tree") != TREE:
        raise IdentityError("upstream_anchor_mismatch")
    if git("rev-parse", "HEAD").decode().strip() != COMMIT or git("rev-parse", "HEAD^{tree}").decode().strip() != TREE:
        raise IdentityError("fixed_head_or_tree_mismatch")
    for name, expected in upstream["fixed_files_sha256"].items():
        blob = git("show", COMMIT + ":" + name)
        if hashlib.sha256(blob).hexdigest() != expected or sha(REPO / name) != expected:
            raise IdentityError("fixed_blob_mismatch:" + name)
    if upstream["scorer_commit"] != COMMIT:
        raise IdentityError("upstream_anchor_mismatch")
    return {"scorer_commit": COMMIT, "scorer_tree": TREE,
            "files_sha256": upstream["fixed_files_sha256"], "git_identity_verified": True}


def check_files(receipt):
    validate_fixed_files(receipt.get("files_sha256"))
    if receipt["scorer_commit"] != COMMIT or receipt["scorer_tree"] != TREE or receipt["git_identity_verified"] is not True:
        raise IdentityError("identity_receipt_mismatch")
    for name, expected in receipt["files_sha256"].items():
        if sha(REPO / name) != expected:
            raise IdentityError("fixed_file_changed:" + name)


def check_module_locations():
    for name, module in list(sys.modules.items()):
        if name == "researchops_behavior_eval_v1" or name.startswith("researchops_behavior_eval_v1."):
            path = Path(getattr(module, "__file__", "")).resolve()
            if not path.is_relative_to(REPO / "src/researchops_behavior_eval_v1"):
                raise IdentityError("foreign_scorer_module:" + name)
        if name == "services.agent_workflow_comparison_v1" or name.startswith("services.agent_workflow_comparison_v1."):
            path = Path(getattr(module, "__file__", "")).resolve()
            if not path.is_relative_to(REPO / "services/agent_workflow_comparison_v1"):
                raise IdentityError("foreign_comparison_module:" + name)


def load(receipt):
    check_files(receipt)
    check_module_locations()
    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.find_spec("researchops_behavior_eval_v1")
    if spec is None or Path(spec.origin).resolve() != REPO / "src/researchops_behavior_eval_v1/__init__.py":
        raise IdentityError("scorer_import_resolution_mismatch")
    package = importlib.import_module("researchops_behavior_eval_v1")
    core = importlib.import_module("researchops_behavior_eval_v1.core")
    check_module_locations()
    if core.VERSION != SCHEMA or core.MEASUREMENT_REVISION != REVISION:
        raise IdentityError("scorer_version_mismatch")
    expected = {"parse_answer": ["text"], "score_case": ["contract", "observation", "allowed_evidence"], "score_plan": ["plan"]}
    for name, parameters in expected.items():
        function = getattr(package, name)
        if list(inspect.signature(function).parameters) != parameters or function is not getattr(core, name):
            raise IdentityError("public_signature_or_export_mismatch:" + name)
    return package, {**receipt, "schema_version": SCHEMA, "measurement_revision": REVISION,
                     "import_file": str(Path(package.__file__).resolve()),
                     "core_import_file": str(Path(core.__file__).resolve()), "public_signatures": expected}
