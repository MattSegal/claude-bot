from pathlib import Path

from claude_bot.store import LeaseRecord, LeaseState, StateStore, ThreadKey

THREAD = ThreadKey("C1", "1.000")


def test_thread_round_trip() -> None:
    store = StateStore(":memory:")

    assert store.get_thread(THREAD) is None
    store.save_thread(THREAD, "sess-1", now=10.0, is_session_started=True)
    record = store.get_thread(THREAD)
    assert record is not None
    assert record.session_id == "sess-1"
    assert record.created_at == 10.0

    store.save_thread(THREAD, "sess-2", now=20.0, is_session_started=False)
    record = store.get_thread(THREAD)
    assert record is not None
    assert record.session_id == "sess-2"
    assert record.is_session_started is False
    assert record.created_at == 10.0, "created_at survives a session replacement"
    assert record.last_active_at == 20.0

    store.forget_thread(THREAD)
    assert store.get_thread(THREAD) is None


def test_lease_round_trip_and_lookup_by_thread() -> None:
    store = StateStore(":memory:")
    workspace = Path("/repo")

    store.save_lease(LeaseRecord(workspace, THREAD, acquired_at=1.0, last_active_at=1.0))
    lease = store.get_lease_for_thread(THREAD)
    assert lease is not None
    assert lease.workspace == workspace
    assert lease.state == LeaseState.ACTIVE

    store.save_lease(lease.held("2 uncommitted change(s)"))
    held = store.get_lease(workspace)
    assert held is not None
    assert held.state == LeaseState.HELD
    assert held.held_reason == "2 uncommitted change(s)"

    store.delete_lease(workspace)
    assert store.list_leases() == []


def test_state_survives_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    store = StateStore(db_path)
    store.save_thread(THREAD, "sess-1", now=1.0, is_session_started=True)
    store.close()

    reopened = StateStore(db_path)
    record = reopened.get_thread(THREAD)
    assert record is not None
    assert record.session_id == "sess-1"


def test_register_thread_never_replaces_an_existing_session() -> None:
    store = StateStore(":memory:")

    store.register_thread(THREAD, "sess-1", now=1.0)
    store.mark_session_started(THREAD, "sess-1", now=2.0)
    store.register_thread(THREAD, "sess-2", now=3.0)

    record = store.get_thread(THREAD)
    assert record is not None
    assert record.session_id == "sess-1"
    assert record.is_session_started is True


def test_replace_session_only_moves_off_the_expected_session() -> None:
    store = StateStore(":memory:")
    store.register_thread(THREAD, "sess-1", now=1.0)

    assert store.replace_session(THREAD, "sess-0", "sess-x", now=2.0) is False
    assert store.replace_session(THREAD, "sess-1", "sess-2", now=2.0) is True

    record = store.get_thread(THREAD)
    assert record is not None
    assert record.session_id == "sess-2"
    assert record.is_session_started is False


def test_mark_session_started_is_a_no_op_for_a_superseded_session() -> None:
    store = StateStore(":memory:")
    store.register_thread(THREAD, "sess-1", now=1.0)
    store.replace_session(THREAD, "sess-1", "sess-2", now=2.0)

    store.mark_session_started(THREAD, "sess-1", now=3.0)

    record = store.get_thread(THREAD)
    assert record is not None
    assert record.session_id == "sess-2" and record.is_session_started is False
