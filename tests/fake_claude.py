#!/usr/bin/env python3
"""A stand-in for the `claude` CLI that speaks just enough stream-json for the bot.

Behaviour is driven by keywords in the prompt (read from stdin, like the real CLI):
    TOOL        emit one Bash tool call before answering
    DENY        emit a permission denial and report it in the result
    FAIL        fail the turn with an error result and exit 1
    SLEEP:<s>   sleep for <s> seconds before answering (for cancel and timeout tests)
    DIRTY       create an untracked file in the working directory
    OUTPUT:<p>  write a file at path <p> (for upload tests)
    LONG        answer with a very long text
Every invocation is appended to calls.jsonl in FAKE_CLAUDE_STATE_DIR so tests can inspect the arguments.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path


def main() -> None:
    args = sys.argv[1:]
    state_dir = Path(os.environ["FAKE_CLAUDE_STATE_DIR"])
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    prompt = sys.stdin.read()

    resume_id = _option(args, "--resume")
    session_id = resume_id or _option(args, "--session-id") or "generated-session"
    with (state_dir / "calls.jsonl").open("a") as calls:
        calls.write(
            json.dumps(
                {
                    "argv": args,
                    "prompt": prompt,
                    "cwd": os.getcwd(),
                    "system_prompt": _option(args, "--append-system-prompt"),
                }
            )
            + "\n"
        )

    if resume_id and not (sessions_dir / resume_id).exists():
        error = f"No conversation found with session ID: {resume_id}"
        print(error, file=sys.stderr)
        _emit(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "session_id": resume_id,
                "errors": [error],
            }
        )
        sys.exit(1)
    (sessions_dir / session_id).write_text(prompt)

    _emit({"type": "system", "subtype": "init", "session_id": session_id})

    sleep_match = re.search(r"SLEEP:(\d+(?:\.\d+)?)", prompt)
    if sleep_match:
        _emit_tool_use("Bash", {"command": "sleep", "description": "Sleeping"})
        time.sleep(float(sleep_match.group(1)))

    if "DIRTY" in prompt:
        Path("scratch.txt").write_text("left behind\n")

    output_match = re.search(r"OUTPUT:(\S+)", prompt)
    if output_match:
        Path(output_match.group(1)).write_text("chart bytes\n")

    if "TOOL" in prompt:
        _emit_tool_use("Bash", {"command": "ls", "description": "List files"})
        _emit(
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "tool_result", "content": "a b"}]},
            }
        )

    denials = []
    if "DENY" in prompt:
        _emit_tool_use("Bash", {"command": "rm -rf build", "description": "Remove build dir"})
        _emit(
            {
                "type": "system",
                "subtype": "permission_denied",
                "tool_name": "Bash",
                "message": "rm needs approval",
            }
        )
        denials.append({"tool_name": "Bash", "tool_input": {"command": "rm -rf build"}})

    if "FAIL" in prompt:
        _emit(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "session_id": session_id,
                "errors": ["boom"],
            }
        )
        sys.exit(1)

    answer = ("x" * 30_000) if "LONG" in prompt else f"echo: {prompt}"
    _emit(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]},
        }
    )
    _emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": session_id,
            "result": answer,
            "duration_ms": 1234,
            "num_turns": 2,
            "permission_denials": denials,
        }
    )


def _emit_tool_use(name: str, tool_input: dict) -> None:
    _emit(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": name, "input": tool_input}],
            },
        }
    )


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _option(args: list[str], name: str) -> str | None:
    if name not in args:
        return None
    return args[args.index(name) + 1]


if __name__ == "__main__":
    main()
