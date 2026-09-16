"""Use only this worktree's modules; activate the established offline process fence."""
import importlib.metadata
import json
from . import identity

# Verify Git identity before aggregate.run installs its irreversible no-subprocess hook.
PREPARED = identity.prepare()
from ..aggregate_read_v1 import run as aggregate_run
PACKAGE, IDENTITY = identity.load(PREPARED)


def environment_record():
    import agents
    import openai
    import sys
    locked = {}
    for line in (identity.REPO / "requirements.lock").read_text(encoding="utf-8").splitlines():
        if "==" in line and not line.startswith("#"):
            name, value = line.split("==", 1)
            locked[name.lower().replace("_", "-")] = value
    used = {name: importlib.metadata.version(name) for name in
            ("openai-agents", "openai", "pydantic", "pydantic-core", "httpx", "griffelib")}
    for name, version in used.items():
        if version != locked[name]:
            raise identity.IdentityError("runtime_dependency_differs_from_fixed_lock:" + name)
    return {"python": sys.version, "executable": sys.executable, "used_dependencies": used,
            "used_dependencies_match_fixed_lock": True,
            "sdk_import_file": agents.__file__, "openai_import_file": openai.__file__,
            "installed_versions": dict(sorted((d.metadata["Name"], d.version)
                                               for d in importlib.metadata.distributions() if d.metadata["Name"]))}


ENVIRONMENT = environment_record()
