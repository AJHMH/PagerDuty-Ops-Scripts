"""Plan files: the reviewed, hashable unit of change for plan → review → apply.

A plan is JSON::

    {
      "format": "pagerduty-ops-plan", "version": 1,
      "kind": "sn-sync" | "pd-reorg",
      "generated_at": "...", "generated_by": "pd-sn-sync plan",
      "options": {...},
      "items": [ {"op": "...", ...}, ... ]
    }

The plan hash (sha256) covers ``kind`` and the items minus informational keys
(``HASH_EXCLUDED_KEYS``). Excluding instance-specific keys such as ServiceNow
``sys_id`` and the observed ``expected`` value lets the *same* plan be rehearsed
on a sub-production instance and then applied to production, with the rehearsal
receipt matching by hash. Any edit to what the plan *does* changes the hash.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone

PLAN_FORMAT = "pagerduty-ops-plan"
PLAN_VERSION = 1
PLAN_KINDS = ("sn-sync", "pd-reorg")
HASH_EXCLUDED_KEYS = frozenset({"sys_id", "expected", "detail"})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_plan(kind: str, items: list[dict], *, generated_by: str, options: dict | None = None):
    if kind not in PLAN_KINDS:
        raise ValueError(f"unknown plan kind {kind!r}")
    return {
        "format": PLAN_FORMAT,
        "version": PLAN_VERSION,
        "kind": kind,
        "generated_at": utc_now_iso(),
        "generated_by": generated_by,
        "options": options or {},
        "items": items,
    }


def plan_hash(plan: dict) -> str:
    material = {
        "kind": plan["kind"],
        "items": [{k: v for k, v in item.items() if k not in HASH_EXCLUDED_KEYS}
                  for item in plan["items"]],
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_plan(plan: dict, path: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, sort_keys=True)
        f.write("\n")
    return plan_hash(plan)


def load_plan(path: str, *, expected_kind: str) -> dict:
    """Read and structurally validate a plan file. Exits 2 on any problem."""
    try:
        with open(path, encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError) as e:
        print(f"Error: cannot read plan {path}: {e}", file=sys.stderr)
        raise SystemExit(2) from e
    problems = []
    if not isinstance(plan, dict) or plan.get("format") != PLAN_FORMAT:
        problems.append("not a pagerduty-ops plan file")
    elif plan.get("version") != PLAN_VERSION:
        problems.append(f"unsupported plan version {plan.get('version')!r}")
    elif plan.get("kind") != expected_kind:
        problems.append(f"plan kind is {plan.get('kind')!r}, expected {expected_kind!r}")
    elif not isinstance(plan.get("items"), list) or not all(
            isinstance(i, dict) and i.get("op") for i in plan["items"]):
        problems.append("items must be a list of objects with an 'op'")
    if problems:
        print(f"Error: {path}: {'; '.join(problems)}", file=sys.stderr)
        raise SystemExit(2)
    return plan
