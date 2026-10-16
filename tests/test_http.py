import hashlib
import hmac
import time
from dataclasses import replace
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from karma_x.app import create_app


def signed(secret, data, timestamp=None):
    ts = str(int(time.time()) if timestamp is None else timestamp)
    body = urlencode(data).encode()
    digest = hmac.new(
        secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return body, {
        "x-slack-request-timestamp": ts,
        "x-slack-signature": "v0=" + digest,
        "content-type": "application/x-www-form-urlencoded",
    }


def payload(text="list-all"):
    return {
        "team_id": "T1",
        "user_id": "U1",
        "channel_id": "C1",
        "text": text,
        "command": "/karma-x",
        "response_url": "https://hooks.slack.com/commands/test",
    }


def test_signed_http_to_worker_to_slack(rig):
    s, slack = rig
    with TestClient(create_app(s.config, s, run_worker=False)) as client:
        data, headers = signed(s.config.signing_secret, payload("<@U2> ++ HTTP award"))
        response = client.post("/slack/commands", content=data, headers=headers)
        assert response.status_code == 200 and response.json()["text"] == "Processing…"
        # Advance the injected worker clock to the real request acceptance instant.
        s.clock = lambda: __import__("datetime").datetime.now(__import__("datetime").UTC)
        assert s.tick()
        assert slack.names["U2"] == "Person 2 🌟1"
        rid = s.store.history("T1", "U1")[0]["id"]
        assert rid not in slack.posts[0][1] and rid in slack.replies[0]["text"]
        client.post("/slack/commands", content=data, headers=headers)
        assert s.tick() is False
        assert s.store.total("T1", "U2") == 1


@pytest.mark.parametrize("timestamp", [0, 1, 1700000000])
def test_replay_rejected(rig, timestamp):
    s, _ = rig
    with TestClient(create_app(s.config, s, False)) as client:
        data, headers = signed(s.config.signing_secret, payload(), timestamp)
        assert client.post("/slack/commands", content=data, headers=headers).status_code == 401


def test_tampering_rejected(rig):
    s, _ = rig
    with TestClient(create_app(s.config, s, False)) as client:
        data, headers = signed(s.config.signing_secret, payload())
        assert (
            client.post("/slack/commands", content=data + b"x", headers=headers).status_code == 401
        )
        assert client.post("/slack/commands", content=data).status_code == 401


@pytest.mark.parametrize("field,value", [("team_id", "T2"), ("channel_id", "C3")])
def test_foreign_scope_not_enqueued(rig, field, value):
    s, _ = rig
    p = payload()
    p[field] = value
    with TestClient(create_app(s.config, s, False)) as client:
        data, headers = signed(s.config.signing_secret, p)
        assert (
            "not enabled"
            in client.post("/slack/commands", content=data, headers=headers).json()["text"]
        )
    assert s.store.claim(10**18) is None


def test_self_rejected_before_write(rig):
    s, _ = rig
    with TestClient(create_app(s.config, s, False)) as client:
        data, headers = signed(s.config.signing_secret, payload("<@U1> ++ self"))
        assert (
            "cannot give yourself"
            in client.post("/slack/commands", content=data, headers=headers).json()["text"]
        )
    assert s.store.claim(10**18) is None


def test_body_limit_and_hidden_docs(rig):
    s, _ = rig
    with TestClient(create_app(s.config, s, False)) as client:
        assert client.post("/slack/commands", content=b"x" * 20000).status_code == 413
        assert client.get("/docs").status_code == 404
        assert client.get("/health").json() == {"status": "ok", "bot": "karma-x"}


def test_duplicate_form_keys_rejected(rig):
    s, _ = rig
    with TestClient(create_app(s.config, s, False)) as client:
        data, headers = signed(
            s.config.signing_secret, list(payload().items()) + [("user_id", "U2")]
        )
        assert client.post("/slack/commands", content=data, headers=headers).status_code == 400


def test_wildcard_allows_any_channel_in_workspace(rig):
    s, _ = rig
    s.config = replace(s.config, channel_ids=frozenset({"*"}))
    p = payload()
    p["channel_id"] = "C9"
    with TestClient(create_app(s.config, s, False)) as client:
        data, headers = signed(s.config.signing_secret, p)
        assert client.post("/slack/commands", content=data, headers=headers).status_code == 200
    assert s.store.claim(10**18) is not None


def test_wildcard_still_rejects_foreign_workspace(rig):
    s, _ = rig
    s.config = replace(s.config, channel_ids=frozenset({"*"}))
    p = payload()
    p["team_id"] = "T2"
    with TestClient(create_app(s.config, s, False)) as client:
        data, headers = signed(s.config.signing_secret, p)
        assert (
            "not enabled"
            in client.post("/slack/commands", content=data, headers=headers).json()["text"]
        )
    assert s.store.claim(10**18) is None
