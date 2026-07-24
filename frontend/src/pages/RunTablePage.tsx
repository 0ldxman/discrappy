// Таблица-лог прогона: правка, разрезание и сборка повествования из сырых
// сообщений Discord. Порядок строк задаёт seq (см. db.py), поэтому вставленное
// вручную сообщение и половинки разрезанного стоят там, где их ожидаешь.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ScopedPayload } from "../api";
import { selectionOffsets, type Offsets } from "../lib/selection";
import { useToast } from "../ui/toast";
import ContextMenu, { type MenuItem, type MenuPosition } from "../ui/ContextMenu";
import { ConfirmModal, PromptModal } from "../ui/Modal";
import CharactersPanel from "../components/CharactersPanel";
import StoryPane from "../components/StoryPane";
import ToolsModal from "../components/ToolsModal";
import UploadModal from "../components/UploadModal";
import { ROLE_LABELS, type ExportFormat, type Message, type Role } from "../types";

const PAGE = 200;

const ROLE_ICONS: Record<Exclude<Role, "">, string> = {
  speech: "💬", action: "🎬", narration: "📖", ooc: "🔇",
};

/** Ленивое подтверждение/ввод: модалка описывается данными, а не JSX. */
type Dialog =
  | { kind: "confirm"; title: string; message: string; label: string; run: () => void }
  | { kind: "prompt"; title: string; label: string; initial: string; confirm: string; run: (v: string) => void };

export default function RunTablePage() {
  const { runId = "" } = useParams();
  const toast = useToast();
  const qc = useQueryClient();

  // --- Фильтры и вид ---
  const [activeAuthors, setActiveAuthors] = useState<Set<string>>(new Set());
  const [q, setQ] = useState("");
  const [after, setAfter] = useState("");
  const [before, setBefore] = useState("");
  const [role, setRole] = useState("");
  const [chat, setChat] = useState("");
  const [showHidden, setShowHidden] = useState(false);
  const [sort, setSort] = useState("seq");
  const [order, setOrder] = useState<"asc" | "desc">("asc");
  const [charsOpen, setCharsOpen] = useState(true);
  const [storyOpen, setStoryOpen] = useState(false);
  const [storyFormat, setStoryFormat] = useState<ExportFormat>("story");

  // --- Выделение и фокус ---
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [focusId, setFocusId] = useState<number | null>(null);

  // --- Оверлеи ---
  const [menu, setMenu] = useState<{ at: MenuPosition; msg: Message; sel: Offsets | null } | null>(null);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  const runQ = useQuery({ queryKey: ["run", runId], queryFn: () => api.getRun(runId) });
  const authorsQ = useQuery({ queryKey: ["authors", runId], queryFn: () => api.getAuthors(runId) });
  const chatsQ = useQuery({ queryKey: ["chats", runId], queryFn: () => api.getChats(runId) });
  const historyQ = useQuery({ queryKey: ["history", runId], queryFn: () => api.getHistory(runId) });

  const filters = useMemo(() => ({
    authors: [...activeAuthors], q, after, before, role, chat,
    hidden: showHidden ? "all" : "",
  }), [activeAuthors, q, after, before, role, chat, showHidden]);

  const msgQ = useInfiniteQuery({
    queryKey: ["messages", runId, filters, sort, order],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      api.getMessages(runId, { ...filters, sort, order, limit: PAGE, offset: pageParam as number }),
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((n, p) => n + p.items.length, 0);
      return loaded < last.total ? loaded : undefined;
    },
  });

  const items = useMemo(() => msgQ.data?.pages.flatMap((p) => p.items) ?? [], [msgQ.data]);
  const total = msgQ.data?.pages[0]?.total ?? 0;

  const invalidate = useCallback(() => {
    qc.invalidateQueries({ queryKey: ["messages", runId] });
    qc.invalidateQueries({ queryKey: ["authors", runId] });
    qc.invalidateQueries({ queryKey: ["chats", runId] });
    qc.invalidateQueries({ queryKey: ["history", runId] });
    qc.invalidateQueries({ queryKey: ["document", runId] });
    qc.invalidateQueries({ queryKey: ["run", runId] });
  }, [qc, runId]);

  // Все действия проходят через одну мутацию: разные тут только вызов и текст.
  const act = useMutation({
    mutationFn: async (job: { run: () => Promise<unknown>; ok?: string }) => {
      const result = await job.run();
      return { result, ok: job.ok };
    },
    onSuccess: ({ ok }) => { invalidate(); if (ok) toast(ok, "ok"); },
    onError: (e) => toast((e as Error).message, "err"),
  });
  // act.mutate стабилен между рендерами — на нём и держим идентичность call.
  const mutate = act.mutate;
  const call = useCallback(
    (run: () => Promise<unknown>, ok?: string) => mutate({ run, ok }),
    [mutate],
  );

  // --- Подгрузка следующей страницы при доскролле ---
  const sentinel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = sentinel.current;
    if (!el) return;
    const io = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting && msgQ.hasNextPage && !msgQ.isFetchingNextPage) {
        msgQ.fetchNextPage();
      }
    }, { rootMargin: "600px" });
    io.observe(el);
    return () => io.disconnect();
  }, [msgQ.hasNextPage, msgQ.isFetchingNextPage, msgQ.fetchNextPage]);

  // --- Операции ---
  const undo = useCallback(() => call(() => api.undo(runId)), [call, runId]);
  const redo = useCallback(() => call(() => api.redo(runId)), [call, runId]);

  const insertNear = useCallback((m: Message, where: "after" | "before") => {
    call(() => api.insertMessage(runId, {
      [where === "after" ? "after_id" : "before_id"]: m.id, author: m.author,
    }), "Сообщение вставлено");
  }, [call, runId]);

  const cutSelection = useCallback((m: Message, sel: Offsets) => {
    call(() => api.splitAtSelection(m.id, sel.start, sel.end), "Разрезано");
  }, [call]);

  const mergeSelected = useCallback(() => {
    if (selected.size < 2) return;
    call(async () => {
      const r = await api.mergeMessages(runId, [...selected]);
      setSelected(new Set());
      return r;
    }, "Объединено");
  }, [call, runId, selected]);

  const mergeWith = useCallback((a: Message, b: Message | undefined) => {
    if (!b) return;
    call(() => api.mergeMessages(runId, [a.id, b.id]), "Объединено");
  }, [call, runId]);

  function toggleRow(id: number) {
    setFocusId(id);
    setSelected((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
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

  function setSortCol(col: string) {
    if (sort === col) setOrder((o) => (o === "asc" ? "desc" : "asc"));
    else { setSort(col); setOrder("asc"); }
  }
  const arrow = (col: string) => (sort === col ? (order === "asc" ? " ▲" : " ▼") : "");

  function toggleAuthorFilter(name: string, additive: boolean) {
    setActiveAuthors((s) => {
      if (!additive) return s.has(name) && s.size === 1 ? new Set() : new Set([name]);
      const n = new Set(s);
      n.has(name) ? n.delete(name) : n.add(name);
      return n;
    });
  }

  function resetFilters() {
    setActiveAuthors(new Set());
    setQ(""); setAfter(""); setBefore(""); setRole(""); setChat(""); setShowHidden(false);
  }

  // --- Горячие клавиши ---
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const target = e.target as HTMLElement;
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) || target.isContentEditable;
      const mod = e.ctrlKey || e.metaKey;

      if (mod && e.key.toLowerCase() === "z" && !typing) {
        e.preventDefault();
        e.shiftKey ? redo() : undo();
        return;
      }
      if (mod && e.key.toLowerCase() === "y" && !typing) { e.preventDefault(); redo(); return; }
      if (mod && e.key.toLowerCase() === "f") {
        e.preventDefault(); searchRef.current?.focus(); searchRef.current?.select(); return;
      }
      if (mod && e.key.toLowerCase() === "h") { e.preventDefault(); setToolsOpen(true); return; }
      if (typing) return;

      const index = items.findIndex((m) => m.id === focusId);
      const move = (delta: number) => {
        const next = items[Math.max(0, Math.min(items.length - 1, index + delta))];
        if (next) {
          setFocusId(next.id);
          document.getElementById(`msg-${next.id}`)?.scrollIntoView({ block: "nearest" });
        }
      };
      if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); move(index < 0 ? 0 : 1); }
      else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); move(index < 0 ? 0 : -1); }
      else if (e.key === " " && focusId !== null) { e.preventDefault(); toggleRow(focusId); }
      else if (e.key === "Escape") { setSelected(new Set()); }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [items, focusId, undo, redo]);

  // --- Контекстное меню ---
  const menuItems = useMemo<MenuItem[]>(() => {
    if (!menu) return [];
    const { msg, sel } = menu;
    const index = items.findIndex((m) => m.id === msg.id);
    const prev = index > 0 ? items[index - 1] : undefined;
    const next = index >= 0 && index < items.length - 1 ? items[index + 1] : undefined;
    const canCut = !!sel && sel.start < msg.content.length && sel.text.trim().length > 0;

    return [
      {
        label: canCut ? `Вырезать выделенное («${short(sel!.text)}»)` : "Вырезать выделенное",
        icon: "✂", disabled: !canCut, hint: "ПКМ по выделению",
        onClick: () => sel && cutSelection(msg, sel),
      },
      {
        label: "Разделить автоматически", icon: "⑂",
        disabled: !msg.content.includes("\n"),
        onClick: () => call(() => api.splitAuto(msg.id, "smart"), "Разделено"),
      },
      {
        label: "Разделить по строкам", icon: "≡",
        disabled: !msg.content.includes("\n"),
        onClick: () => call(() => api.splitAuto(msg.id, "lines"), "Разделено"),
      },
      { sep: true },
      {
        label: "Вставить сообщение выше", icon: "↑＋",
        onClick: () => insertNear(msg, "before"),
      },
      {
        label: "Вставить сообщение ниже", icon: "↓＋",
        onClick: () => insertNear(msg, "after"),
      },
      {
        label: "Дублировать", icon: "⧉",
        onClick: () => call(() => api.duplicateMessage(msg.id), "Дублировано"),
      },
      { sep: true },
      {
        label: "Объединить с предыдущим", icon: "⤒", disabled: !prev,
        onClick: () => mergeWith(prev!, msg),
      },
      {
        label: "Объединить со следующим", icon: "⤓", disabled: !next,
        onClick: () => mergeWith(msg, next!),
      },
      {
        label: `Объединить выделенные (${selected.size})`, icon: "⇶",
        disabled: selected.size < 2, onClick: mergeSelected,
      },
      { sep: true },
      {
        label: "Роль", choices: [
          ...(Object.keys(ROLE_LABELS) as Exclude<Role, "">[]).map((r) => ({
            label: `${ROLE_ICONS[r]} ${ROLE_LABELS[r]}`,
            active: msg.role === r,
            onClick: () => call(() => api.updateMessage(msg.id, { role: r })),
          })),
          { label: "—", active: !msg.role, onClick: () => call(() => api.updateMessage(msg.id, { role: "" })) },
        ],
      },
      {
        label: msg.scene_title ? `Сцена: «${short(msg.scene_title)}»` : "Начать сцену здесь…",
        icon: "🎬",
        onClick: () => setDialog({
          kind: "prompt", title: "Заголовок сцены",
          label: "Сцена начинается с этого сообщения",
          initial: msg.scene_title, confirm: "Сохранить",
          run: (v) => call(() => api.updateMessage(msg.id, { scene_title: v }), "Сцена отмечена"),
        }),
      },
      ...(msg.scene_title ? [{
        label: "Убрать начало сцены", icon: "🚫",
        onClick: () => call(() => api.updateMessage(msg.id, { scene_title: "" }), "Сцена снята"),
      } as MenuItem] : []),
      { sep: true },
      {
        label: msg.hidden ? "Вернуть в экспорт" : "Скрыть из экспорта", icon: msg.hidden ? "👁" : "🚫",
        onClick: () => call(
          () => api.updateMessage(msg.id, { hidden: !msg.hidden }),
          msg.hidden ? "Возвращено" : "Скрыто (лог сохранён)",
        ),
      },
      // Двигаем относительно видимого соседа, а не соседа по всему прогону:
      // при активном фильтре иначе казалось бы, что кнопка не работает.
      { label: "Переместить выше", icon: "▲", disabled: !prev,
        onClick: () => call(() => api.moveMessage(msg.id, { before_id: prev!.id })) },
      { label: "Переместить ниже", icon: "▼", disabled: !next,
        onClick: () => call(() => api.moveMessage(msg.id, { after_id: next!.id })) },
      { sep: true },
      { label: "Копировать текст", icon: "⧉",
        onClick: () => navigator.clipboard?.writeText(msg.content) },
      {
        label: selected.size > 1 ? `Удалить выделенные (${selected.size})` : "Удалить сообщение",
        icon: "🗑", danger: true,
        onClick: () => setDialog({
          kind: "confirm", title: "Удаление",
          message: selected.size > 1
            ? `Удалить выделенные сообщения (${selected.size})?`
            : "Удалить это сообщение?",
          label: "Удалить",
          run: () => selected.size > 1
            ? call(async () => {
                const r = await api.bulkDelete(runId, { ids: [...selected] });
                setSelected(new Set());
                return r;
              }, "Удалено")
            : call(() => api.deleteMessage(msg.id), "Удалено"),
        }),
      },
    ];
  }, [menu, items, selected, call, runId, cutSelection, insertNear, mergeSelected, mergeWith]);

  const runTitle = runQ.data?.title || runId.slice(0, 8);
  const params = runQ.data?.params as { output_format?: string } | undefined;
  const defaultFormat = (params?.output_format === "txt" ? "txt" : "obsidian") as ExportFormat;
  const scope = useCallback(
    (useSelection: boolean): ScopedPayload =>
      useSelection ? { ids: [...selected] } : { filters },
    [selected, filters],
  );

  return (
    <div className="stack">
      <div className="row wrap">
        <Link to="/runs" className="btn ghost sm">← Прогоны</Link>
        <h1 style={{ margin: 0 }}>{runTitle}</h1>
        <span className="badge run">{total} из {runQ.data?.message_count ?? 0}</span>
        <div className="right row wrap">
          <button className="btn sm" disabled={!historyQ.data?.undo_label} onClick={undo}
                  title={historyQ.data?.undo_label ? `Отменить: ${historyQ.data.undo_label} (Ctrl+Z)` : "Отменять нечего"}>
            ↶
          </button>
          <button className="btn sm" disabled={!historyQ.data?.redo_label} onClick={redo}
                  title={historyQ.data?.redo_label ? `Повторить: ${historyQ.data.redo_label} (Ctrl+Shift+Z)` : "Повторять нечего"}>
            ↷
          </button>
          <button className="btn sm" onClick={() => setToolsOpen(true)} title="Ctrl+H">
            🛠 Инструменты
          </button>
          <button className={`btn sm ${storyOpen ? "primary" : ""}`}
                  onClick={() => setStoryOpen((v) => !v)}>📖 Предпросмотр</button>
          <ExportButtons runId={runId} />
          <button className="btn primary sm" onClick={() => setUploadOpen(true)}>☁ В Nextcloud</button>
        </div>
      </div>

      {/* --- Фильтры --- */}
      <div className="card">
        <div className="filters">
          <label className="field">Поиск по тексту <span className="hint">Ctrl+F</span>
            <input ref={searchRef} className="input" value={q} placeholder="подстрока…"
                   onChange={(e) => setQ(e.target.value)} />
          </label>
          <label className="field">Роль
            <select className="input" value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="">— любая —</option>
              {(Object.keys(ROLE_LABELS) as Exclude<Role, "">[]).map((r) => (
                <option key={r} value={r}>{ROLE_ICONS[r]} {ROLE_LABELS[r]}</option>
              ))}
            </select>
          </label>
          {(chatsQ.data?.length ?? 0) > 1 && (
            <label className="field">Канал
              <select className="input" value={chat} onChange={(e) => setChat(e.target.value)}>
                <option value="">— все —</option>
                {chatsQ.data?.map((c) => (
                  <option key={c.chat_name} value={c.chat_name}>{c.chat_name} ({c.count})</option>
                ))}
              </select>
            </label>
          )}
          <label className="field">С <span className="hint">ISO</span>
            <input className="input" value={after} placeholder="2026-01-01"
                   onChange={(e) => setAfter(e.target.value)} />
          </label>
          <label className="field">По <span className="hint">ISO</span>
            <input className="input" value={before} placeholder="2026-07-01"
                   onChange={(e) => setBefore(e.target.value)} />
          </label>
          <label className="switch" style={{ paddingBottom: 6 }}>
            <input type="checkbox" checked={showHidden}
                   onChange={(e) => setShowHidden(e.target.checked)} />
            скрытые
          </label>
          <button className="btn sm" onClick={resetFilters}>Сброс</button>
          <button className={`btn sm ${charsOpen ? "" : "ghost"}`}
                  onClick={() => setCharsOpen((v) => !v)}>👥 Персонажи</button>
        </div>

        {activeAuthors.size > 0 && (
          <div className="toolbar" style={{ marginTop: 10 }}>
            <span className="muted small">Фильтр по персонажам:</span>
            {[...activeAuthors].map((name) => (
              <button key={name} className="badge run chip"
                      onClick={() => toggleAuthorFilter(name, true)}>{name} ✕</button>
            ))}
          </div>
        )}

        {selected.size > 0 && (
          <div className="toolbar" style={{ marginTop: 10 }}>
            <span className="muted small">Выбрано {selected.size}:</span>
            <button className="btn sm" disabled={selected.size < 2} onClick={mergeSelected}>⇶ Объединить</button>
            {(Object.keys(ROLE_LABELS) as Exclude<Role, "">[]).map((r) => (
              <button key={r} className="btn sm" title={`Роль: ${ROLE_LABELS[r]}`}
                      onClick={() => call(() => api.bulkSet(runId, [...selected], { role: r }), "Роль проставлена")}>
                {ROLE_ICONS[r]}
              </button>
            ))}
            <button className="btn sm"
                    onClick={() => call(() => api.bulkSet(runId, [...selected], { hidden: true }), "Скрыто")}>
              🚫 Скрыть
            </button>
            <button className="btn sm"
                    onClick={() => call(() => api.bulkSet(runId, [...selected], { hidden: false }), "Возвращено")}>
              👁 Вернуть
            </button>
            <button className="btn sm danger" onClick={() => setDialog({
              kind: "confirm", title: "Удаление",
              message: `Удалить выделенные сообщения (${selected.size})?`, label: "Удалить",
              run: () => call(async () => {
                const r = await api.bulkDelete(runId, { ids: [...selected] });
                setSelected(new Set());
                return r;
              }, "Удалено"),
            })}>🗑 Удалить</button>
            <button className="btn sm ghost" onClick={() => setSelected(new Set())}>снять выделение</button>
          </div>
        )}
      </div>

      {/* --- Рабочая область --- */}
      <div className={`workbench${charsOpen ? "" : " no-chars"}${storyOpen ? " with-story" : ""}`}>
        {charsOpen && (
          <CharactersPanel
            authors={authorsQ.data ?? []}
            active={activeAuthors}
            onToggleActive={toggleAuthorFilter}
            onClear={() => setActiveAuthors(new Set())}
            onRename={(name) => setDialog({
              kind: "prompt", title: "Переименование персонажа",
              label: `Новое имя для «${name}»`, initial: name, confirm: "Переименовать",
              run: (to) => call(() => api.renameAuthor(runId, name, to), "Переименовано"),
            })}
            onMerge={(names) => setDialog({
              kind: "prompt", title: "Слияние персонажей",
              label: `Свести ${names.length} написаний к одному имени`,
              initial: names[0], confirm: "Слить",
              run: (to) => call(() => api.mergeAuthors(runId, names, to), "Персонажи слиты"),
            })}
            onDeleteMessages={(names) => setDialog({
              kind: "confirm", title: "Удаление реплик",
              message: `Удалить все сообщения персонажей: ${names.join(", ")}?`,
              label: "Удалить",
              run: () => names.forEach((n) => call(() => api.bulkDelete(runId, { author: n }), "Удалено")),
            })}
          />
        )}

        <div className="table-wrap">
          <table className="log-table">
            <thead>
              <tr>
                <th style={{ width: 28 }}>
                  <input type="checkbox" checked={allShownSelected} onChange={toggleAllShown} />
                </th>
                <th onClick={() => setSortCol("seq")} title="Порядок повествования">#{arrow("seq")}</th>
                <th onClick={() => setSortCol("chat")}>Чат{arrow("chat")}</th>
                <th onClick={() => setSortCol("ts")}>Дата-время{arrow("ts")}</th>
                <th onClick={() => setSortCol("author")}>Автор{arrow("author")}</th>
                <th>Сообщение</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((m, i) => (
                <Row key={m.id} m={m} index={i}
                     daySeparator={dayChanged(items[i - 1], m) && sort !== "author"}
                     selected={selected.has(m.id)} focused={focusId === m.id}
                     onToggle={() => toggleRow(m.id)}
                     onFocus={() => setFocusId(m.id)}
                     onSave={(patch) => call(() => api.updateMessage(m.id, patch))}
                     onInsertAfter={() => insertNear(m, "after")}
                     onMenu={(at, sel) => setMenu({ at, msg: m, sel })} />
              ))}
              {items.length === 0 && !msgQ.isLoading && (
                <tr><td colSpan={7} className="empty">Ничего не найдено под текущими фильтрами.</td></tr>
              )}
            </tbody>
          </table>
          <div ref={sentinel} className="sentinel">
            {msgQ.isFetchingNextPage && <span className="spinner" />}
            {!msgQ.hasNextPage && items.length > 0 && (
              <span className="muted small">Показаны все {total}</span>
            )}
          </div>
        </div>

        {storyOpen && (
          <StoryPane runId={runId} format={storyFormat} onFormat={setStoryFormat}
                     onClose={() => setStoryOpen(false)} />
        )}
      </div>

      {menu && <ContextMenu at={menu.at} items={menuItems} onClose={() => setMenu(null)} />}
      {toolsOpen && (
        <ToolsModal runId={runId} scope={scope} selectedCount={selected.size}
                    onDone={(msg) => toast(msg, "ok")} onClose={() => setToolsOpen(false)} />
      )}
      {uploadOpen && <UploadModal runId={runId} defaultFormat={defaultFormat}
                                  onClose={() => setUploadOpen(false)} />}
      {dialog?.kind === "confirm" && (
        <ConfirmModal title={dialog.title} message={dialog.message} confirmLabel={dialog.label}
                      onConfirm={dialog.run} onClose={() => setDialog(null)} />
      )}
      {dialog?.kind === "prompt" && (
        <PromptModal title={dialog.title} label={dialog.label} initial={dialog.initial}
                     confirmLabel={dialog.confirm} onSubmit={dialog.run}
                     onClose={() => setDialog(null)} />
      )}
    </div>
  );
}

// ---- Строка таблицы с инлайн-правкой, ролью и разделителями ----
function Row({ m, index, daySeparator, selected, focused, onToggle, onFocus, onSave,
               onInsertAfter, onMenu }: {
  m: Message;
  index: number;
  daySeparator: boolean;
  selected: boolean;
  focused: boolean;
  onToggle: () => void;
  onFocus: () => void;
  onSave: (patch: { author?: string; content?: string }) => void;
  onInsertAfter: () => void;
  onMenu: (at: MenuPosition, sel: Offsets | null) => void;
}) {
  const [editAuthor, setEditAuthor] = useState<string | null>(null);
  const [editContent, setEditContent] = useState<string | null>(null);
  const dt = useMemo(() => new Date(m.ts), [m.ts]);

  const classes = [
    selected ? "row-selected" : "",
    focused ? "row-focused" : "",
    m.hidden ? "row-hidden" : "",
    m.role ? `role-${m.role}` : "",
  ].filter(Boolean).join(" ");

  return (
    <>
      {daySeparator && (
        <tr className="day-row"><td colSpan={7}>{dt.toLocaleDateString()}</td></tr>
      )}
      {m.scene_title && (
        <tr className="scene-row">
          <td colSpan={7}><span className="scene-mark">🎬</span> {m.scene_title}</td>
        </tr>
      )}
      <tr id={`msg-${m.id}`} className={classes} onClick={onFocus}>
        <td>
          <input type="checkbox" checked={selected} onChange={onToggle} />
        </td>
        <td className="seq faint">{index + 1}</td>
        <td className="chat">{m.chat_name}</td>
        <td className="ts">{dt.toLocaleString()}</td>
        <td className="author" onDoubleClick={() => setEditAuthor(m.author)}>
          {editAuthor === null ? (
            <>
              {m.role && <span className="role-dot" title={ROLE_LABELS[m.role]}>{ROLE_ICONS[m.role]}</span>}
              {m.author}
            </>
          ) : (
            <input className="cell-edit" autoFocus value={editAuthor}
                   onChange={(e) => setEditAuthor(e.target.value)}
                   onBlur={() => {
                     if (editAuthor !== m.author) onSave({ author: editAuthor });
                     setEditAuthor(null);
                   }}
                   onKeyDown={(e) => {
                     if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                     if (e.key === "Escape") setEditAuthor(null);
                   }} />
          )}
        </td>
        <td className="msg">
          {editContent === null ? (
            <div className="msg-text"
                 onDoubleClick={() => setEditContent(m.content)}
                 onContextMenu={(e) => {
                   e.preventDefault();
                   onFocus();
                   onMenu({ x: e.clientX, y: e.clientY }, selectionOffsets(e.currentTarget));
                 }}>
              {m.content}
            </div>
          ) : (
            <textarea className="cell-edit" autoFocus rows={Math.min(12, editContent.split("\n").length + 1)}
                      value={editContent}
                      onChange={(e) => setEditContent(e.target.value)}
                      onBlur={() => {
                        if (editContent !== m.content) onSave({ content: editContent });
                        setEditContent(null);
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Escape") setEditContent(null);
                        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                          (e.target as HTMLTextAreaElement).blur();
                        }
                      }} />
          )}
        </td>
        <td className="actions">
          <button className="btn sm ghost row-add" title="Вставить сообщение ниже"
                  onClick={onInsertAfter}>＋</button>
          <button className="btn sm ghost" title="Действия (или правая кнопка по тексту)"
                  onClick={(e) => {
                    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
                    onFocus();
                    onMenu({ x: r.left, y: r.bottom }, null);
                  }}>⋯</button>
        </td>
      </tr>
    </>
  );
}

// ---- Кнопки экспорта (скачивание файла) ----
function ExportButtons({ runId }: { runId: string }) {
  const fmts: { f: ExportFormat; label: string; title: string }[] = [
    { f: "story", label: "📖", title: "Повествование (.md): сцены, действия курсивом, без меток времени" },
    { f: "obsidian", label: ".md", title: "Obsidian: вики-ссылки на персонажей" },
    { f: "txt", label: ".txt", title: "Плоский лог" },
    { f: "csv", label: ".csv", title: "Таблица" },
    { f: "json", label: ".json", title: "JSON" },
  ];
  return (
    <span className="row" title="Скачать текущее состояние (после правок)">
      <span className="muted small">Экспорт:</span>
      {fmts.map(({ f, label, title }) => (
        <a key={f} className="btn sm" href={api.exportUrl(runId, f)} title={title}>{label}</a>
      ))}
    </span>
  );
}

function dayChanged(prev: Message | undefined, cur: Message): boolean {
  if (!prev) return false;
  return new Date(prev.ts).toDateString() !== new Date(cur.ts).toDateString();
}

function short(text: string, limit = 28): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}
