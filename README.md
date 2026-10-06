# Claude Bot

A Slack bot that runs Claude Code on an always-on machine so a team can ask questions about their codebase,
prod data, logs and errors from Slack, and get answers that use the repo's own skills, rules and scripts.

## How it behaves

- **Start a thread** by @mentioning the bot in a channel or DMing it. The bot reacts with 👀 straight away.
- **Follow up in the thread without mentioning it.** Every Slack thread is one Claude Code session, so
  follow-ups keep their context. Anyone on the allowlist can join in; each message is prefixed with the
  sender's name so Claude knows who is asking.
- **One progress message per turn**, edited in place with the last few tool calls (`Bash: query prod`,
  `Read: models.py`, `🚫 Bash blocked: …`). Edits never notify anyone. The answer arrives as a new message,
  the 👀 becomes ✅ (or ❌ / ⏹), and the progress line is replaced with `✅ Done · 7 tool calls, 2m 14s`.
- **Answers are markdown.** Headings, tables and code blocks render. Answers too long for Slack are
  attached as `answer.md`. Files Claude saves into the output directory with the thread's prefix are
  uploaded too.
- **Commands**, sent as the whole message (in a thread, DM, or after an @mention):
  `!status` (private reply), `!stop` (also drops anything queued in the thread), `!restart`,
  `!release` (give the thread's checkout back now), `!help`.
- **Permissions are deny-by-default.** Claude runs unattended in Claude Code's default permission mode, so
  only tools and commands on the repo's allowlist run. Anything else is denied, shown in the progress
  message, and summarised in a footer under the answer. The system prompt tells Claude to report what it
  could not do rather than work around it.

## Workspaces

Conversations run in real checkouts of the repo, one per thread at a time. Configure several (each with its
own dev-env slot) so two conversations can run tests or local servers side by side.

- A thread **leases** a checkout on its first message and keeps it while active, plus `LEASE_TTL_MINUTES`
  of idle time, so follow-ups land in the same place.
- When every checkout is leased, a new thread waits (it is told so) and takes the longest-idle checkout
  as soon as that thread has been quiet for a couple of minutes. Claude Code sessions resume from any
  directory, so a thread that moves checkout keeps its memory.
- A checkout is only reclaimed **clean**: no uncommitted changes, on the default branch. Otherwise the lease
  is marked **held**, the thread that dirtied it is told, and nobody else gets it until a person cleans it
  up. The bot re-checks held checkouts every minute, so cleaning up releases them with no further action.
- If a session file has gone missing (reinstall, cleanup), the thread's history is fetched from Slack and a
  replacement session is started with it.

## Setup

### Slack app

1. [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From scratch.
2. **Socket Mode** → enable, create an app-level token with `connections:write` → `SLACK_APP_TOKEN`.
3. **OAuth & Permissions** → Bot Token Scopes:
   `app_mentions:read`, `channels:history`, `groups:history`, `im:history`, `im:read`, `im:write`,
   `chat:write`, `files:write`, `reactions:read`, `reactions:write`, `users:read`.
4. **Event Subscriptions** → enable → bot events: `app_mention`, `message.im`, `message.channels`,
   `message.groups`. The last two are what let the bot follow a thread without being re-mentioned.
5. **Install to Workspace** → Bot User OAuth Token → `SLACK_BOT_TOKEN`.
6. Each person's member ID (Profile → ⋯ → Copy member ID) → `ALLOWED_USER_IDS`.

Invite the bot to any channel you want to use it in.

### Host machine

The service script targets macOS (launchd). Requirements: `uv`, and `claude` logged in to the subscription
account (`claude auth status`). Each workspace in `WORKSPACES` must be a git checkout on the default branch.

```bash
cp .env.example .env            # fill in tokens, user IDs and workspaces
uv sync
uv run claude-bot               # run in the foreground to check it connects
./scripts/service.sh install    # install and start the launchd service
```

`./scripts/service.sh status|start|stop|restart|uninstall` manage the service. Bot logs are in
`~/.claude-bot/logs/bot.log`; launchd's own stdout/stderr land in `logs/`. After changing `.env`,
run `uninstall` then `install` so the plist picks up the new values.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format .
uv run ty check
```

The tests never touch Slack or Anthropic: Slack is a recording fake and Claude Code is
`tests/fake_claude.py`, which speaks enough stream-json to exercise every flow.

Modules, top down: `main.py` wires everything and serves Socket Mode; `slack_events.py` decides which
events are for the bot; `conversation.py` runs each thread as a serial worker and owns the turn lifecycle;
`workspaces.py` leases checkouts; `runner.py` and `stream.py` run `claude -p` and parse its output;
`presenter.py` is everything shown in Slack; `store.py` is the SQLite state.
