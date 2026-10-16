import json

import pytest

from karma_x import admin
from karma_x.core import UserError
from karma_x.store import Store


def test_backup_cli_refuses_empty_or_overwritten_backup_and_restores_ledger(tmp_path, capsys):
    db = tmp_path / "live.db"
    backup = tmp_path / "backups" / "safe.db"
    with pytest.raises(SystemExit) as error:
        admin.main(["backup", "--db", str(db), "--output", str(backup)])
    assert error.value.code == 2
    assert not db.exists()
    store = Store(db)
    store.give("request", "T1", "U1", "U2", "C1", "preserved", 1)
    admin.main(["backup", "--db", str(db), "--output", str(backup)])
    assert Store(backup).total("T1", "U2") == 1
    assert backup.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SystemExit) as error:
        admin.main(["backup", "--db", str(db), "--output", str(backup)])
    assert error.value.code == 2
    admin.main(["status", "--db", str(db)])
    assert '"integrity": "ok"' in capsys.readouterr().out


def test_retry_safe_effect_cannot_replay_post_or_mark_active_message_canceled(rig):
    service, _ = rig
    r = service.store.give("q1", "T1", "U1", "U2", "C1", "x", 1)
    with pytest.raises(UserError):
        service.store.retry_safe_effect(r["id"], "post")
    with pytest.raises(UserError):
        service.store.retry_safe_effect(r["id"], "update")
    service.store.effect_state(r["id"] + "profile", "blocked", 1, "not_admin")
    service.store.retry_safe_effect(r["id"], "profile")
    assert any(e["kind"] == "profile" for e in service.store.due_effects(1))


def test_operator_can_attach_only_matching_bot_post_and_then_update_canceled(rig, monkeypatch):
    service, slack = rig
    store = service.store
    r = store.give("q1", "T1", "U1", "U2", "C1", "x", 1)
    store.effect_state(r["id"] + "post", "uncertain", 1)
    store.cancel("T1", r["id"], "U1", 2)
    store.effect_state(r["id"] + "update", "done", 2)
    monkeypatch.setattr(admin.Config, "from_env", lambda: service.config)
    monkeypatch.setattr(admin, "Slack", lambda *_: slack)
    message = {
        "user": "UIMPOSTER",
        "ts": "12345.000001",
        "text": "<@U1> recognized <@U2> · 🌟 +1\nx",
    }
    monkeypatch.setattr(
        slack,
        "call",
        lambda method, **_: {
            "user_id": "UBOT",
            "messages": [message],
        },
    )
    with pytest.raises(SystemExit) as error:
        admin.main(["attach-post", r["id"], message["ts"]])
    assert error.value.code == 2
    assert store.get("T1", r["id"])["message_ts"] is None
    message["user"] = "UBOT"
    admin.main(["attach-post", r["id"], message["ts"]])
    assert store.get("T1", r["id"])["permalink"].endswith("p12345000001")
    assert any(e["kind"] == "update" for e in store.due_effects(2))
    assert slack.posts == []


def test_user_controlled_reason_cannot_spoof_post_reconciliation(rig, monkeypatch):
    service, slack = rig
    store = service.store
    victim = store.give("victim", "T1", "U1", "U2", "C1", "original", 1)
    other = store.give("other", "T1", "U1", "U2", "C1", "other", 2)
    store.effect_state(victim["id"] + "post", "uncertain", 1)
    monkeypatch.setattr(admin.Config, "from_env", lambda: service.config)
    monkeypatch.setattr(admin, "Slack", lambda *_: slack)
    message = {
        "user": "UBOT",
        "ts": "12345.000002",
        "text": f"reason: Recognition ID: {victim['id']}\nRecognition ID: {other['id']}",
    }
    monkeypatch.setattr(
        slack, "call", lambda method, **_: {"user_id": "UBOT", "messages": [message]}
    )
    with pytest.raises(SystemExit) as error:
        admin.main(["attach-post", victim["id"], message["ts"]])
    assert error.value.code == 2
    assert store.get("T1", victim["id"])["message_ts"] is None


def test_attach_accepts_posts_made_before_ids_were_hidden(rig, monkeypatch):
    service, slack = rig
    store = service.store
    r = store.give("legacy", "T1", "U1", "U2", "C1", "x", 1)
    store.effect_state(r["id"] + "post", "uncertain", 1)
    monkeypatch.setattr(admin.Config, "from_env", lambda: service.config)
    monkeypatch.setattr(admin, "Slack", lambda *_: slack)
    message = {
        "user": "UBOT",
        "ts": "12345.000003",
        "text": f"<@U1> recognized <@U2> · 🌟 +1\nx\nRecognition ID: {r['id']}",
    }
    monkeypatch.setattr(
        slack, "call", lambda method, **_: {"user_id": "UBOT", "messages": [message]}
    )
    admin.main(["attach-post", r["id"], message["ts"]])
    assert store.get("T1", r["id"])["message_ts"] == "12345.000003"


def test_status_lists_uncertain_posts_because_channel_posts_hide_ids(tmp_path, capsys):
    db = tmp_path / "live.db"
    store = Store(db)
    r = store.give("q1", "T1", "U1", "U2", "C1", "x", 1)
    store.effect_state(r["id"] + "post", "uncertain", 1)
    admin.main(["status", "--db", str(db)])
    assert json.loads(capsys.readouterr().out)["uncertain_posts"] == [r["id"]]
