"""Loads lexicon.toml (the only I/O in the recognizer) into compiled patterns."""

import re
import tomllib
from dataclasses import dataclass
from functools import cache
from importlib import resources


def phrase_pattern(phrases: list[str], suffix: str = "") -> re.Pattern[str]:
    ordered = sorted(phrases, key=len, reverse=True)
    parts = [r"\s+".join(re.escape(word) for word in phrase.split()) for phrase in ordered]
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)" + suffix, re.IGNORECASE)


@dataclass(frozen=True)
class Lexicon:
    kudos: re.Pattern[str]
    praise: re.Pattern[str]
    thanks: re.Pattern[str]
    trivial: re.Pattern[str]
    before_mention: re.Pattern[str]
    requests: re.Pattern[str]
    negations: frozenset[str]
    sarcasm: tuple[str, ...]
    breaking: frozenset[str]
    stopwords: frozenset[str]


@cache
def load_lexicon() -> Lexicon:
    raw = tomllib.loads(
        resources.files("karma_x.recognizer").joinpath("lexicon.toml").read_text("utf-8")
    )
    cues: dict[str, list[str]] = raw["cues"]
    vetoes: dict[str, list[str]] = raw["vetoes"]
    praise = [f"{a} {n}" for a in cues["praise_adjectives"] for n in cues["praise_nouns"]]
    praise += cues["praise_phrases"]
    return Lexicon(
        kudos=phrase_pattern(cues["kudos"]),
        praise=phrase_pattern(praise),
        thanks=phrase_pattern(cues["thanks"]),
        trivial=phrase_pattern(cues["trivial"]),
        before_mention=phrase_pattern(vetoes["before_mention"], suffix="$"),
        requests=phrase_pattern(vetoes["requests"]),
        negations=frozenset(vetoes["negations"]),
        sarcasm=tuple(vetoes["sarcasm"]),
        breaking=frozenset(vetoes["breaking"]),
        stopwords=frozenset(raw["stopwords"]["words"]),
    )
