// Массовые операции над логом: замена, чистка, авто-разделение, разметка ролей.
// Все четыре сначала показывают предпросмотр «что изменится» и только потом
// применяются — и каждая ложится в журнал одним шагом отмены.

import { useState } from "react";
import { api, type ScopedPayload, type SplitMode } from "../api";
import Modal from "../ui/Modal";
import { ROLE_LABELS, type DiffPreview, type SplitPreview } from "../types";

type Tab = "replace" | "cleanup" | "split" | "roles";

const TABS: { key: Tab; label: string; icon: string }[] = [
  { key: "split", label: "Разделение", icon: "✂" },
  { key: "replace", label: "Замена", icon: "⇄" },
  { key: "cleanup", label: "Чистка", icon: "🧹" },
  { key: "roles", label: "Роли", icon: "🎭" },
];

const CLEANUP_OPS: { key: string; label: string }[] = [
  { key: "ooc", label: "убрать ((OOC)) в двойных скобках" },
  { key: "mentions", label: "убрать упоминания <@id>, @everyone" },
  { key: "emoji", label: "убрать кастомные эмодзи <:name:id>" },
  { key: "markdown", label: "убрать markdown-разметку (* _ ~ ` ||)" },
  { key: "blank", label: "схлопнуть пустые строки" },
  { key: "spaces", label: "схлопнуть повторяющиеся пробелы" },
  { key: "dashes", label: "дефис в начале реплики → тире" },
];

/** Строка предпросмотра в общем для всех операций виде. */
interface PreviewRow {
  id: number;
  author: string;
  before: string;
  after: string[];
}

export default function ToolsModal({ runId, scope, selectedCount, onDone, onClose }: {
  runId: string;
  /** Область действия: выделенные строки либо текущие фильтры таблицы. */
  scope: (useSelection: boolean) => ScopedPayload;
  selectedCount: number;
  onDone: (message: string) => void;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<Tab>("split");
  const [useSelection, setUseSelection] = useState(selectedCount > 0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<{ changed: number; rows: PreviewRow[] } | null>(null);

  // Настройки вкладок
  const [find, setFind] = useState("");
  const [replaceWith, setReplaceWith] = useState("");
  const [regex, setRegex] = useState(false);
  const [matchCase, setMatchCase] = useState(false);
  const [ops, setOps] = useState<Set<string>>(new Set(["ooc", "blank", "spaces"]));
  const [splitMode, setSplitMode] = useState<SplitMode>("smart");
  const [extractAuthor, setExtractAuthor] = useState(true);
  const [overwriteRoles, setOverwriteRoles] = useState(false);

  function payload(isPreview: boolean): ScopedPayload & Record<string, unknown> {
    return { ...scope(useSelection), preview: isPreview };
  }

  async function run(isPreview: boolean) {
    setBusy(true);
    setError("");
    try {
      if (tab === "replace") {
        const r = await api.replace(runId, {
          ...payload(isPreview), find, replace: replaceWith, regex, case: matchCase,
        });
        finish(isPreview, r, (d) => d.items.map(toRow));
      } else if (tab === "cleanup") {
        const r = await api.cleanup(runId, { ...payload(isPreview), ops: [...ops] });
        finish(isPreview, r, (d) => d.items.map(toRow));
      } else if (tab === "split") {
        const r = await api.autoSplit(runId, {
          ...payload(isPreview), mode: splitMode, extract_author: extractAuthor,
        });
        finish(isPreview, r as SplitPreview & { changed: number }, (d: SplitPreview) =>
          d.items.map((it) => ({
            id: it.id, author: it.author, before: it.before,
            after: it.parts.map((p) => `(${p.author})${p.role ? ` [${ROLE_LABELS[p.role as keyof typeof ROLE_LABELS] ?? p.role}]` : ""} ${p.content}`),
          })));
      } else {
        const r = await api.detectRoles(runId, {
          ...payload(isPreview), overwrite: overwriteRoles,
        });
        finish(isPreview, r, (d) => d.items.map((it) => ({
          id: it.id, author: it.author, before: it.before,
          after: [ROLE_LABELS[it.after as keyof typeof ROLE_LABELS] ?? it.after],
        })));
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function finish<T extends { changed: number }>(
    isPreview: boolean, result: T, toRows: (r: T) => PreviewRow[],
  ) {
    if (isPreview) {
      setPreview({ changed: result.changed, rows: toRows(result) });
    } else {
      onDone(`${TABS.find((t) => t.key === tab)!.label}: затронуто ${result.changed}`);
      onClose();
    }
  }

  const canRun = tab === "replace" ? find.length > 0
    : tab === "cleanup" ? ops.size > 0
    : true;

  return (
    <Modal wide title="Инструменты обработки" onClose={onClose} foot={
      <>
        {preview && <span className="muted small right" style={{ marginRight: "auto" }}>
          Изменится строк: <b>{preview.changed}</b>
        </span>}
        <button className="btn ghost" onClick={onClose}>Закрыть</button>
        <button className="btn" disabled={busy || !canRun} onClick={() => run(true)}>
          {busy ? <span className="spinner" /> : "👁"} Предпросмотр
        </button>
        <button className="btn primary" disabled={busy || !canRun || preview?.changed === 0}
                onClick={() => run(false)}>Применить</button>
      </>
    }>
      <div className="tabs">
        {TABS.map((t) => (
          <button key={t.key} className={`tab${tab === t.key ? " on" : ""}`}
                  onClick={() => { setTab(t.key); setPreview(null); setError(""); }}>
            {t.icon} {t.label}
          </button>
        ))}
      </div>

      <div className="scope-pick">
        <span className="muted small">Область:</span>
        <button className={`btn sm ${!useSelection ? "primary" : "ghost"}`}
                onClick={() => { setUseSelection(false); setPreview(null); }}>
          всё под текущими фильтрами
        </button>
        <button className={`btn sm ${useSelection ? "primary" : "ghost"}`}
                disabled={selectedCount === 0}
                onClick={() => { setUseSelection(true); setPreview(null); }}>
          выделенные ({selectedCount})
        </button>
      </div>

      {tab === "split" && (
        <div className="stack">
          <p className="muted small">
            Разносит склеенные посты по отдельным строкам лога — когда действие и
            реплика были отправлены одним сообщением Discord.
          </p>
          <div className="row wrap">
            {([["smart", "автоматически"], ["lines", "по строкам"],
               ["paragraphs", "по абзацам"]] as [SplitMode, string][]).map(([m, label]) => (
              <button key={m} className={`btn sm ${splitMode === m ? "primary" : "ghost"}`}
                      onClick={() => { setSplitMode(m); setPreview(null); }}>{label}</button>
            ))}
          </div>
          <label className="switch">
            <input type="checkbox" checked={extractAuthor}
                   onChange={(e) => { setExtractAuthor(e.target.checked); setPreview(null); }} />
            вытаскивать имя из «(Имя) - текст» и убирать префикс
          </label>
          <p className="hint">
            «Автоматически» режет по строкам, но склеивает обратно соседние строки
            с одинаковой ролью и одним говорящим — длинная реплика не рассыплется.
          </p>
        </div>
      )}

      {tab === "replace" && (
        <div className="stack">
          <div className="fields">
            <label className="field">Найти
              <input className="input" value={find} autoFocus
                     onChange={(e) => { setFind(e.target.value); setPreview(null); }} />
            </label>
            <label className="field">Заменить на
              <input className="input" value={replaceWith}
                     onChange={(e) => { setReplaceWith(e.target.value); setPreview(null); }} />
            </label>
          </div>
          <div className="row wrap">
            <label className="switch">
              <input type="checkbox" checked={regex}
                     onChange={(e) => { setRegex(e.target.checked); setPreview(null); }} />
              регулярное выражение
            </label>
            <label className="switch">
              <input type="checkbox" checked={matchCase}
                     onChange={(e) => { setMatchCase(e.target.checked); setPreview(null); }} />
              учитывать регистр
            </label>
          </div>
          {regex && <p className="hint">В замене доступны группы: \1, \2 и т.д.</p>}
        </div>
      )}

      {tab === "cleanup" && (
        <div className="toggles">
          {CLEANUP_OPS.map((op) => (
            <label key={op.key} className="switch">
              <input type="checkbox" checked={ops.has(op.key)} onChange={() => {
                setOps((s) => {
                  const n = new Set(s);
                  n.has(op.key) ? n.delete(op.key) : n.add(op.key);
                  return n;
                });
                setPreview(null);
              }} />
              {op.label}
            </label>
          ))}
        </div>
      )}

      {tab === "roles" && (
        <div className="stack">
          <p className="muted small">
            Размечает реплики: речь, действие, нарратив, OOC. Роли влияют на
            формат «Повествование» при экспорте и на подсветку в таблице.
          </p>
          <label className="switch">
            <input type="checkbox" checked={overwriteRoles}
                   onChange={(e) => { setOverwriteRoles(e.target.checked); setPreview(null); }} />
            перезаписывать уже проставленные роли
          </label>
          <p className="hint">
            По умолчанию выключено: у разрезанных реплик роль уже определена по
            префиксу «(Имя) -», а после его удаления эвристика её не угадает.
          </p>
        </div>
      )}

      {error && <p className="badge err" style={{ display: "block", padding: 8 }}>{error}</p>}

      {preview && (
        <div className="preview-box">
          <div className="row" style={{ marginBottom: 8 }}>
            <b>Предпросмотр</b>
            <span className="muted small">
              {preview.changed === 0 ? "изменений нет"
                : `показано ${preview.rows.length} из ${preview.changed}`}
            </span>
          </div>
          {preview.rows.map((row) => (
            <div key={row.id} className="diff">
              <div className="diff-author">{row.author}</div>
              <div className="diff-before">{row.before}</div>
              {row.after.map((line, i) => (
                <div key={i} className="diff-after">{line}</div>
              ))}
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}

function toRow(item: DiffPreview["items"][number]): PreviewRow {
  return { id: item.id, author: item.author, before: item.before, after: [item.after] };
}
