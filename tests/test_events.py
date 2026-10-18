import hashlib
import hmac
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
import yaml
from fastapi.testclient import TestClient

from karma_x.app import create_app
from karma_x.events import (
    ButtonClick,
    MessageDeleted,
    MessageEdited,
    MessagePosted,
    blocks_from_slack,
    parse_click,
    parse_event,
)
from karma_x.recognizer import Block


def sign(secret, body, content_type, timestamp=None, extra=None):
    ts = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(
        secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    headers = {
        "x-slack-request-timestamp": ts,
        "x-slack-signature": "v0=" + digest,
        "content-type": content_type,
    }
    return body, {**headers, **(extra or {})}


def signed_json(secret, data, **kw):
    return sign(secret, json.dumps(data).encode(), "application/json", **kw)


def signed_form(secret, data, **kw):
    return sign(secret, urlencode(data).encode(), "application/x-www-form-urlencoded", **kw)


RICH = [
    {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [
                    {"type": "text", "text": "thanks "},
                    {"type": "user", "user_id": "U2"},
                    {"type": "text", "text": " "},
                    {"type": "emoji", "name": "tada"},
                    {"type": "link", "url": "https://x", "text": "doc"},
                    {"type": "link", "url": "https://y"},
                    {"type": "broadcast", "range": "here"},
                    {"type": "usergroup", "usergroup_id": "S1"},
                ],
            },
            {
                "type": "rich_text_list",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "user", "user_id": "U3"},
                            {"type": "text", "text": " fixed CI"},
                        ],
                    },
                ],
            },
            {"type": "rich_text_quote", "elements": [{"type": "text", "text": "quoted"}]},
            {"type": "rich_text_preformatted", "elements": [{"type": "text", "text": "code"}]},
        ],
    },
    {"type": "section", "text": {"type": "mrkdwn", "text": "ignored"}},
]


def envelope(event, team="T1", event_id="Ev1"):
    return {"type": "event_callback", "team_id": team, "event_id": event_id, "event": event}


def message(**over):
    return {
        "type": "message",
        "channel": "C1",
        "user": "U1",
        "text": "thanks <@U2> for the review",
        "ts": "1700000000.000100",
        "blocks": RICH,
        **over,
    }


def click(action_id="karma_award", **over):
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "channel": {"id": "C1"},
        "user": {"id": "U1"},
        "response_url": "https://hooks.slack.com/actions/test",
        "actions": [{"action_id": action_id, "value": "cf_1", "action_ts": "1700000001.5"}],
        **over,
    }


def test_blocks_from_slack():
    assert blocks_from_slack(RICH) == (
        Block("section", "thanks <@U2> :tada:doc"),
        Block("list_item", "<@U3> fixed CI"),
    )
    assert blocks_from_slack(None) == ()


def test_parse_posted_payload_keys():
    item = parse_event(envelope(message(thread_ts="1699.1")))
    assert isinstance(item, MessagePosted) and item.key == "event:Ev1"
    assert item.to_payload() == {
        "kind": "message",
        "team_id": "T1",
        "channel_id": "C1",
        "user_id": "U1",
        "text": "thanks <@U2> for the review",
        "ts": "1700000000.000100",
        "thread_ts": "1699.1",
        "blocks": [
            {"kind": "section", "text": "thanks <@U2> :tada:doc"},
            {"kind": "list_item", "text": "<@U3> fixed CI"},
        ],
    }
    assert parse_event(envelope(message(blocks=None))).to_payload()["thread_ts"] is None


@pytest.mark.parametrize("subtype", ["thread_broadcast", "file_share"])
def test_accepted_posted_subtypes(subtype):
    assert isinstance(parse_event(envelope(message(subtype=subtype))), MessagePosted)


@pytest.mark.parametrize(
    "event",
    [
        message(bot_id="B1"),
        message(text="no mention here"),
        message(subtype="channel_join"),
        message(subtype="bot_message"),
        message(user=None),
        {"type": "reaction_added", "channel": "C1"},
    ],
)
def test_ignored_events(event):
    assert parse_event(envelope(event)) is None


def edit(new_text, old_text="thanks <@U2>", **message_over):
    return message(
        subtype="message_changed",
        user=None,
        text=None,
        ts="1700000009.0",
        message={"user": "U1", "text": new_text, "ts": "1700000000.000100", **message_over},
        previous_message={"user": "U1", "text": old_text, "ts": "1700000000.000100"},
    )


def test_parse_edit_and_delete():
    item = parse_event(envelope(edit("thanks <@U2> and <@U3>")))
    assert isinstance(item, MessageEdited)
    payload = item.to_payload()
    assert payload["kind"] == "message_changed" and payload["ts"] == "1700000000.000100"
    assert set(payload) == set(parse_event(envelope(message())).to_payload())
    assert parse_event(envelope(edit("thanks <@U2>"))) is None  # text unchanged
    assert parse_event(envelope(edit("thanks <@U2>", bot_id="B1"))) is None
    # Removing every mention still re-scores so the worker can cancel.
    assert isinstance(parse_event(envelope(edit("thanks all"))), MessageEdited)
    assert parse_event(envelope(edit("thanks all", old_text="hello"))) is None

    deleted = message(subtype="message_deleted", user=None, deleted_ts="1700000000.000100")
    item = parse_event(envelope(deleted))
    assert isinstance(item, MessageDeleted)
    assert item.to_payload() == {
        "kind": "message_deleted",
        "team_id": "T1",
        "channel_id": "C1",
        "ts": "1700000000.000100",
    }
    no_mention = {**deleted, "previous_message": {"text": "lunch?"}}
    assert parse_event(envelope(no_mention)) is None


def test_parse_click():
    item = parse_click(click())
    assert isinstance(item, ButtonClick) and item.key == "click:1700000001.5:U1"
    assert item.to_payload() == {
        "kind": "click",
        "team_id": "T1",
        "channel_id": "C1",
        "user_id": "U1",
        "confirmation_id": "cf_1",
        "award": True,
        "response_url": "https://hooks.slack.com/actions/test",
    }
    assert parse_click(click("karma_skip")).award is False
    assert parse_click(click("other")) is None
    assert parse_click(click(actions=[])) is None
    assert parse_click(click(type="view_submission")) is None


def queued(s):
    with s.store.connection() as db:
        rows = db.execute("SELECT id, payload FROM requests ORDER BY created_at_us").fetchall()
    return [(r["id"], json.loads(r["payload"])) for r in rows]


def test_url_verification(rig):
    s, _ = rig
    with TestClient(create_app(s.config, s, False)) as client:
        data, headers = signed_json(
            s.config.signing_secret, {"type": "url_verification", "challenge": "abc"}
        )
        response = client.post("/slack", content=data, headers=headers)
        assert response.status_code == 200 and response.json() == {"challenge": "abc"}


def test_event_enqueued_once_and_retry_acknowledged(rig):
    s, _ = rig
    with TestClient(create_app(s.config, s, False)) as client:
        data, headers = signed_json(s.config.signing_secret, envelope(message()))
        response = client.post("/slack", content=data, headers=headers)
        assert response.status_code == 200 and response.json() == {}
        data, headers = signed_json(
            s.config.signing_secret, envelope(message()), extra={"x-slack-retry-num": "1"}
        )
        assert client.post("/slack", content=data, headers=headers).status_code == 200
    rows = queued(s)
    assert [key for key, _ in rows] == ["event:Ev1"]
    assert rows[0][1]["kind"] == "message"


@pytest.mark.parametrize(
    "body",
    [
        envelope(message(bot_id="B1")),
        envelope(message(text="no mention")),
        envelope(message(), team="T9"),
        envelope(message(channel="C9")),
    ],
)
def test_ignored_events_not_enqueued(rig, body):
    s, _ = rig
    with TestClient(create_app(s.config, s, False)) as client:
        data, headers = signed_json(s.config.signing_secret, body)
        assert client.post("/slack", content=data, headers=headers).status_code == 200
    assert queued(s) == []


def test_click_enqueued(rig):
    s, _ = rig
    with TestClient(create_app(s.config, s, False)) as client:
        data, headers = signed_form(s.config.signing_secret, {"payload": json.dumps(click())})
        response = client.post("/slack", content=data, headers=headers)
        assert response.status_code == 200 and response.json() == {}
        foreign = click(team={"id": "T9"})
        data, headers = signed_form(s.config.signing_secret, {"payload": json.dumps(foreign)})
        assert client.post("/slack", content=data, headers=headers).status_code == 200
    rows = queued(s)
    assert [key for key, _ in rows] == ["click:1700000001.5:U1"]
    assert rows[0][1]["confirmation_id"] == "cf_1"


def test_rejections(rig):
    s, _ = rig
    secret = s.config.signing_secret
    with TestClient(create_app(s.config, s, False)) as client:
        data, headers = signed_json(secret, envelope(message()))
        tampered = {**headers, "x-slack-signature": "v0=" + "0" * 64}
        assert client.post("/slack", content=data, headers=tampered).status_code == 401
        stale, stale_headers = signed_json(secret, envelope(message()), timestamp=1700000000)
        assert client.post("/slack", content=stale, headers=stale_headers).status_code == 401
        assert client.post("/slack", content=b"x" * 140000).status_code == 413
        # 20 KB fits /slack (128 KB) though /slack/commands caps at 16 KB.
        big = envelope(message(text="<@U2> " + "x" * 20000))
        data, headers = signed_json(secret, big)
        assert client.post("/slack", content=data, headers=headers).status_code == 200
        bad = [
            sign(secret, b"{not json", "application/json"),
            sign(secret, b"[]", "application/json"),
            signed_json(secret, {"type": "app_rate_limited"}),
            signed_json(secret, {"type": "url_verification"}),
            signed_form(secret, {"payload": json.dumps({"type": "view_submission"})}),
            signed_form(secret, {"command": "/karma-x"}),
            signed_form(secret, {"payload": "{bad"}),
            sign(secret, b"hello", "text/plain"),
        ]
        for data, headers in bad:
            assert client.post("/slack", content=data, headers=headers).status_code == 400


def test_database_busy_returns_503(rig, monkeypatch):
    s, _ = rig

    def busy(*_args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(s.store, "enqueue", busy)
    with TestClient(create_app(s.config, s, False)) as client:
        data, headers = signed_json(s.config.signing_secret, envelope(message()))
        assert client.post("/slack", content=data, headers=headers).status_code == 503


def test_manifest_points_events_and_interactivity_at_slack():
    manifest = yaml.safe_load((Path(__file__).parents[1] / "slack-manifest.yaml").read_text())
    settings = manifest["settings"]
    assert settings["event_subscriptions"]["request_url"].endswith("/slack")
    assert settings["event_subscriptions"]["bot_events"] == ["message.channels", "message.groups"]
    assert settings["interactivity"] == {
        "is_enabled": True,
        "request_url": "https://REPLACE_WITH_YOUR_HOST/slack",
    }
    command = manifest["features"]["slash_commands"][0]
    assert command["url"].endswith("/slack/commands")
