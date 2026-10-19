"""Signed Slack HTTP in, worker out: ingress (P3) and message flow (P4) agree on payloads."""

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_events import signed_form, signed_json
from test_message_flow import make

from karma_x.app import create_app

SHOUTOUT_BLOCKS = [
    {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [{"type": "text", "text": "Big kudos to the team!"}],
            },
            {
                "type": "rich_text_list",
                "style": "bullet",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "user", "user_id": "U2"},
                            {
                                "type": "text",
                                "text": " — worked from the client office through go-live",
                            },
                        ],
                    },
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "user", "user_id": "U3"},
                            {"type": "text", "text": " — built the website integration end to end"},
                        ],
                    },
                ],
            },
        ],
    }
]


def event(text, event_id, ts, blocks=None, thread_ts=None):
    message = {"type": "message", "user": "U1", "channel": "C1", "text": text, "ts": ts}
    if blocks:
        message["blocks"] = blocks
    if thread_ts:
        message["thread_ts"] = thread_ts
    return {"type": "event_callback", "team_id": "T1", "event_id": event_id, "event": message}


def started(tmp_path, auto_award):
    """The route stamps jobs with the real time, so the worker clock must be real too."""
    service, slack, clock = make(tmp_path, auto_award=auto_award)
    clock.now = datetime.now(UTC) + timedelta(minutes=1)
    return service, slack


def drain(service):
    while service.tick():
        pass
    service.tick()


def test_shoutout_with_bullets_awards_both_through_http(tmp_path):
    service, slack = started(tmp_path, "rules")
    text = (
        "Big kudos to the team!\n• <@U2> — worked from the client office through go-live\n"
        "• <@U3> — built the website integration end to end"
    )
    with TestClient(create_app(service.config, service, run_worker=False)) as client:
        body, headers = signed_json("secret", event(text, "Ev1", "1700.000100", SHOUTOUT_BLOCKS))
        assert client.post("/slack", content=body, headers=headers).status_code == 200
        # A Slack retry of the same event is accepted and deduplicated.
        retry = {**headers, "x-slack-retry-num": "1"}
        assert client.post("/slack", content=body, headers=retry).status_code == 200
    drain(service)
    assert len(slack.posts) == 1
    assert "<@U2>" in slack.posts[0][1] and "<@U3>" in slack.posts[0][1]
    assert service.store.total("T1", "U2") == 1
    assert service.store.total("T1", "U3") == 1
    (summary,) = slack.ephemerals
    assert summary["user"] == "U1" and summary["text"].count("recognition cancel K") == 2


def test_thread_thanks_prompts_then_click_awards_through_http(tmp_path):
    service, slack = started(tmp_path, "off")
    with TestClient(create_app(service.config, service, run_worker=False)) as client:
        body, headers = signed_json(
            "secret",
            event(
                "Huge thanks to <@U2> for fixing the payment retries overnight",
                "Ev2",
                "1700.000200",
                thread_ts="1700.000001",
            ),
        )
        assert client.post("/slack", content=body, headers=headers).status_code == 200
        drain(service)
        assert slack.posts == []
        (prompt,) = slack.ephemerals
        assert prompt["ts"] == "1700.000001"
        award = next(
            element
            for block in prompt["blocks"]
            for element in block.get("elements", [])
            if element.get("action_id") == "karma_award"
        )
        payload = {
            "type": "block_actions",
            "team": {"id": "T1"},
            "user": {"id": "U1"},
            "channel": {"id": "C1"},
            "response_url": "https://hooks.slack.com/actions/T1/1/x",
            "actions": [
                {"action_id": "karma_award", "value": award["value"], "action_ts": "1700.5"}
            ],
        }
        body, headers = signed_form("secret", {"payload": json.dumps(payload)})
        assert client.post("/slack", content=body, headers=headers).status_code == 200
    drain(service)
    assert service.store.total("T1", "U2") == 1
    assert slack.threads == ["1700.000001"]
