"""Rescanne l'historique du salon pour corriger les lignes existantes
(Resultat/Gain/Probabilite) et ajouter les combats jamais logues.

Usage: python backfill.py

Le meme rescan est aussi disponible en commande Discord (!backfill,
reservee aux administrateurs) sans avoir a lancer ce script separement,
voir bot.py.
"""

import asyncio

import discord

from bot import DISCORD_TOKEN, intents, run_backfill


async def main() -> None:
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            await run_backfill(client)
        finally:
            await client.close()

    await client.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
