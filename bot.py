import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone

import discord
import gspread
from discord.ext import commands
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
WATCH_CHANNEL_ID = int(os.environ["WATCH_CHANNEL_ID"]) if os.environ.get("WATCH_CHANNEL_ID") else None
UNBELIEVABOAT_ID = int(os.environ.get("UNBELIEVABOAT_ID", "0") or 0)
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
GOOGLE_CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "Cockfights")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cockfight-tracker")

WIN_GAIN_RE = re.compile(r"([\d,]+)\s*richer", re.IGNORECASE)
PERCENT_RE = re.compile(r"(\d+)\s*%")
BET_RE = re.compile(r"\+cf\s+([\d,]+)", re.IGNORECASE)

HEADER = ["Horodatage", "Joueur", "Resultat", "Gain", "Probabilite (%)", "Message ID", "Serveur"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
if GOOGLE_CREDENTIALS_JSON:
    _creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
else:
    _creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
_gc = gspread.authorize(_creds)
_sheet = _gc.open_by_key(GOOGLE_SHEET_ID).worksheet(GOOGLE_SHEET_TAB)


def ensure_header() -> None:
    if _sheet.row_values(1) != HEADER:
        _sheet.update("A1", [HEADER])


def parse_cockfight_embed(embed: discord.Embed):
    description = embed.description or ""
    player = embed.author.name if embed.author and embed.author.name else "Inconnu"

    percent_match = PERCENT_RE.search(embed.footer.text or "")
    strength = percent_match.group(1) if percent_match else ""

    if "lost the fight" in description.lower():
        return player, "Defaite", "", strength

    if "won the fight" in description.lower():
        gain_match = WIN_GAIN_RE.search(description)
        gain = gain_match.group(1).replace(",", "") if gain_match else ""
        return player, "Victoire", gain, strength

    return None


def last_win_strength(player: str) -> str:
    values = _sheet.get_all_values()
    wins = [
        row
        for row in values[1:]
        if len(row) > 4 and row[1] == player and row[2] == "Victoire" and row[4].isdigit() and row[0]
    ]
    if not wins:
        return "50"
    latest = max(wins, key=lambda row: datetime.fromisoformat(row[0]))
    return str(int(latest[4]) + 1)


async def find_bet_amount(message: discord.Message, player: str) -> str:
    player = player.lower()
    async for prev in message.channel.history(limit=10, before=message):
        if prev.author.bot:
            continue
        match = BET_RE.search(prev.content)
        if not match:
            continue
        names = {prev.author.name.lower(), prev.author.display_name.lower()}
        if player not in names:
            continue
        return match.group(1).replace(",", "")
    return ""


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


async def _channel_group(channel: discord.TextChannel) -> list[discord.abc.Messageable]:
    """Le salon lui-meme plus tous ses fils (actifs et archives, publics et prives),
    car channel.history() ne descend pas dans les fils."""
    group = [channel]

    active = await channel.guild.active_threads()
    group.extend(t for t in active if t.parent_id == channel.id)

    try:
        group.extend([t async for t in channel.archived_threads(limit=None)])
    except discord.Forbidden:
        pass
    try:
        group.extend([t async for t in channel.archived_threads(private=True, limit=None)])
    except discord.Forbidden:
        pass

    return group


async def run_backfill(client: discord.Client) -> tuple[int, int]:
    """Rescanne l'historique des salons surveilles (et leurs fils) pour corriger
    les lignes existantes (Resultat/Gain/Probabilite) et ajouter les combats jamais
    logues. Retourne (lignes corrigees, lignes ajoutees)."""
    ensure_header()
    existing = _existing_rows()
    channels = _watched_channels(client)
    if not channels:
        log.warning("Aucun salon accessible trouve.")
        return 0, 0

    updates = []
    corrected_rows: set[int] = set()
    new_rows = []
    last_win_percent: dict[str, str] = {}

    for channel in channels:
        subchannels = await _channel_group(channel)
        log.info("Analyse du salon #%s (%d fil(s) inclus)...", channel.name, len(subchannels) - 1)

        messages = []
        for sub in subchannels:
            messages.extend([m async for m in sub.history(limit=None, oldest_first=True)])
        messages.sort(key=lambda m: m.id)

        for message in messages:
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
                    corrected_rows.add(row)
                    updates.append({"range": f"C{row}:E{row}", "values": [[result, gain, strength]]})
                    updates.append({"range": f"G{row}", "values": [[server]]})
                else:
                    timestamp = message.created_at.astimezone().isoformat(timespec="seconds")
                    new_rows.append([timestamp, player, result, gain, strength, msg_id, server])

    if updates:
        _sheet.batch_update(updates, value_input_option="USER_ENTERED")
        log.info("%d ligne(s) corrigee(s).", len(corrected_rows))
    else:
        log.info("Aucune ligne a corriger.")

    if new_rows:
        _sheet.append_rows(new_rows, value_input_option="USER_ENTERED")
        log.info("%d ligne(s) ajoutee(s).", len(new_rows))
    else:
        log.info("Aucune nouvelle ligne a ajouter.")

    return len(corrected_rows), len(new_rows)


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
_backfill_lock = asyncio.Lock()


@bot.event
async def on_ready():
    ensure_header()
    if WATCH_CHANNEL_ID:
        log.info("Connecte en tant que %s - ecoute du salon %s", bot.user, WATCH_CHANNEL_ID)
    else:
        log.info("Connecte en tant que %s - ecoute de tous les salons", bot.user)


@bot.command(name="backfill")
@commands.has_permissions(administrator=True)
async def backfill_command(ctx: commands.Context):
    if _backfill_lock.locked():
        await ctx.send("Un backfill est deja en cours.")
        return

    async with _backfill_lock:
        await ctx.send("Backfill en cours, ca peut prendre un moment...")
        try:
            corrected, added = await run_backfill(bot)
        except Exception:
            log.exception("Erreur pendant le backfill")
            await ctx.send("Le backfill a echoue, voir les logs.")
            return

        await ctx.send(f"Backfill termine : {corrected} ligne(s) corrigee(s), {added} ligne(s) ajoutee(s).")


@backfill_command.error
async def backfill_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Tu dois etre administrateur pour lancer un backfill.")
    else:
        raise error


@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

    if WATCH_CHANNEL_ID and message.channel.id != WATCH_CHANNEL_ID:
        return
    if not message.author.bot:
        return
    if UNBELIEVABOAT_ID and message.author.id != UNBELIEVABOAT_ID:
        return
    if not message.embeds:
        return

    for embed in message.embeds:
        parsed = parse_cockfight_embed(embed)
        if parsed is None:
            continue

        player, result, gain, strength = parsed

        if result == "Defaite":
            bet = await find_bet_amount(message, player)
            if bet:
                gain = f"-{bet}"
            if not strength:
                strength = last_win_strength(player)

        timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        server = message.guild.name if message.guild else ""

        _sheet.append_row(
            [timestamp, player, result, gain, strength, str(message.id), server],
            value_input_option="USER_ENTERED",
        )
        log.info("%s - %s - gain=%s - force=%s%%", player, result, gain, strength)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
