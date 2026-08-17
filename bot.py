import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
import gspread
from discord import app_commands
from discord.ext import commands, tasks
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
BACKFILL_SHEET_TAB = os.environ.get("BACKFILL_SHEET_TAB", "Backfill")
TRACKED_MEMBER_IDS = {int(x) for x in os.environ.get("TRACKED_MEMBER_IDS", "").split(",") if x.strip()}
# Intervalle minimum (secondes) entre deux ecritures du statut de progression
# de /backfill sur l'onglet BACKFILL_SHEET_TAB - plus large que
# BACKFILL_PROGRESS_INTERVAL (qui ne fait que logguer) pour ne pas empiler
# des ecritures Sheets trop frequentes sur un run de plusieurs heures.
BACKFILL_HEARTBEAT_INTERVAL = float(os.environ.get("BACKFILL_HEARTBEAT_INTERVAL", "30"))
# Chemin du fichier SQLite qui sert de source de verite pour Stats/Bank (voir
# init_db()). En deploiement conteneurise (Coolify...), ce chemin DOIT etre
# sur un volume persistant, sinon il est perdu a chaque redeploiement et la
# durabilite qu'il apporte ne couvre plus qu'un simple crash/restart du process.
DB_PATH = os.environ.get("DB_PATH", "bot.db")
SYNC_INTERVAL = float(os.environ.get("SYNC_INTERVAL", "15"))
RECORD_CHANNEL_ID = int(os.environ["RECORD_CHANNEL_ID"]) if os.environ.get("RECORD_CHANNEL_ID") else None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cockfight-tracker")

WIN_GAIN_RE = re.compile(r"([\d,]+)\s*richer", re.IGNORECASE)
PERCENT_RE = re.compile(r"(\d+)\s*%")
BET_RE = re.compile(r"\+cf\s+([\d,]+)", re.IGNORECASE)
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
AMOUNT_RE = re.compile(r"([\d,]+)")
# Ancien format d'embed UnbelievaBoat (~2019): embed.author.name porte encore
# le discriminant legacy "Pseudo#1234". On le retire pour que "Joueur" reste
# comparable au pseudo actuel (sans discriminant) utilise partout ailleurs.
DISCRIMINATOR_RE = re.compile(r"#\d{4}$")
# Extrait le numero de la premiere ligne d'un append_rows() gspread, ex.
# "'Stats'!A13379:H13381" -> 13379 (voir _flush_stats()).
APPEND_ROW_RE = re.compile(r"![A-Z]+(\d+)")

HEADER = ["Horodatage", "Joueur", "Resultat", "Gain", "Probabilite (%)", "Message ID", "Serveur", "ID Joueur"]
OVERVIEW_HEADER = ["Utilisateur", "Total", "Defaites", "Victoires", "Winrate", "Meilleur poulet", "Rentabilite", "ID Joueur"]
BANK_HEADER = ["Horodatage", "Joueur", "Cash", "Banque", "Total", "Message ID", "Serveur"]
BACKFILL_HEADER = ["Statut", "Salon", "Dernier message traite", "Corrigees", "Ajoutees", "Derniere mise a jour"]

# Rempli au demarrage (on_ready) via fetch_user: l'embed +bal d'UnbelievaBoat
# n'expose pas l'ID Discord, seulement embed.author.name (le pseudo brut, meme
# source que "Joueur" pour les cockfights) - on resout donc TRACKED_MEMBER_IDS
# en pseudos une fois au demarrage pour pouvoir matcher par nom ensuite.
tracked_usernames: set[str] = set()

# Rempli au demarrage (on_ready, voir _load_best_strength()) et rafraichi
# silencieusement apres un /backfill: probabilite de victoire la plus haute
# jamais enregistree, par serveur. Comparee a chaque nouvelle Victoire pour
# detecter un record en temps reel sans re-scanner toute la table a chaque
# fois (voir _maybe_announce_record()).
_best_strength_by_server: dict[str, int] = {}

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
if GOOGLE_CREDENTIALS_JSON:
    _creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
else:
    _creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
_gc = gspread.authorize(_creds)
_sheet = _gc.open_by_key(GOOGLE_SHEET_ID).worksheet(GOOGLE_SHEET_TAB)
_overview_sheet = _gc.open_by_key(GOOGLE_SHEET_ID).worksheet(OVERVIEW_SHEET_TAB)
_bank_sheet = _gc.open_by_key(GOOGLE_SHEET_ID).worksheet(BANK_SHEET_TAB)
_backfill_sheet = _gc.open_by_key(GOOGLE_SHEET_ID).worksheet(BACKFILL_SHEET_TAB)

# sqlite3 sur un fichier local: le bot tourne entierement sur un seul thread
# (la boucle asyncio), donc une connexion partagee sans check_same_thread est
# sure. WAL pour un meilleur comportement en lecture/ecriture concurrente
# (ex. /backfill qui tourne pendant que la boucle de sync tourne aussi).
_db = sqlite3.connect(DB_PATH)
_db.execute("PRAGMA journal_mode=WAL")


def init_db() -> None:
    """Cree les tables si besoin puis, si elles sont vides (premier lancement
    ou volume neuf), importe une fois l'historique deja present sur Sheets -
    sans ca, Overview/last_win_strength/etc. perdraient toute la continuite
    avec les lignes deja loguees avant ce changement."""
    _db.execute(
        """
        CREATE TABLE IF NOT EXISTS stats (
            message_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            player TEXT NOT NULL,
            result TEXT NOT NULL,
            gain TEXT NOT NULL,
            strength TEXT NOT NULL,
            server TEXT NOT NULL,
            player_id TEXT NOT NULL DEFAULT '',
            sheet_row INTEGER,
            synced INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    _db.execute(
        """
        CREATE TABLE IF NOT EXISTS bank (
            message_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            player TEXT NOT NULL,
            cash TEXT NOT NULL,
            banque TEXT NOT NULL,
            total TEXT NOT NULL,
            server TEXT NOT NULL,
            synced INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    _db.commit()
    _bootstrap_from_sheets()


def _bootstrap_from_sheets() -> None:
    if _db.execute("SELECT 1 FROM stats LIMIT 1").fetchone() is None:
        imported = 0
        for i, row in enumerate(_sheet.get_all_values()[1:], start=2):  # ligne 1 = header
            if len(row) < 6 or not row[5]:
                continue
            timestamp, player, result, gain, strength, message_id = row[0], row[1], row[2], row[3], row[4], row[5]
            server = row[6] if len(row) > 6 else ""
            player_id = row[7] if len(row) > 7 else ""
            cur = _db.execute(
                "INSERT OR IGNORE INTO stats "
                "(message_id, timestamp, player, result, gain, strength, server, player_id, sheet_row, synced) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (message_id, timestamp, player, result, gain, strength, server, player_id, i),
            )
            # rowcount vaut 0 si message_id existait deja (Message ID duplique
            # sur la feuille Stats) - on ne compte que les insertions reelles.
            imported += cur.rowcount
        _db.commit()
        if imported:
            log.info("Bootstrap: %d ligne(s) Stats importee(s) depuis Sheets vers SQLite.", imported)

    if _db.execute("SELECT 1 FROM bank LIMIT 1").fetchone() is None:
        imported = 0
        for row in _bank_sheet.get_all_values()[1:]:
            if len(row) < 6 or not row[5]:
                continue
            timestamp, player, cash, banque, total, message_id = row[0], row[1], row[2], row[3], row[4], row[5]
            server = row[6] if len(row) > 6 else ""
            cur = _db.execute(
                "INSERT OR IGNORE INTO bank (message_id, timestamp, player, cash, banque, total, server, synced) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (message_id, timestamp, player, cash, banque, total, server),
            )
            imported += cur.rowcount
        _db.commit()
        if imported:
            log.info("Bootstrap: %d ligne(s) Bank importee(s) depuis Sheets vers SQLite.", imported)


init_db()


async def ensure_header() -> None:
    """gspread est synchrone (requests bloquant) - chaque appel est deporte
    dans un thread via asyncio.to_thread pour ne jamais geler la boucle
    asyncio pendant un aller-retour reseau vers Sheets (voir sync_to_sheets()
    pour le meme raisonnement, plus critique encore vu sa cadence)."""
    if await asyncio.to_thread(_sheet.row_values, 1) != HEADER:
        await asyncio.to_thread(_sheet.update, "A1", [HEADER])
    if await asyncio.to_thread(_overview_sheet.row_values, 1) != OVERVIEW_HEADER:
        await asyncio.to_thread(_overview_sheet.update, "A1", [OVERVIEW_HEADER])
    if await asyncio.to_thread(_bank_sheet.row_values, 1) != BANK_HEADER:
        await asyncio.to_thread(_bank_sheet.update, "A1", [BANK_HEADER])
    if await asyncio.to_thread(_backfill_sheet.row_values, 1) != BACKFILL_HEADER:
        await asyncio.to_thread(_backfill_sheet.update, "A1", [BACKFILL_HEADER])


def parse_cockfight_embed(embed: discord.Embed):
    description = embed.description or ""
    raw_name = embed.author.name if embed.author and embed.author.name else "Inconnu"
    player = DISCRIMINATOR_RE.sub("", raw_name)

    percent_match = PERCENT_RE.search(embed.footer.text or "")
    strength = percent_match.group(1) if percent_match else ""

    description_lower = description.lower()

    # "chicken died" est l'ancien libelle de defaite (~2019, embed.author au
    # format "Pseudo#1234", pas de footer de probabilite) - remplace depuis
    # par "lost the fight", mais toujours present dans le vieil historique
    # qu'un /backfill doit pouvoir relire.
    if "lost the fight" in description_lower or "chicken died" in description_lower:
        return player, "Defaite", "", strength

    if "won the fight" in description_lower:
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
    remonte que lorsqu'une Victoire confirme le palier suivant. Lit SQLite
    (toujours a jour, y compris les lignes pas encore synchronisees vers
    Sheets) plutot que la feuille Stats elle-meme."""
    row = _db.execute(
        "SELECT result, strength FROM stats WHERE player = ? ORDER BY timestamp DESC LIMIT 1",
        (player,),
    ).fetchone()
    if row is None:
        return "50"
    result, strength = row
    if result == "Victoire" and strength.isdigit():
        return str(int(strength) + 1)
    return "50"


def _load_best_strength() -> None:
    """(Re)charge le record de probabilite de victoire par serveur depuis
    SQLite. Appele au demarrage et apres un /backfill (silencieusement - un
    backfill ne fait que decouvrir de l'historique deja joue, annoncer un
    "record" a cette occasion n'aurait pas de sens et spammerait le salon)."""
    global _best_strength_by_server
    _best_strength_by_server = dict(
        _db.execute(
            "SELECT server, MAX(CAST(strength AS INTEGER)) FROM stats "
            "WHERE result = 'Victoire' AND strength != '' GROUP BY server"
        )
    )


async def _maybe_announce_record(message: discord.Message, player: str, server: str, strength: int) -> None:
    """Ping @everyone dans RECORD_CHANNEL_ID quand une Victoire depasse le
    record de probabilite connu pour ce serveur. Ne fait rien si la fonction
    n'est pas configuree (RECORD_CHANNEL_ID vide) ou si ce n'est pas un
    record. L'echec d'annonce (salon introuvable, permissions) est loggue
    mais ne remonte jamais vers on_message - le combat est deja loggue en
    base au moment ou cette fonction est appelee, cet echec ne doit rien
    faire perdre."""
    if not RECORD_CHANNEL_ID or not message.guild:
        return
    if strength <= _best_strength_by_server.get(server, 0):
        return
    previous_best = _best_strength_by_server.get(server, 0)
    _best_strength_by_server[server] = strength

    channel = bot.get_channel(RECORD_CHANNEL_ID)
    if channel is None:
        log.warning("RECORD_CHANNEL_ID=%d introuvable, record non annonce.", RECORD_CHANNEL_ID)
        return

    link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
    embed = discord.Embed(
        title="Nouveau record de meilleur poulet !",
        description=f"**{player}** a atteint **{strength}%** de probabilite de victoire.",
        color=discord.Color.gold(),
        url=link,
        timestamp=message.created_at,
    )
    embed.add_field(name="Joueur", value=player)
    embed.add_field(name="Nouveau record", value=f"{strength}%")
    embed.add_field(name="Ancien record", value=f"{previous_best}%" if previous_best else "aucun")
    embed.add_field(name="Message", value=f"[Voir le combat]({link})", inline=False)

    try:
        await channel.send(content="@everyone", embed=embed)
    except discord.HTTPException:
        log.exception("Echec de l'annonce du nouveau record dans le salon %d", RECORD_CHANNEL_ID)


async def sync_to_sheets() -> None:
    """Pousse vers Sheets tout ce qui est en attente en local (appelee par la
    boucle periodique sync_loop, et une fois de plus hors-cadence a la fin de
    run_backfill() pour un retour immediat). Chaque etape est isolee: l'echec
    d'une des trois ne bloque pas les autres, et les lignes concernees restent
    simplement non-synchronisees pour etre retentees au prochain appel - pas
    de perte possible, juste un delai."""
    try:
        await _flush_stats()
    except gspread.exceptions.APIError:
        log.exception("Echec de synchronisation de Stats vers Sheets (quota Sheets API ?)")
    try:
        await _flush_bank()
    except gspread.exceptions.APIError:
        log.exception("Echec de synchronisation de Bank vers Sheets (quota Sheets API ?)")
    try:
        await _update_overview_impl()
    except gspread.exceptions.APIError:
        log.exception("Echec de la mise a jour d'Overview (quota Sheets API ?)")


async def _flush_stats() -> None:
    """Pousse les lignes stats non synchronisees vers la feuille Stats. Une
    ligne deja presente sur Sheets (sheet_row connu - typiquement une
    correction de /backfill) est corrigee sur place via batch_update plutot
    que re-ajoutee, pour ne jamais dupliquer une ligne existante.

    Les lectures/ecritures SQLite restent synchrones sur le thread principal
    (rapides, locales) ; seuls les appels reseau gspread (batch_update/
    append_rows, potentiellement plusieurs centaines de ms a quelques
    secondes) sont deportes via asyncio.to_thread. Sans ca, ces appels
    bloquent toute la boucle asyncio pendant leur duree - vecu en prod: une
    interaction Discord (/audit) arrivee pendant un batch_update a expire
    (>3s) avant meme que defer() ait pu s'executer, avec un "Unknown
    interaction" en consequence."""
    to_correct = _db.execute(
        "SELECT rowid, message_id, timestamp, player, result, gain, strength, server, player_id, sheet_row "
        "FROM stats WHERE synced = 0 AND sheet_row IS NOT NULL ORDER BY rowid"
    ).fetchall()
    to_append = _db.execute(
        "SELECT rowid, message_id, timestamp, player, result, gain, strength, server, player_id "
        "FROM stats WHERE synced = 0 AND sheet_row IS NULL ORDER BY rowid"
    ).fetchall()

    if not to_correct and not to_append:
        return

    if to_correct:
        updates = [
            {
                "range": f"A{sheet_row}:H{sheet_row}",
                "values": [[timestamp, player, result, gain, strength, message_id, server, player_id]],
            }
            for _rowid, message_id, timestamp, player, result, gain, strength, server, player_id, sheet_row in to_correct
        ]
        await asyncio.to_thread(_sheet.batch_update, updates, value_input_option="USER_ENTERED")
        _db.executemany("UPDATE stats SET synced = 1 WHERE rowid = ?", [(r[0],) for r in to_correct])
        _db.commit()
        log.info("%d ligne(s) Stats corrigee(s) sur Sheets.", len(to_correct))

    if to_append:
        values = [
            [timestamp, player, result, gain, strength, message_id, server, player_id]
            for _rowid, message_id, timestamp, player, result, gain, strength, server, player_id in to_append
        ]
        response = await asyncio.to_thread(_sheet.append_rows, values, value_input_option="USER_ENTERED")
        start_row = _first_appended_row(response)
        for offset, row in enumerate(to_append):
            rowid = row[0]
            _db.execute("UPDATE stats SET synced = 1, sheet_row = ? WHERE rowid = ?", (start_row + offset, rowid))
        _db.commit()
        log.info("%d ligne(s) Stats ajoutee(s) sur Sheets.", len(to_append))


def _first_appended_row(append_response: dict) -> int:
    updated_range = append_response["updates"]["updatedRange"]
    match = APPEND_ROW_RE.search(updated_range)
    return int(match.group(1))


async def _flush_bank() -> None:
    """Pousse les lignes bank non synchronisees. Pas de notion de correction
    ici (Bank est temps-reel uniquement, pas couvert par /backfill), donc
    toujours un simple ajout."""
    rows = _db.execute(
        "SELECT rowid, message_id, timestamp, player, cash, banque, total, server "
        "FROM bank WHERE synced = 0 ORDER BY rowid"
    ).fetchall()
    if not rows:
        return

    values = [
        [timestamp, player, cash, banque, total, message_id, server]
        for _rowid, message_id, timestamp, player, cash, banque, total, server in rows
    ]
    await asyncio.to_thread(_bank_sheet.append_rows, values, value_input_option="USER_ENTERED")
    _db.executemany("UPDATE bank SET synced = 1 WHERE rowid = ?", [(r[0],) for r in rows])
    _db.commit()
    log.info("%d ligne(s) Bank ajoutee(s) sur Sheets.", len(rows))


async def _update_overview_impl() -> None:
    """Recalcule la feuille Overview (une ligne par joueur + une ligne 'Total')
    a partir de l'ensemble des lignes de la table SQLite stats (source de
    verite - toujours a jour, contrairement a une lecture de la feuille Sheets
    qui ne refleterait que ce qui a deja ete synchronise). Regroupe par ID
    Discord (colonne player_id) plutot que par pseudo affiche, car deux
    joueurs differents peuvent partager le meme pseudo/surnom sur un serveur -
    les lignes sans ID (anterieures a son ajout) se replient sur un
    regroupement par pseudo, corrige des qu'un /backfill est relance pour leur
    retro-attribuer un ID."""
    stats: dict[str, dict[str, int]] = {}
    names: dict[str, str] = {"total": "Total"}

    def entry(key: str) -> dict[str, int]:
        return stats.setdefault(key, {"total": 0, "defeats": 0, "wins": 0, "best_percent": 0, "profit": 0})

    for player, result, gain, strength, player_id in _db.execute(
        "SELECT player, result, gain, strength, player_id FROM stats"
    ):
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
            if result == "Victoire" and strength.isdigit():
                data["best_percent"] = max(data["best_percent"], int(strength))

    # Fusionne chaque bucket "name:" (lignes sans ID - anterieures a son ajout,
    # ou un +cf que find_command_info n'a pas retrouve) dans le bucket "id:"
    # du meme joueur des qu'un ID est connu pour ce pseudo. Sans ca, les deux
    # buckets restent des cles separees ci-dessous et se disputent la meme
    # ligne Overview existante (meme pseudo) via le repli par nom: la
    # deuxieme a s'y presenter est bloquee par claimed_name_rows et part sur
    # une toute nouvelle ligne - deux lignes "cheval_2_3" au lieu d'une, dont
    # une ne recoit plus jamais les combats nouvellement ID-tagged (figee).
    # VLOOKUP/QUERY sur les feuilles "User monitor" trouvent alors la
    # premiere des deux, potentiellement perimee. Vecu en prod: 4 joueurs
    # concernes des le premier cycle post-migration SQLite.
    name_to_id_key: dict[str, str] = {}
    for key in stats:
        if key.startswith("id:"):
            name_to_id_key.setdefault(names[key].lower(), key)

    for key in [k for k in stats if k.startswith("name:")]:
        target_key = name_to_id_key.get(key[len("name:"):])
        if target_key is None:
            continue
        source = stats.pop(key)
        target = entry(target_key)
        for field in ("total", "defeats", "wins", "profit"):
            target[field] += source[field]
        target["best_percent"] = max(target["best_percent"], source["best_percent"])
        del names[key]

    overview_rows = await asyncio.to_thread(_overview_sheet.get_all_values)
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
        await asyncio.to_thread(_overview_sheet.batch_update, updates, value_input_option="USER_ENTERED")
    if new_rows:
        await asyncio.to_thread(_overview_sheet.append_rows, new_rows, value_input_option="USER_ENTERED")


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


async def _update_backfill_status(
    status: str, channel: str = "", message_timestamp: str = "", corrected: int = 0, added: int = 0
) -> None:
    """Ecrit l'etat courant d'un /backfill dans l'onglet BACKFILL_SHEET_TAB -
    consultable a tout moment (par un humain ou par Claude via l'API Sheets)
    sans acces aux logs du process, utile vu la duree potentielle d'un run
    complet (plusieurs heures sur un historique de cette taille)."""
    now = datetime.now(TIMEZONE).isoformat(timespec="seconds")
    row = [status, channel, message_timestamp, corrected, added, now]
    try:
        await asyncio.to_thread(_backfill_sheet.update, "A2:F2", [row], value_input_option="USER_ENTERED")
    except gspread.exceptions.APIError:
        log.exception("Echec de l'ecriture du statut de backfill (quota Sheets API ?)")


async def run_backfill(client: discord.Client) -> tuple[int, int]:
    """Rescanne l'historique des salons surveilles (et leurs fils) pour corriger
    les lignes existantes (Resultat/Gain/Probabilite) et ajouter les combats jamais
    logues, dans SQLite (source de verite). Declenche ensuite un sync_to_sheets()
    immediat pour que l'admin voie le resultat tout de suite. Retourne
    (lignes corrigees, lignes ajoutees)."""
    await ensure_header()
    existing_ids = {row[0] for row in _db.execute("SELECT message_id FROM stats")}
    channels = _watched_channels(client)
    if not channels:
        log.warning("Aucun salon accessible trouve.")
        await _update_backfill_status("Erreur (aucun salon accessible)")
        return 0, 0

    corrected = 0
    added = 0
    last_win_percent: dict[str, str] = {}
    last_progress = time.monotonic()
    last_heartbeat = time.monotonic()
    await _update_backfill_status("En cours (recuperation)")

    try:
        for channel in channels:
            subchannels = await _channel_group(channel)
            log.info("Analyse du salon #%s (%d fil(s) inclus)...", channel.name, len(subchannels) - 1)

            messages = []
            for sub in subchannels:
                async for m in sub.history(limit=None, oldest_first=True):
                    messages.append(m)
                    now = time.monotonic()
                    m_timestamp = m.created_at.astimezone(TIMEZONE).isoformat(timespec="seconds")
                    if now - last_progress >= PROGRESS_INTERVAL:
                        log.info("... recuperation: #%s, message du %s", sub.name, m_timestamp)
                        last_progress = now
                    if now - last_heartbeat >= BACKFILL_HEARTBEAT_INTERVAL:
                        await _update_backfill_status("En cours (recuperation)", sub.name, m_timestamp, corrected, added)
                        last_heartbeat = time.monotonic()
            messages.sort(key=lambda m: m.id)

            for message in messages:
                now = time.monotonic()
                m_timestamp = message.created_at.astimezone(TIMEZONE).isoformat(timespec="seconds")
                if now - last_progress >= PROGRESS_INTERVAL:
                    log.info("... traitement: #%s, message du %s", message.channel.name, m_timestamp)
                    last_progress = now
                if now - last_heartbeat >= BACKFILL_HEARTBEAT_INTERVAL:
                    await _update_backfill_status(
                        "En cours (traitement)", message.channel.name, m_timestamp, corrected, added
                    )
                    last_heartbeat = time.monotonic()

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

                    if msg_id in existing_ids:
                        corrected += 1
                    else:
                        added += 1

                    # ON CONFLICT ne touche pas sheet_row: une correction garde la
                    # ligne Sheets qu'elle a deja (repoussee en place par
                    # _flush_stats()), un ajout part de sheet_row=NULL (nouvelle
                    # ligne Sheets a la prochaine synchro).
                    _db.execute(
                        "INSERT INTO stats (message_id, timestamp, player, result, gain, strength, server, player_id, synced) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0) "
                        "ON CONFLICT(message_id) DO UPDATE SET "
                        "timestamp=excluded.timestamp, player=excluded.player, result=excluded.result, "
                        "gain=excluded.gain, strength=excluded.strength, server=excluded.server, "
                        "player_id=excluded.player_id, synced=0",
                        (msg_id, timestamp, player, result, gain, strength, server, id_value),
                    )
    except Exception:
        _db.commit()
        await _update_backfill_status(f"Erreur ({corrected} corrigee(s), {added} ajoutee(s) avant l'echec)")
        raise

    _db.commit()
    if corrected or added:
        log.info("%d ligne(s) corrigee(s), %d ligne(s) ajoutee(s) (SQLite).", corrected, added)
        await sync_to_sheets()
        _load_best_strength()
    else:
        log.info("Aucune ligne a corriger ou ajouter.")

    await _update_backfill_status("Termine", corrected=corrected, added=added)
    return corrected, added


def build_stats_embed(server: str) -> discord.Embed:
    rows = _db.execute("SELECT player, result, strength FROM stats WHERE server = ?", (server,)).fetchall()
    wins = [row for row in rows if row[1] == "Victoire"]
    losses = [row for row in rows if row[1] == "Defaite"]

    total_counts = Counter(row[0] for row in rows)
    win_counts = Counter(row[0] for row in wins)
    winrates = {player: win_counts[player] / total * 100 for player, total in total_counts.items()}
    best_winrate = max(winrates.items(), key=lambda item: item[1], default=None)
    worst_winrate = min(winrates.items(), key=lambda item: item[1], default=None)

    highest_prob_win = max(
        (row for row in wins if row[2].isdigit()),
        key=lambda row: int(row[2]),
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
        value=f"{highest_prob_win[0]} ({highest_prob_win[2]}%)" if highest_prob_win else "Aucun",
        inline=False,
    )
    return embed


def build_audit_embed(server: str) -> discord.Embed:
    """Etat de SQLite (source de verite) pour ce serveur, interroge directement
    - pas via la feuille Sheets, qui n'est qu'un miroir synchronise par lots et
    peut donc etre en retard ou masquer une divergence."""
    total = _db.execute("SELECT COUNT(*) FROM stats WHERE server = ?", (server,)).fetchone()[0]
    missing_id = _db.execute(
        "SELECT COUNT(*) FROM stats WHERE server = ? AND player_id = ''", (server,)
    ).fetchone()[0]
    pending_stats = _db.execute(
        "SELECT COUNT(*) FROM stats WHERE server = ? AND synced = 0", (server,)
    ).fetchone()[0]
    pending_bank = _db.execute(
        "SELECT COUNT(*) FROM bank WHERE server = ? AND synced = 0", (server,)
    ).fetchone()[0]
    date_range = _db.execute(
        "SELECT MIN(timestamp), MAX(timestamp) FROM stats WHERE server = ?", (server,)
    ).fetchone()
    top_missing = _db.execute(
        "SELECT s1.player, COUNT(*) AS missing, "
        "(SELECT COUNT(*) FROM stats s2 WHERE s2.player = s1.player AND s2.server = s1.server) AS total "
        "FROM stats s1 WHERE s1.server = ? AND s1.player_id = '' "
        "GROUP BY s1.player ORDER BY missing DESC LIMIT 5",
        (server,),
    ).fetchall()

    embed = discord.Embed(title=f"Audit SQLite - {server}", color=discord.Color.blue())
    embed.add_field(name="Combats loggues", value=str(total), inline=True)
    pct = f"{missing_id / total * 100:.1f}%" if total else "0%"
    embed.add_field(name="Sans ID Joueur", value=f"{missing_id} ({pct})", inline=True)
    embed.add_field(
        name="En attente vers Sheets",
        value=f"{pending_stats} combat(s), {pending_bank} +bal",
        inline=True,
    )
    if date_range and date_range[0]:
        embed.add_field(name="Periode couverte", value=f"{date_range[0]}\n-> {date_range[1]}", inline=False)
    if top_missing:
        lines = "\n".join(f"{player} : {missing}/{total_p}" for player, missing, total_p in top_missing)
        embed.add_field(name="Top joueurs sans ID", value=lines, inline=False)
    return embed


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
_backfill_lock = asyncio.Lock()


@tasks.loop(seconds=SYNC_INTERVAL)
async def sync_loop():
    await sync_to_sheets()


async def _sync_guild_commands(guild: discord.Guild) -> None:
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)


@bot.event
async def on_ready():
    await ensure_header()
    if not sync_loop.is_running():
        sync_loop.start()
    _load_best_strength()
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


@bot.tree.command(name="audit", description="Verifie l'etat de la base SQLite (source de verite) pour ce serveur")
async def audit_command(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Cette commande doit etre utilisee dans un serveur.", ephemeral=True)
        return

    await interaction.response.defer()
    embed = build_audit_embed(interaction.guild.name)
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
                    timestamp = message.created_at.astimezone(TIMEZONE).isoformat(timespec="seconds")
                    server = message.guild.name if message.guild else ""
                    _db.execute(
                        "INSERT OR IGNORE INTO bank (message_id, timestamp, player, cash, banque, total, server, synced) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                        (str(message.id), timestamp, player, cash, bank_amount, total, server),
                    )
                    _db.commit()
                    log.info("Bank: %s - cash=%s banque=%s total=%s", player, cash, bank_amount, total)
            continue

        player, result, gain, strength = parsed
        bet, player_id = await find_command_info(message, player)

        if result == "Defaite":
            if bet:
                gain = f"-{bet}"
            if not strength:
                strength = last_win_strength(player)

        timestamp = message.created_at.astimezone(TIMEZONE).isoformat(timespec="seconds")
        server = message.guild.name if message.guild else ""

        # Ecriture locale uniquement ici (fiable, pas de reseau) - la boucle
        # sync_loop se charge de pousser vers Sheets par lots. C'est ce qui
        # evite qu'un hoquet reseau/quota Sheets ne fasse disparaitre un combat:
        # avant, ce append_row direct vers Sheets n'avait aucun retry.
        _db.execute(
            "INSERT OR IGNORE INTO stats (message_id, timestamp, player, result, gain, strength, server, player_id, synced) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (str(message.id), timestamp, player, result, gain, strength, server, str(player_id) if player_id else ""),
        )
        _db.commit()
        log.info("%s - %s - gain=%s - force=%s%%", player, result, gain, strength)

        if result == "Victoire" and strength.isdigit():
            await _maybe_announce_record(message, player, server, int(strength))


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
