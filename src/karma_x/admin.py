"""Local operator tools. No additional Slack commands or public admin endpoints."""

import argparse
import json
from pathlib import Path

from karma_x.config import Config
from karma_x.core import UserError, parse_command
from karma_x.slack import Slack, SlackFailure
from karma_x.store import Store


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SQLite backups and explicit delivery recovery")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--db", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    status = commands.add_parser("status")
    status.add_argument("--db", type=Path, required=True)
    retry = commands.add_parser("retry-effect")
    retry.add_argument("recognition")
    retry.add_argument("kind", choices=["profile", "update"])
    reconcile = commands.add_parser("attach-post")
    reconcile.add_argument("recognition")
    reconcile.add_argument("timestamp", help="Exact Slack message ts, such as 1791633600.123456")
    args = parser.parse_args(argv)
    try:
        if args.command in {"backup", "status"}:
            if not args.db.is_file():
                raise UserError("Database does not exist; refusing to create an empty backup.")
            store = Store(args.db)
            if args.command == "backup":
                if args.output.exists():
                    raise UserError("Backup destination already exists; choose a new filename.")
                args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                store.backup(args.output)
                print(f"Backup saved: {args.output}")
                return
            with store.connection() as db:
                counts = {
                    table: {r[0]: r[1] for r in db.execute(query)}
                    for table, query in (
                        ("requests", "SELECT state,COUNT(*) FROM requests GROUP BY state"),
                        ("effects", "SELECT state,COUNT(*) FROM effects GROUP BY state"),
                    )
                }
                integrity = db.execute("PRAGMA quick_check").fetchone()[0]
            print(json.dumps({"integrity": integrity, **counts}, sort_keys=True))
            return
        config = Config.from_env()
        if not config.database.is_file():
            raise UserError("Database does not exist.")
        store = Store(config.database)
        parse_command("recognition view " + args.recognition)
        r = store.get(config.team_id, args.recognition)
        if args.command == "retry-effect":
            store.retry_safe_effect(r["id"], args.kind)
            print(f"Queued {args.kind} recovery for {r['id']}")
            return
        slack = Slack(config.bot_token, config.profile_token)
        bot_user = slack.call("auth.test")["user_id"]
        response = slack.call(
            "conversations.history",
            channel=r["channel"],
            oldest=args.timestamp,
            latest=args.timestamp,
            inclusive=True,
            limit=1,
        )
        messages = response.get("messages", [])
        if not messages or messages[0].get("ts") != args.timestamp:
            raise UserError("Message not found at that exact timestamp.")
        message = messages[0]
        text = message.get("text")
        # The bot appends this immutable final line AFTER the user-controlled reason.
        # Metadata can be stripped by Slack; a substring in the reason is not identity.
        if (
            message.get("user") != bot_user
            or not isinstance(text, str)
            or not text.splitlines()
            or text.splitlines()[-1] != f"Recognition ID: {r['id']}"
        ):
            raise UserError("Message is not this bot's matching recognition.")
        link = slack.permalink(r["channel"], args.timestamp)
        store.attach_post(r["id"], args.timestamp, link)
        print(f"Matched existing Slack post to {r['id']}; no new post sent.")
    except (UserError, ValueError, SlackFailure) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
