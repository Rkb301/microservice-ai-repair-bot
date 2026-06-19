"""Safety checks for local repository operations."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path


class SafetyError(RuntimeError):
    """Raised when a requested action violates repairbot safety rules."""


PROTECTED_BRANCHES = {"main", "master", "develop"}

SENSITIVE_PATTERNS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.crt",
    "id_rsa*",
    "secrets.*",
    "credentials.*",
]

SECRET_NAME_RE = re.compile(r"(secret|credential|password|token)", re.IGNORECASE)

DEPLOY_PATTERNS = [
    ".github/workflows/*",
    "Dockerfile",
    "Dockerfile.*",
    "docker-compose*",
    "k8s/*",
    "k8s/**/*",
    "helm/*",
    "helm/**/*",
    "terraform/*",
    "terraform/**/*",
    "deployment/*",
    "deployment/**/*",
]


def resolve_repo_path(repo_path: str | Path) -> Path:
    repo = Path(repo_path).expanduser().resolve()
    if not repo.exists():
        raise SafetyError(f"Repository path does not exist: {repo}")
    if not repo.is_dir():
        raise SafetyError(f"Repository path is not a directory: {repo}")
    return repo


def resolve_inside_repo(repo_path: str | Path, target_path: str | Path) -> Path:
    repo = resolve_repo_path(repo_path)
    target = Path(target_path)
    if not target.is_absolute():
        target = repo / target
    resolved = target.expanduser().resolve()
    try:
        resolved.relative_to(repo)
    except ValueError as exc:
        raise SafetyError(f"Path escapes repository: {target_path}") from exc
    return resolved


def relative_posix(repo_path: str | Path, target_path: str | Path) -> str:
    repo = resolve_repo_path(repo_path)
    resolved = resolve_inside_repo(repo, target_path)
    return resolved.relative_to(repo).as_posix()


def assert_write_allowed(
    repo_path: str | Path,
    target_path: str | Path,
    *,
    allow_sensitive_edits: bool = False,
    allow_deploy_edits: bool = False,
) -> Path:
    resolved = resolve_inside_repo(repo_path, target_path)
    rel = relative_posix(repo_path, resolved)

    if not allow_sensitive_edits and _matches_sensitive(rel):
        raise SafetyError(f"Refusing to edit sensitive file: {rel}")
    if not allow_deploy_edits and _matches_deploy(rel):
        raise SafetyError(f"Refusing to edit deployment/CI file: {rel}")
    return resolved


def assert_safe_branch_name(
    branch_name: str,
    protected_branches: set[str] | None = None,
) -> None:
    protected = protected_branches or PROTECTED_BRANCHES
    if branch_name in protected:
        raise SafetyError(f"Refusing protected branch operation: {branch_name}")
    if not (branch_name.startswith("ai-fix/") or branch_name.startswith("bugfix/")):
        raise SafetyError("Repair branches must start with 'ai-fix/' or 'bugfix/'")


def assert_not_protected_branch(
    branch_name: str,
    protected_branches: set[str] | None = None,
) -> None:
    protected = protected_branches or PROTECTED_BRANCHES
    if branch_name in protected:
        raise SafetyError(f"Refusing operation on protected branch: {branch_name}")


def assert_small_diff(diff_text: str, *, max_changed_lines: int = 200) -> None:
    changed = [
        line
        for line in diff_text.splitlines()
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith("+++")
        and not line.startswith("---")
    ]
    if len(changed) > max_changed_lines:
        raise SafetyError(
            f"Diff is too large for the default safety limit: {len(changed)} lines"
        )


def _matches_sensitive(rel_path: str) -> bool:
    name = Path(rel_path).name
    if any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_PATTERNS):
        return True
    return bool(SECRET_NAME_RE.search(rel_path))


def _matches_deploy(rel_path: str) -> bool:
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in DEPLOY_PATTERNS)
