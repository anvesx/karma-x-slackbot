"""SQLite ledger. Fresh connections per operation; short serialized transactions.

Network calls never run inside a transaction. The service takes an OS file lock
so only one worker process performs Slack side effects for this database.
"""

import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from karma_x.core import UserError

SCHEMA = """
CREATE TABLE IF NOT EXISTS recognitions (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, team TEXT NOT NULL,
 sender TEXT NOT NULL, recipient TEXT NOT NULL, channel TEXT NOT NULL,
 reason TEXT NOT NULL, created_at_us INTEGER NOT NULL,
 canceled_at_us INTEGER, canceled_by TEXT, message_ts TEXT, permalink TEXT,
 post_state TEXT NOT NULL DEFAULT 'pending', profile_state TEXT NOT NULL DEFAULT 'pending',
 CHECK(sender != recipient)
);
CREATE INDEX IF NOT EXISTS recognition_range ON recognitions(team,created_at_us,recipient) WHERE canceled_at_us IS NULL;
CREATE INDEX IF NOT EXISTS recipient_history ON recognitions(team,recipient,created_at_us DESC);
CREATE INDEX IF NOT EXISTS sender_history ON recognitions(team,sender,created_at_us DESC);
CREATE TABLE IF NOT EXISTS users (
 team TEXT NOT NULL, slack_user_id TEXT NOT NULL, active_karma INTEGER NOT NULL DEFAULT 0 CHECK(active_karma >= 0),
 base_name TEXT NOT NULL DEFAULT '', last_written_name TEXT NOT NULL DEFAULT '',
 intended_name TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(team,slack_user_id)
);
CREATE INDEX IF NOT EXISTS top_users ON users(team,active_karma DESC,slack_user_id);
CREATE TABLE IF NOT EXISTS requests (
 id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at_us INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 due_us INTEGER NOT NULL, lease_us INTEGER, result TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS due_requests ON requests(state,due_us);
CREATE TABLE IF NOT EXISTS effects (
 id TEXT PRIMARY KEY, recognition_id TEXT NOT NULL REFERENCES recognitions(id), kind TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 due_us INTEGER NOT NULL, error TEXT, UNIQUE(recognition_id,kind)
);
CREATE INDEX IF NOT EXISTS due_effects ON effects(state,due_us);
"""

# Message recognition (additive): columns arrive by ALTER first, then these run.
RECOGNITION_COLUMNS = {
    "origin": "TEXT NOT NULL DEFAULT 'command'",
    "source_ts": "TEXT",
    "thread_ts": "TEXT",
    "group_id": "TEXT",
}
MESSAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS confirmations (
 id TEXT PRIMARY KEY, team TEXT NOT NULL, channel TEXT NOT NULL, source_ts TEXT NOT NULL,
 thread_ts TEXT, sender TEXT NOT NULL, recipient TEXT NOT NULL, reason TEXT NOT NULL,
 created_at_us INTEGER NOT NULL, expires_at_us INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending'
);
CREATE UNIQUE INDEX IF NOT EXISTS confirmation_once ON confirmations(team,channel,source_ts,recipient);
CREATE UNIQUE INDEX IF NOT EXISTS message_recognition_once ON recognitions(team,channel,source_ts,recipient) WHERE origin='message' AND canceled_at_us IS NULL;
CREATE INDEX IF NOT EXISTS message_daily ON recognitions(team,sender,recipient,created_at_us) WHERE origin='message' AND canceled_at_us IS NULL;
CREATE INDEX IF NOT EXISTS recognition_group ON recognitions(group_id) WHERE group_id IS NOT NULL;
"""

CONFIRMATION_TTL_US = 24 * 3600 * 1_000_000


@dataclass(frozen=True)
class MessageAward:
    """What happened to one recipient of a message: new, already given, or over the daily limit."""

    recipient: str
    outcome: Literal["awarded", "existing", "limited"]
    recognition_id: str


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.executescript(SCHEMA)
            # Additive upgrade: preserve ledgers created by earlier local builds.
            columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
            if "intended_name" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN intended_name TEXT NOT NULL DEFAULT ''")
            existing = {row[1] for row in db.execute("PRAGMA table_info(recognitions)")}
            for name, decl in RECOGNITION_COLUMNS.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE recognitions ADD COLUMN {name} {decl}")
            db.executescript(MESSAGE_SCHEMA)
        os.chmod(path, 0o600)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=0.5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=500")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def give(
        self,
        request_id: str,
        team: str,
        sender: str,
        recipient: str,
        channel: str,
        reason: str,
        now_us: int,
    ) -> dict[str, Any]:
        if sender == recipient:
            raise UserError("You cannot give yourself karma.")
        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM recognitions WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing:
                return dict(existing)
            rid = "K" + uuid4().hex[:16]
            db.execute(
                "INSERT INTO recognitions(id,request_id,team,sender,recipient,channel,reason,created_at_us) VALUES(?,?,?,?,?,?,?,?)",
                (rid, request_id, team, sender, recipient, channel, reason, now_us),
            )
            db.execute(
                "INSERT INTO users(team,slack_user_id,active_karma) VALUES(?,?,1) ON CONFLICT(team,slack_user_id) DO UPDATE SET active_karma=active_karma+1",
                (team, recipient),
            )
            for kind in ("post", "profile"):
                db.execute(
                    "INSERT INTO effects(id,recognition_id,kind,due_us) VALUES(?,?,?,?)",
                    (rid + kind, rid, kind, now_us),
                )
            return dict(db.execute("SELECT * FROM recognitions WHERE id=?", (rid,)).fetchone())

    def give_from_message(
        self,
        task_id: str,
        team: str,
        sender: str,
        channel: str,
        source_ts: str,
        thread_ts: str | None,
        recipients: Sequence[tuple[str, str]],
        now_us: int,
        day: tuple[int, int],
    ) -> list[MessageAward]:
        """Award each (recipient, reason) once per message and once per giver/day, as one group."""
        awards: list[MessageAward] = []
        group = ""
        with self.transaction() as db:
            for recipient, reason in recipients:
                if recipient == sender:
                    continue
                row = db.execute(
                    "SELECT id FROM recognitions WHERE request_id=? OR (team=? AND channel=? "
                    "AND source_ts=? AND recipient=? AND origin='message' AND canceled_at_us IS NULL) "
                    "ORDER BY canceled_at_us IS NOT NULL LIMIT 1",
                    (f"{task_id}:{recipient}", team, channel, source_ts, recipient),
                ).fetchone()
                if row:
                    awards.append(MessageAward(recipient, "existing", str(row["id"])))
                    continue
                limited = db.execute(
                    "SELECT 1 FROM recognitions WHERE team=? AND sender=? AND recipient=? "
                    "AND origin='message' AND canceled_at_us IS NULL "
                    "AND created_at_us>=? AND created_at_us<? LIMIT 1",
                    (team, sender, recipient, day[0], day[1]),
                ).fetchone()
                if limited:
                    awards.append(MessageAward(recipient, "limited", ""))
                    continue
                rid = "K" + uuid4().hex[:16]
                group = group or rid
                db.execute(
                    "INSERT INTO recognitions(id,request_id,team,sender,recipient,channel,reason,"
                    "created_at_us,origin,source_ts,thread_ts,group_id) "
                    "VALUES(?,?,?,?,?,?,?,?,'message',?,?,?)",
                    (
                        rid,
                        f"{task_id}:{recipient}",
                        team,
                        sender,
                        recipient,
                        channel,
                        reason,
                        now_us,
                        source_ts,
                        thread_ts,
                        group,
                    ),
                )
                db.execute(
                    "INSERT INTO users(team,slack_user_id,active_karma) VALUES(?,?,1) ON CONFLICT(team,slack_user_id) DO UPDATE SET active_karma=active_karma+1",
                    (team, recipient),
                )
                db.execute(
                    "INSERT INTO effects(id,recognition_id,kind,due_us) VALUES(?,?,'profile',?)",
                    (rid + "profile", rid, now_us),
                )
                awards.append(MessageAward(recipient, "awarded", rid))
            if group:
                # One public post per group, owned by the group's first recognition.
                db.execute(
                    "INSERT INTO effects(id,recognition_id,kind,due_us) VALUES(?,?,'post',?)",
                    (group + "post", group, now_us),
                )
        return awards

    def group_members(self, group_id: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM recognitions WHERE group_id=? ORDER BY created_at_us,rowid",
                    (group_id,),
                )
            ]

    def record_group_message(self, group_id: str, timestamp: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE recognitions SET message_ts=?,post_state='posted' WHERE group_id=?",
                (timestamp, group_id),
            )

    def record_group_link(self, group_id: str, permalink: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE recognitions SET permalink=? WHERE group_id=?", (permalink, group_id)
            )

    def message_recognitions(self, team: str, channel: str, source_ts: str) -> list[dict[str, Any]]:
        """Active automatic recognitions that came from one Slack message."""
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM recognitions WHERE team=? AND channel=? AND source_ts=? "
                    "AND origin='message' AND canceled_at_us IS NULL ORDER BY created_at_us,rowid",
                    (team, channel, source_ts),
                )
            ]

    def ask_later(
        self,
        team: str,
        channel: str,
        source_ts: str,
        thread_ts: str | None,
        sender: str,
        recipient: str,
        reason: str,
        now_us: int,
    ) -> tuple[str, bool]:
        """Record a pending Award/Skip question; returns (id, newly created)."""
        cid = "C" + uuid4().hex[:16]
        with self.transaction() as db:
            created = db.execute(
                "INSERT INTO confirmations(id,team,channel,source_ts,thread_ts,sender,recipient,"
                "reason,created_at_us,expires_at_us) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(team,channel,source_ts,recipient) DO NOTHING",
                (
                    cid,
                    team,
                    channel,
                    source_ts,
                    thread_ts,
                    sender,
                    recipient,
                    reason,
                    now_us,
                    now_us + CONFIRMATION_TTL_US,
                ),
            ).rowcount
            if created == 1:
                return cid, True
            row = db.execute(
                "SELECT id FROM confirmations WHERE team=? AND channel=? AND source_ts=? AND recipient=?",
                (team, channel, source_ts, recipient),
            ).fetchone()
            return str(row["id"]), False

    def confirmation(self, team: str, cid: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM confirmations WHERE team=? AND id=?", (team, cid)
            ).fetchone()
        return dict(row) if row else None

    def settle_confirmation(
        self, cid: str, state: Literal["awarded", "skipped", "expired"]
    ) -> bool:
        """Move a pending confirmation to its final state; False when it was no longer pending."""
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE confirmations SET state=? WHERE id=? AND state='pending'", (state, cid)
            ).rowcount
        return changed == 1

    def expire_confirmations(self, team: str, channel: str, source_ts: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE confirmations SET state='expired' WHERE team=? AND channel=? "
                "AND source_ts=? AND state='pending'",
                (team, channel, source_ts),
            )

    def get(self, team: str, rid: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM recognitions WHERE team=? AND id=?", (team, rid)
            ).fetchone()
        if not row:
            raise UserError("Recognition not found.")
        return dict(row)

    def cancel(self, team: str, rid: str, sender: str, now_us: int) -> dict[str, Any]:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM recognitions WHERE team=? AND id=?", (team, rid)
            ).fetchone()
            if not row:
                raise UserError("Recognition not found.")
            if row["sender"] != sender:
                raise UserError("Only the person who gave this recognition can cancel it.")
            if row["canceled_at_us"] is None:
                db.execute(
                    "UPDATE recognitions SET canceled_at_us=?,canceled_by=? WHERE id=?",
                    (now_us, sender, rid),
                )
                db.execute(
                    "UPDATE users SET active_karma=active_karma-1 WHERE team=? AND slack_user_id=?",
                    (team, row["recipient"]),
                )
                for kind in ("update", "profile"):
                    db.execute(
                        "INSERT INTO effects(id,recognition_id,kind,due_us) VALUES(?,?,?,?) ON CONFLICT(recognition_id,kind) DO UPDATE SET state='pending',due_us=excluded.due_us,error=NULL,attempts=0",
                        (rid + kind, rid, kind, now_us),
                    )
                if row["group_id"] is None:
                    db.execute(
                        "UPDATE effects SET state='done' WHERE recognition_id=? AND kind='post' AND state='pending'",
                        (rid,),
                    )
            return dict(db.execute("SELECT * FROM recognitions WHERE id=?", (rid,)).fetchone())

    def total(self, team: str, user: str) -> int:
        with self.connection() as db:
            row = db.execute(
                "SELECT active_karma FROM users WHERE team=? AND slack_user_id=?", (team, user)
            ).fetchone()
        return int(row[0]) if row else 0

    def top(self, team: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT slack_user_id,active_karma FROM users WHERE team=? AND active_karma>0 ORDER BY active_karma DESC,slack_user_id LIMIT 30",
                (team,),
            ).fetchall()
        result = []
        prev = -1
        rank = 0
        for i, row in enumerate(rows, 1):
            if row["active_karma"] != prev:
                rank = i
            prev = row["active_karma"]
            result.append({**dict(row), "rank": rank})
        return result

    def winners(self, team: str, start: int, end: int) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT recipient,COUNT(*) AS karma FROM recognitions WHERE team=? AND canceled_at_us IS NULL AND created_at_us>=? AND created_at_us<? GROUP BY recipient ORDER BY karma DESC,recipient",
                (team, start, end),
            ).fetchall()
        return [dict(r) for r in rows if r["karma"] == rows[0]["karma"]] if rows else []

    def history(self, team: str, user: str, limit: int = 10) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM recognitions WHERE team=? AND (sender=? OR recipient=?) ORDER BY created_at_us DESC,id DESC LIMIT ?",
                    (team, user, user, limit),
                )
            ]

    def recipient_history(self, team: str, user: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM recognitions WHERE team=? AND recipient=? AND canceled_at_us IS NULL ORDER BY created_at_us DESC LIMIT 100",
                    (team, user),
                )
            ]

    def channels(self, team: str) -> list[str]:
        with self.connection() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT channel FROM recognitions WHERE team=?", (team,)
                )
            ]

    def latest_link(self, team: str, user: str, channels: list[str]) -> str:
        if not channels:
            return ""
        with self.connection() as db:
            row = db.execute(
                "SELECT permalink FROM recognitions WHERE team=? AND recipient=? "
                "AND canceled_at_us IS NULL AND permalink IS NOT NULL AND permalink!='' "
                "AND channel IN (SELECT value FROM json_each(?)) ORDER BY created_at_us DESC,id DESC LIMIT 1",
                (team, user, json.dumps(channels)),
            ).fetchone()
        return str(row[0]) if row else ""

    def enqueue(self, key: str, payload: dict[str, Any], now_us: int) -> bool:
        with self.transaction() as db:
            result = db.execute(
                "INSERT INTO requests(id,payload,created_at_us,due_us) VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (key, json.dumps(payload), now_us, now_us),
            )
            return result.rowcount == 1

    def claim(self, now_us: int) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM requests WHERE (state='pending' AND due_us<=?) OR (state='processing' AND lease_us<?) ORDER BY created_at_us LIMIT 1",
                (now_us, now_us - 60_000_000),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE requests SET state='processing',attempts=attempts+1,lease_us=? WHERE id=?",
                (now_us, row["id"]),
            )
            return {**dict(row), "payload": json.loads(row["payload"])}

    def request_finish(self, key: str, result: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE requests SET state='done',result=?,payload='{}',error=NULL WHERE id=?",
                (result, key),
            )

    def request_result(self, key: str, result: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE requests SET result=? WHERE id=?", (result, key))

    def request_response_start(self, key: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE requests SET state='responding' WHERE id=?", (key,))

    def request_retry(self, key: str, error: str, due_us: int, terminal: bool = False) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE requests SET state=?,error=?,due_us=?,payload=CASE WHEN ? THEN '{}' ELSE payload END WHERE id=?",
                ("failed" if terminal else "pending", error, due_us, terminal, key),
            )

    def due_effects(self, now_us: int) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM effects WHERE state='pending' AND due_us<=? ORDER BY due_us,id LIMIT 20",
                    (now_us,),
                )
            ]

    def effect_state(self, eid: str, state: str, now_us: int, error: str = "") -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE effects SET state=?,due_us=?,error=?,attempts=attempts+1 WHERE id=?",
                (state, now_us, error, eid),
            )

    def recover_sends(self) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE effects SET state='uncertain',error='Process interrupted during Slack posting; inspect the channel before replay.' WHERE state='sending'"
            )
            db.execute(
                "UPDATE requests SET state='failed',payload='{}',error='Process interrupted during reply delivery; rerun read commands.' WHERE state='responding'"
            )

    def record_message(self, rid: str, timestamp: str, permalink: str = "") -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE recognitions SET message_ts=?,permalink=?,post_state='posted' WHERE id=?",
                (timestamp, permalink, rid),
            )

    def record_link(self, rid: str, permalink: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE recognitions SET permalink=? WHERE id=?", (permalink, rid))

    def retry_safe_effect(self, rid: str, kind: str) -> None:
        if kind not in {"profile", "update"}:
            raise UserError("Only profile and canceled-message updates are safe to replay.")
        with self.transaction() as db:
            row = db.execute(
                "SELECT canceled_at_us FROM recognitions WHERE id=?", (rid,)
            ).fetchone()
            if not row or (kind == "update" and row[0] is None):
                raise UserError("Recognition is missing or has not been canceled.")
            db.execute(
                "UPDATE effects SET state='pending',attempts=0,due_us=0,error=NULL WHERE recognition_id=? AND kind=?",
                (rid, kind),
            )

    def attach_post(self, rid: str, timestamp: str, permalink: str) -> None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT canceled_at_us FROM recognitions WHERE id=?", (rid,)
            ).fetchone()
            effect = db.execute(
                "SELECT state FROM effects WHERE recognition_id=? AND kind='post'", (rid,)
            ).fetchone()
            if not row or not effect or effect[0] != "uncertain":
                raise UserError("Only an uncertain post can be reconciled.")
            db.execute(
                "UPDATE recognitions SET message_ts=?,permalink=?,post_state='posted' WHERE id=?",
                (timestamp, permalink, rid),
            )
            db.execute(
                "UPDATE effects SET state='done',error=NULL WHERE recognition_id=? AND kind='post'",
                (rid,),
            )
            if row[0] is not None:
                db.execute(
                    "UPDATE effects SET state='pending',attempts=0,due_us=0,error=NULL WHERE recognition_id=? AND kind='update'",
                    (rid,),
                )

    def profile_names(self, team: str, user: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM users WHERE team=? AND slack_user_id=?", (team, user)
            ).fetchone()
        if not row:
            raise UserError("No recognized user.")
        return dict(row)

    def prepare_profile(self, team: str, user: str, base: str, intended: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE users SET base_name=?,intended_name=? WHERE team=? AND slack_user_id=?",
                (base, intended, team, user),
            )

    def record_profile(self, team: str, user: str, base: str, written: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE users SET base_name=?,last_written_name=?,intended_name='' WHERE team=? AND slack_user_id=?",
                (base, written, team, user),
            )

    def backup(self, target: Path) -> None:
        if target.resolve() == self.path.resolve():
            raise ValueError("Backup destination must differ from live database.")
        with self.connection() as db:
            dest = sqlite3.connect(target)
            try:
                db.backup(dest)
            finally:
                dest.close()
        os.chmod(target, 0o600)
