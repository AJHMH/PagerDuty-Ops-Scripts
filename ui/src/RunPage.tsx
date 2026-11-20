import { useEffect, useState } from "react";
import { fileUrl, getRun, type Review, type Run } from "./api";

const SIGN = { add: "+", change: "~", delete: "−" } as const;

// The dry run's diff: what would be added, changed and deleted, read from the command's own
// preview. Log lines are context, hidden unless asked for.
function ReviewPanel({ review }: { review: Review }) {
  const [showContext, setShowContext] = useState(false);
  const lines = review.lines.filter((l) => showContext || l.kind !== "context");
  const { add, change, delete: del } = review.counts;
  return (
    <section aria-label="Review">
      <h2>What this would change</h2>
      <p className="counts">
        <span className="add">{SIGN.add}{add} to add</span>
        <span className="change">{SIGN.change}{change} to change</span>
        <span className="delete">{SIGN.delete}{del} to delete</span>
      </p>
      {add + change + del === 0 && <p>No changes: everything already matches.</p>}
      <ol className="diff">
        {lines.map((l, i) => <li key={i} className={l.kind}>{l.text}</li>)}
      </ol>
      <label className="check">
        <input type="checkbox" checked={showContext} onChange={(e) => setShowContext(e.target.checked)} />
        Show log lines
      </label>
    </section>
  );
}

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
      <p className={`outcome ${run.outcome}`}>
        {OUTCOME_LABELS[run.outcome]}{run.exit_code != null && ` (exit ${run.exit_code})`}
      </p>
      {run.kind === "dry-run" && (
        <p className="notice"><strong>Dry run</strong>: nothing was changed.</p>
      )}
      {run.message && <p className="error" role="alert">{run.message}</p>}
      {run.review && <ReviewPanel review={run.review} />}
      <dl>
        <dt>Requested by</dt><dd>{run.requester_name} ({run.requested_by}, {run.requester_role})</dd>
        <dt>Started</dt><dd>{new Date(run.started_at).toLocaleString()}</dd>
        <dt>Finished</dt><dd>{run.finished_at ? new Date(run.finished_at).toLocaleString() : "—"}</dd>
        <dt>Input hash</dt><dd><code>{run.input_hash.slice(0, 16)}…</code></dd>
        <dt>Output hash</dt><dd><code>{run.output_hash.slice(0, 16)}…</code></dd>
      </dl>
      {run.files.length > 0 && (
        <>
          <h2>Files</h2>
          <table>
            <thead><tr><th>File</th><th>Kind</th><th>Size</th><th>SHA-256</th></tr></thead>
            <tbody>
              {run.files.map((f) => (
                <tr key={f.id}>
                  <td><a href={fileUrl(run.id, f.id)} download>{f.path}</a></td>
                  <td>{f.role}</td>
                  <td>{f.size.toLocaleString()} B</td>
                  <td><code title={f.sha256}>{f.sha256.slice(0, 12)}…</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <h2>Output</h2>
      <pre>{run.stdout || "(none)"}</pre>
      <h2>Log</h2>
      <pre>{run.stderr || "(none)"}</pre>
      <p><a href="#/">Run another report</a></p>
    </article>
  );
}
