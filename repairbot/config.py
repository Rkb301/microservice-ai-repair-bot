"""Runtime configuration for repairbot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import dotenv_values
except ImportError:  # pragma: no cover - exercised only in broken installs
    dotenv_values = None


DEFAULT_MODEL = "openai:gpt-5-mini"
DEFAULT_MAX_RETRIES = 1
DEFAULT_MAX_HYPOTHESES = 5
_DOTENV_LOADED_VALUES: dict[str, str] = {}


@dataclass(frozen=True)
class RepairBotSettings:
    """Small env-backed settings object.

    Provider packages and API keys are only required when the AI agent is
    actually invoked.
    """

    model: str = DEFAULT_MODEL
    provider: str = "openai"
    max_retries: int = DEFAULT_MAX_RETRIES
    max_hypotheses: int = DEFAULT_MAX_HYPOTHESES
    top_hypotheses: int = 3
    max_files_to_read: int = 30
    max_dependency_depth: int = 3
    max_fix_attempts: int = 2
    max_test_runs: int = 3
    max_log_examples_per_cluster: int = 5
    langsmith_tracing: bool = False
    langsmith_project: str = "microservice-ai-repair-bot"
    github_token: str | None = None
    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: str | None = None
    jira_project_key: str | None = None
    protected_branches: set[str] = field(
        default_factory=lambda: {"main", "master", "develop"}
    )

    @classmethod
    def from_env(cls, dotenv_path: str | Path | None = ".env") -> "RepairBotSettings":
        if dotenv_path is not None:
            load_local_env(dotenv_path)
        return cls(
            model=os.getenv("REPAIRBOT_MODEL", DEFAULT_MODEL),
            provider=os.getenv("REPAIRBOT_PROVIDER", "openai"),
            max_retries=_int_env("REPAIRBOT_MAX_RETRIES", DEFAULT_MAX_RETRIES),
            max_hypotheses=_int_env("REPAIRBOT_MAX_HYPOTHESES", DEFAULT_MAX_HYPOTHESES),
            top_hypotheses=_int_env("REPAIRBOT_TOP_HYPOTHESES", 3),
            max_files_to_read=_int_env("REPAIRBOT_MAX_FILES_TO_READ", 30),
            max_dependency_depth=_int_env("REPAIRBOT_MAX_DEPENDENCY_DEPTH", 3),
            max_fix_attempts=_int_env("REPAIRBOT_MAX_FIX_ATTEMPTS", 2),
            max_test_runs=_int_env("REPAIRBOT_MAX_TEST_RUNS", 3),
            max_log_examples_per_cluster=_int_env(
                "REPAIRBOT_MAX_LOG_EXAMPLES_PER_CLUSTER", 5
            ),
            langsmith_tracing=os.getenv("LANGSMITH_TRACING", "false").lower()
            in {"1", "true", "yes", "on"},
            langsmith_project=os.getenv(
                "LANGSMITH_PROJECT", "microservice-ai-repair-bot"
            ),
            github_token=os.getenv("GITHUB_TOKEN") or None,
            jira_base_url=os.getenv("JIRA_BASE_URL") or None,
            jira_email=os.getenv("JIRA_EMAIL") or None,
            jira_api_token=os.getenv("JIRA_API_TOKEN") or None,
            jira_project_key=os.getenv("JIRA_PROJECT_KEY") or None,
        )


def load_local_env(dotenv_path: str | Path = ".env") -> bool:
    """Load a local .env file when present without overriding shell variables."""

    if dotenv_values is None:
        raise RuntimeError(
            "python-dotenv is required for .env support. Install project dependencies with `python -m pip install -e .`."
        )
    path = Path(dotenv_path)
    if not path.exists():
        return False
    values = dotenv_values(path)
    loaded = False
    for key, value in values.items():
        if value is None:
            continue
        previous_dotenv_value = _DOTENV_LOADED_VALUES.get(key)
        if key in os.environ and os.environ[key] != previous_dotenv_value:
            continue
        os.environ[key] = value
        _DOTENV_LOADED_VALUES[key] = value
        loaded = True
    return loaded


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default
