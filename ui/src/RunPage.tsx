import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { applyRun, cancelRun, fileUrl, freshDryRun, getRun, reviewRun, type Me,
  type Outcome, type Review, type Run } from "./api";

const POLL_MS = 1000;

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

const OUTCOME_LABELS: Record<Outcome, string> = {
  "succeeded": "Succeeded",
  "failed": "Failed",
  "usage-error": "Usage error",
  "auth-error": "Auth error: the service credential was rejected",
  "stopped": "Stopped",
  "changed": "Not applied: the changes differ from what was reviewed",
};

function statusLabel(run: Run): string {
  if (run.status === "queued") return "Waiting in the queue: one change runs at a time";
  if (run.status === "running") {
    return run.kind === "apply" ? "Applying… (a fresh dry run is checked first)" : "Running…";
  }
  return run.outcome ? OUTCOME_LABELS[run.outcome] : "Finished";
}

const runLink = (id: string) => `#/runs/${encodeURIComponent(id)}`;

// Runs one step (cancel, review, apply, fresh dry run), then shows the Run it returns:
// this one updated in place, or a new one by navigating to it.
function useStep(run: Run, onChange: (run: Run) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function act(step: () => Promise<Run>) {
    setBusy(true);
    setError("");
    try {
      const result = await step();
      if (result.id === run.id) onChange(result);
      else window.location.hash = runLink(result.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return { busy, error, act };
}

// Cancel (the requester, or any Approver) while a Run waits or executes; then, for a
// Stopped apply, a fresh dry run with the same inputs shows what remains (ADR 0012).
function RunControls({ run, me, onChange }: { run: Run; me: Me; onChange: (run: Run) => void }) {
  const { busy, error, act } = useStep(run, onChange);
  const mine = run.requested_by === me.email;

  let control = null;
  if (run.status !== "finished" && (mine || me.role === "approver")) {
    control = (
      <button type="button" className="danger" disabled={busy || run.cancelled_by !== null}
              onClick={() => act(() => cancelRun(run.id))}>
        {run.cancelled_by ? "Cancelling…" : run.status === "queued" ? "Remove from the queue"
          : "Cancel this Run"}
      </button>
    );
  } else if (run.kind === "apply" && run.outcome === "stopped" && mine && me.role !== "viewer") {
    control = (
      <>
        <p>{run.started_at ? "This apply was stopped and may have been partly applied."
          : "This apply was stopped before it started."} Applies are safe to repeat: a fresh
          dry run with the same inputs shows what remains.</p>
        <button type="button" disabled={busy} onClick={() => act(() => freshDryRun(run.id))}>
          Run a fresh dry run
        </button>
      </>
    );
  } else if (run.kind === "apply" && run.outcome === "changed" && run.fresh_dry_run_id) {
    control = (
      <p>Nothing was applied. <a href={runLink(run.fresh_dry_run_id)}>Review the fresh dry
        run</a> to see what would change now.</p>
    );
  }
  if (!control) return null;
  return (
    <section aria-label="Run controls" className="steps">
      {control}
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  );
}

// Review, then Apply (#73): only for the admin who ran a dry run that succeeded. The service
// enforces every gate; this only decides which step to offer.
function ApplySteps({ run, me, onChange }: { run: Run; me: Me; onChange: (run: Run) => void }) {
  const { busy, error, act } = useStep(run, onChange);
  const [understood, setUnderstood] = useState(false);
  const mine = run.requested_by === me.email && me.role !== "viewer";
  if (run.kind !== "dry-run" || !mine || run.outcome !== "succeeded" || !run.review?.complete) {
    return null;
  }
  if (run.applied_run_id) {
    return (
      <p className="notice">Sent to be applied: <a href={runLink(run.applied_run_id)}>see
        the apply</a>.</p>
    );
  }

  return (
    <section aria-label="Apply" className="steps">
      {!run.reviewed_by ? (
        <button type="button" disabled={busy} onClick={() => act(() => reviewRun(run.id))}>
          I've reviewed these changes
        </button>
      ) : (
        <>
          <p>Reviewed by you. The apply waits its turn (one change runs at a time), then
            runs a fresh dry run first; if anything differs from what you reviewed, nothing is
            applied and you'll see the new changes.</p>
          <label className="check">
            <input type="checkbox" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} />
            I understand this will change live data.
          </label>
          <button type="button" className="danger" disabled={busy || !understood}
                  onClick={() => act(() => applyRun(run.id))}>
            {busy ? "Sending…" : "Apply these changes"}
          </button>
        </>
      )}
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  );
}

// The command's log. While the Run executes, the service stores each complete line as it
// arrives (#68) and this page re-reads the Run, so the log grows here. It stays scrolled to
// the newest line unless the reader has scrolled up to look at something.
function LiveLog({ text, live }: { text: string; live: boolean }) {
  const ref = useRef<HTMLPreElement>(null);
  const following = useRef(true);
  const wasLive = useRef(live);
  useLayoutEffect(() => {
    const pre = ref.current;
    // Also on the update that finishes the Run: it adds the last lines and replaces a
    // trimmed live tail with the whole log. A Run opened after it finished doesn't jump.
    if (pre && (live || wasLive.current) && following.current) {
      pre.scrollTop = pre.scrollHeight;
    }
    wasLive.current = live;
  }, [text, live]);
  return (
    <>
      <h2>Log{live && <span className="live"> · live</span>}</h2>
      <pre ref={ref} className="log" aria-busy={live}
           onScroll={(e) => {
             const pre = e.currentTarget;
             following.current = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
           }}>
        {text || (live ? "(waiting for the first log line…)" : "(none)")}
      </pre>
    </>
  );
}

export function RunPage({ id, me }: { id: string; me: Me }) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");

  // Each effect applies only its own responses: after moving to another Run, a late
  // response for the previous one is ignored rather than shown under the new Run's URL.
  useEffect(() => {
    let current = true;
    setRun(null);
    setError("");
    getRun(id)
      .then((r) => { if (current) setRun(r); })
      .catch((e: Error) => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [id]);

  // Runs execute in the background: re-read this one until it has finished.
  const unfinished = run !== null && run.id === id && run.status !== "finished";
  useEffect(() => {
    if (!unfinished) return;
    let current = true;
    const timer = window.setTimeout(() => {
      getRun(id)
        .then((r) => { if (current) setRun(r); })
        .catch((e: Error) => { if (current) setError(e.message); });
    }, POLL_MS);
    return () => { current = false; window.clearTimeout(timer); };
  }, [id, unfinished, run]);

  if (error) return <p className="error" role="alert">{error}</p>;
  if (!run) return <p>Loading…</p>;
  return (
    <article>
      <h1>{run.command} {run.argv.join(" ")}</h1>
      <p className={`outcome ${run.outcome ?? run.status}`} role="status">
        {statusLabel(run)}{run.exit_code != null && ` (exit ${run.exit_code})`}
      </p>
      {run.kind === "dry-run" && (
        <p className="notice"><strong>Dry run</strong>: nothing is changed.</p>
      )}
      {run.kind === "apply" && run.applied_from && (
        <p className="notice"><strong>Apply</strong> of the reviewed{" "}
          <a href={runLink(run.applied_from)}>dry run</a>
          {run.fresh_dry_run_id && <>, checked against a{" "}
            <a href={runLink(run.fresh_dry_run_id)}>fresh dry run</a></>}.</p>
      )}
      {run.message && <p className="error" role="alert">{run.message}</p>}
      {run.review && <ReviewPanel review={run.review} />}
      {/* Keyed by run: each dry run needs its own acknowledgement, even when Apply moves
          to a fresh dry run and React would otherwise reuse this component's state. */}
      <ApplySteps key={run.id} run={run} me={me} onChange={setRun} />
      <RunControls key={`controls-${run.id}`} run={run} me={me} onChange={setRun} />
      <dl>
        <dt>Requested by</dt><dd>{run.requester_name} ({run.requested_by}, {run.requester_role})</dd>
        <dt>Requested</dt><dd>{new Date(run.queued_at).toLocaleString()}</dd>
        <dt>Started</dt><dd>{run.started_at ? new Date(run.started_at).toLocaleString() : "—"}</dd>
        {run.cancelled_by && <><dt>Cancelled by</dt><dd>{run.cancelled_by}</dd></>}
        <dt>Finished</dt><dd>{run.finished_at ? new Date(run.finished_at).toLocaleString() : "—"}</dd>
        <dt>Input hash</dt><dd><code>{run.input_hash.slice(0, 16)}…</code></dd>
        {run.output_hash && <><dt>Output hash</dt>
          <dd><code>{run.output_hash.slice(0, 16)}…</code></dd></>}
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
      <LiveLog text={run.stderr} live={run.status === "running"} />
      <p><a href="#/">Run another report</a></p>
    </article>
  );
}
