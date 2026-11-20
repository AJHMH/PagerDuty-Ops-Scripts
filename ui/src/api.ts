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
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
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
export const startRun = (command: string, argv: string[]) =>
  call<Run>("/api/runs", { method: "POST", body: JSON.stringify({ command, argv }) });
