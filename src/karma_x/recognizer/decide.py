"""Step 5: combine the rules verdict with the scorer, merge repeated mentions."""

from karma_x.recognizer.attribute import Attribution
from karma_x.recognizer.clean import MENTION
from karma_x.recognizer.lexicon import Lexicon
from karma_x.recognizer.model import MentionDecision, Outcome, Scorer
from karma_x.recognizer.rules import RuleVerdict, judge

STRENGTH: dict[Outcome, int] = {"ignore": 0, "confirm": 1, "auto": 2}


def decide_mention(
    attribution: Attribution,
    scorer: Scorer,
    lexicon: Lexicon,
    rules_auto: bool,
    auto_cutoff: float,
    confirm_cutoff: float,
) -> MentionDecision:
    verdict = judge(attribution, lexicon)
    outcome, notes = _combine(verdict, attribution, scorer, rules_auto, auto_cutoff, confirm_cutoff)
    return MentionDecision(attribution.user_id, outcome, verdict.reason, (*verdict.trace, *notes))


def _combine(
    verdict: RuleVerdict,
    attribution: Attribution,
    scorer: Scorer,
    rules_auto: bool,
    auto_cutoff: float,
    confirm_cutoff: float,
) -> tuple[Outcome, tuple[str, ...]]:
    """The model can only demote rules-AUTO or add a CONFIRM; a veto always wins."""
    if verdict.vetoes or verdict.outcome == "confirm":
        return verdict.outcome, ()
    if verdict.outcome == "ignore" and verdict.content_words == 0:
        return "ignore", ()
    score = scorer.score(MENTION.sub("@user", attribution.segment))
    if score is None:
        if verdict.outcome != "auto":
            return "ignore", ()
        return ("auto", ("rules-auto",)) if rules_auto else ("confirm", ("model-unavailable",))
    seen = f"model={score:.2f}"
    if verdict.outcome == "auto":
        if score >= auto_cutoff:
            return "auto", (seen,)
        return "confirm", (seen, "model-below-cutoff")
    if score >= confirm_cutoff:
        return "confirm", (seen, "model-only")
    return "ignore", (seen,)


def merge(decisions: list[MentionDecision]) -> tuple[MentionDecision, ...]:
    """One decision per person: strongest outcome wins, traces are merged."""
    by_user: dict[str, list[MentionDecision]] = {}
    for decision in decisions:
        by_user.setdefault(decision.user_id, []).append(decision)
    merged: list[MentionDecision] = []
    for user_id, mine in by_user.items():
        best = max(mine, key=lambda d: STRENGTH[d.outcome])
        trace = tuple(entry for d in mine for entry in d.trace)
        merged.append(MentionDecision(user_id, best.outcome, best.reason, trace))
    return tuple(merged)
