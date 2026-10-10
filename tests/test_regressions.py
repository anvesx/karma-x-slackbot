from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from slack_sdk.web.slack_response import SlackResponse

from karma_x.app import create_app
from karma_x.core import UserError, utc_us
from karma_x.slack import Slack, SlackFailure
from karma_x.store import Store


def request(text, key="regression"):
    return {
        "id": key,
        "created_at_us": utc_us(datetime(2026, 10, 10, 12, tzinfo=UTC)),
        "payload": {
            "team_id": "T1",
            "user_id": "U1",
            "channel_id": "C1",
            "text": text,
            "response_url": "https://hooks.slack.com/commands/test",
        },
    }


@pytest.mark.parametrize("failure", ["timeout", "crash"])
def test_profile_applied_before_failure_survives_restart_and_cancel(rig, monkeypatch, failure):
    service, slack = rig

    def apply_then_fail(uid, name):
        slack.names[uid] = name
        if failure == "crash":
            raise SystemExit("process stopped after remote write")
        raise SlackFailure("transport_error", 5)

    monkeypatch.setattr(slack, "name", apply_then_fail)
    if failure == "crash":
        with pytest.raises(SystemExit):
            service.execute(request("<@U2> ++ helpful"))
    else:
        service.execute(request("<@U2> ++ helpful"))
    restored = type(service)(
        service.config,
        Store(service.config.database),
        slack,
        lambda: datetime(2026, 10, 10, 12, tzinfo=UTC) + timedelta(seconds=6),
    )
    restored.effects()
    assert slack.names["U2"] == "Person 2 🌟1"
    assert restored.store.profile_names("T1", "U2")["base_name"] == "Person 2"
    monkeypatch.setattr(slack, "name", lambda uid, name: slack.names.__setitem__(uid, name))
    rid = restored.store.history("T1", "U1")[0]["id"]
    restored.execute(request(f"recognition cancel {rid}", "cancel"))
    assert restored.store.total("T1", "U2") == 0
    assert slack.names["U2"] == "Person 2"


def test_slack_connect_foreign_member_rejected_even_when_channel_member(rig, monkeypatch):
    service, slack = rig
    original = slack.user
    monkeypatch.setattr(
        slack,
        "user",
        lambda uid: {
            **original(uid),
            "team_id": "TOTHER" if uid == "U2" else "T1",
        },
    )
    with pytest.raises(UserError, match="workspace"):
        service.execute(request("<@U2> ++ foreign member"))
    assert service.store.total("T1", "U2") == 0
    assert not slack.posts


@pytest.mark.parametrize("code", ["internal_error", "fatal_error"])
def test_slack_sdk_partial_success_errors_are_ambiguous_at_http_200(monkeypatch, code):
    slack = Slack("testing-token", "testing-profile")

    def api_call(method, **kwargs):
        return SlackResponse(
            client=slack.bot,
            http_verb="POST",
            api_url="https://slack.com/api/" + method,
            req_args=kwargs,
            data={"ok": False, "error": code},
            headers={},
            status_code=200,
        ).validate()

    monkeypatch.setattr(slack.bot, "api_call", api_call)
    with pytest.raises(SlackFailure) as error:
        slack.post("C1", "one physical message", "Ktest")
    assert error.value.ambiguous


@pytest.mark.parametrize("timestamp", ["garbage", "NaN", "1e999", "-1", "9" * 5000])
def test_invalid_untrusted_timestamp_returns_401_without_queue(rig, timestamp):
    service, _ = rig
    with TestClient(
        create_app(service.config, service=service, run_worker=False), raise_server_exceptions=False
    ) as client:
        response = client.post(
            "/slack/commands",
            content=b"text=list-all",
            headers={
                "x-slack-request-timestamp": timestamp,
                "x-slack-signature": "v0=invalid",
                "content-type": "application/x-www-form-urlencoded",
            },
        )
    assert response.status_code == 401
    assert service.store.claim(utc_us(datetime.now(UTC))) is None


def test_history_bounds_escaped_reason_but_id_view_preserves_full_text(rig):
    service, _ = rig
    for i in range(10):
        service.store.give(str(i), "T1", "U1", "U2", "C1", "&" * 2000, i)
    history = service.execute(request("recognition view"))["text"]
    assert len(history) < 20000
    rid = service.store.history("T1", "U1")[0]["id"]
    detail = service.execute(request(f"recognition view {rid}"))["text"]
    assert "&amp;" * 2000 in detail


def test_latest_accessible_thread_can_be_older_than_100_private_posts(rig):
    service, slack = rig
    public = service.store.give("public", "T1", "U1", "U2", "C1", "public", 1)
    link = "https://test.slack.com/archives/C1/p123"
    service.store.record_link(public["id"], link)
    for i in range(100):
        private = service.store.give(str(i), "T1", "U1", "U2", "C2", "private", i + 2)
        service.store.record_link(private["id"], "https://test.slack.com/archives/C2/private")
    slack.allowed[("U3", "C2")] = False
    board = service.execute(
        {
            **request("list-all"),
            "payload": {
                **request("list-all")["payload"],
                "user_id": "U3",
            },
        }
    )
    assert link in str(board)
    assert "archives/C2" not in str(board)


def test_known_rejected_reply_429_retries_cached_result_without_second_award(rig, monkeypatch):
    service, slack = rig
    attempts = []
    original = slack.respond

    def respond(url, payload, **context):
        attempts.append(payload)
        if len(attempts) == 1:
            raise SlackFailure("response_rate_limited", 1)
        original(url, payload, **context)

    monkeypatch.setattr(slack, "respond", respond)
    r = request("<@U2> ++ one award")
    service.store.enqueue(r["id"], r["payload"], r["created_at_us"])
    service.tick()
    service.clock = lambda: datetime(2026, 10, 10, 12, tzinfo=UTC) + timedelta(seconds=10)
    service.tick()
    assert len(slack.replies) == 1
    assert attempts[0] == attempts[1]
    assert service.store.total("T1", "U2") == 1
    assert len(slack.posts) == 1


@pytest.mark.parametrize("code", ["channel_not_found", "no_permission", "access_denied"])
def test_inaccessible_historical_channel_does_not_hide_accessible_leaderboard(
    rig, monkeypatch, code
):
    service, slack = rig
    public = service.store.give("public", "T1", "U1", "U2", "C1", "public", 1)
    private = service.store.give("private", "T1", "U1", "U2", "C2", "private", 2)
    link = "https://test.slack.com/archives/C1/p123"
    service.store.record_link(public["id"], link)
    service.store.record_link(private["id"], "https://test.slack.com/archives/C2/private")
    original = slack.can_access

    def access(user, channel):
        if channel == "C2":
            raise SlackFailure(code)
        return original(user, channel)

    monkeypatch.setattr(slack, "can_access", access)
    board = service.execute(request("list-all"))
    assert board["blocks"][0]["rows"][1][2]["value"] == 2
    assert link in str(board)
    assert "archives/C2" not in str(board)
    history = service.execute(request("recognition view"))["text"]
    assert public["id"] in history
    assert private["id"] not in history


@pytest.mark.parametrize("code", ["ratelimited", "invalid_auth", "missing_scope", "internal_error"])
def test_access_check_does_not_hide_global_auth_or_transient_failure(rig, monkeypatch, code):
    service, slack = rig
    service.store.give("private", "T1", "U1", "U2", "C2", "private", 1)

    def access(user, channel):
        if channel == "C2":
            raise SlackFailure(code)
        return True

    monkeypatch.setattr(slack, "can_access", access)
    with pytest.raises(SlackFailure) as error:
        service.execute(request("list-all"))
    assert error.value.code == code


@pytest.mark.parametrize(
    ("method", "parameters", "encoding", "verb"),
    [
        ("users.info", {"user": "U1"}, "params", "GET"),
        ("conversations.members", {"channel": "C1", "limit": 200}, "params", "GET"),
        ("chat.getPermalink", {"channel": "C1", "message_ts": "1.1"}, "params", "GET"),
        ("users.profile.set", {"user": "U1", "profile": {"display_name": "Name"}}, "json", "POST"),
    ],
)
def test_adapter_uses_official_sdk_method_encoding(monkeypatch, method, parameters, encoding, verb):
    slack = Slack("testing-token", "testing-profile")
    calls = []

    def api_call(api_method, **kwargs):
        calls.append((api_method, kwargs))
        return SlackResponse(
            client=slack.bot,
            http_verb=verb,
            api_url="https://slack.com/api/" + api_method,
            req_args=kwargs,
            data={"ok": True},
            headers={},
            status_code=200,
        )

    monkeypatch.setattr(slack.bot, "api_call", api_call)
    assert slack.call(method, **parameters)["ok"]
    assert calls[0][0] == method
    assert calls[0][1].get("http_verb", "POST") == verb
    assert all(calls[0][1].get(encoding, {}).get(key) == value for key, value in parameters.items())


def test_terminal_slack_command_error_replies_instead_of_leaving_processing(rig, monkeypatch):
    service, slack = rig

    def blocked_user(_user):
        raise SlackFailure("missing_scope")

    monkeypatch.setattr(slack, "user", blocked_user)
    queued = request("<@U2> ++ rejected before award")
    service.store.enqueue(queued["id"], queued["payload"], queued["created_at_us"])
    service.tick()
    assert len(slack.replies) == 1
    assert "missing_scope" in slack.replies[0]["text"]
    assert service.store.total("T1", "U2") == 0
    service.tick()
    assert len(slack.replies) == 1
