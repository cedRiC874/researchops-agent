from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath


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


_COMPLETION_TELEMETRY_RUNTIME_BUNDLE_DOMAIN = (
    b"researchops-phase6-completion-telemetry-runtime-bundle-v1\0"
)
_COMPLETION_TELEMETRY_CONTRACT_BUNDLE_DOMAIN = (
    b"researchops-phase6-completion-telemetry-contract-bundle-v1\0"
)
_COMPLETION_TELEMETRY_RUNTIME_ROOT = PurePosixPath(
    "src/researchops_completion_telemetry"
)
_COMPLETION_TELEMETRY_V1_ROOT = PurePosixPath(
    "evals/provider_completion_telemetry_v1"
)
_COMPLETION_TELEMETRY_V2_ROOT = PurePosixPath(
    "evals/provider_completion_telemetry_v2"
)
_COMPLETION_TELEMETRY_V2_MANIFEST = (
    _COMPLETION_TELEMETRY_V2_ROOT / "fixture_manifest_v2.json"
)
_COMPLETION_TELEMETRY_FIXED_CONTRACT_FILES = (
    _COMPLETION_TELEMETRY_V1_ROOT
    / "provider_completion_record_contract_v1.json",
    _COMPLETION_TELEMETRY_V1_ROOT
    / "schemas/provider_completion_record_v1.schema.json",
    _COMPLETION_TELEMETRY_V1_ROOT / "provider_completion_mapping_v1.json",
    _COMPLETION_TELEMETRY_V2_MANIFEST,
)


def _safe_repository_relative_path(value: object, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} is not a safe repository-relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"{label} is not a safe repository-relative path")
    return relative


def _checked_repository_file(root: Path, relative: PurePosixPath) -> Path:
    lexical = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(
                "Completion telemetry integrity input is a symlink: "
                f"{relative.as_posix()}"
            )
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise ValueError(
            "Completion telemetry integrity input is missing or unreadable: "
            f"{relative.as_posix()}"
        ) from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(
            "Completion telemetry integrity input escapes or is not a file: "
            f"{relative.as_posix()}"
        )
    return resolved


def _strict_json_object(path: Path, *, label: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains a duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value: {item}")
            ),
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def completion_telemetry_runtime_files(project_root: str | Path) -> tuple[str, ...]:
    """Return every Python file in the first-party telemetry sibling package."""

    root = Path(project_root).resolve()
    source_relative = _COMPLETION_TELEMETRY_RUNTIME_ROOT
    source_lexical = root.joinpath(*source_relative.parts)
    current = root
    for part in source_relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Completion telemetry runtime package is a symlink")
    try:
        source_root = source_lexical.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Completion telemetry runtime package is missing") from exc
    if not source_root.is_relative_to(root) or not source_root.is_dir():
        raise ValueError("Completion telemetry runtime package escapes the repository")

    names: list[str] = []
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink():
            raise ValueError(
                "Completion telemetry runtime package contains a symlink"
            )
        if path.is_file() and path.suffix == ".py":
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(source_root):
                raise ValueError(
                    "Completion telemetry runtime source escapes its package"
                )
            names.append(resolved.relative_to(root).as_posix())
    if not names:
        raise ValueError("Completion telemetry runtime package has no Python files")
    if len(names) != len(set(names)):
        raise ValueError("Completion telemetry runtime source list contains duplicates")
    return tuple(sorted(names))


def _v2_contract_manifest_paths(root: Path) -> set[PurePosixPath]:
    manifest_path = _checked_repository_file(
        root, _COMPLETION_TELEMETRY_V2_MANIFEST
    )
    manifest = _strict_json_object(
        manifest_path, label="completion telemetry v2 manifest"
    )
    if set(manifest) != {
        "schema_version",
        "status",
        "registry",
        "fixtures",
        "summary",
    } or manifest.get("schema_version") != (
        "provider-completion-surface-fixture-manifest/2.0"
    ):
        raise ValueError("Completion telemetry v2 manifest shape is invalid")
    registry = manifest.get("registry")
    fixtures = manifest.get("fixtures")
    if not isinstance(registry, dict) or set(registry) != {
        "file",
        "bytes",
        "sha256",
    }:
        raise ValueError("Completion telemetry v2 registry commitment is invalid")
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("Completion telemetry v2 fixture manifest is empty")

    paths: set[PurePosixPath] = set()
    registry_child = _safe_repository_relative_path(
        registry.get("file"), label="completion telemetry v2 registry"
    )
    registry_relative = _COMPLETION_TELEMETRY_V2_ROOT / registry_child
    paths.add(registry_relative)
    seen_fixture_ids: set[str] = set()
    for index, item in enumerate(fixtures):
        if not isinstance(item, dict) or set(item) != {
            "fixture_id",
            "file",
            "bytes",
            "sha256",
            "fixture_kind",
        }:
            raise ValueError(
                f"Completion telemetry v2 fixture commitment {index} is invalid"
            )
        fixture_id = item.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id or fixture_id in seen_fixture_ids:
            raise ValueError("Completion telemetry v2 fixture ID is invalid or repeated")
        seen_fixture_ids.add(fixture_id)
        child = _safe_repository_relative_path(
            item.get("file"), label="completion telemetry v2 fixture"
        )
        relative = _COMPLETION_TELEMETRY_V2_ROOT / child
        if relative in paths:
            raise ValueError("Completion telemetry v2 artifact path is repeated")
        paths.add(relative)

    registry_path = _checked_repository_file(root, registry_relative)
    registry_value = _strict_json_object(
        registry_path, label="completion telemetry v2 registry"
    )
    predecessor = registry_value.get("predecessor_mapping")
    entries = registry_value.get("entries")
    expected_v1_mapping = (
        _COMPLETION_TELEMETRY_V1_ROOT / "provider_completion_mapping_v1.json"
    )
    if (
        registry_value.get("schema_version")
        != "provider-completion-surface-registry/2.0"
        or not isinstance(predecessor, dict)
        or predecessor.get("relative_path") != expected_v1_mapping.as_posix()
        or not isinstance(entries, list)
        or not entries
    ):
        raise ValueError("Completion telemetry v2 registry lineage is invalid")
    probe_paths: set[PurePosixPath] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Completion telemetry v2 registry entry is invalid")
        source = entry.get("source")
        if not isinstance(source, dict) or "relative_path" not in source:
            continue
        if source.get("kind") != "sanitized_live_probe_receipt":
            raise ValueError("Completion telemetry local source kind is unsupported")
        probe = _safe_repository_relative_path(
            source.get("relative_path"), label="completion telemetry probe receipt"
        )
        probe_paths.add(probe)
    if PurePosixPath("probe_out_v3.json") not in probe_paths:
        raise ValueError("Completion telemetry v3 probe receipt is not bound")
    paths.update(probe_paths)
    return paths


def completion_telemetry_contract_files(project_root: str | Path) -> tuple[str, ...]:
    """Return the fail-closed manifest-driven telemetry contract inputs."""

    root = Path(project_root).resolve()
    # The component digest is an integrity commitment to the exact artifacts
    # consumed by the runtime, not a way to legitimize an internally
    # inconsistent manifest.  Reuse the runtime's strict loader before
    # enumerating names: it verifies the manifest commitments, registry and v1
    # lineage, every fixture derivation, the bound live probe, and the record
    # schema.  Keep the import local so the historical v1/v2 source-closure
    # algorithms and their module-import behavior remain unchanged.
    try:
        from researchops_completion_telemetry.surface_mapping import (
            load_verified_surface_registry,
        )

        load_verified_surface_registry(root)
    except Exception as exc:
        raise ValueError(
            "Completion telemetry contract inputs failed strict runtime verification"
        ) from exc
    paths = set(_COMPLETION_TELEMETRY_FIXED_CONTRACT_FILES)
    paths.update(_v2_contract_manifest_paths(root))
    for relative in paths:
        _checked_repository_file(root, relative)
    return tuple(sorted(relative.as_posix() for relative in paths))


def _completion_bundle_sha256(
    project_root: str | Path,
    *,
    domain: bytes,
    relative_names: tuple[str, ...],
) -> str:
    root = Path(project_root).resolve()
    digest = hashlib.sha256()
    digest.update(domain)
    for relative_name in relative_names:
        relative = _safe_repository_relative_path(
            relative_name, label="completion telemetry integrity input"
        )
        path = _checked_repository_file(root, relative)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError(
                "Completion telemetry integrity input is unreadable: "
                f"{relative.as_posix()}"
            ) from exc
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def completion_telemetry_runtime_bundle_sha256(project_root: str | Path) -> str:
    return _completion_bundle_sha256(
        project_root,
        domain=_COMPLETION_TELEMETRY_RUNTIME_BUNDLE_DOMAIN,
        relative_names=completion_telemetry_runtime_files(project_root),
    )


def completion_telemetry_contract_bundle_sha256(project_root: str | Path) -> str:
    return _completion_bundle_sha256(
        project_root,
        domain=_COMPLETION_TELEMETRY_CONTRACT_BUNDLE_DOMAIN,
        relative_names=completion_telemetry_contract_files(project_root),
    )


__all__ = [
    "DEFAULT_SOURCE_BUNDLE_ALGORITHM",
    "SOURCE_BUNDLE_ALGORITHMS",
    "completion_telemetry_contract_bundle_sha256",
    "completion_telemetry_contract_files",
    "completion_telemetry_runtime_bundle_sha256",
    "completion_telemetry_runtime_files",
    "phase6_depth60_source_bundle_sha256",
    "phase6_depth60_source_bundle_sha256_for",
    "phase6_depth60_source_bundle_sha256_v2",
    "phase6_depth60_source_files",
    "phase6_depth60_source_files_for",
    "phase6_depth60_source_files_v2",
]
