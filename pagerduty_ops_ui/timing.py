"""When a Scheduled run happens (#80, ADR 0014): daily at a time, weekly on chosen days at a
time, or every N hours. Times are Central (America/Chicago) and follow daylight saving, so
"02:30" is 02:30 in Dallas all year.

On the two daylight-saving Sundays a local time can be missing or happen twice. A missing
time (02:30 when clocks spring forward) runs an hour later on the clock (03:30 CDT). A time
that happens twice (01:15 when clocks fall back) runs once, the first time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TIME_ZONE = ZoneInfo("America/Chicago")
TIME_ZONE_LABEL = "Central"
DAILY, WEEKLY, HOURLY = "daily", "weekly", "hourly"
KINDS = (DAILY, WEEKLY, HOURLY)
# Every N hours, counted from midnight, so the times are the same every day.
EVERY_HOURS = (1, 2, 3, 4, 6, 8, 12)
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # date.weekday() order
_HH_MM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class BadTiming(ValueError):
    """A schedule's timing that can't be used; the message says why."""


@dataclass(frozen=True)
class Timing:
    kind: str
    at: str = "00:00"                 # "HH:MM" Central; for hourly, only the minute counts
    days: tuple[int, ...] = ()        # weekly: 0 = Monday … 6 = Sunday
    every_hours: int | None = None    # hourly

    @classmethod
    def parse(cls, raw: dict) -> Timing:
        """From the API's {"kind", "at", "days", "every_hours"}; raises BadTiming."""
        if not isinstance(raw, dict):
            raise BadTiming("timing must be an object.")
        kind = raw.get("kind")
        if kind not in KINDS:
            raise BadTiming(f"timing.kind must be one of {', '.join(KINDS)}.")
        at = raw.get("at") or ("00:00" if kind == HOURLY else None)
        if not isinstance(at, str) or not _HH_MM.match(at):
            raise BadTiming("timing.at must be a 24-hour time, HH:MM.")
        days: tuple[int, ...] = ()
        every = None
        if kind == WEEKLY:
            raw_days = raw.get("days")
            if (not isinstance(raw_days, list) or not raw_days
                    or not all(isinstance(d, int) and 0 <= d <= 6 for d in raw_days)):
                raise BadTiming("timing.days must list at least one day, 0 (Monday) to 6.")
            days = tuple(sorted(set(raw_days)))
        if kind == HOURLY:
            every = raw.get("every_hours")
            if every not in EVERY_HOURS:
                raise BadTiming("timing.every_hours must be one of "
                                f"{', '.join(map(str, EVERY_HOURS))}.")
            at = f"00:{at[3:]}"  # only the minute past the hour counts
        return cls(kind, at, days, every)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "at": self.at, "days": list(self.days),
                "every_hours": self.every_hours}

    def describe(self) -> str:
        """E.g. "Mon and Fri at 02:30 Central"."""
        if self.kind == DAILY:
            return f"Every day at {self.at} {TIME_ZONE_LABEL}"
        if self.kind == WEEKLY:
            names = [DAY_NAMES[d] for d in self.days]
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
            return f"{listed} at {self.at} {TIME_ZONE_LABEL}"
        minute = self.at[3:]
        if self.every_hours == 1:
            return f"Every hour at :{minute}"
        return f"Every {self.every_hours} hours at :{minute} ({TIME_ZONE_LABEL}, from midnight)"

    def _times_on(self, day: date) -> list[time]:
        hour, minute = int(self.at[:2]), int(self.at[3:])
        if self.kind == HOURLY:
            return [time(h, minute) for h in range(0, 24, self.every_hours)]
        if self.kind == WEEKLY and day.weekday() not in self.days:
            return []
        return [time(hour, minute)]

    def next_after(self, moment: datetime) -> datetime:
        """The first time strictly after `moment` this schedule is due, in UTC."""
        moment = moment.astimezone(timezone.utc)
        start = moment.astimezone(TIME_ZONE).date() - timedelta(days=1)
        for offset in range(9):  # a weekly schedule is due within 8 days
            day = start + timedelta(days=offset)
            for when in self._times_on(day):
                # fold=0: a doubled time runs the first time; a missing one moves forward.
                due = datetime.combine(day, when, tzinfo=TIME_ZONE).astimezone(timezone.utc)
                if due > moment:
                    return due
        raise AssertionError("no due time within 9 days")  # unreachable for valid timings
