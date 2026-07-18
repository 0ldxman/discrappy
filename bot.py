"""
Discord-бот со slash-командой /scrape.

Вызов прямо в Discord:
    /scrape channel:#рп-канал [after:2026-01-01] [before:...] [upload:True] [share:True]

Бот скрейпит историю канала, заливает результат на Nextcloud и отвечает
публичной ссылкой. Ту же логику имеет CLI (cli.py).
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from typing import Union

import discord
from discord import app_commands
from dotenv import load_dotenv

import core
import nextcloud

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = os.getenv("GUILD_ID", "").strip()  # если задан — мгновенная регистрация команд

# Типы, которые нативный пикер Discord предложит выбрать. Thread покрывает и
# обычные ветки, и посты форума (в Discord пост форума = приватная/публичная ветка).
# Голый ForumChannel не включаем: у него нет своей истории сообщений — сообщения
# живут в его постах-ветках, которые пикер и так покажет как Thread.
ScrapeableChannel = Union[
    discord.TextChannel,     # текстовые и анонс-каналы
    discord.Thread,          # ветки + посты форума
    discord.VoiceChannel,    # встроенный чат голосовых каналов
    discord.StageChannel,    # встроенный чат трибун
]


def _parse_dt(value: str | None, cfg: core.ScrapeConfig) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=cfg.timezone) if cfg.timezone else dt.astimezone()
    return dt


intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready() -> None:
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
    else:
        await tree.sync()
    print(f"Бот онлайн как {client.user}. Команда /scrape зарегистрирована.")


@tree.command(name="scrape", description="Скрейпит embed'ы персонажей и заливает на Nextcloud")
@app_commands.describe(
    channel="Канал, историю которого скрейпим",
    after="Брать сообщения после даты (ISO, напр. 2026-01-01)",
    before="Брать сообщения до даты (ISO)",
    upload="Заливать ли результат на Nextcloud (по умолчанию да)",
    share="Создавать ли публичную ссылку (по умолчанию да)",
)
async def scrape_command(
    interaction: discord.Interaction,
    channel: ScrapeableChannel,
    after: str | None = None,
    before: str | None = None,
    upload: bool = True,
    share: bool = True,
) -> None:
    await interaction.response.defer(thinking=True)

    cfg = core.ScrapeConfig.from_env()
    try:
        after_dt = _parse_dt(after, cfg)
        before_dt = _parse_dt(before, cfg)
    except ValueError:
        await interaction.followup.send("Неверный формат даты. Используй ISO, напр. `2026-01-01`.")
        return

    ts_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    remote_name = f"scrape-{channel.name}-{ts_tag}.txt"

    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, remote_name)
        try:
            scrape = await core.scrape_channel(
                channel, out_path, cfg, after=after_dt, before=before_dt
            )
        except discord.Forbidden:
            await interaction.followup.send(f"Нет прав читать историю {channel.mention}.")
            return

        summary = (
            f"Канал {channel.mention}: просмотрено {scrape.messages_seen} сообщений, "
            f"найдено **{scrape.lines}** реплик."
        )

        if scrape.lines == 0:
            await interaction.followup.send(summary + "\nЗаливать нечего.")
            return

        if not upload:
            await interaction.followup.send(
                summary, file=discord.File(out_path, filename=remote_name)
            )
            return

        try:
            nc = nextcloud.NextcloudConfig.from_env()
            remote_path, link = await nextcloud.upload_and_share(
                nc, out_path, remote_name, share=share
            )
        except Exception as exc:  # noqa: BLE001 — сообщим пользователю
            await interaction.followup.send(summary + f"\n⚠️ Ошибка Nextcloud: `{exc}`")
            return

    msg = summary + f"\n✅ Загружено на Nextcloud: `{remote_path}`"
    if link:
        msg += f"\n🔗 {link}"
    await interaction.followup.send(msg)


def main() -> None:
    if not TOKEN:
        raise SystemExit("[ОШИБКА] Не задан DISCORD_TOKEN.")
    client.run(TOKEN)


if __name__ == "__main__":
    main()
