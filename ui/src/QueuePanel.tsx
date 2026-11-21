import { useEffect, useState } from "react";
import { getQueue, type Queue, type RunSummary } from "./api";

const POLL_MS = 5000;

function Entry({ run }: { run: RunSummary }) {
  return (
    <li>
      <a href={`#/runs/${encodeURIComponent(run.id)}`}>{run.command} {run.argv.join(" ")}</a>
      {" "}· {run.requester_name} · since{" "}
      {new Date(run.started_at ?? run.queued_at).toLocaleTimeString()}
    </li>
  );
}

// One change runs at a time across the account (ADR 0012): what is applying now, and what
// waits for it. Reports and dry runs never wait, so they don't appear here.
export function QueuePanel() {
  const [queue, setQueue] = useState<Queue | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let timer = 0;
    const load = () => getQueue()
      .then((q) => { setQueue(q); setError(""); })
      .catch((e: Error) => setError(e.message))
      .finally(() => { timer = window.setTimeout(load, POLL_MS); });
    load();
    return () => window.clearTimeout(timer);
  }, []);

  if (error) return <p className="error" role="alert">{error}</p>;
  if (!queue) return null;
  return (
    <section aria-label="Changes queue" className="queue">
      <h2>Changes queue</h2>
      {queue.executing ? (
        <>
          <p>Applying now:</p>
          <ul><Entry run={queue.executing} /></ul>
        </>
      ) : <p>Nothing is being applied.</p>}
      {queue.pending.length > 0 && (
        <>
          <p>Waiting, in order:</p>
          <ol>{queue.pending.map((r) => <Entry key={r.id} run={r} />)}</ol>
        </>
      )}
    </section>
  );
}
