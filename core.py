"""
Ядро скрэппинга: логика, общая для CLI и Discord-бота.

Персонажный embed = embed с непустыми `title` (имя персонажа) и
`description` (текст реплики). Строка результата:

    [дата-время] (Имя персонажа): Текст
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import discord


def _parse_id_list(raw: str | None) -> set[int]:
    if not raw:
        return set()
    return {int(p.strip()) for p in raw.split(",") if p.strip()}


def _parse_name_list(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {p.strip().casefold() for p in raw.split(",") if p.strip()}


@dataclass
class ScrapeConfig:
    """Настройки скрэппинга (что считать персонажем и как форматировать)."""

    author_ids: set[int] = field(default_factory=set)
    character_names: set[str] = field(default_factory=set)
    timezone: ZoneInfo | None = None
    time_format: str = "%Y-%m-%d %H:%M:%S"

    @classmethod
    def from_env(cls) -> "ScrapeConfig":
        tz_raw = os.getenv("TIMEZONE", "").strip()
        return cls(
            author_ids=_parse_id_list(os.getenv("AUTHOR_IDS")),
            character_names=_parse_name_list(os.getenv("CHARACTER_NAMES")),
            timezone=ZoneInfo(tz_raw) if tz_raw else None,
            time_format=os.getenv("TIME_FORMAT", "%Y-%m-%d %H:%M:%S").strip()
            or "%Y-%m-%d %H:%M:%S",
        )


@dataclass
class ScrapeResult:
    path: str
    lines: int
    messages_seen: int


# --- Чистые хелперы (без зависимости от discord.py) --------------------------
# Их используют оба пути: discord.py (bot/cli) и REST (веб-приложение).

def is_character(title: str | None, description: str | None, names: set[str]) -> bool:
    """Персонаж = есть непустые title и description (+ опц. белый список имён)."""
    if not title or not description:
        return False
    if names and title.strip().casefold() not in names:
        return False
    return True


def format_timestamp(created_at: datetime, cfg: ScrapeConfig) -> str:
    # created_at — timezone-aware (обычно UTC).
    local = created_at.astimezone(cfg.timezone)  # None => локальная зона машины
    return local.strftime(cfg.time_format)


def format_line(
    title: str, description: str, created_at: datetime, cfg: ScrapeConfig
) -> str:
    """Строка результата: [дата-время] (Имя): Текст"""
    ts = format_timestamp(created_at, cfg)
    return f"[{ts}] ({title.strip()}): {description.strip()}\n"


def is_character_embed(embed: discord.Embed, names: set[str]) -> bool:
    return is_character(embed.title, embed.description, names)


async def scrape_channel(
    channel: discord.abc.Messageable,
    out_path: str,
    cfg: ScrapeConfig,
    *,
    after: datetime | None = None,
    before: datetime | None = None,
    progress=None,
) -> ScrapeResult:
    """
    Проходит историю канала от старых к новым, пишет персонажные реплики в
    `out_path`. `progress` — необязательный async-callable(messages_seen, lines).
    """
    messages_seen = 0
    lines = 0

    with open(out_path, "w", encoding="utf-8") as out:
        async for message in channel.history(
            limit=None, oldest_first=True, after=after, before=before
        ):
            messages_seen += 1

            if cfg.author_ids and message.author.id not in cfg.author_ids:
                continue
            if not message.embeds:
                continue

            for embed in message.embeds:
                if not is_character_embed(embed, cfg.character_names):
                    continue
                out.write(
                    format_line(embed.title, embed.description, message.created_at, cfg)
                )
                lines += 1

            if progress and messages_seen % 1000 == 0:
                await progress(messages_seen, lines)

    return ScrapeResult(path=out_path, lines=lines, messages_seen=messages_seen)
