from datetime import UTC, datetime

import pytest

from karma_x.core import UserError, utc_us
from karma_x.slack import SlackFailure


def task(text, key="q1", user="U1", channel="C1"):
    return {
        "id": key,
        "created_at_us": utc_us(datetime(2026, 10, 10, 12, tzinfo=UTC)),
        "payload": {
            "team_id": "T1",
            "user_id": user,
            "channel_id": channel,
            "text": text,
            "response_url": "https://hooks.slack.com/commands/test",
        },
    }


def test_full_award_cancel_journey(rig):
    service, slack = rig
    response = service.execute(task("<@U2> ++ great work"))
    r = service.store.history("T1", "U1")[0]
    assert r["id"] in response["text"]
    assert slack.names["U2"] == "Person 2 🌟1"
    assert r["permalink"].startswith("https://test.slack.com/")
    service.execute(task("<@U2> ++ second recognition", "q2"))
    assert slack.names["U2"] == "Person 2 🌟2"
    board = service.execute(task("list-all"))
    assert len(board["blocks"][0]["rows"]) == 2
    assert board["blocks"][0]["rows"][1][2]["value"] == 2
    service.execute(task("recognition cancel " + r["id"]))
    assert slack.names["U2"] == "Person 2 🌟1"
    assert len(slack.updates) == 1


def test_zero_restores_name(rig):
    s, slack = rig
    s.execute(task("<@U2> ++ x"))
    r = s.store.history("T1", "U1")[0]
    s.execute(task("recognition cancel " + r["id"]))
    assert slack.names["U2"] == "Person 2"
    assert s.execute(task("list-all"))["text"] == "No recognition yet."


def test_duplicate_task_does_not_repost(rig):
    s, slack = rig
    t = task("<@U2> ++ x")
    s.execute(t)
    s.execute(t)
    assert len(slack.posts) == 1 and s.store.total("T1", "U2") == 1


@pytest.mark.parametrize(
    "command",
    [
        "winner-of-the-day",
        "winner-of-the-month",
        "winner-of-the-year",
        "winner-between 2026-10-10 2026-10-10",
    ],
)
def test_winner_commands_and_ties(rig, command):
    s, _ = rig
    s.execute(task("<@U2> ++ x", "q1"))
    s.execute(task("<@U3> ++ x", "q2"))
    text = s.execute(task(command))["text"]
    assert "<@U2>" in text and "<@U3>" in text and "Asia/Kolkata" in text


def test_no_winners(rig):
    s, _ = rig
    assert "No recognition in this period" in s.execute(task("winner-of-the-day"))["text"]


@pytest.mark.parametrize("text", ["<@U1> ++ self", "<@UBOT> ++ bot"])
def test_invalid_recipients(rig, text):
    s, slack = rig
    with pytest.raises(UserError):
        s.execute(task(text))
    assert s.store.top("T1") == [] and slack.posts == []


def test_private_channel_visibility(rig):
    s, slack = rig
    s.execute(task("<@U2> ++ secret", "q1", channel="C2"))
    r = s.store.history("T1", "U1")[0]
    slack.allowed[("U3", "C2")] = False
    with pytest.raises(UserError):
        s.execute(task("recognition view " + r["id"], user="U3"))
    board = s.execute(task("list-all", user="U3"))
    assert board["blocks"][0]["rows"][1][3]["text"] == "No accessible thread"
    assert "secret" not in str(board)


def test_escape_reason_broadcast(rig):
    s, slack = rig
    s.execute(task("<@U2> ++ <!channel> <@U4> & cool"))
    text = slack.posts[0][1]
    assert "&lt;!channel&gt;" in text and "&lt;@U4&gt;" in text
    assert "<!channel>" not in text


def test_profile_block_does_not_lose_count(rig):
    s, slack = rig
    slack.fail_name = SlackFailure("not_allowed")
    text = s.execute(task("<@U2> ++ x"))["text"]
    assert "blocked" in text
    assert s.store.total("T1", "U2") == 1


def test_ambiguous_post_never_blind_retried(rig):
    s, slack = rig
    slack.fail_post = SlackFailure("transport_error", 5, True)
    response = s.execute(task("<@U2> ++ x"))
    assert "uncertain" in response["text"]
    slack.fail_post = None
    s.effects()
    assert slack.posts == [] and s.store.total("T1", "U2") == 1


def test_link_failure_does_not_duplicate_post(rig):
    s, slack = rig
    slack.fail_link = SlackFailure("not_allowed")
    s.execute(task("<@U2> ++ x"))
    s.effects()
    assert len(slack.posts) == 1


def test_names_do_not_accumulate_suffixes(rig):
    s, slack = rig
    s.execute(task("<@U2> ++ x", "1"))
    s.execute(task("<@U2> ++ x", "2"))
    assert slack.names["U2"] == "Person 2 🌟2"
    slack.names["U2"] = "New Name"
    s.execute(task("<@U2> ++ x", "3"))
    assert slack.names["U2"] == "New Name 🌟3"


def test_recipient_must_be_channel_member(rig):
    s, slack = rig
    slack.allowed[("U2", "C1")] = False
    with pytest.raises(UserError):
        s.execute(task("<@U2> ++ x"))
    assert s.store.total("T1", "U2") == 0


def test_worker_restart_processes_persisted_command(rig):
    s, slack = rig
    t = task("<@U2> ++ durable")
    s.store.enqueue(t["id"], t["payload"], t["created_at_us"])
    assert s.tick() is True
    assert slack.replies[0]["text"].startswith("Recognition K")
    assert s.tick() is False
    assert s.store.total("T1", "U2") == 1
