import { type FormEvent, useEffect, useState } from "react";
import { getCommands, startRun, type Command } from "./api";

// Phase 1 tracer bullet: pick a read-only command and give its arguments, one per line.
// Generated forms replace the free-text arguments in #67.
export function RunForm() {
  const [commands, setCommands] = useState<Command[]>([]);
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    getCommands()
      .then((all) => {
        const readOnly = all.filter((c) => c.read_only);
        setCommands(readOnly);
        if (readOnly.length) setCommand(readOnly[0].name);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const selected = commands.find((c) => c.name === command);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const argv = args.split("\n").map((a) => a.trim()).filter(Boolean);
      const run = await startRun(command, argv);
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
      {selected && <p className="hint">{selected.description}</p>}
      <label>
        Arguments, one per line
        <textarea rows={5} value={args} onChange={(e) => setArgs(e.target.value)}
                  placeholder={"-f\ncsv"} />
      </label>
      {error && <p className="error" role="alert">{error}</p>}
      <button type="submit" disabled={busy || !command}>{busy ? "Running…" : "Run"}</button>
    </form>
  );
}
