# karma-x

A small Slack recognition bot. One `++` gives one karma. SQLite stores the ledger;
Slack is the entire user interface.

| Command | Behavior |
| --- | --- |
| `/karma-x @person ++ reason` | Gives one karma, posts a recognition in the channel, and updates the person's display name to `Person 🌟2`. Self-recognition is rejected. |
| `/karma-x list-all` | A Slack table of the top 30 people by uncanceled lifetime karma, with shared ranks and the latest accessible recognition thread. |
| `/karma-x winner-of-the-day` | Winners of today's local calendar date. |
| `/karma-x winner-of-the-month` | Winners of the current local calendar month. |
| `/karma-x winner-of-the-year` | Winners of the current local calendar year. |
| `/karma-x winner-between 2026-10-01 2026-10-10` | Winners between both inclusive local dates. All tied winners are returned. |
| `/karma-x recognition view [K…]` | Your last 10 given/received recognitions, or one recognition by ID. |
| `/karma-x recognition cancel K…` | Only the giver can cancel. Count, winners, original message, and name update; repeating cancel has no second decrement. |

The Slack client converts a selected `@person` into a user ID. Select a mention from autocomplete.
Dates use the command caller's Slack IANA timezone, falling back to `DEFAULT_TIMEZONE`.
SQLite stores integer UTC microseconds. A queued winner command uses its acceptance date.
Canceled recognition is excluded from all rankings, including past periods.

## Local run

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

1. Install locked dependencies: `uv sync --frozen`.
2. Follow [Slack installation](docs/slack-setup.md) and fill `.env` from `.env.example`.
3. Start the server: `uv run --env-file .env python -m karma_x.app`.
4. Point the Slack slash-command URL at a public HTTPS tunnel ending in `/slack/commands`.
5. Invite `@karma-x` into the allowlisted test channel and run `/karma-x @person ++ helped test`.

For a temporary local tunnel: `cloudflared tunnel --url http://localhost:8790`.
Changing the tunnel URL requires updating the Slack command's request URL.
The unsigned health route is `http://127.0.0.1:8790/health`; all commands require a valid Slack signature.

**The actual name suffix has Slack prerequisites:** a user token with `users.profile:write`,
a paid workspace, and an installing Owner/Admin whose role can update the recipient.
A bot token alone cannot edit other people's names. Slack permission failures are shown explicitly;
the durable recognition remains saved. See [Slack's profile API](https://docs.slack.dev/reference/methods/users.profile.set/).

## Checks

```sh
uv sync --frozen
make check
docker build -t karma-x:local .
uv run --frozen python scripts/smoke_container.py karma-x:local
```

`make check` runs lint, formatting, strict source type checks, and the test suite.
Tests use temporary databases and fake Slack transports; they never touch a real workspace.
The Docker smoke check uses its own temporary volume and verifies restart plus backup integrity.
[Validation](docs/validation.md) distinguishes local test evidence from real Slack and cloud deployment.

## Deployment

[GCE deployment](docs/deploy-gce.md): one VM, one attached Persistent Disk, one container process,
HTTPS reverse proxy, and SQLite's durable queue. GitHub Actions runs checks before deploying an
immutable image digest using Workload Identity Federation. Deployment stays disabled until
Helly configures the cloud variables and the production environment.

No dashboard, analytics, setup command, point budget, reward economy, or additional icons.
Read [CONTRACT.md](CONTRACT.md) for the behavior contract and [operations](docs/operations.md)
for delivery failures and backup/restore.

MIT licensed. See [LICENSE](LICENSE).
