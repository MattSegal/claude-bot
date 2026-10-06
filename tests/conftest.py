from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"

DISPLAY_NAMES = {"UOMAR": "Omar", "UJANE": "Jane"}


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSlackClient:
    """Records every API call and answers with the minimum the bot needs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.thread_replies: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._ts_counter = 0

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self._record("chat_postMessage", kwargs)
        return {"ok": True, "ts": self._next_ts()}

    def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self._record("chat_update", kwargs)
        return {"ok": True}

    def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        self._record("chat_postEphemeral", kwargs)
        return {"ok": True}

    def reactions_add(self, **kwargs: Any) -> dict[str, Any]:
        self._record("reactions_add", kwargs)
        return {"ok": True}

    def reactions_remove(self, **kwargs: Any) -> dict[str, Any]:
        self._record("reactions_remove", kwargs)
        return {"ok": True}

    def files_upload_v2(self, **kwargs: Any) -> dict[str, Any]:
        self._record("files_upload_v2", kwargs)
        return {"ok": True}

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_replies", kwargs)
        return {"ok": True, "messages": list(self.thread_replies), "response_metadata": {}}

    def users_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("users_info", kwargs)
        user_id = kwargs["user"]
        return {"ok": True, "user": {"real_name": DISPLAY_NAMES.get(user_id, user_id)}}

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        with self._lock:
            return [kwargs for name, kwargs in self.calls if name == method]

    def posted_texts(self) -> list[str]:
        return [call.get("text", "") for call in self.calls_to("chat_postMessage")]

    def reactions_added(self) -> list[str]:
        return [call["name"] for call in self.calls_to("reactions_add")]

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError(f"Timed out waiting; calls so far: {self.calls}")

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        with self._lock:
            self.calls.append((name, kwargs))

    def _next_ts(self) -> str:
        with self._lock:
            self._ts_counter += 1
            return f"9000.{self._ts_counter:06d}"


def make_git_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path


class FakeClaudeState:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir

    def calls(self) -> list[dict[str, Any]]:
        calls_path = self.state_dir / "calls.jsonl"
        if not calls_path.exists():
            return []
        return [json.loads(line) for line in calls_path.read_text().splitlines() if line]

    def forget_session(self, session_id: str) -> None:
        (self.state_dir / "sessions" / session_id).unlink()


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeClaudeState:
    state_dir = tmp_path / "fake-claude"
    state_dir.mkdir()
    monkeypatch.setenv("FAKE_CLAUDE_STATE_DIR", str(state_dir))
    return FakeClaudeState(state_dir)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def slack() -> FakeSlackClient:
    return FakeSlackClient()
