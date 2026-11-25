import { type FormEvent, useEffect, useMemo, useState } from "react";
import { getCommands, getForm, startRun, type Command, type CommandForm, type Field,
  type Role, type Values } from "./api";
import { FieldInput, type FieldValue } from "./FieldInput";

// Turn what was typed into the values the service expects; empty and default choices are left
// out so the command line stays minimal.
export function collect(fields: Field[], state: Record<string, FieldValue>) {
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

export function Fields({ fields, state, set }: {
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

// Commands that write run only as dry runs (#72), and only for Operators and Approvers; the
// service enforces both, this only decides what to offer.
export function RunForm({ role }: { role: Role }) {
  const canDryRun = role !== "viewer";
  const [commands, setCommands] = useState<Command[]>([]);
  const [command, setCommand] = useState("");
  const [form, setForm] = useState<CommandForm | null>(null);
  const [subcommand, setSubcommand] = useState<string | null>(null);
  const [state, setState] = useState<Record<string, FieldValue>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [reason, setReason] = useState("");

  useEffect(() => {
    getCommands()
      .then((all) => {
        const runnable = all.filter((c) => canDryRun || c.has_read_only_mode);
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
        const offered = f.subcommands?.filter((s) => canDryRun || !s.writes) ?? [];
        setSubcommand((offered.find((s) => !s.writes) ?? offered[0])?.name ?? null);
      })
      .catch((e: Error) => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [command, canDryRun]);

  const sub = useMemo(() => form?.subcommands?.find((s) => s.name === subcommand) ?? null,
    [form, subcommand]);
  const set = (dest: string, v: FieldValue) => setState((s) => ({ ...s, [dest]: v }));
  const writes = sub ? sub.writes : Boolean(form?.writes);
  // Write caps (marked by the engine) set above their defaults: the service refuses these
  // without a recorded reason (#75), so ask for one as soon as a cap goes up.
  const raised = [...(form?.fields ?? []), ...(sub?.fields ?? [])].filter((f) => {
    const v = state[f.dest];
    return f.cap && typeof f.default === "number" && v !== undefined && v !== ""
      && Number(v) > f.default;
  });

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!form) return;
    setBusy(true);
    setError("");
    try {
      const { values, files } = collect([...form.fields, ...(sub?.fields ?? [])], state);
      const run = await startRun(command, subcommand, values, files,
                                 raised.length ? reason.trim() : undefined);
      window.location.hash = `#/runs/${encodeURIComponent(run.id)}`;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit}>
      <h1>{canDryRun ? "Run a report or a dry run" : "Run a report"}</h1>
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
            {form.subcommands.filter((s) => canDryRun || !s.writes).map((s) => (
              <option key={s.name} value={s.name}>
                {s.name}{s.writes ? " (dry run)" : ""}{s.help ? ` — ${s.help}` : ""}
              </option>
            ))}
          </select>
        </label>
      )}
      {form && <Fields fields={form.fields} state={state} set={set} />}
      {sub && <Fields fields={sub.fields} state={state} set={set} />}
      {raised.length > 0 && (
        <label>
          Why raise {raised.map((f) => f.flags[f.flags.length - 1]).join(" and ")} above
          the default ({raised.map((f) => f.default).join(", ")})?
          <textarea required maxLength={500} rows={2} value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    placeholder="E.g. the change ticket and why this Run may change more than usual" />
        </label>
      )}
      {writes && (
        <p className="notice">This command writes. It will run as a <strong>dry run</strong>:
          you'll see what it would change, and nothing is changed.</p>
      )}
      {error && <p className="error" role="alert">{error}</p>}
      <button type="submit" disabled={busy || !form}>
        {busy ? "Running…" : writes ? "Dry run" : "Run"}
      </button>
    </form>
  );
}
