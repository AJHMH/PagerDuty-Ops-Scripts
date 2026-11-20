"""Setuptools `console_scripts` targets — each wraps a command `main` with `cli.run()`.

Installed CLIs therefore share the same PDApiError → exit 1/3 mapping and stderr
logging as the legacy `pd_*.py` shims.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module

from pagerduty_ops.cli import run as _run


def _entry(module: str, attr: str = "main") -> None:
    main_fn: Callable[..., int] = getattr(import_module(f"pagerduty_ops.commands.{module}"), attr)
    _run(main_fn)


def run_script(name: str) -> None:
    """Run the console script `name` (e.g. "pd-list-users") with the current sys.argv."""
    _entry(*SCRIPTS[name])


# Console script name -> (command module, entry function). The single source of truth for
# what each installed CLI runs; the UI service launches Runs from it (ADR 0004).
SCRIPTS: dict[str, tuple[str, str]] = {
    "pd-list-users": ("list_users", "main"),
    "pd-list-teams": ("list_teams", "main"),
    "pd-list-services": ("list_services", "main"),
    "pd-list-schedules": ("list_schedules", "main"),
    "pd-list-escalation-policies": ("list_escalation_policies", "main"),
    "pd-list-webhooks": ("list_webhooks", "main"),
    "pd-list-incidents": ("list_incidents", "main"),
    "pd-list-status-pages": ("list_status_pages", "main"),
    "pd-v3-schedules": ("v3_schedules", "main"),
    "pd-export-ids": ("export_ids", "main"),
    "pd-audit-export": ("audit_export", "main"),
    "pd-export-log-entries": ("export_log_entries", "main"),
    "pd-export-change-events": ("export_change_events", "main"),
    "pd-scim-user-audit": ("scim_user_audit", "main"),
    "pd-standards-report": ("standards_report", "main"),
    "pd-patch-role": ("patch_role", "main"),
    "pd-rename-resources": ("rename_resources", "main"),
    "pd-team-members": ("team_members", "main"),
    "pd-update-team-roles": ("update_team_roles", "main"),
    "pd-remove-team-members": ("remove_team_members", "main"),
    "pd-service-urgency": ("service_urgency", "main"),
    "pd-bulk-maintenance-window": ("bulk_maintenance_window", "main"),
    "pd-apply-tags": ("apply_tags", "main"),
    "pd-bulk-extensions": ("bulk_extensions", "main"),
    "pd-alert-grouping": ("alert_grouping", "main"),
    "pd-eo-export": ("event_orchestration", "export_main"),
    "pd-eo-apply": ("event_orchestration", "apply_main"),
    "pd-sn-sync": ("sn_sync", "main"),
    "pd-validate": ("validate", "main"),
    "pd-reorg": ("reorg", "main"),
    "pd-service-deps": ("service_deps", "main"),
    "pd-schedule-overrides": ("schedule_overrides", "main"),
    "pd-subscriptions": ("subscriptions", "main"),
    "pd-custom-fields": ("custom_fields", "main"),
    "pd-cmdb-enrichment": ("cmdb_enrichment", "main"),
    "pd-service-field-values": ("service_field_values", "main"),
    "pd-event-rules-migrate": ("event_rules_migrate", "main"),
    "pd-incident-workflows": ("incident_workflows", "main"),
    "pd-scim-user-sync": ("scim_user_sync", "main"),
    "pd-business-services": ("business_services", "main"),
    "pd-security-config": ("security_config", "main"),
    "pd-templates": ("templates", "main"),
}


def pd_list_users() -> None:
    run_script("pd-list-users")


def pd_list_teams() -> None:
    run_script("pd-list-teams")


def pd_list_services() -> None:
    run_script("pd-list-services")


def pd_list_schedules() -> None:
    run_script("pd-list-schedules")


def pd_list_escalation_policies() -> None:
    run_script("pd-list-escalation-policies")


def pd_list_webhooks() -> None:
    run_script("pd-list-webhooks")


def pd_list_incidents() -> None:
    run_script("pd-list-incidents")


def pd_list_status_pages() -> None:
    run_script("pd-list-status-pages")


def pd_v3_schedules() -> None:
    run_script("pd-v3-schedules")


def pd_export_ids() -> None:
    run_script("pd-export-ids")


def pd_audit_export() -> None:
    run_script("pd-audit-export")


def pd_export_log_entries() -> None:
    run_script("pd-export-log-entries")


def pd_export_change_events() -> None:
    run_script("pd-export-change-events")


def pd_scim_user_audit() -> None:
    run_script("pd-scim-user-audit")


def pd_standards_report() -> None:
    run_script("pd-standards-report")


def pd_patch_role() -> None:
    run_script("pd-patch-role")


def pd_rename_resources() -> None:
    run_script("pd-rename-resources")


def pd_team_members() -> None:
    run_script("pd-team-members")


def pd_update_team_roles() -> None:
    run_script("pd-update-team-roles")


def pd_remove_team_members() -> None:
    run_script("pd-remove-team-members")


def pd_service_urgency() -> None:
    run_script("pd-service-urgency")


def pd_bulk_maintenance_window() -> None:
    run_script("pd-bulk-maintenance-window")


def pd_apply_tags() -> None:
    run_script("pd-apply-tags")


def pd_bulk_extensions() -> None:
    run_script("pd-bulk-extensions")


def pd_alert_grouping() -> None:
    run_script("pd-alert-grouping")


def pd_eo_export() -> None:
    run_script("pd-eo-export")


def pd_eo_apply() -> None:
    run_script("pd-eo-apply")


def pd_sn_sync() -> None:
    run_script("pd-sn-sync")


def pd_validate() -> None:
    run_script("pd-validate")


def pd_reorg() -> None:
    run_script("pd-reorg")


def pd_service_deps() -> None:
    run_script("pd-service-deps")


def pd_schedule_overrides() -> None:
    run_script("pd-schedule-overrides")


def pd_subscriptions() -> None:
    run_script("pd-subscriptions")


def pd_custom_fields() -> None:
    run_script("pd-custom-fields")


def pd_cmdb_enrichment() -> None:
    run_script("pd-cmdb-enrichment")


def pd_service_field_values() -> None:
    run_script("pd-service-field-values")


def pd_event_rules_migrate() -> None:
    run_script("pd-event-rules-migrate")


def pd_incident_workflows() -> None:
    run_script("pd-incident-workflows")


def pd_scim_user_sync() -> None:
    run_script("pd-scim-user-sync")


def pd_business_services() -> None:
    run_script("pd-business-services")


def pd_security_config() -> None:
    run_script("pd-security-config")


def pd_templates() -> None:
    run_script("pd-templates")
