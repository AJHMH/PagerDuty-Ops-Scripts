"""How far this service has rolled out (ADRs 0015, 0016), set by PD_UI_MAX_PHASE.

- 1, read only (the default): reports and dry runs. Every apply is refused.
- 2: also applies that aren't an Irreversible operation.
- 3: also Irreversible operations, with Approvals.

Whether an apply is irreversible is the engine's verdict (`pagerduty_ops.risk`, ADR 0010),
never this module's. Code is one control; the service's PagerDuty key and ServiceNow ACLs
should still grant no more than the phase allows (defence in depth).
"""

from __future__ import annotations

from fastapi import HTTPException

ENV_NAME = "PD_UI_MAX_PHASE"
READ_ONLY, REVERSIBLE, IRREVERSIBLE = 1, 2, 3
DEFAULT = READ_ONLY
PHASES = (READ_ONLY, REVERSIBLE, IRREVERSIBLE)


def parse(raw: str | None) -> int:
    """The phase from the environment: unset or blank is the default; anything other than 1, 2
    or 3 stops the service at startup, so a typo can't move it in either direction."""
    value = (raw or "").strip()
    if not value:
        return DEFAULT
    if value not in {str(p) for p in PHASES}:
        raise SystemExit(f"{ENV_NAME} must be 1, 2 or 3, not {value!r}.")
    return int(value)


def _where(max_phase: int) -> str:
    return f"this service is set to Phase {max_phase} ({ENV_NAME})"


def require_apply(max_phase: int, verdict=None) -> None:
    """Refuse an apply the phase doesn't allow (403). Without a verdict, only the phase-1
    check is made; call again with the engine's verdict for the irreversible check."""
    if max_phase < REVERSIBLE:
        raise HTTPException(403, f"Apply is turned off: {_where(max_phase)}, read only. "
                                 "Dry runs still show what would change.")
    if verdict is not None and verdict.irreversible and max_phase < IRREVERSIBLE:
        reasons = "; ".join(verdict.reasons)
        raise HTTPException(403, f"This is an Irreversible operation ({reasons}). Those are "
                                 f"turned on in Phase 3; {_where(max_phase)}.")


def require_approvals(max_phase: int) -> None:
    """Approvals exist only for Irreversible operations, so they wait for Phase 3 too (403)."""
    if max_phase < IRREVERSIBLE:
        raise HTTPException(403, "Approvals are for Irreversible operations, which are turned "
                                 f"on in Phase 3; {_where(max_phase)}.")
