<a href="https://discord.gg/wRCgB7vBQv">
    <img src="https://img.shields.io/discord/811542332678996008?color=7289DA&label=Support&logo=discord&style=for-the-badge" alt="Discord">
</a>

# Vocard Bot (custom fork)

Vocard is a highly customizable Discord music bot, designed to deliver a user-friendly experience. It offers support for a wide range of streaming platforms including Youtube, Soundcloud, Spotify, Twitch, and more.

This is a personal fork of [ChocoMeow/Vocard](https://github.com/ChocoMeow/Vocard) that adds
**TTS song announcements** and a one-command Docker development stack. Day-to-day work happens on
the `beta` branch.

## Fork additions

### 🔊 TTS song announcements

The bot can announce the next song in the voice channel using a local TTS server — either
[Piper](https://github.com/OHF-Voice/piper1-gpl) or the more natural-sounding
[PocketTTS](https://github.com/kyutai-labs/pocket-tts), picked with one settings key. Two
per-guild modes:

* **Simple** — a template such as `Up next: @@track_name@@ by @@track_author@@`, using the same
  placeholder system as the music controller
* **AI** — text written by any OpenAI-compatible endpoint (OpenAI or a local
  [Ollama](https://ollama.com)), with a configurable persona and temperature

Features:

* **True overlay** — running on [NodeLink](https://github.com/PerformanC/NodeLink) instead of
  Lavalink, the announcement plays *over* the outgoing song's outro like a real radio DJ, with
  the music ducked underneath. Falls back to fade mode automatically on servers without a mixer
* **Smooth transitions** — the clip is generated *before* the song ends and the music fades down
  into it, so there is no gap and no abrupt cut
* **Frequency and cooldown** — announce every Nth song, and/or at most once every X minutes
* **`@@track_genre@@`** — real genre data via the Spotify API, cached per artist
* **Fail-open** — if the TTS server, the AI endpoint or the network misbehaves, the song simply
  plays without an announcement and a warning is logged; playback is never blocked
* **Voice tuning** — `/piper` retunes the live voice from Discord: Piper's speed, expressiveness
  and cadence, or PocketTTS's 26 built-in voices, without restarting anything

Configure per guild with `/settings announce` (requires Manage Server). Full setup and tuning
guide: **[docs/tts-announce.md](docs/tts-announce.md)**.

### 🐳 Docker development stack

`docker-compose.dev.yml` brings up the bot, [NodeLink](https://github.com/PerformanC/NodeLink)
(a Lavalink-v4-compatible server with built-in sources and an audio mixer), MongoDB, Piper TTS,
a YouTube cipher service and the [dashboard](https://github.com/ChocoMeow/Vocard-Dashboard) —
all wired together by service name. PocketTTS is there too, behind a `--profile pocket` flag so
its model is only downloaded if you ask for it.

```bash
cp .env.example .env                                  # fill in TOKEN and CLIENT_ID
cp dashboard/settings.example.json dashboard/settings.json
docker compose -f docker-compose.dev.yml up -d --build
```

Prefer Lavalink? `docker-compose.lavalink.yml` is the same stack with Lavalink (plus the
youtube-plugin and LavaSrc for Spotify) instead of NodeLink.

`settings.json` is created from `settings.docker.json` on first boot, so no config editing is
needed to get started. The project directory is mounted into the bot container, so code changes
only need `docker compose restart vocard` (rebuild only when `requirements.txt` changes).

## Features

* Fast song loading
* Works with slash and message commands
* Lightweight design
* Smooth playback
* Clean and nice interface
* Supports many music platforms (YouTube, SoundCloud, Spotify, Apple Music etc.)
* Built-in playlist support
* Fully customizable settings
* Lyrics support
* Various sound effects
* Multiple languages available
* Easy to update
* Supports docker
* [One Click Installer](https://github.com/ChocoMeow/Vocard-Installer)
* [Premium dashboard](https://github.com/ChocoMeow/Vocard-Dashboard)

## Screenshot
![features](https://github.com/user-attachments/assets/2a1baf75-d1c8-41d1-a66f-7011e96d5feb)

## Requirements
* [Python 3.11+](https://www.python.org/downloads/)
* [NodeLink](https://github.com/PerformanC/NodeLink) or a
  [Lavalink Server (4.0.0+)](https://github.com/lavalink-devs/Lavalink) — both included in the
  Docker stacks; NodeLink is required for overlay announcements
* Optional, for announcements: a [Piper](https://github.com/OHF-Voice/piper1-gpl) or
  [PocketTTS](https://github.com/kyutai-labs/pocket-tts) HTTP server (both included in the
  Docker stack)

## Setup

For the Docker stack, see [Docker development stack](#-docker-development-stack) above. To run the
bot directly, follow the upstream [Setup Page](https://docs.vocard.xyz/latest/bot/setup).

## Development

```bash
pip install -r requirements.txt
python tests/runner.py          # static checks; no token, database or network needed
python main.py
```

Tests enforce that every file in `langs/` matches `EN.json` exactly and that every language key
used in code exists — run them after touching any user-facing string. See
[CLAUDE.md](CLAUDE.md) for the architecture overview.

## Need Help?
Join the [Vocard Support Discord](https://discord.gg/wRCgB7vBQv) for help or questions about
upstream Vocard. Issues specific to this fork belong in this repository.
