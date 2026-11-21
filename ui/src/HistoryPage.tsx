import { type FormEvent, useEffect, useState } from "react";
import { getCommands, getHistory, type Command, type HistoryFilters, type HistoryPage,
  type RunSummary } from "./api";

const PER_PAGE = 50;
const OUTCOMES = ["succeeded", "failed", "usage-error", "auth-error", "stopped", "changed"];
const KINDS: [string, string][] = [["report", "Report"], ["dry-run", "Dry run"],
  ["apply", "Apply"]];

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

function startOfLocalDay(date: string, plusDays = 0): string {
  const day = new Date(`${date}T00:00:00`);
  day.setDate(day.getDate() + plusDays);
  return day.toISOString();
}

function filtersFor(form: Form, page: number): HistoryFilters {
  return {
    command: form.command, requester: form.requester.trim(), outcome: form.outcome,
    kind: form.kind,
    since: form.from ? startOfLocalDay(form.from) : undefined,
    before: form.to ? startOfLocalDay(form.to, 1) : undefined,
    page, per_page: PER_PAGE,
  };
}

function label(run: RunSummary): string {
  if (run.status === "queued") return "Queued";
  if (run.status === "running") return "Running";
  return run.outcome ?? "—";
}

export function HistoryPage({ hash }: { hash: string }) {
  const { form: fromUrl, page } = formFromHash(hash);
  const [form, setForm] = useState<Form>(fromUrl);
  const [commands, setCommands] = useState<Command[]>([]);
  const [result, setResult] = useState<HistoryPage | null>(null);
  const [error, setError] = useState("");
  const urlKey = hash.split("?")[1] ?? "";

  useEffect(() => { getCommands().then(setCommands).catch(() => setCommands([])); }, []);

  useEffect(() => {
    let current = true;
    const { form: f, page: p } = formFromHash(hash);
    setForm(f);
    setError("");
    getHistory(filtersFor(f, p))
      .then((r) => { if (current) setResult(r); })
      .catch((e: Error) => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [urlKey]);  // eslint-disable-line react-hooks/exhaustive-deps

  function search(e: FormEvent) {
    e.preventDefault();
    window.location.hash = hashFor(form, 1);
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
          <input type="date" value={form.from} onChange={set("from")} />
        </label>
        <label>To
          <input type="date" value={form.to} onChange={set("to")} />
        </label>
        <label>Outcome
          <select value={form.outcome} onChange={set("outcome")}>
            <option value="">Any outcome</option>
            {OUTCOMES.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        </label>
        <label>Kind
          <select value={form.kind} onChange={set("kind")}>
            <option value="">Any kind</option>
            {KINDS.map(([value, text]) => <option key={value} value={value}>{text}</option>)}
          </select>
        </label>
        <div className="actions">
          <button type="submit">Search</button>
          <a href="#/history" className="clear">Clear filters</a>
        </div>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
      {result && (
        <>
          <p role="status">{result.total.toLocaleString()} Run{result.total === 1 ? "" : "s"}
            {result.total > 0 && ` · page ${result.page} of ${pages}`}</p>
          {result.runs.length > 0 && (
            <table className="history">
              <thead><tr><th>Requested</th><th>Command</th><th>Kind</th><th>Admin</th>
                <th>Outcome</th></tr></thead>
              <tbody>
                {result.runs.map((run) => (
                  <tr key={run.id}>
                    <td>{new Date(run.queued_at).toLocaleString()}</td>
                    <td><a href={`#/runs/${encodeURIComponent(run.id)}`}>
                      {run.command} {run.argv.join(" ")}</a></td>
                    <td>{KINDS.find(([v]) => v === run.kind)?.[1] ?? run.kind}</td>
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
