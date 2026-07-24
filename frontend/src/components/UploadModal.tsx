import { useState } from "react";
import { api } from "../api";
import { useToast } from "../ui/toast";
import type { ExportFormat } from "../types";

// Экспорт текущего состояния прогона из БД и заливка в Nextcloud.
export default function UploadModal({ runId, defaultFormat, onClose }: {
  runId: string; defaultFormat: ExportFormat; onClose: () => void;
}) {
  const toast = useToast();
  const [format, setFormat] = useState<ExportFormat>(defaultFormat);
  const [filename, setFilename] = useState("");
  const [destDir, setDestDir] = useState("");
  const [share, setShare] = useState(true);
  const [busy, setBusy] = useState(false);
  const [link, setLink] = useState<string | null>(null);

  async function upload() {
    setBusy(true);
    try {
      const r = await api.upload(runId, { format, filename, dest_dir: destDir, share });
      setLink(r.link || null);
      toast("Залито в Nextcloud", "ok");
    } catch (e) {
      toast((e as Error).message, "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>Заливка в Nextcloud</h2>
          <button className="btn ghost icon" onClick={onClose}>✕</button>
        </div>
        <div className="modal-body stack">
          <label className="field">Формат
            <select className="input" value={format} onChange={(e) => setFormat(e.target.value as ExportFormat)}>
              <option value="obsidian">Obsidian (.md)</option>
              <option value="txt">Текст (.txt)</option>
              <option value="csv">CSV (.csv)</option>
              <option value="json">JSON (.json)</option>
            </select>
          </label>
          <label className="field">Имя файла <span className="hint">без расширения</span>
            <input className="input" value={filename} onChange={(e) => setFilename(e.target.value)} placeholder="scrape-<дата>" />
          </label>
          <label className="field">Папка на Nextcloud <span className="hint">пусто = по умолчанию</span>
            <input className="input" value={destDir} onChange={(e) => setDestDir(e.target.value)} placeholder="discord-scrapes" />
          </label>
          <label className="switch">
            <input type="checkbox" checked={share} onChange={(e) => setShare(e.target.checked)} />
            <span>Публичная ссылка</span>
          </label>
          {link && <div className="small">Ссылка: <a href={link} target="_blank" rel="noreferrer">{link}</a></div>}
        </div>
        <div className="modal-foot">
          <button className="btn primary" onClick={upload} disabled={busy}>
            {busy ? <span className="spinner" /> : "Залить"}
          </button>
        </div>
      </div>
    </div>
  );
}
