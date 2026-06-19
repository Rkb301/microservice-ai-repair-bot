"""Restricted shell execution for user-provided test commands."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .safety import resolve_repo_path


@dataclass(frozen=True)
class CommandResult:
    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def combined_output(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part)


BLOCKED_TEST_COMMAND_TOKENS = {
    "git push",
    "git merge",
    "git reset",
    "git checkout",
    "rm -rf",
    "del /",
    "format ",
}


def run_test_command(
    repo_path: str | Path,
    command: str,
    *,
    timeout: int = 300,
) -> CommandResult:
    if not command or not command.strip():
        raise ValueError("A non-empty test command is required")
    lowered = command.lower()
    for blocked in BLOCKED_TEST_COMMAND_TOKENS:
        if blocked in lowered:
            raise ValueError(f"Refusing unsafe test command token: {blocked}")
    repo = resolve_repo_path(repo_path)
    completed = subprocess.run(
        command,
        cwd=repo,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
