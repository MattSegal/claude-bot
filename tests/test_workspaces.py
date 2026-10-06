from datetime import timedelta
from pathlib import Path

import pytest

from claude_bot.store import LeaseState, StateStore, ThreadKey
from claude_bot.workspaces import GitInspector, HeldWorkspace, ReleaseResult, WorkspacePool
from tests.conftest import FakeClock, make_git_repo

THREAD_A = ThreadKey("C1", "1.0")
THREAD_B = ThreadKey("C1", "2.0")
THREAD_C = ThreadKey("C1", "3.0")


def always_idle(_: ThreadKey) -> bool:
    return True


def never_idle(_: ThreadKey) -> bool:
    return False


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    return make_git_repo(tmp_path / "repo-0"), make_git_repo(tmp_path / "repo-1")


def make_pool(repos: tuple[Path, ...], clock: FakeClock, **kwargs) -> WorkspacePool:
    return WorkspacePool(
        workspaces=repos,
        store=StateStore(":memory:"),
        inspector=GitInspector("main"),
        clock=clock,
        lease_ttl=kwargs.pop("lease_ttl", timedelta(minutes=30)),
        preempt_after=kwargs.pop("preempt_after", timedelta(minutes=2)),
    )


def test_inspector_reports_clean_dirty_and_off_branch(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    inspector = GitInspector("main")

    assert inspector.describe_unclean(repo) is None

    (repo / "scratch.txt").write_text("x")
    assert inspector.describe_unclean(repo) == "1 uncommitted change(s)"

    (repo / "scratch.txt").unlink()
    import subprocess

    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True)
    assert inspector.describe_unclean(repo) == "checked out on branch 'feature' instead of 'main'"


def test_threads_get_free_workspaces_then_wait(repos, fake_clock) -> None:
    pool = make_pool(repos, fake_clock)

    assert pool.acquire(THREAD_A, always_idle) == (repos[0], [])
    assert pool.acquire(THREAD_B, always_idle) == (repos[1], [])
    assert pool.acquire(THREAD_C, never_idle) == (None, [])
    assert pool.acquire(THREAD_A, always_idle) == (repos[0], []), "re-acquire returns own lease"


def test_idle_lease_is_preempted_oldest_first_once_grace_has_passed(repos, fake_clock) -> None:
    pool = make_pool(repos, fake_clock, preempt_after=timedelta(minutes=2))
    pool.acquire(THREAD_A, always_idle)
    fake_clock.advance(60)
    pool.acquire(THREAD_B, always_idle)

    fake_clock.advance(90)  # A idle 2.5 minutes, B idle 1.5 minutes
    assert pool.acquire(THREAD_C, always_idle) == (repos[0], [])
    assert pool.workspace_for(THREAD_A) is None
    assert pool.workspace_for(THREAD_B) == repos[1]


def test_busy_lease_is_never_preempted(repos, fake_clock) -> None:
    pool = make_pool(repos[:1], fake_clock, preempt_after=timedelta(0))
    pool.acquire(THREAD_A, always_idle)

    workspace, held = pool.acquire(THREAD_B, lambda thread: thread != THREAD_A)

    assert workspace is None
    assert held == []


def test_dirty_workspace_is_held_instead_of_reclaimed(repos, fake_clock) -> None:
    pool = make_pool(repos[:1], fake_clock, preempt_after=timedelta(0))
    pool.acquire(THREAD_A, always_idle)
    (repos[0] / "scratch.txt").write_text("left behind")

    workspace, held = pool.acquire(THREAD_B, always_idle)

    assert workspace is None
    assert held == [HeldWorkspace(repos[0], THREAD_A, "1 uncommitted change(s)")]
    lease = pool.statuses()[0].lease
    assert lease is not None and lease.state == LeaseState.HELD

    # The thread that left the mess keeps access so it can clean up, and the lease goes active again.
    assert pool.acquire(THREAD_A, always_idle) == (repos[0], [])
    lease = pool.statuses()[0].lease
    assert lease is not None and lease.state == LeaseState.ACTIVE


def test_sweep_releases_expired_clean_leases_and_holds_dirty_ones(repos, fake_clock) -> None:
    pool = make_pool(repos, fake_clock, lease_ttl=timedelta(minutes=30))
    pool.acquire(THREAD_A, always_idle)
    pool.acquire(THREAD_B, always_idle)
    (repos[1] / "scratch.txt").write_text("left behind")

    fake_clock.advance(10 * 60)
    assert pool.sweep(always_idle) == []
    assert pool.workspace_for(THREAD_A) == repos[0], "not expired yet"

    fake_clock.advance(25 * 60)
    held = pool.sweep(always_idle)

    assert held == [HeldWorkspace(repos[1], THREAD_B, "1 uncommitted change(s)")]
    assert pool.workspace_for(THREAD_A) is None
    assert pool.sweep(always_idle) == [], "a held lease is reported once"

    # Cleaning the checkout by hand releases the held lease on the next sweep.
    (repos[1] / "scratch.txt").unlink()
    pool.sweep(always_idle)
    assert pool.workspace_for(THREAD_B) is None


def test_sweep_keeps_expired_lease_of_a_busy_thread(repos, fake_clock) -> None:
    pool = make_pool(repos[:1], fake_clock, lease_ttl=timedelta(minutes=1))
    pool.acquire(THREAD_A, always_idle)
    fake_clock.advance(600)

    pool.sweep(never_idle)

    assert pool.workspace_for(THREAD_A) == repos[0]


def test_touch_extends_a_lease(repos, fake_clock) -> None:
    pool = make_pool(repos[:1], fake_clock, lease_ttl=timedelta(minutes=1))
    pool.acquire(THREAD_A, always_idle)
    fake_clock.advance(50)
    pool.touch(THREAD_A)
    fake_clock.advance(50)

    pool.sweep(always_idle)

    assert pool.workspace_for(THREAD_A) == repos[0]


def test_broken_checkout_is_held_not_fatal(tmp_path: Path, fake_clock) -> None:
    repo = make_git_repo(tmp_path / "repo")
    pool = make_pool((repo,), fake_clock, preempt_after=timedelta(0))
    pool.acquire(THREAD_A, always_idle)
    import shutil

    shutil.rmtree(repo / ".git")

    workspace, held = pool.acquire(THREAD_B, always_idle)

    assert workspace is None
    [item] = held
    assert item.thread == THREAD_A
    assert item.reason.startswith("git failing")


def test_unleased_dirty_checkout_is_skipped_and_reported(repos, fake_clock) -> None:
    pool = make_pool(repos, fake_clock)
    (repos[0] / "scratch.txt").write_text("someone's manual work")

    assert pool.acquire(THREAD_A, always_idle) == (repos[1], [])
    assert pool.acquire(THREAD_B, never_idle) == (None, [])
    first, second = pool.statuses()
    assert first.lease is None and first.unclean_reason == "1 uncommitted change(s)"
    assert second.lease is not None and second.unclean_reason is None


def test_release_gives_back_a_clean_checkout_and_holds_a_dirty_one(repos, fake_clock) -> None:
    pool = make_pool(repos, fake_clock)
    pool.acquire(THREAD_A, always_idle)
    pool.acquire(THREAD_B, always_idle)
    (repos[1] / "scratch.txt").write_text("left behind")

    assert pool.release(THREAD_A) == ReleaseResult(repos[0])
    assert pool.workspace_for(THREAD_A) is None

    dirty = pool.release(THREAD_B)
    assert dirty == ReleaseResult(repos[1], "1 uncommitted change(s)")
    assert not dirty.was_released
    lease = pool.statuses()[1].lease
    assert lease is not None and lease.state == LeaseState.HELD

    assert pool.release(THREAD_C) == ReleaseResult(None)
