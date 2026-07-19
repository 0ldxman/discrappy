"""
Клиент Discord REST API для веб-приложения.

Почему REST, а не gateway (discord.py): веб-сервису удобнее делать запросы по
требованию и стримить историю постранично, не держа постоянное соединение.
Требуется бот с включённым Message Content Intent, добавленный на сервер.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

API = "https://discord.com/api/v10"
DISCORD_EPOCH = 1420070400000  # 2015-01-01, начало отсчёта снежинок Discord

# Типы каналов Discord, из которых имеет смысл читать историю сообщений.
# https://discord.com/developers/docs/resources/channel#channel-object-channel-types
TEXT_CHANNEL_TYPES = {
    0,   # GUILD_TEXT
    5,   # GUILD_ANNOUNCEMENT
    10,  # ANNOUNCEMENT_THREAD
    11,  # PUBLIC_THREAD  (в т.ч. посты форума)
    12,  # PRIVATE_THREAD
    2,   # GUILD_VOICE (встроенный чат)
    13,  # GUILD_STAGE_VOICE
}
FORUM_TYPES = {15, 16}  # GUILD_FORUM, GUILD_MEDIA — сами не читаемы, но содержат посты

CHANNEL_TYPE_NAMES = {
    0: "text", 2: "voice", 5: "announcement", 10: "announcement_thread",
    11: "thread", 12: "private_thread", 13: "stage", 15: "forum", 16: "media",
}


class DiscordError(RuntimeError):
    pass


@dataclass
class Channel:
    id: str
    name: str
    type: int
    parent_id: str | None = None

    @property
    def type_name(self) -> str:
        return CHANNEL_TYPE_NAMES.get(self.type, str(self.type))

    @property
    def is_thread(self) -> bool:
        return self.type in (10, 11, 12)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "type_name": self.type_name,
            "parent_id": self.parent_id,
            "is_thread": self.is_thread,
        }


def datetime_to_snowflake(dt: datetime) -> int:
    """Дата -> минимальная снежинка для этого момента (для параметров after/before)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)
    return (ms - DISCORD_EPOCH) << 22


def _headers(token: str) -> dict:
    return {"Authorization": f"Bot {token}", "User-Agent": "discrapp (httpx)"}


async def _get(client: httpx.AsyncClient, token: str, path: str, **params) -> object:
    """GET с обработкой rate-limit (429) и понятными ошибками авторизации."""
    for attempt in range(6):
        resp = await client.get(API + path, headers=_headers(token), params=params or None)
        if resp.status_code == 429:  # rate limited — ждём и повторяем
            retry_after = float(resp.headers.get("Retry-After", "1"))
            await asyncio.sleep(retry_after + 0.1)
            continue
        if resp.status_code == 401:
            raise DiscordError("Неверный токен бота (401).")
        if resp.status_code == 403:
            raise DiscordError(f"Нет доступа (403) к {path}. Проверь права бота.")
        if resp.status_code == 404:
            raise DiscordError(f"Не найдено (404): {path}.")
        resp.raise_for_status()
        return resp.json()
    raise DiscordError("Слишком много rate-limit ответов от Discord.")


async def get_guilds(token: str) -> list[dict]:
    """Серверы, на которых состоит бот."""
    async with httpx.AsyncClient(timeout=30) as client:
        data = await _get(client, token, "/users/@me/guilds")
    return [{"id": g["id"], "name": g["name"]} for g in data]


async def get_channels(token: str, guild_id: str) -> list[Channel]:
    """Каналы сервера + активные ветки (в т.ч. посты форума), пригодные для чтения."""
    async with httpx.AsyncClient(timeout=30) as client:
        raw = await _get(client, token, f"/guilds/{guild_id}/channels")
        channels = [
            Channel(id=c["id"], name=c.get("name") or c["id"], type=c["type"],
                    parent_id=c.get("parent_id"))
            for c in raw
            if c["type"] in TEXT_CHANNEL_TYPES or c["type"] in FORUM_TYPES
        ]
        # Активные ветки (посты форума и обычные треды) отдаются отдельным эндпоинтом.
        active = await _get(client, token, f"/guilds/{guild_id}/threads/active")
        for t in active.get("threads", []):
            channels.append(Channel(id=t["id"], name=t.get("name") or t["id"],
                                    type=t["type"], parent_id=t.get("parent_id")))
    # Форумы/медиа сами не читаемы — оставляем как метки-родители, читаем их посты.
    return channels


async def get_recent_messages(
    token: str, channel_id: str, limit: int = 50
) -> list[dict]:
    """Последние `limit` (макс. 100) сообщений канала, в порядке от старых к новым.
    Используется для образца/предпросмотра — одна быстрая страница."""
    async with httpx.AsyncClient(timeout=30) as client:
        data = await _get(
            client, token, f"/channels/{channel_id}/messages",
            limit=max(1, min(limit, 100)),
        )
    data.reverse()  # Discord отдаёт newest-first -> делаем oldest-first
    return data


async def iter_messages(
    token: str,
    channel_id: str,
    *,
    after: datetime | None = None,
    before: datetime | None = None,
) -> AsyncIterator[dict]:
    """
    Отдаёт сообщения канала от старых к новым (постранично по 100).
    after/before — фильтр по времени (включительно по границам страниц).
    """
    last_id = datetime_to_snowflake(after) if after else 0
    before_snowflake = datetime_to_snowflake(before) if before else None

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            batch = await _get(
                client, token, f"/channels/{channel_id}/messages",
                after=str(last_id), limit=100,
            )
            if not batch:
                return
            # Discord отдаёт newest-first; разворачиваем в oldest-first.
            batch.reverse()
            for msg in batch:
                if before_snowflake is not None and int(msg["id"]) >= before_snowflake:
                    return
                yield msg
            last_id = batch[-1]["id"]  # самый новый в пачке — продолжаем после него
