from datetime import timedelta
from pathlib import Path

import pytest

from claude_bot.config import ConfigError, Settings
from tests.conftest import make_git_repo


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    repo = make_git_repo(tmp_path / "repo")
    env = {
        "SLACK_BOT_TOKEN": "xoxb-1",
        "SLACK_APP_TOKEN": "xapp-1",
        "ALLOWED_USER_IDS": "UOMAR, UJANE",
        "WORKSPACES": str(repo),
    }
    env.update(overrides)
    return env


def test_reads_lists_and_defaults(tmp_path: Path) -> None:
    settings = Settings.from_env(_env(tmp_path))

    assert settings.allowed_user_ids == frozenset({"UOMAR", "UJANE"})
    assert settings.workspaces == (tmp_path / "repo",)
    assert settings.lease_ttl == timedelta(minutes=30)
    assert settings.default_branch == "main"
    assert settings.db_path == Path("~/.claude-bot").expanduser() / "state.sqlite3"


def test_multiple_workspaces_are_colon_separated(tmp_path: Path) -> None:
    second = make_git_repo(tmp_path / "repo-1")
    env = _env(tmp_path)
    env["WORKSPACES"] = f"{tmp_path / 'repo'}:{second}"

    settings = Settings.from_env(env)

    assert settings.workspaces == (tmp_path / "repo", second)


def test_requires_at_least_one_user(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="ALLOWED_USER_IDS"):
        Settings.from_env(_env(tmp_path, ALLOWED_USER_IDS=" "))


def test_requires_at_least_one_workspace(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="WORKSPACES"):
        Settings.from_env(_env(tmp_path, WORKSPACES=" "))


def test_rejects_workspace_that_is_not_a_checkout(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not a git checkout"):
        Settings.from_env(_env(tmp_path, WORKSPACES=str(tmp_path)))


def test_rejects_non_integer_minutes(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="LEASE_TTL_MINUTES"):
        Settings.from_env(_env(tmp_path, LEASE_TTL_MINUTES="soon"))


def test_log_level_defaults_to_info_and_is_validated(tmp_path: Path) -> None:
    env = _env(tmp_path)

    assert Settings.from_env(env).log_level == "INFO"
    assert Settings.from_env({**env, "LOG_LEVEL": "debug"}).log_level == "DEBUG"
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Settings.from_env({**env, "LOG_LEVEL": "loud"})
