import { type FormEvent, useEffect, useState } from "react";
import { getCommands, getLedger, OUTCOMES, type Command, type LedgerPage as Page } from "./api";
import { startOfLocalDay, TIME_ZONE } from "./HistoryPage";

const PER_PAGE = 50;

interface Form { command: string; requester: string; from: string; to: string; outcome: string }
const EMPTY: Form = { command: "", requester: "", from: "", to: "", outcome: "" };

function parse(urlKey: string): { form: Form; page: number } {
  const query = new URLSearchParams(urlKey);
  const form = { ...EMPTY };
  for (const key of Object.keys(EMPTY) as (keyof Form)[]) form[key] = query.get(key) ?? "";
  return { form, page: Math.max(1, Number(query.get("page")) || 1) };
}

function hashFor(form: Form, page: number): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(form)) if (value) query.set(key, value);
  if (page > 1) query.set("page", String(page));
  const text = query.toString();
  return `#/ledger${text ? `?${text}` : ""}`;
}

// The permanent Ledger (#76, ADR 0013): one row per finished Writing run, kept after the Run
// itself is purged. A purged Run's row has no page to link to, and says so.
export function LedgerPage({ hash }: { hash: string }) {
  const urlKey = hash.split("?")[1] ?? "";
  const { page } = parse(urlKey);
  const [form, setForm] = useState<Form>(parse(urlKey).form);
  const [commands, setCommands] = useState<Command[]>([]);
  const [result, setResult] = useState<Page | null>(null);
  const [error, setError] = useState("");
  const [searches, setSearches] = useState(0);

  useEffect(() => { getCommands().then(setCommands).catch(() => setCommands([])); }, []);

  useEffect(() => {
    let current = true;
    const { form: f, page: p } = parse(urlKey);
    setForm(f);
    setResult(null);
    setError("");
    getLedger({
      command: f.command, requester: f.requester.trim(), outcome: f.outcome,
      since: f.from ? startOfLocalDay(f.from) : undefined,
      before: f.to ? startOfLocalDay(f.to, 1) : undefined, page: p, per_page: PER_PAGE,
    })
      .then((r) => { if (current) setResult(r); })
      .catch((e: Error) => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [urlKey, searches]);

  function go(next: Form) {
    const target = hashFor(next, 1);
    if (window.location.hash === target) setSearches((n) => n + 1);
    else window.location.hash = target;
  }

  const set = (key: keyof Form) => (e: { target: { value: string } }) =>
    setForm({ ...form, [key]: e.target.value });
  const pages = result ? Math.max(1, Math.ceil(result.total / result.per_page)) : 1;

  return (
    <article>
      <h1>Ledger</h1>
      <p className="hint">Who requested and approved each change, and how it ended. Kept after
        the Run itself is purged{result ? ` at ${result.retention_days} days` : ""}; it never
        holds arguments, inputs or output.</p>
      {result && !result.enabled && (
        <p className="notice">The Ledger is switched off (PD_UI_LEDGER=off): no new rows are
          written, and the rows already here are purged with their Runs.</p>
      )}
      <form className="filters" onSubmit={(e: FormEvent) => { e.preventDefault(); go(form); }}
            aria-label="Filter the Ledger">
        <label>Command
          <select value={form.command} onChange={set("command")}>
            <option value="">Any command</option>
            {commands.filter((c) => !c.read_only).map((c) => (
              <option key={c.name} value={c.name}>{c.name}</option>))}
          </select>
        </label>
        <label>Admin
          <input type="text" value={form.requester} onChange={set("requester")}
                 placeholder="Name or email" />
        </label>
        <label>From <input type="date" value={form.from} onChange={set("from")} /></label>
        <label>To <input type="date" value={form.to} onChange={set("to")} /></label>
        <label>Outcome
          <select value={form.outcome} onChange={set("outcome")}>
            <option value="">Any outcome</option>
            {OUTCOMES.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        </label>
        <div className="actions">
          <button type="submit">Search</button>
          <button type="button" className="link" onClick={() => go(EMPTY)}>Clear filters</button>
        </div>
      </form>
      <p className="hint">Dates are whole days in {TIME_ZONE}; To includes its day.</p>
      {error && <p className="error" role="alert">{error}</p>}
      {!result && !error && <p>Searching…</p>}
      {result && (
        <>
          <p role="status">{result.total.toLocaleString()} row{result.total === 1 ? "" : "s"}
            {result.total > 0 && page <= pages && ` · page ${result.page} of ${pages}`}</p>
          {result.rows.length > 0 && (
            <table className="history">
              <thead><tr><th>Requested</th><th>Command</th><th>Requested by</th>
                <th>Approved by</th><th>Finished</th><th>Outcome</th></tr></thead>
              <tbody>
                {result.rows.map((row) => (
                  <tr key={row.run_id}>
                    <td>{new Date(row.queued_at).toLocaleString()}</td>
                    <td>{row.run_kept
                      ? <a href={`#/runs/${encodeURIComponent(row.run_id)}`}
                           title="The Run's own page, until it is purged">{row.command}</a>
                      : <>{row.command} <span className="hint">(Run purged)</span></>}</td>
                    <td title={row.requested_by}>{row.requester_name}</td>
                    <td>{row.approved_by ?? "—"}</td>
                    <td>{row.finished_at ? new Date(row.finished_at).toLocaleString() : "—"}</td>
                    <td className={`outcome ${row.outcome}`}>{row.outcome}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {pages > 1 && (
            <nav className="pager" aria-label="Pages">
              {page > 1 && <a href={hashFor(parse(urlKey).form, page - 1)}>← Newer</a>}
              {page < pages && <a href={hashFor(parse(urlKey).form, page + 1)}>Older →</a>}
            </nav>
          )}
        </>
      )}
    </article>
  );
}
