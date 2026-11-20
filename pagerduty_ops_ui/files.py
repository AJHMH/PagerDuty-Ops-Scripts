"""Files stored with a Run (ADR 0007): uploaded inputs and the outputs a command writes.

Everything lives inside the Run's own directory. Uploads go under ``inputs/<field>/`` with a
reduced, safe file name; anything else the command leaves in the directory is an output.
Each file's SHA-256 is recorded so a later change is detectable.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

from .store import RunFile

INPUTS = "inputs"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,199}$")


def safe_name(filename: str) -> str:
    """The last path component of an uploaded file's name, if it is a plain, visible name."""
    name = re.split(r"[\\/]", filename or "")[-1].strip()
    if not SAFE_NAME.match(name):
        raise HTTPException(422, f"{filename!r} is not an acceptable file name: use letters, "
                                 "digits, spaces, dots, dashes or underscores.")
    return name


def plan_uploads(uploads: list[tuple[str, str, bytes]]) -> dict[str, list[str]]:
    """field -> relative paths the uploads will have inside the Run directory."""
    paths: dict[str, list[str]] = {}
    for dest, filename, _data in uploads:
        path = f"{INPUTS}/{dest}/{safe_name(filename)}"
        if path in paths.get(dest, []):
            raise HTTPException(422, f"Two uploads for {dest} are both named {path!r}.")
        paths.setdefault(dest, []).append(path)
    return paths


def write_uploads(run_dir: Path, uploads: list[tuple[str, str, bytes]],
                  paths: dict[str, list[str]]) -> None:
    seen: dict[str, int] = {}
    for dest, _filename, data in uploads:
        index = seen.get(dest, 0)
        seen[dest] = index + 1
        target = run_dir / PurePosixPath(paths[dest][index])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect(run_dir: Path) -> list[RunFile]:
    """Every regular file in the Run directory: inputs under inputs/, the rest outputs."""
    files = []
    for path in sorted(p for p in run_dir.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(run_dir).as_posix()
        files.append(RunFile(role="input" if rel.startswith(f"{INPUTS}/") else "output",
                             path=rel, size=path.stat().st_size, sha256=sha256_file(path)))
    return files


def resolve(run_dir: Path, rel: str) -> Path:
    """The stored file's real path, refusing anything that resolves outside the Run."""
    root = run_dir.resolve()
    path = (run_dir / PurePosixPath(rel)).resolve()
    if root != path and root not in path.parents:
        raise HTTPException(404, "No such file.")
    return path
