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
GOOGLE_CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "Cockfights")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cockfight-tracker")

WIN_GAIN_RE = re.compile(r"made you\D*([\d,]+)\D*richer", re.IGNORECASE)
PERCENT_RE = re.compile(r"(\d+)\s*%")

HEADER = ["Horodatage", "Joueur", "Resultat", "Gain", "Probabilite (%)", "Message ID"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
_gc = gspread.authorize(_creds)
_sheet = _gc.open_by_key(GOOGLE_SHEET_ID).worksheet(GOOGLE_SHEET_TAB)


def ensure_header() -> None:
    if _sheet.row_values(1) != HEADER:
        _sheet.update("A1", [HEADER])


def parse_cockfight_embed(embed: discord.Embed):
    description = embed.description or ""
    player = embed.author.name if embed.author and embed.author.name else "Inconnu"

    if "lost the fight" in description.lower():
        return player, "Defaite", "", ""

    if "won the fight" in description.lower():
        gain_match = WIN_GAIN_RE.search(description)
        gain = gain_match.group(1).replace(",", "") if gain_match else ""

        strength = ""
        for field in embed.fields:
            if "chance of winning" in field.name.lower():
                percent_match = PERCENT_RE.search(field.value)
                strength = percent_match.group(1) if percent_match else ""
                break

        return player, "Victoire", gain, strength

    return None


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    ensure_header()
    if WATCH_CHANNEL_ID:
        log.info("Connecte en tant que %s - ecoute du salon %s", bot.user, WATCH_CHANNEL_ID)
    else:
        log.info("Connecte en tant que %s - ecoute de tous les salons", bot.user)


@bot.event
async def on_message(message: discord.Message):
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
        timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

        _sheet.append_row(
            [timestamp, player, result, gain, strength, str(message.id)],
            value_input_option="USER_ENTERED",
        )
        log.info("%s - %s - gain=%s - force=%s%%", player, result, gain, strength)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
