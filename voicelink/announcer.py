"""MIT License

Copyright (c) 2023 - present Vocard Development

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
import aiohttp

from aiohttp import web
from discord.ext import commands
from typing import TYPE_CHECKING, Optional

from .placeholders import PlayerPlaceholder
from .spotify import SpotifyGenreClient

if TYPE_CHECKING:
    from .player import Player
    from .objects import Track

logger = logging.getLogger("vocard.announcer")


class AnnounceServer:
    """In-memory WAV store served over HTTP so Lavalink can fetch generated clips.

    Entries are removed by a TTL sweeper, not on first fetch — Lavalink requests
    the URL more than once (probe during loadtracks, then playback).
    """

    def __init__(self, host: str, port: int, public_url: str, ttl: int = 300):
        self._host: str = host
        self._port: int = port
        self._public_url: str = public_url.rstrip("/")
        self._ttl: int = ttl
        self._store: dict[str, tuple[bytes, float]] = {}
        self._runner: Optional[web.AppRunner] = None
        self._sweeper: Optional[asyncio.Task] = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("GET", "/announce/{token}", self._handle)
        app.router.add_route("HEAD", "/announce/{token}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        self._sweeper = asyncio.create_task(self._sweep())
        logger.info(f"Announce server listening on {self._host}:{self._port} (public: {self._public_url})")

    async def stop(self) -> None:
        if self._sweeper:
            self._sweeper.cancel()
            self._sweeper = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._store.clear()

    def put(self, wav: bytes) -> str:
        token = uuid.uuid4().hex
        self._store[token] = (wav, time.time())
        return f"{self._public_url}/announce/{token}.wav"

    async def _handle(self, request: web.Request) -> web.Response:
        token = request.match_info["token"].removesuffix(".wav")
        entry = self._store.get(token)
        if not entry:
            return web.Response(status=404)
        return web.Response(body=entry[0], content_type="audio/wav")

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(60)
            cutoff = time.time() - self._ttl
            for token in [t for t, (_, created) in self._store.items() if created < cutoff]:
                self._store.pop(token, None)


class PiperClient:
    """Minimal client for the piper-tts HTTP server: POST JSON to /synthesize, receive WAV bytes."""

    def __init__(self, url: str, voice: Optional[str] = None, timeout: int = 10, options: Optional[dict] = None):
        url = url.rstrip("/")
        if not url.endswith("/synthesize"):
            url += "/synthesize"
        self._url: str = url
        self._voice: Optional[str] = voice
        self._timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(total=timeout)
        # Per-request synthesis overrides, e.g. length_scale, noise_scale,
        # length_w_scale, speaker_id.
        self._options: dict = options or {}

    async def synthesize(self, text: str) -> Optional[bytes]:
        payload = {**self._options, "text": text}
        if self._voice:
            payload["voice"] = self._voice

        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(self._url, json=payload) as resp:
                if resp.status != 200:
                    logger.warning(f"Piper returned status {resp.status} for synthesis request.")
                    return None
                return await resp.read()


class AIClient:
    """Client for an OpenAI-compatible chat completions endpoint (OpenAI, Ollama, etc.)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        timeout: int = 10,
        temperature: Optional[float] = None
    ):
        self._url: str = base_url.rstrip("/") + "/chat/completions"
        self._model: str = model
        self._api_key: Optional[str] = api_key or os.getenv("OPENAI_API_KEY")
        self._timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(total=timeout)
        self._temperature: Optional[float] = temperature

    async def generate(
        self,
        prompt: str,
        *,
        persona: Optional[str] = None,
        temperature: Optional[float] = None
    ) -> Optional[str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        messages = []
        if persona:
            messages.append({"role": "system", "content": persona})
        messages.append({"role": "user", "content": prompt})

        body = {"model": self._model, "messages": messages}

        if (temp := temperature if temperature is not None else self._temperature) is not None:
            body["temperature"] = temp

        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(self._url, json=body, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"AI endpoint returned status {resp.status} for announcement request.")
                    return None
                data = await resp.json()
                try:
                    return data["choices"][0]["message"]["content"].strip()
                except (KeyError, IndexError, AttributeError):
                    logger.warning(f"AI endpoint returned an unexpected response shape: {data}")
                    return None


class Announcer:
    """Builds TTS announcement clips playable through Lavalink.

    Every failure path returns None so the caller falls back to playing the
    track without an announcement.
    """

    def __init__(self, bot: commands.Bot, settings: dict):
        self._bot: commands.Bot = bot

        piper_cfg = settings.get("piper", {})
        server_cfg = settings.get("server", {})
        ai_cfg = settings.get("ai", {})
        timeouts = settings.get("timeouts", {})

        self._server = AnnounceServer(
            host=server_cfg.get("host", "0.0.0.0"),
            port=server_cfg.get("port", 8100),
            public_url=server_cfg.get("public_url", "http://127.0.0.1:8100")
        )
        self._piper = PiperClient(
            url=piper_cfg.get("url", "http://localhost:5000"),
            voice=piper_cfg.get("voice"),
            timeout=timeouts.get("piper", 10),
            options=piper_cfg.get("options")
        )
        self._ai = AIClient(
            base_url=ai_cfg.get("base_url", "https://api.openai.com/v1"),
            model=ai_cfg.get("model", "gpt-4o-mini"),
            api_key=ai_cfg.get("api_key"),
            timeout=timeouts.get("ai", 10),
            temperature=ai_cfg.get("temperature")
        )

        spotify_cfg = settings.get("spotify", {})
        self._spotify = SpotifyGenreClient(
            client_id=spotify_cfg.get("client_id"),
            client_secret=spotify_cfg.get("client_secret"),
            timeout=timeouts.get("spotify", 8)
        )

        self._default_template: str = settings.get("default_template", "Up next: @@track_name@@ by @@track_author@@")
        self._default_ai_persona: str = settings.get(
            "default_ai_persona",
            "You are an energetic radio DJ with a warm, concise delivery."
        )
        self._default_ai_prompt: str = settings.get(
            "default_ai_prompt",
            "In one short sentence of at most 25 words, announce the next song: "
            "@@track_name@@ by @@track_author@@, requested by @@track_requester_name@@. "
            "Reply with only the announcement text."
        )
        self._max_text_length: int = settings.get("max_text_length", 300)

    async def start(self) -> None:
        await self._server.start()

    async def stop(self) -> None:
        await self._server.stop()

    async def build_announcement(self, player: Player, track: Track, guild_cfg: dict) -> Optional[Track]:
        try:
            text = await self._render_text(player, track, guild_cfg)
            if not text:
                return None
            text = " ".join(text.split())[:self._max_text_length]

            wav = await self._piper.synthesize(text)
            if not wav:
                return None

            url = self._server.put(wav)
            results = await player.node.get_tracks(url, requester=player.guild.me)
            if not results:
                logger.warning(f"Lavalink could not load announcement clip from {url}.")
                return None
            return results[0]

        except Exception as e:
            logger.warning(f"TTS announcement failed for guild {player.guild.id}, playing track normally: {e}")
            return None

    async def _render_text(self, player: Player, track: Track, guild_cfg: dict) -> Optional[str]:
        ph = PlayerPlaceholder(self._bot, player, track=track)
        # Static variable - the rv builder below passes non-callables through,
        # so @@track_genre@@ works in both simple and AI mode.
        ph.variables["track_genre"] = await self._spotify.get_genre(track)
        rv = {key: func() if callable(func) else func for key, func in ph.variables.items()}

        if guild_cfg.get("mode", "simple") == "ai":
            prompt = ph.replace(guild_cfg.get("ai_prompt") or self._default_ai_prompt, rv)
            if not prompt:
                return None

            persona = ph.replace(guild_cfg.get("ai_persona") or self._default_ai_persona, rv)
            return await self._ai.generate(
                prompt,
                persona=persona,
                temperature=guild_cfg.get("ai_temperature")
            )

        return ph.replace(guild_cfg.get("template") or self._default_template, rv)
