"""Rehearsal receipts: proof that a plan was applied cleanly on a rehearsal
(sub-production) instance before it may be applied to production.

- A receipt is written only after a *non-dry-run* apply on an instance listed in
  ``SN_REHEARSAL_INSTANCES`` finishes with zero failures.
- A production apply (instance in ``SN_PROD_INSTANCES``) requires a receipt whose
  plan hash matches exactly and which is younger than ``SN_RECEIPT_MAX_AGE_DAYS``
  (default 7). Editing the plan after rehearsal changes its hash and invalidates
  the receipt.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

from .log import get_logger
from .plan import utc_now_iso

log = get_logger("receipts")

DEFAULT_RECEIPTS_DIR = ".pd_ops_receipts"
DEFAULT_MAX_AGE_DAYS = 7


def receipts_dir(cli_dir: str | None = None) -> str:
    return cli_dir or os.environ.get("PD_OPS_RECEIPTS_DIR") or DEFAULT_RECEIPTS_DIR


def max_age_days() -> int:
    raw = os.environ.get("SN_RECEIPT_MAX_AGE_DAYS", "")
    try:
        value = int(raw) if raw else DEFAULT_MAX_AGE_DAYS
    except ValueError:
        value = DEFAULT_MAX_AGE_DAYS
    return max(1, value)


def _path(directory: str, sha: str) -> str:
    return os.path.join(directory, f"{sha}.json")


def write_receipt(directory: str, *, sha: str, kind: str, instance: str,
                  succeeded: int, skipped: int) -> str:
    os.makedirs(directory, exist_ok=True)
    receipt = {
        "plan_sha256": sha,
        "kind": kind,
        "instance": instance,
        "applied_at": utc_now_iso(),
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": 0,
    }
    path = _path(directory, sha)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2, sort_keys=True)
        f.write("\n")
    log.info("Rehearsal receipt written: %s", path)
    return path


def require_receipt(directory: str, *, sha: str, rehearsal_instances: set[str],
                    now: datetime | None = None) -> dict:
    """Return the matching receipt or exit 2 with an explanation."""
    path = _path(directory, sha)

    def refuse(reason: str):
        print(
            f"Error: refusing to write to production: {reason}\n"
            f"Apply this exact plan (sha256 {sha[:12]}…) to a rehearsal instance "
            f"({', '.join(sorted(rehearsal_instances)) or 'set SN_REHEARSAL_INSTANCES'}) "
            "first; a successful run writes the receipt.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    try:
        with open(path, encoding="utf-8") as f:
            receipt = json.load(f)
    except FileNotFoundError:
        refuse(f"no rehearsal receipt at {path}")
    except (OSError, ValueError) as e:
        refuse(f"unreadable receipt {path}: {e}")

    if receipt.get("plan_sha256") != sha:
        refuse("receipt hash does not match the plan")
    if receipt.get("failed", 1) != 0:
        refuse("rehearsal run had failures")
    if receipt.get("instance") not in rehearsal_instances:
        refuse(f"receipt is from {receipt.get('instance')!r}, which is not a rehearsal instance")
    try:
        applied = datetime.fromisoformat(str(receipt.get("applied_at")).replace("Z", "+00:00"))
    except ValueError:
        refuse("receipt has an invalid applied_at timestamp")
    now = now or datetime.now(timezone.utc)
    age_limit = max_age_days()
    if now - applied > timedelta(days=age_limit):
        refuse(f"receipt is older than {age_limit} days (applied {receipt['applied_at']})")
    log.info("Rehearsal receipt OK: %s on %s at %s", sha[:12], receipt["instance"],
             receipt["applied_at"])
    return receipt
