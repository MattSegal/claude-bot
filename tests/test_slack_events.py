from claude_bot.slack_events import Command, CommandRequest, EventRouter, InboundMessage
from claude_bot.store import ThreadKey

BOT = "UBOT"
ALLOWED = frozenset({"UOMAR", "UJANE"})
KNOWN_THREAD = ThreadKey("C1", "100.1")


def _router(known: set[ThreadKey] | None = None) -> EventRouter:
    known_threads = known or set()
    return EventRouter(BOT, ALLOWED, lambda thread: thread in known_threads)


def test_mention_starts_a_thread_at_the_mentioned_message() -> None:
    event = {"user": "UJANE", "channel": "C1", "ts": "200.1", "text": f"<@{BOT}> when was it made?"}

    inbound = _router().route_app_mention(event)

    assert inbound == InboundMessage(
        ThreadKey("C1", "200.1"), "200.1", "UJANE", "when was it made?", False
    )


def test_mention_inside_a_thread_keeps_the_thread() -> None:
    event = {
        "user": "UOMAR",
        "channel": "C1",
        "ts": "200.2",
        "thread_ts": "200.1",
        "text": f"<@{BOT}> more",
    }

    inbound = _router().route_app_mention(event)

    assert isinstance(inbound, InboundMessage)
    assert inbound.thread == ThreadKey("C1", "200.1")
    assert inbound.message_ts == "200.2"


def test_dm_needs_no_mention() -> None:
    event = {"user": "UJANE", "channel": "D1", "channel_type": "im", "ts": "1.1", "text": "hi"}

    inbound = _router().route_message(event)

    assert inbound == InboundMessage(ThreadKey("D1", "1.1"), "1.1", "UJANE", "hi", True)


def test_unmentioned_reply_in_known_thread_is_handled() -> None:
    event = {
        "user": "UJANE",
        "channel": "C1",
        "channel_type": "channel",
        "ts": "100.5",
        "thread_ts": "100.1",
        "text": "and the other one?",
    }

    inbound = _router(known={KNOWN_THREAD}).route_message(event)

    assert isinstance(inbound, InboundMessage)
    assert inbound.thread == KNOWN_THREAD


def test_unmentioned_reply_in_unknown_thread_is_ignored() -> None:
    event = {"user": "UJANE", "channel": "C1", "ts": "100.5", "thread_ts": "100.1", "text": "chat"}

    assert _router().route_message(event) is None


def test_top_level_channel_message_without_mention_is_ignored() -> None:
    event = {
        "user": "UJANE",
        "channel": "C1",
        "channel_type": "channel",
        "ts": "100.5",
        "text": "chat",
    }

    assert _router().route_message(event) is None


def test_channel_message_with_mention_is_left_to_the_app_mention_event() -> None:
    event = {
        "user": "UJANE",
        "channel": "C1",
        "ts": "100.5",
        "thread_ts": "100.1",
        "text": f"<@{BOT}> hi",
    }

    assert _router(known={KNOWN_THREAD}).route_message(event) is None


def test_ignores_strangers_bots_and_edits() -> None:
    router = _router(known={KNOWN_THREAD})
    stranger = {
        "user": "USTRANGER",
        "channel": "D1",
        "channel_type": "im",
        "ts": "1.1",
        "text": "hi",
    }
    bot = {
        "user": "UJANE",
        "bot_id": "B1",
        "channel": "D1",
        "channel_type": "im",
        "ts": "1.1",
        "text": "hi",
    }
    edit = {
        "user": "UJANE",
        "subtype": "message_changed",
        "channel": "D1",
        "channel_type": "im",
        "ts": "1.1",
        "text": "hi",
    }

    assert router.route_message(stranger) is None
    assert router.route_message(bot) is None
    assert router.route_message(edit) is None
    assert router.route_app_mention({**stranger, "channel": "C1"}) is None


def test_empty_mention_is_ignored() -> None:
    event = {"user": "UJANE", "channel": "C1", "ts": "200.1", "text": f"<@{BOT}> "}

    assert _router().route_app_mention(event) is None


def test_bang_commands_are_recognised_exactly() -> None:
    router = _router()
    event = {
        "user": "UJANE",
        "channel": "C1",
        "ts": "200.2",
        "thread_ts": "200.1",
        "text": f"<@{BOT}> !Status",
    }

    inbound = router.route_app_mention(event)

    assert inbound == CommandRequest(Command.STATUS, ThreadKey("C1", "200.1"), "200.2", "UJANE")
    not_a_command = router.route_app_mention({**event, "text": f"<@{BOT}> !status please"})
    assert isinstance(not_a_command, InboundMessage)
