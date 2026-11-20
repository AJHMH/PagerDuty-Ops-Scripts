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

export type Outcome = "succeeded" | "failed" | "usage-error" | "auth-error" | "stopped";

export interface Run {
  id: string;
  command: string;
  argv: string[];
  requested_by: string;
  requester_name: string;
  requester_role: Role;
  started_at: string;
  finished_at: string | null;
  exit_code: number | null;
  outcome: Outcome;
  stdout: string;
  stderr: string;
  message: string | null;
  files: RunFile[];
  kind: "report" | "dry-run";
  input_hash: string;
  output_hash: string;
  review: Review | null;
}

export type LineKind = "add" | "change" | "delete" | "other" | "detail" | "info" | "context";

export interface Review {
  counts: { add: number; change: number; delete: number; other: number };
  lines: { stream: "stdout" | "stderr"; kind: LineKind; text: string }[];
  complete: boolean;  // false when the dry run didn't succeed
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const isForm = init?.body instanceof FormData;  // the browser sets the multipart boundary
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: isForm ? init?.headers : { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : response.statusText;
    throw new ApiError(response.status, detail);
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
                         files: Record<string, File[]>): Promise<Run> {
  const body = { command, subcommand, values };
  const uploads = Object.entries(files).filter(([, list]) => list.length > 0);
  if (uploads.length === 0) {
    return call<Run>("/api/runs", { method: "POST", body: JSON.stringify(body) });
  }
  const form = new FormData();
  form.append("run", JSON.stringify(body));
  for (const [dest, list] of uploads) for (const file of list) form.append(`file.${dest}`, file);
  return call<Run>("/api/runs", { method: "POST", body: form });
}

export const fileUrl = (runId: string, fileId: number) =>
  `/api/runs/${encodeURIComponent(runId)}/files/${fileId}`;
