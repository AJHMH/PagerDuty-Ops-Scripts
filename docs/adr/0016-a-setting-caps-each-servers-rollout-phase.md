# A setting caps each server's rollout phase, read only by default

ADR 0015 rolls the UI out in phases, with no Apply until the team and City security approve it.
The code for Apply (#73) and Approvals (#77) has since merged, before those reviews, and there
was no way to turn it off. A Phase 1 server stayed read only only because its credentials
could not write.

So each server has a setting, `PD_UI_MAX_PHASE`. At `1` the service refuses every apply. At `2`
it allows applies that aren't an Irreversible operation. At `3` it allows everything. The
service enforces the setting in the API and again when an apply reaches the writing slot. The
front end only hides what the service would refuse. Whether an apply is irreversible is the
engine's verdict (ADR 0010), as it is for Approvals.

The default is `1`, so a new server, or one whose configuration lost the setting, can't write.
Any value other than 1, 2 or 3 stops the service at startup, because guessing in either
direction is worse than not starting. Code ships ahead of approval. Approval moves the setting,
not the merge.

Least-privilege credentials stay: they are now defence in depth, not the only control.

## Consequences

- A server that already applies must set `PD_UI_MAX_PHASE` before it is upgraded, or it comes
  back read only.
- Approvals are refused below Phase 3, since they exist only for Irreversible operations.
- Scheduled runs (ADR 0014) are read only at every phase and don't need the setting.
