import { useEffect, useState } from "react";
import { getApprovals, type RunSummary } from "./api";

const POLL_MS = 10000;

// Requests waiting for an Approver (#77), oldest first. Each opens its Run page, where an
// Approver other than the requester reads the diff and approves or rejects.
export function ApprovalsPanel() {
  const [pending, setPending] = useState<RunSummary[] | null>(null);

  useEffect(() => {
    let timer = 0;
    let current = true;
    const load = () => getApprovals()
      .then((r) => { if (current) setPending(r.pending); })
      .catch(() => { if (current) setPending(null); })
      .finally(() => { if (current) timer = window.setTimeout(load, POLL_MS); });
    load();
    return () => { current = false; window.clearTimeout(timer); };
  }, []);

  if (!pending) return null;
  return (
    <section aria-label="Waiting for approval" className="queue">
      <h2>Waiting for approval</h2>
      {pending.length === 0 ? <p>Nothing is waiting for an Approver.</p> : (
        <ol>
          {pending.map((run) => (
            <li key={run.id}>
              <a href={`#/runs/${encodeURIComponent(run.id)}`}>{run.command} {run.argv.join(" ")}</a>
              {" "}· {run.requester_name} · {run.approval?.reasons.join("; ")}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
