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

# The text side of AI announcements: what was said recently, which angle to
# ask for next, how names are cleaned before the model sees them, how the
# model's answer is cleaned before TTS reads it, and whether that answer is
# good enough to speak at all.
#
# Kept to the standard library and free of package-relative imports so the
# test suite can load this file directly, without discord.py or a settings.json.

from __future__ import annotations

import difflib
import json
import logging
import os
import random
import re
import time
import unicodedata

from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Optional

logger = logging.getLogger("vocard.announcer")

# Bot root, not voicelink/ - same reasoning as DEFAULT_VOICE_DIR in announcer.py.
DEFAULT_LIBRARY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "announce")

HISTORY_SIZE = 20
HISTORY_SHOWN = 6
SIMILARITY_LIMIT = 0.6
OPENING_WORDS = 5
WORD_RANGE = (18, 30)
# Free-text personas were written against prompts of their own, which may ask
# for something much shorter than the library's 18-30, so only the extremes
# are rejected.
CUSTOM_WORD_RANGE = (5, 40)

BANNED_OPENERS = ("ah", "oh", "well", "so", "alright", "here's", "heres", "next up", "now playing", "coming up")

# Used when a persona file does not bring its own.
DEFAULT_SHAPES = (
    "open with the artist",
    "remark first, title and artist last",
    "open with the title",
    "two short sentences, remark then introduction",
    "open with the requester or your call sign"
)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

@dataclass
class Spoken:
    text: str
    persona: str
    title: str = ""
    artist: str = ""
    angle: Optional[int] = None
    subject: Optional[str] = None
    shape: Optional[int] = None
    at: float = field(default_factory=time.time)


class AnnouncementHistory:
    """The last few announcements per guild, newest last.

    In memory only: losing it on restart costs one announcement's worth of
    variety, which is not worth a database round trip per song.
    """

    def __init__(self, size: int = HISTORY_SIZE):
        self._size: int = max(1, size)
        self._store: dict[int, deque[Spoken]] = {}

    def record(self, guild_id: int, spoken: Spoken) -> None:
        self._store.setdefault(guild_id, deque(maxlen=self._size)).append(spoken)

    def all(self, guild_id: int) -> list[Spoken]:
        return list(self._store.get(guild_id, ()))

    def recent(self, guild_id: int, persona: str, count: int = HISTORY_SHOWN) -> list[Spoken]:
        """The newest `count` announcements made by this persona, newest last.

        Filtered by persona because one character's lines are no constraint on
        another's.
        """
        matching = [entry for entry in self._store.get(guild_id, ()) if entry.persona == persona]
        return matching[-count:] if count > 0 else []


# ---------------------------------------------------------------------------
# Persona library
# ---------------------------------------------------------------------------

@dataclass
class Angle:
    label: str
    subjects: list[str]


@dataclass
class Persona:
    key: str
    name: str
    persona: str
    angles: list[Angle]
    shapes: list[str]
    never_say: list[str]
    words: tuple[int, int]
    fallback: str
    prompt: Optional[str] = None


class _FileCache:
    """Re-reads a file only when its mtime changes, so edits need no restart."""

    def __init__(self):
        self._entries: dict[str, tuple[float, object]] = {}

    def get(self, path: str, loader):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            self._entries.pop(path, None)
            return None

        cached = self._entries.get(path)
        if cached and cached[0] == mtime:
            return cached[1]

        try:
            with open(path, encoding="utf-8") as f:
                value = loader(f)
        except Exception as e:
            logger.warning(f"Could not read {path}: {e!r}")
            value = None

        self._entries[path] = (mtime, value)
        return value


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def parse_persona(key: str, data: dict, default_shapes: Iterable[str] = DEFAULT_SHAPES) -> Persona:
    """Builds a Persona from its JSON, raising ValueError on anything unusable."""
    if not isinstance(data, dict):
        raise ValueError("the file is not a JSON object")

    persona = data.get("persona")
    if not isinstance(persona, str) or not persona.strip():
        raise ValueError("'persona' must be a non-empty string")

    angles = []
    for index, raw in enumerate(data.get("angles") or [], start=1):
        if not isinstance(raw, dict) or not isinstance(raw.get("label"), str):
            raise ValueError(f"angle {index} needs a 'label'")
        angles.append(Angle(raw["label"].strip(), _string_list(raw.get("subjects"))))
    if len(angles) < 2:
        raise ValueError("at least two angles are needed for the selector to have a choice")

    words = data.get("words") or WORD_RANGE
    try:
        low, high = int(words[0]), int(words[1])
    except (TypeError, ValueError, IndexError):
        raise ValueError("'words' must be [min, max]")

    fallback = data.get("fallback")
    if not isinstance(fallback, str) or not fallback.strip():
        raise ValueError("'fallback' must be a non-empty string")

    prompt = data.get("prompt")
    return Persona(
        key=key,
        name=str(data.get("name") or key),
        persona=persona.strip(),
        angles=angles,
        shapes=_string_list(data.get("shapes")) or list(default_shapes),
        never_say=_string_list(data.get("never_say")),
        words=(low, high),
        fallback=fallback.strip(),
        prompt=prompt.strip() if isinstance(prompt, str) and prompt.strip() else None
    )


class PersonaLibrary:
    """Personas stored as `<dir>/personas/<key>.json`.

    The persona prose is what the model reads; the angles and subjects are
    what the selector draws from. Both live in the one file so they cannot
    drift apart.
    """

    def __init__(self, directory: str = DEFAULT_LIBRARY_DIR):
        self._dir: str = directory
        self._cache = _FileCache()

    @property
    def directory(self) -> str:
        return self._dir

    @property
    def _persona_dir(self) -> str:
        return os.path.join(self._dir, "personas")

    def shapes(self) -> list[str]:
        shapes = self._cache.get(os.path.join(self._dir, "shapes.json"), json.load)
        return _string_list(shapes) or list(DEFAULT_SHAPES)

    def prompt(self) -> Optional[str]:
        """The shared task prompt used with every library persona."""
        text = self._cache.get(os.path.join(self._dir, "prompt.txt"), lambda f: f.read())
        return text.strip() if isinstance(text, str) and text.strip() else None

    def keys(self) -> list[str]:
        try:
            names = os.listdir(self._persona_dir)
        except OSError:
            return []
        return sorted(name[:-5] for name in names if name.endswith(".json") and not name.startswith("_"))

    def get(self, key: Optional[str]) -> Optional[Persona]:
        if not key:
            return None

        key = key.strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]+", key):
            return None

        data = self._cache.get(os.path.join(self._persona_dir, f"{key}.json"), json.load)
        if data is None:
            return None

        try:
            return parse_persona(key, data, self.shapes())
        except ValueError as e:
            logger.warning(f"Ignoring announcer persona '{key}': {e}.")
            return None

    def all(self) -> list[Persona]:
        return [persona for key in self.keys() if (persona := self.get(key))]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

@dataclass
class Direction:
    angle: int
    angle_label: str
    subject: Optional[str]
    shape: int
    shape_text: str

    def instruction(self) -> str:
        line = f"Use angle {self.angle} from your persona"
        line += f", focusing on: {self.subject}." if self.subject else "."
        return f"{line}\nUse shape {self.shape}: {self.shape_text}."


def pick_direction(persona: Persona, recent: list[Spoken], rng: Optional[random.Random] = None) -> Direction:
    """Chooses the angle, subject and shape for the next announcement.

    Drawn at random, but never one of the last two angles or the last shape,
    so a repeat in a row is impossible and the counts even out on their own.
    The subject is named outright because left to itself the model takes the
    first one listed nearly every time.
    """
    rng = rng or random

    used_angles = [entry.angle for entry in recent if entry.angle]
    blocked = set(used_angles[-2:])
    angles = [i for i in range(1, len(persona.angles) + 1) if i not in blocked] or list(range(1, len(persona.angles) + 1))
    # Lean towards angles the window has heard least, so a run of bad luck
    # corrects itself instead of leaving one angle unused for twenty songs.
    counts = {i: used_angles.count(i) for i in angles}
    angle = rng.choices(angles, weights=[1 / (1 + counts[i]) ** 2 for i in angles])[0]
    chosen = persona.angles[angle - 1]

    subject = None
    if chosen.subjects:
        last_subject = next((entry.subject for entry in reversed(recent) if entry.angle == angle), None)
        subjects = [s for s in chosen.subjects if s != last_subject] or chosen.subjects
        subject = rng.choice(subjects)

    shapes = persona.shapes or list(DEFAULT_SHAPES)
    last_shape = next((entry.shape for entry in reversed(recent) if entry.shape), None)
    shape_choices = [i for i in range(1, len(shapes) + 1) if i != last_shape] or list(range(1, len(shapes) + 1))
    shape = rng.choice(shape_choices)

    return Direction(angle, chosen.label, subject, shape, shapes[shape - 1])


def recent_block(recent: list[Spoken]) -> str:
    """The "do not repeat these" list shown to the model, or "" when empty."""
    if not recent:
        return ""
    lines = "\n".join(f"- {entry.text}" for entry in recent)
    return (
        "Recently said on this server. Do not reuse their wording, sentence structure, subject or joke:\n"
        f"{lines}"
    )


# ---------------------------------------------------------------------------
# Normalisation before the prompt
# ---------------------------------------------------------------------------

# Bracketed title junk. Remix, Live, Acoustic and the like are deliberately
# not here: they tell the listener which version this is.
_JUNK_WORDS = (
    r"official(?:\s+(?:music|lyrics?|hd|4k))?(?:\s+(?:video|audio|visuali[sz]er))?|music\s+video|video|audio|"
    r"lyrics?(?:\s+video)?|lyric\s+video|visuali[sz]er|remaster(?:ed)?(?:\s+\d{4})?|\d{4}\s+remaster(?:ed)?|"
    r"hd|hq|4k|mv|m/v|explicit|clean|color\s+coded|\d{4}"
)
_JUNK_BRACKETS = re.compile(rf"\s*[\(\[\{{]\s*(?:{_JUNK_WORDS})\s*[\)\]\}}]", re.IGNORECASE)
_TRAILING_PIPE = re.compile(r"\s*[|｜].*$")
_HASHTAGS = re.compile(r"(?:^|\s)#\w+")
_CHANNEL_SUFFIX = re.compile(r"\s*(?:-\s*topic|vevo|\(official\)|official(?:\s+channel)?)\s*$", re.IGNORECASE)
_CHANNELISH = re.compile(r"\b(?:records|recordings|music|entertainment|official|vevo|topic|channel|tv)\b", re.IGNORECASE)


def _strip_symbols(text: str) -> str:
    """Drops emoji and other pictographs, keeping letters, digits and punctuation."""
    out = []
    for char in text:
        if char in "‍️︎":
            continue
        if unicodedata.category(char) in ("So", "Sk", "Cs", "Co") or ord(char) > 0xFFFF:
            continue
        out.append(char)
    return "".join(out)


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _expand_joiners(text: str) -> str:
    text = re.sub(r"\b(?:feat|ft)\b\.?\s*", "featuring ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bvs\b\.?\s*", "versus ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+&\s+|\s+x\s+", " and ", text)
    # Ke$ha, A$AP: a dollar inside a name is an S.
    text = re.sub(r"(?<=\w)\$(?=\w)", lambda m: "s", text)
    return text


class Normaliser:
    """Cleans track titles, artists and requester names before the prompt.

    The model, asked to do this itself, gave a different spelling of the same
    artist from one call to the next. Doing it here gives one answer, and a
    pronunciation map anyone can edit without touching a prompt.
    """

    def __init__(self, library_dir: str = DEFAULT_LIBRARY_DIR, extra: Optional[dict] = None):
        self._path: str = os.path.join(library_dir, "pronunciations.json")
        self._cache = _FileCache()
        self._extra: dict = {k.casefold(): v for k, v in (extra or {}).items()}

    def _map(self) -> dict[str, str]:
        data = self._cache.get(self._path, json.load)
        mapping = {}
        if isinstance(data, dict):
            mapping = {str(k).casefold(): str(v) for k, v in data.items() if not str(k).startswith("_")}
        mapping.update(self._extra)
        return mapping

    def speak(self, text: str) -> str:
        """Applies the pronunciation map: the whole string first, then word by word.

        Exact matches only. Anything not in the map is left exactly as it was,
        so a name that was already right is never "corrected".
        """
        if not text:
            return text

        mapping = self._map()
        if (whole := mapping.get(text.strip().casefold())) is not None:
            return whole

        def replace(match: re.Match) -> str:
            return mapping.get(match.group(0).casefold(), match.group(0))

        # Tokens may carry inner punctuation (AC/DC, Ke$ha) but not trailing commas.
        return re.sub(r"[^\s,.;:!?]+(?:[.,][^\s,.;:!?]+)*", replace, text)

    def clean_track(self, title: str, author: str) -> tuple[str, str]:
        """Returns the title and artist as they should be spoken."""
        title = _strip_symbols(title or "")
        author = _strip_symbols(author or "")

        channel_like = bool(_CHANNEL_SUFFIX.search(author)) or bool(_CHANNELISH.search(author))
        author = _CHANNEL_SUFFIX.sub("", author).strip() or author.strip()

        title = _JUNK_BRACKETS.sub("", title)
        title = _TRAILING_PIPE.sub("", title)
        title = _HASHTAGS.sub("", title)

        # "Artist - Song" uploads: split when the left half is the uploader, or
        # when the uploader is a label or channel rather than an artist.
        left, sep, right = title.partition(" - ")
        if sep and left.strip() and right.strip():
            left_key = re.sub(r"\W+", "", left).casefold()
            author_key = re.sub(r"\W+", "", author).casefold()
            if left_key and author_key and (left_key in author_key or author_key in left_key):
                title = right
            elif channel_like:
                author, title = left, right

        title = _collapse(_expand_joiners(title)).strip(" -,")
        author = _collapse(_expand_joiners(author)).strip(" -,")

        return self.speak(title) or title, self.speak(author) or author

    def requester(self, name: str, fallback: str = "a listener") -> str:
        """Turns a username into something a voice can say."""
        if not name:
            return fallback

        mapping = self._map()
        if (mapped := mapping.get(name.strip().casefold())) is not None:
            return mapped

        cleaned = _strip_symbols(name)
        # xX_Nathan_99_Xx -> Nathan
        cleaned = re.sub(r"^[xX]{2,}|[xX]{2,}$", "", cleaned)
        cleaned = re.sub(r"[_\.\-~|*]+", " ", cleaned)
        cleaned = re.sub(r"\d+$", "", cleaned.strip())
        cleaned = re.sub(r"[^\w\s']", "", cleaned)
        cleaned = _collapse(cleaned.replace("_", " "))

        if (mapped := mapping.get(cleaned.casefold())) is not None:
            return mapped

        letters = re.sub(r"[^A-Za-z]", "", cleaned)
        if len(letters) < 2 or not re.search(r"[aeiouyAEIOUY]", letters):
            return fallback
        return cleaned


# ---------------------------------------------------------------------------
# Clean-up after the model
# ---------------------------------------------------------------------------

_QUOTE_SINGLE = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "`": "'"})


def clean_for_tts(text: str) -> str:
    """Strips the typography a TTS voice reads aloud or stumbles over."""
    if not text:
        return ""

    text = text.translate(_QUOTE_SINGLE)
    # Double quotes around a title are never spoken, so drop them.
    text = re.sub(r"[\"“”„‟«»]", "", text)
    text = re.sub(r"\s*[—–―]\s*|\s+-{1,2}\s+", ", ", text)
    text = text.replace("…", ".").replace("...", ".")
    text = re.sub(r"[\[\]\(\)\{\}<>]", " ", text)
    text = re.sub(r"[*_#~|>`\\]", " ", text)
    text = _strip_symbols(text)

    text = _collapse(text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"([,.!?;:])(?:\s*[,;:])+", r"\1", text)
    text = re.sub(r",\s*\.", ".", text)
    return text.strip(" ,;:")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _squash(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).split())


def _opening(text: str, count: int = OPENING_WORDS) -> str:
    return " ".join(_squash(text).split()[:count])


def _without(text: str, *names: str) -> str:
    squashed = f" {_squash(text)} "
    for name in names:
        if name := _squash(name):
            squashed = squashed.replace(f" {name} ", " ")
    return squashed.strip()


def _primary_artist(artist: str) -> str:
    return re.split(r"\s+(?:featuring|and|with|versus)\s+|,", artist, maxsplit=1, flags=re.IGNORECASE)[0]


def _mentions(text: str, name: str) -> bool:
    squashed_name = _squash(name)
    if not squashed_name:
        return True

    # The prompt asks for digits as words, so "22" may come back as
    # "twenty two". Check the words the model has no reason to change.
    if re.search(r"\d", squashed_name):
        words = [w for w in squashed_name.split() if not w.isdigit()]
        if not words:
            return True
        squashed_name = " ".join(words)

    return f" {squashed_name} " in f" {_squash(text)} "


def validate(
    text: str,
    *,
    title: str,
    artist: str,
    words: tuple[int, int] = WORD_RANGE,
    never_say: Iterable[str] = (),
    persona_recent: Iterable[Spoken] = (),
    guild_recent: Iterable[Spoken] = (),
    similarity: float = SIMILARITY_LIMIT
) -> Optional[str]:
    """Returns why `text` should not be spoken, or None if it is fine.

    Cheapest checks first. Each reason is phrased to finish the sentence
    "Your previous attempt ..." in the retry prompt.
    """
    if not text or not text.strip():
        return "was empty"

    count = len(text.split())
    if count < words[0]:
        return f"was {count} words, fewer than {words[0]}"
    if count > words[1]:
        return f"was {count} words, more than {words[1]}"

    if not _mentions(text, title):
        return f"did not name the title, {title}"
    if not _mentions(text, _primary_artist(artist)):
        return f"did not name the artist, {artist}"

    lowered = text.strip().casefold().replace("’", "'")
    for opener in BANNED_OPENERS:
        if re.match(rf"{re.escape(opener)}(?![a-z'])", lowered):
            return f"opened with {opener.title()}, which is not allowed"

    for phrase in never_say:
        if phrase and re.search(rf"(?<![a-z]){re.escape(phrase.casefold())}(?![a-z])", lowered):
            return f"used the phrase '{phrase}', which your persona never says"

    opening = _opening(text)
    for entry in persona_recent:
        if opening and opening == _opening(entry.text):
            return "opened the same way as a recent announcement"

    # Title and artist are taken out first: the same track announced twice
    # shares those words by necessity, not by repetition.
    body = _without(text, title, artist)
    for entry in guild_recent:
        other = _without(entry.text, entry.title, entry.artist)
        if body and other and difflib.SequenceMatcher(None, body, other).ratio() > similarity:
            return "was too similar to a recent announcement"

    return None
