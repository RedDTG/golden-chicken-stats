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

There is no test suite, linter, or build step in this repo currently.

## Architecture

Everything lives in `bot.py` and runs as one process with no persistence besides the Google Sheet itself:

1. **Startup**: env vars are read, a `gspread` client is authorized against `GOOGLE_SHEET_ID`/`GOOGLE_SHEET_TAB`, and `on_ready` calls `ensure_header()` to (re)write the header row (`HEADER`) if it doesn't already match.
2. **Message filtering** (`on_message`): messages are discarded unless they're in `WATCH_CHANNEL_ID` (when set — otherwise any channel is accepted), from a bot account (optionally matching `UNBELIEVABOAT_ID`), and contain embeds.
3. **Parsing** (`parse_cockfight_embed`): text-matches the embed description for `"lost the fight"` / `"won the fight"` (case-insensitive), pulling the player name from `embed.author.name`, the payout from the `WIN_GAIN_RE` regex on a win (matched against the digits immediately preceding "richer", since UnbelievaBoat's custom currency emoji embeds a numeric snowflake ID between "made you" and the amount), and the win-probability percentage from `embed.footer.text` via `PERCENT_RE` (not the description, and not a regular embed field). Returns `None` when the embed isn't a recognized cockfight result.
4. **Bet lookup on a loss**: the loss embed never states the wagered amount, so on a `"Defaite"`, `find_bet_amount()` walks the channel history backwards (up to 10 messages before the result) looking for the player's own `+cf <amount>` command message (matched via `BET_RE`, and matched to the player by comparing `embed.author.name` — the raw Discord username — against both the message author's username and display name, since UnbelievaBoat's embed author name doesn't necessarily match `Member.display_name`). If found, `gain` is set to the negative bet amount (e.g. `-100`); otherwise it stays empty.
5. **Probability estimate on a loss**: the loss embed also never states a win-probability (no footer at all), so `last_win_strength()` (bot.py) / the in-memory `last_win_percent` map (backfill.py) looks up that player's most recent logged `"Victoire"` row and uses its Probabilite + 1 as an estimate for the loss row, or `50` if the player has no prior win on record (including their very first logged result being a loss). This is a deliberate approximation, not a value read from Discord.
6. **Sheet append**: each parsed result becomes one row (`timestamp, player, result, gain, strength, message_id, server`) appended with `append_row(..., value_input_option="USER_ENTERED")`, where `server` is `message.guild.name`. The Serveur column was added at the end of `HEADER` (not inserted between existing columns) specifically to avoid shifting the columns of rows already present in a live sheet.

Because parsing is purely regex/string-matching against UnbelievaBoat's embed text (and the player's own bet command), any wording change in that bot's cockfight messages requires updating `WIN_GAIN_RE`, `PERCENT_RE`, `BET_RE`, or the `"lost the fight"`/`"won the fight"` checks in `parse_cockfight_embed`.

## Backfill

`backfill.py` is a one-off script (run manually with `python backfill.py`) that reuses `bot.py`'s config, sheet client, and `parse_cockfight_embed` to rescan a channel's full history: it corrects the Resultat/Gain/Probabilite columns of rows already logged (matched by Message ID) and appends any cockfight results that were never logged. Useful after a parsing bug fix or after the bot was offline for a while. It needs the "Read Message History" permission on the watched channel(s). Because it processes history in chronological order, it rebuilds the "last win % per player" state as it goes rather than querying the sheet (new rows aren't written until the very end).
