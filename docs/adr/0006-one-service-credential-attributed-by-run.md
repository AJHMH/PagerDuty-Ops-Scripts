# The UI uses one service credential; Runs, not PagerDuty, attribute changes to people

The UI service calls PagerDuty with a single scoped account API key (and ServiceNow with its
existing Okta client credentials), held in a secret store, instead of each admin's own
PagerDuty identity. PagerDuty's audit log therefore shows the service, not the person; the
UI's Run record — who requested, who approved, what was applied — is the attribution of
record. Per-admin OAuth would name people in PagerDuty but leaves it unclear whose identity an
approved irreversible operation runs under, and multiplies tokens to protect and refresh.
