# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Vocard is a Discord music bot (Python 3.11+, discord.py) that plays audio through a Lavalink v4 server and stores guild settings and user playlists in MongoDB. This repo is a custom fork; day-to-day work happens on the `beta` branch, with PRs targeting `main`.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests (custom runner, not pytest — same command CI uses)
python tests/runner.py

# Run the bot (requires settings.json, MongoDB, and a Lavalink v4 node)
python main.py
```

Tests need no bot token, database, or network — they are static checks. The runner discovers every `tests/test_*.py` that exposes `run() -> bool`; there is no single-test filter, but the suite runs in seconds. To add a test, create `tests/test_<name>.py` with `NAME`, `DESCRIPTION`, and `run()`.

Runtime configuration lives in `settings.json` (gitignored); `settings Example.json` is the template. `function.py` raises at import time if `settings.json` is missing, so the bot and anything importing `function` won't start without it.

## Architecture

Three layers: `main.py` (bootstrap), `cogs/` (commands), and `voicelink/` (a self-contained Lavalink client library plus the bot's domain logic).

**Bootstrap (`main.py`)** builds `Config` from `settings.json`, initializes `LangHandler`, connects `MongoDBHandler`, auto-loads every `.py` in `cogs/`, optionally connects an IPC websocket client to the Vocard-Dashboard, and installs an app-command `Translator` backed by `local_langs/`. The slash-command tree is only synced when `update.py`'s `__version__` differs from the version recorded in `settings.json` — bump that version (or delete the stored one) to force a re-sync after changing command signatures.

**`function.py`** is a small shared-utility module imported everywhere as `func`: the `vocard` logger, `ROOT_DIR`, JSON helpers, and cooldown/alias lookups that read from `Config`. Command cooldowns and aliases are defined in `settings.json`, not in the cog decorators.

**`voicelink/`** — the core package:
- `pool.py` — `Node`/`NodePool`: Lavalink v4 websocket + REST, node selection, track/playlist resolution, YouTube ratelimit token rotation (`ratelimit.py`)
- `player.py` — `Player` (the voice client): playback control, the persistent "music controller" embed message, DJ/vote logic
- `queue.py`, `objects.py` (Track/Playlist), `filters.py` (audio effects), `events.py`, `exceptions.py` (`VoicelinkException` is the base — subclasses are shown to users as-is; anything else is logged as an unexpected error)
- `mongodb.py` — `MongoDBHandler`: class-level (no instances) access to guild settings and user playlists, with caching
- `language.py` — `LangHandler`: preloads `langs/*.json` flattened into dot-separated keys (e.g. `common.errors.unknown`)
- `placeholders.py` — template engine for controller embeds and voice status: `@@variable@@` substitution, `@@t_<lang.key>@@` translation, and `{{cond ?? text}}` conditionals, driven by `default_controller` in settings
- `views/` — discord.py UI components (controller buttons, queue, playlist, search, etc.)
- `ipc/` — websocket client + method handlers for the optional premium dashboard

`Config` behaves as a singleton: `Config(settings_dict)` loads once in `main.py`, and `Config()` anywhere else returns the loaded instance.

**`cogs/`** — command groups: `basic.py` (playback), `playlist.py`, `settings.py`, `effect.py`, `listeners.py` (voicelink event handlers), `task.py` (background timers).

## Localization

Two separate systems:
- `langs/*.json` — bot response strings, fetched per-guild via `LangHandler` (dot-keys, `{0}`-style placeholders)
- `local_langs/*.json` — Discord-native slash-command name/description localization (locale-keyed, e.g. `zh-TW.json`)

The tests enforce that every `langs/*.json` matches `EN.json` exactly (same keys, same `{n}` placeholders) and that every language key referenced in `cogs/` or `voicelink/` exists in `EN.json`. When adding or renaming a user-facing string, update `langs/EN.json` **and all other files in `langs/`**, then run `python tests/runner.py`.
