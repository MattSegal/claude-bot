"""Turns raw Slack events into the bot's own inbound types and decides which deserve a response."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from claude_bot.store import ThreadKey

MENTION_PATTERN = re.compile(r"<@([A-Z0-9]+)>")


class Command(StrEnum):
    STATUS = "!status"
    STOP = "!stop"
    RESTART = "!restart"
    RELEASE = "!release"
    HELP = "!help"


@dataclass(frozen=True)
class InboundMessage:
    thread: ThreadKey
    message_ts: str
    user_id: str
    text: str
    is_dm: bool


@dataclass(frozen=True)
class CommandRequest:
    command: Command
    thread: ThreadKey
    message_ts: str
    user_id: str


Inbound = InboundMessage | CommandRequest


class EventRouter:
    """Decides whether a Slack event is for the bot.

    The bot answers DMs, @mentions anywhere, and un-mentioned replies inside a thread it already has a session
    for. Everything else, including messages from people not on the allowlist, is ignored silently.
    """

    def __init__(
        self,
        bot_user_id: str,
        allowed_user_ids: frozenset[str],
        is_known_thread: Callable[[ThreadKey], bool],
    ) -> None:
        self._bot_user_id = bot_user_id
        self._allowed_user_ids = allowed_user_ids
        self._is_known_thread = is_known_thread

    def route_app_mention(self, event: dict[str, Any]) -> Inbound | None:
        if not self._is_from_allowed_human(event):
            return None
        return self._build(event, is_dm=False)

    def route_message(self, event: dict[str, Any]) -> Inbound | None:
        if not self._is_from_allowed_human(event):
            return None
        is_dm = event.get("channel_type") == "im"
        if is_dm:
            return self._build(event, is_dm=True)

        # A channel message that mentions the bot also arrives as an app_mention event; that one handles it.
        mentions_bot = f"<@{self._bot_user_id}>" in event.get("text", "")
        if mentions_bot:
            return None
        thread_ts = event.get("thread_ts")
        if thread_ts is None:
            return None
        thread = ThreadKey(event["channel"], thread_ts)
        if not self._is_known_thread(thread):
            return None
        return self._build(event, is_dm=False)

    def _is_from_allowed_human(self, event: dict[str, Any]) -> bool:
        if event.get("bot_id") or event.get("subtype"):
            return False
        return event.get("user") in self._allowed_user_ids

    def _build(self, event: dict[str, Any], is_dm: bool) -> Inbound | None:
        text = strip_mentions(event.get("text", ""))
        if not text:
            return None
        message_ts = event["ts"]
        thread = ThreadKey(event["channel"], event.get("thread_ts") or message_ts)
        command = parse_command(text)
        if command is not None:
            return CommandRequest(command, thread, message_ts, event["user"])
        return InboundMessage(thread, message_ts, event["user"], text, is_dm)


def strip_mentions(text: str) -> str:
    return MENTION_PATTERN.sub("", text).strip()


def parse_command(text: str) -> Command | None:
    try:
        return Command(text.strip().lower())
    except ValueError:
        return None
