from concurrent.futures import ThreadPoolExecutor

import pytest

from karma_x.core import UserError
from karma_x.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "karma.db")


def give(s, key="req1", sender="U1", recipient="U2", now=100):
    return s.give(key, "T1", sender, recipient, "C1", "thanks", now)


def test_self_award_rejected(store):
    with pytest.raises(UserError):
        give(store, sender="U2")
    assert store.top("T1") == []


def test_duplicate_transport_once(store):
    a = give(store)
    b = give(store)
    assert a["id"] == b["id"]
    assert store.total("T1", "U2") == 1


def test_cancel_once_and_sender_only(store):
    r = give(store)
    with pytest.raises(UserError):
        store.cancel("T1", r["id"], "U3", 101)
    assert store.total("T1", "U2") == 1
    assert store.cancel("T1", r["id"], "U1", 101)["canceled_at_us"] == 101
    store.cancel("T1", r["id"], "U1", 102)
    assert store.total("T1", "U2") == 0
    assert store.top("T1") == []


def test_workspace_isolation(store):
    r = give(store)
    with pytest.raises(UserError):
        store.get("T2", r["id"])
    assert store.top("T2") == []


def test_concurrent_gives(store):
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda i: give(store, str(i)), range(50)))
    assert store.total("T1", "U2") == 50


def test_concurrent_cancellations(store):
    r = give(store)
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda i: store.cancel("T1", r["id"], "U1", 101 + i), range(10)))
    assert store.total("T1", "U2") == 0


def test_top30_and_shared_rank(store):
    for i in range(31):
        give(store, str(i), recipient=f"U{i + 10}")
    rows = store.top("T1")
    assert len(rows) == 30
    assert all(r["rank"] == 1 for r in rows)


def test_winners_boundaries_and_cancel(store):
    a = give(store, "a", now=99)
    b = give(store, "b", recipient="U3", now=100)
    give(store, "c", recipient="U4", now=199)
    give(store, "d", recipient="U5", now=200)
    assert {r["recipient"] for r in store.winners("T1", 100, 200)} == {"U3", "U4"}
    store.cancel("T1", b["id"], "U1", 300)
    assert [r["recipient"] for r in store.winners("T1", 100, 200)] == ["U4"]
    assert store.winners("T1", 500, 600) == []
    assert store.get("T1", a["id"])["reason"] == "thanks"


def test_reopen_and_backup(store, tmp_path):
    give(store)
    reopened = Store(store.path)
    assert reopened.total("T1", "U2") == 1
    backup = tmp_path / "backup.db"
    store.backup(backup)
    assert Store(backup).total("T1", "U2") == 1


def test_sql_injection_reason_is_data(store):
    store.give("x", "T1", "U1", "U2", "C1", "'); DROP TABLE recognitions; --", 100)
    assert store.total("T1", "U2") == 1


def test_request_dedup_and_job_recovery(store):
    payload = {"team_id": "T1", "user_id": "U1", "channel_id": "C1", "text": "list-all"}
    assert store.enqueue("a", payload, 100) is True
    assert store.enqueue("a", payload, 100) is False
    task = store.claim(100)
    assert task is not None and task["id"] == "a"
    assert store.claim(101) is None
    assert store.claim(61_000_101)["id"] == "a"
