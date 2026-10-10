"""Use one actual uvicorn event loop: TestClient instances hide event-loop blocking."""

import hashlib
import hmac
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import httpx
import uvicorn

from karma_x.app import create_app


def test_busy_sqlite_does_not_serialize_slack_acknowledgments(rig):
    service, _ = rig
    app = create_app(service.config, service=service, run_worker=False)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    url = f"http://127.0.0.1:{port}/slack/commands"
    timestamp = str(int(time.time()))
    try:
        with (
            service.store.connection() as blocker,
            httpx.Client(timeout=8, trust_env=False) as client,
        ):
            blocker.execute("BEGIN IMMEDIATE")

            def send(i):
                body = urlencode(
                    {
                        "team_id": "T1",
                        "user_id": "U1",
                        "channel_id": "C1",
                        "text": f"<@U2> ++ request {i}",
                        "command": "/karma-x",
                        "response_url": "https://hooks.slack.com/commands/test",
                    }
                ).encode()
                signature = hmac.new(
                    service.config.signing_secret.encode(),
                    b"v0:" + timestamp.encode() + b":" + body,
                    hashlib.sha256,
                ).hexdigest()
                start = time.monotonic()
                response = client.post(
                    url,
                    content=body,
                    headers={
                        "content-type": "application/x-www-form-urlencoded",
                        "x-slack-request-timestamp": timestamp,
                        "x-slack-signature": "v0=" + signature,
                    },
                )
                return response.status_code, time.monotonic() - start

            with ThreadPoolExecutor(max_workers=10) as pool:
                results = list(pool.map(send, range(10)))
            blocker.execute("ROLLBACK")
        assert all(status == 503 for status, _ in results)
        assert max(elapsed for _, elapsed in results) < 2.5, results
        assert service.store.total("T1", "U2") == 0
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()
