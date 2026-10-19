"""Slack SDK adapter with explicit side-effect outcomes and safe retries."""

from typing import Any
from urllib.parse import urlparse

import httpx
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from karma_x.core import UserError


class SlackFailure(RuntimeError):
    def __init__(self, code: str, retry_after: int = 0, ambiguous: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.ambiguous = ambiguous


class Slack:
    def __init__(self, bot_token: str, profile_token: str) -> None:
        # Disable SDK transport retries: a lost POST response is not safe to repeat.
        self.bot = WebClient(token=bot_token, timeout=8, retry_handlers=[])
        self.profile = WebClient(token=profile_token, timeout=8, retry_handlers=[])
        self.profile_enabled = bool(profile_token)

    def call(
        self, method: str, *, profile_client: bool = False, posting: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            client = self.profile if profile_client else self.bot
            response = getattr(client, method.replace(".", "_"))(**kwargs)
            if not isinstance(response.data, dict):
                raise SlackFailure("invalid_slack_response", ambiguous=posting)
            return dict(response.data)
        except SlackApiError as e:
            status = e.response.status_code
            code = str(e.response.get("error", "slack_error"))
            retry = int(e.response.headers.get("Retry-After", "1")) if status == 429 else 0
            # Slack documents partial success for these errors, even at HTTP 200.
            ambiguous = posting and (status >= 500 or code in {"internal_error", "fatal_error"})
            raise SlackFailure(code, retry, ambiguous) from e
        except (OSError, TimeoutError) as e:
            raise SlackFailure("transport_error", 5, posting) from e

    def user(self, uid: str) -> dict[str, Any]:
        user = dict(self.call("users.info", user=uid)["user"])
        if user.get("deleted") or user.get("is_bot") or user.get("is_app_user"):
            raise UserError("Recognize an active person, not a bot or deleted member.")
        return user

    def can_access(self, uid: str, channel: str) -> bool:
        cursor = ""
        while True:
            response = self.call("conversations.members", channel=channel, limit=200, cursor=cursor)
            if uid in response["members"]:
                return True
            cursor = response.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                return False

    def post(self, channel: str, text: str, _rid: str, thread_ts: str | None = None) -> str:
        extra: dict[str, Any] = {"thread_ts": thread_ts} if thread_ts else {}
        r = self.call(
            "chat.postMessage",
            posting=True,
            channel=channel,
            text=text,
            unfurl_links=False,
            unfurl_media=False,
            **extra,
        )
        return str(r["ts"])

    def ephemeral(
        self,
        channel: str,
        user: str,
        text: str,
        blocks: list[dict[str, Any]],
        thread_ts: str | None = None,
    ) -> None:
        """Private reply when there is no response_url (message events)."""
        extra: dict[str, Any] = {}
        if blocks:
            extra["blocks"] = blocks
        if thread_ts:
            extra["thread_ts"] = thread_ts
        try:
            self.call(
                "chat.postEphemeral", posting=True, channel=channel, user=user, text=text, **extra
            )
        except SlackFailure as error:
            if error.retry_after and not error.ambiguous:
                raise SlackFailure("response_rate_limited", error.retry_after) from error
            raise

    def permalink(self, channel: str, ts: str) -> str:
        return str(self.call("chat.getPermalink", channel=channel, message_ts=ts)["permalink"])

    def update(self, channel: str, ts: str, text: str) -> None:
        self.call("chat.update", channel=channel, ts=ts, text=text)

    def name(self, uid: str, name: str) -> None:
        if not self.profile_enabled:
            raise SlackFailure("profile_token_missing")
        self.call(
            "users.profile.set", profile_client=True, user=uid, profile={"display_name": name}
        )

    def respond(
        self, url: str, payload: dict[str, Any], *, channel: str = "", user: str = ""
    ) -> None:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"hooks.slack.com", "hooks.slack-gov.com"}
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or not parsed.path.startswith(("/commands/", "/actions/"))
        ):
            raise UserError("Invalid Slack response URL.")
        if payload.get("blocks"):
            if not channel or not user:
                raise UserError("Table reply requires the original caller and channel.")
            try:
                self.call(
                    "chat.postEphemeral",
                    posting=True,
                    channel=channel,
                    user=user,
                    text=payload.get("text", "Karma-X"),
                    blocks=payload["blocks"],
                )
            except SlackFailure as error:
                if error.retry_after and not error.ambiguous:
                    raise SlackFailure("response_rate_limited", error.retry_after) from error
                raise
            return
        try:
            r = httpx.post(
                url,
                json={"response_type": "ephemeral", **payload},
                timeout=8,
                follow_redirects=False,
            )
        except httpx.HTTPError as e:
            raise SlackFailure("response_delivery_uncertain", ambiguous=True) from e
        if r.status_code == 429:
            raise SlackFailure("response_rate_limited", int(r.headers.get("Retry-After", "1")))
        if r.status_code >= 500:
            raise SlackFailure("response_delivery_uncertain", ambiguous=True)
        if r.status_code != 200:
            raise SlackFailure("response_delivery_rejected")
