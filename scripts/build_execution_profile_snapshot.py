"""Preview, exclusively create, or verify v7/v8 source-only snapshots. No network."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))

from researchops_external_closure.execution_components_v2 import PROFILE_PATHS
from researchops_external_closure.execution_current_v2 import build_current_profile_documents,verify_current_profile


def main(arguments=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root",type=Path,default=ROOT)
    parser.add_argument("--profile",choices=tuple(PROFILE_PATHS),default="first_live")
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument("--write",action="store_true")
    modes.add_argument("--verify",action="store_true")
    args=parser.parse_args(arguments)
    created=[]
    try:
        root=args.project_root.resolve(strict=True)
        manifest_path,plan_path=PROFILE_PATHS[args.profile]
        if args.verify:
            result=verify_current_profile(root,profile=args.profile)
            summary={"status":"valid_source_integrity_only","profile":result.profile,
                     "implementation_commitment_sha256":result.implementation_commitment_sha256,
                     "plan_commitment_sha256":result.source_integrity_commitment_sha256,"selected_file_count":result.selected_file_count}
        else:
            documents=build_current_profile_documents(root,profile=args.profile)
            if args.write:
                for relative in (manifest_path,plan_path):
                    if (root/relative).exists():
                        raise ValueError("source_artifact_already_exists")
                for relative,payload in ((manifest_path,documents.manifest),(plan_path,documents.plan)):
                    path=root/relative
                    path.parent.mkdir(parents=True,exist_ok=True)
                    with path.open("xb") as stream:
                        # Track creation before write/close can fail. Preserve
                        # the partial artifact and name it in the failure receipt.
                        created.append(relative)
                        stream.write(payload)
            summary={"status":"created_source_integrity_only" if args.write else "preview_source_integrity_only","profile":args.profile,
                     "implementation_commitment_sha256":json.loads(documents.manifest)["commitment_sha256"],
                     "plan_commitment_sha256":json.loads(documents.plan)["plan_commitment_sha256"],
                     "manifest_bytes":len(documents.manifest),"manifest_sha256":hashlib.sha256(documents.manifest).hexdigest(),
                     "plan_bytes":len(documents.plan),"plan_sha256":hashlib.sha256(documents.plan).hexdigest()}
        summary.update(created_paths=created,online_execution_authorized=False,runtime_admission_verified=False,network_calls=0,model_calls=0)
        print(json.dumps(summary,indent=2))
        return 0
    except Exception as error:
        code=getattr(error,"code","source_snapshot_operation_failed")
        if type(error) is ValueError and error.args==("source_artifact_already_exists",):
            code="source_artifact_already_exists"
        print(json.dumps({"status":"failed","error_code":code,"created_paths":created,
                          "online_execution_authorized":False,"network_calls":0,"model_calls":0}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())
