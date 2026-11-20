# IIS fronts the service, which runs as a least-privilege Windows Service with direct egress

IIS is the reverse proxy and terminates TLS with a City-issued certificate; the FastAPI service
(uvicorn) listens only on localhost as a Windows Service under a dedicated account with no
local admin rights. Okta sign-in (OIDC) is handled inside the app, so the Viewer, Operator and
Approver roles come from Okta group claims. Secrets come from the City's enterprise vault at
startup and reach each Run's process only through environment variables, never its command
line or a file. Outbound HTTPS goes directly through the firewall (no City proxy) to
`api.pagerduty.com`, the `dallastx`, `dallastx-test` and `dallastx-dev` ServiceNow instances,
and the Okta domain; inbound HTTPS is from the City network only.

## Considered Options

- uvicorn directly on 443 — rejected: certificates and hardening would sit outside the tooling
  the Windows server team already patches and monitors.
- Docker on Windows — rejected: an extra runtime for the server team to support, for no gain
  over a Windows Service.
