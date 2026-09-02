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


_SOURCE_BUNDLE_V2_DOMAIN = b"researchops-phase6-depth60-source-bundle-v2\0"

SOURCE_BUNDLE_ALGORITHMS = ("v1", "v2")
DEFAULT_SOURCE_BUNDLE_ALGORITHM = "v1"


def _local_import_parts(
    tree: ast.AST, package_parts: tuple[str, ...]
) -> set[tuple[str, ...]]:
    """Return local import targets as ``researchops``-relative path tuples.

    Unlike :func:`_local_import_targets` this keeps dotted paths intact so a
    submodule inside a subpackage can be resolved, and it resolves relative
    imports against the package that actually contains the module.
    """

    targets: set[tuple[str, ...]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module == "researchops":
                    targets.update(
                        (alias.name.split(".", 1)[0],) for alias in node.names
                    )
                elif node.module and node.module.startswith("researchops."):
                    module_parts = tuple(node.module.split(".")[1:])
                    targets.add(module_parts)
                    targets.update(
                        module_parts + tuple(alias.name.split("."))
                        for alias in node.names
                        if alias.name != "*"
                    )
                continue
            cut = len(package_parts) - (node.level - 1)
            if cut < 0:
                raise ValueError(
                    "Depth-60 source dependency escapes the researchops package"
                )
            base = package_parts[:cut]
            if node.module:
                module_parts = base + tuple(node.module.split("."))
                targets.add(module_parts)
                targets.update(
                    module_parts + tuple(alias.name.split("."))
                    for alias in node.names
                    if alias.name != "*"
                )
            else:
                targets.update(
                    base + (alias.name.split(".", 1)[0],) for alias in node.names
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("researchops."):
                    targets.add(tuple(alias.name.split(".")[1:]))
    return targets


def _resolve_import_parts(source_root: Path, parts: tuple[str, ...]) -> list[str]:
    """Resolve one import target to the relative files it contributes.

    A target that is neither a module file nor a package directory contributes
    no target file beyond any executable parent initializers. A package
    *directory* contributes its files instead of being skipped silently.
    """

    if not parts:
        return []
    names: set[str] = set()
    for depth in range(1, len(parts)):
        package_root = source_root.joinpath(*parts[:depth])
        if package_root.is_symlink():
            raise ValueError(
                "Depth-60 source dependency package is a symlink: "
                f"{'/'.join(parts[:depth])}"
            )
        initializer = package_root / "__init__.py"
        if initializer.is_symlink():
            raise ValueError(
                "Depth-60 source dependency is a symlink: "
                f"{'/'.join(parts[:depth])}/__init__.py"
            )
        if initializer.is_file():
            names.add("/".join(parts[:depth]) + "/__init__.py")
    directory = source_root.joinpath(*parts[:-1])
    package_root = directory / parts[-1]
    if package_root.is_symlink():
        raise ValueError(
            "Depth-60 source dependency package is a symlink: "
            f"{'/'.join(parts)}"
        )
    package_initializer = package_root / "__init__.py"
    if package_initializer.is_symlink():
        raise ValueError(
            "Depth-60 source dependency is a symlink: "
            f"{'/'.join(parts)}/__init__.py"
        )
    if package_initializer.is_file():
        for path in sorted(package_root.rglob("*.py")):
            if path.is_symlink():
                raise ValueError(
                    "Depth-60 source dependency is a symlink: "
                    f"{path.relative_to(source_root).as_posix()}"
                )
            names.add(path.relative_to(source_root).as_posix())
        return sorted(names)
    module_path = directory / f"{parts[-1]}.py"
    if module_path.is_symlink():
        raise ValueError(
            f"Depth-60 source dependency is a symlink: {'/'.join(parts)}.py"
        )
    if module_path.is_file():
        names.add("/".join(parts) + ".py")
    return sorted(names)


def phase6_depth60_source_files_v2(project_root: str | Path) -> tuple[str, ...]:
    """Return the v2 dependency closure for the Depth-60 entrypoint.

    v2 preserves dotted targets, includes executable parent initializers and
    namespace-package fromlist children, conservatively binds every ``.py`` in
    a directly imported package, and rejects relative imports that escape
    ``researchops``. The current repository tree still yields the same file set
    under both algorithms; domain separation keeps their digests distinct.
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
        package_parts = tuple(relative_name.split("/")[:-1])
        for parts in _local_import_parts(tree, package_parts):
            for name in _resolve_import_parts(source_root, parts):
                if name not in visited:
                    pending.add(name)
    return tuple(sorted(visited))


def phase6_depth60_source_bundle_sha256_v2(project_root: str | Path) -> str:
    root = Path(project_root).resolve()
    source_root = (root / "src/researchops").resolve()
    digest = hashlib.sha256()
    digest.update(_SOURCE_BUNDLE_V2_DOMAIN)
    for relative_name in phase6_depth60_source_files_v2(root):
        path = source_root / relative_name
        digest.update(relative_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _resolve_algorithm(algorithm: str | None) -> str:
    resolved = DEFAULT_SOURCE_BUNDLE_ALGORITHM if algorithm is None else algorithm
    if resolved not in SOURCE_BUNDLE_ALGORITHMS:
        raise ValueError(
            f"Unknown Depth-60 source bundle algorithm: {resolved!r}"
        )
    return resolved


def phase6_depth60_source_files_for(
    project_root: str | Path, algorithm: str | None = None
) -> tuple[str, ...]:
    if _resolve_algorithm(algorithm) == "v1":
        return phase6_depth60_source_files(project_root)
    return phase6_depth60_source_files_v2(project_root)


def phase6_depth60_source_bundle_sha256_for(
    project_root: str | Path, algorithm: str | None = None
) -> str:
    if _resolve_algorithm(algorithm) == "v1":
        return phase6_depth60_source_bundle_sha256(project_root)
    return phase6_depth60_source_bundle_sha256_v2(project_root)


__all__ = [
    "DEFAULT_SOURCE_BUNDLE_ALGORITHM",
    "SOURCE_BUNDLE_ALGORITHMS",
    "phase6_depth60_source_bundle_sha256",
    "phase6_depth60_source_bundle_sha256_for",
    "phase6_depth60_source_bundle_sha256_v2",
    "phase6_depth60_source_files",
    "phase6_depth60_source_files_for",
    "phase6_depth60_source_files_v2",
]
