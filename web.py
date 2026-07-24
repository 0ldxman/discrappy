"""
Веб-приложение (FastAPI) для скрэппинга Discord → Nextcloud.

Возможности UI:
  1. список каналов сервера с поиском (вкл. ветки и посты форума);
  2. настройка параметров (ID сервера, токен бота, креды Nextcloud);
  3. запуск скрэппинга по выбранным каналам с доп. опциями;
  4. live-просмотр собираемых реплик (SSE);
  5. выбор папки Nextcloud, куда сохранить файл.

Секреты хранятся на сервере (config_store) и не возвращаются в браузер.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

import config_store
import core
import db
import discord_rest
import exporters
import narrative
import nextcloud

BASE_DIR = Path(__file__).parent
# Собранный фронтенд (frontend/dist) — приоритетно; иначе старая статика.
STATIC_DIR = Path(os.getenv("STATIC_DIR") or (BASE_DIR / "frontend" / "dist"))
if not STATIC_DIR.exists():
    STATIC_DIR = BASE_DIR / "static"
INDEX_FILE = STATIC_DIR / "index.html"
OUTPUT_DIR = Path(os.getenv("DATA_DIR", "data")) / "outputs"
# Разрешённые источники для встраивания в iframe (Nextcloud). Напр. https://cloud.example.com
FRAME_ANCESTORS = os.getenv("FRAME_ANCESTORS", "'self'").strip()

app = FastAPI(title="discrapp")


@app.middleware("http")
async def _frame_headers(request: Request, call_next):
    # Разрешаем встраивание в Nextcloud (External Sites) через CSP frame-ancestors.
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = f"frame-ancestors {FRAME_ANCESTORS}"
    return resp


# ------------------------------- Задачи скрэппинга ---------------------------

@dataclass
class Job:
    id: str
    queue: "asyncio.Queue[dict | None]" = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    output_path: Path | None = None

JOBS: dict[str, Job] = {}


def _cfg_from_params(p: dict) -> core.ScrapeConfig:
    return core.ScrapeConfig.build(
        author_ids=p.get("author_ids", ""),
        name_whitelist=p.get("character_names", ""),
        name_blacklist=p.get("name_blacklist", ""),
        text_contains=p.get("text_contains", ""),
        text_masks=p.get("text_masks", ""),
        text_fuzzy=p.get("text_fuzzy", ""),
        fuzzy_threshold=p.get("fuzzy_threshold", ""),
        timezone=p.get("timezone", ""),
        time_format=p.get("time_format", ""),
        output_format=p.get("output_format", ""),
        mode=p.get("mode", ""),
        text_name_patterns=p.get("text_name_patterns", ""),
        text_fallback_nick=p.get("text_fallback_nick"),
        text_ignore_bots=p.get("text_ignore_bots"),
        text_command_prefixes=p.get("text_command_prefixes", ""),
        text_ooc_prefixes=p.get("text_ooc_prefixes", ""),
    )


def _parse_dt(value: str | None, cfg: core.ScrapeConfig) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=cfg.timezone) if cfg.timezone else dt.astimezone()
    return dt


async def _channel_bounds(token, ch_id, after, before):
    """Границы диапазона ID канала для прогресс-бара + отметка старта времени."""
    t0 = time.monotonic()
    start_id = end_id = 0
    try:
        start_id = (discord_rest.datetime_to_snowflake(after) if after
                    else int(await discord_rest.get_first_message_id(token, ch_id) or 0))
        if before:
            end_id = discord_rest.datetime_to_snowflake(before)
        else:
            ch = await discord_rest.get_channel(token, ch_id)
            end_id = int(ch.get("last_message_id") or 0)
    except Exception:  # noqa: BLE001 — прогресс необязателен, не валим задачу
        pass
    return start_id, end_id, t0


def _progress(cur: int, start: int, end: int, t0: float):
    """(процент, ETA сек) по позиции ID в диапазоне. None если оценить нельзя."""
    if not end or end <= start:
        return (None, None)
    frac = min(max((cur - start) / (end - start), 0.0), 1.0)
    elapsed = time.monotonic() - t0
    eta = round(elapsed * (1 - frac) / frac) if frac > 0.02 else None
    return (round(frac * 100, 1), eta)


async def _run_job(job: Job, params: dict) -> None:
    q = job.queue
    total_lines = 0
    total_seen = 0

    async def emit(event: dict) -> None:
        await q.put(event)

    try:
        stored = config_store.load()
        token = stored.get("discord_token", "").strip()
        if not token:
            raise RuntimeError("Токен бота не задан в настройках.")

        cfg = _cfg_from_params({**stored, **params})
        after = _parse_dt(params.get("after"), cfg)
        before = _parse_dt(params.get("before"), cfg)
        channels: list[dict] = params.get("channels") or []
        if not channels:
            raise RuntimeError("Не выбран ни один канал.")

        obsidian = cfg.output_format == "obsidian"
        ext = ".md" if obsidian else ".txt"
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        job.output_path = OUTPUT_DIR / f"{job.id}{ext}"
        characters: set[str] = set()

        # Каждый скрэппинг — «прогон» в БД; строки сохраняем туда (таблица-лог).
        title = ", ".join(c.get("name", str(c["id"])) for c in channels)[:200]
        db.create_run(job.id, guild_id=stored.get("guild_id", ""),
                      channels=channels, params=params, title=title)

        with open(job.output_path, "w", encoding="utf-8") as out:
            if obsidian:
                names = ", ".join(f'"{c.get("name", c["id"])}"' for c in channels)
                out.write(
                    "---\n"
                    f"scraped: {datetime.now().isoformat(timespec='seconds')}\n"
                    f"source: discord\n"
                    f"channels: [{names}]\n"
                    "tags: [rp-scrape]\n"
                    "---\n\n"
                )

            for idx, ch in enumerate(channels):
                ch_id = str(ch["id"])
                ch_name = ch.get("name", ch_id)
                # Оценка диапазона для прогресс-бара (по снежинкам ID).
                start_id, end_id, t0 = await _channel_bounds(
                    token, ch_id, after, before
                )
                await emit({"type": "channel", "name": ch_name, "id": ch_id,
                            "index": idx + 1, "count": len(channels)})
                out.write(f"\n## #{ch_name}\n\n" if obsidian
                          else f"# ===== {ch_name} =====\n")

                seen = 0
                async for msg in discord_rest.iter_messages(
                    token, ch_id, after=after, before=before
                ):
                    seen += 1
                    total_seen += 1
                    author = msg.get("author", {})
                    author_id = int(author.get("id", 0))
                    created = datetime.fromisoformat(msg["timestamp"])

                    async def _write(nm, txt, kind):
                        nonlocal total_lines
                        out.write(core.format_line(nm, txt, created, cfg))
                        total_lines += 1
                        characters.add(nm.strip())
                        db.add_message(job.id, chat_id=ch_id, chat_name=ch_name,
                                       created_at=created, author=nm, author_id=author_id,
                                       content=txt, kind=kind, discord_msg_id=msg["id"])
                        await emit({"type": "line", "channel": ch_name,
                                    "ts": core.format_timestamp(created, cfg),
                                    "name": nm.strip(), "text": txt.strip()})

                    # эмбеды (фильтр по author_ids — только здесь)
                    if cfg.collect_embeds and not (cfg.author_ids and author_id not in cfg.author_ids):
                        for embed in msg.get("embeds", []):
                            name = (embed.get("author") or {}).get("name") or embed.get("title")
                            desc = embed.get("description")
                            if core.is_character(name, desc, cfg):
                                await _write(name, desc, "embed")

                    # обычный текст от лица персонажа
                    if cfg.collect_text:
                        nm, txt, reason = core.extract_text_reply(
                            msg.get("content"), cfg, author)
                        if reason is None:
                            await _write(nm, txt, "text")

                    if seen % 100 == 0:
                        pct, eta = _progress(int(msg["id"]), start_id, end_id, t0)
                        await emit({"type": "progress", "seen": total_seen,
                                    "lines": total_lines, "percent": pct, "eta": eta,
                                    "channel_index": idx + 1, "count": len(channels)})

            if obsidian and characters:
                out.write("\n## Персонажи\n\n")
                for nm in sorted(characters):
                    out.write(f"- [[{core._wikilink(nm)}]]\n")

        await emit({"type": "progress", "seen": total_seen, "lines": total_lines,
                    "percent": 100.0, "eta": 0})

        if total_lines == 0:
            db.set_run_status(job.id, "done")
            await emit({"type": "done", "lines": 0, "run_id": job.id,
                        "message": "Реплик не найдено."})
            return

        db.set_run_status(job.id, "done")
        upload = params.get("upload", False)
        result: dict = {"type": "done", "lines": total_lines, "run_id": job.id,
                        "characters": len(characters),
                        "download": f"/api/scrape/{job.id}/download"}
        if upload:
            nc = nextcloud.NextcloudConfig.from_mapping(stored)
            ts_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = params.get("filename", "").strip() or f"scrape-{ts_tag}"
            if not filename.endswith((".txt", ".md")):
                filename += ext
            await emit({"type": "status", "message": "Заливаю на Nextcloud…"})
            remote_path, link = await nextcloud.upload_and_share(
                nc, str(job.output_path), filename,
                remote_dir=params.get("dest_dir"), share=params.get("share", True),
            )
            result["remote_path"] = remote_path
            result["link"] = link
        await emit(result)
    except asyncio.CancelledError:
        # Пользователь нажал «Стоп»: сообщаем о частичном результате и выходим.
        db.set_run_status(job.id, "stopped")
        await q.put({
            "type": "done", "stopped": True, "lines": total_lines, "run_id": job.id,
            "download": f"/api/scrape/{job.id}/download" if total_lines else None,
        })
    except Exception as exc:  # noqa: BLE001 — доносим ошибку в UI
        db.set_run_status(job.id, "error")
        await q.put({"type": "error", "message": str(exc)})
    finally:
        await q.put(None)  # сигнал завершения SSE


# --------------------------------- API ---------------------------------------

@app.get("/api/config")
async def get_config() -> dict:
    return config_store.public_view()


@app.post("/api/config")
async def set_config(payload: dict) -> dict:
    return config_store.save(payload)


@app.get("/api/guilds")
async def guilds() -> list[dict]:
    token = config_store.load().get("discord_token", "").strip()
    if not token:
        raise HTTPException(400, "Токен бота не задан.")
    try:
        return await discord_rest.get_guilds(token)
    except discord_rest.DiscordError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/channels")
async def channels(guild_id: str = "") -> list[dict]:
    stored = config_store.load()
    token = stored.get("discord_token", "").strip()
    guild_id = guild_id or stored.get("guild_id", "").strip()
    if not token:
        raise HTTPException(400, "Токен бота не задан.")
    if not guild_id:
        raise HTTPException(400, "Не указан ID сервера.")
    try:
        chans = await discord_rest.get_channels(token, guild_id)
    except discord_rest.DiscordError as exc:
        raise HTTPException(400, str(exc))
    return [c.to_dict() for c in chans]


@app.get("/api/folders")
async def folders(path: str = "") -> dict:
    stored = config_store.load()
    try:
        nc = nextcloud.NextcloudConfig.from_mapping(stored)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        subfolders = await nextcloud.list_folders(nc, path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Nextcloud: {exc}")
    return {"path": path.strip("/"), "folders": subfolders}


@app.post("/api/preview")
async def preview(payload: dict) -> dict:
    """
    Образец последних сообщений канала с классификацией каждого embed'а
    (взято/отброшено и почему) + структурные признаки (color/thumbnail/fields)
    для подбора фильтров. Использует токен и фильтры из сохранённого конфига,
    переопределяемые полями payload.
    """
    stored = config_store.load()
    token = stored.get("discord_token", "").strip()
    if not token:
        raise HTTPException(400, "Токен бота не задан.")
    channel_id = str(payload.get("channel_id") or "").strip()
    if not channel_id:
        raise HTTPException(400, "Не выбран канал для предпросмотра.")
    limit = int(payload.get("limit", 50) or 50)

    cfg = _cfg_from_params({**stored, **payload})
    try:
        messages = await discord_rest.get_recent_messages(token, channel_id, limit)
    except discord_rest.DiscordError as exc:
        raise HTTPException(400, str(exc))

    items: list[dict] = []
    kept = 0
    dropped: dict[str, int] = {}

    def add(kind, ts, name, text, ok, reason, **extra):
        nonlocal kept
        items.append({"kind": kind, "ts": ts, "name": (name or "").strip(),
                      "text": (text or "").strip()[:280], "kept": ok,
                      "reason": reason, **extra})
        if ok:
            kept += 1
        else:
            dropped[reason] = dropped.get(reason, 0) + 1

    for msg in messages:
        author = msg.get("author", {})
        author_id = int(author.get("id", 0))
        ts = core.format_timestamp(datetime.fromisoformat(msg["timestamp"]), cfg)

        if cfg.collect_embeds:
            for embed in msg.get("embeds", []):
                name = (embed.get("author") or {}).get("name") or embed.get("title")
                desc = embed.get("description")
                if cfg.author_ids and author_id not in cfg.author_ids:
                    ok, reason = False, "другой автор"
                else:
                    ok, reason = core.classify(name, desc, cfg)
                add("embed", ts, name, desc, ok, reason,
                    color=embed.get("color"), has_thumbnail="thumbnail" in embed,
                    has_fields=bool(embed.get("fields")))

        if cfg.collect_text and (msg.get("content") or "").strip():
            nm, txt, reason = core.extract_text_reply(msg.get("content"), cfg, author)
            display = nm if nm else core.text_display_name(author)
            add("text", ts, display, txt or msg.get("content"), reason is None, reason or "ок",
                color=None, has_thumbnail=False, has_fields=False)

    return {"total": len(items), "kept": kept, "dropped": dropped, "items": items}


@app.post("/api/scrape")
async def start_scrape(params: dict) -> dict:
    job = Job(id=uuid.uuid4().hex)
    JOBS[job.id] = job
    job.task = asyncio.create_task(_run_job(job, params))
    return {"job_id": job.id}


@app.post("/api/scrape/{job_id}/stop")
async def stop_scrape(job_id: str) -> dict:
    """Принудительно останавливает задачу скрэппинга (отменяет фоновую корутину)."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена.")
    if job.task and not job.task.done():
        job.task.cancel()
    return {"stopping": True}


@app.get("/api/scrape/{job_id}/events")
async def scrape_events(job_id: str) -> StreamingResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена.")

    async def gen():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(job.queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # keep-alive для прокси
                    continue
                if item is None:
                    yield "event: end\ndata: {}\n\n"
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            JOBS.pop(job_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/scrape/{job_id}/download")
async def download(job_id: str):
    job = JOBS.get(job_id)
    path = job.output_path if job and job.output_path else None
    if not path:  # задача уже удалена из памяти — ищем файл по обоим расширениям
        for ext in (".md", ".txt"):
            cand = OUTPUT_DIR / f"{job_id}{ext}"
            if cand.exists():
                path = cand
                break
    if not path or not Path(path).exists():
        raise HTTPException(404, "Файл не найден (возможно, задача ещё идёт).")
    return FileResponse(path, filename=f"scrape-{job_id}{Path(path).suffix}",
                        media_type="text/plain")


# ----------------------- Прогоны и таблица-лог (БД) --------------------------

@app.get("/api/runs")
async def api_runs() -> list[dict]:
    return db.list_runs()


@app.get("/api/runs/{run_id}")
async def api_run(run_id: str) -> dict:
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Прогон не найден.")
    return run


@app.delete("/api/runs/{run_id}")
async def api_delete_run(run_id: str) -> dict:
    db.delete_run(run_id)
    return {"deleted": True}


@app.get("/api/runs/{run_id}/messages")
async def api_messages(run_id: str, author: str = "", authors: str = "",
                       after: str = "", before: str = "", q: str = "",
                       role: str = "", hidden: str = "", chat: str = "",
                       sort: str = "seq", order: str = "asc",
                       limit: int = 100, offset: int = 0) -> dict:
    if not db.get_run(run_id):
        raise HTTPException(404, "Прогон не найден.")
    names = _author_list(authors) or ([author] if author else None)
    return db.query_messages(run_id, authors=names, after=after or None,
                             before=before or None, q=q or None, role=role or None,
                             hidden=hidden or None, chat=chat or None, sort=sort,
                             order=order, limit=limit, offset=offset)


@app.get("/api/runs/{run_id}/authors")
async def api_authors(run_id: str) -> list[dict]:
    return db.list_authors(run_id)


@app.get("/api/runs/{run_id}/chats")
async def api_chats(run_id: str) -> list[dict]:
    return db.list_chats(run_id)


@app.get("/api/runs/{run_id}/scenes")
async def api_scenes(run_id: str) -> list[dict]:
    return db.list_scenes(run_id)


@app.patch("/api/messages/{msg_id}")
async def api_update_message(msg_id: int, payload: dict) -> dict:
    ok = db.update_message(
        msg_id,
        author=payload.get("author"), content=payload.get("content"),
        role=payload.get("role"), hidden=payload.get("hidden"),
        scene_title=payload.get("scene_title"), note=payload.get("note"),
    )
    if not ok:
        raise HTTPException(404, "Сообщение не найдено или нечего менять.")
    return {"updated": True}


@app.delete("/api/messages/{msg_id}")
async def api_delete_message(msg_id: int) -> dict:
    if not db.delete_message(msg_id):
        raise HTTPException(404, "Сообщение не найдено.")
    return {"deleted": True}


@app.post("/api/runs/{run_id}/messages/delete")
async def api_bulk_delete(run_id: str, payload: dict) -> dict:
    """Массовое удаление: по списку id и/или по автору (все сообщения автора)."""
    n = db.delete_messages(run_id, ids=payload.get("ids"), author=payload.get("author"))
    return {"deleted": n}


@app.post("/api/runs/{run_id}/rename-author")
async def api_rename_author(run_id: str, payload: dict) -> dict:
    old = (payload.get("from") or "").strip()
    new = (payload.get("to") or "").strip()
    if not old or not new:
        raise HTTPException(400, "Нужны непустые поля from и to.")
    n = db.rename_author(run_id, old, new)
    return {"updated": n}


@app.post("/api/runs/{run_id}/merge-authors")
async def api_merge_authors(run_id: str, payload: dict) -> dict:
    """Сводит несколько написаний имени персонажа к одному каноническому."""
    sources = [s.strip() for s in (payload.get("sources") or []) if s and s.strip()]
    target = (payload.get("target") or "").strip()
    if not sources or not target:
        raise HTTPException(400, "Нужны sources и target.")
    return {"updated": db.merge_authors(run_id, sources, target)}


# ------------------- Разрезание, вставка, объединение ------------------------

def _known_authors(run_id: str) -> set[str]:
    return {a["author"] for a in db.list_authors(run_id) if a["author"]}


def _author_list(raw: str) -> list[str] | None:
    """Параметр authors — имена через перевод строки (запятая может быть в имени)."""
    names = [n for n in (raw or "").split("\n") if n.strip()]
    return names or None


def _to_parts(fragments: list[str], base_author: str, known: set[str],
              extract: bool) -> list[dict]:
    """
    Фрагменты текста → заготовки сообщений.

    Роль определяется по исходному фрагменту (префикс «(Имя) - » сам по себе
    признак прямой речи), поэтому считается до отрезания имени.
    """
    parts: list[dict] = []
    for fragment in fragments:
        role = narrative.detect_role(fragment)
        author, text = (narrative.extract_author(fragment, known) if extract
                        else (None, fragment))
        parts.append({"content": (text or fragment).strip(),
                      "author": author or base_author, "role": role})
    return parts


@app.post("/api/messages/{msg_id}/split")
async def api_split_message(msg_id: int, payload: dict) -> dict:
    """
    Режет сообщение по выделению на «до / выделенное / после».

    Пустые части отбрасываются, поэтому получается 2 или 3 сообщения; все — с
    тем же временем и подряд в порядке повествования.
    """
    msg = db.get_message(msg_id)
    if not msg:
        raise HTTPException(404, "Сообщение не найдено.")
    fragments = narrative.split_at_selection(
        msg["content"], int(payload.get("start", 0)), int(payload.get("end", 0)))
    if len(fragments) < 2:
        raise HTTPException(400, "Выделение не делит сообщение на части.")
    extract = payload.get("extract_author", True)
    parts = _to_parts(fragments, msg["author"], _known_authors(msg["run_id"]), extract)
    rows = db.split_message(msg_id, parts)
    if rows is None:
        raise HTTPException(400, "Разрезать не получилось.")
    return {"items": rows}


@app.post("/api/messages/{msg_id}/split-auto")
async def api_split_auto(msg_id: int, payload: dict) -> dict:
    """Разрезание одного сообщения по строкам / абзацам / автоматически."""
    msg = db.get_message(msg_id)
    if not msg:
        raise HTTPException(404, "Сообщение не найдено.")
    mode = (payload.get("mode") or "smart").strip()
    known = _known_authors(msg["run_id"])
    fragments = narrative.split_fragments(msg["content"], mode, known)
    if len(fragments) < 2:
        raise HTTPException(400, "В этом сообщении нечего разделять.")
    parts = _to_parts(fragments, msg["author"], known,
                      payload.get("extract_author", True))
    rows = db.split_message(msg_id, parts)
    if rows is None:
        raise HTTPException(400, "Разрезать не получилось.")
    return {"items": rows}


@app.post("/api/runs/{run_id}/messages")
async def api_insert_message(run_id: str, payload: dict) -> dict:
    """Вставляет пустое сообщение рядом с указанным (кнопка «+» в таблице)."""
    if not db.get_run(run_id):
        raise HTTPException(404, "Прогон не найден.")
    row = db.insert_message(
        run_id, after_id=payload.get("after_id"), before_id=payload.get("before_id"),
        author=(payload.get("author") or "").strip(),
        content=(payload.get("content") or "").strip(),
        role=(payload.get("role") or "").strip(),
    )
    if row is None:
        raise HTTPException(400, "Некуда вставлять: соседнее сообщение не найдено.")
    return row


@app.post("/api/runs/{run_id}/messages/merge")
async def api_merge_messages(run_id: str, payload: dict) -> dict:
    """Склеивает выбранные сообщения в одно (в порядке повествования)."""
    ids = payload.get("ids") or []
    if len(ids) < 2:
        raise HTTPException(400, "Нужно выбрать хотя бы два сообщения.")
    row = db.merge_messages(run_id, ids, separator=payload.get("separator", "\n"))
    if row is None:
        raise HTTPException(400, "Объединить не получилось.")
    return row


@app.post("/api/messages/{msg_id}/move")
async def api_move_message(msg_id: int, payload: dict) -> dict:
    ok = db.move_message(msg_id, after_id=payload.get("after_id"),
                         before_id=payload.get("before_id"),
                         direction=payload.get("direction"))
    if not ok:
        raise HTTPException(400, "Двигать некуда.")
    return {"moved": True}


@app.post("/api/messages/{msg_id}/duplicate")
async def api_duplicate_message(msg_id: int) -> dict:
    row = db.duplicate_message(msg_id)
    if row is None:
        raise HTTPException(404, "Сообщение не найдено.")
    return row


# --------------------------- Массовые операции -------------------------------

def _scope_rows(run_id: str, payload: dict) -> list[dict]:
    """
    Строки, на которые действует массовая операция.

    Либо явный список `ids` (выделенные строки), либо текущие фильтры таблицы —
    чтобы «применить ко всему, что на экране» вело себя предсказуемо.
    """
    filters = payload.get("filters") or {}
    return db.select_ids(
        run_id, ids=payload.get("ids"),
        authors=filters.get("authors"), q=filters.get("q") or None,
        role=filters.get("role") or None, chat=filters.get("chat") or None,
        after=filters.get("after") or None, before=filters.get("before") or None,
        hidden=filters.get("hidden") or "all",
    )


def _diff_preview(changes: list[dict], limit: int = 40) -> dict:
    return {"changed": len(changes), "items": changes[:limit]}


@app.post("/api/runs/{run_id}/replace")
async def api_replace(run_id: str, payload: dict) -> dict:
    """Поиск и замена по области действия. preview=true — только показать diff."""
    find = payload.get("find") or ""
    if not find:
        raise HTTPException(400, "Нечего искать.")
    repl = payload.get("replace") or ""
    regex = bool(payload.get("regex"))
    case = bool(payload.get("case"))
    changes: list[dict] = []
    updates: dict[int, str] = {}
    try:
        for row in _scope_rows(run_id, payload):
            new, n = narrative.apply_replace(row["content"], find, repl,
                                             regex=regex, case=case)
            if n and new != row["content"]:
                updates[row["id"]] = new
                changes.append({"id": row["id"], "author": row["author"],
                                "before": row["content"], "after": new, "hits": n})
    except re.error as exc:
        raise HTTPException(400, f"Некорректное регулярное выражение: {exc}")

    if payload.get("preview"):
        return _diff_preview(changes)
    label = f"Замена «{find}» → «{repl}» ({len(updates)})"
    return {"changed": db.apply_content_updates(run_id, updates, label=label)}


@app.post("/api/runs/{run_id}/cleanup")
async def api_cleanup(run_id: str, payload: dict) -> dict:
    """Пакетная чистка текста выбранными операциями (см. narrative.CLEANUP_OPS)."""
    ops = [o for o in (payload.get("ops") or []) if o in narrative.CLEANUP_OPS]
    if not ops:
        raise HTTPException(400, "Не выбрана ни одна операция чистки.")
    changes: list[dict] = []
    updates: dict[int, str] = {}
    for row in _scope_rows(run_id, payload):
        new = narrative.cleanup_text(row["content"], ops)
        if new != row["content"]:
            updates[row["id"]] = new
            changes.append({"id": row["id"], "author": row["author"],
                            "before": row["content"], "after": new, "hits": 1})
    if payload.get("preview"):
        return _diff_preview(changes)
    return {"changed": db.apply_content_updates(
        run_id, updates, label=f"Чистка текста ({len(updates)})")}


@app.post("/api/runs/{run_id}/auto-split")
async def api_auto_split(run_id: str, payload: dict) -> dict:
    """
    Массовое разрезание склеенных постов.

    Именно эта операция разносит «**действие**» и «(Имя) - реплика», попавшие
    в одно сообщение Discord, по отдельным строкам лога.
    """
    mode = (payload.get("mode") or "smart").strip()
    extract = payload.get("extract_author", True)
    known = _known_authors(run_id)
    plan: list[tuple[int, list[dict]]] = []
    preview: list[dict] = []
    for row in _scope_rows(run_id, payload):
        fragments = narrative.split_fragments(row["content"], mode, known)
        if len(fragments) < 2:
            continue
        parts = _to_parts(fragments, row["author"], known, extract)
        plan.append((row["id"], parts))
        preview.append({"id": row["id"], "author": row["author"],
                        "before": row["content"], "parts": parts})
    if payload.get("preview"):
        return {"changed": len(plan), "items": preview[:40]}
    label = f"Авто-разделение сообщений ({len(plan)})"
    return {"changed": db.split_many(run_id, plan, label=label)}


@app.post("/api/runs/{run_id}/detect-roles")
async def api_detect_roles(run_id: str, payload: dict) -> dict:
    """Расставляет роли (речь / действие / нарратив / OOC) по эвристике."""
    updates: dict[int, str] = {}
    preview: list[dict] = []
    overwrite = bool(payload.get("overwrite"))
    for row in _scope_rows(run_id, payload):
        if row["role"] and not overwrite:
            continue
        role = narrative.detect_role(row["content"])
        if role != row["role"]:
            updates[row["id"]] = role
            preview.append({"id": row["id"], "author": row["author"],
                            "before": row["content"], "after": role, "hits": 1})
    if payload.get("preview"):
        return _diff_preview(preview)
    return {"changed": db.apply_field_updates(
        run_id, "role", updates, label=f"Разметка ролей ({len(updates)})")}


@app.post("/api/runs/{run_id}/messages/set")
async def api_bulk_set(run_id: str, payload: dict) -> dict:
    """Массово ставит роль / скрытие / автора выбранным сообщениям."""
    ids = payload.get("ids") or []
    if not ids:
        raise HTTPException(400, "Не выбрано ни одного сообщения.")
    fields = {k: payload[k] for k in ("role", "hidden", "author") if k in payload}
    if not fields:
        raise HTTPException(400, "Нечего менять.")
    labels = {"role": "Роль", "hidden": "Видимость", "author": "Автор"}
    label = f"{labels[next(iter(fields))]}: массовая правка ({len(ids)})"
    return {"changed": db.bulk_set(run_id, ids, label=label, **fields)}


# ------------------------------ Отмена правок --------------------------------

@app.post("/api/runs/{run_id}/undo")
async def api_undo(run_id: str) -> dict:
    label = db.undo(run_id)
    if label is None:
        raise HTTPException(400, "Отменять нечего.")
    return {"label": label}


@app.post("/api/runs/{run_id}/redo")
async def api_redo(run_id: str) -> dict:
    label = db.redo(run_id)
    if label is None:
        raise HTTPException(400, "Повторять нечего.")
    return {"label": label}


@app.get("/api/runs/{run_id}/history")
async def api_history(run_id: str, limit: int = 30) -> dict:
    return db.history(run_id, limit)


def _export_doc(run_id: str, fmt: str) -> tuple[str, str, str]:
    """Собирает документ прогона в формате fmt. → (текст, расширение, mime)."""
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Прогон не найден.")
    if fmt not in exporters.FORMATS:
        raise HTTPException(400, f"Неизвестный формат: {fmt}")
    stored = config_store.load()
    cfg = _cfg_from_params({**stored, **run.get("params", {})})
    rows = db.iter_run_messages(run_id)
    doc = exporters.build(fmt, rows, cfg, run=run)
    ext, mime = exporters.FORMATS[fmt]
    return doc, ext, mime


@app.get("/api/runs/{run_id}/document")
async def api_document(run_id: str, format: str = "story") -> dict:
    """Тот же документ, что и экспорт, но текстом в JSON — для панели предпросмотра."""
    doc, _, _ = _export_doc(run_id, format)
    return {"format": format, "text": doc}


@app.get("/api/runs/{run_id}/export")
async def api_export(run_id: str, format: str = "txt"):
    doc, ext, mime = _export_doc(run_id, format)
    filename = f"scrape-{run_id}{ext}"
    return Response(content=doc, media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/api/runs/{run_id}/upload")
async def api_upload(run_id: str, payload: dict) -> dict:
    """Экспортирует текущее состояние прогона из БД и заливает в Nextcloud."""
    fmt = payload.get("format", "obsidian")
    doc, ext, _ = _export_doc(run_id, fmt)
    stored = config_store.load()
    try:
        nc = nextcloud.NextcloudConfig.from_mapping(stored)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_DIR / f"export-{run_id}{ext}"
    tmp.write_text(doc, encoding="utf-8")
    ts_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = (payload.get("filename") or f"scrape-{ts_tag}").strip()
    if not filename.endswith(ext):
        filename += ext
    try:
        remote_path, link = await nextcloud.upload_and_share(
            nc, str(tmp), filename,
            remote_dir=(payload.get("dest_dir") or "").strip() or None,
            share=payload.get("share", True),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Nextcloud: {exc}")
    return {"remote_path": remote_path, "link": link}


# SPA: отдаём собранный фронтенд, а на неизвестные пути — index.html
# (клиентский роутинг react-router). Объявляем последним, /api/* уже разобраны выше.
@app.get("/{full_path:path}")
async def spa(full_path: str):
    if full_path.startswith("api"):
        raise HTTPException(404, "Not found")
    if full_path:
        candidate = (STATIC_DIR / full_path).resolve()
        if candidate.is_file() and STATIC_DIR.resolve() in candidate.parents:
            return FileResponse(candidate)
    if INDEX_FILE.exists():
        return FileResponse(INDEX_FILE)
    raise HTTPException(404, "UI не собран (frontend/dist отсутствует).")
