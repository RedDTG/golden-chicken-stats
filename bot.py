import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
import gspread
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Paris"))
WATCH_CHANNEL_ID = int(os.environ["WATCH_CHANNEL_ID"]) if os.environ.get("WATCH_CHANNEL_ID") else None
UNBELIEVABOAT_ID = int(os.environ.get("UNBELIEVABOAT_ID", "0") or 0)
PROGRESS_INTERVAL = float(os.environ.get("BACKFILL_PROGRESS_INTERVAL", "5"))
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
GOOGLE_CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "Cockfights")
OVERVIEW_SHEET_TAB = os.environ.get("OVERVIEW_SHEET_TAB", "Overview")
BANK_SHEET_TAB = os.environ.get("BANK_SHEET_TAB", "Bank")
TRACKED_MEMBER_IDS = {int(x) for x in os.environ.get("TRACKED_MEMBER_IDS", "").split(",") if x.strip()}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cockfight-tracker")

WIN_GAIN_RE = re.compile(r"([\d,]+)\s*richer", re.IGNORECASE)
PERCENT_RE = re.compile(r"(\d+)\s*%")
BET_RE = re.compile(r"\+cf\s+([\d,]+)", re.IGNORECASE)
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
AMOUNT_RE = re.compile(r"([\d,]+)")

HEADER = ["Horodatage", "Joueur", "Resultat", "Gain", "Probabilite (%)", "Message ID", "Serveur", "ID Joueur"]
OVERVIEW_HEADER = ["Utilisateur", "Total", "Defaites", "Victoires", "Winrate", "Meilleur poulet", "Rentabilite", "ID Joueur"]
BANK_HEADER = ["Horodatage", "Joueur", "Cash", "Banque", "Total", "Message ID", "Serveur"]

# Rempli au demarrage (on_ready) via fetch_user: l'embed +bal d'UnbelievaBoat
# n'expose pas l'ID Discord, seulement embed.author.name (le pseudo brut, meme
# source que "Joueur" pour les cockfights) - on resout donc TRACKED_MEMBER_IDS
# en pseudos une fois au demarrage pour pouvoir matcher par nom ensuite.
tracked_usernames: set[str] = set()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
if GOOGLE_CREDENTIALS_JSON:
    _creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
else:
    _creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
_gc = gspread.authorize(_creds)
_sheet = _gc.open_by_key(GOOGLE_SHEET_ID).worksheet(GOOGLE_SHEET_TAB)
_overview_sheet = _gc.open_by_key(GOOGLE_SHEET_ID).worksheet(OVERVIEW_SHEET_TAB)
_bank_sheet = _gc.open_by_key(GOOGLE_SHEET_ID).worksheet(BANK_SHEET_TAB)


def ensure_header() -> None:
    if _sheet.row_values(1) != HEADER:
        _sheet.update("A1", [HEADER])
    if _overview_sheet.row_values(1) != OVERVIEW_HEADER:
        _overview_sheet.update("A1", [OVERVIEW_HEADER])
    if _bank_sheet.row_values(1) != BANK_HEADER:
        _bank_sheet.update("A1", [BANK_HEADER])


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


def parse_balance_embed(embed: discord.Embed):
    """Parse un embed +bal d'UnbelievaBoat, uniquement pour un des joueurs
    suivis (TRACKED_MEMBER_IDS resolus en pseudos dans tracked_usernames)."""
    player = embed.author.name if embed.author and embed.author.name else None
    if not player or player.lower() not in tracked_usernames:
        return None

    amounts = {"cash": "", "bank": "", "total": ""}
    for field in embed.fields:
        name = (field.name or "").strip().lower()
        key = next((k for k in amounts if name.startswith(k)), None)
        if key is None:
            continue
        value = CUSTOM_EMOJI_RE.sub("", field.value or "")
        match = AMOUNT_RE.search(value)
        amounts[key] = match.group(1).replace(",", "") if match else ""

    if not amounts["total"]:
        return None
    return player, amounts["cash"], amounts["bank"], amounts["total"]


def last_win_strength(player: str) -> str:
    """Estime la probabilite du poulet actuel du joueur: chaque poulet neuf
    (achete apres une Defaite, qui le tue toujours) repart a 50%, et ne
    remonte que lorsqu'une Victoire confirme le palier suivant."""
    values = _sheet.get_all_values()
    rows = [row for row in values[1:] if len(row) > 4 and row[1] == player and row[0]]
    if not rows:
        return "50"
    latest = max(rows, key=lambda row: datetime.fromisoformat(row[0]))
    if latest[2] == "Victoire" and latest[4].isdigit():
        return str(int(latest[4]) + 1)
    return "50"


def update_overview() -> None:
    """Recalcule la feuille Overview (une ligne par joueur + une ligne 'Total')
    a partir de l'ensemble des lignes de la feuille principale. Regroupe par ID
    Discord (colonne H de la feuille principale) plutot que par pseudo affiche,
    car deux joueurs differents peuvent partager le meme pseudo/surnom sur un
    serveur - les lignes anterieures a l'ajout de cette colonne (sans ID) se
    replient sur un regroupement par pseudo, corrige des qu'un /backfill est
    relance pour leur retro-attribuer un ID."""
    stats: dict[str, dict[str, int]] = {}
    names: dict[str, str] = {"total": "Total"}

    def entry(key: str) -> dict[str, int]:
        return stats.setdefault(key, {"total": 0, "defeats": 0, "wins": 0, "best_percent": 0, "profit": 0})

    for row in _sheet.get_all_values()[1:]:
        if len(row) < 4:
            continue
        player, result, gain = row[1], row[2], row[3]
        player_id = row[7] if len(row) > 7 and row[7] else ""
        key = f"id:{player_id}" if player_id else f"name:{player.lower()}"
        names[key] = player

        for target_key in (key, "total"):
            data = entry(target_key)
            data["total"] += 1
            if result == "Victoire":
                data["wins"] += 1
            elif result == "Defaite":
                data["defeats"] += 1
            if gain.lstrip("-").isdigit():
                data["profit"] += int(gain)
            if result == "Victoire" and len(row) > 4 and row[4].isdigit():
                data["best_percent"] = max(data["best_percent"], int(row[4]))

    overview_rows = _overview_sheet.get_all_values()
    existing_by_id = {row[7]: i for i, row in enumerate(overview_rows, start=1) if i > 1 and len(row) > 7 and row[7]}
    existing_by_name = {row[0]: i for i, row in enumerate(overview_rows, start=1) if i > 1 and row and row[0]}
    claimed_name_rows: set[int] = set()

    updates = []
    new_rows = []
    for key in ["total"] + sorted((k for k in stats if k != "total"), key=lambda k: names[k]):
        data = stats[key]
        display_name = names[key]
        id_value = key[3:] if key.startswith("id:") else ""
        winrate = round(data["wins"] / data["total"] * 100) / 100 if data["total"] else ""
        best_chicken = data["best_percent"] / 100 if data["best_percent"] else ""
        values = [data["total"], data["defeats"], data["wins"], winrate, best_chicken, data["profit"], id_value]

        row_index = existing_by_id.get(id_value) if id_value else None
        if row_index is None:
            # Repli par pseudo (ligne pas encore rattachee a un ID). "Reclame"
            # la ligne pour eviter que deux joueurs au meme pseudo ne se
            # disputent - et s'ecrasent mutuellement - la meme ligne existante.
            candidate = existing_by_name.get(display_name)
            if candidate is not None and candidate not in claimed_name_rows:
                row_index = candidate
                claimed_name_rows.add(candidate)

        if row_index is not None:
            updates.append({"range": f"A{row_index}:H{row_index}", "values": [[display_name, *values]]})
        else:
            new_rows.append([display_name, *values])

    if updates:
        _overview_sheet.batch_update(updates, value_input_option="USER_ENTERED")
    if new_rows:
        _overview_sheet.append_rows(new_rows, value_input_option="USER_ENTERED")


async def find_command_info(message: discord.Message, player: str) -> tuple[str, int | None]:
    """Cherche en arriere (jusqu'a 10 messages) le +cf du joueur ayant produit
    ce resultat: renvoie (montant mise ou '', ID Discord de l'auteur ou None).
    L'ID sert de cle stable pour l'agregation Overview: embed.author.name (le
    pseudo affiche par UnbelievaBoat) n'est pas garanti unique sur un serveur
    (doublons de pseudo/surnom), contrairement a message.author.id de qui a
    tape la commande. Limite connue: la recherche elle-meme filtre encore par
    pseudo (prev.author.name/display_name) pour ignorer les +cf d'autres
    joueurs intercales dans le salon, donc si deux joueurs au pseudo identique
    parient au meme moment, le mauvais ID peut etre attribue - inevitable sans
    lien requete/reponse fourni par UnbelievaBoat lui-meme."""
    player = player.lower()
    async for prev in message.channel.history(limit=10, before=message):
        if prev.author.bot:
            continue
        if not prev.content.lower().startswith("+cf"):
            continue
        names = {prev.author.name.lower(), prev.author.display_name.lower()}
        if player not in names:
            continue
        # Le +cf le plus recent du joueur est forcement celui qui a produit
        # ce resultat: on s'arrete la meme s'il n'a pas de montant chiffre
        # (ex. "+cf all"), plutot que de continuer vers un +cf plus ancien
        # et sans rapport (ex. une commande precedente ratee).
        match = BET_RE.search(prev.content)
        bet = match.group(1).replace(",", "") if match else ""
        return bet, prev.author.id
    return "", None


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
    last_progress = time.monotonic()

    for channel in channels:
        subchannels = await _channel_group(channel)
        log.info("Analyse du salon #%s (%d fil(s) inclus)...", channel.name, len(subchannels) - 1)

        messages = []
        for sub in subchannels:
            async for m in sub.history(limit=None, oldest_first=True):
                messages.append(m)
                now = time.monotonic()
                if now - last_progress >= PROGRESS_INTERVAL:
                    log.info(
                        "... recuperation: #%s, message du %s",
                        sub.name,
                        m.created_at.astimezone(TIMEZONE).isoformat(timespec="seconds"),
                    )
                    last_progress = now
        messages.sort(key=lambda m: m.id)

        for message in messages:
            now = time.monotonic()
            if now - last_progress >= PROGRESS_INTERVAL:
                log.info(
                    "... traitement: #%s, message du %s",
                    message.channel.name,
                    message.created_at.astimezone(TIMEZONE).isoformat(timespec="seconds"),
                )
                last_progress = now

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
                bet, player_id = await find_command_info(message, player)

                if result == "Victoire" and strength:
                    last_win_percent[player] = strength

                if result == "Defaite":
                    if bet:
                        gain = f"-{bet}"
                    if not strength:
                        strength = str(int(last_win_percent[player]) + 1) if player in last_win_percent else "50"
                    # Une Defaite tue toujours le poulet: le suivant reparaitra a 50%
                    # tant qu'aucune nouvelle Victoire ne confirme un palier plus haut.
                    last_win_percent[player] = "49"

                msg_id = str(message.id)
                server = message.guild.name if message.guild else ""
                timestamp = message.created_at.astimezone(TIMEZONE).isoformat(timespec="seconds")
                id_value = str(player_id) if player_id else ""

                if msg_id in existing:
                    row = existing[msg_id]
                    corrected_rows.add(row)
                    updates.append({"range": f"A{row}", "values": [[timestamp]]})
                    updates.append({"range": f"C{row}:E{row}", "values": [[result, gain, strength]]})
                    updates.append({"range": f"G{row}:H{row}", "values": [[server, id_value]]})
                else:
                    new_rows.append([timestamp, player, result, gain, strength, msg_id, server, id_value])

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

    if updates or new_rows:
        update_overview()

    return len(corrected_rows), len(new_rows)


def build_stats_embed(server: str) -> discord.Embed:
    rows = [row for row in _sheet.get_all_values()[1:] if len(row) > 6 and row[6] == server]
    wins = [row for row in rows if row[2] == "Victoire"]
    losses = [row for row in rows if row[2] == "Defaite"]

    total_counts = Counter(row[1] for row in rows)
    win_counts = Counter(row[1] for row in wins)
    winrates = {player: win_counts[player] / total * 100 for player, total in total_counts.items()}
    best_winrate = max(winrates.items(), key=lambda item: item[1], default=None)
    worst_winrate = min(winrates.items(), key=lambda item: item[1], default=None)

    highest_prob_win = max(
        (row for row in wins if len(row) > 4 and row[4].isdigit()),
        key=lambda row: int(row[4]),
        default=None,
    )

    embed = discord.Embed(title=f"Statistiques cockfight - {server}", color=discord.Color.gold())
    embed.add_field(name="Combats", value=str(len(rows)), inline=True)
    embed.add_field(name="Victoires", value=str(len(wins)), inline=True)
    embed.add_field(name="Defaites", value=str(len(losses)), inline=True)
    embed.add_field(
        name="Meilleur winrate",
        value=f"{best_winrate[0]} ({best_winrate[1]:.0f}%)" if best_winrate else "Aucune",
        inline=False,
    )
    embed.add_field(
        name="Pire winrate",
        value=f"{worst_winrate[0]} ({worst_winrate[1]:.0f}%)" if worst_winrate else "Aucune",
        inline=False,
    )
    embed.add_field(
        name="Poulet a la plus haute probabilite",
        value=f"{highest_prob_win[1]} ({highest_prob_win[4]}%)" if highest_prob_win else "Aucun",
        inline=False,
    )
    return embed


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
_backfill_lock = asyncio.Lock()


async def _sync_guild_commands(guild: discord.Guild) -> None:
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)


@bot.event
async def on_ready():
    ensure_header()
    global tracked_usernames
    tracked_usernames = set()
    for user_id in TRACKED_MEMBER_IDS:
        try:
            user = await bot.fetch_user(user_id)
        except discord.HTTPException:
            log.warning("Impossible de resoudre l'utilisateur suivi %d", user_id)
            continue
        tracked_usernames.add(user.name.lower())
    if TRACKED_MEMBER_IDS:
        log.info("%d membre(s) suivi(s) pour la feuille Bank", len(tracked_usernames))
    try:
        # On synchronise uniquement par serveur (quasi instantane) : un sync
        # global met jusqu'a une heure a apparaitre, et le combiner avec un
        # sync par serveur fait apparaitre chaque commande en double dans le
        # client Discord. bulk_upsert_global_commands([]) efface les
        # commandes globales laissees par un ancien deploiement, sans passer
        # par tree.sync() qui viderait aussi la source utilisee par copy_global_to.
        await bot.http.bulk_upsert_global_commands(bot.application_id, [])
        for guild in bot.guilds:
            await _sync_guild_commands(guild)
    except discord.HTTPException:
        log.exception("Echec de la synchronisation des commandes slash")
    else:
        log.info("Commandes slash synchronisees sur %d serveur(s)", len(bot.guilds))
    if WATCH_CHANNEL_ID:
        log.info("Connecte en tant que %s - ecoute du salon %s", bot.user, WATCH_CHANNEL_ID)
    else:
        log.info("Connecte en tant que %s - ecoute de tous les salons", bot.user)


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    await _sync_guild_commands(guild)


@bot.tree.command(name="backfill", description="Rescanne l'historique (et les fils) pour corriger/ajouter les combats manques")
@app_commands.checks.has_permissions(administrator=True)
async def backfill_command(interaction: discord.Interaction):
    if _backfill_lock.locked():
        await interaction.response.send_message("Un backfill est deja en cours.", ephemeral=True)
        return

    await interaction.response.defer()
    async with _backfill_lock:
        await interaction.followup.send("Backfill en cours, ca peut prendre un moment...")
        try:
            corrected, added = await run_backfill(bot)
        except Exception:
            log.exception("Erreur pendant le backfill")
            await interaction.followup.send("Le backfill a echoue, voir les logs.")
            return

        await interaction.followup.send(f"Backfill termine : {corrected} ligne(s) corrigee(s), {added} ligne(s) ajoutee(s).")


@backfill_command.error
async def backfill_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("Tu dois etre administrateur pour lancer un backfill.", ephemeral=True)
    else:
        raise error


@bot.tree.command(name="stats", description="Affiche les statistiques cockfight du serveur")
async def stats_command(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Cette commande doit etre utilisee dans un serveur.", ephemeral=True)
        return

    await interaction.response.defer()
    embed = build_stats_embed(interaction.guild.name)
    await interaction.followup.send(embed=embed)


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
            if tracked_usernames:
                balance = parse_balance_embed(embed)
                if balance is not None:
                    player, cash, bank_amount, total = balance
                    timestamp = datetime.now(TIMEZONE).isoformat(timespec="seconds")
                    server = message.guild.name if message.guild else ""
                    _bank_sheet.append_row(
                        [timestamp, player, cash, bank_amount, total, str(message.id), server],
                        value_input_option="USER_ENTERED",
                    )
                    log.info("Bank: %s - cash=%s banque=%s total=%s", player, cash, bank_amount, total)
            continue

        player, result, gain, strength = parsed
        bet, player_id = await find_command_info(message, player)

        if result == "Defaite":
            if bet:
                gain = f"-{bet}"
            if not strength:
                strength = last_win_strength(player)

        timestamp = datetime.now(TIMEZONE).isoformat(timespec="seconds")
        server = message.guild.name if message.guild else ""

        _sheet.append_row(
            [timestamp, player, result, gain, strength, str(message.id), server, str(player_id) if player_id else ""],
            value_input_option="USER_ENTERED",
        )
        update_overview()
        log.info("%s - %s - gain=%s - force=%s%%", player, result, gain, strength)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
