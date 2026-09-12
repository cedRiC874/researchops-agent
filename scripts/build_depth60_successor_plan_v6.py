"""Preview, create once, or verify the T6-C implementation/v6 source artifacts.

Neither output grants runtime authority or validates first-live/registry review.
Existing outputs and all historical plans are never overwritten or deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from researchops_external_closure.execution_binding import (
    build_current_execution_component_documents,
    verify_current_execution_components,
)
from researchops_external_closure.execution_components import MANIFEST_PATH, V6_PLAN_PATH


def _safe_target(root: Path, relative: str) -> Path:
    parent = root
    parts = relative.split("/")
    for component in parts[:-1]:
        parent = parent / component
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or int(getattr(info, "st_file_attributes", 0)) & 0x400:
            raise ValueError("source_artifact_parent_invalid")
    target = root / relative
    if target.exists() or target.is_symlink():
        raise FileExistsError("source_artifact_already_exists")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Create both absent artifacts once.")
    mode.add_argument("--verify", action="store_true", help="Recompute existing artifacts without writing.")
    args = parser.parse_args(argv)
    created: list[str] = []
    try:
        root = args.project_root.resolve(strict=True)
        if args.verify:
            proof = verify_current_execution_components(root)
            output = {
                "status": "valid_source_integrity_only",
                "implementation_commitment_sha256": proof.implementation_commitment_sha256,
                "plan_commitment_sha256": proof.source_integrity_commitment_sha256,
                "component_hashes": dict(proof.component_hashes),
                "verified_file_count": proof.file_count,
            }
        else:
            targets = (
                [_safe_target(root, relative) for relative in (MANIFEST_PATH, V6_PLAN_PATH)]
                if args.write else []
            )
            docs = build_current_execution_component_documents(root)
            payloads = (docs.implementation_manifest, docs.source_integrity_v6_plan)
            if args.write:
                for relative, target, raw in zip((MANIFEST_PATH, V6_PLAN_PATH), targets, payloads):
                    # Check both targets before the first write, then use an
                    # exclusive create to prevent overwriting a raced-in file.
                    _safe_target(root, relative)
                    with target.open("xb") as stream:
                        # The exclusive open has already created this file.
                        # A later write/close failure must not report zero files.
                        created.append(relative)
                        stream.write(raw)
                verify_current_execution_components(root)
            manifest = json.loads(docs.implementation_manifest)
            plan = json.loads(docs.source_integrity_v6_plan)
            output = {
                "status": "created_source_integrity_candidate" if args.write else "source_integrity_preview",
                "implementation_manifest": {
                    "path": MANIFEST_PATH, "bytes": len(payloads[0]),
                    "sha256": hashlib.sha256(payloads[0]).hexdigest(),
                    "commitment_sha256": manifest["commitment_sha256"],
                },
                "source_integrity_v6": {
                    "path": V6_PLAN_PATH, "bytes": len(payloads[1]),
                    "sha256": hashlib.sha256(payloads[1]).hexdigest(),
                    "plan_commitment_sha256": plan["plan_commitment_sha256"],
                },
                "verified_file_count": len(manifest["file_inventory"]),
            }
        output.update({
            "online_execution_authorized": False,
            "runtime_admission_verified": False,
            "network_calls": 0,
            "model_calls": 0,
            "historical_result_revalidated": False,
            "historical_plans_unchanged": True,
            "created_paths": created,
        })
        print(json.dumps(output, ensure_ascii=True, indent=2))
        return 0
    except Exception as error:
        code = (
            "source_artifact_already_exists" if isinstance(error, FileExistsError)
            else getattr(error, "code", "source_artifact_generation_failed")
        )
        print(json.dumps({
            "status": "failed", "error_code": code, "created_paths": created,
            "online_execution_authorized": False, "network_calls": 0, "model_calls": 0,
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
