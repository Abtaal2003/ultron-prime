import type { components } from "./api.gen";

type Schemas = components["schemas"];

export interface ApiErrorBody {
  error: { kind: string; message: string };
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly kind: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const BASE = "/api/v1";
let onUnauthenticated: (() => void) | null = null;

export function setUnauthenticatedHandler(fn: (() => void) | null): void {
  onUnauthenticated = fn;
}

export function readCookie(name: string): string | undefined {
  const match = document.cookie.split("; ").find((c) => c.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.slice(name.length + 1)) : undefined;
}

interface RequestOptions {
  json?: unknown;
  form?: FormData;
  signal?: AbortSignal;
  query?: Record<string, string | number | boolean | undefined>;
}

export async function request<T>(
  method: "GET" | "POST" | "PATCH" | "DELETE",
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const url = new URL(BASE + path, window.location.origin);
  for (const [k, v] of Object.entries(opts.query ?? {})) {
    if (v !== undefined) url.searchParams.set(k, String(v));
  }
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") {
    const token = readCookie("ultron_csrf");
    if (token) headers["X-CSRF-Token"] = token;
  }
  let body: BodyInit | undefined;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.json);
  } else if (opts.form) {
    body = opts.form;
  }
  let res: Response;
  try {
    res = await fetch(url, { method, headers, body, credentials: "same-origin", signal: opts.signal });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw new ApiError(0, "network", "The house is unreachable.");
  }
  if (res.status === 401) {
    onUnauthenticated?.();
    throw new ApiError(401, "permission", "Sign in required.");
  }
  if (!res.ok) {
    let kind = "internal";
    let message = `Request failed (${res.status}).`;
    try {
      const data = (await res.json()) as Partial<ApiErrorBody>;
      if (data.error) ({ kind, message } = data.error);
    } catch {
      /* not JSON: keep the generic, themed message; never surface raw bodies */
    }
    throw new ApiError(res.status, kind, message);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---- response shapes (mirrors of the routes in src/api/routes; request bodies come from api.gen)
export interface SessionInfo {
  authenticated: boolean;
  csrf_token: string | null;
  remote: boolean;
  expires_at: string | null;
  access_mode: string;
}
export interface Chat {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  archived: boolean;
  private: boolean;
  message_count: number | null;
  active_turn: string | null;
}
export interface StoredMessage {
  id: string;
  chat_id: string;
  turn_id: string | null;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  created_at: string;
  tool_calls: Record<string, unknown>[];
  receipts: string[];
  attachments: string[];
  meta: Record<string, unknown>;
}
export interface MemoryItem {
  id: string;
  text: string;
  category: string;
  importance: number;
  created_at: string;
  updated_at: string;
  edited_by_user: boolean;
  source_chat_id: string | null;
}
export interface PendingProposal {
  id: string;
  text: string;
  category: string;
  rationale: string;
  evidence: Record<string, unknown>[];
  chat_id: string | null;
  created_at: string;
}
export interface WorkshopFile {
  file_id: string;
  logical_name: string;
  kind: string;
  latest_version: number;
  mime: string;
  size: number;
  sha256: string;
  external_content: boolean;
  version_count: number;
  updated_at: string;
}
export interface FileVersion {
  file_id: string;
  logical_name: string;
  kind: string;
  version: number;
  sha256: string;
  mime: string;
  size: number;
  created_by: string;
  derived_from: string[];
  sources: string[];
  external_content: boolean;
  change_note: string | null;
  created_at: string;
  chat_id: string | null;
}
export interface AttachmentMeta {
  id: string;
  name: string;
  mime: string;
  size: number;
  trust: "trusted" | "external";
  status: string;
  secret_warning: boolean;
}
export interface UploadedAttachment {
  id: string;
  name: string;
  family: string;
  warnings: string[];
  quarantine: boolean;
}
export interface SettingsView {
  values: Record<string, unknown>;
  mutable: string[];
  keys_present: Record<string, boolean>;
  routes: Record<
    string,
    { route: string; available: boolean; capabilities: Record<string, unknown> | null }
  >;
  routing_problems: string[];
  tools: string[];
  disabled_tools: string[];
  muted_categories: string[];
  charter_sha256: string;
  canon_through: string;
  storage: { attachments_bytes: number; versions_bytes: number };
}
export interface StatusView {
  provider: string;
  model: string;
  main_available: boolean;
  vault_count: number;
  guard: { route: string; available: boolean; status: string };
  remote: boolean;
  access_mode: string;
  strict: boolean;
  host: Record<string, Record<string, number | string> | number> | null;
  budget?: { daily_usd: number; spent_today_usd: number };
}
export interface SafetyView {
  decisions: Record<string, string | number | null>[];
  class_counts: Record<string, number>;
  egress: Record<string, string | null>[];
  drift: { chat_id: string | null; score: number; created_at: string }[];
  canary_events: { event: string; created_at: string; payload: Record<string, unknown> }[];
}

export type ChatPatch = Partial<Pick<Chat, "title" | "archived" | "private">>;
export type ProposalDecisionBody = {
  decision: "approve" | "edit_approve" | "decline";
  edited_text?: string;
  mute_category?: boolean;
};
export type LoginBody = Schemas["LoginBody"];
export type PassphraseBody = Schemas["PassphraseBody"];

export const api = {
  session: () => request<SessionInfo>("GET", "/auth/session"),
  login: (passphrase: string) =>
    request<SessionInfo>("POST", "/auth/login", { json: { passphrase } satisfies LoginBody }),
  logout: () => request<{ ok: boolean }>("POST", "/auth/logout"),
  logoutAll: () => request<{ revoked: number }>("POST", "/auth/logout-all"),
  changePassphrase: (body: PassphraseBody) => request<{ ok: boolean }>("POST", "/auth/passphrase", { json: body }),
  sessions: () => request<Record<string, string | number>[]>("GET", "/auth/sessions"),

  chats: (archived = false) => request<Chat[]>("GET", "/chats", { query: { archived } }),
  createChat: (title = "New chat", priv = false) =>
    request<Chat>("POST", "/chats", { json: { title, private: priv } }),
  getChat: (id: string) => request<Chat>("GET", `/chats/${id}`),
  patchChat: (id: string, body: ChatPatch) => request<Chat>("PATCH", `/chats/${id}`, { json: body }),
  deleteChat: (id: string) => request<void>("DELETE", `/chats/${id}`),
  purgeArchived: () => request<{ deleted: number }>("POST", "/chats/purge-archived"),
  messages: (id: string, before?: string, limit = 50) =>
    request<{ messages: StoredMessage[]; has_more: boolean }>("GET", `/chats/${id}/messages`, {
      query: { before, limit },
    }),
  exportUrl: (id: string) => `${BASE}/chats/${id}/export`,

  startTurn: (chatId: string, text: string, files: File[]) => {
    const form = new FormData();
    form.set("text", text);
    for (const f of files) form.append("files", f, f.name);
    return request<{ turn_id: string; attachments: UploadedAttachment[] }>("POST", `/chats/${chatId}/turns`, {
      form,
    });
  },
  stopTurn: (id: string) => request<{ stopped: boolean }>("POST", `/turns/${id}/stop`),
  egressDecision: (callId: string, decision: "approve" | "reject") =>
    request<{ ok: boolean }>("POST", `/egress/${callId}/decision`, { json: { decision } }),
  quarantineOriginal: (turnId: string, handle: string) =>
    request<{ handle: string; text: string }>("GET", `/turns/${turnId}/quarantine/${encodeURIComponent(handle)}`),

  memories: (q?: string, category?: string) =>
    request<{ items: MemoryItem[]; total: number }>("GET", "/memory", { query: { q, category, limit: 200 } }),
  proposals: () => request<{ pending: PendingProposal[]; muted: string[] }>("GET", "/memory/proposals"),
  decideProposal: (id: string, body: ProposalDecisionBody) =>
    request<{ status: string }>("POST", `/memory/proposals/${id}/decision`, { json: body }),
  patchMemory: (id: string, body: Partial<Pick<MemoryItem, "text" | "category" | "importance">>) =>
    request<MemoryItem>("PATCH", `/memory/${id}`, { json: body }),
  deleteMemory: (id: string) => request<void>("DELETE", `/memory/${id}`),
  unmute: (category: string) => request<{ ok: boolean }>("POST", "/memory/unmute", { query: { category } }),
  memoryExportUrl: () => `${BASE}/memory-export`,

  files: (chat?: string) => request<WorkshopFile[]>("GET", "/files", { query: { chat } }),
  versions: (fileId: string) => request<FileVersion[]>("GET", `/files/${fileId}/versions`),
  downloadUrl: (fileId: string, v: number) => `${BASE}/files/${fileId}/versions/${v}/download`,
  previewUrl: (fileId: string, v: number) => `${BASE}/files/${fileId}/versions/${v}/preview`,
  attachment: (id: string) => request<AttachmentMeta>("GET", `/attachments/${id}`),
  attachmentUrl: (id: string) => `${BASE}/attachments/${id}/content`,
  thumbnailUrl: (id: string) => `${BASE}/attachments/${id}/thumbnail`,
  setTrust: (id: string, trust: "trusted" | "external") =>
    request<{ id: string; trust: string }>("PATCH", `/attachments/${id}`, { json: { trust } }),

  settings: () => request<SettingsView>("GET", "/settings"),
  patchSettings: (body: Record<string, unknown>) => request<SettingsView>("PATCH", "/settings", { json: body }),
  status: () => request<StatusView>("GET", "/status"),
  safety: () => request<SafetyView>("GET", "/safety"),
  redteam: () =>
    request<{ run: null | { id: string; started_at: string; finished_at: string | null; summary: Record<string, unknown> } }>(
      "GET",
      "/redteam/latest",
    ),
};
