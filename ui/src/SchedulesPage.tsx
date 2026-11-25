import { type FormEvent, useEffect, useMemo, useState } from "react";
import { createSchedule, DAY_NAMES, deleteSchedule, editSchedule, EVERY_HOURS, getCommands,
  getForm, getSchedules, pauseSchedule, type Command, type CommandForm, type Field,
  type Role, type Schedule, type Timing, type TimingKind, type Values } from "./api";
import { type FieldValue } from "./FieldInput";
import { collect, Fields } from "./RunForm";

// Scheduled runs (#80, ADR 0014): read-only commands on a schedule, in place of cron. The
// service refuses anything that writes, and judges each schedule again every time it fires;
// this page only offers what it would accept. Schedules take arguments, not files.
const NO_FILES = (f: Field) => f.kind !== "input-file" && f.kind !== "input-dir";
const NEW_TIMING: Timing = { kind: "daily", at: "02:30", days: [0, 1, 2, 3, 4],
  every_hours: null };

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

// A schedule's stored values, as the generated inputs hold them.
function stateFrom(values: Values): Record<string, FieldValue> {
  const state: Record<string, FieldValue> = {};
  for (const [dest, v] of Object.entries(values)) {
    state[dest] = Array.isArray(v) ? v.join("\n") : typeof v === "number" ? String(v) : v;
  }
  return state;
}

function ScheduleForm({ editing, onDone }: { editing: Schedule | null; onDone: () => void }) {
  const [commands, setCommands] = useState<Command[]>([]);
  const [name, setName] = useState(editing?.name ?? "");
  const [command, setCommand] = useState(editing?.command ?? "");
  const [form, setForm] = useState<CommandForm | null>(null);
  const [subcommand, setSubcommand] = useState<string | null>(editing?.subcommand ?? null);
  const [state, setState] = useState<Record<string, FieldValue>>(
    editing ? stateFrom(editing.values) : {});
  const [timing, setTiming] = useState<Timing>(editing?.timing ?? NEW_TIMING);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    getCommands()
      .then((all) => {
        const readOnly = all.filter((c) => c.has_read_only_mode);
        setCommands(readOnly);
        if (!command && readOnly.length) {
          setCommand((readOnly.find((c) => c.name === "pd-validate") ?? readOnly[0]).name);
        }
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!command) return;
    let current = true;
    setForm(null);
    getForm(command)
      .then((f) => {
        if (!current) return;
        setForm(f);
        const readOnly = f.subcommands?.filter((s) => !s.writes) ?? [];
        setSubcommand((prev) => (readOnly.some((s) => s.name === prev) ? prev
          : readOnly[0]?.name ?? null));
      })
      .catch((e: Error) => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [command]);

  const sub = useMemo(() => form?.subcommands?.find((s) => s.name === subcommand) ?? null,
    [form, subcommand]);
  const fields = [...(form?.fields ?? []), ...(sub?.fields ?? [])].filter(NO_FILES);
  const set = (dest: string, v: FieldValue) => setState((s) => ({ ...s, [dest]: v }));
  const setKind = (kind: TimingKind) => setTiming((t) => ({ ...t, kind,
    every_hours: kind === "hourly" ? (t.every_hours ?? 6) : null }));
  const toggleDay = (day: number) => setTiming((t) => ({ ...t,
    days: t.days.includes(day) ? t.days.filter((d) => d !== day) : [...t.days, day].sort() }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!form) return;
    setBusy(true);
    setError("");
    try {
      const { values } = collect(fields, state);
      const input = { name: name.trim(), command, subcommand, values, timing };
      await (editing ? editSchedule(editing.id, input) : createSchedule(input));
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit}>
      <h2>{editing ? `Edit “${editing.name}”` : "New schedule"}</h2>
      <label>
        Name
        <input required maxLength={100} value={name} onChange={(e) => setName(e.target.value)}
               placeholder="E.g. Nightly drift report" />
      </label>
      <label>
        Command (read-only commands only)
        <select value={command} onChange={(e) => { setCommand(e.target.value); setState({}); }}>
          {commands.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
        </select>
      </label>
      {form && <p className="hint">{form.description}</p>}
      {form?.subcommands && (
        <label>
          Subcommand
          <select value={subcommand ?? ""}
                  onChange={(e) => { setSubcommand(e.target.value); setState({}); }}>
            {form.subcommands.filter((s) => !s.writes).map((s) => (
              <option key={s.name} value={s.name}>{s.name}{s.help ? ` — ${s.help}` : ""}</option>
            ))}
          </select>
        </label>
      )}
      {form && <Fields fields={fields} state={state} set={set} />}

      <fieldset>
        <legend>When (Central time)</legend>
        {(["daily", "weekly", "hourly"] as const).map((kind) => (
          <label key={kind} className="inline">
            <input type="radio" name="kind" checked={timing.kind === kind}
                   onChange={() => setKind(kind)} />
            {kind === "daily" ? "Every day" : kind === "weekly" ? "Some days of the week"
              : "Every few hours"}
          </label>
        ))}
        {timing.kind === "weekly" && (
          <div>
            {DAY_NAMES.map((day, i) => (
              <label key={day} className="inline">
                <input type="checkbox" checked={timing.days.includes(i)}
                       onChange={() => toggleDay(i)} /> {day}
              </label>
            ))}
          </div>
        )}
        {timing.kind === "hourly" ? (
          <label>
            Every
            <select value={timing.every_hours ?? 6}
                    onChange={(e) => setTiming((t) => ({ ...t, every_hours: Number(e.target.value) }))}>
              {EVERY_HOURS.map((n) => <option key={n} value={n}>{n} hour{n > 1 ? "s" : ""}</option>)}
            </select>
            from midnight, at minute
            <input type="number" min={0} max={59} value={Number(timing.at.slice(3))}
                   onChange={(e) => setTiming((t) => ({ ...t,
                     at: `00:${String(Math.min(59, Math.max(0, Number(e.target.value) || 0))).padStart(2, "0")}` }))} />
          </label>
        ) : (
          <label>
            At
            <input type="time" required value={timing.at}
                   onChange={(e) => setTiming((t) => ({ ...t, at: e.target.value }))} />
          </label>
        )}
      </fieldset>

      {error && <p className="error" role="alert">{error}</p>}
      <button type="submit" disabled={busy || !form}>{busy ? "Saving…" : "Save schedule"}</button>
      {" "}<button type="button" onClick={onDone}>Cancel</button>
    </form>
  );
}

export function SchedulesPage({ role }: { role: Role }) {
  const canChange = role !== "viewer";
  const [schedules, setSchedules] = useState<Schedule[] | null>(null);
  const [editing, setEditing] = useState<Schedule | "new" | null>(null);
  const [error, setError] = useState("");

  const load = () => getSchedules()
    .then((r) => setSchedules(r.schedules))
    .catch((e: Error) => setError(e.message));
  useEffect(() => { load(); }, []);

  async function act(action: () => Promise<unknown>) {
    setError("");
    try { await action(); await load(); } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (editing) {
    return <ScheduleForm editing={editing === "new" ? null : editing}
                         onDone={() => { setEditing(null); load(); }} />;
  }
  return (
    <section>
      <h1>Schedules</h1>
      <p className="hint">Read-only commands run on a schedule, in place of cron. Each Scheduled
        run lands in the History. When a scheduled pd-validate finds Drift, the Teams channel
        is told.</p>
      {error && <p className="error" role="alert">{error}</p>}
      {canChange && <p><button type="button" onClick={() => setEditing("new")}>New schedule</button></p>}
      {schedules === null ? <p>Loading…</p> : schedules.length === 0 ? <p>No schedules yet.</p> : (
        <table>
          <thead>
            <tr><th>Name</th><th>Command</th><th>When</th><th>Next run</th><th>Last run</th>
              <th>Created by</th>{canChange && <th></th>}</tr>
          </thead>
          <tbody>
            {schedules.map((s) => (
              <tr key={s.id}>
                <td>{s.name}{s.paused && <> <strong>(paused)</strong></>}</td>
                <td><code>{s.command}{s.subcommand ? ` ${s.subcommand}` : ""}</code></td>
                <td>{s.description}</td>
                <td>{s.paused ? "—" : when(s.next_run_at)}</td>
                <td>{s.last_run_id
                  ? <a href={`#/runs/${encodeURIComponent(s.last_run_id)}`}>{when(s.last_run_at)}</a>
                  : "—"}</td>
                <td>{s.creator_name}</td>
                {canChange && (
                  <td>
                    <button type="button" onClick={() => setEditing(s)}>Edit</button>{" "}
                    <button type="button" onClick={() => act(() => pauseSchedule(s.id, !s.paused))}>
                      {s.paused ? "Resume" : "Pause"}</button>{" "}
                    <button type="button" onClick={() => {
                      if (window.confirm(`Delete the schedule “${s.name}”? Its Runs stay in the History.`)) {
                        act(() => deleteSchedule(s.id));
                      }
                    }}>Delete</button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
