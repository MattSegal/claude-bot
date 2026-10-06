import json

from claude_bot.stream import (
    AssistantText,
    PermissionDenied,
    ToolUse,
    TurnResult,
    TurnStatus,
    parse_stream_line,
    summarise_tool_use,
)


def test_ignores_noise_lines() -> None:
    assert parse_stream_line("") == []
    assert parse_stream_line("No conversation found with session ID: abc") == []
    assert parse_stream_line('{"type": "system", "subtype": "init"}') == []
    assert parse_stream_line("{not json") == []


def test_assistant_message_yields_tool_uses_and_text() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"description": "List files"}},
                    {"type": "text", "text": "Done."},
                    {"type": "thinking", "thinking": ""},
                ]
            },
        }
    )

    assert parse_stream_line(line) == [ToolUse("Bash", "List files"), AssistantText("Done.")]


def test_permission_denied_event() -> None:
    line = json.dumps(
        {
            "type": "system",
            "subtype": "permission_denied",
            "tool_name": "Bash",
            "message": "rm needs approval",
        }
    )

    assert parse_stream_line(line) == [PermissionDenied("Bash", "rm needs approval")]


def test_success_result() -> None:
    line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "The answer",
            "session_id": "sess",
            "duration_ms": 3240,
            "num_turns": 2,
            "permission_denials": [{"tool_name": "Bash"}],
        }
    )

    [result] = parse_stream_line(line)
    assert result == TurnResult(
        status=TurnStatus.COMPLETED,
        text="The answer",
        session_id="sess",
        denied_tools=("Bash",),
        duration_ms=3240,
        num_turns=2,
    )


def test_missing_session_result() -> None:
    line = json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "session_id": "sess",
            "errors": ["No conversation found with session ID: sess"],
        }
    )

    [result] = parse_stream_line(line)
    assert isinstance(result, TurnResult)
    assert result.status == TurnStatus.FAILED
    assert result.is_session_missing


def test_error_result_without_errors_list_uses_subtype() -> None:
    [result] = parse_stream_line(json.dumps({"type": "result", "subtype": "error_max_turns"}))

    assert isinstance(result, TurnResult)
    assert result.errors == ("error_max_turns",)


def test_tool_summaries() -> None:
    assert (
        summarise_tool_use("Bash", {"command": "ls -la", "description": "List files"})
        == "List files"
    )
    assert summarise_tool_use("Bash", {"command": "ls -la"}) == "ls -la"
    assert summarise_tool_use("Read", {"file_path": "/repo/a.py"}) == "/repo/a.py"
    assert summarise_tool_use("Grep", {"pattern": "def main"}) == "def main"
    assert summarise_tool_use("Skill", {"skill": "database"}) == "database"
    assert summarise_tool_use("mcp__sentry__search", {"query": "x"}) == ""
    long_command = "x" * 200
    assert len(summarise_tool_use("Bash", {"command": long_command})) == 80


def test_error_flagged_on_a_success_subtype_reports_the_result_text() -> None:
    line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
        }
    )

    [result] = parse_stream_line(line)

    assert isinstance(result, TurnResult)
    assert result.status == TurnStatus.FAILED
    assert result.errors == (
        "Failed to authenticate: OAuth session expired and could not be refreshed",
    )
