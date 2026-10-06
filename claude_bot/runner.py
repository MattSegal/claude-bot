"""Runs one Claude Code turn as a headless subprocess and streams its events back."""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from claude_bot.stream import StreamEvent, TurnResult, TurnStatus, parse_stream_line

logger = logging.getLogger(__name__)

OnEvent = Callable[[StreamEvent], None]

WATCHDOG_POLL_SECONDS = 0.5


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


@dataclass(frozen=True)
class TurnRequest:
    prompt: str
    workspace: Path
    session_id: str
    is_resume: bool
    system_prompt: str


class ClaudeRunner:
    def __init__(self, claude_bin: str, turn_timeout: timedelta) -> None:
        self._claude_bin = claude_bin
        self._turn_timeout_seconds = turn_timeout.total_seconds()

    def run_turn(self, request: TurnRequest, on_event: OnEvent, cancel: CancelToken) -> TurnResult:
        command = self._build_command(request)
        logger.info("Starting claude in %s (resume=%s)", request.workspace, request.is_resume)
        # The prompt goes in on stdin rather than argv so a message starting with "-" is never parsed as a flag.
        process = subprocess.Popen(
            command,
            cwd=request.workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        assert process.stdin and process.stdout and process.stderr

        stderr_lines: list[str] = []
        stderr_reader = threading.Thread(
            target=_drain, args=(process.stderr, stderr_lines), daemon=True
        )
        stderr_reader.start()

        timed_out = threading.Event()
        watchdog = threading.Thread(
            target=self._watch, args=(process, cancel, timed_out), daemon=True
        )
        watchdog.start()

        # Written from its own thread so a large prompt can never deadlock against our stdout reading.
        stdin_writer = threading.Thread(
            target=_write_prompt, args=(process.stdin, request.prompt), daemon=True
        )
        stdin_writer.start()

        result: TurnResult | None = None
        for line in process.stdout:
            for event in parse_stream_line(line):
                if isinstance(event, TurnResult):
                    result = event
                else:
                    on_event(event)
        process.wait()
        stderr_reader.join(timeout=5)
        stderr_text = "".join(stderr_lines).strip()

        if cancel.is_cancelled:
            return TurnResult(TurnStatus.CANCELLED, session_id=request.session_id)
        if timed_out.is_set():
            return TurnResult(TurnStatus.TIMED_OUT, session_id=request.session_id)
        if result is None:
            logger.error("claude exited %s without a result: %s", process.returncode, stderr_text)
            errors = (stderr_text or f"claude exited with status {process.returncode}",)
            return TurnResult(TurnStatus.FAILED, session_id=request.session_id, errors=errors)
        if result.status == TurnStatus.FAILED and stderr_text:
            logger.error("claude turn failed: %s", stderr_text)
        return result

    def _build_command(self, request: TurnRequest) -> list[str]:
        session_args = (
            ["--resume", request.session_id]
            if request.is_resume
            else ["--session-id", request.session_id]
        )
        return [
            self._claude_bin,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "default",
            "--append-system-prompt",
            request.system_prompt,
            *session_args,
        ]

    def _watch(
        self, process: subprocess.Popen, cancel: CancelToken, timed_out: threading.Event
    ) -> None:
        """Kills the whole process group when the turn is cancelled or exceeds its time limit."""
        deadline = time.monotonic() + self._turn_timeout_seconds
        while process.poll() is None:
            if cancel.wait(WATCHDOG_POLL_SECONDS):
                break
            if time.monotonic() >= deadline:
                timed_out.set()
                break
        if process.poll() is None:
            _kill_process_group(process)


def _write_prompt(stdin, prompt: str) -> None:
    with contextlib.suppress(BrokenPipeError, OSError):
        stdin.write(prompt)
        stdin.close()


def _drain(stream, sink: list[str]) -> None:
    for line in stream:
        sink.append(line)


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
