import threading
import time
from datetime import timedelta
from pathlib import Path

from claude_bot.runner import CancelToken, ClaudeRunner, TurnRequest
from claude_bot.stream import AssistantText, PermissionDenied, ToolUse, TurnStatus
from tests.conftest import FAKE_CLAUDE


def _request(prompt: str, workspace: Path, is_resume: bool = False) -> TurnRequest:
    return TurnRequest(
        prompt=prompt,
        workspace=workspace,
        session_id="sess-1",
        is_resume=is_resume,
        system_prompt="be brief",
    )


def test_streams_events_and_returns_result(tmp_path: Path, fake_claude) -> None:
    runner = ClaudeRunner(str(FAKE_CLAUDE), timedelta(seconds=30))
    events = []

    result = runner.run_turn(_request("TOOL hello", tmp_path), events.append, CancelToken())

    assert result.status == TurnStatus.COMPLETED
    assert result.text == "echo: TOOL hello"
    assert result.session_id == "sess-1"
    assert events == [ToolUse("Bash", "List files"), AssistantText("echo: TOOL hello")]
    [call] = fake_claude.calls()
    assert call["prompt"] == "TOOL hello"
    assert call["cwd"] == str(tmp_path)
    assert call["system_prompt"] == "be brief"
    assert "--session-id" in call["argv"] and "sess-1" in call["argv"]
    assert "--permission-mode" in call["argv"]


def test_prompt_starting_with_a_dash_is_not_treated_as_a_flag(tmp_path: Path, fake_claude) -> None:
    runner = ClaudeRunner(str(FAKE_CLAUDE), timedelta(seconds=30))

    result = runner.run_turn(_request("--help me", tmp_path), lambda _: None, CancelToken())

    assert result.text == "echo: --help me"


def test_resume_passes_the_session_id(tmp_path: Path, fake_claude) -> None:
    runner = ClaudeRunner(str(FAKE_CLAUDE), timedelta(seconds=30))
    runner.run_turn(_request("first", tmp_path), lambda _: None, CancelToken())

    result = runner.run_turn(
        _request("second", tmp_path, is_resume=True), lambda _: None, CancelToken()
    )

    assert result.status == TurnStatus.COMPLETED
    assert fake_claude.calls()[1]["argv"][-2:] == ["--resume", "sess-1"]


def test_missing_session_is_reported(tmp_path: Path, fake_claude) -> None:
    runner = ClaudeRunner(str(FAKE_CLAUDE), timedelta(seconds=30))

    result = runner.run_turn(
        _request("hi", tmp_path, is_resume=True), lambda _: None, CancelToken()
    )

    assert result.status == TurnStatus.FAILED
    assert result.is_session_missing


def test_permission_denials_are_streamed_and_summarised(tmp_path: Path, fake_claude) -> None:
    runner = ClaudeRunner(str(FAKE_CLAUDE), timedelta(seconds=30))
    events = []

    result = runner.run_turn(_request("DENY", tmp_path), events.append, CancelToken())

    assert PermissionDenied("Bash", "rm needs approval") in events
    assert result.denied_tools == ("Bash",)


def test_failed_turn(tmp_path: Path, fake_claude) -> None:
    runner = ClaudeRunner(str(FAKE_CLAUDE), timedelta(seconds=30))

    result = runner.run_turn(_request("FAIL", tmp_path), lambda _: None, CancelToken())

    assert result.status == TurnStatus.FAILED
    assert result.errors == ("boom",)


def test_cancel_kills_the_process(tmp_path: Path, fake_claude) -> None:
    runner = ClaudeRunner(str(FAKE_CLAUDE), timedelta(seconds=30))
    cancel = CancelToken()
    threading.Timer(0.5, cancel.cancel).start()
    started = time.monotonic()

    result = runner.run_turn(_request("SLEEP:30", tmp_path), lambda _: None, cancel)

    assert result.status == TurnStatus.CANCELLED
    assert time.monotonic() - started < 10


def test_timeout_kills_the_process(tmp_path: Path, fake_claude) -> None:
    runner = ClaudeRunner(str(FAKE_CLAUDE), timedelta(seconds=1))
    started = time.monotonic()

    result = runner.run_turn(_request("SLEEP:30", tmp_path), lambda _: None, CancelToken())

    assert result.status == TurnStatus.TIMED_OUT
    assert time.monotonic() - started < 10


def test_missing_binary_is_a_failed_turn_not_a_crash(tmp_path: Path) -> None:
    runner = ClaudeRunner(str(tmp_path / "no-such-claude"), timedelta(seconds=5))

    try:
        runner.run_turn(_request("hi", tmp_path), lambda _: None, CancelToken())
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError to surface to the caller")


def test_watchdog_thread_exits_with_the_process(tmp_path: Path, fake_claude) -> None:
    runner = ClaudeRunner(str(FAKE_CLAUDE), timedelta(minutes=30))
    before = threading.active_count()

    runner.run_turn(_request("hi", tmp_path), lambda _: None, CancelToken())

    deadline = time.monotonic() + 5
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert threading.active_count() == before
