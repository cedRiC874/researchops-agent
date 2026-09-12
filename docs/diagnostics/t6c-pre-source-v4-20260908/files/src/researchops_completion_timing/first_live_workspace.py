"""Create-once work database with Windows path handles held for its lifetime."""
from __future__ import annotations

import ctypes
import os
import re
import stat
from contextlib import contextmanager

from .local_claim import _kernel, _locked_chain


@contextmanager
def _new_database(path):
    # CREATE_NEW is exclusive; the handle denies DELETE sharing, including
    # rename/replacement. READ/WRITE sharing permits SQLite's own connections.
    import msvcrt
    kernel = _kernel()
    handle = kernel.CreateFileW(str(path), 0xC0000000, 0x3, None, 1, 0x00200000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ValueError("first_live_work_database_create_failed")
    descriptor = None
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        handle = None  # descriptor now owns this handle; never close it twice.
        initial = os.fstat(descriptor)
        def check():
            held, named = os.fstat(descriptor), path.lstat()
            if any(not stat.S_ISREG(item.st_mode) or item.st_nlink != 1 or
                   getattr(item, 'st_file_attributes', 0) & 0x400 for item in (held, named)):
                raise ValueError("first_live_work_database_changed")
            if (held.st_dev, held.st_ino) != (initial.st_dev, initial.st_ino) or (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino):
                raise ValueError("first_live_work_database_changed")
        check()
        os.fsync(descriptor)
        yield path
        check()
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        elif handle is not None and not kernel.CloseHandle(handle):
            raise ValueError("first_live_work_database_close_unknown")


@contextmanager
def _owned_work_paths(root, identifier):
    """No deletion or recovery; exceptions retain even a newly empty database."""
    if type(identifier) is not str or re.fullmatch(r"(?!0{64}$)[0-9a-f]{64}", identifier) is None:
        raise ValueError("first_live_work_identifier_invalid")
    output = root / 'output'
    parent = output / 'first-live-v4'
    work, archive = parent / (identifier + '.work'), parent / identifier
    envelope = parent / (identifier + '.bundle.json')
    with _locked_chain(root):
        output.mkdir(exist_ok=True)
        with _locked_chain(output):
            parent.mkdir(exist_ok=True)
            with _locked_chain(parent):
                if os.path.lexists(archive) or os.path.lexists(envelope):
                    raise FileExistsError("first-live output exists")
                work.mkdir(exist_ok=False)
                with _locked_chain(work), _new_database(work / 'audit.sqlite3') as database:
                    yield database, archive, envelope
