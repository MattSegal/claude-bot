"""SQLite persistence for thread sessions and workspace leases.

A Slack thread is identified by its channel and root timestamp. Each known thread owns one Claude Code
session ID, and may hold a lease on one workspace. Everything is keyed so the bot can restart without losing
which thread maps to which session or checkout.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple


class ThreadKey(NamedTuple):
    channel: str
    thread_ts: str


class LeaseState(StrEnum):
    ACTIVE = "active"
    # The lease expired or was preempted but the checkout is not clean, so a human has to look at it before
    # another conversation can use it.
    HELD = "held"


@dataclass(frozen=True)
class ThreadRecord:
    key: ThreadKey
    session_id: str
    # False until the session has had a turn, which is when Claude Code creates it. A restart assigns a fresh
    # ID with this cleared, so the thread stays known to the router while the next turn starts from scratch.
    is_session_started: bool
    created_at: float
    last_active_at: float


@dataclass(frozen=True)
class LeaseRecord:
    workspace: Path
    thread: ThreadKey
    acquired_at: float
    last_active_at: float
    state: LeaseState = LeaseState.ACTIVE
    held_reason: str | None = None

    def held(self, reason: str) -> LeaseRecord:
        return replace(self, state=LeaseState.HELD, held_reason=reason)


class StateStore:
    def __init__(self, path: Path | str) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_thread(self, key: ThreadKey) -> ThreadRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM threads WHERE channel = ? AND thread_ts = ?", key
            ).fetchone()
        return _thread_from_row(row) if row else None

    def save_thread(
        self, key: ThreadKey, session_id: str, now: float, *, is_session_started: bool
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO threads
                    (channel, thread_ts, session_id, is_session_started, created_at, last_active_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (channel, thread_ts) DO UPDATE SET
                    session_id = excluded.session_id,
                    is_session_started = excluded.is_session_started,
                    last_active_at = excluded.last_active_at
                """,
                (key.channel, key.thread_ts, session_id, int(is_session_started), now, now),
            )

    def register_thread(self, key: ThreadKey, session_id: str, now: float) -> None:
        """Creates the thread with an unstarted session, leaving an existing record untouched."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO threads
                    (channel, thread_ts, session_id, is_session_started, created_at, last_active_at)
                VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT (channel, thread_ts) DO NOTHING
                """,
                (key.channel, key.thread_ts, session_id, now, now),
            )

    def replace_session(
        self, key: ThreadKey, expected_session_id: str, new_session_id: str, now: float
    ) -> bool:
        """Moves the thread to a new, unstarted session. Returns False if it already left the expected one."""
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE threads SET session_id = ?, is_session_started = 0, last_active_at = ?
                WHERE channel = ? AND thread_ts = ? AND session_id = ?
                """,
                (new_session_id, now, key.channel, key.thread_ts, expected_session_id),
            )
            return cursor.rowcount == 1

    def mark_session_started(self, key: ThreadKey, session_id: str, now: float) -> None:
        """Records that a turn ran in this session, unless the thread has since moved to another session."""
        with self._lock:
            self._conn.execute(
                """
                UPDATE threads SET is_session_started = 1, last_active_at = ?
                WHERE channel = ? AND thread_ts = ? AND session_id = ?
                """,
                (now, key.channel, key.thread_ts, session_id),
            )

    def touch_thread(self, key: ThreadKey, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE threads SET last_active_at = ? WHERE channel = ? AND thread_ts = ?",
                (now, key.channel, key.thread_ts),
            )

    def forget_thread(self, key: ThreadKey) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM threads WHERE channel = ? AND thread_ts = ?", key)

    def get_lease(self, workspace: Path) -> LeaseRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM leases WHERE workspace = ?", (str(workspace),)
            ).fetchone()
        return _lease_from_row(row) if row else None

    def get_lease_for_thread(self, key: ThreadKey) -> LeaseRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM leases WHERE channel = ? AND thread_ts = ?", key
            ).fetchone()
        return _lease_from_row(row) if row else None

    def list_leases(self) -> list[LeaseRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM leases ORDER BY acquired_at").fetchall()
        return [_lease_from_row(row) for row in rows]

    def save_lease(self, lease: LeaseRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO leases
                    (workspace, channel, thread_ts, acquired_at, last_active_at, state, held_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (workspace) DO UPDATE SET
                    channel = excluded.channel,
                    thread_ts = excluded.thread_ts,
                    acquired_at = excluded.acquired_at,
                    last_active_at = excluded.last_active_at,
                    state = excluded.state,
                    held_reason = excluded.held_reason
                """,
                (
                    str(lease.workspace),
                    lease.thread.channel,
                    lease.thread.thread_ts,
                    lease.acquired_at,
                    lease.last_active_at,
                    lease.state.value,
                    lease.held_reason,
                ),
            )

    def delete_lease(self, workspace: Path) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM leases WHERE workspace = ?", (str(workspace),))

    def _create_tables(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    channel TEXT NOT NULL,
                    thread_ts TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    is_session_started INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    last_active_at REAL NOT NULL,
                    PRIMARY KEY (channel, thread_ts)
                );
                CREATE TABLE IF NOT EXISTS leases (
                    workspace TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    thread_ts TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    last_active_at REAL NOT NULL,
                    state TEXT NOT NULL,
                    held_reason TEXT
                );
                """
            )


def _thread_from_row(row: sqlite3.Row) -> ThreadRecord:
    return ThreadRecord(
        key=ThreadKey(row["channel"], row["thread_ts"]),
        session_id=row["session_id"],
        is_session_started=bool(row["is_session_started"]),
        created_at=row["created_at"],
        last_active_at=row["last_active_at"],
    )


def _lease_from_row(row: sqlite3.Row) -> LeaseRecord:
    return LeaseRecord(
        workspace=Path(row["workspace"]),
        thread=ThreadKey(row["channel"], row["thread_ts"]),
        acquired_at=row["acquired_at"],
        last_active_at=row["last_active_at"],
        state=LeaseState(row["state"]),
        held_reason=row["held_reason"],
    )
