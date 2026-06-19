"""Public GitHub safety scanner."""

from __future__ import annotations

import re
import stat
import os
from dataclasses import dataclass
from pathlib import Path


SECRET_PATTERNS = [
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(r"-{5}BEGIN [A-Z ]*PRIVATE KEY-{5}")),
    ("password_assignment", re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*['\"]?[^'\"\s]{8,}")),
    (
        "api_token_assignment",
        re.compile(
            r"(?i)\b(jira_api_token|github_token|openai_api_key|google_api_key)[ \t]*[:=][ \t]*['\"]?[A-Za-z0-9_./+=-]{12,}"
        ),
    ),
]

SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "pytest_temp",
    ".tmp",
    ".repairbot",
    ".workspace",
    "artifacts",
    "uploads",
    "runs",
    "target",
    "build",
    ".gradle",
    ".idea",
    ".vscode",
}
MAX_LOG_BYTES = 250_000


@dataclass(frozen=True)
class SafetyFinding:
    path: str
    issue_type: str
    message: str


def scan_path(path: str | Path) -> list[SafetyFinding]:
    root = Path(path).resolve()
    findings: list[SafetyFinding] = []
    for file_path in _iter_files(root):
        rel = file_path.relative_to(root).as_posix()
        if file_path.name.startswith(".env") and file_path.name != ".env.example":
            findings.append(SafetyFinding(rel, "env_file", ".env files must not be committed"))
            continue
        if file_path.suffix == ".log" and file_path.stat().st_size > MAX_LOG_BYTES:
            findings.append(SafetyFinding(rel, "large_log", "large log files should stay out of git"))
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for issue_type, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(SafetyFinding(rel, issue_type, f"possible {issue_type} detected"))
    return findings


def install_pre_commit_hook(repo_path: str | Path = ".") -> Path:
    repo = Path(repo_path).resolve()
    hooks = repo / ".git" / "hooks"
    if not hooks.exists():
        raise RuntimeError(f"No .git/hooks directory found under {repo}")
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!/bin/sh\npython -m repairbot.cli safety-check --path .\n",
        encoding="utf-8",
    )
    try:
        mode = hook.stat().st_mode
        hook.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    return hook


def _iter_files(root: Path):
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        current_path = Path(current)
        for file_name in files:
            path = current_path / file_name
            if path.is_file():
                yield path
