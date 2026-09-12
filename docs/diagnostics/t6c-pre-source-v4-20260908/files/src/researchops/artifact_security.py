from __future__ import annotations

import os
import subprocess
from pathlib import Path


class ArtifactPermissionError(RuntimeError):
    """Raised when a published artifact would retain sandbox-only permissions."""


def enable_parent_acl_inheritance(path: str | Path) -> None:
    """Make an atomic-publication staging tree inherit its parent's Windows ACL.

    A directory rename preserves the staging directory's security descriptor on
    Windows. Sandboxed staging directories can therefore remain readable only
    by the sandbox identity after publication unless inheritance is restored
    before the rename.
    """

    if os.name != "nt":
        return
    target = Path(path).resolve()
    if not target.exists():
        raise ArtifactPermissionError("权限修复目标不存在。")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["icacls", str(target), "/inheritance:e", "/T", "/C", "/Q"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise ArtifactPermissionError("无法启动 Windows ACL 修复工具。") from exc
    if completed.returncode != 0:
        raise ArtifactPermissionError("无法恢复产物目录的父级 ACL 继承。")
