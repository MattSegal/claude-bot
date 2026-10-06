"""Leasing of repo checkouts (workspaces) to Slack threads.

A thread leases a workspace for as long as it is active, then for an idle grace period so follow-up questions
land in the same checkout. A lease is reclaimed when it expires or when another thread needs a workspace and
this one has been idle long enough. A checkout is only ever reclaimed clean: if it has uncommitted changes or
is off the default branch, the lease is marked held and the thread is told, and a person sorts it out. Held
leases are retried on every sweep, so cleaning the checkout by hand releases it without touching the bot.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from claude_bot.store import LeaseRecord, LeaseState, StateStore, ThreadKey

logger = logging.getLogger(__name__)

Clock = Callable[[], float]
IsThreadIdle = Callable[[ThreadKey], bool]


@dataclass(frozen=True)
class HeldWorkspace:
    workspace: Path
    thread: ThreadKey
    reason: str


@dataclass(frozen=True)
class ReleaseResult:
    # None when the thread held no lease.
    workspace: Path | None
    # Why the checkout could not be given back, or None when it was released.
    unclean_reason: str | None = None

    @property
    def was_released(self) -> bool:
        return self.workspace is not None and self.unclean_reason is None


@dataclass(frozen=True)
class WorkspaceStatus:
    workspace: Path
    lease: LeaseRecord | None
    idle_seconds: float | None
    # Why an unleased checkout cannot be handed out, or None when it is clean or leased.
    unclean_reason: str | None


class GitInspector:
    def __init__(self, default_branch: str) -> None:
        self._default_branch = default_branch

    def describe_unclean(self, workspace: Path) -> str | None:
        """Returns why the checkout is not ready for a new conversation, or None if it is clean."""
        try:
            return self._describe_unclean(workspace)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            # A checkout git cannot read (stale lock, moved directory) needs a person just as much as a
            # dirty one, and must not stop the other checkouts from being handed out.
            return f"git failing ({_describe_git_error(exc)})"

    def _describe_unclean(self, workspace: Path) -> str | None:
        status = _git(workspace, "status", "--porcelain")
        if status:
            changed_count = len(status.splitlines())
            return f"{changed_count} uncommitted change(s)"
        branch = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
        if branch != self._default_branch:
            return f"checked out on branch {branch!r} instead of {self._default_branch!r}"
        return None


class WorkspacePool:
    def __init__(
        self,
        workspaces: tuple[Path, ...],
        store: StateStore,
        inspector: GitInspector,
        clock: Clock,
        lease_ttl: timedelta,
        preempt_after: timedelta = timedelta(minutes=2),
    ) -> None:
        self._workspaces = workspaces
        self._store = store
        self._inspector = inspector
        self._clock = clock
        self._lease_ttl_seconds = lease_ttl.total_seconds()
        self._preempt_after_seconds = preempt_after.total_seconds()
        self._lock = threading.Lock()

    def acquire(
        self, thread: ThreadKey, is_idle: IsThreadIdle
    ) -> tuple[Path | None, list[HeldWorkspace]]:
        """Leases a workspace for the thread, or returns None when every workspace is busy.

        Also returns any leases that had to be marked held along the way, so the caller can notify
        their threads.
        """
        now = self._clock()
        with self._lock:
            own_lease = self._store.get_lease_for_thread(thread)
            if own_lease is not None:
                # A thread may keep working in a checkout it dirtied itself; it is the one that can clean it.
                self._store.save_lease(
                    LeaseRecord(own_lease.workspace, thread, own_lease.acquired_at, now)
                )
                return own_lease.workspace, []

            free_workspace = self._first_free_workspace()
            if free_workspace is not None:
                self._store.save_lease(LeaseRecord(free_workspace, thread, now, now))
                return free_workspace, []

            held: list[HeldWorkspace] = []
            for lease in self._preemptable_leases(now, is_idle):
                reclaimed = self._reclaim(lease, held)
                if reclaimed:
                    self._store.save_lease(LeaseRecord(lease.workspace, thread, now, now))
                    return lease.workspace, held
            return None, held

    def touch(self, thread: ThreadKey) -> None:
        now = self._clock()
        with self._lock:
            lease = self._store.get_lease_for_thread(thread)
            if lease is not None:
                self._store.save_lease(LeaseRecord(lease.workspace, thread, lease.acquired_at, now))

    def release(self, thread: ThreadKey) -> ReleaseResult:
        """Gives the thread's checkout back on request. A checkout that is not clean stays held."""
        with self._lock:
            lease = self._store.get_lease_for_thread(thread)
            if lease is None:
                return ReleaseResult(workspace=None)
            reason = self._inspector.describe_unclean(lease.workspace)
            if reason is not None:
                self._store.save_lease(lease.held(reason))
                return ReleaseResult(lease.workspace, reason)
            self._store.delete_lease(lease.workspace)
            return ReleaseResult(lease.workspace)

    def workspace_for(self, thread: ThreadKey) -> Path | None:
        lease = self._store.get_lease_for_thread(thread)
        return lease.workspace if lease is not None else None

    def sweep(self, is_idle: IsThreadIdle) -> list[HeldWorkspace]:
        """Releases leases idle past the TTL and retries held ones. Returns leases newly marked held."""
        now = self._clock()
        held: list[HeldWorkspace] = []
        with self._lock:
            for lease in self._store.list_leases():
                if lease.state == LeaseState.HELD:
                    if self._inspector.describe_unclean(lease.workspace) is None:
                        self._store.delete_lease(lease.workspace)
                    continue
                is_expired = now - lease.last_active_at >= self._lease_ttl_seconds
                if is_expired and is_idle(lease.thread):
                    self._reclaim(lease, held)
        return held

    def statuses(self) -> list[WorkspaceStatus]:
        now = self._clock()
        statuses = []
        for workspace in self._workspaces:
            lease = self._store.get_lease(workspace)
            idle_seconds = now - lease.last_active_at if lease else None
            unclean_reason = None if lease else self._inspector.describe_unclean(workspace)
            statuses.append(WorkspaceStatus(workspace, lease, idle_seconds, unclean_reason))
        return statuses

    def _first_free_workspace(self) -> Path | None:
        """The first unleased checkout that is clean. A checkout someone left dirty by hand is skipped."""
        for workspace in self._workspaces:
            if self._store.get_lease(workspace) is not None:
                continue
            reason = self._inspector.describe_unclean(workspace)
            if reason is None:
                return workspace
            logger.warning("Skipping unleased checkout %s: %s", workspace, reason)
        return None

    def _preemptable_leases(self, now: float, is_idle: IsThreadIdle) -> list[LeaseRecord]:
        candidates = [
            lease
            for lease in self._store.list_leases()
            if lease.state == LeaseState.ACTIVE
            and is_idle(lease.thread)
            and now - lease.last_active_at >= self._preempt_after_seconds
        ]
        return sorted(candidates, key=lambda lease: lease.last_active_at)

    def _reclaim(self, lease: LeaseRecord, held: list[HeldWorkspace]) -> bool:
        reason = self._inspector.describe_unclean(lease.workspace)
        if reason is None:
            self._store.delete_lease(lease.workspace)
            return True
        self._store.save_lease(lease.held(reason))
        held.append(HeldWorkspace(lease.workspace, lease.thread, reason))
        return False


def _describe_git_error(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return (exc.stderr or "").strip() or f"exit status {exc.returncode}"
    return type(exc).__name__


def _git(workspace: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, check=True, timeout=30
    )
    return completed.stdout.strip()
