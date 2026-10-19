"""Step 3: map each mention to its segment, group names, pass down kudos cues."""

import re
from dataclasses import dataclass

from karma_x.recognizer.clean import MENTION
from karma_x.recognizer.lexicon import Lexicon

GAP = r"(?:\s|,|&|\band\b)*"
GROUP_GAP = re.compile(f"^{GAP}$", re.IGNORECASE)
INHERITING = re.compile(rf"^\s*{MENTION.pattern}(?:{GAP}{MENTION.pattern})*\s*[—–:-]\s*\S")


@dataclass(frozen=True)
class Attribution:
    user_id: str
    segment: str
    group_start: int
    inherited_kudos: bool


def attribute(segments: tuple[str, ...], lexicon: Lexicon) -> tuple[Attribution, ...]:
    opening_kudos = (
        bool(segments)
        and MENTION.search(segments[0]) is None
        and lexicon.kudos.search(segments[0]) is not None
    )
    found: list[Attribution] = []
    for index, text in enumerate(segments):
        inherited = opening_kudos and index > 0 and INHERITING.match(text) is not None
        previous_end: int | None = None
        group_start = 0
        for match in MENTION.finditer(text):
            joined = previous_end is not None and GROUP_GAP.match(
                text[previous_end : match.start()]
            )
            if not joined:
                group_start = match.start()
            found.append(Attribution(match.group(1), text, group_start, inherited))
            previous_end = match.end()
    return tuple(found)
