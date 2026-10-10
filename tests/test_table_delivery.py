import httpx
import pytest

from karma_x.slack import Slack, SlackFailure


def table_payload():
    return {
        "text": "Karma-X table",
        "blocks": [{"type": "table", "rows": [[{"type": "raw_text", "text": "Person"}]]}],
    }


def test_native_table_is_private_to_the_caller_using_slack_api(monkeypatch):
    slack = Slack("testing-token", "testing-profile")
    calls = []

    def api_call(method, **kwargs):
        calls.append((method, kwargs))
        return {"ok": True}

    monkeypatch.setattr(slack, "call", api_call)
    monkeypatch.setattr(
        httpx, "post", lambda *_args, **_kwargs: pytest.fail("Table sent to webhook")
    )
    payload = table_payload()
    slack.respond("https://hooks.slack.com/commands/test", payload, channel="C1", user="U1")
    assert len(calls) == 1
    assert calls[0][0] == "chat.postEphemeral"
    assert calls[0][1]["channel"] == "C1"
    assert calls[0][1]["user"] == "U1"
    assert calls[0][1]["blocks"] == payload["blocks"]


def test_table_rate_limit_is_a_known_rejected_reply(monkeypatch):
    slack = Slack("testing-token", "testing-profile")

    def limited(*_args, **_kwargs):
        raise SlackFailure("ratelimited", 3)

    monkeypatch.setattr(slack, "call", limited)
    with pytest.raises(SlackFailure) as error:
        slack.respond(
            "https://hooks.slack.com/commands/test", table_payload(), channel="C1", user="U1"
        )
    assert error.value.code == "response_rate_limited"
    assert error.value.retry_after == 3
    assert not error.value.ambiguous


def test_reply_target_comes_from_signed_caller_and_channel(rig, monkeypatch):
    service, slack = rig
    targets = []
    monkeypatch.setattr(slack, "respond", lambda _url, _payload, **target: targets.append(target))
    service.store.give("seed", "T1", "U1", "U2", "C1", "test", 1)
    service.store.enqueue(
        "list",
        {
            "team_id": "T1",
            "channel_id": "C1",
            "user_id": "U1",
            "text": "list-all",
            "response_url": "https://hooks.slack.com/commands/test",
        },
        1,
    )
    service.tick()
    assert targets == [{"channel": "C1", "user": "U1"}]
