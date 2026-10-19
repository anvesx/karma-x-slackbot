"""One local worker owns Slack mutations. Durable ledger is the source of truth."""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from karma_x.config import Config
from karma_x.core import (
    HELP_TEXT,
    Command,
    GroupMember,
    UserError,
    canceled_post_text,
    date_window,
    escaped,
    group_post_text,
    parse_command,
    post_text,
    utc_us,
)
from karma_x.recognizer import Block, Message, NoModel, Scorer, recognize
from karma_x.slack import Slack, SlackFailure
from karma_x.store import MessageAward, Store

log = logging.getLogger(__name__)

DELIVERY_SUBJECTS = {
    "post": "the channel post",
    "update": "the canceled-post update",
    "profile": "the display-name update",
}


def delivery_problem(kind: str, state: str, error: str | None) -> str:
    subject = DELIVERY_SUBJECTS.get(kind, kind)
    if state == "uncertain":
        return f"{subject} may not have gone through; an operator will check it"
    if state in {"blocked", "failed"}:
        return f"{subject} is {state} ({error or 'unknown error'})"
    return f"{subject} is still on its way"


class Service:
    def __init__(
        self,
        config: Config,
        store: Store,
        slack: Slack,
        clock: Callable[[], datetime],
        scorer: Scorer | None = None,
    ) -> None:
        self.config, self.store, self.slack, self.clock = config, store, slack, clock
        self.access_cache: dict[tuple[str, str], bool] = {}
        self.scorer: Scorer = scorer or NoModel()
        # AUTO_AWARD=rules lets a rules-AUTO verdict stand while no model is deployed.
        self.rules_auto = config.auto_award == "rules"

    def access(self, user: str, channel: str) -> bool:
        key = (user, channel)
        if key not in self.access_cache:
            try:
                self.access_cache[key] = self.slack.can_access(user, channel)
            except SlackFailure as error:
                if error.code not in {"channel_not_found", "no_permission", "access_denied"}:
                    raise
                self.access_cache[key] = False
        return self.access_cache[key]

    def member(self, uid: str) -> dict[str, Any]:
        member = self.slack.user(uid)
        if member.get("team_id") != self.config.team_id:
            raise UserError("Recognize a person in this workspace.")
        return member

    def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        self.access_cache.clear()
        p = task["payload"]
        kind = p.get("kind", "command")
        if kind in {"message", "message_changed"}:
            return self.on_message(task, edited=kind == "message_changed")
        if kind == "message_deleted":
            return self.on_message_deleted(p)
        if kind == "click":
            return self.on_click(task)
        team, user, channel = p["team_id"], p["user_id"], p["channel_id"]
        if team != self.config.team_id or not self.config.allows_channel(channel):
            raise UserError("This workspace or channel is not enabled for karma-x.")
        self.member(user)
        if not self.access(user, channel):
            raise UserError("You must be a member of this channel.")
        c = parse_command(p["text"])
        now = self.clock()
        created = task["created_at_us"]
        if c.action == "give":
            if user == c.target:
                raise UserError("You cannot give yourself karma.")
            self.member(c.target)
            if not self.access(c.target, channel):
                raise UserError("The recipient must be a member of this recognition channel.")
            r = self.store.give(task["id"], team, user, c.target, channel, c.reason, created)
            self.effects()
            # Only the giver sees this reply, so it is the one place the ID appears.
            saved = (
                f"Saved! <@{c.target}> now has 🌟{self.store.total(team, c.target)}. "
                f"To undo: /karma-x recognition cancel {r['id']}"
            )
            return {"text": " ".join(filter(None, [saved, self.delivery_note(r["id"])]))}
        if c.action == "cancel":
            r = self.store.get(team, c.target)
            if not self.access(user, r["channel"]):
                raise UserError("You cannot access this recognition channel.")
            r = self.store.cancel(team, c.target, user, utc_us(now))
            self.effects()
            canceled = (
                f"Canceled. <@{r['recipient']}> now has 🌟{self.store.total(team, r['recipient'])}."
            )
            return {"text": " ".join(filter(None, [canceled, self.delivery_note(r["id"])]))}
        if c.action == "help":
            return {"text": HELP_TEXT}
        if c.action == "list":
            return self.leaderboard(user)
        if c.action == "winner":
            # A queued command belongs to the calendar period when Slack sent it.
            return self.winner(c, user, datetime.fromtimestamp(created / 1_000_000, UTC))
        if c.action == "view":
            r = self.store.get(team, c.target)
            if not self.access(user, r["channel"]):
                raise UserError("You cannot access this recognition channel.")
            return {"text": self.recognition_text(r, now)}
        rows = self.store.history(team, user)
        visible = [r for r in rows if self.access(user, r["channel"])]
        return {
            "text": "\n\n".join(self.recognition_text(r, now, brief=True) for r in visible)
            or "No recognitions yet."
        }

    def recipient_problem(self, uid: str, channel: str) -> str:
        """The /karma-x recipient checks, as a reason to skip instead of an error."""
        try:
            self.member(uid)
        except UserError as e:
            return str(e)
        if not self.access(uid, channel):
            return "They are not a member of this channel."
        return ""

    def day(self, created_us: int) -> tuple[int, int]:
        start, end = date_window(
            "day", self.config.timezone, datetime.fromtimestamp(created_us / 1_000_000, UTC)
        )
        return utc_us(start), utc_us(end)

    def award_lines(self, awards: list[MessageAward]) -> list[str]:
        lines = []
        for a in awards:
            if a.outcome == "awarded":
                total = self.store.total(self.config.team_id, a.recipient)
                lines.append(
                    f"Saved! <@{a.recipient}> now has 🌟{total}. "
                    f"To undo: /karma-x recognition cancel {a.recognition_id}"
                )
            elif a.outcome == "limited":
                lines.append(
                    f"You've already given <@{a.recipient}> automatic karma today, "
                    "so this one wasn't counted."
                )
        return lines

    def on_message(self, task: dict[str, Any], edited: bool) -> dict[str, Any]:
        p = task["payload"]
        team, author, channel, ts = p["team_id"], p["user_id"], p["channel_id"], p["ts"]
        thread_ts = p.get("thread_ts")
        if team != self.config.team_id or not self.config.allows_channel(channel):
            return {}
        try:
            self.member(author)
        except UserError:
            return {}
        blocks = tuple(Block(b["kind"], b["text"]) for b in p.get("blocks") or ())
        decisions = recognize(
            Message(author, p["text"], blocks), self.scorer, rules_auto=self.rules_auto
        )
        lines: list[str] = []
        eligible: list[tuple[str, str]] = []
        auto = {d.user_id for d in decisions if d.outcome == "auto"}
        for d in decisions:
            if d.outcome != "auto":
                continue
            problem = self.recipient_problem(d.user_id, channel)
            if problem:
                lines.append(f"<@{d.user_id}> wasn't counted: {problem}")
            else:
                eligible.append((d.user_id, d.reason or "Recognized in a message"))
        now = utc_us(self.clock())
        if edited:
            for r in self.store.message_recognitions(team, channel, ts):
                if r["recipient"] not in auto:
                    self.store.cancel(team, r["id"], r["sender"], now)
                    lines.append(
                        f"Canceled <@{r['recipient']}>'s karma ({r['id']}): "
                        "the edited message no longer recognizes them."
                    )
        created = task["created_at_us"]
        awards = self.store.give_from_message(
            task["id"], team, author, channel, ts, thread_ts, eligible, created, self.day(created)
        )
        self.effects()
        lines = self.award_lines(awards) + lines
        prompts: list[dict[str, Any]] = []
        for d in decisions:
            if d.outcome != "confirm":
                continue
            cid, new = self.store.ask_later(
                team, channel, ts, thread_ts, author, d.user_id, d.reason, created
            )
            if new:
                prompts += confirmation_blocks(cid, d.user_id, d.reason)
        if not lines and not prompts:
            return {}
        text = "\n".join(lines) or "Karma-X: confirm a recognition."
        if not prompts:
            return {"text": text}
        summary = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}] if lines else []
        return {"text": text, "blocks": summary + prompts}

    def on_message_deleted(self, p: dict[str, Any]) -> dict[str, Any]:
        team, channel, ts = p["team_id"], p["channel_id"], p["ts"]
        if team != self.config.team_id:
            return {}
        now = utc_us(self.clock())
        for r in self.store.message_recognitions(team, channel, ts):
            self.store.cancel(team, r["id"], r["sender"], now)
        self.store.expire_confirmations(team, channel, ts)
        self.effects()
        return {}

    def on_click(self, task: dict[str, Any]) -> dict[str, Any]:
        p = task["payload"]
        team, user = p["team_id"], p["user_id"]

        def reply(text: str, replace: bool = True) -> dict[str, Any]:
            return {"replace_original": replace, "text": text}

        c = (
            self.store.confirmation(team, p["confirmation_id"])
            if team == self.config.team_id
            else None
        )
        if c is None:
            return reply("This karma request is no longer available.", False)
        if user != c["sender"]:
            return reply("Only the author can award this.", False)
        if c["state"] != "pending":
            return reply(f"This karma request was already {c['state']}.")
        created = task["created_at_us"]
        if created >= c["expires_at_us"]:
            self.store.settle_confirmation(c["id"], "expired")
            return reply("This karma request expired after 24 hours.")
        if not p.get("award"):
            self.store.settle_confirmation(c["id"], "skipped")
            return reply(f"Skipped. No karma given to <@{c['recipient']}>.")
        problem = self.recipient_problem(c["recipient"], c["channel"])
        if problem:
            return reply(f"<@{c['recipient']}> wasn't counted: {problem}")
        awards = self.store.give_from_message(
            task["id"],
            team,
            c["sender"],
            c["channel"],
            c["source_ts"],
            c["thread_ts"],
            [(c["recipient"], c["reason"] or "Recognized in a message")],
            created,
            self.day(created),
        )
        if awards and awards[0].outcome != "limited":
            self.store.settle_confirmation(c["id"], "awarded")
        self.effects()
        return reply("\n".join(self.award_lines(awards)) or "Already awarded.")

    def recognition_text(self, r: dict[str, Any], now: datetime, brief: bool = False) -> str:
        created = datetime.fromtimestamp(r["created_at_us"] / 1_000_000, UTC)
        stamp = created.astimezone(ZoneInfo(self.config.timezone)).strftime("%d %b %Y, %H:%M %Z")
        state = "Canceled" if r["canceled_at_us"] is not None else "Active"
        link = f" <{r['permalink']}|Recognition thread>" if r["permalink"] else " (thread pending)"
        reason = r["reason"]
        if brief and len(reason) > 150:
            reason = reason[:150] + f"… (recognition view {r['id']} shows the full reason)"
        return f"{r['id']} · {state} · {stamp}\n<@{r['sender']}> → <@{r['recipient']}>: {escaped(reason)}{link}"

    def delivery_note(self, rid: str) -> str:
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT kind,state,error FROM effects WHERE recognition_id=? AND state!='done'",
                (rid,),
            ).fetchall()
        return " ".join(
            f"Note: {delivery_problem(r['kind'], r['state'], r['error'])}." for r in rows
        )

    def leaderboard(self, user: str) -> dict[str, Any]:
        rows = self.store.top(self.config.team_id)
        if not rows:
            return {"text": "No recognition yet."}
        visible_channels = [
            c for c in self.store.channels(self.config.team_id) if self.access(user, c)
        ]
        table: list[list[dict[str, Any]]] = [
            [
                {"type": "raw_text", "text": s}
                for s in ["Rank", "Person", "Karma", "Latest recognition thread"]
            ]
        ]
        for row in rows:
            link = self.store.latest_link(
                self.config.team_id, row["slack_user_id"], visible_channels
            )
            person = {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "user", "user_id": row["slack_user_id"]}],
                    }
                ],
            }
            thread: dict[str, Any] = {"type": "raw_text", "text": "No accessible thread"}
            if link:
                thread = {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [{"type": "link", "url": link, "text": "View recognition"}],
                        }
                    ],
                }
            table.append(
                [
                    {"type": "raw_number", "value": row["rank"], "text": str(row["rank"])},
                    person,
                    {
                        "type": "raw_number",
                        "value": row["active_karma"],
                        "text": str(row["active_karma"]),
                    },
                    thread,
                ]
            )
        return {
            "text": "Karma-X · Top 30 · lifetime uncanceled recognition",
            "blocks": [{"type": "table", "rows": table}],
        }

    def winner(self, c: Command, user: str, now: datetime) -> dict[str, Any]:
        tz = str(self.member(user).get("tz") or self.config.timezone)
        try:
            ZoneInfo(tz)
        except (KeyError, ValueError):
            tz = self.config.timezone
        start, end = date_window(c.period, tz, now, c.start, c.end)
        rows = self.store.winners(self.config.team_id, utc_us(start), utc_us(end))
        header = f"Karma-X winners · {start.astimezone(ZoneInfo(tz)).date()} to {(end.astimezone(ZoneInfo(tz))).date()} (end exclusive) · {tz}"
        lines = [f"<@{r['recipient']}> · 🌟{r['karma']}" for r in rows]
        return {"text": header + "\n" + ("\n".join(lines) or "No recognition in this period.")}

    def effects(self) -> None:
        for effect in self.store.due_effects(utc_us(self.clock())):
            eid = effect["id"]
            with self.store.connection() as db:
                row = db.execute(
                    "SELECT * FROM recognitions WHERE id=?", (effect["recognition_id"],)
                ).fetchone()
            if not row:
                continue
            r = dict(row)
            kind = effect["kind"]
            now = utc_us(self.clock())
            try:
                if kind in {"post", "update"} and r["group_id"]:
                    self.group_effect(kind, r)
                elif kind == "post":
                    if r["canceled_at_us"] is not None:
                        self.store.effect_state(eid, "done", now)
                        continue
                    if not r["message_ts"]:
                        self.store.effect_state(eid, "sending", now)
                        text = post_text(r["sender"], r["recipient"], r["reason"])
                        ts = self.slack.post(r["channel"], text, r["id"])
                        self.store.record_message(r["id"], ts)
                    else:
                        ts = r["message_ts"]
                    self.store.record_link(r["id"], self.slack.permalink(r["channel"], ts))
                elif kind == "update":
                    if r["message_ts"]:
                        self.slack.update(
                            r["channel"],
                            r["message_ts"],
                            canceled_post_text(r["sender"], r["recipient"], r["reason"]),
                        )
                elif kind == "profile":
                    uid = r["recipient"]
                    current = self.member(uid)["profile"]
                    name = str(current.get("display_name") or current.get("real_name") or uid)
                    saved = self.store.profile_names(r["team"], uid)
                    base = (
                        saved["base_name"]
                        if name in {saved["last_written_name"], saved["intended_name"]}
                        and saved["base_name"]
                        else name
                    )
                    count = self.store.total(r["team"], uid)
                    suffix = f" 🌟{count}" if count else ""
                    written = base[: 80 - len(suffix)] + suffix
                    # Persist intent BEFORE external I/O: timeout/crash may follow a successful write.
                    self.store.prepare_profile(r["team"], uid, base, written)
                    if name != written:
                        self.slack.name(uid, written)
                        observed = self.member(uid)["profile"].get("display_name")
                        if observed != written:
                            raise SlackFailure("profile_sync_not_observed", 5)
                    self.store.record_profile(r["team"], uid, base, written)
                self.store.effect_state(eid, "done", now)
            except SlackFailure as e:
                if kind == "post" and e.ambiguous:
                    state = "uncertain"
                    due = now
                elif e.retry_after or e.code in {
                    "internal_error",
                    "fatal_error",
                    "service_unavailable",
                    "transport_error",
                }:
                    state = "failed" if effect["attempts"] >= 8 else "pending"
                    due = now + max(e.retry_after, min(300, 2 ** effect["attempts"])) * 1_000_000
                else:
                    state = "blocked"
                    due = now
                self.store.effect_state(eid, state, due, e.code)
                log.warning("Slack effect %s %s: %s", kind, state, e.code)
            except UserError as e:
                self.store.effect_state(eid, "blocked", now, str(e))

    def group_effect(self, kind: str, r: dict[str, Any]) -> None:
        """Post or re-render the one combined post for a message's recognitions."""
        group = self.store.group_members(r["group_id"])
        members = [
            GroupMember(m["recipient"], m["reason"], m["canceled_at_us"] is not None) for m in group
        ]
        text = group_post_text(r["sender"], members)
        if kind == "update":
            if r["message_ts"]:
                self.slack.update(r["channel"], r["message_ts"], text)
            return
        if all(m.canceled for m in members):
            return
        ts = r["message_ts"]
        if not ts:
            self.store.effect_state(r["id"] + "post", "sending", utc_us(self.clock()))
            ts = self.slack.post(r["channel"], text, r["id"], thread_ts=r["thread_ts"])
            self.store.record_group_message(r["group_id"], ts)
        self.store.record_group_link(r["group_id"], self.slack.permalink(r["channel"], ts))

    def tick(self) -> bool:
        now = utc_us(self.clock())
        task = self.store.claim(now)
        if task is None:
            self.effects()
            return False
        try:
            result = json.loads(task["result"]) if task["result"] else self.execute(task)
        except UserError as e:
            result = {"text": str(e)}
        except SlackFailure as e:
            terminal = task["attempts"] >= 8 or (
                not e.retry_after
                and e.code
                not in {"transport_error", "service_unavailable", "internal_error", "fatal_error"}
            )
            if not terminal:
                self.store.request_retry(
                    task["id"], e.code, now + max(5, e.retry_after) * 1_000_000, False
                )
                return True
            result = {
                "text": f"Slack rejected this command: {e.code}. Check app access, then rerun."
            }
            log.warning("Slack command blocked: %s", e.code)
        # Cache before response delivery: known-rejected429 retries never re-execute a command.
        serialized = json.dumps(result)
        self.store.request_result(task["id"], serialized)
        p = task["payload"]
        if not result:
            # Message events with nothing to tell the author finish silently.
            self.store.request_finish(task["id"], serialized)
            return True
        try:
            self.store.request_response_start(task["id"])
            if p.get("response_url"):
                self.slack.respond(
                    p["response_url"], result, channel=p["channel_id"], user=p["user_id"]
                )
            else:
                self.slack.ephemeral(
                    p["channel_id"],
                    p["user_id"],
                    str(result.get("text", "Karma-X")),
                    list(result.get("blocks") or []),
                    p.get("thread_ts"),
                )
            self.store.request_finish(task["id"], serialized)
        except (SlackFailure, UserError) as e:
            retry = (
                isinstance(e, SlackFailure)
                and e.code == "response_rate_limited"
                and task["attempts"] < 8
            )
            delay = max(1, e.retry_after) if isinstance(e, SlackFailure) else 0
            # Lost replies can have been delivered; only explicit rejection is safe to repeat.
            self.store.request_retry(task["id"], str(e), now + delay * 1_000_000, not retry)
            log.warning(
                "Command reply %s for %s: %s", "pending" if retry else "failed", task["id"], str(e)
            )
        return True


def confirmation_blocks(cid: str, recipient: str, reason: str) -> list[dict[str, Any]]:
    """Award/Skip prompt; the button value is the confirmation id."""
    question = (
        f"Give <@{recipient}> karma for “{escaped(reason)}”?"
        if reason
        else f"Give <@{recipient}> karma?"
    )

    def button(action: str, label: str) -> dict[str, Any]:
        return {
            "type": "button",
            "action_id": action,
            "text": {"type": "plain_text", "text": label},
            "value": cid,
        }

    award = {**button("karma_award", "Award"), "style": "primary"}
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": question}},
        {"type": "actions", "elements": [award, button("karma_skip", "Skip")]},
    ]
