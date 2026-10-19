"""Decides, per mentioned person, whether a Slack message recognises them."""

from karma_x.recognizer.attribute import attribute
from karma_x.recognizer.clean import HAS_MENTION
from karma_x.recognizer.decide import decide_mention, merge
from karma_x.recognizer.lexicon import load_lexicon
from karma_x.recognizer.model import (
    Block,
    FixedScore,
    MentionDecision,
    Message,
    NoModel,
    Scorer,
)
from karma_x.recognizer.segment import segment

__all__ = [
    "Block",
    "FixedScore",
    "MentionDecision",
    "Message",
    "NoModel",
    "Scorer",
    "recognize",
]


def recognize(
    message: Message,
    scorer: Scorer,
    *,
    rules_auto: bool = False,
    auto_cutoff: float = 0.9,
    confirm_cutoff: float = 0.8,
) -> tuple[MentionDecision, ...]:
    if not message.author or not HAS_MENTION.search(message.text):
        return ()
    lexicon = load_lexicon()
    decisions: list[MentionDecision] = []
    for attribution in attribute(segment(message), lexicon):
        if attribution.user_id == message.author:
            decisions.append(MentionDecision(attribution.user_id, "ignore", "", ("self",)))
        else:
            decisions.append(
                decide_mention(
                    attribution, scorer, lexicon, rules_auto, auto_cutoff, confirm_cutoff
                )
            )
    return merge(decisions)
