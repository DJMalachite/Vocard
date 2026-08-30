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

import array
import asyncio
import io
import logging
import os
import sys
import time
import uuid
import wave
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

# Discord voice - and therefore NodeLink's audio mixer - works in 48 kHz
# stereo signed 16-bit PCM, read one 20ms frame at a time.
MIXER_SAMPLE_RATE = 48000
MIXER_CHANNELS = 2
MIXER_SAMPLE_WIDTH = 2
MIXER_FRAME_BYTES = 3840

# Bot root, not voicelink/ - where an uploaded voice-cloning reference clip
# lands on disk. Computed locally rather than imported from function.py to
# avoid a circular import (function.py imports voicelink at module level).
DEFAULT_VOICE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "voice_samples")


def safe_voice_key(key: str) -> str:
    """Strips a voice-sample key down to what's safe as a filename and URL segment."""
    return "".join(c for c in key if c.isalnum() or c in "-_") or "voice"


def repair_wav_header(wav: bytes) -> bytes:
    """Rewrites the RIFF/data sizes of a WAV to match how many bytes arrived.

    A server that streams synthesis has to write the header before it knows how
    long the clip will be, so it declares a placeholder length it can never go
    back and correct - PocketTTS writes one billion frames. Every reader then
    believes the clip is hours long: `wave` reports the bogus frame count, the
    resampler in `to_mixer_pcm` tries to allocate an array for it, and the
    duration stamped on the track is meaningless.

    The audio itself is fine, only the two size fields are wrong, so patch them
    from the real byte count. A WAV that already agrees with itself is returned
    untouched.
    """
    if len(wav) < 44 or wav[0:4] != b"RIFF" or wav[8:12] != b"WAVE":
        return wav

    pos = 12
    while pos + 8 <= len(wav):
        size = int.from_bytes(wav[pos + 4:pos + 8], "little")
        body = pos + 8

        if wav[pos:pos + 4] == b"data":
            actual = len(wav) - body
            if size == actual:
                return wav

            logger.debug(f"Repairing a streamed WAV header: data chunk claims {size} bytes, {actual} arrived.")
            patched = bytearray(wav)
            patched[pos + 4:body] = actual.to_bytes(4, "little")
            patched[4:8] = (len(wav) - 8).to_bytes(4, "little")
            return bytes(patched)

        # Chunks are padded to an even length.
        pos = body + size + (size & 1)

    return wav


def wav_format(wav: bytes) -> Optional[tuple[int, int, int, int]]:
    """Returns (channels, sample width, frame rate, frames) for a PCM WAV, or None."""
    try:
        with wave.open(io.BytesIO(wav)) as handle:
            return (
                handle.getnchannels(),
                handle.getsampwidth(),
                handle.getframerate(),
                handle.getnframes()
            )
    except Exception:
        return None


def wav_duration_ms(wav: bytes) -> Optional[int]:
    """Returns the duration of a PCM WAV in milliseconds, or None."""
    fmt = wav_format(wav)
    if not fmt:
        return None

    _, _, rate, frames = fmt
    if not frames or not rate:
        return None
    return int(frames / rate * 1000)


def _read_samples(wav: bytes, frames: int) -> array.array:
    """Reads a 16-bit WAV's frames as a flat array of native-endian samples."""
    with wave.open(io.BytesIO(wav)) as handle:
        raw = handle.readframes(frames)

    samples = array.array("h")
    samples.frombytes(raw)
    # WAV data is little-endian; array("h") is native.
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def as_percent(value, fallback: int) -> int:
    """Reads a 0-100 setting, falling back when it is missing or nonsense.

    These arrive from settings.json and from guild documents, so neither the
    type nor the range can be taken on trust.
    """
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return fallback


def _boost_to_peak(samples: array.array, target_percent: int) -> array.array:
    """Scales a clip up until its loudest sample sits at `target_percent`.

    Piper normalises what it synthesises to full scale, PocketTTS does not -
    its decoder just clamps to +-1.0 - so the same announcement comes out
    audibly quieter on one engine than the other, and the mixer cannot make up
    the difference: NodeLink's layer volume tops out at 1.0, which is already
    the default.

    Boost only. A clip that is already loud is left exactly as it was, so this
    lifts the quiet engine to match rather than re-levelling everything.
    """
    if target_percent <= 0 or not samples:
        return samples

    peak = max(abs(min(samples)), abs(max(samples)))
    if not peak:
        return samples

    ceiling = 32767 * min(100, target_percent) / 100
    gain = ceiling / peak
    if gain <= 1.0:
        return samples

    logger.debug(f"Boosting announcement clip by {gain:.2f}x to reach {target_percent}% of full scale.")
    return array.array("h", (max(-32768, min(32767, int(sample * gain))) for sample in samples))


def to_mixer_pcm(wav: bytes, loudness: int = 0) -> bytes:
    """Re-renders a PCM WAV as 48 kHz stereo 16-bit, optionally levelled up.

    NodeLink's mixer never converts sample formats. `AudioMixer.mixBuffers`
    adds each layer onto the main track sample by sample, and `readLayerChunks`
    reads 3840 bytes per 20ms frame - both of which assume the layer is already
    48 kHz stereo s16. The TTS engines synthesise at whatever rate their model
    uses (22050 Hz mono for Piper's medium voices, 24000 Hz mono for PocketTTS),
    so passing that output straight through lines the clip up against the wrong
    samples and it comes out as noise rather than speech.

    The clip is also padded out to a whole number of 20ms mixer frames.
    `readLayerChunks` drains a layer 3840 bytes at a time, but only retires it
    once the buffer is *exactly* empty:

        if (layer.ringBuffer.length < safeSize) {
          if (layer.finishedFeeding && layer.ringBuffer.length === 0) {
            if (this.autoCleanup) this.removeLayer(id, 'FINISHED')

    A final part-frame is therefore too small to read but not empty either, so
    the layer is never retired and MixEnded never fires - the music stays
    ducked until the failsafe timer rescues it. A few milliseconds of trailing
    silence is inaudible and keeps the drain landing on zero.

    Converting here keeps the mixer's assumptions true whichever voice is
    configured, and leaves an already-correct clip untouched.

    `loudness` levels the speech up to that percentage of full scale on the
    way through; 0 leaves the engine's own level alone.
    """
    wav = repair_wav_header(wav)

    fmt = wav_format(wav)
    if not fmt:
        logger.warning("The TTS engine did not return a readable PCM WAV; sending the clip through unconverted.")
        return wav

    channels, width, rate, frames = fmt
    if width != MIXER_SAMPLE_WIDTH:
        logger.warning(
            f"The TTS engine returned {width * 8}-bit audio, which cannot be converted for the mixer; "
            "sending the clip through unconverted."
        )
        return wav

    if not frames:
        return wav

    native = (channels, rate) == (MIXER_CHANNELS, MIXER_SAMPLE_RATE)
    aligned = (frames * channels * width) % MIXER_FRAME_BYTES == 0
    if native and aligned and loudness <= 0:
        return wav

    samples = _read_samples(wav, frames)
    # Trust what was actually read over what the header promised: a truncated
    # clip would otherwise size the output buffer from a frame count that never
    # arrived.
    frames = len(samples) // channels
    if not frames:
        return wav

    # Level the speech before resampling, so the gain is worked out from the
    # samples the engine actually produced.
    levelled = _boost_to_peak(samples, loudness)
    if native and aligned and levelled is samples:
        # Right format, right length, and loud enough already.
        return wav
    samples = levelled

    if not native:
        logger.debug(
            f"Converting announcement clip from {rate}Hz/{channels}ch "
            f"to {MIXER_SAMPLE_RATE}Hz/{MIXER_CHANNELS}ch."
        )

        # Collapse to a single channel first. Piper is mono, and averaging
        # keeps a multi-channel voice centred instead of dropping half of it.
        if channels > 1:
            mono = array.array("h", bytes(MIXER_SAMPLE_WIDTH * frames))
            for i in range(frames):
                base = i * channels
                mono[i] = sum(samples[base:base + channels]) // channels
            samples = mono

        # Linear interpolation is plenty for speech, and this runs off the hot
        # path - the transition worker generates the clip well before it is
        # needed.
        out_frames = int(frames * MIXER_SAMPLE_RATE / rate)
        out = array.array("h", bytes(MIXER_SAMPLE_WIDTH * MIXER_CHANNELS * out_frames))
        step = rate / MIXER_SAMPLE_RATE
        last = frames - 1

        for i in range(out_frames):
            pos = i * step
            left = int(pos)
            start = samples[left]
            end = samples[left + 1] if left < last else start
            value = int(start + (end - start) * (pos - left))
            out[2 * i] = value
            out[2 * i + 1] = value

        samples = out

    frame_samples = MIXER_FRAME_BYTES // MIXER_SAMPLE_WIDTH
    if short := len(samples) % frame_samples:
        padding = frame_samples - short
        logger.debug(f"Padding announcement clip with {padding} samples to land on a whole mixer frame.")
        samples.frombytes(bytes(MIXER_SAMPLE_WIDTH * padding))

    if sys.byteorder == "big":
        samples.byteswap()

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(MIXER_CHANNELS)
        handle.setsampwidth(MIXER_SAMPLE_WIDTH)
        handle.setframerate(MIXER_SAMPLE_RATE)
        handle.writeframes(samples.tobytes())

    return buffer.getvalue()


class AnnounceServer:
    """In-memory WAV store served over HTTP so Lavalink can fetch generated clips.

    Entries are removed by a TTL sweeper, not on first fetch — Lavalink requests
    the URL more than once (probe during loadtracks, then playback).
    """

    def __init__(self, host: str, port: int, public_url: str, ttl: int = 300, voice_dir: str = DEFAULT_VOICE_DIR):
        self._host: str = host
        self._port: int = port
        self._public_url: str = public_url.rstrip("/")
        self._ttl: int = ttl
        self._store: dict[str, tuple[bytes, float]] = {}
        self._voice_dir: str = voice_dir
        self._runner: Optional[web.AppRunner] = None
        self._sweeper: Optional[asyncio.Task] = None

    async def start(self) -> None:
        os.makedirs(self._voice_dir, exist_ok=True)

        app = web.Application()
        app.router.add_route("GET", "/announce/{token}", self._handle)
        app.router.add_route("HEAD", "/announce/{token}", self._handle)
        app.router.add_route("GET", "/voices/{key}", self._handle_voice)
        app.router.add_route("HEAD", "/voices/{key}", self._handle_voice)
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

    def save_voice_sample(self, key: str, wav: bytes) -> str:
        """Persists an uploaded voice-cloning reference clip and returns its URL.

        Unlike `put`, this writes to disk rather than the in-memory TTL store:
        the whole point of cloning a voice from an upload, rather than a URL,
        is that a guild shouldn't have to re-upload it after every restart.
        """
        path = os.path.join(self._voice_dir, f"{safe_voice_key(key)}.wav")
        with open(path, "wb") as f:
            f.write(wav)
        return f"{self._public_url}/voices/{safe_voice_key(key)}.wav"

    async def _handle_voice(self, request: web.Request) -> web.Response:
        key = safe_voice_key(request.match_info["key"].removesuffix(".wav"))
        path = os.path.join(self._voice_dir, f"{key}.wav")
        if not os.path.isfile(path):
            return web.Response(status=404)
        return web.FileResponse(path, headers={"Accept-Ranges": "bytes"})

    async def _handle(self, request: web.Request) -> web.Response:
        """Serves a clip, honouring Range requests.

        Range support is not optional: audio servers probe durations and
        resume playback with `Range: bytes=N-`. Answering those with the whole
        file from byte 0 makes a clip restart from the beginning instead of
        finishing.
        """
        token = request.match_info["token"].removesuffix(".wav")
        entry = self._store.get(token)
        if not entry:
            return web.Response(status=404)

        wav = entry[0]
        total = len(wav)
        headers = {"Accept-Ranges": "bytes"}

        requested = self._parse_range(request.headers.get("Range"), total)
        if requested is None:
            return web.Response(body=wav, content_type="audio/wav", headers=headers)

        start, end = requested
        if start >= total:
            headers["Content-Range"] = f"bytes */{total}"
            return web.Response(status=416, headers=headers)

        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        return web.Response(
            status=206,
            body=wav[start:end + 1],
            content_type="audio/wav",
            headers=headers
        )

    @staticmethod
    def _parse_range(header: Optional[str], total: int) -> Optional[tuple[int, int]]:
        """Parses a single byte range. Returns None to serve the whole file."""
        if not header:
            return None

        units, _, spec = header.partition("=")
        if units.strip().lower() != "bytes" or "," in spec:
            return None

        start_text, sep, end_text = spec.strip().partition("-")
        if not sep:
            return None

        try:
            if start_text:
                start = int(start_text)
                end = int(end_text) if end_text else total - 1
            else:
                # Suffix form: bytes=-N asks for the final N bytes.
                start = max(0, total - int(end_text))
                end = total - 1
        except ValueError:
            return None

        if start < 0:
            return None

        # Past the end is a valid request the caller answers with 416, so it
        # must not fall through to the "serve everything" path.
        if start >= total:
            return start, total - 1

        if end < start:
            return None

        return start, min(end, total - 1)

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(100)
            cutoff = time.time() - self._ttl
            for token in [t for t, (_, created) in self._store.items() if created < cutoff]:
                self._store.pop(token, None)
            logger.info(f"Announce server sweep complete, {len(self._store)} clips remain in memory.")

class TTSClient:
    """Base class for the speech engines announcements can be synthesised with.

    Subclasses differ only in the request they build; everything the rest of
    the bot touches - the configured voice, the tunable knobs, and how guild
    overrides layer on top of the bot-wide defaults - lives here.
    """

    # Shown in logs and in /piper show, and the key both the settings block and
    # the timeout are read from.
    NAME: str = "tts"
    DEFAULT_URL: str = "http://localhost:8000"
    DEFAULT_TIMEOUT: int = 10

    # The synthesis knobs this engine accepts alongside the text. Anything
    # outside this list is not forwarded, so a stray guild setting cannot
    # corrupt the request - and switching engines cannot carry a knob over to
    # one that has never heard of it.
    OPTION_KEYS: tuple = ()

    def __init__(self, url: str, voice: Optional[str] = None, timeout: int = 60, options: Optional[dict] = None):
        self._url: str = self._build_url(url)
        self._voice: Optional[str] = voice
        self._timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(total=timeout)
        # Bot-wide synthesis overrides from settings.json, passed through to
        # the engine as configured.
        self._options: dict = dict(options or {})

    @staticmethod
    def _build_url(url: str) -> str:
        """Turns the configured base URL into the synthesis endpoint."""
        return url.rstrip("/")

    @property
    def url(self) -> str:
        return self._url

    @property
    def voice(self) -> Optional[str]:
        return self._voice

    @property
    def options(self) -> dict:
        """A copy, so callers cannot edit the defaults by accident."""
        return dict(self._options)

    def configure(self, voice: Optional[str] = None, options: Optional[dict] = None) -> None:
        """Changes the bot-wide defaults without a restart.

        Only the values actually supplied are touched, so a caller can change
        the speed without having to restate the voice.
        """
        if voice:
            self._voice = voice
        if options:
            self._options.update(options)

    @classmethod
    def filter_options(cls, source: Optional[dict]) -> dict:
        """Picks the recognised synthesis knobs out of a settings dict."""
        return {key: value for key, value in (source or {}).items() if key in cls.OPTION_KEYS}

    def resolve_voice(self, voice: Optional[str]) -> Optional[str]:
        """The voice to synthesise with, given a per-call override."""
        return voice or self._voice

    async def synthesize(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        options: Optional[dict] = None
    ) -> Optional[bytes]:
        """Returns WAV bytes, or None if the engine could not produce them."""
        raise NotImplementedError


class PiperClient(TTSClient):
    """Minimal client for the piper-tts HTTP server: POST JSON to /synthesize, receive WAV bytes."""

    NAME: str = "piper"
    DEFAULT_URL: str = "http://localhost:5000"
    DEFAULT_TIMEOUT: int = 10
    OPTION_KEYS: tuple = ("length_scale", "noise_scale", "length_w_scale", "speaker_id")

    @staticmethod
    def _build_url(url: str) -> str:
        url = url.rstrip("/")
        return url if url.endswith("/synthesize") else url + "/synthesize"

    async def synthesize(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        options: Optional[dict] = None
    ) -> Optional[bytes]:
        # Per-call values win over the bot-wide defaults, which is what makes
        # a guild override an override.
        payload = {**self._options, **self.filter_options(options), "text": text}
        if selected := self.resolve_voice(voice):
            payload["voice"] = selected

        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(self._url, json=payload) as resp:
                logger.info(f"Piper synthesis requested with payload {payload}, got status {resp.status}.")
                if resp.status != 200:
                    logger.warning(f"Piper returned status {resp.status} for synthesis request.")
                    return None
                return await resp.read()


class PocketTTSClient(TTSClient):
    """Client for a `pocket-tts serve` instance.

    PocketTTS (https://github.com/kyutai-labs/pocket-tts) is a 100M-parameter
    CPU model with noticeably more natural delivery than Piper. Its API is a
    single form-encoded POST to /tts which answers with a streamed WAV; there
    are no per-request knobs, because speed and temperature are fixed when the
    server loads its model.
    """

    NAME: str = "pocket"
    DEFAULT_URL: str = "http://localhost:8000"
    # Synthesis runs on the CPU and the first call also has to fetch the voice,
    # so this needs far more headroom than Piper.
    DEFAULT_TIMEOUT: int = 60

    # Voice names built into the server. Anything else has to be a URL it can
    # download the conditioning audio from.
    VOICES: tuple = (
        "alba", "anna", "azelma", "bill_boerst", "caro_davy", "charles", "cosette",
        "eponine", "estelle", "eve", "fantine", "george", "giovanni", "jane",
        "javert", "jean", "juergen", "lola", "marius", "mary", "michael", "paul",
        "peter_yearsley", "rafael", "stuart_bell", "vera"
    )
    VOICE_URL_SCHEMES: tuple = ("http://", "https://", "hf://")

    @staticmethod
    def _build_url(url: str) -> str:
        url = url.rstrip("/")
        return url if url.endswith("/tts") else url + "/tts"

    @classmethod
    def is_valid_voice(cls, voice: str) -> bool:
        return voice in cls.VOICES or voice.startswith(cls.VOICE_URL_SCHEMES)

    def resolve_voice(self, voice: Optional[str] = None) -> Optional[str]:
        """Picks a voice the server will actually accept.

        A guild that chose a Piper voice keeps that setting after the owner
        switches engines, and PocketTTS answers an unknown name with a 400 -
        which would silently cost that guild every announcement. Dropping back
        to the bot default, and then to the server's own, keeps it talking.
        """
        for candidate in (voice, self._voice):
            if not candidate:
                continue
            if self.is_valid_voice(candidate):
                return candidate
            logger.warning(
                f"'{candidate}' is not a PocketTTS voice and is not a http(s):// or hf:// URL, ignoring it. "
                f"Built-in voices: {', '.join(self.VOICES)}."
            )

        return None

    async def synthesize(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        options: Optional[dict] = None
    ) -> Optional[bytes]:
        payload = {"text": text}
        if selected := self.resolve_voice(voice):
            payload["voice_url"] = selected

        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(self._url, data=payload) as resp:
                logger.info(f"PocketTTS synthesis requested with payload {payload}, got status {resp.status}.")
                if resp.status != 200:
                    detail = (await resp.text())[:200]
                    logger.warning(f"PocketTTS returned status {resp.status} for synthesis request: {detail}")
                    return None

                # The response is generated as it is spoken, so the header it
                # opens with promises a length the clip will never have.
                wav = repair_wav_header(await resp.read())

        if not wav:
            logger.warning("PocketTTS returned an empty response for the synthesis request.")
            return None

        return wav


# The engines announce_settings.engine can name, and the settings block each
# one reads its url/voice from.
ENGINES: dict = {
    "piper": PiperClient,
    "pocket": PocketTTSClient,
    "pockettts": PocketTTSClient,
    "pocket-tts": PocketTTSClient,
    "pocket_tts": PocketTTSClient
}


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
                logger.info(f"AI generation requested with body {body}")
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

        server_cfg = settings.get("server", {})
        ai_cfg = settings.get("ai", {})
        timeouts = settings.get("timeouts", {})

        self._server = AnnounceServer(
            host=server_cfg.get("host", "0.0.0.0"),
            port=server_cfg.get("port", 8100),
            public_url=server_cfg.get("public_url", "http://127.0.0.1:8100")
        )

        engine = ENGINES.get(str(settings.get("engine", PiperClient.NAME)).strip().lower())
        if engine is None:
            logger.warning(
                f"announce_settings.engine is not one of {', '.join(sorted(ENGINES))}; falling back to Piper."
            )
            engine = PiperClient

        # Each engine reads its own settings block, so switching between them
        # is one key and neither one's URL or voice is lost on the way.
        engine_cfg = settings.get(engine.NAME) or {}
        self._tts: TTSClient = engine(
            url=engine_cfg.get("url", engine.DEFAULT_URL),
            voice=engine_cfg.get("voice"),
            timeout=timeouts.get(engine.NAME, engine.DEFAULT_TIMEOUT),
            options=engine_cfg.get("options")
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
        # Percentage of full scale to lift quiet clips to. NodeLink's mixer
        # volume already tops out at 1.0, so loudness the engine did not
        # produce has to be added here or not at all.
        self._loudness: int = as_percent(settings.get("loudness"), 95)

    @property
    def tts(self) -> TTSClient:
        """The synthesis client, so its defaults can be retuned at runtime."""
        return self._tts

    def save_voice_sample(self, key: str, wav: bytes) -> str:
        """Persists an uploaded voice-cloning reference clip, returning the URL a
        cloning-capable engine can fetch it from."""
        return self._server.save_voice_sample(key, wav)

    @property
    def loudness(self) -> int:
        """The bot-wide clip level, as a percentage of full scale."""
        return self._loudness

    @loudness.setter
    def loudness(self, value: int) -> None:
        self._loudness = as_percent(value, self._loudness)

    async def start(self) -> None:
        await self._server.start()
        logger.info(f"Announcements will be synthesised by {self._tts.NAME} at {self._tts.url}.")
        if not self._spotify.is_configured:
            logger.info(
                "No Spotify credentials configured, so @@track_genre@@ will always be empty. "
                "Set announce_settings.spotify or SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET to enable it."
            )

    async def stop(self) -> None:
        await self._server.stop()

    async def render_preview(self, player: Player, track: Track, guild_cfg: dict) -> Optional[str]:
        """Renders the announcement text without synthesising or playing it.

        Lets a guild tune a template or prompt without waiting out a song, and
        without spending a Piper call on text they are about to rewrite.
        """
        try:
            text = await self._render_text(player, track, guild_cfg)
            return " ".join(text.split())[:self._max_text_length] if text else None
        except Exception as e:
            logger.warning(f"TTS announcement preview failed for guild {player.guild.id}: {e}")
            return None

    async def build_announcement(self, player: Player, track: Track, guild_cfg: dict) -> Optional[Track]:
        try:
            text = await self._render_text(player, track, guild_cfg)
            if not text:
                return None
            text = " ".join(text.split())[:self._max_text_length]

            # Guild voice overrides live under the same key whichever engine is
            # in use; the client drops anything its engine cannot honour.
            voice_cfg = guild_cfg.get("piper") or {}
            wav = await self._tts.synthesize(
                text,
                voice=voice_cfg.get("voice"),
                options=self._tts.filter_options(voice_cfg)
            )
            if not wav:
                return None

            logger.debug(f"{self._tts.NAME} returned {len(wav)} bytes, format {wav_format(wav)}.")
            # Conversion is pure CPU work on a few hundred KB, so keep it off
            # the event loop.
            loudness = as_percent(voice_cfg.get("loudness"), self._loudness)
            wav = await asyncio.to_thread(to_mixer_pcm, wav, loudness)

            url = self._server.put(wav)
            results = await player.node.get_tracks(url, requester=player.guild.me)
            if not results:
                logger.warning(f"Lavalink could not load announcement clip from {url}.")
                return None

            return self._stamp_duration(results[0], wav, player)

        except Exception as e:
            logger.warning(f"TTS announcement failed for guild {player.guild.id}, playing track normally: {e}")
            return None

    def _stamp_duration(self, clip: Track, wav: bytes, player: Player) -> Track:
        """Gives the clip an explicit end time.

        Audio servers do not always work out how long an HTTP resource is -
        NodeLink hardcodes `length: -1` for its http source, and only schedules
        a track-end timer when `endTime` or the track length is positive. With
        neither, the clip never ends: playback stalls at the last sample, the
        server calls it stuck and restarts the stream, so the announcement
        repeats instead of handing back to the song.

        We generated the audio, so we know its length exactly. Setting it as
        the end time leaves the server's own encoded track untouched - it is
        sent as a plain `endTime` field on the play request.
        """
        duration = wav_duration_ms(wav)
        if duration:
            clip.end_time = duration
        else:
            logger.debug("Could not read the announcement duration; playing the clip as-is.")

        return clip

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
