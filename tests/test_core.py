from datetime import UTC, datetime

import pytest

from karma_x.core import UserError, date_window, parse_command


@pytest.mark.parametrize("text", ["<@U123> ++ Thank you", "<@W123|Name> ++ Great work 🌟"])
def test_award_parser(text):
    c = parse_command(text)
    assert c.action == "give" and c.target in {"U123", "W123"}
    assert c.reason


@pytest.mark.parametrize(
    "text",
    [
        "",
        "<@U123> ++",
        "<@U123> 2++ x",
        "<@U123> +++ x",
        "@name ++ x",
        "unknown",
        "winner-between 2026-13-01 2026-01-01",
        "winner-between 2026-10-10 2026-10-01",
        "list-all extra",
        "recognition cancel",
        "recognition view bad;sql",
    ],
)
def test_invalid_commands(text):
    with pytest.raises(UserError):
        parse_command(text)


def test_reason_size():
    with pytest.raises(UserError):
        parse_command("<@U123> ++ " + "x" * 2001)


@pytest.mark.parametrize(
    "text,action",
    [
        ("list-all", "list"),
        ("winner-of-the-day", "winner"),
        ("winner-of-the-month", "winner"),
        ("winner-of-the-year", "winner"),
        ("recognition view", "history"),
        ("recognition view K123", "view"),
        ("recognition cancel K123", "cancel"),
    ],
)
def test_commands(text, action):
    assert parse_command(text).action == action


def test_ist_day():
    start, end = date_window("day", "Asia/Kolkata", datetime(2026, 10, 10, 12, tzinfo=UTC))
    assert start == datetime(2026, 10, 9, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 10, 10, 18, 30, tzinfo=UTC)


@pytest.mark.parametrize("day,hours", [("2026-03-08", 23), ("2026-11-01", 25)])
def test_dst(day, hours):
    start, end = date_window("between", "America/New_York", datetime.now(UTC), day, day)
    assert (end - start).total_seconds() == hours * 3600


def test_inclusive_end():
    start, end = date_window("between", "UTC", datetime.now(UTC), "2026-10-01", "2026-10-10")
    assert start.day == 1 and end.day == 11


def test_group_post_keeps_user_mentions_in_reasons_but_escapes_broadcasts():
    from karma_x.core import GroupMember, group_post_text

    reason = "Kudos to <@U2> and <@U3> <!here> for the launch & more"
    single = group_post_text("U1", [GroupMember("U2", reason, False)])
    assert "Kudos to <@U2> and <@U3>" in single and "&lt;!here&gt;" in single
    assert "&amp; more" in single and single.startswith("<@U1> recognized <@U2> · 🌟 +1\n")
    both = group_post_text(
        "U1", [GroupMember("U2", reason, False), GroupMember("U3", reason, False)]
    )
    assert "— Kudos to <@U2> and <@U3>" in both and "<!here>" not in both


@pytest.mark.parametrize("text", ["help", "  help  ", "HELP"])
def test_help_parser(text):
    assert parse_command(text).action == "help"


def test_unknown_command_points_to_help():
    with pytest.raises(UserError, match="/karma-x help"):
        parse_command("unknown")
