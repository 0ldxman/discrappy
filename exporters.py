"""
Сборка выходного документа из строк БД (после правок в таблице-логе).

Форматы:
  - txt       — «[дата] (Имя): текст», заголовки-разделители по каналам;
  - obsidian  — то же + YAML-frontmatter, `## #канал`, имена как [[вики-ссылки]],
                и блок «Персонажи» со списком ссылок;
  - csv       — колонки chat_id, chat_name, ts, author, content;
  - json      — массив объектов сообщений.

Дата-время в БД хранится в UTC; здесь переводится в cfg.timezone и формат cfg.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime

import core

# Соответствие формата расширению и MIME-типу.
FORMATS = {
    "txt": (".txt", "text/plain; charset=utf-8"),
    "obsidian": (".md", "text/markdown; charset=utf-8"),
    "csv": (".csv", "text/csv; charset=utf-8"),
    "json": (".json", "application/json; charset=utf-8"),
}


def _by_chat(rows: list[dict]):
    """Группирует строки по каналу, сохраняя порядок (rows уже отсортированы)."""
    groups: list[tuple[str, list[dict]]] = []
    for row in rows:
        name = row.get("chat_name") or row.get("chat_id") or ""
        if not groups or groups[-1][0] != name:
            groups.append((name, []))
        groups[-1][1].append(row)
    return groups


def _dt(row: dict) -> datetime:
    return datetime.fromisoformat(row["ts"])


def _text_document(rows: list[dict], cfg: core.ScrapeConfig, *,
                   obsidian: bool, run: dict | None) -> str:
    cfg.output_format = "obsidian" if obsidian else "txt"
    out = io.StringIO()
    characters: set[str] = set()

    if obsidian:
        chans = ", ".join(f'"{c.get("name", c.get("id"))}"'
                          for c in (run or {}).get("channels", []))
        out.write(
            "---\n"
            f"scraped: {datetime.now().isoformat(timespec='seconds')}\n"
            "source: discord\n"
            f"channels: [{chans}]\n"
            "tags: [rp-scrape]\n"
            "---\n\n"
        )

    for chat_name, group in _by_chat(rows):
        out.write(f"\n## #{chat_name}\n\n" if obsidian
                  else f"# ===== {chat_name} =====\n")
        for row in group:
            out.write(core.format_line(row["author"], row["content"], _dt(row), cfg))
            characters.add((row["author"] or "").strip())

    if obsidian and characters:
        out.write("\n## Персонажи\n\n")
        for name in sorted(characters):
            out.write(f"- [[{core._wikilink(name)}]]\n")

    return out.getvalue()


def _csv_document(rows: list[dict], cfg: core.ScrapeConfig) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["chat_id", "chat_name", "datetime", "author", "message"])
    for row in rows:
        writer.writerow([
            row["chat_id"], row["chat_name"],
            core.format_timestamp(_dt(row), cfg),
            row["author"], row["content"],
        ])
    return out.getvalue()


def _json_document(rows: list[dict], cfg: core.ScrapeConfig) -> str:
    payload = [
        {
            "chat_id": row["chat_id"],
            "chat_name": row["chat_name"],
            "datetime": core.format_timestamp(_dt(row), cfg),
            "author": row["author"],
            "message": row["content"],
            "kind": row["kind"],
        }
        for row in rows
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build(fmt: str, rows: list[dict], cfg: core.ScrapeConfig,
          *, run: dict | None = None) -> str:
    """Строит документ выбранного формата из строк БД."""
    if fmt == "csv":
        return _csv_document(rows, cfg)
    if fmt == "json":
        return _json_document(rows, cfg)
    return _text_document(rows, cfg, obsidian=(fmt == "obsidian"), run=run)
