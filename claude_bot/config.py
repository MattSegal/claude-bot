"""Settings loaded from the environment (or a `.env` file via python-dotenv)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    slack_bot_token: str
    slack_app_token: str
    allowed_user_ids: frozenset[str]
    # Checkouts of the repo that conversations lease one at a time. Each needs its own dev-env slot
    # so two conversations can run tests or local servers side by side.
    workspaces: tuple[Path, ...]
    state_dir: Path
    claude_bin: str
    default_branch: str
    lease_ttl: timedelta
    turn_timeout: timedelta
    workspace_wait_timeout: timedelta
    # DEBUG makes slack_bolt log every Socket Mode envelope, including the reason Slack gives when it asks
    # the bot to disconnect, which INFO hides.
    log_level: str

    @property
    def db_path(self) -> Path:
        return self.state_dir / "state.sqlite3"

    @property
    def output_dir(self) -> Path:
        return self.state_dir / "output"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        allowed_user_ids = frozenset(_split_list(env.get("ALLOWED_USER_IDS", ""), ","))
        if not allowed_user_ids:
            raise ConfigError("ALLOWED_USER_IDS must list at least one Slack user ID")

        workspace_paths = _split_list(env.get("WORKSPACES", ""), ":")
        if not workspace_paths:
            raise ConfigError("WORKSPACES must list at least one repo checkout")
        workspaces = tuple(Path(path).expanduser().resolve() for path in workspace_paths)
        for workspace in workspaces:
            if not (workspace / ".git").exists():
                raise ConfigError(f"Workspace {workspace} is not a git checkout")

        return cls(
            slack_bot_token=_require(env, "SLACK_BOT_TOKEN"),
            slack_app_token=_require(env, "SLACK_APP_TOKEN"),
            allowed_user_ids=allowed_user_ids,
            workspaces=workspaces,
            state_dir=Path(env.get("STATE_DIR", "~/.claude-bot")).expanduser(),
            claude_bin=env.get("CLAUDE_BIN", "claude"),
            default_branch=env.get("DEFAULT_BRANCH", "main"),
            lease_ttl=timedelta(minutes=_int(env, "LEASE_TTL_MINUTES", 30)),
            turn_timeout=timedelta(minutes=_int(env, "TURN_TIMEOUT_MINUTES", 30)),
            workspace_wait_timeout=timedelta(minutes=_int(env, "WORKSPACE_WAIT_MINUTES", 30)),
            log_level=_log_level(env),
        )


def _require(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"{key} is required")
    return value


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _log_level(env: Mapping[str, str]) -> str:
    level = env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError(f"LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR, got {level!r}")
    return level


def _split_list(raw: str, separator: str) -> list[str]:
    return [item.strip() for item in raw.split(separator) if item.strip()]
