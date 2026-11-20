import { type FormEvent, useEffect, useMemo, useState } from "react";
import { getCommands, getForm, startRun, type Command, type CommandForm, type Field,
  type Values } from "./api";
import { FieldInput, type FieldValue } from "./FieldInput";

// Turn what was typed into the values the service expects; empty and default choices are left
// out so the command line stays minimal.
function collect(fields: Field[], state: Record<string, FieldValue>) {
  const values: Values = {};
  const files: Record<string, File[]> = {};
  for (const f of fields) {
    const v = state[f.dest];
    if (v === undefined || v === "" || v === false) continue;
    if (Array.isArray(v)) { files[f.dest] = v; continue; }
    if (f.kind === "choice" && String(v) === String(f.default)) continue;
    if (f.kind === "number" || f.kind === "count") values[f.dest] = Number(v);
    else if (f.multiple && typeof v === "string") {
      values[f.dest] = v.split("\n").map((s) => s.trim()).filter(Boolean);
    } else values[f.dest] = v;
  }
  return { values, files };
}

function Fields({ fields, state, set }: {
  fields: Field[];
  state: Record<string, FieldValue>;
  set: (dest: string, v: FieldValue) => void;
}) {
  const groups = new Map<number, string[]>();
  for (const f of fields) {
    if (f.group != null) groups.set(f.group, [...(groups.get(f.group) ?? []), f.flags[0]]);
  }
  return (
    <>
      {fields.map((f) => (
        <div key={f.dest} className="field">
          <FieldInput field={f} value={state[f.dest]} onChange={(v) => set(f.dest, v)} />
          {f.group != null && groups.get(f.group)![0] === f.flags[0] && (
            <small className="hint">Use only one of {groups.get(f.group)!.join(", ")}.</small>
          )}
        </div>
      ))}
    </>
  );
}

export function RunForm() {
  const [commands, setCommands] = useState<Command[]>([]);
  const [command, setCommand] = useState("");
  const [form, setForm] = useState<CommandForm | null>(null);
  const [subcommand, setSubcommand] = useState<string | null>(null);
  const [state, setState] = useState<Record<string, FieldValue>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    getCommands()
      .then((all) => {
        const runnable = all.filter((c) => c.has_read_only_mode);
        setCommands(runnable);
        if (runnable.length) setCommand(runnable[0].name);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!command) return;
    // Responses can arrive out of order when the command changes quickly: only the one for
    // the command still selected may update the form.
    let current = true;
    setForm(null);
    setState({});
    setError("");
    getForm(command)
      .then((f) => {
        if (!current) return;
        setForm(f);
        // Phase 1 runs read-only modes only; the service refuses the rest anyway.
        setSubcommand(f.subcommands?.find((s) => !s.writes)?.name ?? null);
      })
      .catch((e: Error) => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [command]);

  const sub = useMemo(() => form?.subcommands?.find((s) => s.name === subcommand) ?? null,
    [form, subcommand]);
  const set = (dest: string, v: FieldValue) => setState((s) => ({ ...s, [dest]: v }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!form) return;
    setBusy(true);
    setError("");
    try {
      const { values, files } = collect([...form.fields, ...(sub?.fields ?? [])], state);
      const run = await startRun(command, subcommand, values, files);
      window.location.hash = `#/runs/${encodeURIComponent(run.id)}`;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit}>
      <h1>Run a report</h1>
      <label>
        Command
        <select value={command} onChange={(e) => setCommand(e.target.value)}>
          {commands.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
        </select>
      </label>
      {form && <p className="hint">{form.description}</p>}
      {form?.subcommands && (
        <label>
          Subcommand
          <select value={subcommand ?? ""} onChange={(e) => { setSubcommand(e.target.value); setState({}); }}>
            {form.subcommands.filter((s) => !s.writes).map((s) => (
              <option key={s.name} value={s.name}>{s.name}{s.help ? ` — ${s.help}` : ""}</option>
            ))}
          </select>
        </label>
      )}
      {form && <Fields fields={form.fields} state={state} set={set} />}
      {sub && <Fields fields={sub.fields} state={state} set={set} />}
      {error && <p className="error" role="alert">{error}</p>}
      <button type="submit" disabled={busy || !form}>{busy ? "Running…" : "Run"}</button>
    </form>
  );
}
