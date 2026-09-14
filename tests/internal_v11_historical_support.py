"""Read-only raw-Git v11 checkpoint, independent of the current Internal tree."""
import atexit
from pathlib import Path
import tempfile

from researchops_external_closure.git_objects_v2 import read_git_object_snapshot
from researchops_external_closure import execution_components_v4 as recipe

ROOT=Path(__file__).resolve().parents[1]
COMMIT="c945d168ee084f82ab690a2f118912bf28956c27"
TREE="0ee6bae3a2c56ef1d8c6c9193d9030f1a8573553"
_root=None


def historical_v11_root():
    global _root
    if _root is not None:return _root
    meta=read_git_object_snapshot(ROOT,COMMIT,(recipe.RECIPE_PATH,),expected_tree_oid=TREE)
    recipe_bytes=meta.blobs[0].payload
    fixed=recipe.decode_recipe(recipe_bytes)
    predecessor_paths=tuple(item["path"] for item in fixed["predecessor_recipes"])
    predecessors=read_git_object_snapshot(ROOT,COMMIT,predecessor_paths,expected_tree_oid=TREE)
    paths=tuple(item.path for item in meta.entries if item.mode in ("100644","100755"))
    selected=recipe.select_profile_paths(paths,recipe_bytes,
        predecessor_recipe_bytes={item.path:item.payload for item in predecessors.blobs},profile="first_live")
    snapshot=read_git_object_snapshot(ROOT,COMMIT,tuple(sorted(set(selected)|set(recipe.PROFILE_PATHS["first_live"]))),expected_tree_oid=TREE)
    temporary=tempfile.TemporaryDirectory(prefix="internal-v11-historical-")
    root=Path(temporary.name).resolve(strict=True)
    for item in snapshot.blobs:
        target=root/item.path;target.parent.mkdir(parents=True,exist_ok=True)
        with target.open("xb") as stream:stream.write(item.payload)
    _root=root
    atexit.register(temporary.cleanup)
    return root
