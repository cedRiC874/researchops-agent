"""Explicit public derivation; original records remain local and unchanged."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import re
import sys
from .binding import REPO


def sanitize(text):
    for path in (REPO, Path(sys.prefix)):
        text = text.replace(str(path), "<local-root>").replace(path.as_posix(), "<local-root>")
    text = re.sub(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\r\n\"<>]*", "<local-path>", text)
    text = re.sub(r"/(?:Users|home)/[^\s\"<>]+", "<local-path>", text)
    for secret in ("offline-fixture-key-item6", "PRIVATE_REASONING_SENTINEL", "SECRET_SENTINEL"):
        text = text.replace(secret, "<fixture-redacted>")
    return text


def project(value):
    if isinstance(value, dict): return {k: project(v) for k, v in value.items()}
    if isinstance(value, list): return [project(v) for v in value]
    if isinstance(value, str): return sanitize(value)
    return deepcopy(value)


def final_texts(value):
    if isinstance(value, dict):
        return ([value["final_output"]] if "final_output" in value else []) + [x for v in value.values() for x in final_texts(v)]
    if isinstance(value, list): return [x for v in value for x in final_texts(v)]
    return []


def write_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")


def derive(raw_path, public_path):
    raw_bytes = raw_path.read_bytes()
    raw = json.loads(raw_bytes.decode("utf-8"))
    public = project(raw)
    if final_texts(raw) != final_texts(public):
        raise ValueError("public_projection_would_change_answer")
    write_json(public_path, public)
    return {"original_artifact": raw_path.name, "original_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "derived_artifact": public_path.name, "derived_sha256": hashlib.sha256(public_path.read_bytes()).hexdigest(),
            "projection": "local paths and fixture privacy sentinels redacted; final_output immutable",
            "original_available_in_public_package": False}


def added_files_diff(files):
    """Encode a publication addition patch without normalizing candidate bytes."""
    chunks = []
    for name, data in sorted(files.items()):
        if type(name) is not str or re.fullmatch(r"[A-Za-z0-9_.\-/]+", name) is None or name.startswith("/") or any(
            part in {"", ".", ".."} for part in name.split("/")
        ):
            raise ValueError("diff_relative_path_required")
        if type(data) is not bytes or b"\0" in data:
            raise ValueError("diff_utf8_text_required")
        data.decode("utf-8")
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        chunks.append((f"diff --git a/{name} b/{name}\nnew file mode 100644\n"
                       f"index {'0' * 40}..{blob}\n").encode())
        if not data:
            continue
        lines = data.split(b"\n")
        if data.endswith(b"\n"):
            lines.pop()
        chunks.append((f"--- /dev/null\n+++ b/{name}\n@@ -0,0 +1,{len(lines)} @@\n").encode())
        chunks.extend(b"+" + line + b"\n" for line in lines)
        if not data.endswith(b"\n"):
            chunks.append(b"\\ No newline at end of file\n")
    return b"".join(chunks)
