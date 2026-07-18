"""
Серверное хранилище настроек (JSON-файл на диске).

Секреты (токен бота, app-password Nextcloud) хранятся здесь и НИКОГДА не
возвращаются в браузер — наружу отдаётся только `public_view()` с флагами
«задано / не задано». Файл кладётся в каталог данных (по умолчанию ./data),
который в Docker монтируется томом.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
CONFIG_FILE = DATA_DIR / "config.json"

_lock = Lock()

# Ключи, которые считаются секретами и не отдаются наружу как значения.
_SECRET_KEYS = {"discord_token", "nextcloud_app_password"}

_DEFAULTS: dict = {
    "discord_token": "",
    "guild_id": "",
    "nextcloud_url": "",
    "nextcloud_user": "",
    "nextcloud_app_password": "",
    "nextcloud_dir": "discord-scrapes",
    # значения по умолчанию для формы скрэппинга
    "author_ids": "",
    "character_names": "",
    "timezone": "",
    "time_format": "%Y-%m-%d %H:%M:%S",
}


def load() -> dict:
    with _lock:
        if not CONFIG_FILE.exists():
            return dict(_DEFAULTS)
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    merged = dict(_DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in _DEFAULTS})
    return merged


def save(update: dict) -> dict:
    """Обновляет только известные ключи. Пустая строка секрета = не менять его."""
    current = load()
    for key, value in update.items():
        if key not in _DEFAULTS:
            continue
        if key in _SECRET_KEYS and value == "":
            continue  # не затираем уже сохранённый секрет пустым полем
        current[key] = value
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        try:
            os.chmod(CONFIG_FILE, 0o600)  # секреты — только владельцу
        except OSError:
            pass
    return public_view(current)


def public_view(cfg: dict | None = None) -> dict:
    """Безопасное представление для UI: секреты заменены флагами *_set."""
    cfg = cfg or load()
    view = {k: v for k, v in cfg.items() if k not in _SECRET_KEYS}
    for key in _SECRET_KEYS:
        view[f"{key}_set"] = bool(cfg.get(key))
    return view
