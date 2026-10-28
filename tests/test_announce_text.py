"""Announcer variety: angle selection, normalisation, TTS clean-up, validation, persona files."""

import importlib.util
import os
import random
import sys
from collections import Counter

from helpers import ROOT, detail, fail, ok

NAME = "Announcer Text"
DESCRIPTION = (
    "Exercise voicelink/announce_text.py: angle/shape selection spreads evenly and never repeats, "
    "names normalise deterministically, TTS text is clean, validation rejects repeats, "
    "and every persona in announce/personas/ parses."
)

LIBRARY = os.path.join(ROOT, "announce")


def _load():
    # By path, so voicelink/__init__ (discord.py, settings.json) is never imported.
    path = os.path.join(ROOT, "voicelink", "announce_text.py")
    spec = importlib.util.spec_from_file_location("announce_text_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _check(failures: list[str], condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def _selection(at, failures):
    persona = at.PersonaLibrary(LIBRARY).get("frieren")
    if not persona:
        failures.append("frieren persona did not load")
        return

    rng = random.Random(1027)
    history = at.AnnouncementHistory()
    picks = []
    for i in range(50):
        recent = history.recent(1, persona.key)
        direction = at.pick_direction(persona, recent, rng)
        picks.append(direction)
        history.record(1, at.Spoken(
            text=f"line {i}", persona=persona.key,
            angle=direction.angle, subject=direction.subject, shape=direction.shape
        ))

    for prev, cur in zip(picks, picks[1:]):
        _check(failures, prev.angle != cur.angle, f"angle {cur.angle} repeated back to back")
        _check(failures, prev.shape != cur.shape, f"shape {cur.shape} repeated back to back")

    counts = Counter(p.angle for p in picks)
    for angle in range(1, len(persona.angles) + 1):
        _check(failures, 7 <= counts[angle] <= 13, f"angle {angle} used {counts[angle]} times in 50, want 7-13")

    # The same track three times running: three different directions.
    trio = {(p.angle, p.subject) for p in picks[:3]}
    _check(failures, len(trio) == 3, "three consecutive picks shared an angle and subject")


def _normaliser(at, failures):
    n = at.Normaliser(LIBRARY)

    seen = {n.clean_track(t, a)[1] for t, a in [
        ("Talk That Talk", "HVRR"), ("Talk That Talk", "HVRR - Topic"), ("HVRR - Talk That Talk", "HVRR")
    ]}
    _check(failures, seen == {"H V R R"}, f"HVRR rendered as {seen}")

    _check(failures, n.clean_track("Greedy", "Tate McRae") == ("Greedy", "Tate McRae"), "Tate McRae was altered")
    _check(failures, n.clean_track("Infohazard (Official Video)", "Ninajirachi")[0] == "Infohazard", "title junk kept")
    _check(failures, n.clean_track("Ninajirachi - Wayside", "Ninajirachi") == ("Wayside", "Ninajirachi"),
           "Artist - Song upload was not split")
    _check(failures, n.clean_track("Song (Live)", "A")[0] == "Song (Live)", "Live tag was stripped")
    _check(failures, "featuring" in n.clean_track("Song ft. Bob", "A")[0], "ft. not expanded")

    _check(failures, n.requester("djmalachite") == "DJ Malakite", "djmalachite not mapped")
    _check(failures, n.requester("xX_Nathan_99") == "Nathan", f"xX_Nathan_99 -> {n.requester('xX_Nathan_99')}")
    _check(failures, n.requester("___") == "a listener", "unpronounceable name not replaced")


def _clean(at, failures):
    cleaned = at.clean_for_tts("“Wayside” — by **Ninajirachi**, tonight’s pick \U0001f3b5 (enjoy)…")
    for bad in ("“", "”", "’", "—", "*", "(", ")", "…", "\U0001f3b5"):
        _check(failures, bad not in cleaned, f"{bad!r} reached the TTS text: {cleaned}")
    _check(failures, "tonight's" in cleaned, f"apostrophe lost: {cleaned}")


def _validation(at, failures):
    title, artist = "Wayside", "Ninajirachi"
    good = "Fern says I should stay awake for the introductions. I am trying my best tonight, and this is Wayside by Ninajirachi."

    def check(text, **kwargs):
        return at.validate(text, title=title, artist=artist, **kwargs)

    _check(failures, check(good) is None, f"good line rejected: {check(good)}")
    _check(failures, check("Wayside by Ninajirachi.") is not None, "short line accepted")
    _check(failures, check(good.replace("Wayside", "this one")) is not None, "missing title accepted")
    _check(failures, check(good.replace("Ninajirachi", "them")) is not None, "missing artist accepted")
    _check(failures, check("Well, " + good) is not None, "banned opener accepted")
    _check(failures, check(good, never_say=["introductions"]) is not None, "never-say phrase accepted")

    # The logged Frieren cluster: same joke, three times.
    first = at.Spoken(text=good, persona="frieren", title=title, artist=artist)
    second = "Fern says I should stay awake for the introductions, so I am trying my best, and here is Wayside by Ninajirachi."
    third = "Talk That Talk by H V R R, which Fern says I should stay awake for, though I am trying my best to stay awake tonight."
    _check(failures, check(second, persona_recent=[first]) is not None, "second Fern opening accepted")
    reason = at.validate(third, title="Talk That Talk", artist="H V R R", guild_recent=[first])
    _check(failures, reason is not None, "third Fern line accepted despite similarity")

    # A different line about the same track is fine.
    fresh = "I traded a perfectly good grimoire for a spell that warms socks, which seems fair, and now here is Wayside by Ninajirachi."
    _check(failures, check(fresh, persona_recent=[first], guild_recent=[first]) is None,
           f"distinct line rejected: {check(fresh, persona_recent=[first], guild_recent=[first])}")


def _library(at, failures):
    library = at.PersonaLibrary(LIBRARY)
    keys = library.keys()
    _check(failures, len(keys) >= 1, "no personas found in announce/personas/")
    for key in keys:
        path = os.path.join(LIBRARY, "personas", f"{key}.json")
        try:
            import json
            with open(path, encoding="utf-8") as f:
                persona = at.parse_persona(key, json.load(f))
        except Exception as e:
            failures.append(f"{key}: {e}")
            continue
        for index, angle in enumerate(persona.angles, start=1):
            _check(failures, bool(angle.subjects), f"{key}: angle {index} has no subjects")
        _check(failures, len(persona.persona) <= 4000, f"{key}: persona prose is {len(persona.persona)} characters")
    _check(failures, library.prompt() and "@@announce_direction@@" in library.prompt(),
           "announce/prompt.txt is missing or has no @@announce_direction@@ slot")


def run() -> bool:
    at = _load()
    passed = True

    for label, check in (
        ("selection", _selection),
        ("normaliser", _normaliser),
        ("TTS clean-up", _clean),
        ("validation", _validation),
        ("persona library", _library),
    ):
        failures: list[str] = []
        try:
            check(at, failures)
        except Exception as e:
            failures.append(f"raised {e!r}")

        if failures:
            passed = False
            fail(f"Announcer {label}")
            for message in failures:
                detail(message)
        else:
            ok(f"Announcer {label}")

    return passed
