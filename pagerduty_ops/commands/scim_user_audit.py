"""Diff PagerDuty SCIM users against an expected-users CSV from your IdP.

Reports orphaned users (in PD, not expected), missing users (expected, not in
PD), and field drift (displayName/active). Read-only. Token needs SCIM scope.
"""

from __future__ import annotations

import sys

from ..api import request
from ..bulkops import load_csv_rows
from ..cli import init, standard_parser
from ..log import get_logger
from ..output import write_payload
from ..risk import READ_ONLY

log = get_logger("scim_user_audit")

SCIM_PAGE_SIZE = 100
SCIM_HEADERS = {"Accept": "application/scim+json"}


RISK = READ_ONLY


def build_parser():
    p = standard_parser("Diff PagerDuty SCIM users vs an expected-users CSV.")
    p.add_argument("expected_csv", help="CSV with columns: email, displayName, active")
    p.add_argument("-o", "--output", help="Write the report to this file (default stdout).")
    return p


def fetch_scim_users(token) -> list[dict]:
    users: list[dict] = []
    start_index = 1
    while True:
        body = request(
            "scim/v2/Users", token,
            params={"startIndex": start_index, "count": SCIM_PAGE_SIZE},
            extra_headers=SCIM_HEADERS,
        )
        resources = body.get("Resources", [])
        users.extend(resources)
        total = body.get("totalResults", 0)
        if not resources or start_index + len(resources) > total:
            return users
        start_index += len(resources)


def primary_email(scim_user) -> str:
    emails = scim_user.get("emails") or []
    for entry in emails:
        if entry.get("primary"):
            return (entry.get("value") or "").lower()
    if emails:
        return (emails[0].get("value") or "").lower()
    return (scim_user.get("userName") or "").lower()


def normalize_pd_user(u) -> dict:
    return {
        "id": u.get("id"),
        "email": primary_email(u),
        "displayName": u.get("displayName") or "",
        "active": bool(u.get("active", True)),
    }


TRUE_VALUES = {"true", "yes", "y", "1", "active"}
FALSE_VALUES = {"false", "no", "n", "0", "inactive"}


def load_expected(path) -> dict:
    """{email: {email, displayName, active}} from the IdP CSV.

    Strict: a row without an email, an unrecognized `active` value, or a duplicate
    email is a validation error (exit 2). A typo such as "ture" must never read as
    inactive, because pd-scim-user-sync deletes users the IdP marks inactive.
    """
    rows = load_csv_rows(path, {"email", "displayName", "active"},
                         require_values=("email", "active"))
    out, errors = {}, []
    for row in rows:
        email = row["email"].lower()
        flag = row["active"].lower()
        if flag in TRUE_VALUES:
            active = True
        elif flag in FALSE_VALUES:
            active = False
        else:
            errors.append(f"line {row['_line']}: active={row['active']!r} is not one of "
                          "true/false, yes/no, 1/0, active/inactive")
            continue
        if email in out:
            errors.append(f"line {row['_line']}: duplicate email {email}")
            continue
        out[email] = {"email": email, "displayName": row["displayName"], "active": active}
    if errors:
        for e in errors:
            print(f"Error: {path}: {e}", file=sys.stderr)
        raise SystemExit(2)
    return out


def diff(pd_users, expected):
    pd_by_email = {u["email"]: u for u in pd_users if u["email"]}
    orphans = sorted(set(pd_by_email) - set(expected))
    missing = sorted(set(expected) - set(pd_by_email))
    drifts = []
    for email in sorted(set(pd_by_email) & set(expected)):
        pd, exp = pd_by_email[email], expected[email]
        problems = []
        if pd["displayName"] != exp["displayName"]:
            problems.append(
                f"displayName: pd={pd['displayName']!r} expected={exp['displayName']!r}"
            )
        if pd["active"] != exp["active"]:
            problems.append(f"active: pd={pd['active']} expected={exp['active']}")
        if problems:
            drifts.append((email, pd["id"], "; ".join(problems)))
    return orphans, missing, drifts


def render(orphans, missing, drifts) -> str:
    lines = [f"=== Orphaned in PagerDuty ({len(orphans)}) ==="]
    lines += [f"  {e}" for e in orphans]
    lines += ["", f"=== Missing from PagerDuty ({len(missing)}) ==="]
    lines += [f"  {e}" for e in missing]
    lines += ["", f"=== Field drift ({len(drifts)}) ==="]
    lines += [f"  {email} ({pd_id}): {detail}" for email, pd_id, detail in drifts]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    token = init(args)
    expected = load_expected(args.expected_csv)
    log.info("Loaded %d expected users from %s", len(expected), args.expected_csv)
    log.info("Fetching PagerDuty SCIM users...")
    pd_users = [normalize_pd_user(u) for u in fetch_scim_users(token)]
    log.info("Got %d SCIM users.", len(pd_users))
    orphans, missing, drifts = diff(pd_users, expected)
    write_payload(render(orphans, missing, drifts), args.output)
    return 0
