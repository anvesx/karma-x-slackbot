"""Public types of the recognizer. Pure data, no behaviour beyond NoModel."""

from dataclasses import dataclass
from typing import Literal, Protocol

Outcome = Literal["auto", "confirm", "ignore"]
BlockKind = Literal["section", "list_item"]


@dataclass(frozen=True)
class Block:
    """One rich_text piece in Slack mrkdwn: a section, or a single list item."""

    kind: BlockKind
    text: str


@dataclass(frozen=True)
class Message:
    author: str
    text: str
    blocks: tuple[Block, ...] = ()


@dataclass(frozen=True)
class MentionDecision:
    user_id: str
    outcome: Outcome
    reason: str
    trace: tuple[str, ...]


class Scorer(Protocol):
    def score(self, segment: str) -> float | None:
        """Probability that the segment recognises someone; None when unavailable."""


class FixedScore:
    """Always returns the same score. For offline evaluation and tests."""

    def __init__(self, value: float | None) -> None:
        self.value = value

    def score(self, segment: str) -> float | None:
        return self.value


class NoModel:
    """Fallback scorer: never available, so nothing is ever automatic."""

    def score(self, segment: str) -> float | None:
        return None
