"""
Сборка выходного документа из строк БД (после правок в таблице-логе).

Форматы:
  - txt       — «[дата] (Имя): текст», заголовки-разделители по каналам;
  - obsidian  — то же + YAML-frontmatter, `## #канал`, имена как [[вики-ссылки]],
                и блок «Персонажи» со списком ссылок;
  - story     — чистое повествование: без отметок времени, сцены как заголовки,
                действия курсивом, реплики — «**Имя.** текст», OOC отброшен;
  - csv       — колонки chat_id, chat_name, ts, author, content, role…;
  - json      — массив объектов сообщений.

Строки идут в порядке повествования (`seq`), скрытые из экспорта исключены
вызывающим. Сообщение с непустым `scene_title` открывает новую сцену —
заголовок печатается перед ним.

Дата-время в БД хранится в UTC; здесь переводится в cfg.timezone и формат cfg.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime

import core
import narrative

# Соответствие формата расширению и MIME-типу.
FORMATS = {
    "txt": (".txt", "text/plain; charset=utf-8"),
    "obsidian": (".md", "text/markdown; charset=utf-8"),
    "story": (".md", "text/markdown; charset=utf-8"),
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


def _scene(row: dict) -> str:
    return (row.get("scene_title") or "").strip()


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
            title = _scene(row)
            if title:
                out.write(f"\n### {title}\n\n" if obsidian
                          else f"\n----- {title} -----\n")
            out.write(core.format_line(row["author"], row["content"], _dt(row), cfg))
            characters.add((row["author"] or "").strip())

    if obsidian and characters:
        out.write("\n## Персонажи\n\n")
        for name in sorted(characters):
            out.write(f"- [[{core._wikilink(name)}]]\n")

    return out.getvalue()


def _story_document(rows: list[dict], cfg: core.ScrapeConfig,
                    run: dict | None) -> str:
    """
    Читаемый текст без служебной шелухи: сцены заголовками, действия курсивом,
    реплики с именем говорящего. Даты не печатаются — они в таблице-логе.
    """
    out = io.StringIO()
    title = (run or {}).get("title") or "Хроника"
    out.write(f"# {title}\n\n")

    multi_chat = len({r.get("chat_name") for r in rows}) > 1
    current_chat: str | None = None
    last_speaker: str | None = None
    wrote_body = False  # чтобы заголовок сразу после шапки не отбивался пустой строкой

    def heading(level: int, text: str) -> None:
        nonlocal last_speaker
        if wrote_body:
            out.write("\n")
        out.write("#" * level + f" {text}\n\n")
        last_speaker = None

    for row in rows:
        role = row.get("role") or narrative.detect_role(row.get("content") or "")
        if role == narrative.ROLE_OOC:
            continue

        chat = row.get("chat_name") or ""
        if multi_chat and chat != current_chat:
            heading(2, f"#{chat}")
            current_chat = chat

        scene = _scene(row)
        if scene:
            heading(3, scene)

        author = (row.get("author") or "").strip()
        text = (row.get("content") or "").strip()
        if not text:
            continue

        if role == narrative.ROLE_ACTION:
            out.write(f"*{narrative.strip_action_marks(text)}*\n\n")
            last_speaker = None
        elif role == narrative.ROLE_NARRATION:
            out.write(f"{text}\n\n")
            last_speaker = None
        else:  # речь: имя не повторяем, если говорит тот же персонаж подряд
            prefix = "" if author == last_speaker else f"**{author}.** "
            out.write(f"{prefix}{text}\n\n")
            last_speaker = author
        wrote_body = True

    return out.getvalue()


def _csv_document(rows: list[dict], cfg: core.ScrapeConfig) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["chat_id", "chat_name", "datetime", "author", "message",
                     "role", "scene"])
    for row in rows:
        writer.writerow([
            row["chat_id"], row["chat_name"],
            core.format_timestamp(_dt(row), cfg),
            row["author"], row["content"],
            row.get("role") or "", _scene(row),
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
            "role": row.get("role") or "",
            "scene": _scene(row),
            "note": row.get("note") or "",
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
    if fmt == "story":
        return _story_document(rows, cfg, run)
    return _text_document(rows, cfg, obsidian=(fmt == "obsidian"), run=run)
