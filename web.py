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
async def api_messages(run_id: str, author: str = "", after: str = "", before: str = "",
                       q: str = "", sort: str = "ts", order: str = "asc",
                       limit: int = 100, offset: int = 0) -> dict:
    if not db.get_run(run_id):
        raise HTTPException(404, "Прогон не найден.")
    return db.query_messages(run_id, author=author or None, after=after or None,
                             before=before or None, q=q or None, sort=sort,
                             order=order, limit=limit, offset=offset)


@app.get("/api/runs/{run_id}/authors")
async def api_authors(run_id: str) -> list[dict]:
    return db.list_authors(run_id)


@app.patch("/api/messages/{msg_id}")
async def api_update_message(msg_id: int, payload: dict) -> dict:
    ok = db.update_message(msg_id, author=payload.get("author"),
                           content=payload.get("content"))
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
            nc, str(tmp), filename, remote_dir=payload.get("dest_dir"),
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
