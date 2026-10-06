"""End-to-end flows through Conversations with a fake Slack client and the fake claude executable."""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest

from claude_bot.conversation import Conversations
from claude_bot.presenter import SlackPresenter
from claude_bot.runner import ClaudeRunner
from claude_bot.slack_events import Command, CommandRequest, InboundMessage
from claude_bot.store import StateStore, ThreadKey
from claude_bot.workspaces import GitInspector, WorkspacePool
from tests.conftest import FAKE_CLAUDE, FakeClaudeState, FakeClock, FakeSlackClient, make_git_repo

THREAD_A = ThreadKey("C1", "100.1")
THREAD_B = ThreadKey("C1", "200.1")


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        slack: FakeSlackClient,
        clock: FakeClock,
        workspace_count: int = 1,
        **overrides,
    ) -> None:
        self.slack = slack
        self.clock = clock
        self.workspaces = tuple(
            make_git_repo(tmp_path / f"repo-{index}") for index in range(workspace_count)
        )
        self.store = StateStore(":memory:")
        self.output_dir = tmp_path / "output"
        self.output_dir.mkdir()
        self.pool = WorkspacePool(
            workspaces=self.workspaces,
            store=self.store,
            inspector=GitInspector("main"),
            clock=clock,
            lease_ttl=overrides.get("lease_ttl", timedelta(minutes=30)),
            preempt_after=overrides.get("preempt_after", timedelta(0)),
        )
        self.conversations = Conversations(
            store=self.store,
            pool=self.pool,
            runner=ClaudeRunner(
                str(FAKE_CLAUDE), overrides.get("turn_timeout", timedelta(seconds=30))
            ),
            presenter=SlackPresenter(
                slack, "UBOT", frozenset({"UOMAR", "UJANE"}), clock=clock, progress_edit_interval=0
            ),
            output_dir=self.output_dir,
            default_branch="main",
            workspace_wait_timeout=overrides.get("wait_timeout", timedelta(minutes=30)),
            clock=clock,
            worker_idle_timeout=overrides.get("worker_idle_timeout", timedelta(minutes=5)),
        )
        self._message_counter = 0

    def send(self, text: str, thread: ThreadKey = THREAD_A, user: str = "UJANE") -> InboundMessage:
        self._message_counter += 1
        message_ts = f"{thread.thread_ts.split('.')[0]}.{self._message_counter:03d}"
        message = InboundMessage(thread, message_ts, user, text, is_dm=False)
        self.conversations.submit(message)
        return message

    def command(self, command: Command, thread: ThreadKey = THREAD_A, user: str = "UOMAR") -> None:
        self.conversations.handle_command(CommandRequest(command, thread, "1.1", user))

    def wait_for_reaction(self, name: str, count: int = 1) -> None:
        self.slack.wait_for(lambda: self.slack.reactions_added().count(name) >= count)

    def wait_for_text(self, fragment: str) -> None:
        self.slack.wait_for(lambda: any(fragment in text for text in self.slack.posted_texts()))

    def wait_for_progress(self, fragment: str) -> None:
        self.slack.wait_for(
            lambda: any(fragment in call["text"] for call in self.slack.calls_to("chat_update"))
        )

    def answers(self) -> list[str]:
        return [
            call["text"] for call in self.slack.calls_to("chat_postMessage") if "blocks" in call
        ]


@pytest.fixture
def harness(
    tmp_path: Path, slack: FakeSlackClient, fake_clock: FakeClock, fake_claude: FakeClaudeState
) -> Harness:
    return Harness(tmp_path, slack, fake_clock)


def test_first_message_runs_a_turn_and_answers_in_thread(
    harness: Harness, fake_claude: FakeClaudeState
) -> None:
    harness.send("TOOL when was it created?")
    harness.wait_for_reaction("white_check_mark")

    assert harness.slack.reactions_added() == ["eyes", "white_check_mark"]
    assert harness.answers() == ["echo: Jane: TOOL when was it created?"]
    progress_edits = harness.slack.calls_to("chat_update")
    assert any("Bash: List files" in edit["text"] for edit in progress_edits)
    assert progress_edits[-1]["text"].startswith("✅ Done")

    [call] = fake_claude.calls()
    assert call["cwd"] == str(harness.workspaces[0])
    assert "--session-id" in call["argv"]
    assert str(harness.workspaces[0]) in call["system_prompt"]
    record = harness.store.get_thread(THREAD_A)
    assert record is not None
    assert harness.pool.workspace_for(THREAD_A) == harness.workspaces[0]


def test_follow_up_resumes_the_same_session(harness: Harness, fake_claude: FakeClaudeState) -> None:
    harness.send("first")
    harness.wait_for_reaction("white_check_mark")
    harness.send("second", user="UOMAR")
    harness.wait_for_reaction("white_check_mark", count=2)

    first, second = fake_claude.calls()
    session_id = first["argv"][first["argv"].index("--session-id") + 1]
    assert second["argv"][-2:] == ["--resume", session_id]
    assert second["prompt"] == "Omar: second"


def test_lost_session_is_rebuilt_from_thread_history(
    harness: Harness, fake_claude: FakeClaudeState
) -> None:
    harness.send("first")
    harness.wait_for_reaction("white_check_mark")
    record = harness.store.get_thread(THREAD_A)
    assert record is not None
    fake_claude.forget_session(record.session_id)
    harness.slack.thread_replies = [
        {"user": "UJANE", "ts": "100.001000", "text": "first"},
        {
            "user": "UBOT",
            "ts": "100.001500",
            "text": "echo: Jane: first",
            "metadata": {"event_type": "claude_answer", "event_payload": {}},
        },
    ]

    harness.send("second")
    harness.wait_for_reaction("white_check_mark", count=2)

    calls = fake_claude.calls()
    assert len(calls) == 3
    assert "--resume" in calls[1]["argv"], "tried to resume first"
    primed = calls[2]
    assert "--session-id" in primed["argv"]
    assert "Jane: first" in primed["prompt"]
    assert "Claude: echo: Jane: first" in primed["prompt"]
    assert primed["prompt"].endswith("Jane: second")
    new_record = harness.store.get_thread(THREAD_A)
    assert new_record is not None and new_record.session_id != record.session_id


def test_second_thread_waits_for_a_workspace_then_takes_it(harness: Harness) -> None:
    harness.send("SLEEP:1.5 a", thread=THREAD_A)
    harness.slack.wait_for(lambda: harness.pool.workspace_for(THREAD_A) is not None)
    harness.send("b", thread=THREAD_B)

    harness.wait_for_text("Every workspace is busy")
    harness.wait_for_reaction("white_check_mark", count=2)

    assert harness.pool.workspace_for(THREAD_B) == harness.workspaces[0]
    assert harness.pool.workspace_for(THREAD_A) is None


def test_dirty_workspace_is_held_and_the_waiting_thread_eventually_gives_up(
    tmp_path: Path, slack: FakeSlackClient, fake_clock: FakeClock, fake_claude: FakeClaudeState
) -> None:
    harness = Harness(tmp_path, slack, fake_clock, wait_timeout=timedelta(seconds=0))
    harness.send("DIRTY leave a mess", thread=THREAD_A)
    harness.wait_for_reaction("white_check_mark")

    harness.send("b", thread=THREAD_B)
    harness.wait_for_reaction("x")

    notices = harness.slack.calls_to("chat_postMessage")
    held_notice = next(call for call in notices if "⚠️" in call["text"])
    assert held_notice["thread_ts"] == THREAD_A.thread_ts
    assert "1 uncommitted change(s)" in held_notice["text"]
    gave_up = next(call for call in notices if "No workspace came free" in call["text"])
    assert gave_up["thread_ts"] == THREAD_B.thread_ts


def test_stop_cancels_the_running_turn(harness: Harness) -> None:
    harness.send("SLEEP:30 long task")
    harness.wait_for_progress("Sleeping")

    harness.command(Command.STOP)

    harness.wait_for_reaction("octagonal_sign")
    assert "⏹ Stopped by Omar." in harness.slack.posted_texts()
    assert harness.slack.calls_to("chat_update")[-1]["text"].startswith("⏹ Stopped")
    assert harness.answers() == []


def test_stop_with_nothing_running_is_private(harness: Harness) -> None:
    harness.command(Command.STOP)

    [ephemeral] = harness.slack.calls_to("chat_postEphemeral")
    assert ephemeral["user"] == "UOMAR"
    assert "Nothing is running" in ephemeral["text"]


def test_restart_starts_a_new_session_but_keeps_following_the_thread(
    harness: Harness, fake_claude: FakeClaudeState
) -> None:
    harness.send("first")
    harness.wait_for_reaction("white_check_mark")

    harness.command(Command.RESTART)
    assert harness.conversations.is_known_thread(THREAD_A), (
        "un-mentioned replies must still reach us"
    )
    harness.send("second")
    harness.wait_for_reaction("white_check_mark", count=2)

    first, second = fake_claude.calls()
    assert "--session-id" in second["argv"]
    assert second["argv"] != first["argv"]
    assert any("Fresh session" in text for text in harness.slack.posted_texts())


def test_restart_before_any_session_is_private(harness: Harness) -> None:
    harness.command(Command.RESTART)

    [ephemeral] = harness.slack.calls_to("chat_postEphemeral")
    assert "No session" in ephemeral["text"]


def test_stop_drops_queued_messages_too(harness: Harness, fake_claude: FakeClaudeState) -> None:
    harness.send("SLEEP:30 one")
    harness.wait_for_progress("Sleeping")
    harness.send("two")
    harness.send("three")

    harness.command(Command.STOP)
    harness.wait_for_reaction("octagonal_sign", count=3)

    assert any("Dropped 2 queued message(s)" in text for text in harness.slack.posted_texts())
    assert len(fake_claude.calls()) == 1


def test_stop_while_waiting_for_a_workspace(harness: Harness) -> None:
    harness.send("SLEEP:30 hog", thread=THREAD_A)
    harness.wait_for_progress("Sleeping")
    harness.send("waiting", thread=THREAD_B)
    harness.wait_for_text("Every workspace is busy")

    harness.command(Command.STOP, thread=THREAD_B)

    harness.wait_for_reaction("octagonal_sign")
    stopped = [
        call for call in harness.slack.calls_to("reactions_add") if call["name"] == "octagonal_sign"
    ]
    assert stopped[0]["timestamp"].startswith("200.")
    assert "x" not in harness.slack.reactions_added()


def test_idle_workers_are_retired(
    tmp_path: Path, slack: FakeSlackClient, fake_clock: FakeClock, fake_claude: FakeClaudeState
) -> None:
    harness = Harness(tmp_path, slack, fake_clock, worker_idle_timeout=timedelta(seconds=0.2))
    harness.send("first")
    harness.wait_for_reaction("white_check_mark")

    harness.slack.wait_for(lambda: not harness.conversations._workers)

    harness.send("second")
    harness.wait_for_reaction("white_check_mark", count=2)
    assert len(fake_claude.calls()) == 2


def test_progress_is_closed_when_a_turn_crashes(harness: Harness) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("slack is down")

    harness.conversations._presenter.post_answer = explode  # type: ignore[method-assign]

    harness.send("first")
    harness.wait_for_reaction("x")

    assert harness.slack.calls_to("chat_update")[-1]["text"].startswith("❌ Failed")
    assert any("Something went wrong" in text for text in harness.slack.posted_texts())


def test_status_is_private_and_lists_workspaces(harness: Harness) -> None:
    harness.send("first")
    harness.wait_for_reaction("white_check_mark")

    harness.command(Command.STATUS, thread=THREAD_B)

    [ephemeral] = harness.slack.calls_to("chat_postEphemeral")
    assert ephemeral["text"].startswith("This thread: no session yet.")
    assert "leased by <https://slack.com/archives/C1/p1001|another thread>" in ephemeral["text"]


def test_failed_turn_posts_the_error(harness: Harness) -> None:
    harness.send("FAIL please")

    harness.wait_for_reaction("x")

    assert any(
        "Claude Code failed" in text and "boom" in text for text in harness.slack.posted_texts()
    )
    assert harness.slack.calls_to("chat_update")[-1]["text"].startswith("❌ Failed")


def test_denied_tools_are_shown_in_progress_and_footer(harness: Harness) -> None:
    harness.send("DENY clean the build")

    harness.wait_for_reaction("white_check_mark")

    assert any("🚫 Bash blocked" in edit["text"] for edit in harness.slack.calls_to("chat_update"))
    [answer] = harness.answers()
    assert answer.endswith("⚠️ 1 tool call was blocked by permissions: Bash")


def test_timed_out_turn(
    tmp_path: Path, slack: FakeSlackClient, fake_clock: FakeClock, fake_claude: FakeClaudeState
) -> None:
    harness = Harness(tmp_path, slack, fake_clock, turn_timeout=timedelta(seconds=1))
    harness.send("SLEEP:30")

    harness.wait_for_reaction("x")

    assert any("I gave up" in text for text in harness.slack.posted_texts())


def test_output_files_are_uploaded_and_removed(harness: Harness) -> None:
    prefix = f"{THREAD_A.channel}_{THREAD_A.thread_ts.replace('.', '_')}_"
    output_path = harness.output_dir / f"{prefix}chart.png"

    harness.send(f"OUTPUT:{output_path} draw a chart")
    harness.wait_for_reaction("white_check_mark")

    [upload] = harness.slack.calls_to("files_upload_v2")
    assert upload["filename"] == "chart.png"
    assert upload["thread_ts"] == THREAD_A.thread_ts
    assert not output_path.exists()


def test_messages_in_one_thread_run_in_order(
    harness: Harness, fake_claude: FakeClaudeState
) -> None:
    harness.send("SLEEP:0.5 one")
    harness.send("two")
    harness.send("three")

    harness.wait_for_reaction("white_check_mark", count=3)

    prompts = [call["prompt"] for call in fake_claude.calls()]
    assert prompts == ["Jane: SLEEP:0.5 one", "Jane: two", "Jane: three"]


def test_sweep_releases_an_idle_lease(harness: Harness) -> None:
    harness.send("first")
    harness.wait_for_reaction("white_check_mark")

    harness.clock.advance(31 * 60)
    harness.conversations.sweep()

    assert harness.pool.workspace_for(THREAD_A) is None


def test_thread_is_known_as_soon_as_its_first_message_is_accepted(harness: Harness) -> None:
    harness.send("SLEEP:30 first")
    harness.wait_for_progress("Sleeping")

    assert harness.conversations.is_known_thread(THREAD_A)

    harness.command(Command.STOP)
    harness.wait_for_reaction("octagonal_sign")


def test_restart_during_a_running_turn_is_not_undone_by_that_turn(harness: Harness) -> None:
    harness.send("SLEEP:30 first")
    harness.wait_for_progress("Sleeping")
    old_record = harness.store.get_thread(THREAD_A)
    assert old_record is not None

    harness.command(Command.RESTART)
    harness.wait_for_reaction("octagonal_sign")

    record = harness.store.get_thread(THREAD_A)
    assert record is not None
    assert record.session_id != old_record.session_id
    assert record.is_session_started is False


def test_worker_survives_slack_failing_while_reporting_a_failure(
    harness: Harness, fake_claude: FakeClaudeState
) -> None:
    presenter = harness.conversations._presenter
    real_post_notice = presenter.post_notice

    def explode(*args, **kwargs):
        raise RuntimeError("slack is down")

    presenter.post_answer = explode  # type: ignore[method-assign]
    presenter.post_notice = explode  # type: ignore[method-assign]
    harness.send("first")
    harness.slack.wait_for(lambda: len(fake_claude.calls()) == 1)
    harness.slack.wait_for(lambda: harness.conversations.is_idle(THREAD_A))

    presenter.post_answer = SlackPresenter.post_answer.__get__(presenter)  # type: ignore[method-assign]
    presenter.post_notice = real_post_notice  # type: ignore[method-assign]
    harness.send("second")
    harness.wait_for_reaction("white_check_mark")

    assert harness.answers() == ["echo: Jane: second"]


def test_dirty_unleased_checkout_is_not_handed_out(
    tmp_path: Path, slack: FakeSlackClient, fake_clock: FakeClock, fake_claude: FakeClaudeState
) -> None:
    harness = Harness(tmp_path, slack, fake_clock, wait_timeout=timedelta(seconds=0))
    (harness.workspaces[0] / "scratch.txt").write_text("manual work in progress")

    harness.send("first")
    harness.wait_for_reaction("x")

    assert fake_claude.calls() == []
    harness.command(Command.STATUS)
    [ephemeral] = harness.slack.calls_to("chat_postEphemeral")
    assert "unleased but needs cleanup (1 uncommitted change(s))" in ephemeral["text"]


def test_restart_during_session_recovery_wins(
    harness: Harness, fake_claude: FakeClaudeState
) -> None:
    harness.send("first")
    harness.wait_for_reaction("white_check_mark")
    record = harness.store.get_thread(THREAD_A)
    assert record is not None
    fake_claude.forget_session(record.session_id)

    presenter = harness.conversations._presenter
    fetch_started = threading.Event()
    release_fetch = threading.Event()

    def blocking_fetch_history(*args, **kwargs):
        fetch_started.set()
        release_fetch.wait(timeout=10)
        return []

    presenter.fetch_history = blocking_fetch_history  # type: ignore[method-assign]
    harness.send("second")
    assert fetch_started.wait(timeout=10)

    harness.command(Command.RESTART)
    restarted = harness.store.get_thread(THREAD_A)
    assert restarted is not None
    release_fetch.set()
    harness.wait_for_reaction("octagonal_sign")

    final = harness.store.get_thread(THREAD_A)
    assert final is not None
    assert final.session_id == restarted.session_id, "recovery must not overwrite the restart"
    assert final.is_session_started is False
    assert len(fake_claude.calls()) == 2, "no primed replacement turn ran"


def test_concurrent_first_messages_share_one_session(
    harness: Harness, fake_claude: FakeClaudeState
) -> None:
    messages = [
        InboundMessage(THREAD_A, f"100.{index:03d}", "UJANE", f"message {index}", is_dm=False)
        for index in range(1, 6)
    ]
    threads = [
        threading.Thread(target=harness.conversations.submit, args=(message,))
        for message in messages
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    harness.wait_for_reaction("white_check_mark", count=5)

    session_ids = {
        call["argv"][call["argv"].index("--session-id") + 1]
        for call in fake_claude.calls()
        if "--session-id" in call["argv"]
    }
    assert len(session_ids) == 1
    assert all("--resume" in call["argv"] for call in fake_claude.calls()[1:])


def test_release_frees_the_workspace_for_a_waiting_thread(harness: Harness) -> None:
    harness.send("first", thread=THREAD_A)
    harness.wait_for_reaction("white_check_mark")

    harness.command(Command.RELEASE, thread=THREAD_A)

    assert harness.pool.workspace_for(THREAD_A) is None
    assert any(text.startswith("🔓 Released") for text in harness.slack.posted_texts())
    assert harness.conversations.is_known_thread(THREAD_A), "the session survives a release"


def test_release_is_refused_while_working_and_private_when_nothing_is_held(
    harness: Harness,
) -> None:
    harness.command(Command.RELEASE, thread=THREAD_B)
    harness.send("SLEEP:30 busy", thread=THREAD_A)
    harness.wait_for_progress("Sleeping")
    harness.command(Command.RELEASE, thread=THREAD_A)

    nothing_held, still_working = harness.slack.calls_to("chat_postEphemeral")
    assert "isn't holding a workspace" in nothing_held["text"]
    assert "still working" in still_working["text"]
    assert harness.pool.workspace_for(THREAD_A) == harness.workspaces[0]

    harness.command(Command.STOP)
    harness.wait_for_reaction("octagonal_sign")


def test_release_of_a_dirty_checkout_keeps_it_held(harness: Harness) -> None:
    harness.send("DIRTY leave a mess")
    harness.wait_for_reaction("white_check_mark")

    harness.command(Command.RELEASE)

    assert harness.pool.workspace_for(THREAD_A) == harness.workspaces[0]
    assert any(
        "Can't release" in text and "1 uncommitted change(s)" in text
        for text in harness.slack.posted_texts()
    )
