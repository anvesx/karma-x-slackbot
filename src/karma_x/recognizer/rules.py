"""Step 4: cues, vetoes and the rules-only verdict for one mention."""

import re
from dataclasses import dataclass
from typing import Literal

from karma_x.recognizer.attribute import Attribution
from karma_x.recognizer.clean import MENTION
from karma_x.recognizer.lexicon import Lexicon
from karma_x.recognizer.model import Outcome

CueKind = Literal["K", "T", "P"]

SHORTCODE = re.compile(r":[a-z0-9_+\-]+:", re.IGNORECASE)
WORD = re.compile(r"\w+(?:[-'’]\w+)*")
SARCASM_SLASH_S = re.compile(r"\s/s(?!\w)", re.IGNORECASE)
EDGE_PUNCTUATION = " \t-—–:,.;!?*•()"
NEGATION_DISTANCE = 3
BREAKING_DISTANCE = 4
AUTO_CONTENT_WORDS = 3


@dataclass(frozen=True)
class Cue:
    kind: CueKind
    start: int
    end: int


@dataclass(frozen=True)
class RuleVerdict:
    outcome: Outcome
    vetoes: tuple[str, ...]
    trace: tuple[str, ...]
    reason: str
    content_words: int


def find_cues(text: str, lexicon: Lexicon) -> tuple[Cue, ...]:
    """K first, then P, then T; a later cue never overlaps an earlier one."""
    taken: list[Cue] = []
    ordered: tuple[tuple[CueKind, re.Pattern[str]], ...] = (
        ("K", lexicon.kudos),
        ("P", lexicon.praise),
        ("T", lexicon.thanks),
    )
    for kind, pattern in ordered:
        for match in pattern.finditer(text):
            overlaps = any(match.start() < c.end and c.start < match.end() for c in taken)
            if not overlaps:
                taken.append(Cue(kind, match.start(), match.end()))
    return tuple(sorted(taken, key=lambda cue: cue.start))


def _blank(text: str) -> str:
    """Same-length text with mention tokens and emoji shortcodes blanked out."""
    blanked = MENTION.sub(lambda m: " " * len(m.group(0)), text)
    return SHORTCODE.sub(lambda m: " " * len(m.group(0)), blanked)


def _tidy(text: str) -> str:
    return " ".join(text.split()).strip(EDGE_PUNCTUATION)


def _without_cues(text: str, cues: tuple[Cue, ...]) -> str:
    pieces: list[str] = []
    position = 0
    for cue in cues:
        pieces.append(text[position : cue.start])
        position = cue.end
    pieces.append(text[position:])
    return MENTION.sub(" ", " ".join(pieces))


def _vetoes(attribution: Attribution, cues: tuple[Cue, ...], lexicon: Lexicon) -> list[str]:
    text = attribution.segment
    fired: list[str] = []
    before = text[: attribution.group_start].rstrip(EDGE_PUNCTUATION)
    if before.endswith("+") or lexicon.before_mention.search(before):
        fired.append("V1")
    if "?" in text:
        fired.append("V2")
    if lexicon.requests.search(text):
        fired.append("V3")
    words = [(m.start(), m.end(), m.group(0).lower()) for m in WORD.finditer(_blank(text))]
    negated = False
    broken = False
    for cue in cues:
        inside = [i for i, (s, e, _) in enumerate(words) if s >= cue.start and e <= cue.end]
        if not inside:
            continue
        low, high = inside[0], inside[-1]
        for i, (_, _, word) in enumerate(words):
            if word in lexicon.negations and (
                (i < low and low - i <= NEGATION_DISTANCE)
                or (i > high and i - high <= NEGATION_DISTANCE)
            ):
                negated = True
            if word in lexicon.breaking and high < i <= high + BREAKING_DISTANCE:
                broken = True
    if negated:
        fired.append("V4")
    if (
        broken
        or any(marker in text.lower() for marker in lexicon.sarcasm)
        or SARCASM_SLASH_S.search(text)
    ):
        fired.append("V5")
    return fired


def judge(attribution: Attribution, lexicon: Lexicon) -> RuleVerdict:
    text = attribution.segment
    cues = find_cues(text, lexicon)
    kinds = {cue.kind for cue in cues}
    trace: list[str] = [kind for kind in ("K", "T", "P") if kind in kinds]
    if attribution.inherited_kudos:
        trace.append("K-inherited")
    remainder = _without_cues(text, cues)
    reason = _tidy(text)
    content = [
        word.lower()
        for word in WORD.findall(_blank(remainder))
        if word.lower() not in lexicon.stopwords
    ]
    vetoes = _vetoes(attribution, cues, lexicon)
    has_cue = bool(kinds) or attribution.inherited_kudos
    if vetoes:
        return RuleVerdict("ignore", tuple(vetoes), (*trace, *vetoes), reason, len(content))
    enough = len(content) >= AUTO_CONTENT_WORDS
    kudos = "K" in kinds or attribution.inherited_kudos
    own_thanks_or_praise = bool(kinds & {"T", "P"})
    outcome: Outcome
    if kudos and enough:
        outcome = "auto"
        trace.append("A1")
    elif own_thanks_or_praise and enough and not lexicon.trivial.search(remainder):
        outcome = "auto"
        trace.append("A2")
    elif has_cue:
        outcome = "confirm"
    else:
        outcome = "ignore"
    return RuleVerdict(outcome, (), tuple(trace), reason, len(content))
