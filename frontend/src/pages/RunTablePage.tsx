import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useToast } from "../ui/toast";
import type { ExportFormat, Message } from "../types";
import UploadModal from "../components/UploadModal";

const PAGE = 100;

export default function RunTablePage() {
  const { runId = "" } = useParams();
  const toast = useToast();
  const qc = useQueryClient();

  // Фильтры / сортировка / страница
  const [author, setAuthor] = useState("");
  const [q, setQ] = useState("");
  const [after, setAfter] = useState("");
  const [before, setBefore] = useState("");
  const [sort, setSort] = useState("ts");
  const [order, setOrder] = useState<"asc" | "desc">("asc");
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [uploadOpen, setUploadOpen] = useState(false);

  const runQ = useQuery({ queryKey: ["run", runId], queryFn: () => api.getRun(runId) });
  const authorsQ = useQuery({ queryKey: ["authors", runId], queryFn: () => api.getAuthors(runId) });

  const query = { author, q, after, before, sort, order, limit: PAGE, offset: page * PAGE };
  const msgQ = useQuery({
    queryKey: ["messages", runId, query],
    queryFn: () => api.getMessages(runId, query),
    placeholderData: keepPreviousData,
  });

  const total = msgQ.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE));
  const items = msgQ.data?.items ?? [];

  function invalidate() {
    qc.invalidateQueries({ queryKey: ["messages", runId] });
    qc.invalidateQueries({ queryKey: ["authors", runId] });
    qc.invalidateQueries({ queryKey: ["run", runId] });
  }

  const update = useMutation({
    mutationFn: (v: { id: number; patch: { author?: string; content?: string } }) => api.updateMessage(v.id, v.patch),
    onSuccess: invalidate,
    onError: (e) => toast((e as Error).message, "err"),
  });
  const delOne = useMutation({
    mutationFn: (id: number) => api.deleteMessage(id),
    onSuccess: () => { invalidate(); toast("Удалено", "ok"); },
    onError: (e) => toast((e as Error).message, "err"),
  });
  const bulk = useMutation({
    mutationFn: (payload: { ids?: number[]; author?: string }) => api.bulkDelete(runId, payload),
    onSuccess: (r) => { setSelected(new Set()); invalidate(); toast(`Удалено сообщений: ${r.deleted}`, "ok"); },
    onError: (e) => toast((e as Error).message, "err"),
  });
  const rename = useMutation({
    mutationFn: (v: { from: string; to: string }) => api.renameAuthor(runId, v.from, v.to),
    onSuccess: (r) => { invalidate(); toast(`Переименовано сообщений: ${r.updated}`, "ok"); },
    onError: (e) => toast((e as Error).message, "err"),
  });

  function setSortCol(col: string) {
    if (sort === col) setOrder((o) => (o === "asc" ? "desc" : "asc"));
    else { setSort(col); setOrder("asc"); }
    setPage(0);
  }
  const arrow = (col: string) => (sort === col ? (order === "asc" ? " ▲" : " ▼") : "");

  function toggleRow(id: number) {
    setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  }
  const allShownSelected = items.length > 0 && items.every((m) => selected.has(m.id));
  function toggleAllShown() {
    setSelected((s) => {
      const n = new Set(s);
      if (allShownSelected) items.forEach((m) => n.delete(m.id));
      else items.forEach((m) => n.add(m.id));
      return n;
    });
  }

  function renameAuthorPrompt() {
    if (!author) { toast("Сначала выбери автора в фильтре", "info"); return; }
    const to = prompt(`Новое имя для «${author}»:`, author);
    if (to && to.trim() && to !== author) {
      rename.mutate({ from: author, to: to.trim() });
      setAuthor("");
    }
  }
  function deleteAuthor() {
    if (!author) { toast("Сначала выбери автора в фильтре", "info"); return; }
    if (confirm(`Удалить ВСЕ сообщения автора «${author}»?`)) {
      bulk.mutate({ author });
      setAuthor("");
    }
  }

  const runTitle = runQ.data?.title || runId.slice(0, 8);
  const params = runQ.data?.params as { output_format?: string } | undefined;
  const defaultFormat = (params?.output_format === "txt" ? "txt" : "obsidian") as ExportFormat;

  return (
    <div className="stack">
      <div className="row wrap">
        <Link to="/runs" className="btn ghost sm">← Прогоны</Link>
        <h1 style={{ margin: 0 }}>{runTitle}</h1>
        <span className="badge run">{total} сообщений</span>
        <div className="right row">
          <ExportButtons runId={runId} />
          <button className="btn primary sm" onClick={() => setUploadOpen(true)}>☁ В Nextcloud</button>
        </div>
      </div>

      {/* --- Фильтры --- */}
      <div className="card">
        <div className="filters">
          <label className="field">Автор
            <select className="input" value={author} onChange={(e) => { setAuthor(e.target.value); setPage(0); }}>
              <option value="">— все —</option>
              {authorsQ.data?.map((a) => <option key={a.author} value={a.author}>{a.author} ({a.count})</option>)}
            </select>
          </label>
          <label className="field">Поиск по тексту
            <input className="input" value={q} onChange={(e) => { setQ(e.target.value); setPage(0); }} placeholder="подстрока…" />
          </label>
          <label className="field">С <span className="hint">ISO</span>
            <input className="input" value={after} onChange={(e) => { setAfter(e.target.value); setPage(0); }} placeholder="2026-01-01" />
          </label>
          <label className="field">По <span className="hint">ISO</span>
            <input className="input" value={before} onChange={(e) => { setBefore(e.target.value); setPage(0); }} placeholder="2026-07-01" />
          </label>
          <button className="btn sm" onClick={() => { setAuthor(""); setQ(""); setAfter(""); setBefore(""); setPage(0); }}>Сброс</button>
        </div>
        {author && (
          <div className="toolbar" style={{ marginTop: 12 }}>
            <span className="muted small">Действия над автором «{author}»:</span>
            <button className="btn sm" onClick={renameAuthorPrompt}>✎ Переименовать</button>
            <button className="btn sm danger" onClick={deleteAuthor}>🗑 Удалить все сообщения</button>
          </div>
        )}
        {selected.size > 0 && (
          <div className="toolbar" style={{ marginTop: 10 }}>
            <span className="muted small">Выбрано {selected.size}:</span>
            <button className="btn sm danger" onClick={() => confirm(`Удалить выбранные (${selected.size})?`) && bulk.mutate({ ids: [...selected] })}>🗑 Удалить выбранные</button>
            <button className="btn sm ghost" onClick={() => setSelected(new Set())}>снять выделение</button>
          </div>
        )}
      </div>

      {/* --- Таблица --- */}
      <div className="table-wrap">
        <table className="log-table">
          <thead>
            <tr>
              <th style={{ width: 28 }}><input type="checkbox" checked={allShownSelected} onChange={toggleAllShown} /></th>
              <th onClick={() => setSortCol("chat")}>Чат{arrow("chat")}</th>
              <th onClick={() => setSortCol("ts")}>Дата-время{arrow("ts")}</th>
              <th onClick={() => setSortCol("author")}>Автор{arrow("author")}</th>
              <th>Сообщение</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((m) => (
              <Row key={m.id} m={m} selected={selected.has(m.id)} onToggle={() => toggleRow(m.id)}
                   onSave={(patch) => update.mutate({ id: m.id, patch })}
                   onDelete={() => confirm("Удалить это сообщение?") && delOne.mutate(m.id)} />
            ))}
            {items.length === 0 && !msgQ.isLoading && (
              <tr><td colSpan={6} className="empty">Ничего не найдено под текущими фильтрами.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {pages > 1 && (
        <div className="pager">
          <button className="btn sm" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>← Назад</button>
          <span className="muted small">Стр. {page + 1} из {pages}</span>
          <button className="btn sm" disabled={page + 1 >= pages} onClick={() => setPage((p) => p + 1)}>Вперёд →</button>
        </div>
      )}

      {uploadOpen && <UploadModal runId={runId} defaultFormat={defaultFormat} onClose={() => setUploadOpen(false)} />}
    </div>
  );
}

// ---- Строка с инлайн-редактированием автора и текста ----
function Row({ m, selected, onToggle, onSave, onDelete }: {
  m: Message; selected: boolean; onToggle: () => void;
  onSave: (patch: { author?: string; content?: string }) => void; onDelete: () => void;
}) {
  const [editAuthor, setEditAuthor] = useState<string | null>(null);
  const [editContent, setEditContent] = useState<string | null>(null);
  const dt = useMemo(() => new Date(m.ts).toLocaleString(), [m.ts]);

  return (
    <tr className={selected ? "row-selected" : ""}>
      <td><input type="checkbox" checked={selected} onChange={onToggle} /></td>
      <td className="chat">{m.chat_name}</td>
      <td className="ts">{dt}</td>
      <td className="author" onDoubleClick={() => setEditAuthor(m.author)}>
        {editAuthor === null ? m.author : (
          <input className="cell-edit" autoFocus value={editAuthor}
            onChange={(e) => setEditAuthor(e.target.value)}
            onBlur={() => { if (editAuthor !== m.author) onSave({ author: editAuthor }); setEditAuthor(null); }}
            onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); if (e.key === "Escape") setEditAuthor(null); }} />
        )}
      </td>
      <td className="msg" onDoubleClick={() => setEditContent(m.content)}>
        {editContent === null ? m.content : (
          <textarea className="cell-edit" autoFocus rows={2} value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
            onBlur={() => { if (editContent !== m.content) onSave({ content: editContent }); setEditContent(null); }}
            onKeyDown={(e) => { if (e.key === "Escape") setEditContent(null); }} />
        )}
      </td>
      <td className="actions">
        <button className="btn sm danger" onClick={onDelete} title="Удалить">🗑</button>
      </td>
    </tr>
  );
}

// ---- Кнопки экспорта (скачивание файла) ----
function ExportButtons({ runId }: { runId: string }) {
  const fmts: { f: ExportFormat; label: string }[] = [
    { f: "obsidian", label: ".md" }, { f: "txt", label: ".txt" },
    { f: "csv", label: ".csv" }, { f: "json", label: ".json" },
  ];
  return (
    <span className="row" title="Скачать текущее состояние (после правок)">
      <span className="muted small">Экспорт:</span>
      {fmts.map(({ f, label }) => (
        <a key={f} className="btn sm" href={api.exportUrl(runId, f)}>{label}</a>
      ))}
    </span>
  );
}
