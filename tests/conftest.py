from datetime import UTC, datetime

import pytest

from karma_x.config import Config
from karma_x.core import UserError
from karma_x.service import Service
from karma_x.slack import Slack
from karma_x.store import Store


class FakeSlack(Slack):
    def __init__(self):
        self.names = {f"U{i}": f"Person {i}" for i in range(100)}
        self.posts = []
        self.replies = []
        self.updates = []
        self.allowed = {}
        self.fail_post = None
        self.fail_name = None
        self.fail_link = None

    def user(self, uid):
        if uid == "UBOT":
            raise UserError("Recognize an active person, not a bot or deleted member.")
        return {
            "id": uid,
            "team_id": "T1",
            "tz": "Asia/Kolkata",
            "profile": {"display_name": self.names.get(uid, uid)},
        }

    def can_access(self, uid, channel):
        return self.allowed.get((uid, channel), True)

    def post(self, channel, text, rid):
        if self.fail_post:
            raise self.fail_post
        self.posts.append((channel, text, rid))
        return f"12345.{len(self.posts):06d}"

    def permalink(self, channel, ts):
        if self.fail_link:
            raise self.fail_link
        return f"https://test.slack.com/archives/{channel}/p{ts.replace('.', '')}"

    def update(self, channel, ts, text):
        self.updates.append((channel, ts, text))

    def name(self, uid, name):
        if self.fail_name:
            raise self.fail_name
        self.names[uid] = name

    def respond(self, url, payload, **_context):
        self.replies.append(payload)


@pytest.fixture
def rig(tmp_path):
    config = Config(
        "testing-signing-secret",
        "testing-token",
        "testing-profile",
        "T1",
        frozenset({"C1", "C2"}),
        tmp_path / "karma.db",
    )
    slack = FakeSlack()
    store = Store(config.database)

    def clock():
        return datetime(2026, 10, 10, 12, tzinfo=UTC)

    return Service(config, store, slack, clock), slack
