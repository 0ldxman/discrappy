"use strict";

const $ = (id) => document.getElementById(id);
const CONFIG_FIELDS = [
  "guild_id", "nextcloud_url", "nextcloud_user", "nextcloud_dir",
  "author_ids", "character_names", "timezone", "time_format",
];
const SECRET_FIELDS = ["discord_token", "nextcloud_app_password"];

let channels = [];        // {id,name,type,type_name,parent_id,is_thread}
const selected = new Set();

// ------------------------------- Настройки ---------------------------------

async function loadConfig() {
  const cfg = await fetch("/api/config").then((r) => r.json());
  CONFIG_FIELDS.forEach((f) => { if ($(f) && cfg[f] != null) $(f).value = cfg[f]; });
  SECRET_FIELDS.forEach((f) => {
    const set = cfg[`${f}_set`];
    const badge = $(`${f}_state`);
    if (badge) {
      badge.textContent = set ? "задан" : "не задан";
      badge.className = "badge " + (set ? "set" : "unset");
    }
  });
  // подставим папку по умолчанию в поле назначения, если пусто
  if (!$("dest_dir").value) $("dest_dir").value = cfg.nextcloud_dir || "";
}

async function saveConfig() {
  const payload = {};
  CONFIG_FIELDS.forEach((f) => { payload[f] = $(f).value.trim(); });
  SECRET_FIELDS.forEach((f) => {
    const v = $(f).value;
    if (v) payload[f] = v;          // пустое поле не отправляем — секрет не затрётся
  });
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
  } catch { status.textContent = "Ошибка сохранения"; }
}

// ------------------------------- Каналы ------------------------------------

async function loadChannels() {
  const box = $("channels");
  box.innerHTML = '<div class="chan"><span class="muted">Загрузка…</span></div>';
  const guild = $("guild_id").value.trim();
  const url = "/api/channels" + (guild ? `?guild_id=${encodeURIComponent(guild)}` : "");
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error((await res.json()).detail || "ошибка");
    channels = await res.json();
    channels.sort((a, b) => a.name.localeCompare(b.name));
    renderChannels();
  } catch (e) {
    box.innerHTML = `<div class="chan"><span class="l-err">Ошибка: ${e.message}</span></div>`;
  }
}

function renderChannels() {
  const q = $("channel-search").value.trim().toLowerCase();
  const box = $("channels");
  const shown = channels.filter((c) => c.name.toLowerCase().includes(q));
  box.innerHTML = "";
  shown.forEach((c) => {
    const row = document.createElement("label");
    row.className = "chan" + (c.is_thread ? " thread" : "");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = selected.has(c.id);
    cb.onchange = () => { cb.checked ? selected.add(c.id) : selected.delete(c.id); updateCount(); };
    const name = document.createElement("span");
    name.textContent = c.name;
    name.className = "grow";
    const type = document.createElement("span");
    type.className = "type";
    type.textContent = c.type_name;
    row.append(cb, name, type);
    box.append(row);
  });
  updateCount();
}

function updateCount() {
  $("channels-count").textContent =
    `· выбрано ${selected.size} из ${channels.length}`;
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
      ? "Подпапки: " + data.folders.join(", ")
      : "Подпапок нет (папку создадим при заливке).";
  } catch (e) { out.textContent = "Ошибка: " + e.message; }
}

// ------------------------------- Запуск + SSE ------------------------------

function logLine(html) {
  const log = $("log");
  const div = document.createElement("div");
  div.innerHTML = html;
  log.append(div);
  log.scrollTop = log.scrollHeight;
}
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

async function run() {
  if (selected.size === 0) { alert("Выбери хотя бы один канал."); return; }
  $("log").innerHTML = "";
  $("result").className = "result hidden";
  $("counter").textContent = "";

  const chosen = channels.filter((c) => selected.has(c.id))
    .map((c) => ({ id: c.id, name: c.name }));
  const params = {
    channels: chosen,
    after: $("after").value.trim(),
    before: $("before").value.trim(),
    filename: $("filename").value.trim(),
    dest_dir: $("dest_dir").value.trim(),
    author_ids: $("author_ids").value.trim(),
    character_names: $("character_names").value.trim(),
    timezone: $("timezone").value.trim(),
    time_format: $("time_format").value.trim(),
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

  const es = new EventSource(`/api/scrape/${job.job_id}/events`);
  es.addEventListener("end", () => es.close());
  es.onerror = () => es.close();
  es.onmessage = (ev) => {
    const e = JSON.parse(ev.data);
    if (e.type === "line") {
      logLine(`<span class="l-ts">[${esc(e.ts)}]</span> ` +
        `<span class="l-name">(${esc(e.name)})</span>: ${esc(e.text)}`);
    } else if (e.type === "channel") {
      logLine(`<span class="l-chan">══ #${esc(e.name)} ══</span>`);
    } else if (e.type === "progress") {
      $("counter").textContent = `· просмотрено ${e.seen}, реплик ${e.lines}`;
    } else if (e.type === "status") {
      logLine(`<span class="l-sys">${esc(e.message)}</span>`);
    } else if (e.type === "done") {
      showResult(e); es.close();
    } else if (e.type === "error") {
      const box = $("result");
      box.className = "result err";
      box.innerHTML = `Ошибка: ${esc(e.message)}`;
      es.close();
    }
  };
}

function showResult(e) {
  const box = $("result");
  box.className = "result";
  if (!e.lines) { box.innerHTML = e.message || "Готово, но реплик не найдено."; return; }
  let html = `Готово: собрано <b>${e.lines}</b> реплик. `;
  if (e.link) html += `<br>🔗 Публичная ссылка: <a href="${esc(e.link)}" target="_blank">${esc(e.link)}</a>`;
  else if (e.remote_path) html += `<br>Загружено: <code>${esc(e.remote_path)}</code>`;
  if (e.download) html += `<br>⬇ <a href="${esc(e.download)}">Скачать файл</a>`;
  box.innerHTML = html;
}

// ------------------------------- Привязка ----------------------------------

$("toggle-settings").onclick = () => $("settings").classList.toggle("hidden");
$("save-settings").onclick = saveConfig;
$("load-channels").onclick = loadChannels;
$("channel-search").oninput = renderChannels;
$("browse-folders").onclick = browseFolders;
$("run").onclick = run;
$("select-all").onclick = (e) => { e.preventDefault(); channels.forEach((c) => selected.add(c.id)); renderChannels(); };
$("select-none").onclick = (e) => { e.preventDefault(); selected.clear(); renderChannels(); };

loadConfig();
