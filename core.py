"""
Ядро скрэппинга: логика, общая для CLI, Discord-бота и веб-приложения.

Реплика персонажа = embed, у которого есть имя (embed.author.name или, как
фолбэк, embed.title) и текст (embed.description), и который проходит фильтры.
Строка результата:

    [дата-время] (Имя персонажа): Текст

Фильтры (все необязательные, маски поддерживают * и ? , регистронезависимы):
  - author_ids     — брать embed'ы только от этих ботов/юзеров;
  - name_whitelist — если задан, имя должно совпасть хотя бы с одной маской;
  - name_blacklist — если имя совпало с маской — пропустить;
  - text_blacklist — если текст совпал с маской — пропустить (отсев служебных).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import translate
from zoneinfo import ZoneInfo

import discord


def _parse_id_list(raw: str | None) -> set[int]:
    if not raw:
        return set()
    return {int(p.strip()) for p in re.split(r"[\n,]", raw) if p.strip()}


def _parse_patterns(raw: str | None) -> list[str]:
    """Разбивает список масок по переводам строк и запятым."""
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[\n,]", raw) if p.strip()]


def _compile_masks(patterns: list[str]) -> list[re.Pattern]:
    """Компилирует маски (fnmatch: * и ?) в регистронезависимые регулярки."""
    return [re.compile(translate(p), re.IGNORECASE) for p in patterns if p]


def _match_any(value: str, patterns: list[re.Pattern]) -> bool:
    return any(p.match(value) for p in patterns)


@dataclass
class ScrapeConfig:
    """Настройки скрэппинга (что считать персонажем и как форматировать)."""

    author_ids: set[int] = field(default_factory=set)
    name_whitelist: list[re.Pattern] = field(default_factory=list)
    name_blacklist: list[re.Pattern] = field(default_factory=list)
    text_blacklist: list[re.Pattern] = field(default_factory=list)
    timezone: ZoneInfo | None = None
    time_format: str = "%Y-%m-%d %H:%M:%S"

    @classmethod
    def build(
        cls,
        *,
        author_ids: str | None = None,
        name_whitelist: str | None = None,
        name_blacklist: str | None = None,
        text_blacklist: str | None = None,
        timezone: str | None = None,
        time_format: str | None = None,
    ) -> "ScrapeConfig":
        tz = (timezone or "").strip()
        return cls(
            author_ids=_parse_id_list(author_ids),
            name_whitelist=_compile_masks(_parse_patterns(name_whitelist)),
            name_blacklist=_compile_masks(_parse_patterns(name_blacklist)),
            text_blacklist=_compile_masks(_parse_patterns(text_blacklist)),
            timezone=ZoneInfo(tz) if tz else None,
            time_format=(time_format or "").strip() or "%Y-%m-%d %H:%M:%S",
        )

    @classmethod
    def from_env(cls) -> "ScrapeConfig":
        return cls.build(
            author_ids=os.getenv("AUTHOR_IDS"),
            name_whitelist=os.getenv("CHARACTER_NAMES"),
            name_blacklist=os.getenv("NAME_BLACKLIST"),
            text_blacklist=os.getenv("TEXT_BLACKLIST"),
            timezone=os.getenv("TIMEZONE"),
            time_format=os.getenv("TIME_FORMAT"),
        )


@dataclass
class ScrapeResult:
    path: str
    lines: int
    messages_seen: int


# --- Отбор и форматирование --------------------------------------------------

def is_character(name: str | None, description: str | None, cfg: ScrapeConfig) -> bool:
    """Проходит ли embed под критерии реплики персонажа (с учётом фильтров)."""
    if not name or not description:
        return False
    name = name.strip()
    text = description.strip()
    if not name or not text:
        return False
    if cfg.name_whitelist and not _match_any(name, cfg.name_whitelist):
        return False
    if cfg.name_blacklist and _match_any(name, cfg.name_blacklist):
        return False
    if cfg.text_blacklist and _match_any(text, cfg.text_blacklist):
        return False
    return True


def format_timestamp(created_at: datetime, cfg: ScrapeConfig) -> str:
    # created_at — timezone-aware (обычно UTC).
    local = created_at.astimezone(cfg.timezone)  # None => локальная зона машины
    return local.strftime(cfg.time_format)


def format_line(
    name: str, description: str, created_at: datetime, cfg: ScrapeConfig
) -> str:
    """Строка результата: [дата-время] (Имя): Текст"""
    ts = format_timestamp(created_at, cfg)
    return f"[{ts}] ({name.strip()}): {description.strip()}\n"


def embed_char_name(embed: discord.Embed) -> str | None:
    """Имя персонажа: сначала embed.author.name, затем embed.title (фолбэк)."""
    author = getattr(embed, "author", None)
    name = getattr(author, "name", None) if author else None
    return name or embed.title


def is_character_embed(embed: discord.Embed, cfg: ScrapeConfig) -> bool:
    return is_character(embed_char_name(embed), embed.description, cfg)


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
                name = embed_char_name(embed)
                if not is_character(name, embed.description, cfg):
                    continue
                out.write(format_line(name, embed.description, message.created_at, cfg))
                lines += 1

            if progress and messages_seen % 1000 == 0:
                await progress(messages_seen, lines)

    return ScrapeResult(path=out_path, lines=lines, messages_seen=messages_seen)
