"""#67 generated forms: every command's form comes from its own argparse parser (ADR 0004)."""

import re

import pytest
from conftest import NO_ROLE, VIEWER

from pagerduty_ops_ui.catalog import engine_catalog
from pagerduty_ops_ui.forms import FILE_ROLES, NOT_FILES, all_fields, form_for

ENGINE = engine_catalog()


def fields_by_dest(form, subcommand=None):
    section = form if subcommand is None else next(
        s for s in form["subcommands"] if s["name"] == subcommand)
    return {f["dest"]: f for f in section["fields"]}


# ---------- every command renders, and no file argument becomes a text box ----------

@pytest.mark.parametrize("name", sorted(ENGINE))
def test_every_engine_command_renders_a_form(name):
    form = form_for(ENGINE[name])
    assert form["name"] == name
    assert form["fields"] or form["subcommands"]


PATH_LIKE = re.compile(r"csv|json|yaml|file|_dir$|^output|_out$|^input|plan|config|field_map")


@pytest.mark.parametrize("name", sorted(ENGINE))
def test_every_path_like_argument_is_classified_as_a_file(name):
    for field in all_fields(form_for(ENGINE[name])):
        if (PATH_LIKE.search(field["dest"]) and field["dest"] not in NOT_FILES
                and field["kind"] not in ("flag", "choice", "number", "count")):
            assert field["kind"] in ("input-file", "input-dir", "output-file", "output-dir"), (
                f"{name}: {field['dest']} looks like a path but is a {field['kind']}; "
                f"add it to FILE_ROLES")


def test_file_roles_only_name_real_arguments():
    dests = {f["dest"] for e in ENGINE.values() for f in all_fields(form_for(e))}
    assert set(FILE_ROLES) - {"log_file", "receipts_dir"} <= dests


# ---------- what a form says ----------

def test_a_report_form_reflects_its_parser():
    fields = fields_by_dest(form_for(ENGINE["pd-list-users"]))
    fmt = fields["format"]
    assert (fmt["kind"], fmt["choices"], fmt["default"]) == (
        "choice", ["table", "csv", "json"], "table")
    assert fmt["flags"] == ["-f", "--format"] and fmt["help"]
    assert fields["output"]["kind"] == "output-file"
    assert fields["text_filter"]["kind"] == "text"
    # The UI never offers credentials, prompts or its own log file.
    assert not {"token", "prompt", "log_file", "help"} & set(fields)
    # -v and -q are mutually exclusive: same group.
    assert fields["verbose"]["group"] is not None
    assert fields["verbose"]["group"] == fields["quiet"]["group"]
    assert fields["verbose"]["kind"] == "flag"


def test_required_positional_input_file():
    fields = fields_by_dest(form_for(ENGINE["pd-scim-user-audit"]))
    csv_field = fields["expected_csv"]
    assert (csv_field["kind"], csv_field["required"], csv_field["positional"]) == (
        "input-file", True, True)


def test_repeatable_option_and_number():
    fields = fields_by_dest(form_for(ENGINE["pd-list-incidents"]))
    assert fields["statuses"]["multiple"] is True
    assert fields["statuses"]["kind"] == "text"


def test_subcommands_carry_their_own_fields_and_whether_they_write():
    form = form_for(ENGINE["pd-business-services"])
    subs = {s["name"]: s for s in form["subcommands"]}
    assert subs["export"]["writes"] is False
    assert subs["apply"]["writes"] is True
    assert fields_by_dest(form, "apply")["input"]["kind"] == "input-file"
    assert fields_by_dest(form, "export")["output"]["kind"] == "output-file"


def test_an_input_directory_accepts_several_files():
    form = form_for(ENGINE["pd-eo-apply"])
    assert fields_by_dest(form)["input_dir"]["kind"] == "input-dir"


# ---------- over the API ----------

def test_form_endpoint(client):
    response = client.get("/api/commands/pd-list-users/form", headers=VIEWER)
    assert response.status_code == 200
    assert response.json()["name"] == "pd-list-users"
    assert client.get("/api/commands/pd-nope/form", headers=VIEWER).status_code == 404
    assert client.get("/api/commands/pd-list-users/form", headers=NO_ROLE).status_code == 403


def test_commands_with_a_read_only_mode_are_listed_as_such(client):
    commands = {c["name"]: c for c in client.get("/api/commands", headers=VIEWER).json()}
    assert commands["pd-business-services"]["has_read_only_mode"] is True
    assert commands["pd-apply-tags"]["has_read_only_mode"] is False
    assert commands["pd-list-users"]["has_read_only_mode"] is True
