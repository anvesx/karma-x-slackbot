import pytest

from karma_x.config import Config


@pytest.fixture
def env(monkeypatch):
    for key, value in {
        "SLACK_SIGNING_SECRET": "testing-signing-secret",
        "SLACK_BOT_TOKEN": "testing-token",
        "SLACK_TEAM_ID": "T1",
    }.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_listed_channels_only(env):
    env.setenv("ALLOWED_CHANNEL_IDS", "C1, C2")
    config = Config.from_env()
    assert config.allows_channel("C1") and config.allows_channel("C2")
    assert not config.allows_channel("C3")


def test_wildcard_allows_every_channel(env):
    env.setenv("ALLOWED_CHANNEL_IDS", "*")
    assert Config.from_env().allows_channel("C999")
