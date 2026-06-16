"""Shared filesystem paths for Free Claude Code configuration."""

import contextlib
import os
from pathlib import Path

FCC_CONFIG_DIRNAME = ".fcc"
FCC_ENV_FILENAME = ".env"
LEGACY_REPO_DIRNAME = "free-claude-code"
LEGACY_XDG_CONFIG_DIRNAME = ".config"
CLAUDE_WORKSPACE_DIRNAME = "agent_workspace"
FCC_LOGS_DIRNAME = "logs"
SERVER_LOG_FILENAME = "server.log"


def config_dir_path() -> Path:
    """Return the default user config directory."""

    return Path.home() / FCC_CONFIG_DIRNAME


def managed_env_path() -> Path:
    """Return the default user-managed env file path, respecting ACTIVE_PROFILE."""
    active_profile = ""
    profile_file = config_dir_path() / "active_profile.txt"
    if profile_file.is_file():
        with contextlib.suppress(Exception):
            active_profile = profile_file.read_text(encoding="utf-8").strip()
    if not active_profile:
        active_profile = os.environ.get("ACTIVE_PROFILE", "").strip()
    if active_profile and active_profile != "default" and active_profile != "undefined":
        return config_dir_path() / "profiles" / f"{active_profile}.env"
    return config_dir_path() / FCC_ENV_FILENAME


def legacy_env_paths() -> tuple[Path, ...]:
    """Return legacy user env paths that can be migrated to ~/.fcc/.env."""

    home = Path.home()
    return (
        home / LEGACY_REPO_DIRNAME / FCC_ENV_FILENAME,
        home / LEGACY_XDG_CONFIG_DIRNAME / LEGACY_REPO_DIRNAME / FCC_ENV_FILENAME,
    )


def default_claude_workspace_path() -> Path:
    """Return the default Claude workspace path."""

    return config_dir_path() / CLAUDE_WORKSPACE_DIRNAME


def server_log_path() -> Path:
    """Return the canonical server log path."""

    return config_dir_path() / FCC_LOGS_DIRNAME / SERVER_LOG_FILENAME
