"""Step 2: split a message into segments (list items, lines, sentences)."""

import re

from karma_x.recognizer.clean import clean
from karma_x.recognizer.model import Message

BULLET = re.compile(r"^\s*(?:[•\-*]|\d+\.)\s+")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _sentences(line: str) -> list[str]:
    pieces = (" ".join(piece.split()) for piece in SENTENCE_END.split(line))
    return [piece for piece in pieces if piece]


def segment(message: Message) -> tuple[str, ...]:
    lines: list[str] = []
    if message.blocks:
        for block in message.blocks:
            text = clean(block.text)
            lines.extend([text] if block.kind == "list_item" else text.split("\n"))
    else:
        lines = [BULLET.sub("", line) for line in clean(message.text).split("\n")]
    return tuple(piece for line in lines for piece in _sentences(line))
