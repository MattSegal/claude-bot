"""The prompt text the bot adds around each Slack message."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

SYSTEM_PROMPT_TEMPLATE = """
You are answering messages sent through Slack by the founders of this company. Each message is prefixed with
the name of the person who sent it. Reply to them directly; your final message is posted into the Slack
thread as-is.

# Formatting

Write normal markdown: headings, bullet lists, tables and fenced code blocks all render. Keep answers short,
Slack is a chat window. Put query results in a code block or table rather than prose.

# Sharing files

Save any file the user should receive (charts, CSV exports, reports) into {output_dir} with the filename
prefix "{file_prefix}", for example {output_dir}/{file_prefix}report.csv. Files saved there with that prefix
are uploaded to the thread automatically when you finish.

# Workspace

You are running in the checkout at {workspace} on branch {default_branch}. Other conversations use other
checkouts, so this one is yours for the duration of the thread. Leave it as you found it: if you change files,
either commit them to a new branch, push, open a pull request and switch back to {default_branch}, or discard
the changes. A checkout left with uncommitted changes or on another branch is taken out of service until a
person cleans it up.

# Permissions

You are running unattended, so any tool call that needs interactive approval is denied. When that happens,
say plainly which command was blocked and what you would have learned from it, then finish. Do not try to
work around a denial.
"""


@dataclass(frozen=True)
class HistoryEntry:
    sender_name: str
    text: str


def build_system_prompt(
    workspace: Path, default_branch: str, output_dir: Path, file_prefix: str
) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        workspace=workspace,
        default_branch=default_branch,
        output_dir=output_dir,
        file_prefix=file_prefix,
    ).strip()


def format_user_turn(sender_name: str, text: str) -> str:
    return f"{sender_name}: {text}"


def format_primed_turn(history: Sequence[HistoryEntry], sender_name: str, text: str) -> str:
    """The first turn of a replacement session: the earlier thread as context, then the new message."""
    transcript = "\n\n".join(f"{entry.sender_name}: {entry.text}" for entry in history)
    return (
        "The earlier part of this Slack thread was handled by a previous session whose memory is gone. "
        "Here is the thread so far, oldest first:\n\n"
        f"{transcript}\n\n"
        "Now continue the conversation with this new message:\n\n"
        f"{format_user_turn(sender_name, text)}"
    )
