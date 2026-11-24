// The service's HTTP API. The UI only displays what it returns (ADR 0001): every rule about
// what may run lives in the engine and is enforced by the service.

export type Role = "viewer" | "operator" | "approver";

export interface Me {
  name: string;
  email: string;
  role: Role;
}

export interface Command {
  name: string;
  description: string;
  read_only: boolean;
  has_read_only_mode: boolean;
}

export type FieldKind = "flag" | "count" | "choice" | "number" | "text" | "input-file"
  | "input-dir" | "output-file" | "output-dir";

export interface Field {
  dest: string;
  flags: string[];
  positional: boolean;
  kind: FieldKind;
  required: boolean;
  default: string | number | boolean | string[] | null;
  choices: (string | number)[] | null;
  help: string;
  metavar: string | null;
  multiple: boolean;
  group: number | null;
  cap: boolean;  // a write cap (from the engine): raising it needs an override reason (#75)
}

export interface Subcommand {
  name: string;
  help: string;
  fields: Field[];
  writes: boolean;
}

export interface CommandForm {
  name: string;
  description: string;
  fields: Field[];
  subcommands: Subcommand[] | null;
  writes: boolean | null;
}

export interface RunFile {
  id: number;
  role: "input" | "output";
  path: string;
  size: number;
  sha256: string;
}

// How a finished Run ended. "changed": an apply whose fresh dry run no longer matched the
// review, so nothing ran. The service's list is the source of truth (executor.FINAL_OUTCOMES).
export const OUTCOMES = ["succeeded", "failed", "usage-error", "auth-error", "stopped",
  "changed"] as const;
export type Outcome = typeof OUTCOMES[number];

export const RUN_KINDS = [["report", "Report"], ["dry-run", "Dry run"],
  ["apply", "Apply"]] as const;
export type RunKind = typeof RUN_KINDS[number][0];

// Runs execute in the background: applies wait "queued" for the one writing slot.
export type Status = "queued" | "running" | "finished";

export interface Run {
  id: string;
  command: string;
  argv: string[];
  requested_by: string;
  requester_name: string;
  requester_role: Role;
  status: Status;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  outcome: Outcome | null;  // null until the Run has finished
  cancelled_by: string | null;
  fresh_dry_run_id: string | null;
  stdout: string;
  stderr: string;
  message: string | null;
  files: RunFile[];
  kind: RunKind;
  reviewed_by: string | null;
  reviewed_at: string | null;
  applied_run_id: string | null;
  applied_from: string | null;
  input_hash: string;
  output_hash: string;
  review: Review | null;
  cap_override: CapOverride | null;
  approval: Approval | null;
}

// A write cap raised above its default, with the reason recorded for it (#75).
export interface CapOverride {
  reason: string;
  by: string;
  caps: { flag: string; default: number; value: number }[];
}

// An Approval of a dry run of an Irreversible operation (#77). It expires 24 hours after it
// is asked for (if nobody answers) or given; a rejection is final for that dry run.
export interface Approval {
  status: "awaiting-approval" | "approved" | "rejected" | "expired";
  reasons: string[];  // why the engine counts the apply as irreversible
  requested_at: string;
  expires_at: string;
  decided_by: string | null;
  decided_at: string | null;
  comment: string | null;
}

export type LineKind = "add" | "change" | "delete" | "other" | "detail" | "info" | "context";

export interface Review {
  counts: { add: number; change: number; delete: number; other: number };
  lines: { stream: "stdout" | "stderr"; kind: LineKind; text: string }[];
  complete: boolean;  // false when the dry run didn't succeed
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public detail: unknown = null) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const isForm = init?.body instanceof FormData;  // the browser sets the multipart boundary
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    // X-PD-Ops-Request marks the request as the UI's own; the service refuses state-changing
    // requests without it, so other sites can't forge them.
    headers: isForm
      ? { "X-PD-Ops-Request": "1", ...(init?.headers ?? {}) }
      : { "Content-Type": "application/json", "X-PD-Ops-Request": "1", ...(init?.headers ?? {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail
      : typeof body.detail?.message === "string" ? body.detail.message : response.statusText;
    throw new ApiError(response.status, detail, body.detail ?? null);
  }
  return body as T;
}

export const getMe = () => call<Me>("/api/me");
export const getCommands = () => call<Command[]>("/api/commands");
export const getRun = (id: string) => call<Run>(`/api/runs/${encodeURIComponent(id)}`);
export const getForm = (name: string) =>
  call<CommandForm>(`/api/commands/${encodeURIComponent(name)}/form`);

export type Values = Record<string, string | number | boolean | string[]>;

// Form values go to the service, which builds the command line from the form (ADR 0004).
export function startRun(command: string, subcommand: string | null, values: Values,
                         files: Record<string, File[]>,
                         capOverrideReason?: string): Promise<Run> {
  const body = { command, subcommand, values,
    ...(capOverrideReason ? { cap_override_reason: capOverrideReason } : {}) };
  const uploads = Object.entries(files).filter(([, list]) => list.length > 0);
  if (uploads.length === 0) {
    return call<Run>("/api/runs", { method: "POST", body: JSON.stringify(body) });
  }
  const form = new FormData();
  form.append("run", JSON.stringify(body));
  for (const [dest, list] of uploads) for (const file of list) form.append(`file.${dest}`, file);
  return call<Run>("/api/runs", { method: "POST", body: form });
}

export const reviewRun = (id: string) =>
  call<Run>(`/api/runs/${encodeURIComponent(id)}/review`, { method: "POST" });
export const applyRun = (id: string) =>
  call<Run>(`/api/runs/${encodeURIComponent(id)}/apply`, { method: "POST" });

export const cancelRun = (id: string) =>
  call<Run>(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
// After a Stopped apply: dry-run the same command and inputs again to show what remains.
export const freshDryRun = (id: string) =>
  call<Run>(`/api/runs/${encodeURIComponent(id)}/fresh-dry-run`, { method: "POST" });

export interface Queue {
  executing: RunSummary | null;
  pending: RunSummary[];
}
export const getQueue = () => call<Queue>("/api/queue");

// A row of the Run history (#69): what ran and how it ended, without output or log.
export interface RunSummary {
  id: string;
  command: string;
  argv: string[];
  kind: Run["kind"];
  status: Status;
  outcome: Outcome | null;
  requested_by: string;
  requester_name: string;
  requester_role: Role;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  applied_from: string | null;
  cap_override: CapOverride | null;
  approval: Approval | null;
}

export interface HistoryPage {
  runs: RunSummary[];
  total: number;
  page: number;
  per_page: number;
}

export interface HistoryFilters {
  command?: string;
  requester?: string;
  since?: string;   // ISO 8601 with a timezone, inclusive
  before?: string;  // exclusive
  outcome?: string;
  kind?: string;
  page?: number;
  per_page?: number;
}

export function getHistory(filters: HistoryFilters): Promise<HistoryPage> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "") query.set(key, String(value));
  }
  return call<HistoryPage>(`/api/runs?${query}`);
}

export const askForApproval = (id: string) =>
  call<Run>(`/api/runs/${encodeURIComponent(id)}/approval`, { method: "POST" });
export const decideApproval = (id: string, verdict: "approve" | "reject", comment: string) =>
  call<Run>(`/api/runs/${encodeURIComponent(id)}/approval/${verdict}`,
            { method: "POST", body: JSON.stringify({ comment }) });
export const getApprovals = () => call<{ pending: RunSummary[] }>("/api/approvals");

export const fileUrl = (runId: string, fileId: number) =>
  `/api/runs/${encodeURIComponent(runId)}/files/${fileId}`;
