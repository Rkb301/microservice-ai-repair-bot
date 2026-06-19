"""Git operations wrapped in deterministic safety checks."""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .safety import (
    SafetyError,
    assert_not_protected_branch,
    assert_safe_branch_name,
    resolve_repo_path,
)


class GitError(RuntimeError):
    """Raised when a git command fails."""


def run_git(repo_path: str | Path, args: list[str], *, timeout: int = 120) -> str:
    repo = resolve_repo_path(repo_path)
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise GitError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout.strip()


def ensure_git_repo(repo_path: str | Path) -> Path:
    repo = resolve_repo_path(repo_path)
    try:
        root = run_git(repo, ["rev-parse", "--show-toplevel"])
    except GitError as exc:
        raise GitError(f"Not a git repository: {repo}") from exc
    root_path = Path(root).resolve()
    if root_path != repo:
        raise GitError(f"Pass the git repository root explicitly: {root_path}")
    return root_path


def current_branch(repo_path: str | Path) -> str:
    branch = run_git(repo_path, ["branch", "--show-current"])
    if not branch:
        raise GitError("Detached HEAD is not supported")
    return branch


def status_porcelain(repo_path: str | Path) -> str:
    repo = resolve_repo_path(repo_path)
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise GitError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout.rstrip()


def ensure_clean_worktree(repo_path: str | Path, *, allow_dirty: bool = False) -> None:
    status = status_porcelain(repo_path)
    if status and not allow_dirty:
        raise SafetyError("Repository has uncommitted changes; pass --allow-dirty to continue")


def diff(repo_path: str | Path) -> str:
    return run_git(repo_path, ["diff", "--"])


def diff_stat(repo_path: str | Path) -> str:
    return run_git(repo_path, ["diff", "--stat", "--"])


def diff_patch(repo_path: str | Path) -> str:
    return run_git(repo_path, ["diff", "--binary", "--"])


def changed_files(repo_path: str | Path) -> list[str]:
    output = status_porcelain(repo_path)
    files: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        if line.startswith("?? "):
            path = line[3:].strip()
        else:
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
        if path:
            files.append(path)
    return files


def restore_worktree(repo_path: str | Path) -> None:
    run_git(repo_path, ["restore", "--staged", "--worktree", "--", "."])
    run_git(repo_path, ["clean", "-fd", "--", "."])


def make_branch_name(service_name: str) -> str:
    service = re.sub(r"[^A-Za-z0-9._-]+", "-", service_name.strip()).strip("-._")
    service = service.lower() or "service"
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"ai-fix/{service}/{stamp}"


def make_bugfix_branch_name(jira_key: str) -> str:
    key = re.sub(r"[^A-Za-z0-9._-]+", "-", jira_key.strip()).strip("-._").upper()
    if not key:
        raise GitError("Jira key is required for bugfix branch creation")
    return f"bugfix/{key}"


def checkout_base_branch(repo_path: str | Path, base_branch: str) -> None:
    assert_not_protected_branch("not-a-target-branch")
    run_git(repo_path, ["switch", base_branch])


def create_branch(repo_path: str | Path, branch_name: str) -> None:
    assert_safe_branch_name(branch_name)
    run_git(repo_path, ["switch", "-c", branch_name])


def commit_changes(repo_path: str | Path, files: list[str], message: str) -> str:
    if not files:
        raise GitError("No changed files to commit")
    run_git(repo_path, ["add", "--", *files])
    run_git(repo_path, ["commit", "-m", message])
    return run_git(repo_path, ["rev-parse", "HEAD"])


def push_branch(repo_path: str | Path, branch_name: str) -> None:
    assert_safe_branch_name(branch_name)
    assert_not_protected_branch(branch_name)
    run_git(repo_path, ["push", "-u", "origin", branch_name], timeout=300)
