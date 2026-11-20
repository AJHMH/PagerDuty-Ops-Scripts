# The UI service runs on an on-prem Windows Server VM

The UI service is hosted on a VM in the City's data center running Windows Server, because
that is the platform City IT provides for internal services. The engine has so far only been
tested on Linux in CI, so Windows support has to be proven, not assumed.

## Consequences

The first ticket, before any UI work, adds a `windows-latest` unit-test job to `ci.yml` so a
Windows regression fails every PR.
