// Тонкий клиент REST API discrapp. Все ответы — JSON, ошибки → Error(detail).

import type {
  AuthorCount,
  Channel,
  ExportFormat,
  Guild,
  MessagePage,
  PublicConfig,
  RunSummary,
} from "./types";

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* тело не JSON — оставляем statusText */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const api = {
  // --- Настройки ---
  getConfig: () => req<PublicConfig>("/api/config"),
  saveConfig: (patch: Record<string, unknown>) =>
    req<PublicConfig>("/api/config", { method: "POST", body: JSON.stringify(patch) }),

  // --- Discord ---
  getGuilds: () => req<Guild[]>("/api/guilds"),
  getChannels: (guildId?: string) =>
    req<Channel[]>(`/api/channels${guildId ? `?guild_id=${encodeURIComponent(guildId)}` : ""}`),

  // --- Nextcloud ---
  getFolders: (path = "") =>
    req<{ path: string; folders: string[] }>(`/api/folders?path=${encodeURIComponent(path)}`),

  // --- Скрэппинг ---
  startScrape: (params: Record<string, unknown>) =>
    req<{ job_id: string }>("/api/scrape", { method: "POST", body: JSON.stringify(params) }),
  stopScrape: (jobId: string) =>
    req<{ stopping: boolean }>(`/api/scrape/${jobId}/stop`, { method: "POST" }),
  preview: (payload: Record<string, unknown>) =>
    req<PreviewResult>("/api/preview", { method: "POST", body: JSON.stringify(payload) }),

  // --- Прогоны и таблица-лог ---
  getRuns: () => req<RunSummary[]>("/api/runs"),
  getRun: (runId: string) => req<RunSummary>(`/api/runs/${runId}`),
  deleteRun: (runId: string) => req<{ deleted: boolean }>(`/api/runs/${runId}`, { method: "DELETE" }),

  getMessages: (runId: string, params: MessageQuery) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    });
    return req<MessagePage>(`/api/runs/${runId}/messages?${qs.toString()}`);
  },
  getAuthors: (runId: string) => req<AuthorCount[]>(`/api/runs/${runId}/authors`),

  updateMessage: (msgId: number, patch: { author?: string; content?: string }) =>
    req<{ updated: boolean }>(`/api/messages/${msgId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  deleteMessage: (msgId: number) =>
    req<{ deleted: boolean }>(`/api/messages/${msgId}`, { method: "DELETE" }),
  bulkDelete: (runId: string, payload: { ids?: number[]; author?: string }) =>
    req<{ deleted: number }>(`/api/runs/${runId}/messages/delete`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  renameAuthor: (runId: string, from: string, to: string) =>
    req<{ updated: number }>(`/api/runs/${runId}/rename-author`, {
      method: "POST",
      body: JSON.stringify({ from, to }),
    }),

  exportUrl: (runId: string, format: ExportFormat) =>
    `/api/runs/${runId}/export?format=${format}`,
  upload: (runId: string, payload: Record<string, unknown>) =>
    req<{ remote_path: string; link: string }>(`/api/runs/${runId}/upload`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};

export interface MessageQuery {
  author?: string;
  after?: string;
  before?: string;
  q?: string;
  sort?: string;
  order?: string;
  limit?: number;
  offset?: number;
}

export interface PreviewItem {
  kind: string;
  ts: string;
  name: string;
  text: string;
  kept: boolean;
  reason: string;
  color?: number | null;
  has_thumbnail?: boolean;
  has_fields?: boolean;
}

export interface PreviewResult {
  total: number;
  kept: number;
  dropped: Record<string, number>;
  items: PreviewItem[];
}
