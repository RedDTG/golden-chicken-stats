# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Discord bot (`bot.py`) that watches one channel for UnbelievaBoat "cockfight" result embeds and logs each fight to a Google Sheet via the Sheets API.

## Setup & running

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in the values
python bot.py
```

Required environment variables (see `.env.example`, loaded via `python-dotenv`):
- `DISCORD_BOT_TOKEN` — bot token from the Discord Developer Portal.
- `WATCH_CHANNEL_ID` (optional) — if set, the only channel the bot listens to; if empty/unset, the bot listens across every channel it can see.
- `UNBELIEVABOAT_ID` (optional) — if set, only embeds authored by this bot ID are parsed; otherwise any bot message in the channel is considered.
- `GOOGLE_CREDENTIALS_FILE` (default `credentials.json`) — Google service-account key file, not committed (gitignored).
- `GOOGLE_CREDENTIALS_JSON` (optional) — the service-account key as a raw JSON string, used instead of `GOOGLE_CREDENTIALS_FILE` when set (takes priority). Useful in deployments (e.g. Coolify) where providing a file is inconvenient.
- `GOOGLE_SHEET_ID` — target spreadsheet ID (from its URL).
- `GOOGLE_SHEET_TAB` (default `Cockfights`) — worksheet/tab name; must already exist in the sheet.
- `OVERVIEW_SHEET_TAB` (default `Overview`) — second worksheet/tab name for the per-player rollup kept up to date by `update_overview()`; must already exist in the sheet.
- `TIMEZONE` (default `Europe/Paris`) — IANA zone name used to render the Horodatage column, via `zoneinfo`. Deliberately independent of the host machine's system timezone (a bare `.astimezone()` on a UTC-configured server/container silently produces `+00:00` timestamps instead of local time — this bit us once, see `tzdata` in `requirements.txt` for the portable data source).

There is no test suite, linter, or build step in this repo currently.

## Architecture

Everything lives in `bot.py` and runs as one process with no persistence besides the Google Sheet itself:

1. **Startup**: env vars are read, a `gspread` client is authorized against `GOOGLE_SHEET_ID`/`GOOGLE_SHEET_TAB`, and `on_ready` calls `ensure_header()` to (re)write the header row (`HEADER`) if it doesn't already match.
2. **Message filtering** (`on_message`): messages are discarded unless they're in `WATCH_CHANNEL_ID` (when set — otherwise any channel is accepted), from a bot account (optionally matching `UNBELIEVABOAT_ID`), and contain embeds.
3. **Parsing** (`parse_cockfight_embed`): text-matches the embed description for `"lost the fight"` / `"won the fight"` (case-insensitive), pulling the player name from `embed.author.name`, the payout from the `WIN_GAIN_RE` regex on a win (matched against the digits immediately preceding "richer", since UnbelievaBoat's custom currency emoji embeds a numeric snowflake ID between "made you" and the amount), and the win-probability percentage from `embed.footer.text` via `PERCENT_RE` (not the description, and not a regular embed field). Returns `None` when the embed isn't a recognized cockfight result.
4. **Bet lookup on a loss**: the loss embed never states the wagered amount, so on a `"Defaite"`, `find_bet_amount()` walks the channel history backwards (up to 10 messages before the result) looking for the player's own `+cf <amount>` command message (matched via `BET_RE`, and matched to the player by comparing `embed.author.name` — the raw Discord username — against both the message author's username and display name, since UnbelievaBoat's embed author name doesn't necessarily match `Member.display_name`). If found, `gain` is set to the negative bet amount (e.g. `-100`); otherwise it stays empty.
5. **Probability estimate on a loss**: the loss embed also never states a win-probability (no footer at all), so `last_win_strength()` (bot.py) / the in-memory `last_win_percent` map (`run_backfill()`) looks up that player's most recent logged `"Victoire"` row and uses its Probabilite + 1 as an estimate for the loss row, or `50` if the player has no prior win on record (including their very first logged result being a loss). This is a deliberate approximation, not a value read from Discord. `last_win_strength()` picks the row with the latest Horodatage (parsed via `datetime.fromisoformat`), not the bottom-most row in the sheet — `run_backfill()` can append older, thread-sourced rows after newer ones already in the sheet, so row position alone isn't chronological.
6. **Sheet append**: each parsed result becomes one row (`timestamp, player, result, gain, strength, message_id, server`) appended with `append_row(..., value_input_option="USER_ENTERED")`, where `server` is `message.guild.name` and `timestamp` is `datetime.now(TIMEZONE)` (real time of processing — not the message's own creation time). The Serveur column was added at the end of `HEADER` (not inserted between existing columns) specifically to avoid shifting the columns of rows already present in a live sheet.

Because parsing is purely regex/string-matching against UnbelievaBoat's embed text (and the player's own bet command), any wording change in that bot's cockfight messages requires updating `WIN_GAIN_RE`, `PERCENT_RE`, `BET_RE`, or the `"lost the fight"`/`"won the fight"` checks in `parse_cockfight_embed`.

## Backfill

The rescan logic itself (`run_backfill()`) lives in `bot.py` so it can be reused from two entry points:

- **`backfill.py`**: a thin one-off script (run manually with `python backfill.py`) that opens its own `discord.Client`, calls `run_backfill(client)` once in `on_ready`, then disconnects.
- **`/backfill` slash command** (`bot.py`): the same rescan triggered from within the already-running bot, as an `app_commands` command registered on `bot.tree` (synced in `on_ready`), gated by `@app_commands.checks.has_permissions(administrator=True)` and an `asyncio.Lock` that rejects a second concurrent run. Since a backfill can run well past Discord's 3-second interaction deadline, it immediately `defer()`s and reports the corrected/added row counts via `followup.send()`. Works in any channel/guild the bot is in, independent of `WATCH_CHANNEL_ID`. The bot's Discord invite link must include the `applications.commands` OAuth2 scope (in addition to `bot`) for slash commands to register at all.

Commands are synced **per-guild only** (`_sync_guild_commands()`: `copy_global_to()` + `sync(guild=...)`), called for every guild in `on_ready` and again in `on_guild_join` for guilds joined later. A global-only sync (`tree.sync()` with no `guild=`) can take up to an hour to show up on Discord clients after adding/changing a command, but combining a global sync *and* per-guild syncs registers each command twice — Discord then shows two separate entries with the same name in the picker, since a global command and a guild-scoped command with identical names are distinct registrations, not automatically deduplicated. `on_ready` also does a one-off `bot.http.bulk_upsert_global_commands(bot.application_id, [])` (bypassing `tree.sync()`, which would also wipe the tree's own global command list that `copy_global_to()` reads from) to clear out any global commands left over from an earlier deploy. Sync failures are logged instead of failing silently.

Either way, `run_backfill()` rescans each watched channel plus all of its threads (active, archived, public and private, via `_channel_group()` — `channel.history()` alone doesn't descend into threads), merges and sorts every message by ID (chronological) before processing, and corrects the Horodatage/Resultat/Gain/Probabilite/Serveur columns of rows already logged (matched by Message ID) while appending any cockfight results that were never logged. Horodatage is recomputed from `message.created_at.astimezone(TIMEZONE)` — the message's real creation time, unlike the real-time bot which logs its own processing time — so re-running the backfill also retroactively fixes any row written under a wrong/inconsistent timezone. Useful after a parsing bug fix or after the bot was offline for a while. It needs the "Read Message History" permission on the watched channel(s). Because it processes history in chronological order, it rebuilds the "last win % per player" state as it goes rather than querying the sheet (new rows aren't written until the very end).

## Stats

`/stats` (open to anyone, no permission gate) builds a Discord embed from `build_stats_embed()`: it filters the sheet's rows to the invoking `interaction.guild.name` (matching the Serveur column — stats are per-server even though one sheet can hold rows from several guilds), then reports combat/win/loss counts, the player with the best and the player with the worst winrate (wins / total games played, via `collections.Counter`; not raw win/loss counts, since those trivially favor whoever has played the most), and the win with the single highest Probabilite value (and who scored it).

## Overview sheet

`update_overview()` keeps a second worksheet (`OVERVIEW_SHEET_TAB`, one row per player plus a `"Total"` aggregate row, columns `Total / Defaites / Victoires / Meilleur % / Rentabilite / Ratio`) in sync with the main sheet: it re-derives per-player and global totals from every row on each call (games played, defeats, wins, the highest Probabilite among that player's wins, Rentabilite = the sum of the Gain column with non-numeric/blank gains counting as 0, and Ratio = wins / games played as a rounded percentage), then matches existing Overview rows by exact string equality on column A — same convention as the Joueur column elsewhere — updating them in place and appending a new row for any player not yet present. Unlike `/stats`, this aggregates across every server in the sheet (the Overview layout has no Serveur split). It's called after every real-time log in `on_message` and once at the end of `run_backfill()` (not per-row during a backfill, to avoid redundant full-sheet recomputation), so it stays current automatically without a dedicated command. Note: because matching is exact-string, a player row must be spelled exactly like the Joueur value logged from `embed.author.name` (Discord username) to be picked up — a manually-typed row with a different spelling/casing won't be matched and a second row will be created instead.
