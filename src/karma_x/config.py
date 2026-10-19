"""Explicit deployment configuration; secrets never enter source or logs."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    signing_secret: str
    bot_token: str
    profile_token: str
    team_id: str
    channel_ids: frozenset[str]
    database: Path
    timezone: str = "Asia/Kolkata"
    # off: every recognised message asks the author first; rules: rules-AUTO awards directly.
    auto_award: Literal["off", "rules"] = "off"

    def allows_channel(self, channel: str) -> bool:
        # "*" opens every channel in the workspace; per-user membership checks still apply.
        return "*" in self.channel_ids or channel in self.channel_ids

    @classmethod
    def from_env(cls) -> "Config":
        required = [
            "SLACK_SIGNING_SECRET",
            "SLACK_BOT_TOKEN",
            "SLACK_TEAM_ID",
            "ALLOWED_CHANNEL_IDS",
        ]
        if any(not os.environ.get(k) for k in required):
            raise ValueError(
                "Configure SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN, SLACK_TEAM_ID, and ALLOWED_CHANNEL_IDS."
            )
        zone = os.environ.get("DEFAULT_TIMEZONE", "Asia/Kolkata")
        ZoneInfo(zone)
        auto_award = os.environ.get("AUTO_AWARD", "off").strip().lower() or "off"
        if auto_award not in {"off", "rules"}:
            raise ValueError("AUTO_AWARD must be off or rules.")
        return cls(
            os.environ["SLACK_SIGNING_SECRET"],
            os.environ["SLACK_BOT_TOKEN"],
            os.environ.get("SLACK_PROFILE_TOKEN", ""),
            os.environ["SLACK_TEAM_ID"],
            frozenset(c.strip() for c in os.environ["ALLOWED_CHANNEL_IDS"].split(",") if c.strip()),
            Path(os.environ.get("DATABASE_PATH", "data/karma.db")),
            zone,
            "rules" if auto_award == "rules" else "off",
        )
