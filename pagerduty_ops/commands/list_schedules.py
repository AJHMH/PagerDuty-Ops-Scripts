"""List PagerDuty v2 schedules (GET /schedules). Read-only.

For v3 'flexible schedules' (Early Access), use the v3-schedules command.
"""

from __future__ import annotations

from ..api import fetch_all
from ..cli import init, standard_parser
from ..output import render_rows, write_payload
from ..risk import READ_ONLY
from .list_incidents import parse_multi

FIELDNAMES = [
    "id",
    "name",
    "description",
    "time_zone",
    "team_ids",
    "team_names",
    "html_url",
]


RISK = READ_ONLY


def build_parser():
    p = standard_parser(
        "List PagerDuty schedules (API v2 /schedules).", formats=("table", "csv", "json")
    )
    p.add_argument(
        "--filter",
        "--name-filter",
        dest="text_filter",
        metavar="TEXT",
        help="Substring match on schedule name (server-side query, case-insensitive).",
    )
    p.add_argument(
        "--team-id",
        dest="team_ids",
        action="append",
        default=[],
        metavar="ID",
        help="Only schedules linked to this team; repeat or comma-separate.",
    )
    return p


def build_query_params(args) -> dict:
    params = {"include[]": ["teams"]}
    if args.text_filter:
        params["query"] = args.text_filter
    team_ids = parse_multi(args.team_ids)
    if team_ids:
        params["team_ids[]"] = team_ids
    return params


def _join_team_field(teams, key: str) -> str:
    parts = []
    for team in teams or []:
        val = (team.get(key) or "").strip()
        if val:
            parts.append(val)
    return ", ".join(parts)


def schedule_row(schedule: dict) -> dict:
    teams = schedule.get("teams") or []
    return {
        "id": schedule.get("id", ""),
        "name": schedule.get("name") or schedule.get("summary", ""),
        "description": schedule.get("description") or "",
        "time_zone": schedule.get("time_zone", ""),
        "team_ids": _join_team_field(teams, "id"),
        "team_names": _join_team_field(teams, "summary"),
        "html_url": schedule.get("html_url", ""),
    }


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    token = init(args)
    params = build_query_params(args)
    schedules = fetch_all(
        "schedules",
        token,
        params=params,
        name_filter=args.text_filter,
        label="schedules",
    )
    rows = [schedule_row(s) for s in schedules]
    write_payload(render_rows(rows, FIELDNAMES, args.format, raw=schedules),
                  args.output, fmt=args.format)
    return 0
