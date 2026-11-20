import { useEffect, useState } from "react";
import { ApiError, applyRun, fileUrl, getRun, reviewRun, type Me, type Review,
  type Run } from "./api";

const SIGN = { add: "+", change: "~", delete: "−" } as const;

// The dry run's diff: what would be added, changed and deleted, read from the command's own
// preview. Log lines are context, hidden unless asked for.
function ReviewPanel({ review }: { review: Review }) {
  const [showContext, setShowContext] = useState(false);
  const lines = review.lines.filter((l) => showContext || l.kind !== "context");
  const { add, change, delete: del, other } = review.counts;
  return (
    <section aria-label="Review">
      <h2>What this would change</h2>
      <p className="counts">
        <span className="add">{SIGN.add}{add} to add</span>
        <span className="change">{SIGN.change}{change} to change</span>
        <span className="delete">{SIGN.delete}{del} to delete</span>
        {other > 0 && <span className="other">{other} other change{other === 1 ? "" : "s"}</span>}
      </p>
      {!review.complete && (
        <p className="error" role="alert">The dry run didn't finish, so what it would change
          couldn't be determined. See the log below.</p>
      )}
      {review.complete && add + change + del + other === 0 && (
        <p>No changes: everything already matches.</p>
      )}
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

// Review, then Apply (#73): only for the admin who ran a dry run that succeeded. The service
// enforces every gate; this only decides which step to offer.
function ApplySteps({ run, me, onChange }: { run: Run; me: Me; onChange: (run: Run) => void }) {
  const [busy, setBusy] = useState(false);
  const [understood, setUnderstood] = useState(false);
  const [error, setError] = useState("");
  const mine = run.requested_by === me.email && me.role !== "viewer";
  if (run.kind !== "dry-run" || !mine || run.outcome !== "succeeded" || !run.review?.complete) {
    return null;
  }
  if (run.applied_run_id) {
    return (
      <p className="notice">Applied: <a href={`#/runs/${encodeURIComponent(run.applied_run_id)}`}>
        see the apply</a>.</p>
    );
  }

  async function act(step: () => Promise<Run>) {
    setBusy(true);
    setError("");
    try {
      const result = await step();
      if (result.id === run.id) onChange(result);
      else window.location.hash = `#/runs/${encodeURIComponent(result.id)}`;
    } catch (e) {
      const fresh = e instanceof ApiError && typeof e.detail === "object" && e.detail !== null
        ? (e.detail as { fresh_dry_run_id?: string }).fresh_dry_run_id : undefined;
      if (fresh) {
        sessionStorage.setItem("pd-ops-notice", e instanceof Error ? e.message : "");
        window.location.hash = `#/runs/${encodeURIComponent(fresh)}`;
      } else setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Apply" className="steps">
      {!run.reviewed_by ? (
        <button type="button" disabled={busy} onClick={() => act(() => reviewRun(run.id))}>
          I've reviewed these changes
        </button>
      ) : (
        <>
          <p>Reviewed by you. Applying runs a fresh dry run first; if anything differs from
            what you reviewed, nothing is applied and you'll see the new changes.</p>
          <label className="check">
            <input type="checkbox" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} />
            I understand this will change live data.
          </label>
          <button type="button" className="danger" disabled={busy || !understood}
                  onClick={() => act(() => applyRun(run.id))}>
            {busy ? "Applying…" : "Apply these changes"}
          </button>
        </>
      )}
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  );
}

export function RunPage({ id, me }: { id: string; me: Me }) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    setNotice(sessionStorage.getItem("pd-ops-notice") ?? "");
    sessionStorage.removeItem("pd-ops-notice");
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
      {notice && <p className="error" role="alert">{notice}</p>}
      {run.kind === "dry-run" && (
        <p className="notice"><strong>Dry run</strong>: nothing was changed.</p>
      )}
      {run.kind === "apply" && run.applied_from && (
        <p className="notice"><strong>Apply</strong> of the reviewed{" "}
          <a href={`#/runs/${encodeURIComponent(run.applied_from)}`}>dry run</a>.</p>
      )}
      {run.message && <p className="error" role="alert">{run.message}</p>}
      {run.review && <ReviewPanel review={run.review} />}
      {/* Keyed by run: each dry run needs its own acknowledgement, even when Apply moves
          to a fresh dry run and React would otherwise reuse this component's state. */}
      <ApplySteps key={run.id} run={run} me={me} onChange={setRun} />
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
