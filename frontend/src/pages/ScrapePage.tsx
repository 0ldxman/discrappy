import { useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { useToast } from "../ui/toast";
import type { Channel, ScrapeEvent } from "../types";
import PreviewModal from "../components/PreviewModal";

interface LiveLine { name: string; text: string; ts: string; }

export default function ScrapePage() {
  const toast = useToast();
  const navigate = useNavigate();

  const [guildId, setGuildId] = useState("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [mode, setMode] = useState("both");
  const [outputFormat, setOutputFormat] = useState("obsidian");
  const [after, setAfter] = useState("");
  const [before, setBefore] = useState("");
  const [previewOpen, setPreviewOpen] = useState(false);

  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<{ pct: number | null; seen: number; lines: number; eta: number | null; channel: string } | null>(null);
  const [log, setLog] = useState<LiveLine[]>([]);
  const jobRef = useRef<string | null>(null);
  const esRef = useRef<EventSource | null>(null);

  const { data: guilds } = useQuery({ queryKey: ["guilds"], queryFn: api.getGuilds, retry: false });
  const channelsQ = useQuery({
    queryKey: ["channels", guildId],
    queryFn: () => api.getChannels(guildId),
    enabled: false,
  });
  const channels: Channel[] = channelsQ.data ?? [];

  const filtered = useMemo(() => {
    const q = search.toLowerCase();
    return channels.filter((c) => c.name.toLowerCase().includes(q));
  }, [channels, search]);

  function toggle(id: string) {
    setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  }

  function startScrape() {
    const chosen = channels.filter((c) => selected.has(c.id)).map((c) => ({ id: c.id, name: c.name }));
    if (!chosen.length) { toast("Не выбран ни один канал", "err"); return; }

    setRunning(true);
    setLog([]);
    setProgress({ pct: 0, seen: 0, lines: 0, eta: null, channel: "" });

    api.startScrape({ channels: chosen, mode, output_format: outputFormat, after, before, upload: false })
      .then(({ job_id }) => {
        jobRef.current = job_id;
        const es = new EventSource(`/api/scrape/${job_id}/events`);
        esRef.current = es;
        es.onmessage = (ev) => handleEvent(JSON.parse(ev.data) as ScrapeEvent);
        es.addEventListener("end", () => { es.close(); setRunning(false); });
        es.onerror = () => { es.close(); setRunning(false); };
      })
      .catch((e) => { toast((e as Error).message, "err"); setRunning(false); });
  }

  function handleEvent(ev: ScrapeEvent) {
    switch (ev.type) {
      case "channel":
        setProgress((p) => ({ ...(p ?? { pct: 0, seen: 0, lines: 0, eta: null }), channel: `${ev.name} (${ev.index}/${ev.count})` }));
        break;
      case "line":
        setLog((l) => [...l.slice(-199), { name: ev.name, text: ev.text, ts: ev.ts }]);
        break;
      case "progress":
        setProgress((p) => ({ pct: ev.percent, seen: ev.seen, lines: ev.lines, eta: ev.eta, channel: p?.channel ?? "" }));
        break;
      case "status":
        toast(ev.message);
        break;
      case "error":
        toast(ev.message, "err"); setRunning(false);
        break;
      case "done":
        setRunning(false);
        if (ev.stopped) toast(`Остановлено. Собрано реплик: ${ev.lines}`, "info");
        else toast(`Готово. Собрано реплик: ${ev.lines}`, "ok");
        if (ev.run_id && ev.lines > 0) {
          setTimeout(() => navigate(`/runs/${ev.run_id}`), 600);
        }
        break;
    }
  }

  function stopScrape() {
    if (jobRef.current) api.stopScrape(jobRef.current).catch(() => {});
  }

  return (
    <div className="stack">
      <h1>Скрэппинг</h1>
      <div className="grid-2">
        {/* --- Каналы --- */}
        <section className="card">
          <div className="card-head">
            <h2>1 · Каналы</h2>
            <span className="muted small right">{selected.size ? `выбрано ${selected.size}` : ""}</span>
          </div>
          <div className="toolbar">
            {guilds && guilds.length > 0 && (
              <select className="input" style={{ maxWidth: 200 }} value={guildId} onChange={(e) => setGuildId(e.target.value)}>
                <option value="">— сервер из настроек —</option>
                {guilds.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
              </select>
            )}
            <button className="btn" onClick={() => channelsQ.refetch()} disabled={channelsQ.isFetching}>
              {channelsQ.isFetching ? <span className="spinner" /> : "↻ Загрузить"}
            </button>
            <input className="input grow" placeholder="поиск по названию…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <div className="toolbar sub small" style={{ margin: "8px 0" }}>
            <a href="#" onClick={(e) => { e.preventDefault(); setSelected(new Set(filtered.map((c) => c.id))); }}>выбрать все</a>
            <a href="#" onClick={(e) => { e.preventDefault(); setSelected(new Set()); }}>снять все</a>
          </div>
          {channelsQ.isError && <div className="empty">{(channelsQ.error as Error).message}</div>}
          <div className="list">
            {filtered.length === 0 && <div className="empty">Нажми «Загрузить», чтобы получить список каналов.</div>}
            {filtered.map((c) => (
              <label className="item" key={c.id}>
                <input type="checkbox" checked={selected.has(c.id)} onChange={() => toggle(c.id)} />
                <span className="grow">{c.name}</span>
                {c.kind && <span className="badge small">{c.kind}</span>}
              </label>
            ))}
          </div>
        </section>

        {/* --- Параметры и запуск --- */}
        <section className="card">
          <div className="card-head"><h2>2 · Параметры и запуск</h2></div>
          <div className="fields">
            <label className="field">Режим сбора
              <select className="input" value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="both">Текст + Эмбеды</option>
                <option value="embeds">Только эмбеды</option>
                <option value="text">Только текст</option>
              </select>
            </label>
            <label className="field">Формат при экспорте <span className="hint">можно сменить позже</span>
              <select className="input" value={outputFormat} onChange={(e) => setOutputFormat(e.target.value)}>
                <option value="obsidian">Obsidian (.md)</option>
                <option value="txt">Текст (.txt)</option>
              </select>
            </label>
            <label className="field">Дата от <span className="hint">ISO</span>
              <input className="input" value={after} onChange={(e) => setAfter(e.target.value)} placeholder="2026-01-01" />
            </label>
            <label className="field">Дата до <span className="hint">ISO</span>
              <input className="input" value={before} onChange={(e) => setBefore(e.target.value)} placeholder="2026-07-01" />
            </label>
          </div>

          <div className="toolbar" style={{ marginTop: 12 }}>
            <button className="btn ghost" onClick={() => setPreviewOpen(true)} disabled={selected.size === 0}>👁 Предпросмотр</button>
            <span className="hint">классифицирует последние сообщения первого выбранного канала</span>
          </div>

          {!running ? (
            <button className="btn primary big" onClick={startScrape}>▶ Запустить скрэппинг → в таблицу</button>
          ) : (
            <button className="btn danger big" onClick={stopScrape}>■ Остановить</button>
          )}

          {progress && (
            <div className="stack" style={{ marginTop: 14 }}>
              <div className="progress"><span style={{ width: `${progress.pct ?? 0}%` }} /></div>
              <div className="row small muted">
                <span>{progress.channel}</span>
                <span className="right">
                  просмотрено {progress.seen} · реплик {progress.lines}
                  {progress.eta != null && ` · ~${progress.eta}с`}
                  {progress.pct != null && ` · ${progress.pct}%`}
                </span>
              </div>
              <div className="log">
                {log.length === 0 && <div className="muted small">Собранные реплики появятся здесь…</div>}
                {log.map((l, i) => (
                  <div className="line" key={i}><span className="faint mono">{l.ts}</span> <b>{l.name}</b>: {l.text}</div>
                ))}
              </div>
            </div>
          )}
        </section>
      </div>

      {previewOpen && (
        <PreviewModal
          channelId={channels.find((c) => selected.has(c.id))?.id ?? ""}
          onClose={() => setPreviewOpen(false)}
        />
      )}
    </div>
  );
}
