"use strict";

const $ = (id) => document.getElementById(id);

// Поля, которые едут в конфиг на сервер (несекретные значения возвращаются как есть).
const CONFIG_FIELDS = [
  "guild_id", "nextcloud_url", "nextcloud_user", "nextcloud_dir",
  "author_ids", "character_names", "name_blacklist", "text_blacklist",
  "timezone", "time_format",
];
const SECRET_FIELDS = ["discord_token", "nextcloud_app_password"];

let channels = [];        // {id,name,type_name,is_thread,...}
const selected = new Set();
let currentJob = null;    // id текущей задачи (для остановки)
let currentES = null;     // текущий EventSource

// ------------------------------- Настройки ---------------------------------

async function loadConfig() {
  const cfg = await fetch("/api/config").then((r) => r.json());
  CONFIG_FIELDS.forEach((f) => { if ($(f) && cfg[f] != null) $(f).value = cfg[f]; });
  SECRET_FIELDS.forEach((f) => {
    const badge = $(`${f}_state`);
    if (badge) {
      const set = cfg[`${f}_set`];
      badge.textContent = set ? "задан" : "не задан";
      badge.className = "badge " + (set ? "set" : "unset");
    }
  });
  if (!$("dest_dir").value) $("dest_dir").value = cfg.nextcloud_dir || "";
}

async function saveConfig() {
  const payload = {};
  CONFIG_FIELDS.forEach((f) => { payload[f] = $(f).value.trim(); });
  SECRET_FIELDS.forEach((f) => { if ($(f).value) payload[f] = $(f).value; });
  const status = $("settings-status");
  status.textContent = "Сохраняю…";
  try {
    await fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then((r) => { if (!r.ok) throw new Error(); });
    SECRET_FIELDS.forEach((f) => { $(f).value = ""; });
    status.textContent = "Сохранено ✓";
    await loadConfig();
    setTimeout(() => { status.textContent = ""; }, 2000);
  } catch { status.textContent = "Ошибка сохранения"; }
}

const openSettings = () => $("settings-overlay").classList.remove("hidden");
const closeSettings = () => $("settings-overlay").classList.add("hidden");

// ------------------------------- Каналы ------------------------------------

async function loadChannels() {
  const box = $("channels");
  box.innerHTML = '<div class="empty">Загрузка…</div>';
  const guild = $("guild_id").value.trim();
  const url = "/api/channels" + (guild ? `?guild_id=${encodeURIComponent(guild)}` : "");
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error((await res.json()).detail || "ошибка");
    channels = await res.json();
    channels.sort((a, b) => a.name.localeCompare(b.name));
    renderChannels();
  } catch (e) {
    box.innerHTML = `<div class="empty l-err">Ошибка: ${esc(e.message)}</div>`;
  }
}

function renderChannels() {
  const q = $("channel-search").value.trim().toLowerCase();
  const box = $("channels");
  const shown = channels.filter((c) => c.name.toLowerCase().includes(q));
  if (!shown.length) { box.innerHTML = '<div class="empty">Ничего не найдено.</div>'; updateCount(); return; }
  box.innerHTML = "";
  shown.forEach((c) => {
    const row = document.createElement("label");
    row.className = "chan" + (c.is_thread ? " thread" : "");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = selected.has(c.id);
    cb.onchange = () => { cb.checked ? selected.add(c.id) : selected.delete(c.id); updateCount(); };
    const nm = document.createElement("span");
    nm.className = "nm"; nm.textContent = c.name;
    const tp = document.createElement("span");
    tp.className = "type"; tp.textContent = c.type_name;
    row.append(cb, nm, tp);
    box.append(row);
  });
  updateCount();
}

function updateCount() {
  $("channels-count").textContent = channels.length
    ? `выбрано ${selected.size} из ${channels.length}` : "";
}

// ------------------------------- Папки Nextcloud ---------------------------

async function browseFolders() {
  const path = $("dest_dir").value.trim();
  const out = $("folder-list");
  out.textContent = "Загрузка…";
  try {
    const res = await fetch("/api/folders?path=" + encodeURIComponent(path));
    if (!res.ok) throw new Error((await res.json()).detail || "ошибка");
    const data = await res.json();
    out.textContent = data.folders.length
      ? "Подпапки: " + data.folders.join(" · ")
      : "Подпапок нет (папка создастся при заливке).";
  } catch (e) { out.textContent = "Ошибка: " + e.message; }
}

// ------------------------------- Предпросмотр ------------------------------

const hex = (color) => color == null ? null : "#" + color.toString(16).padStart(6, "0");

async function preview() {
  if (selected.size === 0) { alert("Выбери канал для предпросмотра."); return; }
  const first = channels.find((c) => selected.has(c.id));
  const summary = $("preview-summary");
  const table = $("preview-table");
  $("preview-overlay").classList.remove("hidden");
  summary.textContent = "Загрузка образца…";
  table.innerHTML = "";
  try {
    const res = await fetch("/api/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel_id: first.id, limit: 50 }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || "ошибка");
    const data = await res.json();
    renderPreview(data, first.name);
  } catch (e) { summary.innerHTML = `<span class="l-err">Ошибка: ${esc(e.message)}</span>`; }
}

function renderPreview(data, chanName) {
  const dropChips = Object.entries(data.dropped)
    .map(([r, n]) => `<span class="tag drop">${esc(r)}: ${n}</span>`).join("");
  $("preview-summary").innerHTML =
    `<b>#${esc(chanName)}</b> · эмбедов: ${data.total} ` +
    `<span class="tag kept">взято: ${data.kept}</span>${dropChips}`;

  const rows = data.items.map((it) => {
    const sw = it.color != null
      ? `<span class="swatch" style="background:${hex(it.color)}" title="${hex(it.color)}"></span>` : "—";
    const verdict = it.kept ? "✓ взято" : esc(it.reason);
    return `<tr class="${it.kept ? "kept" : "drop"}">
      <td>${esc(it.ts)}</td>
      <td class="nm">${esc(it.name) || "—"}</td>
      <td class="ctr">${sw}</td>
      <td class="ctr">${it.has_thumbnail ? "🖼" : "·"}</td>
      <td class="ctr">${it.has_fields ? "🔖" : "·"}</td>
      <td class="verdict">${verdict}</td>
      <td class="txt">${esc(it.text)}</td>
    </tr>`;
  }).join("");
  $("preview-table").innerHTML =
    `<thead><tr><th>время</th><th>имя</th><th>цвет</th><th>🖼</th><th>🔖</th>
      <th>вердикт</th><th>текст</th></tr></thead><tbody>${rows}</tbody>`;
}

// ------------------------------- Запуск + SSE ------------------------------

const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

function logLine(html) {
  const log = $("log");
  if (log.querySelector(".empty")) log.innerHTML = "";
  const div = document.createElement("div");
  div.innerHTML = html;
  log.append(div);
  log.scrollTop = log.scrollHeight;
}

function setRunning(running) {
  $("run").classList.toggle("hidden", running);
  $("stop").classList.toggle("hidden", !running);
  $("stop").disabled = false;
}

async function stop() {
  if (!currentJob) return;
  $("stop").disabled = true;
  logLine('<span class="l-sys">Останавливаю…</span>');
  try {
    await fetch(`/api/scrape/${currentJob}/stop`, { method: "POST" });
  } catch { /* сервер всё равно закроет поток */ }
}

async function run() {
  if (selected.size === 0) { alert("Выбери хотя бы один канал."); return; }
  $("log").innerHTML = "";
  $("result").className = "result hidden";
  $("counter").textContent = "";

  const chosen = channels.filter((c) => selected.has(c.id)).map((c) => ({ id: c.id, name: c.name }));
  const params = {
    channels: chosen,
    after: $("after").value.trim(),
    before: $("before").value.trim(),
    filename: $("filename").value.trim(),
    dest_dir: $("dest_dir").value.trim(),
    upload: $("upload").checked,
    share: $("share").checked,
  };

  logLine('<span class="l-sys">Запуск…</span>');
  let job;
  try {
    job = await fetch("/api/scrape", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    }).then((r) => r.json());
  } catch { logLine('<span class="l-err">Не удалось запустить задачу.</span>'); return; }

  currentJob = job.job_id;
  setRunning(true);

  const es = new EventSource(`/api/scrape/${job.job_id}/events`);
  currentES = es;
  const finish = () => { es.close(); setRunning(false); currentJob = null; currentES = null; };
  es.addEventListener("end", finish);
  es.onerror = finish;
  es.onmessage = (ev) => {
    const e = JSON.parse(ev.data);
    switch (e.type) {
      case "line":
        logLine(`<span class="l-ts">[${esc(e.ts)}]</span> ` +
          `<span class="l-name">(${esc(e.name)})</span>: ${esc(e.text)}`); break;
      case "channel":
        logLine(`<span class="l-chan">══ #${esc(e.name)} ══</span>`); break;
      case "progress":
        $("counter").textContent = `просмотрено ${e.seen}, реплик ${e.lines}`; break;
      case "status":
        logLine(`<span class="l-sys">${esc(e.message)}</span>`); break;
      case "done":
        showResult(e); break;          // закрытие потока сделает событие "end"
      case "error": {
        const box = $("result");
        box.className = "result err";
        box.innerHTML = `Ошибка: ${esc(e.message)}`;
        break;
      }
    }
  };
}

function showResult(e) {
  const box = $("result");
  box.className = "result";
  if (!e.lines) {
    box.innerHTML = e.stopped ? "Остановлено. Реплик собрать не успели."
      : (e.message || "Готово, но реплик не найдено.");
    return;
  }
  let html = e.stopped
    ? `⏹ Остановлено. Успели собрать <b>${e.lines}</b> реплик.`
    : `Готово: собрано <b>${e.lines}</b> реплик.`;
  if (e.link) html += `<br>🔗 <a href="${esc(e.link)}" target="_blank">${esc(e.link)}</a>`;
  else if (e.remote_path) html += `<br>Загружено: <code>${esc(e.remote_path)}</code>`;
  if (e.download) html += `<br>⬇ <a href="${esc(e.download)}">Скачать файл</a>`;
  box.innerHTML = html;
}

// ------------------------------- Привязка ----------------------------------

$("open-settings").onclick = openSettings;
$("close-settings").onclick = closeSettings;
$("settings-overlay").onclick = (e) => { if (e.target === $("settings-overlay")) closeSettings(); };
$("save-settings").onclick = saveConfig;
$("load-channels").onclick = loadChannels;
$("channel-search").oninput = renderChannels;
$("browse-folders").onclick = browseFolders;
$("run").onclick = run;
$("stop").onclick = stop;
$("preview").onclick = preview;
$("close-preview").onclick = () => $("preview-overlay").classList.add("hidden");
$("preview-overlay").onclick = (e) => { if (e.target === $("preview-overlay")) $("preview-overlay").classList.add("hidden"); };
$("select-all").onclick = (e) => { e.preventDefault(); channels.forEach((c) => selected.add(c.id)); renderChannels(); };
$("select-none").onclick = (e) => { e.preventDefault(); selected.clear(); renderChannels(); };
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeSettings(); $("preview-overlay").classList.add("hidden"); }
});

loadConfig();
