"""Materialize only pinned v2 Git blobs; never execute snapshot Python code."""
from contextlib import contextmanager
from pathlib import Path
import tempfile

from researchops_internal_telemetry import source_integrity_v2 as v2, source_integrity_v3 as v3

ROOT = Path(__file__).resolve().parents[1]
COMMIT = v3.HISTORICAL_COMMIT
TREE = v3.HISTORICAL_TREE


@contextmanager
def historical_v2_root():
    lineage = v3.verify_lineage(ROOT)
    manifest = v2.decode(v3.historical_blobs(ROOT, [v2.MANIFEST])[0])
    names = [row["path"] for row in manifest["files"]] + [v2.MANIFEST]
    with tempfile.TemporaryDirectory(prefix="i6-v2-") as directory:
        root = Path(directory)
        for name, data in zip(names, v3.historical_blobs(ROOT, names)):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        # Own the temporary Git metadata under the executing user. Reuse only the
        # immutable object database, never another user's repository identity.
        common = v2._git(ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()
        v2._git(root, "init", "--quiet")
        info = root / ".git/objects/info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "alternates").write_text((Path(common) / "objects").as_posix() + "\n", encoding="utf-8", newline="\n")
        yield root
