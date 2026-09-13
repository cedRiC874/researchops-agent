"""320-path successor using one bounded legacy object reader/cache/deadline."""
from __future__ import annotations

from collections.abc import Sequence
import hashlib
from pathlib import Path
import re

from . import git_objects as old

MAX_PATHS=320


def read_git_object_snapshot(repository,commit_oid,required_paths,*,expected_tree_oid=None):
    if not isinstance(repository,Path): old._fail('repository_invalid')
    commit_oid=old._oid(commit_oid)
    expected=old._oid(expected_tree_oid) if expected_tree_oid is not None else None
    if (not isinstance(required_paths,Sequence) or isinstance(required_paths,(str,bytes,bytearray))
        or not required_paths or len(required_paths)>MAX_PATHS):
        old._fail('paths_invalid')
    paths=tuple(old._path(path) for path in required_paths)
    if len({path.casefold() for path in paths})!=len(paths): old._fail('path_collision')
    wanted=set(paths); reader=old._Reader(repository)
    commit=reader.read(commit_oid,'commit')
    headers,separator,_=commit.partition(b'\n\n'); lines=headers.split(b'\n')
    if (not separator or not lines or re.fullmatch(rb'tree [0-9a-f]{40}',lines[0]) is None
        or any(line.startswith(b'tree ') for line in lines[1:])): old._fail('commit_invalid')
    tree_oid=lines[0][5:].decode('ascii')
    if expected is not None and tree_oid!=expected: old._fail('tree_mismatch')
    pending=[('',tree_oid)]; trees=[]; entries=[]; selected={}; entry_count=selected_bytes=0
    while pending:
        prefix,oid=pending.pop()
        trees.append(old.GitTreeSummary(path=prefix,oid=oid))
        for mode,name,entry_oid in old._tree_entries(reader.read(oid,'tree')):
            entry_count+=1
            if entry_count>old._MAX_TREE_ENTRIES: old._fail('tree_entry_limit')
            path=old._path(prefix+'/'+name if prefix else name)
            entries.append(old.GitEntrySummary(path=path,mode=mode,oid=entry_oid))
            if mode=='40000': pending.append((path,entry_oid))
            elif path in wanted:
                payload=reader.read(entry_oid,'blob')
                selected_bytes+=len(payload)
                if selected_bytes>old._MAX_TOTAL_BYTES: old._fail('selected_total_bytes_limit')
                selected[path]=old.GitBlobSnapshot(path=path,mode=mode,oid=entry_oid,
                    sha256=hashlib.sha256(payload).hexdigest(),payload=payload)
    if set(selected)!=wanted: old._fail('required_path_missing')
    return old.GitObjectSnapshot(commit_oid=commit_oid,tree_oid=tree_oid,
        trees=tuple(sorted(trees,key=lambda value:value.path)),entries=tuple(sorted(entries,key=lambda value:value.path)),
        blobs=tuple(selected[path] for path in paths),objects_read=len(reader.cache),total_payload_bytes=reader.total_bytes)
