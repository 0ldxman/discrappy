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
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import config_store
import core
import discord_rest
import nextcloud

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
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
        text_blacklist=p.get("text_blacklist", ""),
        timezone=p.get("timezone", ""),
        time_format=p.get("time_format", ""),
    )


def _parse_dt(value: str | None, cfg: core.ScrapeConfig) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=cfg.timezone) if cfg.timezone else dt.astimezone()
    return dt


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

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        job.output_path = OUTPUT_DIR / f"{job.id}.txt"

        with open(job.output_path, "w", encoding="utf-8") as out:
            for ch in channels:
                ch_id = str(ch["id"])
                ch_name = ch.get("name", ch_id)
                await emit({"type": "channel", "name": ch_name, "id": ch_id})
                if len(channels) > 1:
                    out.write(f"# ===== {ch_name} =====\n")

                seen = 0
                async for msg in discord_rest.iter_messages(
                    token, ch_id, after=after, before=before
                ):
                    seen += 1
                    total_seen += 1
                    author_id = int(msg.get("author", {}).get("id", 0))
                    if cfg.author_ids and author_id not in cfg.author_ids:
                        continue
                    created = datetime.fromisoformat(msg["timestamp"])
                    for embed in msg.get("embeds", []):
                        # имя персонажа: сначала author.name, затем title (фолбэк)
                        name = (embed.get("author") or {}).get("name") or embed.get("title")
                        desc = embed.get("description")
                        if not core.is_character(name, desc, cfg):
                            continue
                        out.write(core.format_line(name, desc, created, cfg))
                        total_lines += 1
                        await emit({
                            "type": "line",
                            "channel": ch_name,
                            "ts": core.format_timestamp(created, cfg),
                            "name": name.strip(),
                            "text": desc.strip(),
                        })
                    if seen % 200 == 0:
                        await emit({"type": "progress", "seen": total_seen,
                                    "lines": total_lines})

        await emit({"type": "progress", "seen": total_seen, "lines": total_lines})

        if total_lines == 0:
            await emit({"type": "done", "lines": 0, "message": "Реплик не найдено."})
            return

        upload = params.get("upload", True)
        result: dict = {"type": "done", "lines": total_lines,
                        "download": f"/api/scrape/{job.id}/download"}
        if upload:
            nc = nextcloud.NextcloudConfig.from_mapping(stored)
            ts_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = params.get("filename", "").strip() or f"scrape-{ts_tag}.txt"
            if not filename.endswith(".txt"):
                filename += ".txt"
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
        await q.put({
            "type": "done", "stopped": True, "lines": total_lines,
            "download": f"/api/scrape/{job.id}/download" if total_lines else None,
        })
    except Exception as exc:  # noqa: BLE001 — доносим ошибку в UI
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
    for msg in messages:
        author_id = int(msg.get("author", {}).get("id", 0))
        created = datetime.fromisoformat(msg["timestamp"])
        for embed in msg.get("embeds", []):
            name = (embed.get("author") or {}).get("name") or embed.get("title")
            desc = embed.get("description")
            if cfg.author_ids and author_id not in cfg.author_ids:
                ok, reason = False, "другой автор"
            else:
                ok, reason = core.classify(name, desc, cfg)
            items.append({
                "ts": core.format_timestamp(created, cfg),
                "name": (name or "").strip(),
                "text": (desc or "").strip()[:280],
                "color": embed.get("color"),
                "has_thumbnail": "thumbnail" in embed,
                "has_fields": bool(embed.get("fields")),
                "author_id": str(author_id),
                "kept": ok,
                "reason": reason,
            })
            if ok:
                kept += 1
            else:
                dropped[reason] = dropped.get(reason, 0) + 1

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
    path = job.output_path if job else (OUTPUT_DIR / f"{job_id}.txt")
    if not path or not Path(path).exists():
        raise HTTPException(404, "Файл не найден (возможно, задача ещё идёт).")
    return FileResponse(path, filename=f"scrape-{job_id}.txt", media_type="text/plain")


# Статика и index — монтируем последними, чтобы не перехватывать /api/*.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
