"""
CLI-обёртка: скрейпит канал по ID и (по умолчанию) заливает результат на Nextcloud.

Примеры:
    python cli.py 123456789012345678
    python cli.py 123456789012345678 --after 2026-01-01 --before 2026-07-01
    python cli.py 123456789012345678 --no-upload      # только локальный файл
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime

import discord
from dotenv import load_dotenv

import core
import nextcloud

load_dotenv()


def _parse_dt(value: str | None, cfg: core.ScrapeConfig) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:  # наивную дату трактуем в настроенной/локальной зоне
        dt = dt.replace(tzinfo=cfg.timezone) if cfg.timezone else dt.astimezone()
    return dt


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Скрэппер персонажных реплик из Discord.")
    p.add_argument("channel_id", type=int, help="ID канала Discord")
    p.add_argument("--after", help="брать сообщения после даты (ISO, напр. 2026-01-01)")
    p.add_argument("--before", help="брать сообщения до даты (ISO)")
    p.add_argument("--output", help="локальный файл (по умолчанию scrape-<channel>-<ts>.txt)")
    p.add_argument("--remote-name", help="имя файла на Nextcloud (по умолчанию как локальный)")
    p.add_argument("--no-upload", action="store_true", help="не заливать на Nextcloud")
    p.add_argument("--no-share", action="store_true", help="не создавать публичную ссылку")
    return p.parse_args()


async def run(args: argparse.Namespace) -> int:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        print("[ОШИБКА] Не задан DISCORD_TOKEN.", file=sys.stderr)
        return 1

    cfg = core.ScrapeConfig.from_env()
    after = _parse_dt(args.after, cfg)
    before = _parse_dt(args.before, cfg)

    ts_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = args.output or f"scrape-{args.channel_id}-{ts_tag}.txt"
    remote_name = args.remote_name or os.path.basename(out_path)

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    result: dict[str, object] = {}

    @client.event
    async def on_ready() -> None:
        try:
            channel = client.get_channel(args.channel_id) or await client.fetch_channel(
                args.channel_id
            )
            if not isinstance(channel, discord.abc.Messageable):
                raise RuntimeError(f"Канал {args.channel_id} не текстовый.")

            print(f"Читаю историю канала {args.channel_id}...")

            async def progress(seen: int, lines: int) -> None:
                print(f"  ...{seen} сообщений, {lines} реплик")

            scrape = await core.scrape_channel(
                channel, out_path, cfg, after=after, before=before, progress=progress
            )
            print(f"Готово: {scrape.lines} реплик из {scrape.messages_seen} сообщений -> {out_path}")
            result["scrape"] = scrape
        except Exception as exc:  # noqa: BLE001 — донесём ошибку наверх
            result["error"] = exc
        finally:
            await client.close()

    await client.start(token)

    if "error" in result:
        print(f"[ОШИБКА] {result['error']}", file=sys.stderr)
        return 1

    scrape = result.get("scrape")
    if not isinstance(scrape, core.ScrapeResult):
        print("[ОШИБКА] Скрэппинг не выполнился.", file=sys.stderr)
        return 1

    if scrape.lines == 0:
        print("Реплик не найдено — на Nextcloud ничего не заливаю.")
        return 0

    if args.no_upload:
        return 0

    try:
        nc = nextcloud.NextcloudConfig.from_env()
    except ValueError as exc:
        print(f"[ОШИБКА Nextcloud] {exc}", file=sys.stderr)
        return 1

    print("Заливаю на Nextcloud...")
    remote_path, link = await nextcloud.upload_and_share(
        nc, out_path, remote_name, share=not args.no_share
    )
    print(f"Загружено: {remote_path}")
    if link:
        print(f"Публичная ссылка: {link}")
    return 0


def main() -> None:
    args = parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
