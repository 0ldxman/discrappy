// Боковая панель персонажей: сортировка, поиск, фильтр по нескольким именам
// и операции над именем (переименовать, слить написания, удалить реплики).

import { useMemo, useState } from "react";
import type { AuthorCount } from "../types";

type SortKey = "name" | "count" | "first";

const SORTS: { key: SortKey; label: string; title: string }[] = [
  { key: "name", label: "А-Я", title: "По алфавиту" },
  { key: "count", label: "№", title: "По количеству реплик" },
  { key: "first", label: "⏱", title: "По первому появлению" },
];

export default function CharactersPanel({
  authors, active, onToggleActive, onClear, onRename, onMerge, onDeleteMessages,
}: {
  authors: AuthorCount[];
  active: Set<string>;
  onToggleActive: (name: string, additive: boolean) => void;
  onClear: () => void;
  onRename: (name: string) => void;
  onMerge: (sources: string[]) => void;
  onDeleteMessages: (names: string[]) => void;
}) {
  const [sort, setSort] = useState<SortKey>("count");
  const [asc, setAsc] = useState(false);
  const [search, setSearch] = useState("");
  const [checked, setChecked] = useState<Set<string>>(new Set());

  const shown = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const list = authors.filter((a) => !needle || a.author.toLowerCase().includes(needle));
    const dir = asc ? 1 : -1;
    return [...list].sort((a, b) => {
      if (sort === "name") return dir * a.author.localeCompare(b.author, "ru");
      if (sort === "first") return dir * (a.first_ts || "").localeCompare(b.first_ts || "");
      return dir * (a.count - b.count) || a.author.localeCompare(b.author, "ru");
    });
  }, [authors, search, sort, asc]);

  function pickSort(key: SortKey) {
    if (sort === key) setAsc((v) => !v);
    // По имени естественно от А к Я, по числу и времени — от большего/раннего.
    else { setSort(key); setAsc(key === "name"); }
  }

  function toggleCheck(name: string) {
    setChecked((s) => {
      const n = new Set(s);
      n.has(name) ? n.delete(name) : n.add(name);
      return n;
    });
  }

  const total = authors.reduce((n, a) => n + a.count, 0);

  return (
    <aside className="chars card">
      <div className="chars-head">
        <h2>Персонажи <span className="muted small">{authors.length}</span></h2>
        {active.size > 0 && (
          <button className="btn ghost sm" onClick={onClear} title="Показать всех">✕</button>
        )}
      </div>

      <input className="input sm" placeholder="поиск имени…" value={search}
             onChange={(e) => setSearch(e.target.value)} />

      <div className="chars-sort">
        <span className="muted small">Сортировка:</span>
        {SORTS.map((s) => (
          <button key={s.key} title={s.title}
                  className={`btn sm ${sort === s.key ? "primary" : "ghost"}`}
                  onClick={() => pickSort(s.key)}>
            {s.label}{sort === s.key ? (asc ? " ↑" : " ↓") : ""}
          </button>
        ))}
      </div>

      <div className="chars-list">
        {shown.map((a) => {
          const share = total ? Math.round((a.count / total) * 100) : 0;
          return (
            <div key={a.author}
                 className={`chars-row${active.has(a.author) ? " on" : ""}`}
                 onClick={(e) => onToggleActive(a.author, e.ctrlKey || e.metaKey || e.shiftKey)}
                 title={`${a.count} реплик · ${share}% лога${a.hidden ? ` · скрыто ${a.hidden}` : ""}`}>
              <input type="checkbox" checked={checked.has(a.author)}
                     onClick={(e) => e.stopPropagation()}
                     onChange={() => toggleCheck(a.author)} />
              <span className="chars-name">{a.author || <i className="faint">без имени</i>}</span>
              <span className="chars-count">{a.count}</span>
              <span className="chars-bar" style={{ width: `${share}%` }} />
            </div>
          );
        })}
        {shown.length === 0 && <div className="empty small">Никого не нашлось.</div>}
      </div>

      {checked.size > 0 ? (
        <div className="chars-actions">
          <span className="muted small">Отмечено {checked.size}:</span>
          {checked.size === 1 && (
            <button className="btn sm" onClick={() => { onRename([...checked][0]); setChecked(new Set()); }}>
              ✎ Переименовать
            </button>
          )}
          {checked.size > 1 && (
            <button className="btn sm" onClick={() => { onMerge([...checked]); setChecked(new Set()); }}>
              ⇉ Слить в одного
            </button>
          )}
          <button className="btn sm danger"
                  onClick={() => { onDeleteMessages([...checked]); setChecked(new Set()); }}>
            🗑 Удалить реплики
          </button>
          <button className="btn sm ghost" onClick={() => setChecked(new Set())}>снять</button>
        </div>
      ) : (
        <p className="muted small chars-tip">
          Клик — фильтр по персонажу, Ctrl+клик — добавить к фильтру.
          Галочками отмечай, чтобы слить разные написания одного имени.
        </p>
      )}
    </aside>
  );
}
