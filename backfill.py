"""Rescanne l'historique du salon pour corriger les lignes existantes
(Resultat/Gain/Probabilite) et ajouter les combats jamais logues.

Usage: python backfill.py
"""

import asyncio

import discord

from bot import (
    DISCORD_TOKEN,
    UNBELIEVABOAT_ID,
    WATCH_CHANNEL_ID,
    _sheet,
    ensure_header,
    find_bet_amount,
    intents,
    log,
    parse_cockfight_embed,
)


def _existing_rows() -> dict[str, int]:
    values = _sheet.get_all_values()
    return {row[5]: i for i, row in enumerate(values, start=1) if i > 1 and len(row) > 5 and row[5]}


def _watched_channels(client: discord.Client) -> list[discord.TextChannel]:
    if WATCH_CHANNEL_ID:
        channel = client.get_channel(WATCH_CHANNEL_ID)
        return [channel] if channel else []

    channels = []
    for guild in client.guilds:
        channels.extend(c for c in guild.text_channels if c.permissions_for(guild.me).read_message_history)
    return channels


async def backfill() -> None:
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            ensure_header()
            existing = _existing_rows()
            channels = _watched_channels(client)
            if not channels:
                log.warning("Aucun salon accessible trouve.")
                return

            updates = []
            new_rows = []
            last_win_percent: dict[str, str] = {}

            for channel in channels:
                log.info("Analyse du salon #%s...", channel.name)
                async for message in channel.history(limit=None, oldest_first=True):
                    if not message.author.bot:
                        continue
                    if UNBELIEVABOAT_ID and message.author.id != UNBELIEVABOAT_ID:
                        continue
                    if not message.embeds:
                        continue

                    for embed in message.embeds:
                        parsed = parse_cockfight_embed(embed)
                        if parsed is None:
                            continue

                        player, result, gain, strength = parsed

                        if result == "Victoire" and strength:
                            last_win_percent[player] = strength

                        if result == "Defaite":
                            bet = await find_bet_amount(message, player)
                            if bet:
                                gain = f"-{bet}"
                            if not strength:
                                strength = str(int(last_win_percent[player]) + 1) if player in last_win_percent else "50"

                        msg_id = str(message.id)
                        server = message.guild.name if message.guild else ""

                        if msg_id in existing:
                            row = existing[msg_id]
                            updates.append({"range": f"C{row}:E{row}", "values": [[result, gain, strength]]})
                            updates.append({"range": f"G{row}", "values": [[server]]})
                        else:
                            timestamp = message.created_at.astimezone().isoformat(timespec="seconds")
                            new_rows.append([timestamp, player, result, gain, strength, msg_id, server])

            if updates:
                _sheet.batch_update(updates, value_input_option="USER_ENTERED")
                log.info("%d ligne(s) corrigee(s).", len(updates))
            else:
                log.info("Aucune ligne a corriger.")

            if new_rows:
                _sheet.append_rows(new_rows, value_input_option="USER_ENTERED")
                log.info("%d ligne(s) ajoutee(s).", len(new_rows))
            else:
                log.info("Aucune nouvelle ligne a ajouter.")
        finally:
            await client.close()

    await client.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(backfill())
