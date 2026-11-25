"""When a Scheduled run is due (#80): daily, weekly or every N hours, in Central time."""

from datetime import datetime, timezone

import pytest

from pagerduty_ops_ui.timing import TIME_ZONE, BadTiming, Timing


def central(text: str) -> datetime:
    """A Central wall-clock time, e.g. "2026-11-25 02:30"."""
    return datetime.fromisoformat(text).replace(tzinfo=TIME_ZONE)


def due(timing: dict, after: str) -> str:
    when = Timing.parse(timing).next_after(central(after)).astimezone(TIME_ZONE)
    return when.strftime("%Y-%m-%d %H:%M %Z")


def test_daily_is_due_at_its_time_today_or_else_tomorrow():
    assert due({"kind": "daily", "at": "02:30"}, "2026-11-25 01:00") == "2026-11-25 02:30 CST"
    assert due({"kind": "daily", "at": "02:30"}, "2026-11-25 02:30") == "2026-11-26 02:30 CST"


def test_weekly_skips_to_the_next_chosen_day():
    friday_and_monday = {"kind": "weekly", "at": "06:00", "days": [0, 4]}
    # Wednesday 25 November 2026 -> Friday 27th, then Monday 30th.
    assert due(friday_and_monday, "2026-11-25 12:00") == "2026-11-27 06:00 CST"
    assert due(friday_and_monday, "2026-11-27 06:00") == "2026-11-30 06:00 CST"


def test_every_n_hours_counts_from_midnight():
    every_six = {"kind": "hourly", "every_hours": 6, "at": "00:15"}
    assert due(every_six, "2026-11-25 07:00") == "2026-11-25 12:15 CST"
    assert due(every_six, "2026-11-25 18:15") == "2026-11-26 00:15 CST"


def test_central_time_follows_daylight_saving():
    nightly = {"kind": "daily", "at": "02:30"}
    assert due(nightly, "2026-07-01 00:00") == "2026-07-01 02:30 CDT"  # summer: UTC-5
    assert due(nightly, "2026-12-01 00:00") == "2026-12-01 02:30 CST"  # winter: UTC-6


def test_a_time_missing_when_clocks_spring_forward_runs_an_hour_later():
    # 8 March 2026: 02:00 CST jumps to 03:00 CDT, so 02:30 never happens.
    assert due({"kind": "daily", "at": "02:30"}, "2026-03-08 00:00") == "2026-03-08 03:30 CDT"


def test_a_time_that_happens_twice_when_clocks_fall_back_runs_once():
    # 1 November 2026: 01:00-02:00 happens twice. 01:30 runs the first time only.
    nightly = Timing.parse({"kind": "daily", "at": "01:30"})
    first = nightly.next_after(central("2026-11-01 00:00"))
    assert first == datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)  # 01:30 CDT
    assert nightly.next_after(first).astimezone(TIME_ZONE).strftime("%m-%d %H:%M %Z") == \
        "11-02 01:30 CST"


@pytest.mark.parametrize("raw, text", [
    ({"kind": "daily", "at": "02:30"}, "Every day at 02:30 Central"),
    ({"kind": "weekly", "at": "06:00", "days": [4, 0, 4]}, "Mon and Fri at 06:00 Central"),
    ({"kind": "weekly", "at": "06:00", "days": [2]}, "Wed at 06:00 Central"),
    ({"kind": "hourly", "every_hours": 1, "at": "00:05"}, "Every hour at :05"),
    ({"kind": "hourly", "every_hours": 4, "at": "13:45"},
     "Every 4 hours at :45 (Central, from midnight)"),
])
def test_descriptions(raw, text):
    assert Timing.parse(raw).describe() == text


@pytest.mark.parametrize("raw", [
    None, {}, {"kind": "monthly"}, {"kind": "daily"}, {"kind": "daily", "at": "2:30"},
    {"kind": "daily", "at": "24:00"}, {"kind": "weekly", "at": "06:00"},
    {"kind": "weekly", "at": "06:00", "days": [7]}, {"kind": "weekly", "at": "06:00",
                                                    "days": ["Mon"]},
    {"kind": "hourly", "every_hours": 5}, {"kind": "hourly", "every_hours": "2"},
])
def test_unusable_timings_say_why(raw):
    with pytest.raises(BadTiming):
        Timing.parse(raw)


def test_timing_round_trips_through_storage():
    timing = Timing.parse({"kind": "weekly", "at": "06:00", "days": [4, 0]})
    assert Timing.parse(timing.as_dict()) == timing
