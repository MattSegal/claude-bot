from collections.abc import Callable
from typing import Any

from claude_bot.main import register_handlers
from claude_bot.slack_events import EventRouter


class FakeBoltApp:
    def __init__(self) -> None:
        self.handlers: dict[str, Callable[..., None]] = {}

    def event(self, name: str) -> Callable[[Callable[..., None]], Callable[..., None]]:
        def register(handler: Callable[..., None]) -> Callable[..., None]:
            self.handlers[name] = handler
            return handler

        return register


class RecordingConversations:
    def __init__(self) -> None:
        self.submitted: list[Any] = []
        self.commands: list[Any] = []

    def is_known_thread(self, thread: Any) -> bool:
        return False

    def submit(self, message: Any) -> None:
        self.submitted.append(message)

    def handle_command(self, request: Any) -> None:
        self.commands.append(request)


def test_a_dm_that_mentions_the_bot_is_handled_once() -> None:
    app = FakeBoltApp()
    conversations = RecordingConversations()
    router = EventRouter("UBOT", frozenset({"UJANE"}), conversations.is_known_thread)
    register_handlers(app, router, conversations)  # type: ignore[arg-type]
    dm = {"user": "UJANE", "channel": "D1", "channel_type": "im", "ts": "1.1", "text": "<@UBOT> hi"}

    app.handlers["message"](event=dm)
    app.handlers["app_mention"](event=dm)

    assert len(conversations.submitted) == 1


def test_a_redelivered_command_is_handled_once() -> None:
    app = FakeBoltApp()
    conversations = RecordingConversations()
    router = EventRouter("UBOT", frozenset({"UJANE"}), conversations.is_known_thread)
    register_handlers(app, router, conversations)  # type: ignore[arg-type]
    mention = {"user": "UJANE", "channel": "C1", "ts": "1.1", "text": "<@UBOT> !status"}

    app.handlers["app_mention"](event=mention)
    app.handlers["app_mention"](event=mention)

    assert len(conversations.commands) == 1
