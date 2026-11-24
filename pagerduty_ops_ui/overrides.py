"""Cap overrides (#75, ADR 0003): raising a write cap above its default needs a recorded
reason. Which flags are caps, and whether an argv raises one, is the engine's call
(`assess().raised_caps`); this module only records the reason and checks it later."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

MAX_REASON_LENGTH = 500


@dataclass(frozen=True)
class CapOverride:
    reason: str
    by: str          # the admin who recorded it (their Okta email)
    caps: tuple[tuple[str, float, float], ...]  # (flag, default, value) per raised cap

    @classmethod
    def of(cls, run) -> CapOverride | None:
        """The override stored with a Run, or None."""
        if run.cap_override_reason is None:
            return None
        return cls(run.cap_override_reason, run.cap_override_by,
                   tuple((c["flag"], c["default"], c["value"]) for c in run.cap_overrides or []))

    def as_dict(self) -> dict:
        return {"reason": self.reason, "by": self.by,
                "caps": [{"flag": f, "default": d, "value": v} for f, d, v in self.caps]}

    def columns(self) -> dict:
        """The Run columns that store it."""
        return {"cap_override_reason": self.reason, "cap_override_by": self.by,
                "cap_overrides": self.as_dict()["caps"]}


def raised(verdict) -> tuple[tuple[str, float, float], ...]:
    return tuple((c.flag, c.default, c.value) for c in verdict.raised_caps)


def record_override(verdict, reason: str | None, by: str, secrets) -> CapOverride | None:
    """The override a new dry run records: required exactly when the engine says the argv
    raises a cap (else 422), and only then recorded. Like an argument, the reason may not
    contain a secret value: it is stored with the Run and shown to every role."""
    if not verdict.raised_caps:
        return None
    reason = (reason or "").strip()
    if not reason:
        caps = ", ".join(f"{f} to {v} (default {d})" for f, d, v in raised(verdict))
        raise HTTPException(422, f"Raising {caps} needs an override reason: say why this Run "
                                 "may change more than usual.")
    if any(s and len(s) >= 4 and s in reason for s in secrets):
        raise HTTPException(422, "The override reason contains a secret value; it is stored "
                                 "with the Run and shown to every role.")
    return CapOverride(reason, by, raised(verdict))


def check_reviewed(verdict, override: CapOverride | None) -> None:
    """An apply (or a dry run made again from a reviewed one) raises only the caps its dry
    run recorded a reason for (else 409). A dry run recorded by an older version of the
    service, or under different cap defaults, needs a new dry run."""
    if not verdict.raised_caps:
        return
    if override is None or override.caps != raised(verdict):
        caps = ", ".join(f"{f} {v} (default {d})" for f, d, v in raised(verdict))
        raise HTTPException(409, f"This raises {caps}, but the reviewed dry run has no "
                                 "matching override on record. Run a new dry run and give "
                                 "the reason.")
