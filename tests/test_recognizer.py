import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

from karma_x.recognizer import Block, FixedScore, Message, NoModel, Scorer, recognize
from karma_x.recognizer.attribute import attribute
from karma_x.recognizer.clean import clean
from karma_x.recognizer.lexicon import load_lexicon
from karma_x.recognizer.rules import find_cues
from karma_x.recognizer.segment import segment

FIXTURES = Path(__file__).parent / "fixtures" / "recognition_cases.toml"


@dataclass(frozen=True)
class Case:
    name: str
    author: str
    text: str
    blocks: tuple[Block, ...]
    expect: dict[str, str]
    nomodel: dict[str, str]


def load_cases() -> list[Case]:
    cases: list[Case] = []
    for raw in tomllib.loads(FIXTURES.read_text("utf-8"))["case"]:
        expect: dict[str, str] = raw["expect"]
        derived = {u: ("confirm" if o == "auto" else o) for u, o in expect.items()}
        blocks = tuple(Block(b["kind"], b["text"]) for b in raw.get("blocks", []))
        cases.append(
            Case(
                raw["name"],
                raw.get("author", "U1"),
                raw["text"],
                blocks,
                expect,
                raw.get("nomodel", derived),
            )
        )
    return cases


CASES = load_cases()


def outcomes(
    text: str,
    scorer: Scorer,
    author: str = "U1",
    blocks: tuple[Block, ...] = (),
    rules_auto: bool = False,
    **cutoffs: float,
) -> dict[str, str]:
    result = recognize(Message(author, text, blocks), scorer, rules_auto=rules_auto, **cutoffs)
    return {d.user_id: d.outcome for d in result}


def test_fixture_size() -> None:
    assert len(CASES) >= 60
    assert len({c.name for c in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_fixture_with_fake_scorer(case: Case) -> None:
    assert outcomes(case.text, FixedScore(0.95), case.author, case.blocks) == case.expect


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_fixture_with_no_model(case: Case) -> None:
    assert outcomes(case.text, NoModel(), case.author, case.blocks) == case.nomodel


AUTO_TEXT = "Kudos to <@UB> for fixing the payment retries"


def test_below_auto_cutoff_becomes_confirm_with_trace() -> None:
    (decision,) = recognize(Message("U1", AUTO_TEXT), FixedScore(0.5))
    assert decision.outcome == "confirm"
    assert "model-below-cutoff" in decision.trace


def test_no_model_trace() -> None:
    (decision,) = recognize(Message("U1", AUTO_TEXT), NoModel())
    assert decision.outcome == "confirm"
    assert "model-unavailable" in decision.trace


def test_auto_cutoff_is_inclusive_and_configurable() -> None:
    assert outcomes(AUTO_TEXT, FixedScore(0.9)) == {"UB": "auto"}
    assert outcomes(AUTO_TEXT, FixedScore(0.89)) == {"UB": "confirm"}
    assert outcomes(AUTO_TEXT, FixedScore(0.89), auto_cutoff=0.85) == {"UB": "auto"}


def test_confirm_cutoff_for_messages_without_cue() -> None:
    text = "<@UB> fixed the payment retries overnight"
    assert outcomes(text, FixedScore(0.8)) == {"UB": "confirm"}
    assert outcomes(text, FixedScore(0.79)) == {"UB": "ignore"}
    assert outcomes(text, FixedScore(0.79), confirm_cutoff=0.7) == {"UB": "confirm"}
    assert outcomes(text, FixedScore(None)) == {"UB": "ignore"}


def test_veto_beats_the_model() -> None:
    assert outcomes("<@UB> please do the needful", FixedScore(1.0)) == {"UB": "ignore"}


def test_trace_names_the_rules_that_fired() -> None:
    (decision,) = recognize(Message("U1", AUTO_TEXT), FixedScore(0.95))
    assert decision.trace[:2] == ("K", "A1")
    assert "model=0.95" in decision.trace
    # The reason is the segment as written, so the public post reads naturally.
    assert decision.reason == "Kudos to <@UB> for fixing the payment retries"
    (inherited,) = recognize(
        Message("U1", "Kudos to the team\n<@UB> — led the vendor negotiation"), FixedScore(0.95)
    )
    assert "K-inherited" in inherited.trace
    assert inherited.reason == "<@UB> — led the vendor negotiation"
    (veto,) = recognize(Message("U1", "cc <@UB>"), FixedScore(0.95))
    assert veto.trace == ("V1",)


def test_repeated_mention_merges_traces_in_order_of_first_mention() -> None:
    result = recognize(
        Message("U1", "<@UC> hi <@UB> thanks. Kudos <@UB> for fixing the payment retries"),
        FixedScore(0.95),
    )
    assert [d.user_id for d in result] == ["UC", "UB"]
    assert result[1].outcome == "auto"
    assert "T" in result[1].trace and "A1" in result[1].trace


def test_clean_strips_everything_it_should() -> None:
    raw = "> quoted <@UQ>\nhi <!channel> <https://x.test/a?b|l> https://y.test `c` <@UA|al>"
    assert " ".join(clean(raw).split()) == "hi <@UA>"


def test_segment_splits_lines_bullets_and_sentences() -> None:
    message = Message("U1", "Intro line. Next one?\n• item one\n2. item two")
    assert segment(message) == ("Intro line.", "Next one?", "item one", "item two")


def test_segment_blocks_ignore_message_text() -> None:
    message = Message(
        "U1",
        "ignored",
        (Block("section", "a b\nc d"), Block("list_item", "e. f")),
    )
    assert segment(message) == ("a b", "c d", "e.", "f")


def test_attribute_groups_and_inherits() -> None:
    lexicon = load_lexicon()
    found = attribute(("Kudos to all", "<@UA>, <@UB> - did things", "x <@UC>"), lexicon)
    assert [(a.user_id, a.inherited_kudos) for a in found] == [
        ("UA", True),
        ("UB", True),
        ("UC", False),
    ]
    assert found[0].group_start == found[1].group_start == 0
    assert found[2].group_start == 2


def test_find_cues_prefers_longer_kudos_over_thanks() -> None:
    cues = find_cues("Many thanks and ty", load_lexicon())
    assert [c.kind for c in cues] == ["K", "T"]


def test_empty_author_or_text_returns_nothing() -> None:
    assert recognize(Message("", AUTO_TEXT), NoModel()) == ()
    assert recognize(Message("U1", ""), NoModel()) == ()


def test_rules_auto_lets_rules_auto_stand_without_a_model() -> None:
    assert outcomes(AUTO_TEXT, NoModel(), rules_auto=True) == {"UB": "auto"}
    assert outcomes(AUTO_TEXT, NoModel()) == {"UB": "confirm"}


def test_rules_auto_never_invents_model_only_prompts() -> None:
    # Real #kudos data: with an always-yes scorer every mention with words became a prompt.
    text = "<@UB> we should have a leaderboard here too"
    assert outcomes(text, NoModel(), rules_auto=True) == {"UB": "ignore"}


def test_rules_auto_still_respects_vetoes_and_confirms() -> None:
    assert outcomes("cc <@UB> kudos for the release notes draft", NoModel(), rules_auto=True) == {
        "UB": "ignore"
    }
    assert outcomes("thanks <@UB> for the link", NoModel(), rules_auto=True) == {"UB": "confirm"}
