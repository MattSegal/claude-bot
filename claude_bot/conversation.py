"""Runs each Slack thread as its own serial conversation with Claude Code.

Every thread gets a worker that processes its messages one at a time, so a follow-up never races the turn
before it. Different threads run in parallel, bounded by how many workspaces are free to lease.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from queue import Empty, Queue

from claude_bot.presenter import ProgressMessage, SlackPresenter
from claude_bot.prompt import build_system_prompt, format_primed_turn, format_user_turn
from claude_bot.runner import CancelToken, ClaudeRunner, TurnRequest
from claude_bot.slack_events import Command, CommandRequest, InboundMessage
from claude_bot.store import LeaseState, StateStore, ThreadKey
from claude_bot.stream import TurnResult, TurnStatus
from claude_bot.workspaces import HeldWorkspace, WorkspacePool

logger = logging.getLogger(__name__)

HELP_TEXT = """\
Mention me in a channel or DM me to start a thread; I'll follow the rest of that thread without being mentioned.
• `!status` — what I'm doing in this thread and which workspaces are in use
• `!stop` — stop what I'm doing in this thread and drop anything queued behind it
• `!restart` — forget this thread's session and start fresh on the next message
• `!release` — give this thread's workspace back now instead of waiting for it to time out
• `!help` — this message"""

WORKSPACE_WAIT_POLL_SECONDS = 15.0
ERROR_EXCERPT_LIMIT = 1500


class Conversations:
    def __init__(
        self,
        store: StateStore,
        pool: WorkspacePool,
        runner: ClaudeRunner,
        presenter: SlackPresenter,
        output_dir: Path,
        default_branch: str,
        workspace_wait_timeout: timedelta,
        clock: Callable[[], float] = time.time,
        worker_idle_timeout: timedelta = timedelta(minutes=5),
    ) -> None:
        self._store = store
        self._pool = pool
        self._runner = runner
        self._presenter = presenter
        self._output_dir = output_dir
        self._default_branch = default_branch
        self._workspace_wait_seconds = workspace_wait_timeout.total_seconds()
        self._clock = clock
        self._worker_idle_seconds = worker_idle_timeout.total_seconds()
        self._workers: dict[ThreadKey, ThreadWorker] = {}
        self._workers_lock = threading.Lock()
        self._workspace_released = threading.Condition()

    def is_known_thread(self, thread: ThreadKey) -> bool:
        return self._store.get_thread(thread) is not None

    def is_idle(self, thread: ThreadKey) -> bool:
        with self._workers_lock:
            worker = self._workers.get(thread)
        return worker is None or not worker.is_busy

    def submit(self, message: InboundMessage) -> None:
        self._presenter.acknowledge(message.thread.channel, message.message_ts)
        # Register the thread now, not after the first turn, so follow-ups and `!stop` sent while the first
        # turn is still running are routed here rather than dropped.
        self._store.register_thread(message.thread, _new_session_id(), self._clock())
        with self._workers_lock:
            worker = self._workers.get(message.thread)
            if worker is None:
                worker = ThreadWorker(
                    message.thread,
                    self._process_message,
                    idle_seconds=self._worker_idle_seconds,
                    on_idle=self._retire_worker,
                )
                self._workers[message.thread] = worker
            worker.enqueue(message)

    def handle_command(self, request: CommandRequest) -> None:
        thread = request.thread
        if request.command == Command.HELP:
            self._presenter.post_ephemeral(thread, request.user_id, HELP_TEXT)
        elif request.command == Command.STATUS:
            self._presenter.post_ephemeral(thread, request.user_id, self._status_text(thread))
        elif request.command == Command.STOP:
            self._handle_stop(request)
        elif request.command == Command.RESTART:
            self._handle_restart(request)
        elif request.command == Command.RELEASE:
            self._handle_release(request)

    def sweep(self) -> None:
        """Releases idle workspaces. Run periodically."""
        held = self._pool.sweep(self.is_idle)
        self._notify_held(held)
        self._signal_workspace_released()

    def _handle_stop(self, request: CommandRequest) -> None:
        thread = request.thread
        with self._workers_lock:
            worker = self._workers.get(thread)
        stopped = worker.stop_all() if worker else StopOutcome()
        self._signal_workspace_released()
        if not stopped.was_running and not stopped.dropped:
            self._presenter.post_ephemeral(thread, request.user_id, "Nothing is running here.")
            return
        for dropped in stopped.dropped:
            self._presenter.mark_stopped(thread.channel, dropped.message_ts)
        name = self._presenter.display_name(request.user_id)
        dropped_note = (
            f" Dropped {len(stopped.dropped)} queued message(s)." if stopped.dropped else ""
        )
        self._presenter.post_notice(thread, f"⏹ Stopped by {name}.{dropped_note}")

    def _handle_restart(self, request: CommandRequest) -> None:
        thread = request.thread
        if self._store.get_thread(thread) is None:
            self._presenter.post_ephemeral(
                thread, request.user_id, "No session in this thread yet."
            )
            return
        with self._workers_lock:
            worker = self._workers.get(thread)
        if worker:
            worker.stop_all()
        # Keep the thread known so un-mentioned follow-ups still reach us; only the session is replaced.
        self._store.save_thread(thread, _new_session_id(), self._clock(), is_session_started=False)
        self._presenter.post_notice(
            thread, "🔄 Fresh session: the next message starts over without the earlier context."
        )

    def _handle_release(self, request: CommandRequest) -> None:
        thread = request.thread
        if not self.is_idle(thread):
            self._presenter.post_ephemeral(
                thread,
                request.user_id,
                "I'm still working in this thread. `!stop` first, then `!release`.",
            )
            return
        result = self._pool.release(thread)
        if result.workspace is None:
            self._presenter.post_ephemeral(
                thread, request.user_id, "This thread isn't holding a workspace."
            )
        elif result.was_released:
            self._presenter.post_notice(thread, f"🔓 Released `{result.workspace}`.")
            self._signal_workspace_released()
        else:
            self._presenter.post_notice(
                thread,
                f"⚠️ Can't release `{result.workspace}`: it has {result.unclean_reason}. "
                "It stays reserved for this thread until someone cleans it up.",
            )

    def _retire_worker(self, worker: ThreadWorker) -> bool:
        """Lets an idle worker exit, unless a message slipped in. Called with the worker's own lock held."""
        with self._workers_lock:
            if worker.is_busy:
                return False
            del self._workers[worker.thread]
            return True

    def _process_message(self, message: InboundMessage, cancel: CancelToken) -> None:
        thread = message.thread
        try:
            workspace = self._acquire_workspace(message, cancel)
            if workspace is None:
                if cancel.is_cancelled:
                    self._presenter.mark_stopped(thread.channel, message.message_ts)
                else:
                    self._presenter.mark_failed(thread.channel, message.message_ts)
                return
            self._run_turn(message, workspace, cancel)
        except Exception:
            logger.exception("Unhandled error while processing a message in %s", thread)
            self._report_internal_failure(message)
        finally:
            self._signal_workspace_released()

    def _report_internal_failure(self, message: InboundMessage) -> None:
        # Reporting goes through Slack too; if that is what failed, the worker must survive regardless.
        thread = message.thread
        try:
            self._presenter.post_notice(
                thread, "❌ Something went wrong on my side; check the bot logs."
            )
            self._presenter.mark_failed(thread.channel, message.message_ts)
        except Exception:
            logger.exception("Could not report the failure to Slack either")

    def _run_turn(self, message: InboundMessage, workspace: Path, cancel: CancelToken) -> None:
        thread = message.thread
        sender_name = self._presenter.display_name(message.user_id)
        record = self._store.get_thread(thread)
        assert record is not None, "submit() registers the thread before any turn runs"
        file_prefix = _file_prefix(thread)
        request = TurnRequest(
            prompt=format_user_turn(sender_name, message.text),
            workspace=workspace,
            session_id=record.session_id,
            is_resume=record.is_session_started,
            system_prompt=build_system_prompt(
                workspace, self._default_branch, self._output_dir, file_prefix
            ),
        )

        progress = self._presenter.start_progress(thread)
        try:
            result = self._runner.run_turn(request, progress.note, cancel)

            if request.is_resume and result.is_session_missing:
                # The session file for this thread is gone (reinstall, cleanup), so start a replacement
                # session that has read the thread so far.
                history = self._presenter.fetch_history(thread, before_ts=message.message_ts)
                replacement = replace(
                    request,
                    prompt=format_primed_turn(history, sender_name, message.text),
                    session_id=_new_session_id(),
                    is_resume=False,
                )
                # A `!restart` that landed while we were fetching history already moved the thread on;
                # recovery must not drag the old context back in on top of it.
                is_still_current = not cancel.is_cancelled and self._store.replace_session(
                    thread, request.session_id, replacement.session_id, self._clock()
                )
                if not is_still_current:
                    result = TurnResult(TurnStatus.CANCELLED, session_id=request.session_id)
                else:
                    request = replacement
                    result = self._runner.run_turn(request, progress.note, cancel)

            if not result.is_session_missing:
                # Conditional on the thread still being on this session, so a turn that `!restart`
                # cancelled cannot resurrect the session the restart just replaced.
                self._store.mark_session_started(thread, request.session_id, self._clock())
            self._pool.touch(thread)
            self._present_result(message, progress, result)
        except Exception:
            progress.finish("❌ Failed")
            raise
        self._upload_output_files(thread, file_prefix)

    def _present_result(
        self, message: InboundMessage, progress: ProgressMessage, result: TurnResult
    ) -> None:
        thread = message.thread
        channel, message_ts = thread.channel, message.message_ts
        if result.status == TurnStatus.COMPLETED:
            progress.finish("✅ Done")
            self._presenter.post_answer(thread, _answer_with_denials(result))
            self._presenter.mark_done(channel, message_ts)
        elif result.status == TurnStatus.CANCELLED:
            progress.finish("⏹ Stopped")
            self._presenter.mark_stopped(channel, message_ts)
        elif result.status == TurnStatus.TIMED_OUT:
            progress.finish("❌ Timed out")
            self._presenter.post_notice(
                thread, f"❌ I gave up after {progress.elapsed_text()} without finishing."
            )
            self._presenter.mark_failed(channel, message_ts)
        else:
            progress.finish("❌ Failed")
            error_text = "\n".join(result.errors) or "unknown error"
            self._presenter.post_notice(
                thread, f"❌ Claude Code failed:\n```\n{error_text[:ERROR_EXCERPT_LIMIT]}\n```"
            )
            self._presenter.mark_failed(channel, message_ts)

    def _acquire_workspace(self, message: InboundMessage, cancel: CancelToken) -> Path | None:
        thread = message.thread
        deadline = self._clock() + self._workspace_wait_seconds
        has_announced_wait = False
        while not cancel.is_cancelled:
            workspace, held = self._pool.acquire(thread, self.is_idle)
            self._notify_held(held)
            if workspace is not None:
                return workspace
            if not has_announced_wait:
                self._presenter.post_notice(
                    thread,
                    "⏳ Every workspace is busy right now; I'll start as soon as one frees up.",
                )
                has_announced_wait = True
            remaining = deadline - self._clock()
            if remaining <= 0:
                self._presenter.post_notice(
                    thread,
                    "❌ No workspace came free in time. Try again later, or `!status` to see who has them.",
                )
                return None
            with self._workspace_released:
                self._workspace_released.wait(min(remaining, WORKSPACE_WAIT_POLL_SECONDS))
        return None

    def _notify_held(self, held: list[HeldWorkspace]) -> None:
        for item in held:
            self._presenter.post_notice(
                item.thread,
                f"⚠️ The checkout at `{item.workspace}` was left with {item.reason}, so it stays reserved for "
                "this thread and out of service for others until someone cleans it up.",
            )

    def _signal_workspace_released(self) -> None:
        with self._workspace_released:
            self._workspace_released.notify_all()

    def _upload_output_files(self, thread: ThreadKey, file_prefix: str) -> None:
        if not self._output_dir.exists():
            return
        for path in sorted(self._output_dir.iterdir()):
            if not path.is_file() or not path.name.startswith(file_prefix):
                continue
            display_name = path.name[len(file_prefix) :]
            try:
                self._presenter.upload_file(thread, path, display_name)
            except Exception:
                logger.exception("Failed to upload %s", path)
            finally:
                path.unlink(missing_ok=True)

    def _status_text(self, thread: ThreadKey) -> str:
        with self._workers_lock:
            worker = self._workers.get(thread)
        if worker is not None and worker.is_busy:
            thread_line = (
                f"This thread: working, {worker.pending_count} message(s) queued behind it."
            )
        elif self._store.get_thread(thread) is not None:
            thread_line = "This thread: idle, session kept."
        else:
            thread_line = "This thread: no session yet."

        workspace_lines = []
        for status in self._pool.statuses():
            lease = status.lease
            if lease is None and status.unclean_reason:
                state = f"unleased but needs cleanup ({status.unclean_reason})"
            elif lease is None:
                state = "free"
            elif lease.state == LeaseState.HELD:
                state = f"held, needs cleanup ({lease.held_reason})"
            else:
                owner = "this thread" if lease.thread == thread else _thread_link(lease.thread)
                idle = _format_minutes(status.idle_seconds or 0)
                state = f"leased by {owner}, last active {idle} ago"
            workspace_lines.append(f"• `{status.workspace}` — {state}")
        return "\n".join([thread_line, "Workspaces:", *workspace_lines])


class StopOutcome:
    def __init__(
        self, was_running: bool = False, dropped: list[InboundMessage] | None = None
    ) -> None:
        self.was_running = was_running
        self.dropped = dropped or []


class ThreadWorker:
    """Processes one thread's messages in order on its own OS thread, and exits once the thread goes quiet."""

    def __init__(
        self,
        thread: ThreadKey,
        process: Callable[[InboundMessage, CancelToken], None],
        idle_seconds: float,
        on_idle: Callable[[ThreadWorker], bool],
    ) -> None:
        self.thread = thread
        self._process = process
        self._idle_seconds = idle_seconds
        self._on_idle = on_idle
        self._queue: Queue[InboundMessage] = Queue()
        self._lock = threading.Lock()
        self._current_cancel: CancelToken | None = None
        self._runner = threading.Thread(
            target=self._run, name=f"thread-{thread.thread_ts}", daemon=True
        )
        self._runner.start()

    def enqueue(self, message: InboundMessage) -> None:
        self._queue.put(message)

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._current_cancel is not None or not self._queue.empty()

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    def stop_all(self) -> StopOutcome:
        """Cancels the running turn, if any, and drops every message queued behind it."""
        dropped: list[InboundMessage] = []
        while True:
            try:
                dropped.append(self._queue.get_nowait())
            except Empty:
                break
            self._queue.task_done()
        with self._lock:
            cancel = self._current_cancel
        if cancel is not None:
            cancel.cancel()
        return StopOutcome(was_running=cancel is not None, dropped=dropped)

    def _run(self) -> None:
        while True:
            try:
                message = self._queue.get(timeout=self._idle_seconds)
            except Empty:
                if self._on_idle(self):
                    return
                continue
            cancel = CancelToken()
            with self._lock:
                self._current_cancel = cancel
            try:
                self._process(message, cancel)
            except Exception:
                # The worker must outlive any one message, or the thread's queue is stranded forever.
                logger.exception("Worker for %s failed on a message", self.thread)
            finally:
                with self._lock:
                    self._current_cancel = None
                self._queue.task_done()


def _answer_with_denials(result: TurnResult) -> str:
    if not result.denied_tools:
        return result.text
    count = len(result.denied_tools)
    noun = "tool call was" if count == 1 else "tool calls were"
    return f"{result.text}\n\n⚠️ {count} {noun} blocked by permissions: {', '.join(result.denied_tools)}"


def _file_prefix(thread: ThreadKey) -> str:
    return f"{thread.channel}_{thread.thread_ts.replace('.', '_')}_"


def _new_session_id() -> str:
    return str(uuid.uuid4())


def _thread_link(thread: ThreadKey) -> str:
    permalink_ts = thread.thread_ts.replace(".", "")
    return f"<https://slack.com/archives/{thread.channel}/p{permalink_ts}|another thread>"


def _format_minutes(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes}m" if minutes else "under a minute"
