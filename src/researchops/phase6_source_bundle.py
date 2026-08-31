from __future__ import annotations

import ast
import hashlib
from pathlib import Path


_ENTRYPOINT_FILES = frozenset(
    {
        "__init__.py",
        "cli.py",
        "phase6_agent.py",
        "phase6_depth60.py",
        "phase6_eval.py",
        "phase6_runner.py",
        "phase6_source_bundle.py",
        "tool_runtime.py",
    }
)


def phase6_depth60_source_files(project_root: str | Path) -> tuple[str, ...]:
    """Return the local Python dependency closure for the Depth-60 entrypoint.

    New, unimported Provider modules do not invalidate an already frozen plan.
    Any edit or new local import reachable from a bound entrypoint does.
    """

    root = Path(project_root).resolve()
    source_root = (root / "src/researchops").resolve()
    pending = set(_ENTRYPOINT_FILES)
    visited: set[str] = set()
    while pending:
        relative_name = min(pending)
        pending.remove(relative_name)
        if relative_name in visited:
            continue
        path = source_root / relative_name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Depth-60 source dependency missing: {relative_name}")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative_name)
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise ValueError(
                f"Depth-60 source dependency is unreadable: {relative_name}"
            ) from exc
        visited.add(relative_name)
        for dependency in _local_import_targets(tree):
            candidate = source_root / dependency
            if candidate.is_file() and dependency not in visited:
                pending.add(dependency)
    return tuple(sorted(visited))


def phase6_depth60_source_bundle_sha256(project_root: str | Path) -> str:
    root = Path(project_root).resolve()
    source_root = (root / "src/researchops").resolve()
    digest = hashlib.sha256()
    for relative_name in phase6_depth60_source_files(root):
        path = source_root / relative_name
        digest.update(relative_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _local_import_targets(tree: ast.AST) -> set[str]:
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                targets.add(node.module.split(".", 1)[0] + ".py")
            else:
                targets.update(alias.name.split(".", 1)[0] + ".py" for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module == "researchops":
                targets.update(alias.name.split(".", 1)[0] + ".py" for alias in node.names)
            elif node.module and node.module.startswith("researchops."):
                targets.add(node.module.split(".", 1)[1].split(".", 1)[0] + ".py")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("researchops."):
                    targets.add(alias.name.split(".", 1)[1].split(".", 1)[0] + ".py")
    return targets


__all__ = [
    "phase6_depth60_source_bundle_sha256",
    "phase6_depth60_source_files",
]
