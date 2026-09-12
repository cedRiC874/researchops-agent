"""Preview, exclusively create, or verify v11/v12 source-only snapshots."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from researchops_external_closure import execution_components_v4 as source
from researchops_external_closure.execution_current_v4 import build_current_timed_profile_documents, verify_current_timed_profile
from researchops_external_closure.execution_current_v3 import _root, _directory
from researchops_external_closure.io import read_regular_file_no_follow


class _Parser(argparse.ArgumentParser):
    def error(self, message): raise ValueError('source_snapshot_arguments_invalid')


def _write_new(root, relative, payload, created):
    path = root / relative
    _root(path.parent)  # Existing required parent chain; never follow a reparse path.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    created.append(relative)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1: raise ValueError('source_snapshot_file_invalid')
        offset = 0
        while offset < len(payload):
            count = os.write(descriptor, payload[offset:])
            if type(count) is not int or count <= 0: raise ValueError('source_snapshot_write_failed')
            offset += count
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_nlink, after.st_size) != (before.st_dev, before.st_ino, 1, len(payload)):
            raise ValueError('source_snapshot_file_changed')
    finally:
        os.close(descriptor)
    if read_regular_file_no_follow(path, max_bytes=max(1, len(payload))) != payload:
        raise ValueError('source_snapshot_file_changed')


def main(arguments=None):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--project-root', type=Path, default=ROOT)
    parser.add_argument('--profile', choices=tuple(source.PROFILE_PATHS), default='first_live')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--write', action='store_true'); modes.add_argument('--verify', action='store_true')
    created = []
    try:
        args = parser.parse_args(arguments)
        root = _root(args.project_root)
        manifest_path, plan_path = source.PROFILE_PATHS[args.profile]
        if args.verify:
            checked = verify_current_timed_profile(root, profile=args.profile)
            summary = dict(status='valid_source_integrity_only', profile=checked.profile,
                implementation_commitment_sha256=checked.implementation_commitment_sha256,
                plan_commitment_sha256=checked.source_integrity_commitment_sha256, selected_file_count=checked.selected_file_count)
        else:
            if args.write and any(os.path.lexists(root / path) for path in (manifest_path, plan_path)):
                raise ValueError('source_artifact_already_exists')
            documents = build_current_timed_profile_documents(root, profile=args.profile)
            if args.write:
                with ExitStack() as guards:
                    if os.name == 'nt':
                        from researchops_completion_timing.local_claim import _locked_chain
                        for parent in sorted({(root / path).parent for path in (manifest_path, plan_path)}, key=str):
                            guards.enter_context(_locked_chain(parent))
                    identities = {parent: _directory(parent)[:2] for parent in {(root / manifest_path).parent, (root / plan_path).parent}}
                    for relative, payload in ((manifest_path, documents.manifest), (plan_path, documents.plan)):
                        _write_new(root, relative, payload, created)
                    if any(_directory(parent)[:2] != identity for parent, identity in identities.items()):
                        raise ValueError('source_snapshot_directory_changed')
                    verify_current_timed_profile(root, profile=args.profile)
            summary = dict(status='created_source_integrity_only' if args.write else 'preview_source_integrity_only', profile=args.profile,
                implementation_commitment_sha256=json.loads(documents.manifest)['commitment_sha256'],
                plan_commitment_sha256=json.loads(documents.plan)['plan_commitment_sha256'],
                manifest_bytes=len(documents.manifest), manifest_sha256=source._sha(documents.manifest),
                plan_bytes=len(documents.plan), plan_sha256=source._sha(documents.plan))
        summary.update(created_paths=created, source_recipe_version=4, online_execution_authorized=False,
            runtime_admission_verified=False, historical_result_revalidated=False, network_calls=0, model_calls=0)
        print(json.dumps(summary, sort_keys=True))
        return 0
    except Exception as error:
        code = getattr(error, 'code', 'source_snapshot_operation_failed')
        if type(error) is ValueError and len(error.args) == 1 and type(error.args[0]) is str and re.fullmatch(r'source_[a-z_]{1,110}', error.args[0]):
            code = error.args[0]
        if type(code) is not str or re.fullmatch(r'[a-z0-9_]{1,128}', code) is None: code = 'source_snapshot_operation_failed'
        print(json.dumps(dict(status='failed', error_code=code, created_paths=created, source_recipe_version=4,
            online_execution_authorized=False, runtime_admission_verified=False, network_calls=0, model_calls=0), sort_keys=True))
        return 2


if __name__ == '__main__': raise SystemExit(main())
