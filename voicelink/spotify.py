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

import base64
import logging
import os
import re
import time
import aiohttp

from typing import TYPE_CHECKING, Optional
from urllib.parse import quote

if TYPE_CHECKING:
    from .objects import Track

logger = logging.getLogger("vocard.spotify")

API_BASE = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# Junk commonly found in YouTube titles that hurts Spotify search matching.
_TITLE_NOISE = re.compile(
    r"\((?:official|lyric|audio|video|music|hd|4k|visualizer|explicit)[^)]*\)"
    r"|\[[^\]]*\]"
    r"|\b(?:official\s+)?(?:music\s+)?video\b"
    r"|\bfeat\.?\b.*$|\bft\.?\b.*$",
    re.IGNORECASE
)


class SpotifyGenreClient:
    """Looks up genre tags for a track via the Spotify Web API.

    Spotify attaches genres to artists rather than tracks, so a lookup is at
    most two calls: resolve the artist, then read its genres. Results are
    cached per artist. Every failure path returns an empty string - genre is
    a nice-to-have and must never block an announcement.
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        timeout: int = 8,
        cache_ttl: int = 86400,
        max_cache_size: int = 1000,
        max_genres: int = 3
    ):
        self._client_id: Optional[str] = client_id or os.getenv("SPOTIFY_CLIENT_ID")
        self._client_secret: Optional[str] = client_secret or os.getenv("SPOTIFY_CLIENT_SECRET")
        self._timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(total=timeout)
        self._cache_ttl: int = cache_ttl
        self._max_cache_size: int = max_cache_size
        self._max_genres: int = max_genres

        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._artist_cache: dict[str, tuple[str, float]] = {}

    @property
    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    async def get_genre(self, track: Track) -> str:
        """Returns a comma-separated genre string, or "" if unavailable."""
        if not self.is_configured:
            return ""

        title = getattr(track, "title", "?")
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                token = await self._get_token(session)
                if not token:
                    return ""

                artist_id = await self._resolve_artist_id(session, token, track)
                if not artist_id:
                    logger.debug(
                        f"No Spotify artist matched '{title}' by '{getattr(track, 'author', '?')}', "
                        "so @@track_genre@@ is empty."
                    )
                    return ""

                # An artist with no genres caches as "", which is a real
                # answer - distinguish it from a miss or the lookup repeats on
                # every announcement.
                cached = self._cache_get(artist_id)
                if cached is not None:
                    return cached

                genres = await self._fetch_artist_genres(session, token, artist_id)
                self._cache_put(artist_id, genres)

                if genres:
                    logger.debug(f"Spotify genres for '{title}': {genres}.")
                else:
                    logger.debug(
                        f"Spotify returned an empty genre list for artist {artist_id} ('{title}'), "
                        "so @@track_genre@@ is empty. The Web API omits genres for many artists."
                    )
                return genres

        except Exception as e:
            logger.debug(f"Genre lookup failed for '{title}': {e}")
            return ""

    async def _get_token(self, session: aiohttp.ClientSession) -> Optional[str]:
        if self._token and time.time() < self._token_expires_at:
            return self._token

        credentials = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded"
        }

        async with session.post(TOKEN_URL, headers=headers, data={"grant_type": "client_credentials"}) as resp:
            if resp.status != 200:
                detail = (await resp.text())[:200]
                if resp.status in (400, 401):
                    logger.warning(
                        "Spotify rejected the credentials in announce_settings.spotify "
                        f"(or SPOTIFY_CLIENT_ID/SECRET), so @@track_genre@@ will be empty: {detail}"
                    )
                else:
                    logger.warning(f"Spotify token request returned status {resp.status}: {detail}")
                return None

            data = await resp.json()

        self._token = data.get("access_token")
        # Renew a minute early to avoid racing the expiry.
        self._token_expires_at = time.time() + data.get("expires_in", 3600) - 60
        return self._token

    async def _resolve_artist_id(self, session: aiohttp.ClientSession, token: str, track: Track) -> Optional[str]:
        headers = {"Authorization": f"Bearer {token}"}

        if track.source == "spotify" and track.identifier:
            async with session.get(f"{API_BASE}/tracks/{track.identifier}", headers=headers) as resp:
                if resp.status != 200:
                    logger.debug(
                        f"Spotify track lookup for {track.identifier} returned status {resp.status}: "
                        f"{(await resp.text())[:200]}"
                    )
                    return None
                data = await resp.json()
            return self._first_artist_id(data)

        query = quote(f'track:"{self._clean_title(track.title)}" artist:"{track.author}"')
        async with session.get(f"{API_BASE}/search?q={query}&type=track&limit=1", headers=headers) as resp:
            if resp.status != 200:
                logger.debug(
                    f"Spotify search for '{track.title}' returned status {resp.status}: "
                    f"{(await resp.text())[:200]}"
                )
                return None
            data = await resp.json()

        items = data.get("tracks", {}).get("items") or []
        return self._first_artist_id(items[0]) if items else None

    async def _fetch_artist_genres(self, session: aiohttp.ClientSession, token: str, artist_id: str) -> str:
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get(f"{API_BASE}/artists/{artist_id}", headers=headers) as resp:
            if resp.status != 200:
                logger.debug(
                    f"Spotify artist lookup for {artist_id} returned status {resp.status}: "
                    f"{(await resp.text())[:200]}"
                )
                return ""
            data = await resp.json()

        return ", ".join((data.get("genres") or [])[:self._max_genres])

    @staticmethod
    def _first_artist_id(track_data: dict) -> Optional[str]:
        artists = track_data.get("artists") or []
        return artists[0].get("id") if artists else None

    @staticmethod
    def _clean_title(title: str) -> str:
        return " ".join(_TITLE_NOISE.sub("", title or "").split()).strip(" -–—")

    def _cache_get(self, artist_id: str) -> Optional[str]:
        """Returns the cached genres, or None on a miss. "" is a real hit."""
        entry = self._artist_cache.get(artist_id)
        if not entry:
            return None

        genres, created = entry
        if time.time() - created > self._cache_ttl:
            self._artist_cache.pop(artist_id, None)
            return None

        return genres

    def _cache_put(self, artist_id: str, genres: str) -> None:
        if len(self._artist_cache) >= self._max_cache_size:
            # Drop the oldest entry - dicts preserve insertion order.
            self._artist_cache.pop(next(iter(self._artist_cache)), None)

        self._artist_cache[artist_id] = (genres, time.time())
