// Тонкий клиент REST API discrapp. Все ответы — JSON, ошибки → Error(detail).

import type {
  AuthorCount,
  Channel,
  ChatCount,
  DiffPreview,
  ExportFormat,
  Guild,
  History,
  Message,
  MessagePage,
  PublicConfig,
  Role,
  RunSummary,
  Scene,
  SplitPreview,
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
      if (v === undefined || v === null || v === "") return;
      // Имена персонажей разделяем переводом строки: запятая может быть в имени.
      qs.set(k, Array.isArray(v) ? v.join("\n") : String(v));
    });
    return req<MessagePage>(`/api/runs/${runId}/messages?${qs.toString()}`);
  },
  getAuthors: (runId: string) => req<AuthorCount[]>(`/api/runs/${runId}/authors`),
  getChats: (runId: string) => req<ChatCount[]>(`/api/runs/${runId}/chats`),
  getScenes: (runId: string) => req<Scene[]>(`/api/runs/${runId}/scenes`),

  updateMessage: (msgId: number, patch: MessagePatch) =>
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
  mergeAuthors: (runId: string, sources: string[], target: string) =>
    req<{ updated: number }>(`/api/runs/${runId}/merge-authors`, {
      method: "POST",
      body: JSON.stringify({ sources, target }),
    }),

  // --- Разрезание, вставка, порядок ---
  splitAtSelection: (msgId: number, start: number, end: number, extractAuthor = true) =>
    req<{ items: Message[] }>(`/api/messages/${msgId}/split`, {
      method: "POST",
      body: JSON.stringify({ start, end, extract_author: extractAuthor }),
    }),
  splitAuto: (msgId: number, mode: SplitMode, extractAuthor = true) =>
    req<{ items: Message[] }>(`/api/messages/${msgId}/split-auto`, {
      method: "POST",
      body: JSON.stringify({ mode, extract_author: extractAuthor }),
    }),
  insertMessage: (runId: string, payload: {
    after_id?: number; before_id?: number; author?: string; content?: string; role?: Role;
  }) =>
    req<Message>(`/api/runs/${runId}/messages`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  mergeMessages: (runId: string, ids: number[], separator = "\n") =>
    req<Message>(`/api/runs/${runId}/messages/merge`, {
      method: "POST",
      body: JSON.stringify({ ids, separator }),
    }),
  moveMessage: (msgId: number, payload: {
    direction?: "up" | "down"; after_id?: number; before_id?: number;
  }) =>
    req<{ moved: boolean }>(`/api/messages/${msgId}/move`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  duplicateMessage: (msgId: number) =>
    req<Message>(`/api/messages/${msgId}/duplicate`, { method: "POST" }),
  bulkSet: (runId: string, ids: number[], fields: { role?: Role; hidden?: boolean; author?: string }) =>
    req<{ changed: number }>(`/api/runs/${runId}/messages/set`, {
      method: "POST",
      body: JSON.stringify({ ids, ...fields }),
    }),

  // --- Массовые операции над текстом (preview:true — только показать) ---
  replace: (runId: string, payload: ScopedPayload & {
    find: string; replace: string; regex?: boolean; case?: boolean; preview?: boolean;
  }) =>
    req<DiffPreview & { changed: number }>(`/api/runs/${runId}/replace`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  cleanup: (runId: string, payload: ScopedPayload & { ops: string[]; preview?: boolean }) =>
    req<DiffPreview & { changed: number }>(`/api/runs/${runId}/cleanup`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  autoSplit: (runId: string, payload: ScopedPayload & {
    mode: SplitMode; extract_author?: boolean; preview?: boolean;
  }) =>
    req<SplitPreview & { changed: number }>(`/api/runs/${runId}/auto-split`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  detectRoles: (runId: string, payload: ScopedPayload & {
    overwrite?: boolean; preview?: boolean;
  }) =>
    req<DiffPreview & { changed: number }>(`/api/runs/${runId}/detect-roles`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  // --- Отмена правок ---
  undo: (runId: string) =>
    req<{ label: string }>(`/api/runs/${runId}/undo`, { method: "POST" }),
  redo: (runId: string) =>
    req<{ label: string }>(`/api/runs/${runId}/redo`, { method: "POST" }),
  getHistory: (runId: string) => req<History>(`/api/runs/${runId}/history`),

  getDocument: (runId: string, format: ExportFormat = "story") =>
    req<{ format: string; text: string }>(`/api/runs/${runId}/document?format=${format}`),
  exportUrl: (runId: string, format: ExportFormat) =>
    `/api/runs/${runId}/export?format=${format}`,
  upload: (runId: string, payload: Record<string, unknown>) =>
    req<{ remote_path: string; link: string }>(`/api/runs/${runId}/upload`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};

export interface MessageQuery {
  authors?: string[];
  after?: string;
  before?: string;
  q?: string;
  role?: string;
  hidden?: string; // "" — скрытые не показывать, "all" — все, "only" — только скрытые
  chat?: string;
  sort?: string;
  order?: string;
  limit?: number;
  offset?: number;
}

export interface MessagePatch {
  author?: string;
  content?: string;
  role?: Role;
  hidden?: boolean;
  scene_title?: string;
  note?: string;
}

export type SplitMode = "smart" | "lines" | "paragraphs";

/** Область действия массовой операции: явные строки либо текущие фильтры. */
export interface ScopedPayload {
  ids?: number[];
  filters?: {
    authors?: string[];
    q?: string;
    role?: string;
    chat?: string;
    after?: string;
    before?: string;
    hidden?: string;
  };
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
