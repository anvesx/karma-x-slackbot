"""Pure command and calendar logic: clocks and I/O are caller supplied."""

import re
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


def post_text(sender: str, recipient: str, reason: str) -> str:
    """Public channel post. Recognition IDs stay in private replies only."""
    return f"<@{sender}> recognized <@{recipient}> · 🌟 +1\n{escaped(reason)}"


def canceled_post_text(sender: str, recipient: str, reason: str) -> str:
    return f"Canceled · <@{sender}> → <@{recipient}>\n{escaped(reason)}"
