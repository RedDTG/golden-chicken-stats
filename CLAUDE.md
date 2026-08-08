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
3. **Parsing** (`parse_cockfight_embed`): text-matches the embed description for `"lost the fight"` / `"won the fight"` (case-insensitive), pulling the player name from `embed.author.name`, the payout from the `WIN_GAIN_RE` regex on a win, and the win-probability percentage from the same description text via `PERCENT_RE` (the percentage line is part of the description, not a separate embed field). Returns `None` when the embed isn't a recognized cockfight result.
4. **Sheet append**: each parsed result becomes one row (`timestamp, player, result, gain, strength, message_id`) appended with `append_row(..., value_input_option="USER_ENTERED")`.

Because parsing is purely regex/string-matching against UnbelievaBoat's embed text, any wording change in that bot's cockfight messages requires updating `WIN_GAIN_RE`, `PERCENT_RE`, or the `"lost the fight"`/`"won the fight"` checks in `parse_cockfight_embed`.

## Backfill

`backfill.py` is a one-off script (run manually with `python backfill.py`) that reuses `bot.py`'s config, sheet client, and `parse_cockfight_embed` to rescan a channel's full history: it corrects the Resultat/Gain/Probabilite columns of rows already logged (matched by Message ID) and appends any cockfight results that were never logged. Useful after a parsing bug fix or after the bot was offline for a while. It needs the "Read Message History" permission on the watched channel(s).
