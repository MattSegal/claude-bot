"""Everything the bot shows in Slack: reactions, the progress message, answers, notices and file uploads."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from claude_bot.markdown import split_markdown
from claude_bot.prompt import HistoryEntry
from claude_bot.slack_events import parse_command
from claude_bot.store import ThreadKey
from claude_bot.stream import PermissionDenied, StreamEvent, ToolUse

logger = logging.getLogger(__name__)

RECEIVED_REACTION = "eyes"
DONE_REACTION = "white_check_mark"
FAILED_REACTION = "x"
STOPPED_REACTION = "octagonal_sign"

# Posting more than this many pieces floods the thread; past it the answer goes up as a file instead.
MAX_ANSWER_PIECES = 2
PROGRESS_LINES = 4

# Attached to every answer so history priming can tell answers apart from progress and notice messages.
ANSWER_METADATA = {"event_type": "claude_answer", "event_payload": {}}


class SlackClient(Protocol):
    """The slice of slack_sdk.WebClient the bot uses, so tests can substitute a recorder."""

    def chat_postMessage(self, *, channel: str, **kwargs: Any) -> Any: ...
    def chat_update(self, *, channel: str, ts: str, **kwargs: Any) -> Any: ...
    def chat_postEphemeral(self, *, channel: str, user: str, **kwargs: Any) -> Any: ...
    def reactions_add(self, *, channel: str, name: str, timestamp: str, **kwargs: Any) -> Any: ...
    def reactions_remove(self, *, name: str, **kwargs: Any) -> Any: ...
    def files_upload_v2(self, **kwargs: Any) -> Any: ...
    def conversations_replies(self, *, channel: str, ts: str, **kwargs: Any) -> Any: ...
    def users_info(self, *, user: str, **kwargs: Any) -> Any: ...


class ProgressMessage:
    """A single "working" message in the thread, edited in place as tool calls happen.

    Edits never notify anyone, which is the point: the thread stays quiet until the answer arrives as a new
    message. Edits are throttled because Slack rate-limits chat.update.
    """

    def __init__(
        self,
        client: SlackClient,
        thread: ThreadKey,
        clock: Callable[[], float],
        min_edit_interval: float,
    ) -> None:
        self._client = client
        self._thread = thread
        self._clock = clock
        self._min_edit_interval = min_edit_interval
        self._lock = threading.Lock()
        self._lines: deque[str] = deque(maxlen=PROGRESS_LINES)
        self._tool_call_count = 0
        self._started_at = clock()
        self._last_edit_at = 0.0
        self._message_ts: str | None = None

    def start(self) -> None:
        response = self._client.chat_postMessage(
            channel=self._thread.channel, thread_ts=self._thread.thread_ts, text=self._render()
        )
        self._message_ts = response["ts"]
        self._last_edit_at = self._clock()

    def note(self, event: StreamEvent) -> None:
        if isinstance(event, ToolUse):
            line = f"{event.name}: {event.summary}" if event.summary else event.name
            self._add_line(line)
        elif isinstance(event, PermissionDenied):
            self._add_line(f"🚫 {event.tool_name} blocked: {_truncate(event.message)}")

    def finish(self, summary: str) -> None:
        self._edit(f"{summary} · {self._describe_effort()}", force=True)

    @property
    def tool_call_count(self) -> int:
        return self._tool_call_count

    def elapsed_text(self) -> str:
        return _format_duration(self._clock() - self._started_at)

    def _add_line(self, line: str) -> None:
        with self._lock:
            self._tool_call_count += 1
            self._lines.append(line)
        self._edit(self._render(), force=False)

    def _render(self) -> str:
        header = f"⏳ Working… ({self._describe_effort()})"
        lines = "\n".join(f"• {line}" for line in self._lines)
        return f"{header}\n{lines}" if lines else header

    def _describe_effort(self) -> str:
        calls = self._tool_call_count
        noun = "tool call" if calls == 1 else "tool calls"
        return f"{calls} {noun}, {self.elapsed_text()}"

    def _edit(self, text: str, force: bool) -> None:
        if self._message_ts is None:
            return
        now = self._clock()
        if not force and now - self._last_edit_at < self._min_edit_interval:
            return
        self._last_edit_at = now
        try:
            self._client.chat_update(channel=self._thread.channel, ts=self._message_ts, text=text)
        except Exception:
            logger.warning("Could not update progress message", exc_info=True)


class SlackPresenter:
    def __init__(
        self,
        client: SlackClient,
        bot_user_id: str,
        allowed_user_ids: frozenset[str],
        clock: Callable[[], float] = time.time,
        progress_edit_interval: float = 3.0,
    ) -> None:
        self._client = client
        self._bot_user_id = bot_user_id
        self._allowed_user_ids = allowed_user_ids
        self._clock = clock
        self._progress_edit_interval = progress_edit_interval
        self._display_names: dict[str, str] = {}

    def acknowledge(self, channel: str, message_ts: str) -> None:
        self._react(channel, message_ts, RECEIVED_REACTION)

    def mark_done(self, channel: str, message_ts: str) -> None:
        self._swap_reaction(channel, message_ts, DONE_REACTION)

    def mark_failed(self, channel: str, message_ts: str) -> None:
        self._swap_reaction(channel, message_ts, FAILED_REACTION)

    def mark_stopped(self, channel: str, message_ts: str) -> None:
        self._swap_reaction(channel, message_ts, STOPPED_REACTION)

    def start_progress(self, thread: ThreadKey) -> ProgressMessage:
        progress = ProgressMessage(self._client, thread, self._clock, self._progress_edit_interval)
        progress.start()
        return progress

    def post_answer(self, thread: ThreadKey, markdown_text: str) -> None:
        pieces = split_markdown(markdown_text)
        if not pieces:
            self.post_notice(thread, "_Claude finished without saying anything._")
            return
        if len(pieces) > MAX_ANSWER_PIECES:
            self._post_markdown(thread, pieces[0] + "\n\n_Full answer attached._")
            self.upload_text(thread, "answer.md", markdown_text)
            return
        for piece in pieces:
            self._post_markdown(thread, piece)

    def post_notice(self, thread: ThreadKey, text: str) -> None:
        self._client.chat_postMessage(channel=thread.channel, thread_ts=thread.thread_ts, text=text)

    def post_ephemeral(self, thread: ThreadKey, user_id: str, text: str) -> None:
        try:
            self._client.chat_postEphemeral(
                channel=thread.channel, thread_ts=thread.thread_ts, user=user_id, text=text
            )
        except Exception:
            # Ephemeral posts are not allowed everywhere (for example some DMs); fall back to a real message.
            logger.info("Ephemeral post failed, posting normally", exc_info=True)
            self.post_notice(thread, text)

    def upload_file(self, thread: ThreadKey, path: Path, display_name: str) -> None:
        self._client.files_upload_v2(
            channel=thread.channel,
            thread_ts=thread.thread_ts,
            file=str(path),
            filename=display_name,
        )

    def upload_text(self, thread: ThreadKey, filename: str, content: str) -> None:
        self._client.files_upload_v2(
            channel=thread.channel, thread_ts=thread.thread_ts, content=content, filename=filename
        )

    def display_name(self, user_id: str) -> str:
        cached = self._display_names.get(user_id)
        if cached:
            return cached
        try:
            user = self._client.users_info(user=user_id)["user"]
            name = user.get("real_name") or user.get("name") or user_id
        except Exception:
            logger.warning("Could not look up user %s", user_id, exc_info=True)
            name = user_id
        self._display_names[user_id] = name
        return name

    def fetch_history(self, thread: ThreadKey, before_ts: str) -> list[HistoryEntry]:
        """The thread's earlier messages, oldest first, as (who, what) pairs. Bot progress messages are skipped."""
        entries: list[HistoryEntry] = []
        cursor: str | None = None
        while True:
            response = self._client.conversations_replies(
                channel=thread.channel,
                ts=thread.thread_ts,
                cursor=cursor,
                limit=200,
                include_all_metadata=True,
            )
            for message in response.get("messages", []):
                if float(message.get("ts", "0")) >= float(before_ts):
                    continue
                entry = self._history_entry(message)
                if entry is not None:
                    entries.append(entry)
            cursor = response.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                break
        return entries

    def _history_entry(self, message: dict[str, Any]) -> HistoryEntry | None:
        text = message.get("text", "")
        if not text or message.get("subtype"):
            return None
        is_from_bot = message.get("user") == self._bot_user_id or bool(message.get("bot_id"))
        if is_from_bot:
            is_answer = (
                message.get("metadata", {}).get("event_type") == ANSWER_METADATA["event_type"]
            )
            return HistoryEntry("Claude", text) if is_answer else None
        user_id = message.get("user", "")
        # People the bot ignores live must stay ignored when a session is rebuilt from the thread.
        if user_id not in self._allowed_user_ids or parse_command(text) is not None:
            return None
        return HistoryEntry(self.display_name(user_id), text)

    def _post_markdown(self, thread: ThreadKey, markdown_text: str) -> None:
        blocks = [{"type": "markdown", "text": markdown_text}]
        try:
            self._client.chat_postMessage(
                channel=thread.channel,
                thread_ts=thread.thread_ts,
                text=markdown_text,
                blocks=blocks,
                metadata=ANSWER_METADATA,
            )
        except Exception:
            logger.warning("Markdown block rejected, posting as plain text", exc_info=True)
            self._client.chat_postMessage(
                channel=thread.channel,
                thread_ts=thread.thread_ts,
                text=markdown_text,
                metadata=ANSWER_METADATA,
            )

    def _react(self, channel: str, message_ts: str, name: str) -> None:
        try:
            self._client.reactions_add(channel=channel, timestamp=message_ts, name=name)
        except Exception:
            logger.debug("Could not add reaction %s", name, exc_info=True)

    def _swap_reaction(self, channel: str, message_ts: str, name: str) -> None:
        try:
            self._client.reactions_remove(
                channel=channel, timestamp=message_ts, name=RECEIVED_REACTION
            )
        except Exception:
            logger.debug("Could not remove reaction", exc_info=True)
        self._react(channel, message_ts, name)


def _format_duration(seconds: float) -> str:
    whole = int(seconds)
    if whole < 60:
        return f"{whole}s"
    minutes, remainder = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _truncate(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
