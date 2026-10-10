"""One local worker owns Slack mutations. Durable ledger is the source of truth."""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from karma_x.config import Config
from karma_x.core import Command, UserError, date_window, escaped, parse_command, utc_us
from karma_x.slack import Slack, SlackFailure
from karma_x.store import Store

log = logging.getLogger(__name__)


class Service:
    def __init__(
        self, config: Config, store: Store, slack: Slack, clock: Callable[[], datetime]
    ) -> None:
        self.config, self.store, self.slack, self.clock = config, store, slack, clock
        self.access_cache: dict[tuple[str, str], bool] = {}

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
        team, user, channel = p["team_id"], p["user_id"], p["channel_id"]
        if team != self.config.team_id or channel not in self.config.channel_ids:
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
            return {
                "text": f"Recognition {r['id']} saved. <@{c.target}> has 🌟{self.store.total(team, c.target)}. {self.delivery_note(r['id'])}"
            }
        if c.action == "cancel":
            r = self.store.get(team, c.target)
            if not self.access(user, r["channel"]):
                raise UserError("You cannot access this recognition channel.")
            r = self.store.cancel(team, c.target, user, utc_us(now))
            self.effects()
            return {
                "text": f"Recognition {r['id']} canceled. <@{r['recipient']}> has 🌟{self.store.total(team, r['recipient'])}. {self.delivery_note(r['id'])}"
            }
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

    def recognition_text(self, r: dict[str, Any], now: datetime, brief: bool = False) -> str:
        stamp = datetime.fromtimestamp(r["created_at_us"] / 1_000_000, UTC).isoformat()
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
        return " ".join(f"{r['kind']}: {r['state']} ({r['error'] or 'waiting'})." for r in rows)

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
                if kind == "post":
                    if r["canceled_at_us"] is not None:
                        self.store.effect_state(eid, "done", now)
                        continue
                    if not r["message_ts"]:
                        self.store.effect_state(eid, "sending", now)
                        text = f"<@{r['sender']}> recognized <@{r['recipient']}> · 🌟 +1\n{escaped(r['reason'])}\nRecognition ID: {r['id']}"
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
                            f"Canceled recognition {r['id']} · <@{r['sender']}> → <@{r['recipient']}>\n{escaped(r['reason'])}",
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
        try:
            self.store.request_response_start(task["id"])
            self.slack.respond(
                task["payload"]["response_url"],
                result,
                channel=task["payload"]["channel_id"],
                user=task["payload"]["user_id"],
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
