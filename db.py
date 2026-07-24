"""
Хранилище собранных сообщений (SQLite в каталоге данных).

Каждый скрэппинг — это «прогон» (таблица `runs`). Сообщения (`messages`)
ссылаются на прогон и соответствуют запрошенной таблице-логу:

    Чат (chat_id / chat_name) · Дата-время (ts, реальная, в UTC ISO) ·
    Автор (author) · Сообщение (content).

Дата-время хранится нормализованной в UTC (offset +00:00), чтобы диапазонные
фильтры работали простым лексикографическим сравнением строк; в нужный
часовой пояс значение переводится уже на этапе экспорта/отдачи в UI.

Порядок повествования задаёт колонка `seq` (REAL), а не время: одно исходное
сообщение Discord может быть разрезано на несколько постов с одинаковым `ts`,
а вручную вставленная реплика вообще не привязана ко времени. Вставка между
соседями — среднее их `seq`; когда зазор исчерпан, прогон перенумеровывается.

Правки обратимы: каждая мутация пишет в `edits` снимки затронутых строк «до» и
«после», отмена/повтор просто восстанавливают нужный снимок.

БД лежит в DATA_DIR/discrapp.db и переживает пересборку контейнера (том /data).
Секретов здесь нет — только собранный контент.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DB_FILE = DATA_DIR / "discrapp.db"

SEQ_STEP = 1.0
# Минимальный зазор между соседями: ниже — двойная точность уже ненадёжна.
_MIN_GAP = 1e-9
# Сколько шагов отмены хранить на прогон.
HISTORY_LIMIT = 100

# Все колонки сообщения — снимок для журнала отмены должен быть полным.
_MSG_COLS = ("id", "run_id", "chat_id", "chat_name", "ts", "author", "author_id",
             "content", "kind", "discord_msg_id", "seq", "role", "hidden",
             "scene_title", "note")

_lock = Lock()
_conn: sqlite3.Connection | None = None
# Последний выданный seq на прогон — чтобы не спрашивать MAX(seq) на каждую вставку.
_seq_cache: dict[str, float] = {}


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
        CREATE TABLE IF NOT EXISTS edits (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            label       TEXT,
            before_json TEXT NOT NULL,     -- снимки затронутых строк до операции
            after_json  TEXT NOT NULL,     -- ... и после
            undone      INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_msg_run    ON messages(run_id);
        CREATE INDEX IF NOT EXISTS idx_msg_author ON messages(run_id, author);
        CREATE INDEX IF NOT EXISTS idx_msg_ts     ON messages(run_id, ts);
        CREATE INDEX IF NOT EXISTS idx_edit_run   ON edits(run_id, id);
        """
    )
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Досоздаёт колонки повествования в БД, созданной предыдущими версиями."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    added = {
        "seq": "ALTER TABLE messages ADD COLUMN seq REAL",
        "role": "ALTER TABLE messages ADD COLUMN role TEXT DEFAULT ''",
        "hidden": "ALTER TABLE messages ADD COLUMN hidden INTEGER DEFAULT 0",
        "scene_title": "ALTER TABLE messages ADD COLUMN scene_title TEXT DEFAULT ''",
        "note": "ALTER TABLE messages ADD COLUMN note TEXT DEFAULT ''",
    }
    for column, sql in added.items():
        if column not in have:
            conn.execute(sql)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_seq ON messages(run_id, seq)")

    # Первичная нумерация: сохраняем текущий видимый порядок (чат → время → id).
    pending = conn.execute(
        "SELECT DISTINCT run_id FROM messages WHERE seq IS NULL"
    ).fetchall()
    for row in pending:
        _renumber(conn, row["run_id"])


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
        _seq_cache.pop(run_id, None)


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
        conn.execute("DELETE FROM edits WHERE run_id=?", (run_id,))
        conn.commit()
        _seq_cache.pop(run_id, None)


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


# ------------------------- Порядок повествования (seq) -----------------------

def _renumber(conn: sqlite3.Connection, run_id: str) -> None:
    """Раздаёт seq = 1, 2, 3… в текущем видимом порядке. Порядок не меняет."""
    rows = conn.execute(
        "SELECT id FROM messages WHERE run_id=? "
        "ORDER BY (seq IS NULL), seq, chat_name, ts, id", (run_id,)
    ).fetchall()
    conn.executemany(
        "UPDATE messages SET seq=? WHERE id=?",
        [(float(i), r["id"]) for i, r in enumerate(rows, 1)],
    )
    _seq_cache[run_id] = float(len(rows))


def _next_seq(conn: sqlite3.Connection, run_id: str) -> float:
    """Seq для дописывания в конец прогона (путь скрэппинга — самый горячий)."""
    current = _seq_cache.get(run_id)
    if current is None:
        row = conn.execute(
            "SELECT MAX(seq) AS s FROM messages WHERE run_id=?", (run_id,)
        ).fetchone()
        current = float(row["s"] or 0.0)
    current += SEQ_STEP
    _seq_cache[run_id] = current
    return current


def _sibling_seq(conn: sqlite3.Connection, run_id: str, seq: float,
                 *, back: bool) -> float | None:
    """Seq ближайшего соседа по порядку (None — соседа нет)."""
    if back:
        sql = ("SELECT seq FROM messages WHERE run_id=? AND seq<? "
               "ORDER BY seq DESC LIMIT 1")
    else:
        sql = ("SELECT seq FROM messages WHERE run_id=? AND seq>? "
               "ORDER BY seq ASC LIMIT 1")
    row = conn.execute(sql, (run_id, seq)).fetchone()
    return float(row["seq"]) if row else None


def _try_slots(conn: sqlite3.Connection, run_id: str, seq: float, count: int,
               *, back: bool) -> list[float] | None:
    """`count` значений seq вплотную после (или до) якоря. None — зазор исчерпан."""
    if count <= 0:
        return []
    neighbour = _sibling_seq(conn, run_id, seq, back=back)
    sign = -1.0 if back else 1.0
    if neighbour is None:
        values = [seq + sign * SEQ_STEP * (i + 1) for i in range(count)]
    else:
        gap = abs(neighbour - seq)
        if gap <= _MIN_GAP * (count + 1):
            return None
        step = gap / (count + 1)
        values = [seq + sign * step * (i + 1) for i in range(count)]
    return list(reversed(values)) if back else values


def _slots(conn: sqlite3.Connection, run_id: str, anchor_id: int, count: int,
           *, back: bool) -> list[float]:
    """
    Свободные seq рядом с якорем, с перенумерацией прогона при исчерпании зазора.

    Перенумерация выполняется до снятия снимка для журнала — она не меняет
    порядок, поэтому в отмену её включать не нужно.
    """
    row = conn.execute("SELECT seq FROM messages WHERE id=?", (anchor_id,)).fetchone()
    if row is None:
        raise KeyError(anchor_id)
    values = _try_slots(conn, run_id, float(row["seq"]), count, back=back)
    if values is None:
        _renumber(conn, run_id)
        conn.commit()
        row = conn.execute("SELECT seq FROM messages WHERE id=?", (anchor_id,)).fetchone()
        values = _try_slots(conn, run_id, float(row["seq"]), count, back=back)
    return values or []


# --------------------------- Журнал отмены (undo) ----------------------------

def _snapshot(conn: sqlite3.Connection, ids) -> list[dict]:
    ids = [int(i) for i in ids]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM messages WHERE id IN ({placeholders})", ids
    ).fetchall()
    return [{c: r[c] for c in _MSG_COLS} for r in rows]


def _restore(conn: sqlite3.Connection, target: list[dict], other: list[dict]) -> None:
    """Приводит строки к снимку `target`; лишнее из `other` удаляет."""
    keep = {int(r["id"]) for r in target}
    for row in other:
        if int(row["id"]) not in keep:
            conn.execute("DELETE FROM messages WHERE id=?", (int(row["id"]),))
    columns = ",".join(_MSG_COLS)
    placeholders = ",".join("?" * len(_MSG_COLS))
    conn.executemany(
        f"INSERT OR REPLACE INTO messages ({columns}) VALUES ({placeholders})",
        [[row.get(c) for c in _MSG_COLS] for row in target],
    )


@contextmanager
def _journal(conn: sqlite3.Connection, run_id: str, label: str, ids):
    """
    Оборачивает мутацию в запись для отмены.

    В `yield`-значение (множество id) мутирующий код доливает идентификаторы
    созданных строк — снимок «после» снимается по объединению.
    """
    ids = {int(i) for i in ids}
    before = _snapshot(conn, ids)
    touched = set(ids)
    try:
        yield touched
    except Exception:
        conn.rollback()  # незавершённая мутация не должна утечь в следующий commit
        raise
    after = _snapshot(conn, touched)
    if not before and not after:
        return
    conn.execute("DELETE FROM edits WHERE run_id=? AND undone=1", (run_id,))
    conn.execute(
        "INSERT INTO edits (run_id, created_at, label, before_json, after_json) "
        "VALUES (?,?,?,?,?)",
        (run_id, datetime.now(timezone.utc).isoformat(), label,
         json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False)),
    )
    conn.execute(
        "DELETE FROM edits WHERE run_id=? AND id NOT IN "
        "(SELECT id FROM edits WHERE run_id=? ORDER BY id DESC LIMIT ?)",
        (run_id, run_id, HISTORY_LIMIT),
    )


def undo(run_id: str) -> str | None:
    """Откатывает последнюю правку. → её описание, либо None если откатывать нечего."""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM edits WHERE run_id=? AND undone=0 ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        _restore(conn, json.loads(row["before_json"]), json.loads(row["after_json"]))
        conn.execute("UPDATE edits SET undone=1 WHERE id=?", (row["id"],))
        conn.commit()
        _seq_cache.pop(run_id, None)
        return row["label"]


def redo(run_id: str) -> str | None:
    """Повторяет ближайшую отменённую правку. → её описание либо None."""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM edits WHERE run_id=? AND undone=1 ORDER BY id ASC LIMIT 1",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        _restore(conn, json.loads(row["after_json"]), json.loads(row["before_json"]))
        conn.execute("UPDATE edits SET undone=0 WHERE id=?", (row["id"],))
        conn.commit()
        _seq_cache.pop(run_id, None)
        return row["label"]


def history(run_id: str, limit: int = 30) -> dict:
    """Последние правки прогона + доступность отмены/повтора."""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT id, created_at, label, undone FROM edits WHERE run_id=? "
            "ORDER BY id DESC LIMIT ?", (run_id, int(limit)),
        ).fetchall()
        can_undo = conn.execute(
            "SELECT label FROM edits WHERE run_id=? AND undone=0 ORDER BY id DESC LIMIT 1",
            (run_id,)).fetchone()
        can_redo = conn.execute(
            "SELECT label FROM edits WHERE run_id=? AND undone=1 ORDER BY id ASC LIMIT 1",
            (run_id,)).fetchone()
    return {
        "items": [{"id": r["id"], "created_at": r["created_at"], "label": r["label"],
                   "undone": bool(r["undone"])} for r in rows],
        "undo_label": can_undo["label"] if can_undo else None,
        "redo_label": can_redo["label"] if can_redo else None,
    }


# ------------------------------- Сообщения -----------------------------------

def add_message(run_id: str, *, chat_id: str, chat_name: str, created_at: datetime,
                author: str, author_id: str, content: str, kind: str,
                discord_msg_id: str = "", role: str = "") -> int:
    """Дописывает сообщение в конец прогона (используется скрэппингом)."""
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO messages "
            "(run_id, chat_id, chat_name, ts, author, author_id, content, kind, "
            " discord_msg_id, seq, role, hidden, scene_title, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,'','')",
            (run_id, str(chat_id), chat_name, _utc_iso(created_at), author.strip(),
             str(author_id or ""), content.strip(), kind, str(discord_msg_id or ""),
             _next_seq(conn, run_id), role),
        )
        conn.commit()
        return int(cur.lastrowid)


def _run_of(conn: sqlite3.Connection, msg_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM messages WHERE id=?", (int(msg_id),)).fetchone()


def _insert_row(conn: sqlite3.Connection, base: sqlite3.Row | dict, *, seq: float,
                content: str, author: str, role: str = "", scene_title: str = "") -> int:
    """Создаёт сообщение, наследуя чат/время/происхождение от образца."""
    cur = conn.execute(
        "INSERT INTO messages "
        "(run_id, chat_id, chat_name, ts, author, author_id, content, kind, "
        " discord_msg_id, seq, role, hidden, scene_title, note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,'')",
        (base["run_id"], base["chat_id"], base["chat_name"], base["ts"],
         (author or "").strip(), base["author_id"], content, base["kind"],
         base["discord_msg_id"], seq, role, scene_title),
    )
    return int(cur.lastrowid)


# ------------------------------- Выборка -------------------------------------

def _filter_sql(authors: list[str] | None, after: str | None, before: str | None,
                q: str | None, role: str | None, hidden: str | None,
                chat: str | None) -> tuple[str, list]:
    """Строит WHERE-хвост и параметры из фильтров таблицы-лога."""
    where = ["run_id = ?"]
    args: list = []  # run_id подставляется вызывающим первым
    if authors:
        where.append(f"author IN ({','.join('?' * len(authors))})")
        args.extend(authors)
    if chat:
        where.append("chat_name = ?")
        args.append(chat)
    if after:
        where.append("ts >= ?")
        args.append(_utc_iso(datetime.fromisoformat(after)))
    if before:
        where.append("ts < ?")
        args.append(_utc_iso(datetime.fromisoformat(before)))
    if q:
        where.append("lower_u(content) LIKE lower_u(?)")
        args.append(f"%{q}%")
    if role:
        where.append("COALESCE(role,'') = ?")
        args.append(role)
    if hidden == "only":
        where.append("hidden = 1")
    elif hidden != "all":  # по умолчанию скрытые не показываем
        where.append("COALESCE(hidden,0) = 0")
    return " AND ".join(where), args


_SORT_COLUMNS = {"seq": "seq", "ts": "ts", "author": "author",
                 "chat": "chat_name", "id": "id"}


def query_messages(run_id: str, *, authors: list[str] | None = None,
                   after: str | None = None, before: str | None = None,
                   q: str | None = None, role: str | None = None,
                   hidden: str | None = None, chat: str | None = None,
                   sort: str = "seq", order: str = "asc",
                   limit: int = 100, offset: int = 0) -> dict:
    """Отфильтрованная страница сообщений + общее число под фильтром."""
    col = _SORT_COLUMNS.get(sort, "seq")
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    tail, args = _filter_sql(authors, after, before, q, role, hidden, chat)
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


def get_message(msg_id: int) -> dict | None:
    with _lock:
        conn = _connect()
        row = _run_of(conn, msg_id)
    return _msg_dict(row) if row else None


def list_authors(run_id: str) -> list[dict]:
    """Персонажи прогона со счётчиками (сортировку выбирает UI)."""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT author, COUNT(*) AS n, "
            "       SUM(CASE WHEN COALESCE(hidden,0)=1 THEN 1 ELSE 0 END) AS hidden_n, "
            "       MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
            "       SUM(LENGTH(COALESCE(content,''))) AS chars "
            "FROM messages WHERE run_id=? GROUP BY author "
            "ORDER BY n DESC, author ASC", (run_id,)
        ).fetchall()
    return [{"author": r["author"], "count": r["n"], "hidden": r["hidden_n"] or 0,
             "first_ts": r["first_ts"], "last_ts": r["last_ts"],
             "chars": r["chars"] or 0} for r in rows]


def list_chats(run_id: str) -> list[dict]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT chat_name, COUNT(*) AS n FROM messages WHERE run_id=? "
            "GROUP BY chat_name ORDER BY chat_name", (run_id,)
        ).fetchall()
    return [{"chat_name": r["chat_name"], "count": r["n"]} for r in rows]


def list_scenes(run_id: str) -> list[dict]:
    """Сообщения, помеченные как начало сцены, в порядке повествования."""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT id, seq, scene_title, ts, chat_name FROM messages "
            "WHERE run_id=? AND COALESCE(scene_title,'') <> '' ORDER BY seq, id",
            (run_id,),
        ).fetchall()
    return [{"id": r["id"], "seq": r["seq"], "title": r["scene_title"],
             "ts": r["ts"], "chat_name": r["chat_name"]} for r in rows]


def iter_run_messages(run_id: str, *, include_hidden: bool = False) -> list[dict]:
    """Все сообщения прогона в порядке повествования (для экспорта)."""
    tail = "" if include_hidden else " AND COALESCE(hidden,0)=0"
    with _lock:
        conn = _connect()
        rows = conn.execute(
            f"SELECT * FROM messages WHERE run_id=?{tail} ORDER BY seq, id", (run_id,)
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
        "seq": r["seq"],
        "role": r["role"] or "",
        "hidden": bool(r["hidden"]),
        "scene_title": r["scene_title"] or "",
        "note": r["note"] or "",
    }


# ------------------------------ Правки строк ---------------------------------

_EDITABLE = {"author": "author", "content": "content", "role": "role",
             "scene_title": "scene_title", "note": "note", "hidden": "hidden"}


def update_message(msg_id: int, **fields) -> bool:
    """Точечная правка полей сообщения. Неизвестные поля игнорируются."""
    patch = {k: v for k, v in fields.items() if k in _EDITABLE and v is not None}
    if not patch:
        return False
    if "author" in patch:
        patch["author"] = str(patch["author"]).strip()
    if "hidden" in patch:
        patch["hidden"] = 1 if patch["hidden"] else 0
    with _lock:
        conn = _connect()
        row = _run_of(conn, msg_id)
        if row is None:
            return False
        label = "Правка сообщения"
        if set(patch) == {"hidden"}:
            label = "Скрытие сообщения" if patch["hidden"] else "Возврат сообщения"
        elif set(patch) == {"scene_title"}:
            label = "Заголовок сцены"
        with _journal(conn, row["run_id"], label, [msg_id]):
            conn.execute(
                f"UPDATE messages SET {', '.join(f'{k}=?' for k in patch)} WHERE id=?",
                [*patch.values(), int(msg_id)],
            )
        conn.commit()
    return True


def delete_message(msg_id: int) -> bool:
    with _lock:
        conn = _connect()
        row = _run_of(conn, msg_id)
        if row is None:
            return False
        with _journal(conn, row["run_id"], "Удаление сообщения", [msg_id]):
            conn.execute("DELETE FROM messages WHERE id=?", (int(msg_id),))
        conn.commit()
    return True


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
    tail = " AND ".join(where)
    with _lock:
        conn = _connect()
        victims = [r["id"] for r in
                   conn.execute(f"SELECT id FROM messages WHERE {tail}", args)]
        if not victims:
            return 0
        label = (f"Удаление сообщений автора «{author}»" if author
                 else f"Удаление сообщений ({len(victims)})")
        with _journal(conn, run_id, label, victims):
            conn.execute(f"DELETE FROM messages WHERE {tail}", args)
        conn.commit()
    return len(victims)


def rename_author(run_id: str, old: str, new: str) -> int:
    with _lock:
        conn = _connect()
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM messages WHERE run_id=? AND author=?", (run_id, old))]
        if not ids:
            return 0
        with _journal(conn, run_id, f"Переименование «{old}» → «{new}»", ids):
            conn.execute("UPDATE messages SET author=? WHERE run_id=? AND author=?",
                         (new.strip(), run_id, old))
        conn.commit()
    return len(ids)


def merge_authors(run_id: str, sources: list[str], target: str) -> int:
    """Сводит несколько написаний имени к одному каноническому."""
    sources = [s for s in sources if s and s != target]
    if not sources:
        return 0
    placeholders = ",".join("?" * len(sources))
    with _lock:
        conn = _connect()
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM messages WHERE run_id=? AND author IN ({placeholders})",
            [run_id, *sources])]
        if not ids:
            return 0
        label = f"Слияние персонажей ({len(sources)}) → «{target}»"
        with _journal(conn, run_id, label, ids):
            conn.execute(
                f"UPDATE messages SET author=? WHERE run_id=? AND author IN ({placeholders})",
                [target.strip(), run_id, *sources])
        conn.commit()
    return len(ids)


# --------------------- Вставка, разрезание, объединение ----------------------

def insert_message(run_id: str, *, after_id: int | None = None,
                   before_id: int | None = None, author: str = "",
                   content: str = "", role: str = "") -> dict | None:
    """
    Вставляет новое сообщение рядом с указанным (в порядке повествования).

    Чат, время и происхождение наследуются от соседа — новое сообщение
    считается отправленным «тогда же». Без соседа вставка идёт в конец прогона.
    """
    anchor_id = after_id if after_id is not None else before_id
    with _lock:
        conn = _connect()
        anchor = _run_of(conn, anchor_id) if anchor_id is not None else None
        if anchor_id is not None and anchor is None:
            return None
        if anchor is not None:
            seq = _slots(conn, anchor["run_id"], int(anchor["id"]), 1,
                         back=(after_id is None))[0]
            base = anchor
            run_id = anchor["run_id"]
        else:
            row = conn.execute(
                "SELECT * FROM messages WHERE run_id=? ORDER BY seq DESC, id DESC LIMIT 1",
                (run_id,)).fetchone()
            if row is None:
                return None  # пустой прогон: не от чего наследовать чат и время
            base, seq = row, _next_seq(conn, run_id)

        with _journal(conn, run_id, "Вставка сообщения", []) as touched:
            new_id = _insert_row(conn, base, seq=seq, content=content,
                                 author=author or base["author"], role=role)
            touched.add(new_id)
        conn.commit()
        return _msg_dict(_run_of(conn, new_id))


def split_message(msg_id: int, parts: list[dict], *, label: str = "Разрезание сообщения"
                  ) -> list[dict] | None:
    """
    Заменяет сообщение цепочкой фрагментов, идущих подряд и в том же порядке.

    `parts` — список словарей с ключами content / author / role. Первый
    фрагмент переиспользует исходную строку (сохраняя её id, время и пометки),
    остальные вставляются следом. Все получают то же время: это один и тот же
    момент разговора, просто разнесённый на отдельные посты.
    """
    parts = [p for p in parts if (p.get("content") or "").strip()]
    if len(parts) < 2:
        return None
    with _lock:
        conn = _connect()
        row = _run_of(conn, msg_id)
        if row is None:
            return None
        run_id = row["run_id"]
        slots = _slots(conn, run_id, int(msg_id), len(parts) - 1, back=False)
        with _journal(conn, run_id, label, [msg_id]) as touched:
            head = parts[0]
            conn.execute(
                "UPDATE messages SET content=?, author=?, role=? WHERE id=?",
                (head["content"].strip(), (head.get("author") or row["author"]).strip(),
                 head.get("role") or row["role"] or "", int(msg_id)),
            )
            for part, seq in zip(parts[1:], slots):
                touched.add(_insert_row(
                    conn, row, seq=seq, content=part["content"].strip(),
                    author=part.get("author") or row["author"],
                    role=part.get("role") or "",
                ))
        conn.commit()
        ids = sorted(touched)
        return [_msg_dict(r) for r in
                conn.execute(
                    f"SELECT * FROM messages WHERE id IN ({','.join('?' * len(ids))}) "
                    "ORDER BY seq, id", ids)]


def split_many(run_id: str, plan: list[tuple[int, list[dict]]], *, label: str) -> int:
    """
    Массовое разрезание: список (id, фрагменты). → сколько сообщений разрезано.

    Одна запись в журнале отмены на всю операцию — иначе откатывать
    авто-разделение сотни постов пришлось бы по одному.
    """
    plan = [(int(i), [p for p in parts if (p.get("content") or "").strip()])
            for i, parts in plan]
    plan = [(i, parts) for i, parts in plan if len(parts) >= 2]
    if not plan:
        return 0
    with _lock:
        conn = _connect()
        # Перенумеровываем заранее: иначе исчерпанный зазор вызвал бы commit
        # из середины журналируемой операции и закрепил бы половину работы.
        # После перенумерации между соседями ровно 1.0 — фрагментам хватит.
        _renumber(conn, run_id)
        conn.commit()
        with _journal(conn, run_id, label, [i for i, _ in plan]) as touched:
            for msg_id, parts in plan:
                row = _run_of(conn, msg_id)
                if row is None:
                    continue
                slots = _slots(conn, run_id, msg_id, len(parts) - 1, back=False)
                head = parts[0]
                conn.execute(
                    "UPDATE messages SET content=?, author=?, role=? WHERE id=?",
                    (head["content"].strip(),
                     (head.get("author") or row["author"]).strip(),
                     head.get("role") or row["role"] or "", msg_id),
                )
                for part, seq in zip(parts[1:], slots):
                    touched.add(_insert_row(
                        conn, row, seq=seq, content=part["content"].strip(),
                        author=part.get("author") or row["author"],
                        role=part.get("role") or "",
                    ))
        conn.commit()
    return len(plan)


def merge_messages(run_id: str, ids: list[int], *, separator: str = "\n") -> dict | None:
    """
    Склеивает сообщения в одно (порядок — по seq). Остаётся первое из них:
    его id, время и пометка сцены сохраняются, тексты дописываются следом.
    """
    ids = [int(i) for i in ids]
    if len(ids) < 2:
        return None
    placeholders = ",".join("?" * len(ids))
    with _lock:
        conn = _connect()
        rows = conn.execute(
            f"SELECT * FROM messages WHERE run_id=? AND id IN ({placeholders}) "
            "ORDER BY seq, id", [run_id, *ids]).fetchall()
        if len(rows) < 2:
            return None
        keeper = rows[0]
        merged = separator.join((r["content"] or "").strip() for r in rows
                                if (r["content"] or "").strip())
        victims = [int(r["id"]) for r in rows[1:]]
        label = f"Объединение сообщений ({len(rows)})"
        with _journal(conn, run_id, label, [int(keeper["id"]), *victims]):
            conn.execute("UPDATE messages SET content=? WHERE id=?",
                         (merged, int(keeper["id"])))
            conn.execute(
                f"DELETE FROM messages WHERE id IN ({','.join('?' * len(victims))})",
                victims)
        conn.commit()
        return _msg_dict(_run_of(conn, int(keeper["id"])))


def move_message(msg_id: int, *, after_id: int | None = None,
                 before_id: int | None = None, direction: str | None = None) -> bool:
    """
    Переставляет сообщение в порядке повествования.

    Либо явно (after_id / before_id), либо на шаг: direction = "up" | "down".
    """
    with _lock:
        conn = _connect()
        row = _run_of(conn, msg_id)
        if row is None:
            return False
        run_id, seq = row["run_id"], float(row["seq"])

        if direction in ("up", "down"):
            back = direction == "up"
            # Через соседа: встаём по ту сторону от него.
            neighbour = conn.execute(
                "SELECT id FROM messages WHERE run_id=? AND seq{} ? "
                "ORDER BY seq {} LIMIT 1".format("<" if back else ">",
                                                 "DESC" if back else "ASC"),
                (run_id, seq)).fetchone()
            if neighbour is None:
                return False
            anchor_id, back = int(neighbour["id"]), back
        elif after_id is not None:
            anchor_id, back = int(after_id), False
        elif before_id is not None:
            anchor_id, back = int(before_id), True
        else:
            return False
        if anchor_id == int(msg_id):
            return False

        new_seq = _slots(conn, run_id, anchor_id, 1, back=back)[0]
        with _journal(conn, run_id, "Перемещение сообщения", [msg_id]):
            conn.execute("UPDATE messages SET seq=? WHERE id=?", (new_seq, int(msg_id)))
        conn.commit()
    return True


def duplicate_message(msg_id: int) -> dict | None:
    """Копия сообщения сразу под оригиналом (заготовка для ручной правки)."""
    with _lock:
        conn = _connect()
        row = _run_of(conn, msg_id)
        if row is None:
            return None
        run_id = row["run_id"]
        seq = _slots(conn, run_id, int(msg_id), 1, back=False)[0]
        with _journal(conn, run_id, "Дублирование сообщения", []) as touched:
            new_id = _insert_row(conn, row, seq=seq, content=row["content"],
                                 author=row["author"], role=row["role"] or "")
            touched.add(new_id)
        conn.commit()
        return _msg_dict(_run_of(conn, new_id))


# --------------------------- Массовые операции -------------------------------

def apply_content_updates(run_id: str, updates: dict[int, str], *, label: str) -> int:
    """Записывает новые тексты пачкой (поиск-замена, чистка). → число строк."""
    updates = {int(k): v for k, v in updates.items()}
    if not updates:
        return 0
    with _lock:
        conn = _connect()
        with _journal(conn, run_id, label, updates.keys()):
            conn.executemany("UPDATE messages SET content=? WHERE id=? AND run_id=?",
                             [(v, k, run_id) for k, v in updates.items()])
        conn.commit()
    return len(updates)


def apply_field_updates(run_id: str, field: str, updates: dict[int, object], *,
                        label: str) -> int:
    """Пачечная правка одного поля разными значениями (роли, авторы)."""
    if field not in _EDITABLE or not updates:
        return 0
    updates = {int(k): v for k, v in updates.items()}
    with _lock:
        conn = _connect()
        with _journal(conn, run_id, label, updates.keys()):
            conn.executemany(
                f"UPDATE messages SET {field}=? WHERE id=? AND run_id=?",
                [(v, k, run_id) for k, v in updates.items()])
        conn.commit()
    return len(updates)


def bulk_set(run_id: str, ids: list[int], *, label: str, **fields) -> int:
    """Ставит одинаковые значения полей выбранным сообщениям."""
    patch = {k: v for k, v in fields.items() if k in _EDITABLE and v is not None}
    ids = [int(i) for i in ids]
    if not patch or not ids:
        return 0
    if "hidden" in patch:
        patch["hidden"] = 1 if patch["hidden"] else 0
    placeholders = ",".join("?" * len(ids))
    with _lock:
        conn = _connect()
        with _journal(conn, run_id, label, ids):
            conn.execute(
                f"UPDATE messages SET {', '.join(f'{k}=?' for k in patch)} "
                f"WHERE run_id=? AND id IN ({placeholders})",
                [*patch.values(), run_id, *ids])
        conn.commit()
    return len(ids)


def select_ids(run_id: str, *, ids: list[int] | None = None,
               authors: list[str] | None = None, q: str | None = None,
               role: str | None = None, chat: str | None = None,
               after: str | None = None, before: str | None = None,
               hidden: str | None = "all") -> list[dict]:
    """
    Строки под областью действия массовой операции.

    Если передан явный список id — берётся он; иначе применяются те же фильтры,
    что и в таблице, чтобы «заменить во всём, что сейчас на экране» работало
    предсказуемо.
    """
    if ids:
        ids = [int(i) for i in ids]
        placeholders = ",".join("?" * len(ids))
        sql = f"SELECT * FROM messages WHERE run_id=? AND id IN ({placeholders}) ORDER BY seq, id"
        args = [run_id, *ids]
    else:
        tail, extra = _filter_sql(authors, after, before, q, role, hidden, chat)
        sql = f"SELECT * FROM messages WHERE {tail} ORDER BY seq, id"
        args = [run_id, *extra]
    with _lock:
        conn = _connect()
        rows = conn.execute(sql, args).fetchall()
    return [_msg_dict(r) for r in rows]
