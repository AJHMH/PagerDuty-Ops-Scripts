import { useEffect, useState } from "react";
import { getRun, type Run } from "./api";

const OUTCOME_LABELS: Record<Run["outcome"], string> = {
  "succeeded": "Succeeded",
  "failed": "Failed",
  "usage-error": "Usage error",
  "auth-error": "Auth error: the service credential was rejected",
  "stopped": "Stopped",
};

export function RunPage({ id }: { id: string }) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getRun(id).then(setRun).catch((e: Error) => setError(e.message));
  }, [id]);

  if (error) return <p className="error" role="alert">{error}</p>;
  if (!run) return <p>Loading…</p>;
  return (
    <article>
      <h1>{run.command} {run.argv.join(" ")}</h1>
      <p className={`outcome ${run.outcome}`}>{OUTCOME_LABELS[run.outcome]} (exit {run.exit_code})</p>
      <dl>
        <dt>Requested by</dt><dd>{run.requester_name} ({run.requested_by}, {run.requester_role})</dd>
        <dt>Started</dt><dd>{new Date(run.started_at).toLocaleString()}</dd>
        <dt>Finished</dt><dd>{run.finished_at ? new Date(run.finished_at).toLocaleString() : "—"}</dd>
      </dl>
      <h2>Output</h2>
      <pre>{run.stdout || "(none)"}</pre>
      <h2>Log</h2>
      <pre>{run.stderr || "(none)"}</pre>
      <p><a href="#/">Run another report</a></p>
    </article>
  );
}
