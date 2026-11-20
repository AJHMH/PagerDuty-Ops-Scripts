"""The review of a dry run: which lines of its output are additions, changes and deletions.

Engine commands preview in two styles: diff-first commands print "+ ", "~ " and "- " lines on
stdout, and --dry-run commands print "[dry-run] <verb> ..." lines (mostly on stderr). Only
lines that *start* with those markers count; log lines carry their own prefix (time, level)
and stay as context, so the diff, and its hash, don't change with timestamps.
"""

from __future__ import annotations

import hashlib
import json
import re

ADD = {"add", "create", "attach", "upload", "post", "activate", "insert"}
CHANGE = {"update", "set", "put", "patch", "rename", "move", "convert", "change", "replace",
          "apply", "enable", "disable"}
DELETE = {"delete", "remove", "detach", "deactivate", "clear", "prune", "unset"}
DRY_RUN = re.compile(r"^\[dry-run\]\s+(?:would\s+)?([A-Za-z]+)", re.IGNORECASE)


def classify(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("+ "):
        return "add"
    if stripped.startswith("~ "):
        return "change"
    if stripped.startswith("- "):
        return "delete"
    match = DRY_RUN.match(stripped)
    if match:
        verb = match.group(1).lower()
        if verb in ADD:
            return "add"
        if verb in CHANGE:
            return "change"
        if verb in DELETE:
            return "delete"
        return "info"
    return "context"


def review(stdout: str, stderr: str) -> dict:
    lines = [{"stream": stream, "kind": classify(line), "text": line}
             for stream, text in (("stdout", stdout), ("stderr", stderr))
             for line in text.splitlines() if line.strip()]
    counts = {k: sum(1 for line in lines if line["kind"] == k)
              for k in ("add", "change", "delete")}
    return {"counts": counts, "lines": lines}


def _digest(material) -> str:
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def input_hash(command: str, argv: list[str], inputs: list[tuple[str, str]]) -> str:
    """Identifies a Run's inputs: the command, its exact argv and each uploaded file's hash."""
    return _digest({"command": command, "argv": argv, "inputs": sorted(inputs)})


def output_hash(stdout: str, stderr: str, outputs: list[tuple[str, str]]) -> str:
    """Identifies what a Run produced: its classified diff lines (not log context) and the
    hash of each output file."""
    diff = [(line["kind"], line["text"].strip()) for line in review(stdout, stderr)["lines"]
            if line["kind"] != "context"]
    return _digest({"diff": diff, "outputs": sorted(outputs)})
