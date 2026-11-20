# pd-remove-team-members gains a non-interactive mode instead of the UI answering prompts

`pd-remove-team-members` only works by prompting member by member, and a Run cannot answer
prompts. Rather than have the UI drive the prompts (which would put command behaviour in the
UI, against ADR 0001) or leave the command out of the UI, the CLI gains flags — `--user-ids`
plus explicit flags for also removing the users from schedules and escalation policies — and
keeps its interactive mode for the terminal. The UI only ever runs commands non-interactively.
