import logging

import httpx
import uvicorn

from karma_x import app


def test_runtime_does_not_log_private_slack_response_url(rig, monkeypatch, caplog):
    service, _ = rig
    monkeypatch.setattr(app.Config, "from_env", lambda: service.config)
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: None)
    caplog.set_level(logging.INFO)
    app.main()
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    ) as client:
        client.post("https://hooks.slack.com/commands/private-response-credential")
    logging.getLogger("karma_x").info("safe runtime marker")
    assert "safe runtime marker" in caplog.text
    assert "private-response-credential" not in caplog.text
    assert "hooks.slack.com" not in caplog.text
