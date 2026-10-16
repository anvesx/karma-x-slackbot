# Slack installation

1. Create a Slack app from `slack-manifest.yaml`, replacing the HTTPS host.
2. Install into one workspace; an authorized Owner/Admin must approve the profile-writing user scope.
3. Put the Signing Secret, bot OAuth token, and user OAuth token into a local `.env`, then `chmod 600 .env`.
4. Set the workspace ID and comma-separated channel IDs in `.env` (or `*` for every channel in the workspace); invite `@karma-x` to those channels.
5. Run an award in the test channel and verify the actual recipient name changes before enabling other channels.

Copy secrets from Slack's Basic Information and OAuth & Permissions pages.
Never paste secrets into GitHub, issues, Slack messages, screenshots, command logs, or this repo.
The app serves one slash-command endpoint. No Events API subscription or Socket Mode is needed.

| Scope | Purpose |
| --- | --- |
| `commands` | Receive `/karma-x`. |
| `chat:write` | Post/update the bot's recognition messages and fetch their permalinks. |
| `users:read` | Validate active humans, workspace identity, current names, and caller timezone. |
| `channels:read`, `groups:read` | Check membership before awards, history, and private thread links. |
| `channels:history`, `groups:history` | Operator reconciliation of a post whose response was lost. |
| User `users.profile:write` | Change the actual display name using an authorized user's token. |
| User `users.profile:read` | Inspect profile access during installation; the bot's normal read uses `users:read`. |

Only allowlisted channels are accepted; `ALLOWED_CHANNEL_IDS=*` accepts every channel in the
workspace, and the bot must still be invited to a channel to post there. Foreign Slack Connect users, bots, deleted accounts,
and self-recognition are rejected. The recipient must also belong to the recognition channel.
The leaderboard and winners are workspace totals. Reasons and private thread links remain
visible only to members of their original channel.

Slack limits editing other users' names to a paid workspace and an appropriate Owner/Admin token.
The installing user's role must be above the target user's role. Permissions are a workspace constraint;
a successful recognition must not be described as a successful name change if Slack rejected it.
The local live test confirmed this constraint: an ordinary member's token installed successfully
but profile writes returned `not_admin`. Have the authorized Owner/Admin provide the replacement
user token privately, restart, and repeat the two-award name/readback test before accepting this feature.
Reference: [users.profile.set](https://docs.slack.dev/reference/methods/users.profile.set/).

The app intentionally does not implement automatic token rotation. If your workspace requires
rotation, implement the refresh flow before enabling it in the app manifest; replace tokens securely
and restart the process when manually rotating equivalent credentials.
