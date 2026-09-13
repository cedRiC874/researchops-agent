"""Readonly loaded-origin and locked-version checks, not binary attestation."""
from __future__ import annotations

import importlib.metadata
from importlib.machinery import ModuleSpec
import re
import sys
import tomllib
import types
from pathlib import Path

from researchops_external_closure.execution_binding import _current_file
from researchops_external_closure.execution_current_v3 import _root
from .contract import digest, fail


_PACKAGES = ("researchops", "researchops_completion_telemetry", "researchops_external_closure", "researchops_completion_timing")


def _expected_hash(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None or value == "0" * 64:
        fail("process_environment_expected_hash_invalid")


def _locked_versions(payload):
    if type(payload) is not bytes or len(payload) > 262144:
        fail("process_environment_lock_invalid")
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeError:
        fail("process_environment_lock_invalid")
    result = {}
    for line in lines:
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]{0,127})==([A-Za-z0-9][A-Za-z0-9._+!-]{0,127})", line)
        if match is None:
            fail("process_environment_lock_invalid")
        name, version = match.groups()
        canonical = re.sub(r"[-_.]+", "-", name).lower()
        if canonical in result or len(result) >= 512:
            fail("process_environment_lock_invalid")
        result[canonical] = version
    if not result:
        fail("process_environment_lock_invalid")
    return result


def _project_origins(root):
    if len(sys.modules) > 8192:
        fail("process_environment_module_limit")
    # No module getattr/import hook is called during inspection.
    modules = []
    for entry in sys.modules.items():
        if len(modules) >= 8192:
            fail("process_environment_module_limit")
        modules.append(entry)
    found = {}
    for name, module in modules:
        if not any(name == package or name.startswith(package + ".") for package in _PACKAGES):
            continue
        if type(module) is not types.ModuleType:
            fail("process_environment_module_invalid")
        values = vars(module)
        spec, file = values.get("__spec__"), values.get("__file__")
        if (type(spec) is not ModuleSpec or type(file) is not str or type(spec.origin) is not str
            or values.get("__name__") != name):
            fail("process_environment_module_invalid")
        path = Path(file).absolute()
        if path != Path(spec.origin).absolute():
            fail("process_environment_origin_mismatch")
        stem = "src/" + name.replace(".", "/")
        choices = (stem + ".py", stem + "/__init__.py")
        selected = next((relative for relative in choices if path == root / relative), None)
        if selected is None:
            fail("process_environment_foreign_project_module")
        _current_file(root, selected)  # Single-link/no-reparse path validation.
        if selected.endswith("/__init__.py"):
            package_paths = values.get("__path__")
            if (type(package_paths) not in (list, tuple) or len(package_paths) != 1
                or type(package_paths[0]) is not str or Path(package_paths[0]).absolute() != path.parent):
                fail("process_environment_package_path_mismatch")
        found[name] = selected
    if __name__ not in found:
        fail("process_environment_checker_origin_missing")
    return found


def verify_process_environment(project_root, *, expected_dependency_lock_sha256, expected_pyproject_sha256):
    """Check project origins and all lock entries without importing Providers.

    Version metadata and origin paths do not attest loaded bytecode, wheel bytes,
    arbitrary in-memory patching, extra packages or a trusted launcher.
    """
    try:
        _expected_hash(expected_dependency_lock_sha256); _expected_hash(expected_pyproject_sha256)
        root = _root(project_root)
        lock = _current_file(root, "requirements.lock")
        project = _current_file(root, "pyproject.toml")
        if digest(lock) != expected_dependency_lock_sha256 or digest(project) != expected_pyproject_sha256:
            fail("process_environment_input_hash_mismatch")
        versions = _locked_versions(lock)
        actual = {name: importlib.metadata.version(name) for name in versions}
        if actual != versions:
            fail("process_environment_locked_version_mismatch")
        from packaging.specifiers import SpecifierSet
        python_requirement = tomllib.loads(project.decode("utf-8"))["project"]["requires-python"]
        python_version = ".".join(str(value) for value in sys.version_info[:3])
        if type(python_requirement) is not str or python_version not in SpecifierSet(python_requirement):
            fail("process_environment_python_version_mismatch")
        origins = _project_origins(root)
        if (_current_file(root, "requirements.lock") != lock or _current_file(root, "pyproject.toml") != project
            or _project_origins(root) != origins or {name: importlib.metadata.version(name) for name in versions} != versions):
            fail("process_environment_changed")
        return {"status": "process_origins_and_locked_versions_matched", "locked_distribution_count": len(versions),
            "loaded_project_module_count": len(origins), "python_version": python_version,
            "loaded_project_origins_matched": True, "locked_distribution_versions_matched": True,
            "loaded_bytecode_integrity_verified": False, "dependency_binary_integrity_verified": False,
            "extra_distributions_audited": False, "trusted_launcher_verified": False,
            "runtime_authority_granted": False, "provider_calls": 0, "real_key_loads": 0}
    except Exception as error:
        if getattr(error, "code", None):
            raise
        fail("process_environment_unavailable")
