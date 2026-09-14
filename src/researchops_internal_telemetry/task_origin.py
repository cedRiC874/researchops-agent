"""Bounded exact-string exclusion over repository eval documents; not unseen proof."""
from __future__ import annotations
import json

from .contract import ROOT, DIRECTORY, decode, digest, read, require
from .source import source_files

ORIGIN_PATH = DIRECTORY + "/case_origin_v1.json"


def build_origin_record(root=ROOT):
    tasks_bytes=read(root/DIRECTORY/"cases_v1.json")
    cases=decode(tasks_bytes)["cases"]
    wanted={digest(case["input"].encode()):case["case_id"] for case in cases}
    compared, matches=[],set()
    for name in source_files(root):
        if not name.startswith("evals/") or name.startswith(DIRECTORY+"/") or not name.endswith((".json",".jsonl")):
            continue
        payload=read(root/name,8*1024*1024)
        compared.append(dict(path=name,sha256=digest(payload)))
        documents=[json.loads(line) for line in payload.decode("utf-8-sig").splitlines() if line.strip()] if name.endswith(".jsonl") else [json.loads(payload)]
        pending=[(document,0) for document in documents];count=0
        while pending:
            value,depth=pending.pop();count+=1
            require(count<=100000 and depth<=64,"origin_shape_limit")
            if type(value) is str:
                if digest(value.encode()) in wanted:matches.add(wanted[digest(value.encode())])
            elif type(value) is dict:pending.extend((child,depth+1) for child in value.values())
            elif type(value) is list:pending.extend((child,depth+1) for child in value)
    require(not matches,"old_task_exact_match")
    return dict(schema_version="provider-completion-internal-case-origin/1.0",tasks_sha256=digest(tasks_bytes),
        authored_by="internal_assistant",developer_known=True,external_unseen=False,external_review=False,
        target_provider_calls_for_authoring=0,selection_after_provider_observation=False,
        comparison_scope="repo-evals-json-jsonl-excluding-internal-v1",compared_documents=compared,
        exact_string_match_case_ids=sorted(matches),semantic_or_global_novelty_proven=False,
        human_final_task_approval_recorded=False)
