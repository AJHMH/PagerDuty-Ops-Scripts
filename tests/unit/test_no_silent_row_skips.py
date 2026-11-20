"""Regression: bulk CSV commands must never silently drop an incomplete row.

Each case feeds one valid row plus one row missing a required value. Before the fix,
the incomplete row was skipped, the valid row was written, and the run exited 0.
Now the whole file is rejected (exit 2) before any API call that writes.
"""

import re

import pytest
import responses

from pagerduty_ops.commands import (
    apply_tags,
    service_deps,
    service_field_values,
    subscriptions,
)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")


def run(main, argv):
    try:
        return main(argv)
    except SystemExit as e:
        return e.code


CASES = [
    (service_deps.main, ["apply"],
     "action,supporting_kind,supporting_id,dependent_kind,dependent_id\n"
     "associate,service,PS1,business_service,PB1\n"
     "associate,service,,business_service,PB1\n"),
    (subscriptions.main, ["apply"],
     "action,subscriber_kind,subscriber_id,subscribable_kind,subscribable_id\n"
     "subscribe,user,PU1,business_service,PB1\n"
     "subscribe,user,PU2,business_service,\n"),
    (apply_tags.main, [],
     "entity_type,entity_id,tag_label,action\n"
     "services,PS1,prod,add\n"
     "services,,prod,add\n"),
    (apply_tags.main, [],                      # unknown enum used to be warned-and-skipped
     "entity_type,entity_id,tag_label,action\n"
     "services,PS1,prod,add\n"
     "servcies,PS2,prod,add\n"),
    (service_field_values.main, ["apply"],
     "service_id,tier\nPS1,gold\n,gold\n"),
]


@pytest.mark.parametrize("main,prefix,csv_text", CASES,
                         ids=["service_deps", "subscriptions", "apply_tags-missing",
                              "apply_tags-enum", "service_field_values"])
@responses.activate
def test_incomplete_row_rejects_whole_file_before_any_write(tmp_path, main, prefix, csv_text):
    # Any GET returns an empty-but-valid payload; any write would be recorded.
    responses.get(re.compile(r".*"), json={"more": False, "services": [{"id": "PS1"}],
                                            "fields": [{"id": "F1", "name": "tier",
                                                        "data_type": "string",
                                                        "field_type": "single_value"}],
                                            "tags": [], "relationships": [],
                                            "subscriptions": [], "custom_fields": []})
    p = tmp_path / "in.csv"
    p.write_text(csv_text)
    code = run(main, [*prefix, str(p), "-y"])
    assert code == 2
    assert not [c for c in responses.calls if c.request.method != "GET"]


def test_completely_blank_rows_are_still_ignored(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("action,subscriber_kind,subscriber_id,subscribable_kind,subscribable_id\n"
                 "subscribe,user,PU1,business_service,PB1\n,,,,\n")
    assert len(subscriptions.load_rows(str(p))) == 1
