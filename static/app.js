"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

const CONFIG_FIELDS = [
  "guild_id", "nextcloud_url", "nextcloud_user", "nextcloud_dir",
  "author_ids", "character_names", "name_blacklist",
  "text_contains", "text_masks", "text_fuzzy", "fuzzy_threshold",
  "timezone", "time_format", "output_format",
];
const SECRET_FIELDS = ["discord_token", "nextcloud_app_password"];

let channels = [];
const selected = new Set();

// ------------------------------- Настройки ---------------------------------

async function loadConfig() {
  const cfg = await fetch("/api/config").then((r) => r.json());
  CONFIG_FIELDS.forEach((f) => { if ($(f) && cfg[f] != null && cfg[f] !== "") $(f).value = cfg[f]; });
  SECRET_FIELDS.forEach((f) => {
    const badge = $(`${f}_state`);
    if (badge) {
      const set = cfg[`${f}_set`];
      badge.textContent = set ? "задан" : "не задан";
      badge.className = "badge " + (set ? "set" : "unset");
    }
  });
  $("fuzzy_val").textContent = $("fuzzy_threshold").value;
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

const closeModal = (id) => $(id).classList.add("hidden");
const openModal = (id) => $(id).classList.remove("hidden");

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
  } catch (e) { box.innerHTML = `<div class="empty l-err">Ошибка: ${esc(e.message)}</div>`; }
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
    cb.type = "checkbox"; cb.checked = selected.has(c.id);
    cb.onchange = () => { cb.checked ? selected.add(c.id) : selected.delete(c.id); updateCount(); };
    const nm = document.createElement("span"); nm.className = "nm"; nm.textContent = c.name;
    const tp = document.createElement("span"); tp.className = "type"; tp.textContent = c.type_name;
    row.append(cb, nm, tp); box.append(row);
  });
  updateCount();
}
const updateCount = () => {
  $("channels-count").textContent = channels.length ? `выбрано ${selected.size} из ${channels.length}` : "";
};

// ------------------------------- Папки Nextcloud ---------------------------

async function browseFolders() {
  const out = $("folder-list");
  out.textContent = "Загрузка…";
  try {
    const res = await fetch("/api/folders?path=" + encodeURIComponent($("dest_dir").value.trim()));
    if (!res.ok) throw new Error((await res.json()).detail || "ошибка");
    const data = await res.json();
    out.textContent = data.folders.length ? "Подпапки: " + data.folders.join(" · ")
      : "Подпапок нет (папка создастся при заливке).";
  } catch (e) { out.textContent = "Ошибка: " + e.message; }
}

// ------------------------------- Предпросмотр ------------------------------

const hex = (color) => color == null ? null : "#" + (color >>> 0).toString(16).padStart(6, "0");

async function preview() {
  if (selected.size === 0) { alert("Выбери канал для предпросмотра."); return; }
  const first = channels.find((c) => selected.has(c.id));
  openModal("preview-overlay");
  $("preview-summary").textContent = "Загрузка образца…";
  $("preview-table").innerHTML = "";
  try {
    const res = await fetch("/api/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel_id: first.id, limit: 50 }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || "ошибка");
    renderPreview(await res.json(), first.name);
  } catch (e) { $("preview-summary").innerHTML = `<span class="l-err">Ошибка: ${esc(e.message)}</span>`; }
}

function renderPreview(data, chanName) {
  const chips = Object.entries(data.dropped)
    .map(([r, n]) => `<span class="tag drop">${esc(r)}: ${n}</span>`).join("");
  $("preview-summary").innerHTML =
    `<b>#${esc(chanName)}</b> · эмбедов: ${data.total} <span class="tag kept">взято: ${data.kept}</span>${chips}`;
  const rows = data.items.map((it) => {
    const sw = it.color != null
      ? `<span class="swatch" style="background:${hex(it.color)}" title="${hex(it.color)}"></span>` : "—";
    return `<tr class="${it.kept ? "kept" : "drop"}">
      <td>${esc(it.ts)}</td><td class="nm">${esc(it.name) || "—"}</td>
      <td class="ctr">${sw}</td><td class="ctr">${it.has_thumbnail ? "🖼" : "·"}</td>
      <td class="ctr">${it.has_fields ? "🔖" : "·"}</td>
      <td class="verdict">${it.kept ? "✓ взято" : esc(it.reason)}</td>
      <td class="txt">${esc(it.text)}</td></tr>`;
  }).join("");
  $("preview-table").innerHTML =
    `<thead><tr><th>время</th><th>имя</th><th>цвет</th><th>🖼</th><th>🔖</th><th>вердикт</th><th>текст</th></tr></thead>
     <tbody>${rows}</tbody>`;
}

// ------------------------------- Запуски (параллельные) --------------------

function fmtEta(sec) {
  if (sec == null) return "";
  if (sec < 60) return `~${sec}с`;
  const m = Math.floor(sec / 60), s = sec % 60;
  return m < 60 ? `~${m}м${s ? " " + s + "с" : ""}` : `~${Math.floor(m / 60)}ч ${m % 60}м`;
}

async function startRun(chList, base) {
  const params = { ...base, channels: chList.map((c) => ({ id: c.id, name: c.name })) };
  let job;
  try {
    job = await fetch("/api/scrape", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    }).then((r) => r.json());
  } catch { return; }

  const card = renderRunCard(job.job_id, chList.map((c) => "#" + c.name).join(", "));
  const q = (sel) => card.querySelector(sel);
  const bar = q(".run-bar"), fill = q(".run-bar-fill");
  bar.classList.add("indeterminate");

  const es = new EventSource(`/api/scrape/${job.job_id}/events`);
  const finish = () => es.close();
  es.addEventListener("end", finish);
  es.onerror = finish;
  card.querySelector(".run-stop").onclick = () => {
    card.querySelector(".run-stop").disabled = true;
    fetch(`/api/scrape/${job.job_id}/stop`, { method: "POST" }).catch(() => {});
  };

  const log = (html) => {
    const l = q(".run-log"); const d = document.createElement("div");
    d.innerHTML = html; l.append(d); l.scrollTop = l.scrollHeight;
  };
  let seen = 0, lines = 0;
  const counts = () => { q(".run-counts").textContent = `просмотрено ${seen} · собрано ${lines}`; };

  es.onmessage = (ev) => {
    const e = JSON.parse(ev.data);
    switch (e.type) {
      case "line":
        lines++; counts();
        log(`<span class="l-ts">[${esc(e.ts)}]</span> <span class="l-name">(${esc(e.name)})</span>: ${esc(e.text)}`);
        break;
      case "channel":
        log(`<span class="l-sys">══ #${esc(e.name)} (${e.index}/${e.count}) ══</span>`);
        break;
      case "progress": {
        seen = e.seen; lines = e.lines; counts();
        if (e.percent == null) { bar.classList.add("indeterminate"); }
        else { bar.classList.remove("indeterminate"); fill.style.width = e.percent + "%"; }
        q(".run-eta").textContent = `${fmtEta(e.eta)}${e.percent == null ? "" : " · " + e.percent + "%"}`;
        break;
      }
      case "status": log(`<span class="l-sys">${esc(e.message)}</span>`); break;
      case "done": setRunDone(card, e); break;
      case "error": setRunState(card, "error", "ошибка");
        q(".run-result").classList.remove("hidden");
        q(".run-result").innerHTML = `<span class="l-err">Ошибка: ${esc(e.message)}</span>`; break;
    }
  };
}

function renderRunCard(jobId, title) {
  const wrap = $("runs");
  if (wrap.querySelector(".empty")) wrap.innerHTML = "";
  const card = document.createElement("div");
  card.className = "run"; card.id = "run-" + jobId;
  card.innerHTML = `
    <div class="run-head">
      <span class="run-title">${esc(title)}</span>
      <span class="run-status pill running">идёт</span>
      <button class="btn ghost icon run-stop" title="Остановить">■</button>
    </div>
    <div class="run-bar"><div class="run-bar-fill"></div></div>
    <div class="run-meta"><span class="run-counts">запуск…</span><span class="run-eta"></span></div>
    <div class="run-result hidden"></div>
    <details class="run-log-wrap"><summary>лог</summary><div class="run-log"></div></details>`;
  wrap.prepend(card);
  return card;
}

function setRunState(card, cls, label) {
  const pill = card.querySelector(".run-status");
  pill.className = "run-status pill " + cls; pill.textContent = label;
  card.querySelector(".run-stop").disabled = true;
  card.querySelector(".run-bar").classList.remove("indeterminate");
}

function setRunDone(card, e) {
  const stopped = e.stopped;
  setRunState(card, stopped ? "stopped" : "done", stopped ? "остановлено" : "готово");
  if (!stopped) card.querySelector(".run-bar-fill").style.width = "100%";
  const box = card.querySelector(".run-result");
  box.classList.remove("hidden"); box.className = "run-result ok";
  if (!e.lines) { box.innerHTML = stopped ? "Остановлено, реплик не собрано." : "Реплик не найдено."; return; }
  let html = `${stopped ? "⏹ Остановлено. " : ""}Собрано <b>${e.lines}</b> реплик`;
  if (e.characters) html += `, персонажей: ${e.characters}`;
  html += ".";
  if (e.link) html += `<br>🔗 <a href="${esc(e.link)}" target="_blank">${esc(e.link)}</a>`;
  else if (e.remote_path) html += `<br>Загружено: <code>${esc(e.remote_path)}</code>`;
  if (e.download) html += `<br>⬇ <a href="${esc(e.download)}">Скачать файл</a>`;
  box.innerHTML = html;
}

function run() {
  if (selected.size === 0) { alert("Выбери хотя бы один канал."); return; }
  const chosen = channels.filter((c) => selected.has(c.id));
  const base = {
    after: $("after").value.trim(), before: $("before").value.trim(),
    filename: $("filename").value.trim(), dest_dir: $("dest_dir").value.trim(),
    output_format: $("output_format").value,
    upload: $("upload").checked, share: $("share").checked,
  };
  if ($("per_channel").checked) chosen.forEach((c) => startRun([c], base));
  else startRun(chosen, base);
}

// ------------------------------- Привязка ----------------------------------

$("open-settings").onclick = () => openModal("settings-overlay");
$("close-settings").onclick = () => closeModal("settings-overlay");
$("settings-overlay").onclick = (e) => { if (e.target === $("settings-overlay")) closeModal("settings-overlay"); };
$("save-settings").onclick = saveConfig;
$("fuzzy_threshold").oninput = () => { $("fuzzy_val").textContent = $("fuzzy_threshold").value; };
$("load-channels").onclick = loadChannels;
$("channel-search").oninput = renderChannels;
$("browse-folders").onclick = browseFolders;
$("preview").onclick = preview;
$("close-preview").onclick = () => closeModal("preview-overlay");
$("preview-overlay").onclick = (e) => { if (e.target === $("preview-overlay")) closeModal("preview-overlay"); };
$("run").onclick = run;
$("clear-runs").onclick = (e) => {
  e.preventDefault();
  document.querySelectorAll("#runs .run").forEach((c) => {
    if (!c.querySelector(".run-status").classList.contains("running")) c.remove();
  });
  if (!$("runs").children.length)
    $("runs").innerHTML = '<div class="empty">Запущенные процессы появятся здесь — можно несколько параллельно.</div>';
};
$("select-all").onclick = (e) => { e.preventDefault(); channels.forEach((c) => selected.add(c.id)); renderChannels(); };
$("select-none").onclick = (e) => { e.preventDefault(); selected.clear(); renderChannels(); };
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeModal("settings-overlay"); closeModal("preview-overlay"); }
});

loadConfig();
