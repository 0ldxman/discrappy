"""
Хранилище собранных сообщений (SQLite в каталоге данных).

Каждый скрэппинг — это «прогон» (таблица `runs`). Сообщения (`messages`)
ссылаются на прогон и соответствуют запрошенной таблице-логу:

    Чат (chat_id / chat_name) · Дата-время (ts, реальная, в UTC ISO) ·
    Автор (author) · Сообщение (content).

Дата-время хранится нормализованной в UTC (offset +00:00), чтобы диапазонные
фильтры работали простым лексикографическим сравнением строк; в нужный
часовой пояс значение переводится уже на этапе экспорта/отдачи в UI.

БД лежит в DATA_DIR/discrapp.db и переживает пересборку контейнера (том /data).
Секретов здесь нет — только собранный контент.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DB_FILE = DATA_DIR / "discrapp.db"

_lock = Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    """Ленивое подключение (один коннект на процесс, uvicorn — один воркер)."""
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # Регистрозависимость LIKE в SQLite ASCII-only; для кириллицы даём свой lower.
        conn.create_function("lower_u", 1, lambda s: s.lower() if s else s)
        _init_schema(conn)
        _conn = conn
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id          TEXT PRIMARY KEY,
            created_at  TEXT NOT NULL,
            guild_id    TEXT,
            channels    TEXT,             -- JSON: [{"id":..., "name":...}]
            params      TEXT,             -- JSON параметров скрэппинга
            status      TEXT DEFAULT 'running',   -- running|done|stopped|error
            title       TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            chat_id         TEXT,
            chat_name       TEXT,
            ts              TEXT,          -- реальная дата-время, UTC ISO
            author          TEXT,
            author_id       TEXT,
            content         TEXT,
            kind            TEXT,          -- 'embed' | 'text'
            discord_msg_id  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_msg_run    ON messages(run_id);
        CREATE INDEX IF NOT EXISTS idx_msg_author ON messages(run_id, author);
        CREATE INDEX IF NOT EXISTS idx_msg_ts     ON messages(run_id, ts);
        """
    )
    conn.commit()


def _utc_iso(dt: datetime) -> str:
    """Нормализует дату-время в UTC ISO (offset +00:00) для хранения."""
    if dt.tzinfo is None:
        dt = dt.astimezone()  # наивное — считаем локальным временем машины
    return dt.astimezone(timezone.utc).isoformat()


# ------------------------------- Прогоны -------------------------------------

def create_run(run_id: str, *, guild_id: str = "", channels: list[dict] | None = None,
               params: dict | None = None, title: str = "") -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO runs (id, created_at, guild_id, channels, params, status, title) "
            "VALUES (?,?,?,?,?, 'running', ?)",
            (run_id, datetime.now(timezone.utc).isoformat(), str(guild_id or ""),
             json.dumps(channels or [], ensure_ascii=False),
             json.dumps(params or {}, ensure_ascii=False), title or ""),
        )
        conn.commit()


def set_run_status(run_id: str, status: str) -> None:
    with _lock:
        conn = _connect()
        conn.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
        conn.commit()


def list_runs() -> list[dict]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT r.*, "
            "(SELECT COUNT(*) FROM messages m WHERE m.run_id = r.id) AS message_count "
            "FROM runs r ORDER BY r.created_at DESC"
        ).fetchall()
    return [_run_dict(r) for r in rows]


def get_run(run_id: str) -> dict | None:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT r.*, "
            "(SELECT COUNT(*) FROM messages m WHERE m.run_id = r.id) AS message_count "
            "FROM runs r WHERE r.id=?", (run_id,)
        ).fetchone()
    return _run_dict(row) if row else None


def delete_run(run_id: str) -> None:
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM runs WHERE id=?", (run_id,))  # messages — по каскаду
        conn.commit()


def _run_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "created_at": r["created_at"],
        "guild_id": r["guild_id"],
        "channels": json.loads(r["channels"] or "[]"),
        "params": json.loads(r["params"] or "{}"),
        "status": r["status"],
        "title": r["title"],
        "message_count": r["message_count"] if "message_count" in r.keys() else None,
    }


# ------------------------------- Сообщения -----------------------------------

def add_message(run_id: str, *, chat_id: str, chat_name: str, created_at: datetime,
                author: str, author_id: str, content: str, kind: str,
                discord_msg_id: str = "") -> int:
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO messages "
            "(run_id, chat_id, chat_name, ts, author, author_id, content, kind, discord_msg_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, str(chat_id), chat_name, _utc_iso(created_at), author.strip(),
             str(author_id or ""), content.strip(), kind, str(discord_msg_id or "")),
        )
        conn.commit()
        return int(cur.lastrowid)


def _filter_sql(author: str | None, after: str | None, before: str | None,
                q: str | None) -> tuple[str, list]:
    """Строит WHERE-хвост и параметры из фильтров (author/время/текст)."""
    where = ["run_id = ?"]
    args: list = []  # run_id подставляется вызывающим первым
    if author:
        where.append("author = ?")
        args.append(author)
    if after:
        where.append("ts >= ?")
        args.append(_utc_iso(datetime.fromisoformat(after)))
    if before:
        where.append("ts < ?")
        args.append(_utc_iso(datetime.fromisoformat(before)))
    if q:
        where.append("lower_u(content) LIKE lower_u(?)")
        args.append(f"%{q}%")
    return " AND ".join(where), args


_SORT_COLUMNS = {"ts": "ts", "author": "author", "chat": "chat_name", "id": "id"}


def query_messages(run_id: str, *, author: str | None = None, after: str | None = None,
                   before: str | None = None, q: str | None = None,
                   sort: str = "ts", order: str = "asc",
                   limit: int = 100, offset: int = 0) -> dict:
    """Отфильтрованная страница сообщений + общее число под фильтром."""
    col = _SORT_COLUMNS.get(sort, "ts")
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    tail, args = _filter_sql(author, after, before, q)
    with _lock:
        conn = _connect()
        total = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE {tail}", [run_id, *args]
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM messages WHERE {tail} "
            f"ORDER BY {col} {direction}, id {direction} LIMIT ? OFFSET ?",
            [run_id, *args, int(limit), int(offset)],
        ).fetchall()
    return {"total": total, "items": [_msg_dict(r) for r in rows]}


def list_authors(run_id: str) -> list[dict]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT author, COUNT(*) AS n FROM messages WHERE run_id=? "
            "GROUP BY author ORDER BY n DESC, author ASC", (run_id,)
        ).fetchall()
    return [{"author": r["author"], "count": r["n"]} for r in rows]


def update_message(msg_id: int, *, author: str | None = None,
                   content: str | None = None) -> bool:
    sets, args = [], []
    if author is not None:
        sets.append("author = ?")
        args.append(author.strip())
    if content is not None:
        sets.append("content = ?")
        args.append(content)
    if not sets:
        return False
    args.append(int(msg_id))
    with _lock:
        conn = _connect()
        cur = conn.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()
    return cur.rowcount > 0


def delete_message(msg_id: int) -> bool:
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM messages WHERE id=?", (int(msg_id),))
        conn.commit()
    return cur.rowcount > 0


def delete_messages(run_id: str, *, ids: list[int] | None = None,
                    author: str | None = None) -> int:
    """Массовое удаление: по списку id и/или по автору (в пределах прогона)."""
    if not ids and not author:
        return 0
    where = ["run_id = ?"]
    args: list = [run_id]
    if ids:
        where.append(f"id IN ({','.join('?' * len(ids))})")
        args.extend(int(i) for i in ids)
    if author:
        where.append("author = ?")
        args.append(author)
    with _lock:
        conn = _connect()
        cur = conn.execute(
            f"DELETE FROM messages WHERE {' AND '.join(where)}", args
        )
        conn.commit()
    return cur.rowcount


def rename_author(run_id: str, old: str, new: str) -> int:
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "UPDATE messages SET author=? WHERE run_id=? AND author=?",
            (new.strip(), run_id, old),
        )
        conn.commit()
    return cur.rowcount


def iter_run_messages(run_id: str) -> list[dict]:
    """Все сообщения прогона в порядке чат → время → id (для экспорта)."""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM messages WHERE run_id=? ORDER BY chat_name, ts, id",
            (run_id,),
        ).fetchall()
    return [_msg_dict(r) for r in rows]


def _msg_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "run_id": r["run_id"],
        "chat_id": r["chat_id"],
        "chat_name": r["chat_name"],
        "ts": r["ts"],
        "author": r["author"],
        "author_id": r["author_id"],
        "content": r["content"],
        "kind": r["kind"],
        "discord_msg_id": r["discord_msg_id"],
    }
