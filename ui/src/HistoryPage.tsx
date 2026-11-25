import { type FormEvent, useEffect, useState } from "react";
import { getCommands, getHistory, OUTCOMES, RUN_KINDS, type Command, type HistoryFilters,
  type HistoryPage, type RunSummary } from "./api";

const PER_PAGE = 50;
const UNFINISHED = "unfinished";  // the service's filter for Runs still queued or running
export const TIME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;

// The filters live in the URL (#/history?command=…), so a search can be shared or
// bookmarked. Dates are whole local days; "to" includes its day.
interface Form {
  command: string;
  requester: string;
  from: string;   // yyyy-mm-dd
  to: string;
  outcome: string;
  kind: string;
}

const EMPTY: Form = { command: "", requester: "", from: "", to: "", outcome: "", kind: "" };

function formFromHash(hash: string): { form: Form; page: number } {
  const query = new URLSearchParams(hash.split("?")[1] ?? "");
  const form = { ...EMPTY };
  for (const key of Object.keys(EMPTY) as (keyof Form)[]) form[key] = query.get(key) ?? "";
  return { form, page: Math.max(1, Number(query.get("page")) || 1) };
}

function hashFor(form: Form, page: number): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(form)) if (value) query.set(key, value);
  if (page > 1) query.set("page", String(page));
  const text = query.toString();
  return `#/history${text ? `?${text}` : ""}`;
}

// Local midnight of a yyyy-mm-dd day (plus some days), as UTC; undefined for a date that
// isn't one, e.g. from a hand-edited link.
export function startOfLocalDay(date: string, plusDays = 0): string | undefined {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return undefined;
  const day = new Date(`${date}T00:00:00`);
  if (Number.isNaN(day.getTime())) return undefined;
  day.setDate(day.getDate() + plusDays);
  return day.toISOString();
}

function filtersFor(form: Form, page: number): { filters: HistoryFilters; badDate: boolean } {
  const since = form.from ? startOfLocalDay(form.from) : undefined;
  const before = form.to ? startOfLocalDay(form.to, 1) : undefined;
  return {
    filters: {
      command: form.command, requester: form.requester.trim(), outcome: form.outcome,
      kind: form.kind, since, before, page, per_page: PER_PAGE,
    },
    badDate: (!!form.from && !since) || (!!form.to && !before),
  };
}

function label(run: RunSummary): string {
  if (run.status === "queued") return "Queued";
  if (run.status === "running") return "Running";
  return run.outcome ?? "—";
}

export function HistoryPage({ hash }: { hash: string }) {
  const urlKey = hash.split("?")[1] ?? "";
  const { form: fromUrl, page } = formFromHash(urlKey ? `?${urlKey}` : "");
  const [form, setForm] = useState<Form>(fromUrl);
  const [commands, setCommands] = useState<Command[]>([]);
  const [result, setResult] = useState<HistoryPage | null>(null);
  const [error, setError] = useState("");
  const [badDate, setBadDate] = useState(false);
  const [searches, setSearches] = useState(0);  // Search and Clear always search again

  useEffect(() => { getCommands().then(setCommands).catch(() => setCommands([])); }, []);

  useEffect(() => {
    let current = true;
    const { form: f, page: p } = formFromHash(urlKey ? `?${urlKey}` : "");
    const { filters, badDate: bad } = filtersFor(f, p);
    setForm(f);
    setBadDate(bad);
    setResult(null);  // never show the previous search's rows under a new one
    setError("");
    getHistory(filters)
      .then((r) => { if (current) setResult(r); })
      .catch((e: Error) => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [urlKey, searches]);

  function go(next: Form) {
    const target = hashFor(next, 1);
    if (window.location.hash === target) setSearches((n) => n + 1);
    else window.location.hash = target;
  }

  function search(e: FormEvent) {
    e.preventDefault();
    go(form);
  }

  const set = (key: keyof Form) => (e: { target: { value: string } }) =>
    setForm({ ...form, [key]: e.target.value });
  const pages = result ? Math.max(1, Math.ceil(result.total / result.per_page)) : 1;

  return (
    <article>
      <h1>Run history</h1>
      <form className="filters" onSubmit={search} aria-label="Filter Runs">
        <label>Command
          <select value={form.command} onChange={set("command")}>
            <option value="">Any command</option>
            {commands.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
          </select>
        </label>
        <label>Admin
          <input type="text" value={form.requester} onChange={set("requester")}
                 placeholder="Name or email" />
        </label>
        <label>From
          <input type="date" value={form.from} onChange={set("from")}
                 aria-describedby="zone" />
        </label>
        <label>To
          <input type="date" value={form.to} onChange={set("to")} aria-describedby="zone" />
        </label>
        <label>Outcome
          <select value={form.outcome} onChange={set("outcome")}>
            <option value="">Any outcome</option>
            <option value={UNFINISHED}>Queued or running</option>
            {OUTCOMES.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        </label>
        <label>Kind
          <select value={form.kind} onChange={set("kind")}>
            <option value="">Any kind</option>
            {RUN_KINDS.map(([value, text]) => (
              <option key={value} value={value}>{text}</option>))}
          </select>
        </label>
        <div className="actions">
          <button type="submit">Search</button>
          <button type="button" className="link" onClick={() => go(EMPTY)}>
            Clear filters</button>
        </div>
      </form>
      <p id="zone" className="hint">Dates are whole days in {TIME_ZONE}; To includes its day.</p>
      {badDate && <p className="error" role="alert">A date in this link isn't a valid date,
        so it was ignored.</p>}
      {error && <p className="error" role="alert">{error}</p>}
      {!result && !error && <p>Searching…</p>}
      {result && (
        <>
          <p role="status">{result.total.toLocaleString()} Run{result.total === 1 ? "" : "s"}
            {result.total > 0 && page <= pages && ` · page ${result.page} of ${pages}`}</p>
          {result.total > 0 && page > pages && (
            <p>There's no page {page} for this search. <a href={hashFor(fromUrl, 1)}>Go to the
              newest Runs</a>.</p>
          )}
          {result.runs.length > 0 && (
            <table className="history">
              <thead><tr><th>Requested</th><th>Command</th><th>Kind</th><th>Admin</th>
                <th>Outcome</th></tr></thead>
              <tbody>
                {result.runs.map((run) => (
                  <tr key={run.id}>
                    <td>{new Date(run.queued_at).toLocaleString()}</td>
                    <td><a href={`#/runs/${encodeURIComponent(run.id)}`}>
                      {run.command} {run.argv.join(" ")}</a>
                      {run.cap_override && (
                        <p className="override-line"><span className="badge">Cap override
                        </span> {run.cap_override.caps.map((c) => `${c.flag} ${c.value}`)
                          .join(", ")}: “{run.cap_override.reason}” ({run.cap_override.by})</p>
                      )}</td>
                    <td>{RUN_KINDS.find(([v]) => v === run.kind)?.[1] ?? run.kind}
                      {run.schedule_id && <> (scheduled{run.drift ? ", Drift found" : ""})</>}</td>
                    <td title={run.requested_by}>{run.requester_name}</td>
                    <td className={`outcome ${run.outcome ?? run.status}`}>{label(run)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {pages > 1 && (
            <nav className="pager" aria-label="Pages">
              {page > 1 && <a href={hashFor(fromUrl, page - 1)}>← Newer</a>}
              {page < pages && <a href={hashFor(fromUrl, page + 1)}>Older →</a>}
            </nav>
          )}
        </>
      )}
    </article>
  );
}
