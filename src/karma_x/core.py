"""Pure command and calendar logic: clocks and I/O are caller supplied."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class UserError(ValueError):
    """A safe, actionable command rejection."""


@dataclass(frozen=True)
class Command:
    action: str
    target: str = ""
    reason: str = ""
    period: str = ""
    start: str = ""
    end: str = ""


def parse_command(text: str) -> Command:
    if not isinstance(text, str) or len(text) > 3000:
        raise UserError("Command exceeds 3000 characters.")
    text = text.strip()
    m = re.fullmatch(r"<@([UW][A-Z0-9]+)(?:\|[^>]+)?>\s+\+\+\s+(.+)", text, re.DOTALL)
    if m:
        reason = m[2].strip()
        if not reason or len(reason) > 2000:
            raise UserError("Provide a reason of 1–2000 characters.")
        return Command("give", m[1], reason)
    if text == "list-all":
        return Command("list")
    if text in {"winner-of-the-day", "winner-of-the-month", "winner-of-the-year"}:
        return Command("winner", period=text.removeprefix("winner-of-the-"))
    m = re.fullmatch(r"winner-between (\d{4}-\d{2}-\d{2}) (\d{4}-\d{2}-\d{2})", text)
    if m:
        try:
            start, end = date.fromisoformat(m[1]), date.fromisoformat(m[2])
            if start > end or end.year >= 9999:
                raise ValueError("range")
        except ValueError as e:
            raise UserError("Use valid YYYY-MM-DD dates with start <= end.") from e
        return Command("winner", period="between", start=m[1], end=m[2])
    if text == "recognition view":
        return Command("history")
    m = re.fullmatch(r"recognition (view|cancel) (K[A-Za-z0-9]{3,40})", text)
    if m:
        return Command(m[1], m[2])
    raise UserError(
        "Use /karma-x @person ++ reason, list-all, winner-of-the-day/month/year, "
        "winner-between YYYY-MM-DD YYYY-MM-DD, or recognition view/cancel <ID>."
    )


def date_window(
    period: str, timezone: str, now: datetime, start: str = "", end: str = ""
) -> tuple[datetime, datetime]:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as e:
        raise UserError("Unknown timezone; configure an IANA timezone.") from e
    local = now.astimezone(zone).date()
    if period == "day":
        first, after = local, local + timedelta(days=1)
    elif period == "month":
        first = local.replace(day=1)
        after = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    elif period == "year":
        first, after = local.replace(month=1, day=1), date(local.year + 1, 1, 1)
    elif period == "between":
        first, after = date.fromisoformat(start), date.fromisoformat(end) + timedelta(days=1)
    else:
        raise UserError("Unknown period.")
    return datetime.combine(first, time.min, zone).astimezone(UTC), datetime.combine(
        after, time.min, zone
    ).astimezone(UTC)


def utc_us(value: datetime) -> int:
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def escaped(value: str) -> str:
    """Escape untrusted Slack markup, including broadcast mentions."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


USER_MENTION = re.compile(r"&lt;@([UW][A-Z0-9]+)&gt;")


def message_reason(reason: str) -> str:
    """Escape a quoted Slack message but keep its user mentions (never broadcasts)."""
    return USER_MENTION.sub(r"<@\1>", escaped(reason))


def post_text(sender: str, recipient: str, reason: str, *, rendered: str = "") -> str:
    """Public channel post. Recognition IDs stay in private replies only."""
    return f"<@{sender}> recognized <@{recipient}> · 🌟 +1\n{rendered or escaped(reason)}"


def canceled_post_text(sender: str, recipient: str, reason: str) -> str:
    return f"Canceled · <@{sender}> → <@{recipient}>\n{escaped(reason)}"


@dataclass(frozen=True)
class GroupMember:
    recipient: str
    reason: str
    canceled: bool


def group_post_text(sender: str, members: Sequence[GroupMember]) -> str:
    """One public post for every person recognised by one message."""
    if len(members) == 1:
        only = members[0]
        if only.canceled:
            return f"Canceled · <@{sender}> → <@{only.recipient}>\n{message_reason(only.reason)}"
        return post_text(sender, only.recipient, only.reason, rendered=message_reason(only.reason))
    lines = [
        f"• ~<@{m.recipient}>~ canceled"
        if m.canceled
        else f"• <@{m.recipient}> · 🌟 +1 — {message_reason(m.reason)}"
        for m in members
    ]
    return f"<@{sender}> recognized:\n" + "\n".join(lines)
