"""Splitting long markdown answers into Slack-sized pieces without breaking code fences."""

from __future__ import annotations

SLACK_MARKDOWN_BLOCK_LIMIT = 12_000
FENCE = "```"


def split_markdown(text: str, limit: int = SLACK_MARKDOWN_BLOCK_LIMIT) -> list[str]:
    """Splits at line boundaries, keeping each piece under the limit and every code fence balanced.

    A fence that is open when a piece ends is closed there and reopened at the start of the next piece, so
    both halves still render as code.
    """
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []

    # Leave room to close a fence at the end of any piece.
    usable_limit = limit - len(FENCE) - 1
    pieces: list[str] = []
    current: list[str] = []
    current_length = 0
    is_in_fence = False

    for line in _lines_no_longer_than(text, usable_limit):
        separator_length = 1 if current else 0
        if current and current_length + separator_length + len(line) > usable_limit:
            pieces.append(_join(current, close_fence=is_in_fence))
            current = [FENCE] if is_in_fence else []
            current_length = len(FENCE) if is_in_fence else 0
            separator_length = 1 if current else 0
        current.append(line)
        current_length += separator_length + len(line)
        if line.strip().startswith(FENCE):
            is_in_fence = not is_in_fence

    if current:
        pieces.append(_join(current, close_fence=False))
    return [piece for piece in pieces if piece.strip()]


def _join(lines: list[str], close_fence: bool) -> str:
    return "\n".join([*lines, FENCE] if close_fence else lines)


def _lines_no_longer_than(text: str, limit: int) -> list[str]:
    lines: list[str] = []
    for line in text.split("\n"):
        while len(line) > limit:
            lines.append(line[:limit])
            line = line[limit:]
        lines.append(line)
    return lines
