"""Private fixed campaign work paths; ownership/authorization remains external."""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path

from .first_live_workspace import _new_database
from .local_claim import _locked_chain, _kernel


@contextmanager
def _owned_campaign_work_paths(root, authorization_id_sha256):
    """Hold DB and ancestor identity until the caller finishes phase/publication.

    The future public owner supplies its fixed code root and verified grant ID;
    this private filesystem helper accepts no Key and does not prove consumption.
    Failed/partial outputs stay in place; there is no recovery or deletion.
    """
    if (type(authorization_id_sha256) is not str
        or re.fullmatch(r'(?!0{64}$)[0-9a-f]{64}',authorization_id_sha256) is None):
        raise ValueError('campaign_work_identifier_invalid')
    if (type(root) is not type(Path()) or not root.is_absolute() or '..' in root.parts
        or os.name != 'nt' or len(root.drive) != 2 or root.drive[1] != ':'
        or any(':' in part for part in root.parts[1:]) or _kernel().GetDriveTypeW(root.anchor) != 3):
        raise ValueError('campaign_work_root_invalid')
    output = root/'output'; parent = output/'campaign-v1'
    work = parent/(authorization_id_sha256+'.work')
    archive = parent/authorization_id_sha256
    summary = parent/(authorization_id_sha256+'.invocation.json')
    with _locked_chain(root):
        output.mkdir(exist_ok=True)
        with _locked_chain(output):
            parent.mkdir(exist_ok=True)
            with _locked_chain(parent):
                if os.path.lexists(archive) or os.path.lexists(summary):
                    raise FileExistsError('campaign output exists')
                work.mkdir(exist_ok=False)
                with _locked_chain(work), _new_database(work/'phase6_audit.sqlite3') as database:
                    yield database, archive, summary
