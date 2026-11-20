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
# "[dry-run]" lines that only narrate. Any other unrecognised "[dry-run]" line is counted as
# "other": a preview line is a change unless known otherwise, so the review can never claim
# "no changes" for a style it doesn't recognise (e.g. pd-sn-sync's "<table> <key>: <values>").
INFO = {"previewing", "no", "nothing", "skip", "skipping", "would", "summary"}
# Flags that choose the mode, not the inputs: a dry run and its apply share an input hash.
MODE_FLAGS = frozenset({"--dry-run", "--apply", "-y", "--yes"})
# "[dry-run] [line N:] [would] <verb> ..." -- e.g. pd-bulk-maintenance-window's
# "[dry-run] line 3: would create window for ..." is read by its verb, "create".
DRY_RUN = re.compile(r"^\[dry-run\]\s+(?:line\s+\d+:\s*)?(?:would\s+)?([A-Za-z]+)",
                     re.IGNORECASE)
PREVIEW_KINDS = ("add", "change", "delete", "other", "info")


def _continues(line: str) -> bool:
    """A payload line under a preview header ending in ':' (e.g. pd-alert-grouping prints
    "[dry-run] would POST ... with:" and then the JSON it would send)."""
    return bool(line) and (line[0].isspace() or line[0] in '{}[]"')


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
        return "info" if verb in INFO else "other"
    return "context"


def review(stdout: str, stderr: str, outcome: str | None = "succeeded") -> dict:
    """Counts and classified lines. A payload under a preview header is "detail": shown and
    hashed with its header, not counted. `complete` is false when the dry run didn't
    succeed, so the absence of changes can't be read as "nothing to change"."""
    lines = []
    for stream, text in (("stdout", stdout), ("stderr", stderr)):
        under_header = False
        for line in text.splitlines():
            if not line.strip():
                continue
            if under_header and _continues(line):
                kind = "detail"
            else:
                kind = classify(line)
                under_header = kind in PREVIEW_KINDS and line.rstrip().endswith(":")
            lines.append({"stream": stream, "kind": kind, "text": line})
    counts = {k: sum(1 for line in lines if line["kind"] == k)
              for k in ("add", "change", "delete", "other")}
    return {"counts": counts, "lines": lines, "complete": outcome == "succeeded"}


def _digest(material) -> str:
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def input_hash(command: str, argv: list[str], inputs: list[tuple[str, str]]) -> str:
    """Identifies a Run's inputs: the command, its argv without the mode flags (--dry-run,
    --apply, -y), and each uploaded file's hash, so a dry run and its apply match (#73)."""
    return _digest({"command": command, "argv": [a for a in argv if a not in MODE_FLAGS],
                    "inputs": sorted(inputs)})


def output_hash(stdout: str, stderr: str, outputs: list[tuple[str, str]]) -> str:
    """Identifies what a Run produced: its classified diff lines (not log context) and the
    hash of each output file."""
    diff = [(line["kind"], line["text"].strip()) for line in review(stdout, stderr)["lines"]
            if line["kind"] != "context"]
    return _digest({"diff": diff, "outputs": sorted(outputs)})
