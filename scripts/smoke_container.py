"""Prove nonroot/read-only runtime, volume persistence and SQLite online backup.

Creates only uniquely named test containers/volumes; never uses a real Slack token.
"""

import hashlib
import hmac
import shutil
import subprocess
import sys
import time
from urllib.parse import urlencode
from uuid import uuid4

import httpx


def main() -> None:
    docker = shutil.which("docker")
    if not docker:
        raise SystemExit("Docker is required.")
    image = sys.argv[1] if len(sys.argv) > 1 else "karma-x:local"
    name = "karma-x-smoke-" + uuid4().hex[:10]
    volume = name + "-data"
    client = httpx.Client(timeout=2, trust_env=False)
    container_tmpfs = "/tmp:rw,noexec,nosuid,size=16m"  # noqa: S108 -- private container tmpfs, not a host file

    def command(*args: str, script: str | None = None, check: bool = True) -> str:
        result = subprocess.run(  # noqa: S603 -- fixed executable, argument array, no shell
            [docker, *args], input=script, capture_output=True, text=True, check=check
        )
        return (
            (result.stdout + result.stderr).strip() if args[0] == "logs" else result.stdout.strip()
        )

    def assert_healthy(url: str) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                response = client.get(url + "/health")
                if response.json() == {"status": "ok", "bot": "karma-x"}:
                    return
            except (httpx.HTTPError, ValueError):
                time.sleep(0.1)
        raise RuntimeError("Container did not become healthy within 15 seconds.")

    try:
        command("volume", "create", volume)
        command(
            "run",
            "--rm",
            "--user",
            "0",
            "-v",
            volume + ":/data",
            image,
            "chown",
            "10001:10001",
            "/data",
        )
        # Seed before starting the server. No pending Slack I/O, no network credentials.
        command(
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "-v",
            volume + ":/data",
            image,
            "python",
            "-",
            script="""
from pathlib import Path
from karma_x.store import Store
s = Store(Path('/data/karma.db'))
for i in range(2):
    r = s.give(str(i), 'TTEST', 'U1', 'U2', 'CTEST', 'synthetic container test', i)
    for kind in ('post', 'profile'):
        s.effect_state(r['id'] + kind, 'done', 0)
assert s.total('TTEST', 'U2') == 2
""",
        )
        command(
            "run",
            "-d",
            "--name",
            name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            container_tmpfs,
            "--add-host",
            "slack.com:127.0.0.1",
            "--add-host",
            "hooks.slack.com:127.0.0.1",
            "-v",
            volume + ":/data",
            "-p",
            "127.0.0.1::8790",
            "-e",
            "SLACK_SIGNING_SECRET=synthetic-secret",
            "-e",
            "SLACK_BOT_TOKEN=synthetic-token",
            "-e",
            "SLACK_TEAM_ID=TTEST",
            "-e",
            "ALLOWED_CHANNEL_IDS=CTEST",
            image,
        )
        address = command("port", name, "8790/tcp")
        url = "http://" + address
        assert_healthy(url)
        body = urlencode(
            {
                "command": "/karma-x",
                "text": "<@U1> ++ self",
                "user_id": "U1",
                "team_id": "TTEST",
                "channel_id": "CTEST",
                "response_url": "https://hooks.slack.com/commands/test",
            }
        ).encode()
        timestamp = str(int(time.time()))
        digest = hmac.new(
            b"synthetic-secret", b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
        ).hexdigest()
        response = client.post(
            url + "/slack/commands",
            content=body,
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "x-slack-request-timestamp": timestamp,
                "x-slack-signature": "v0=" + digest,
            },
        )
        if "cannot give yourself" not in response.json()["text"]:
            raise RuntimeError("Signed self-recognition was not rejected.")
        if client.post(url + "/slack/commands", content=b"text=list-all").status_code != 401:
            raise RuntimeError("Unsigned command was accepted.")
        command(
            "exec",
            name,
            "python",
            "-m",
            "karma_x.admin",
            "backup",
            "--db",
            "/data/karma.db",
            "--output",
            "/data/backups/smoke.db",
        )
        command("restart", "--time", "10", name)
        restarted_address = command("port", name, "8790/tcp")
        if restarted_address != address:
            print(f"Docker reassigned the test-only dynamic port: {address} → {restarted_address}")
        url = "http://" + restarted_address
        assert_healthy(url)
        command(
            "exec",
            "-i",
            name,
            "python",
            "-",
            script="""
import os
from pathlib import Path
from karma_x.store import Store
assert os.getuid() == 10001
assert Store(Path('/data/karma.db')).total('TTEST', 'U2') == 2
assert Store(Path('/data/backups/smoke.db')).total('TTEST', 'U2') == 2
print('uid10001; restarted total2; backup total2')
""",
        )
        print(
            "PASS: signed self-block, unsigned401, UID10001/read-only, persistent total2 after restart, restored backup total2"
        )
    except Exception:
        print(command("logs", name, check=False), file=sys.stderr)
        raise
    finally:
        client.close()
        command("rm", "-f", name, check=False)
        command("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
