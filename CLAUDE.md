# Claude Bot

Slack bot that lets an allowlist of people talk to Claude Code running on an always-on machine. Each Slack
thread is one Claude Code session running inside a leased checkout of the target repo. See `README.md` for
the full behaviour and `claude_bot/` for the code.

## Working here

- `uv run pytest` runs the tests. They never contact Slack or Anthropic: Slack is a recording fake and Claude
  Code is `tests/fake_claude.py`, a stand-in that emits recorded stream-json.
- `uv run ruff check . && uv run ruff format .` and `uv run ty check` before finishing.
- Engineering conventions: obvious names, named intermediate steps, thin handlers, comments only for the
  non-obvious, tests for every behaviour.
