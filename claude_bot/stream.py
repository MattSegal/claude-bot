"""Parsing of Claude Code's `--output-format stream-json` lines into the few events the bot cares about."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TurnStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class ToolUse:
    name: str
    summary: str


@dataclass(frozen=True)
class AssistantText:
    text: str


@dataclass(frozen=True)
class PermissionDenied:
    tool_name: str
    message: str


@dataclass(frozen=True)
class TurnResult:
    status: TurnStatus
    text: str = ""
    session_id: str | None = None
    errors: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    duration_ms: int = 0
    num_turns: int = 0

    @property
    def is_session_missing(self) -> bool:
        return any("No conversation found with session ID" in error for error in self.errors)


StreamEvent = ToolUse | AssistantText | PermissionDenied | TurnResult


def parse_stream_line(line: str) -> list[StreamEvent]:
    line = line.strip()
    if not line.startswith("{"):
        return []
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return []

    kind = payload.get("type")
    if kind == "assistant":
        return _parse_assistant(payload)
    if kind == "system" and payload.get("subtype") == "permission_denied":
        return [PermissionDenied(payload.get("tool_name", "?"), payload.get("message", ""))]
    if kind == "result":
        return [_parse_result(payload)]
    return []


def summarise_tool_use(name: str, tool_input: dict[str, Any]) -> str:
    """One short line describing a tool call, for the progress message."""
    if name == "Bash":
        description = tool_input.get("description") or tool_input.get("command") or ""
        return _truncate(description)
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        return _truncate(str(tool_input.get("file_path") or tool_input.get("notebook_path") or ""))
    if name in ("Grep", "Glob"):
        return _truncate(str(tool_input.get("pattern", "")))
    if name == "Skill":
        return _truncate(str(tool_input.get("skill", "")))
    if name in ("Task", "Agent"):
        return _truncate(str(tool_input.get("description", "")))
    if name in ("WebFetch", "WebSearch"):
        return _truncate(str(tool_input.get("url") or tool_input.get("query") or ""))
    return ""


def _parse_assistant(payload: dict[str, Any]) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    for block in payload.get("message", {}).get("content", []):
        block_type = block.get("type")
        if block_type == "tool_use":
            name = block.get("name", "?")
            events.append(ToolUse(name, summarise_tool_use(name, block.get("input") or {})))
        elif block_type == "text" and block.get("text"):
            events.append(AssistantText(block["text"]))
    return events


def _parse_result(payload: dict[str, Any]) -> TurnResult:
    is_error = bool(payload.get("is_error")) or payload.get("subtype") != "success"
    errors = tuple(str(error) for error in payload.get("errors") or [])
    if is_error and not errors:
        # Some failures, such as an expired login, arrive with subtype "success" and the reason in `result`.
        reason = payload.get("result") or payload.get("subtype") or "unknown error"
        errors = (str(reason),)
    denied_tools = tuple(
        str(denial.get("tool_name", "?")) for denial in payload.get("permission_denials") or []
    )
    return TurnResult(
        status=TurnStatus.FAILED if is_error else TurnStatus.COMPLETED,
        text=str(payload.get("result") or ""),
        session_id=payload.get("session_id"),
        errors=errors,
        denied_tools=denied_tools,
        duration_ms=int(payload.get("duration_ms") or 0),
        num_turns=int(payload.get("num_turns") or 0),
    )


def _truncate(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
