"""
Ядро скрэппинга: логика, общая для CLI, Discord-бота и веб-приложения.

Реплика персонажа = embed, у которого есть имя (embed.author.name или, как
фолбэк, embed.title) и текст (embed.description), и который проходит фильтры.

Фильтры (все необязательные):
  - author_ids     — брать embed'ы только от этих ботов/юзеров;
  - name_whitelist — маски (* ?), имя должно совпасть хотя бы с одной;
  - name_blacklist — маски (* ?), совпало — пропускаем;
  - text_contains  — подстроки: текст содержит любую → пропускаем (регистр не важен);
  - text_masks     — маски (* ?) по всей строке текста;
  - text_fuzzy     — примеры служебных сообщений: текст «похож» (>= порога) → пропуск.

Формат вывода — txt или obsidian (имена как [[Вики-ссылки]]).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from fnmatch import translate
from zoneinfo import ZoneInfo

import discord

DEFAULT_FUZZY_THRESHOLD = 0.82


def _parse_id_list(raw: str | None) -> set[int]:
    if not raw:
        return set()
    return {int(p.strip()) for p in re.split(r"[\n,]", raw) if p.strip()}


def _parse_lines(raw: str | None) -> list[str]:
    """Разбивает список по переводам строк и запятым."""
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[\n,]", raw) if p.strip()]


def _compile_masks(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(translate(p), re.IGNORECASE) for p in patterns if p]


def _match_any(value: str, patterns: list[re.Pattern]) -> bool:
    return any(p.match(value) for p in patterns)


def _normalize(text: str) -> str:
    """Нормализация для нечёткого сравнения: нижний регистр, без цифр/пунктуации."""
    text = text.lower()
    text = re.sub(r"\d+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _parse_tokens(raw: str | None) -> list[str]:
    """Список коротких токенов (префиксов) по пробелам/запятым/строкам."""
    if not raw:
        return []
    return [p for p in re.split(r"[\s,]+", raw.strip()) if p]


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "да")


def _compile_name_templates(lines: list[str]) -> list[re.Pattern]:
    """Шаблоны с {name}/{text} -> регулярки с именованными группами.
    Пример: `({name}) {text}` ловит «(Джон Фрай) Привет»."""
    patterns: list[re.Pattern] = []
    for line in lines:
        if "{name}" not in line:
            continue
        rx = re.escape(line)
        rx = rx.replace(r"\{name\}", r"(?P<name>.+?)")
        if r"\{text\}" in rx:
            rx = rx.replace(r"\{text\}", r"(?P<text>.*)")
        else:
            rx += r"(?P<text>.*)"  # если {text} не указан — остаток строки
        patterns.append(re.compile(r"^\s*" + rx, re.IGNORECASE | re.DOTALL))
    return patterns


@dataclass
class ScrapeConfig:
    """Настройки скрэппинга (что считать персонажем, фильтры, формат)."""

    author_ids: set[int] = field(default_factory=set)
    name_whitelist: list[re.Pattern] = field(default_factory=list)
    name_blacklist: list[re.Pattern] = field(default_factory=list)
    text_contains: list[str] = field(default_factory=list)      # уже в нижнем регистре
    text_masks: list[re.Pattern] = field(default_factory=list)
    text_fuzzy: list[str] = field(default_factory=list)         # нормализованные примеры
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD
    timezone: ZoneInfo | None = None
    time_format: str = "%Y-%m-%d %H:%M:%S"
    output_format: str = "obsidian"                             # "obsidian" | "txt"
    # --- режим сбора и настройки текстового режима ---
    mode: str = "both"                                          # "both" | "embeds" | "text"
    text_name_patterns: list[re.Pattern] = field(default_factory=list)
    text_fallback_nick: bool = False
    text_ignore_bots: bool = True
    text_command_prefixes: list[str] = field(default_factory=list)
    text_ooc_prefixes: list[str] = field(default_factory=list)

    @property
    def collect_embeds(self) -> bool:
        return self.mode in ("both", "embeds")

    @property
    def collect_text(self) -> bool:
        return self.mode in ("both", "text")

    @classmethod
    def build(
        cls,
        *,
        author_ids: str | None = None,
        name_whitelist: str | None = None,
        name_blacklist: str | None = None,
        text_contains: str | None = None,
        text_masks: str | None = None,
        text_fuzzy: str | None = None,
        fuzzy_threshold: str | float | None = None,
        timezone: str | None = None,
        time_format: str | None = None,
        output_format: str | None = None,
        mode: str | None = None,
        text_name_patterns: str | None = None,
        text_fallback_nick=None,
        text_ignore_bots=None,
        text_command_prefixes: str | None = None,
        text_ooc_prefixes: str | None = None,
    ) -> "ScrapeConfig":
        tz = (timezone or "").strip()
        try:
            thr = float(fuzzy_threshold) if fuzzy_threshold not in (None, "") else DEFAULT_FUZZY_THRESHOLD
        except (TypeError, ValueError):
            thr = DEFAULT_FUZZY_THRESHOLD
        return cls(
            author_ids=_parse_id_list(author_ids),
            name_whitelist=_compile_masks(_parse_lines(name_whitelist)),
            name_blacklist=_compile_masks(_parse_lines(name_blacklist)),
            text_contains=[s.lower() for s in _parse_lines(text_contains)],
            text_masks=_compile_masks(_parse_lines(text_masks)),
            text_fuzzy=[_normalize(s) for s in _parse_lines(text_fuzzy)],
            fuzzy_threshold=min(max(thr, 0.1), 1.0),
            timezone=ZoneInfo(tz) if tz else None,
            time_format=(time_format or "").strip() or "%Y-%m-%d %H:%M:%S",
            output_format=(output_format or "obsidian").strip().lower(),
            mode=(mode or "both").strip().lower(),
            text_name_patterns=_compile_name_templates(_parse_lines(text_name_patterns)),
            text_fallback_nick=_as_bool(text_fallback_nick),
            text_ignore_bots=_as_bool(text_ignore_bots) if text_ignore_bots is not None else True,
            text_command_prefixes=_parse_tokens(text_command_prefixes),
            text_ooc_prefixes=_parse_tokens(text_ooc_prefixes),
        )

    @classmethod
    def from_env(cls) -> "ScrapeConfig":
        return cls.build(
            author_ids=os.getenv("AUTHOR_IDS"),
            name_whitelist=os.getenv("CHARACTER_NAMES"),
            name_blacklist=os.getenv("NAME_BLACKLIST"),
            text_contains=os.getenv("TEXT_CONTAINS"),
            text_masks=os.getenv("TEXT_BLACKLIST"),
            text_fuzzy=os.getenv("TEXT_FUZZY"),
            fuzzy_threshold=os.getenv("FUZZY_THRESHOLD"),
            timezone=os.getenv("TIMEZONE"),
            time_format=os.getenv("TIME_FORMAT"),
            output_format=os.getenv("OUTPUT_FORMAT"),
            mode=os.getenv("MODE"),
            text_name_patterns=os.getenv("TEXT_NAME_PATTERNS"),
            text_fallback_nick=os.getenv("TEXT_FALLBACK_NICK"),
            text_ignore_bots=os.getenv("TEXT_IGNORE_BOTS"),
            text_command_prefixes=os.getenv("TEXT_COMMAND_PREFIXES"),
            text_ooc_prefixes=os.getenv("TEXT_OOC_PREFIXES"),
        )


@dataclass
class ScrapeResult:
    path: str
    lines: int
    messages_seen: int


# --- Отбор и форматирование --------------------------------------------------

def _fuzzy_hit(text: str, cfg: ScrapeConfig) -> bool:
    if not cfg.text_fuzzy:
        return False
    norm = _normalize(text)
    if not norm:
        return False
    return any(
        SequenceMatcher(None, norm, ex).ratio() >= cfg.fuzzy_threshold
        for ex in cfg.text_fuzzy
    )


def classify(
    name: str | None, description: str | None, cfg: ScrapeConfig
) -> tuple[bool, str]:
    """Вердикт по embed'у: (взято?, причина). Единый источник правды для
    is_character и предпросмотра."""
    if not name or not name.strip():
        return (False, "нет имени")
    if not description or not description.strip():
        return (False, "нет текста")
    name = name.strip()
    text = description.strip()
    if cfg.name_whitelist and not _match_any(name, cfg.name_whitelist):
        return (False, "не в белом списке имён")
    if cfg.name_blacklist and _match_any(name, cfg.name_blacklist):
        return (False, "имя в чёрном списке")
    low = text.lower()
    if cfg.text_contains and any(sub in low for sub in cfg.text_contains):
        return (False, "текст: содержит стоп-слово")
    if cfg.text_masks and _match_any(text, cfg.text_masks):
        return (False, "текст: маска")
    if _fuzzy_hit(text, cfg):
        return (False, "текст: похоже на служебное")
    return (True, "ок")


def is_character(name: str | None, description: str | None, cfg: ScrapeConfig) -> bool:
    return classify(name, description, cfg)[0]


def text_display_name(author: dict | None) -> str:
    a = author or {}
    return (a.get("global_name") or a.get("username") or "").strip()


def extract_text_reply(
    content: str | None, cfg: ScrapeConfig, author: dict | None = None
) -> tuple[str | None, str | None, str | None]:
    """
    Разбор обычного (не-embed) сообщения в режиме «Текст».
    Возвращает (имя, текст, причина_отбраковки|None). None в причине = взято.
    Имя ищется по шаблонам {name}/{text}; при отсутствии — опц. по имени автора.
    """
    if not content or not content.strip():
        return (None, None, "пустой текст")
    if cfg.text_ignore_bots and (author or {}).get("bot"):
        return (None, None, "бот")
    stripped = content.lstrip()
    if any(stripped.startswith(p) for p in cfg.text_ooc_prefixes):
        return (None, None, "OOC")
    if any(stripped.startswith(p) for p in cfg.text_command_prefixes):
        return (None, None, "команда")

    name: str | None = None
    text = content.strip()
    for pat in cfg.text_name_patterns:
        m = pat.match(content)
        if m:
            name = (m.group("name") or "").strip()
            text = (m.group("text") or "").strip()
            break
    if name is None:
        if cfg.text_fallback_nick:
            name = text_display_name(author)
            text = content.strip()
        else:
            return (None, None, "нет маски имени")

    ok, reason = classify(name, text, cfg)
    return (name, text, None if ok else reason)


def format_timestamp(created_at: datetime, cfg: ScrapeConfig) -> str:
    local = created_at.astimezone(cfg.timezone)  # None => локальная зона машины
    return local.strftime(cfg.time_format)


def _wikilink(name: str) -> str:
    """Экранирует имя под Obsidian-вики-ссылку [[...]]."""
    n = name.strip().replace("[", "(").replace("]", ")").replace("|", "/")
    n = n.replace("#", "").replace("^", "")
    return re.sub(r"\s+", " ", n).strip()


def format_speaker(name: str, cfg: ScrapeConfig) -> str:
    if cfg.output_format == "obsidian":
        return f"[[{_wikilink(name)}]]"
    return name.strip()


def format_line(
    name: str, description: str, created_at: datetime, cfg: ScrapeConfig
) -> str:
    """Строка результата: [дата-время] (Имя): Текст"""
    ts = format_timestamp(created_at, cfg)
    return f"[{ts}] ({format_speaker(name, cfg)}): {description.strip()}\n"


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
    """Проходит историю канала (для CLI/бота), пишет персонажные реплики."""
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
