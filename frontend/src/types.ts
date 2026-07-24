// Типы данных, общие для API-клиента и компонентов.

export interface Guild {
  id: string;
  name: string;
  icon?: string | null;
}

export interface Channel {
  id: string;
  name: string;
  type?: number;
  kind?: string;
  parent?: string | null;
}

export interface RunSummary {
  id: string;
  created_at: string;
  guild_id: string;
  channels: { id: string; name?: string }[];
  params: Record<string, unknown>;
  status: "running" | "done" | "stopped" | "error";
  title: string;
  message_count: number;
}

// Роль реплики в повествовании. Пустая строка — не размечено.
export type Role = "" | "speech" | "action" | "narration" | "ooc";

export const ROLE_LABELS: Record<Exclude<Role, "">, string> = {
  speech: "речь",
  action: "действие",
  narration: "нарратив",
  ooc: "OOC",
};

export interface Message {
  id: number;
  run_id: string;
  chat_id: string;
  chat_name: string;
  ts: string; // UTC ISO
  author: string;
  author_id: string;
  content: string;
  kind: "embed" | "text";
  discord_msg_id: string;
  seq: number; // порядок повествования
  role: Role;
  hidden: boolean;
  scene_title: string; // непустой — сообщение открывает сцену
  note: string;
}

export interface MessagePage {
  total: number;
  items: Message[];
}

export interface AuthorCount {
  author: string;
  count: number;
  hidden: number;
  first_ts: string;
  last_ts: string;
  chars: number;
}

export interface ChatCount {
  chat_name: string;
  count: number;
}

export interface Scene {
  id: number;
  seq: number;
  title: string;
  ts: string;
  chat_name: string;
}

export interface EditEntry {
  id: number;
  created_at: string;
  label: string;
  undone: boolean;
}

export interface History {
  items: EditEntry[];
  undo_label: string | null;
  redo_label: string | null;
}

// Общий ответ массовых операций в режиме предпросмотра.
export interface DiffItem {
  id: number;
  author: string;
  before: string;
  after: string;
  hits: number;
}

export interface DiffPreview {
  changed: number;
  items: DiffItem[];
}

export interface SplitPreviewItem {
  id: number;
  author: string;
  before: string;
  parts: { content: string; author: string; role: Role }[];
}

export interface SplitPreview {
  changed: number;
  items: SplitPreviewItem[];
}

export type ExportFormat = "txt" | "obsidian" | "story" | "csv" | "json";

// Публичное представление конфига (секреты — как флаги *_set).
export interface PublicConfig {
  guild_id: string;
  nextcloud_url: string;
  nextcloud_user: string;
  nextcloud_dir: string;
  author_ids: string;
  character_names: string;
  name_blacklist: string;
  text_contains: string;
  text_masks: string;
  text_fuzzy: string;
  fuzzy_threshold: string;
  timezone: string;
  time_format: string;
  output_format: string;
  mode: string;
  text_name_patterns: string;
  text_fallback_nick: string;
  text_ignore_bots: string;
  text_command_prefixes: string;
  text_ooc_prefixes: string;
  discord_token_set: boolean;
  nextcloud_app_password_set: boolean;
  [key: string]: unknown;
}

// События SSE потока скрэппинга.
export type ScrapeEvent =
  | { type: "channel"; name: string; id: string; index: number; count: number }
  | { type: "line"; channel: string; ts: string; name: string; text: string }
  | { type: "progress"; seen: number; lines: number; percent: number | null; eta: number | null; channel_index: number; count: number }
  | { type: "status"; message: string }
  | { type: "done"; lines: number; run_id?: string; characters?: number; download?: string; stopped?: boolean; message?: string; link?: string; remote_path?: string }
  | { type: "error"; message: string };
