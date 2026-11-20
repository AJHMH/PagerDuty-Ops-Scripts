# The UI is a City-hosted service behind Okta, not a local app

The admin-team UI runs as one City-hosted service behind Okta single sign-on rather than as
an app each admin runs on their own machine. A shared service is the only way to have one Run
history, one place that holds PagerDuty and ServiceNow credentials, and approvals by a second
admin; the cost is a hosting target and a security review.

## Considered Options

- Local app per admin — rejected: no shared history or approvals, and every admin holds tokens.
- Inside ServiceNow — not a substitute: ServiceNow cannot run the Python engine, so it would
  still need this service behind it. It remains possible later as a second front end.
