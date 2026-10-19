"""P4: automatic karma from messages, R1-R8, driven through the worker with FakeSlack."""

import re
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import FakeSlack

from karma_x.config import Config
from karma_x.core import UserError, utc_us
from karma_x.recognizer import NoModel
from karma_x.service import Service
from karma_x.slack import Slack, SlackFailure
from karma_x.store import SCHEMA, Store

KUDOS = "Huge kudos to <@U2> and <@U3> for shipping Mayave through the ERP change"
CANCEL_ID = re.compile(r"cancel (K[0-9a-f]{16})")


class MessageSlack(FakeSlack):
    """FakeSlack plus thread-aware posts and chat.postEphemeral."""

    def __init__(self):
        super().__init__()
        self.threads = []
        self.ephemerals = []

    def post(self, channel, text, rid, thread_ts=None):
        self.threads.append(thread_ts)
        return super().post(channel, text, rid)

    def ephemeral(self, channel, user, text, blocks, thread_ts=None):
        self.ephemerals.append(
            {"channel": channel, "user": user, "text": text, "blocks": blocks, "ts": thread_ts}
        )


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 10, 12, tzinfo=UTC)

    def __call__(self):
        return self.now


def make(tmp_path, auto_award="rules", scorer=None):
    config = Config(
        "secret",
        "token",
        "profile",
        "T1",
        frozenset({"C1"}),
        tmp_path / "karma.db",
        auto_award=auto_award,
    )
    slack, clock = MessageSlack(), Clock()
    return Service(config, Store(config.database), slack, clock, scorer), slack, clock


@pytest.fixture
def flow(tmp_path):
    return make(tmp_path)


def message(text, ts="1700.000100", thread_ts=None, user="U1", kind="message", blocks=()):
    return {
        "kind": kind,
        "team_id": "T1",
        "channel_id": "C1",
        "user_id": user,
        "text": text,
        "ts": ts,
        "thread_ts": thread_ts,
        "blocks": [{"kind": k, "text": t} for k, t in blocks],
    }


def deleted(ts="1700.000100"):
    return {"kind": "message_deleted", "team_id": "T1", "channel_id": "C1", "ts": ts}


def click(cid, user="U1", award=True):
    return {
        "kind": "click",
        "team_id": "T1",
        "channel_id": "C1",
        "user_id": user,
        "confirmation_id": cid,
        "award": award,
        "response_url": "https://hooks.slack.com/actions/T1/1/x",
    }


def command(text, key):
    return {
        "team_id": "T1",
        "user_id": "U1",
        "channel_id": "C1",
        "text": text,
        "response_url": "https://hooks.slack.com/commands/test",
    }


def run(service, clock, key, payload):
    assert service.store.enqueue(key, payload, utc_us(clock.now))
    assert service.tick()


def prompt_ids(eph):
    return [e["value"] for b in eph["blocks"] if b["type"] == "actions" for e in b["elements"]]


def total(service, uid):
    return service.store.total("T1", uid)


def test_recognition_awards_each_person_with_one_combined_post(flow):
    service, slack, clock = flow
    run(service, clock, "event:E1", message(KUDOS))
    assert total(service, "U2") == total(service, "U3") == 1
    assert len(slack.posts) == 1 and slack.threads == [None]
    post = slack.posts[0][1]
    assert post.startswith("<@U1> recognized:\n")
    assert "• <@U2> · 🌟 +1" in post and "• <@U3> · 🌟 +1" in post
    (summary,) = slack.ephemerals
    assert summary["user"] == "U1" and summary["channel"] == "C1"
    ids = CANCEL_ID.findall(summary["text"])
    assert len(ids) == 2 and not any(i in post for i in ids)
    rows = service.store.message_recognitions("T1", "C1", "1700.000100")
    assert {r["origin"] for r in rows} == {"message"}
    assert len({r["group_id"] for r in rows}) == 1
    assert len({r["permalink"] for r in rows}) == 1 and rows[0]["permalink"]


def test_thread_reply_posts_in_the_same_thread(flow):
    service, slack, clock = flow
    run(service, clock, "event:E1", message(KUDOS, ts="1700.2", thread_ts="1700.1"))
    assert slack.threads == ["1700.1"]
    assert slack.ephemerals[0]["ts"] == "1700.1"


def test_single_recipient_uses_todays_post_text(flow):
    service, slack, clock = flow
    text = "Huge kudos to <@U2> for shipping Mayave through the ERP change"
    run(service, clock, "event:E1", message(text))
    assert slack.posts[0][1].startswith("<@U1> recognized <@U2> · 🌟 +1\n")


def test_cancel_one_person_rerenders_the_combined_post(flow):
    service, slack, clock = flow
    run(service, clock, "event:E1", message(KUDOS))
    first = CANCEL_ID.findall(slack.ephemerals[0]["text"])[0]
    run(service, clock, "cmd1", command(f"recognition cancel {first}", "cmd1"))
    assert slack.replies[-1]["text"].startswith("Canceled.")
    (update,) = slack.updates
    assert "~<@U2>~ canceled" in update[2] and "• <@U3> · 🌟 +1" in update[2]
    assert total(service, "U2") == 0 and total(service, "U3") == 1


def test_recipient_checks_skip_bots_and_outsiders(flow):
    service, slack, clock = flow
    slack.allowed[("U3", "C1")] = False
    text = "Huge kudos to <@UBOT>, <@U2> and <@U3> for shipping Mayave through the ERP change"
    run(service, clock, "event:E1", message(text))
    assert total(service, "U2") == 1 and total(service, "U3") == 0
    summary = slack.ephemerals[0]["text"]
    assert "<@UBOT> wasn't counted" in summary and "<@U3> wasn't counted" in summary
    assert slack.posts[0][1].startswith("<@U1> recognized <@U2>")


def test_daily_limit_tells_the_author_privately(flow):
    service, slack, clock = flow
    text = "Huge kudos to <@U2> for shipping Mayave through the ERP change"
    run(service, clock, "event:E1", message(text, ts="1.1"))
    run(service, clock, "event:E2", message(text, ts="1.2"))
    assert total(service, "U2") == 1 and len(slack.posts) == 1
    assert slack.ephemerals[-1]["text"] == (
        "You've already given <@U2> automatic karma today, so this one wasn't counted."
    )
    clock.now += timedelta(days=1)
    run(service, clock, "event:E3", message(text, ts="1.3"))
    assert total(service, "U2") == 2


def test_command_karma_does_not_count_toward_the_daily_limit(flow):
    service, slack, clock = flow
    run(service, clock, "cmd1", command("<@U2> ++ great work", "cmd1"))
    run(service, clock, "event:E1", message(KUDOS))
    assert total(service, "U2") == 2


def test_confirm_then_award_by_the_author(flow):
    service, slack, clock = flow
    run(service, clock, "event:E1", message("thanks <@U4> for the link"))
    assert not slack.posts and total(service, "U4") == 0
    (prompt,) = slack.ephemerals
    assert "Give <@U4> karma for" in prompt["blocks"][0]["text"]["text"]
    actions = prompt["blocks"][1]["elements"]
    assert [a["action_id"] for a in actions] == ["karma_award", "karma_skip"]
    cid = actions[0]["value"]
    run(service, clock, "click:1:U9", click(cid, user="U9"))
    assert slack.replies[-1]["text"] == "Only the author can award this."
    assert total(service, "U4") == 0
    run(service, clock, "click:2:U1", click(cid))
    assert slack.replies[-1]["replace_original"] is True
    assert "Saved! <@U4> now has 🌟1" in slack.replies[-1]["text"]
    assert total(service, "U4") == 1 and len(slack.posts) == 1
    run(service, clock, "click:3:U1", click(cid))
    assert slack.replies[-1]["text"] == "This karma request was already awarded."
    assert total(service, "U4") == 1


def test_award_applies_recipient_checks_and_daily_limit_at_click_time(flow):
    service, slack, clock = flow
    run(service, clock, "event:E1", message("thanks <@U4> for the link", ts="1.1"))
    cid = prompt_ids(slack.ephemerals[0])[0]
    slack.allowed[("U4", "C1")] = False
    run(service, clock, "click:1:U1", click(cid))
    assert "wasn't counted" in slack.replies[-1]["text"] and total(service, "U4") == 0
    slack.allowed[("U4", "C1")] = True
    text = "Huge kudos to <@U4> for shipping Mayave through the ERP change"
    run(service, clock, "event:E2", message(text, ts="1.2"))
    run(service, clock, "click:2:U1", click(cid))
    assert "already given <@U4> automatic karma today" in slack.replies[-1]["text"]
    assert total(service, "U4") == 1


def test_skip_and_expiry_give_nothing(flow):
    service, slack, clock = flow
    run(service, clock, "event:E1", message("thanks <@U4> for the link", ts="1.1"))
    run(service, clock, "event:E2", message("thanks <@U5> for the link", ts="1.2"))
    skip, late = prompt_ids(slack.ephemerals[0])[1], prompt_ids(slack.ephemerals[1])[0]
    run(service, clock, "click:1:U1", click(skip, award=False))
    assert slack.replies[-1]["text"] == "Skipped. No karma given to <@U4>."
    clock.now += timedelta(hours=24)
    run(service, clock, "click:2:U1", click(late))
    assert slack.replies[-1]["text"] == "This karma request expired after 24 hours."
    assert service.store.confirmation("T1", late)["state"] == "expired"
    assert total(service, "U4") == total(service, "U5") == 0
    run(service, clock, "click:3:U1", click("Cmissing"))
    assert slack.replies[-1]["text"] == "This karma request is no longer available."


def test_auto_award_off_turns_every_auto_into_a_prompt(tmp_path):
    service, slack, clock = make(tmp_path, auto_award="off")
    assert isinstance(service.scorer, NoModel)
    run(service, clock, "event:E1", message(KUDOS))
    assert not slack.posts and total(service, "U2") == 0
    assert len(prompt_ids(slack.ephemerals[0])) == 4


def test_rules_mode_uses_trust_rules(flow):
    assert isinstance(flow[0].scorer, NoModel) and flow[0].rules_auto


def test_deleting_the_message_cancels_its_karma(flow):
    service, slack, clock = flow
    run(service, clock, "event:E1", message(KUDOS + ". thanks <@U4> for the link"))
    cid = prompt_ids(slack.ephemerals[0])[0]
    run(service, clock, "event:E2", deleted())
    assert total(service, "U2") == total(service, "U3") == 0
    assert len(slack.ephemerals) == 1
    assert "~<@U2>~ canceled" in slack.updates[-1][2]
    assert "~<@U3>~ canceled" in slack.updates[-1][2]
    assert service.store.confirmation("T1", cid)["state"] == "expired"


def test_edit_rescores_cancels_and_awards(flow):
    service, slack, clock = flow
    run(service, clock, "event:E1", message(KUDOS))
    edit = "Huge kudos to <@U2> and <@U5> for shipping Mayave through the ERP change"
    run(service, clock, "event:E2", message(edit, kind="message_changed"))
    assert total(service, "U2") == 1 and total(service, "U3") == 0 and total(service, "U5") == 1
    summary = slack.ephemerals[-1]["text"]
    assert "Saved! <@U5>" in summary and "Canceled <@U3>'s karma" in summary
    assert "<@U2>" not in summary
    assert len(slack.posts) == 2
    run(service, clock, "event:E3", message(edit, kind="message_changed"))
    assert len(slack.ephemerals) == 2 and len(slack.posts) == 2


def test_replayed_event_never_awards_twice(flow):
    service, slack, clock = flow
    run(service, clock, "event:E1", message(KUDOS))
    assert not service.store.enqueue("event:E1", message(KUDOS), utc_us(clock.now))
    task = {"id": "event:E1", "created_at_us": utc_us(clock.now), "payload": message(KUDOS)}
    service.execute(task)
    run(service, clock, "event:E1-redelivered", message(KUDOS))
    assert total(service, "U2") == total(service, "U3") == 1
    assert len(slack.posts) == 1


def test_chatter_finishes_without_a_reply(flow):
    service, slack, clock = flow
    run(service, clock, "event:E1", message("<@U2> can you review my PR? thanks"))
    other = message(KUDOS)
    other["channel_id"] = "C9"
    run(service, clock, "event:E2", other)
    run(service, clock, "event:E3", message(KUDOS, user="UBOT"))
    run(service, clock, "event:E4", {**deleted(), "team_id": "T9"})
    assert not slack.ephemerals and not slack.posts
    with service.store.connection() as db:
        states = {r[0] for r in db.execute("SELECT state FROM requests")}
    assert states == {"done"}


def test_rich_text_list_items_inherit_the_opening_kudos(flow):
    service, slack, clock = flow
    blocks = [
        ("section", "Big kudos to the team!"),
        ("list_item", "<@U2> — worked from the client office through go-live"),
        ("list_item", "<@U3> — built the website integration"),
    ]
    text = "\n".join(t for _, t in blocks)
    run(service, clock, "event:E1", message(text, blocks=blocks))
    assert total(service, "U2") == total(service, "U3") == 1


def test_slash_command_flow_is_unchanged(flow):
    service, slack, clock = flow
    run(service, clock, "cmd1", command("<@U2> ++ great work", "cmd1"))
    assert slack.posts[0][1] == "<@U1> recognized <@U2> · 🌟 +1\ngreat work"
    assert slack.threads == [None] and not slack.ephemerals
    assert "Saved! <@U2> now has 🌟1" in slack.replies[-1]["text"]
    (row,) = service.store.history("T1", "U1")
    assert row["origin"] == "command" and row["group_id"] is None


def test_message_reply_failure_follows_the_reply_retry_rules(flow):
    service, slack, clock = flow

    def limited(*_args, **_kwargs):
        raise SlackFailure("response_rate_limited", 3)

    slack.ephemeral = limited
    run(service, clock, "event:E1", message(KUDOS))
    with service.store.connection() as db:
        state, error = db.execute("SELECT state,error FROM requests").fetchone()
    assert (state, error) == ("pending", "response_rate_limited")
    assert total(service, "U2") == 1


def test_migration_is_additive_and_idempotent(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute(
        "INSERT INTO recognitions(id,request_id,team,sender,recipient,channel,reason,created_at_us) "
        "VALUES('Kold','r','T1','U1','U2','C1','x',1)"
    )
    db.commit()
    db.close()
    Store(path)
    store = Store(path)
    row = store.get("T1", "Kold")
    assert row["origin"] == "command" and row["source_ts"] is None and row["group_id"] is None
    with store.connection() as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
    assert {"confirmations", "confirmation_once", "message_recognition_once"} <= names


def test_store_refuses_a_second_active_award_for_the_same_message(tmp_path):
    store = Store(tmp_path / "k.db")
    day = (0, 10**18)
    (first,) = store.give_from_message("t1", "T1", "U1", "C1", "1.1", None, [("U2", "r")], 5, day)
    (again,) = store.give_from_message("t2", "T1", "U1", "C1", "1.1", None, [("U2", "r")], 6, day)
    assert first.outcome == "awarded" and again == type(first)(
        "U2", "existing", first.recognition_id
    )
    assert store.give_from_message("t3", "T1", "U1", "C1", "1.2", None, [("U1", "r")], 7, day) == []
    with pytest.raises(sqlite3.IntegrityError), store.connection() as db:
        db.execute(
            "INSERT INTO recognitions(id,request_id,team,sender,recipient,channel,reason,"
            "created_at_us,origin,source_ts) VALUES('Kdup','x','T1','U3','U2','C1','r',1,'message','1.1')"
        )


class Recorder(Slack):
    def __init__(self, failure=None):
        super().__init__("xoxb-test", "")
        self.calls = []
        self.failure = failure

    def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if self.failure:
            raise self.failure
        return {"ts": "9.9"}


def test_slack_post_and_ephemeral_carry_thread_ts():
    slack = Recorder()
    assert slack.post("C1", "hi", "K1", thread_ts="1.1") == "9.9"
    slack.post("C1", "hi", "K1")
    slack.ephemeral("C1", "U1", "hi", [{"type": "section"}], "1.1")
    slack.ephemeral("C1", "U1", "hi", [])
    (_, threaded), (_, top), (_, eph), (_, plain) = slack.calls
    assert threaded["thread_ts"] == "1.1" and "thread_ts" not in top
    assert eph["thread_ts"] == "1.1" and eph["blocks"] and eph["posting"]
    assert "blocks" not in plain and "thread_ts" not in plain


def test_ephemeral_rate_limit_maps_to_reply_retry():
    with pytest.raises(SlackFailure) as caught:
        Recorder(SlackFailure("ratelimited", 7)).ephemeral("C1", "U1", "hi", [])
    assert (caught.value.code, caught.value.retry_after) == ("response_rate_limited", 7)
    with pytest.raises(SlackFailure) as caught:
        Recorder(SlackFailure("channel_not_found")).ephemeral("C1", "U1", "hi", [])
    assert caught.value.code == "channel_not_found"


def test_respond_accepts_action_urls(monkeypatch):
    sent = []

    def fake_post(url, **kwargs):
        sent.append(url)
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "post", fake_post)
    slack = Slack("xoxb-test", "")
    slack.respond("https://hooks.slack.com/actions/T1/1/x", {"text": "ok"})
    assert sent == ["https://hooks.slack.com/actions/T1/1/x"]
    with pytest.raises(UserError):
        slack.respond("https://hooks.slack.com/other/T1", {"text": "ok"})


def test_config_reads_auto_award(monkeypatch):
    for key, value in {
        "SLACK_SIGNING_SECRET": "s",
        "SLACK_BOT_TOKEN": "t",
        "SLACK_TEAM_ID": "T1",
        "ALLOWED_CHANNEL_IDS": "C1",
    }.items():
        monkeypatch.setenv(key, value)
    assert Config.from_env().auto_award == "off"
    monkeypatch.setenv("AUTO_AWARD", "rules")
    assert Config.from_env().auto_award == "rules"
    monkeypatch.setenv("AUTO_AWARD", "model")
    with pytest.raises(ValueError):
        Config.from_env()
