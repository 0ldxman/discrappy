// Панель предпросмотра: тот же документ, что уйдёт в экспорт, рядом с таблицей.
// Обновляется вместе с правками — видно, во что складывается лог.

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "../api";
import type { ExportFormat } from "../types";

const FORMATS: { f: ExportFormat; label: string }[] = [
  { f: "story", label: "Повествование" },
  { f: "obsidian", label: "Obsidian" },
  { f: "txt", label: "Лог" },
];

export default function StoryPane({ runId, format, onFormat, onClose }: {
  runId: string;
  format: ExportFormat;
  onFormat: (f: ExportFormat) => void;
  onClose: () => void;
}) {
  const docQ = useQuery({
    queryKey: ["document", runId, format],
    queryFn: () => api.getDocument(runId, format),
  });

  const blocks = useMemo(() => parse(docQ.data?.text ?? ""), [docQ.data?.text]);

  return (
    <aside className="story card">
      <div className="chars-head">
        <h2>Предпросмотр</h2>
        <button className="btn ghost sm" onClick={onClose} title="Скрыть панель">✕</button>
      </div>
      <div className="row wrap" style={{ marginBottom: 8 }}>
        {FORMATS.map(({ f, label }) => (
          <button key={f} className={`btn sm ${format === f ? "primary" : "ghost"}`}
                  onClick={() => onFormat(f)}>{label}</button>
        ))}
      </div>
      <div className="story-body">
        {docQ.isLoading && <div className="empty"><span className="spinner" /></div>}
        {docQ.isError && <div className="empty">{(docQ.error as Error).message}</div>}
        {blocks.map((b, i) => {
          if (b.kind === "h1") return <h3 key={i} className="story-h1">{b.text}</h3>;
          if (b.kind === "h2") return <h4 key={i} className="story-h2">{b.text}</h4>;
          if (b.kind === "h3") return <h4 key={i} className="story-h3">{b.text}</h4>;
          if (b.kind === "action") return <p key={i} className="story-action">{b.text}</p>;
          return (
            <p key={i} className="story-p">
              {b.speaker && <b className="story-speaker">{b.speaker} </b>}{b.text}
            </p>
          );
        })}
        {!docQ.isLoading && blocks.length === 0 && (
          <div className="empty small">Пусто — нечего показывать.</div>
        )}
      </div>
    </aside>
  );
}

interface Block { kind: "h1" | "h2" | "h3" | "action" | "p"; text: string; speaker?: string }

/**
 * Разметка, которую генерируют экспортеры, — узкая и известная заранее:
 * заголовки #/##/###, действия целиком в *…*, реплики с «**Имя.** ».
 * Полноценный markdown-парсер здесь не нужен.
 */
function parse(text: string): Block[] {
  return text
    .split(/\n{2,}/)
    .map((chunk) => chunk.trim())
    .filter(Boolean)
    .flatMap((chunk): Block[] => {
      if (chunk.startsWith("---")) return []; // YAML-frontmatter Obsidian
      if (chunk.startsWith("### ")) return [{ kind: "h3", text: chunk.slice(4) }];
      if (chunk.startsWith("## ")) return [{ kind: "h2", text: chunk.slice(3) }];
      if (chunk.startsWith("# ")) return [{ kind: "h1", text: chunk.slice(2) }];
      const action = chunk.match(/^\*(?!\*)(.+)\*$/s);
      if (action) return [{ kind: "action", text: action[1] }];
      const speech = chunk.match(/^\*\*(.+?)\*\*\s*(.*)$/s);
      if (speech) return [{ kind: "p", speaker: speech[1], text: speech[2] }];
      return [{ kind: "p", text: chunk }];
    });
}
