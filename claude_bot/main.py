"""Entry point: wires settings, storage, the Claude runner and Slack together, then serves Socket Mode."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from claude_bot.config import Settings
from claude_bot.conversation import Conversations
from claude_bot.presenter import SlackPresenter
from claude_bot.runner import ClaudeRunner
from claude_bot.slack_events import CommandRequest, EventRouter, InboundMessage
from claude_bot.store import StateStore
from claude_bot.workspaces import GitInspector, WorkspacePool

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 60.0
SEEN_MESSAGE_LIMIT = 500


def main() -> None:
    load_dotenv()
    settings = Settings.from_env()
    _configure_logging(settings)
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    app = App(token=settings.slack_bot_token)
    bot_user_id = app.client.auth_test()["user_id"]

    store = StateStore(settings.db_path)
    pool = WorkspacePool(
        workspaces=settings.workspaces,
        store=store,
        inspector=GitInspector(settings.default_branch),
        clock=time.time,
        lease_ttl=settings.lease_ttl,
    )
    conversations = Conversations(
        store=store,
        pool=pool,
        runner=ClaudeRunner(settings.claude_bin, settings.turn_timeout),
        presenter=SlackPresenter(app.client, bot_user_id, settings.allowed_user_ids),
        output_dir=settings.output_dir,
        default_branch=settings.default_branch,
        workspace_wait_timeout=settings.workspace_wait_timeout,
    )
    router = EventRouter(bot_user_id, settings.allowed_user_ids, conversations.is_known_thread)
    register_handlers(app, router, conversations)
    _start_sweeper(conversations)

    logger.info(
        "Claude bot starting as %s with %d workspace(s) for %d user(s)",
        bot_user_id,
        len(settings.workspaces),
        len(settings.allowed_user_ids),
    )
    SocketModeHandler(app, settings.slack_app_token).start()


def register_handlers(app: App, router: EventRouter, conversations: Conversations) -> None:
    # A message can reach us twice: Slack redelivers events it thinks were not acknowledged, and a DM that
    # mentions the bot arrives as both a message and an app_mention event. Keying on the message itself
    # covers both.
    seen_messages = _RecentKeys(SEEN_MESSAGE_LIMIT)

    def dispatch(inbound: InboundMessage | CommandRequest | None) -> None:
        if inbound is None:
            return
        if seen_messages.is_duplicate(f"{inbound.thread.channel}:{inbound.message_ts}"):
            return
        if isinstance(inbound, InboundMessage):
            conversations.submit(inbound)
        else:
            conversations.handle_command(inbound)

    @app.event("app_mention")
    def on_app_mention(event: dict[str, Any]) -> None:
        dispatch(router.route_app_mention(event))

    @app.event("message")
    def on_message(event: dict[str, Any]) -> None:
        dispatch(router.route_message(event))


class _RecentKeys:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def is_duplicate(self, key: str) -> bool:
        with self._lock:
            if key in self._seen:
                return True
            self._seen[key] = None
            while len(self._seen) > self._limit:
                self._seen.popitem(last=False)
            return False


def _start_sweeper(conversations: Conversations) -> None:
    def loop() -> None:
        while True:
            time.sleep(SWEEP_INTERVAL_SECONDS)
            try:
                conversations.sweep()
            except Exception:
                logger.exception("Workspace sweep failed")

    threading.Thread(target=loop, name="workspace-sweeper", daemon=True).start()


def _configure_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(settings.log_dir / "bot.log"),
        ],
    )


if __name__ == "__main__":
    main()
