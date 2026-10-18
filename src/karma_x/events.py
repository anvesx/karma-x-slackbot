"""Pure parsing of Slack Events API and interactivity payloads. No I/O."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from karma_x.recognizer.clean import HAS_MENTION
from karma_x.recognizer.model import Block

POSTED_SUBTYPES = frozenset({None, "thread_broadcast", "file_share"})
CLICK_ACTIONS = {"karma_award": True, "karma_skip": False}


@dataclass(frozen=True)
class MessagePosted:
    key: str
    team_id: str
    channel_id: str
    user_id: str
    text: str
    ts: str
    thread_ts: str | None
    blocks: tuple[Block, ...]

    kind = "message"

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "team_id": self.team_id,
            "channel_id": self.channel_id,
            "user_id": self.user_id,
            "text": self.text,
            "ts": self.ts,
            "thread_ts": self.thread_ts,
            "blocks": [{"kind": b.kind, "text": b.text} for b in self.blocks],
        }


@dataclass(frozen=True)
class MessageEdited(MessagePosted):
    kind = "message_changed"


@dataclass(frozen=True)
class MessageDeleted:
    key: str
    team_id: str
    channel_id: str
    ts: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": "message_deleted",
            "team_id": self.team_id,
            "channel_id": self.channel_id,
            "ts": self.ts,
        }


@dataclass(frozen=True)
class ButtonClick:
    key: str
    team_id: str
    channel_id: str
    user_id: str
    confirmation_id: str
    award: bool
    response_url: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": "click",
            "team_id": self.team_id,
            "channel_id": self.channel_id,
            "user_id": self.user_id,
            "confirmation_id": self.confirmation_id,
            "award": self.award,
            "response_url": self.response_url,
        }


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _mentions(msg: Mapping[str, Any]) -> bool:
    return bool(HAS_MENTION.search(_str(msg.get("text")) or ""))


def _map(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _inline(elements: object) -> str:
    parts: list[str] = []
    for el in elements if isinstance(elements, Sequence) else ():
        el = _map(el)
        match el.get("type"):
            case "text":
                parts.append(_str(el.get("text")) or "")
            case "user" if _str(el.get("user_id")):
                parts.append(f"<@{el['user_id']}>")
            case "emoji" if _str(el.get("name")):
                parts.append(f":{el['name']}:")
            case "link":
                parts.append(_str(el.get("text")) or "")
    return "".join(parts)


def blocks_from_slack(blocks: object) -> tuple[Block, ...]:
    """Slack rich_text sections and list items as recognizer Blocks; quotes and code drop."""
    out: list[Block] = []
    for block in blocks if isinstance(blocks, Sequence) else ():
        block = _map(block)
        if block.get("type") != "rich_text":
            continue
        for element in block.get("elements") or ():
            element = _map(element)
            if element.get("type") == "rich_text_section":
                out.append(Block("section", _inline(element.get("elements"))))
            elif element.get("type") == "rich_text_list":
                for item in element.get("elements") or ():
                    out.append(Block("list_item", _inline(_map(item).get("elements"))))
    return tuple(out)


def _message(
    key: str,
    team_id: str,
    channel_id: str,
    msg: Mapping[str, Any],
    cls: type[MessagePosted],
    had_mention: bool = False,
) -> MessagePosted | None:
    user, ts, text = _str(msg.get("user")), _str(msg.get("ts")), msg.get("text")
    if msg.get("bot_id") or not (user and ts and isinstance(text, str)):
        return None
    if not (had_mention or HAS_MENTION.search(text)):
        return None
    thread_ts = _str(msg.get("thread_ts"))
    blocks = blocks_from_slack(msg.get("blocks"))
    return cls(key, team_id, channel_id, user, text, ts, thread_ts, blocks)


def parse_event(body: Mapping[str, Any]) -> MessagePosted | MessageEdited | MessageDeleted | None:
    """A typed message event from an event_callback envelope, or None to ignore it."""
    event = _map(body.get("event"))
    event_id, team_id = _str(body.get("event_id")), _str(body.get("team_id"))
    channel_id = _str(event.get("channel"))
    if body.get("type") != "event_callback" or event.get("type") != "message":
        return None
    if not (event_id and team_id and channel_id) or event.get("bot_id"):
        return None
    key = "event:" + event_id
    subtype = event.get("subtype")
    if subtype in POSTED_SUBTYPES:
        return _message(key, team_id, channel_id, event, MessagePosted)
    if subtype == "message_changed":
        new, old = _map(event.get("message")), _map(event.get("previous_message"))
        if new.get("text") == old.get("text"):
            return None
        # An edit that removes every mention still reaches the worker so it can re-score.
        return _message(key, team_id, channel_id, new, MessageEdited, _mentions(old))
    if subtype == "message_deleted":
        deleted_ts, old = _str(event.get("deleted_ts")), _map(event.get("previous_message"))
        if old and not _mentions(old):
            return None
        return MessageDeleted(key, team_id, channel_id, deleted_ts) if deleted_ts else None
    return None


def parse_click(payload: Mapping[str, Any]) -> ButtonClick | None:
    """A karma confirmation button press from a block_actions payload, or None."""
    actions = payload.get("actions")
    if payload.get("type") != "block_actions" or not isinstance(actions, Sequence) or not actions:
        return None
    action = _map(actions[0])
    team_id = _str(_map(payload.get("team")).get("id"))
    channel_id = _str(_map(payload.get("channel")).get("id"))
    user_id = _str(_map(payload.get("user")).get("id"))
    confirmation_id, action_ts = _str(action.get("value")), _str(action.get("action_ts"))
    response_url = _str(payload.get("response_url"))
    action_id = action.get("action_id")
    if action_id not in CLICK_ACTIONS or not (
        team_id and channel_id and user_id and confirmation_id and action_ts and response_url
    ):
        return None
    key = f"click:{action_ts}:{user_id}"
    award = CLICK_ACTIONS[action_id]
    return ButtonClick(key, team_id, channel_id, user_id, confirmation_id, award, response_url)
