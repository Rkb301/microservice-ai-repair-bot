"""GitHub clone, branch, commit, push, and pull-request adapters."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx

from ..redaction import redact_sensitive_text
from ..tools.safety import (
    PROTECTED_BRANCHES,
    assert_safe_branch_name,
    assert_write_allowed,
)


class GitHubIntegrationError(RuntimeError):
    """Safe GitHub failure that excludes credentials and response bodies."""


@dataclass(frozen=True)
class PullRequestResult:
    branch_name: str
    commit_sha: str
    pull_request_url: str


@dataclass(frozen=True)
class GitHubRepositoryStatus:
    full_name: str
    private: bool
    permissions: dict[str, bool]


class GitHubClient(Protocol):
    async def clone_repository(
        self, repo_url: str, branch: str, destination: Path
    ) -> Path: ...

    async def create_branch(
        self,
        *,
        repo_path: Path,
        base_branch: str,
        branch_name: str,
    ) -> str: ...

    async def commit_and_open_pull_request(
        self,
        *,
        repo_url: str,
        repo_path: Path,
        base_branch: str,
        branch_name: str,
        title: str,
        body: str,
        changed_files: list[str],
    ) -> PullRequestResult: ...


class FakeGitHubClient:
    """Deterministic fake; it never invokes git or makes a network request."""

    async def clone_repository(
        self, repo_url: str, branch: str, destination: Path
    ) -> Path:
        del repo_url, branch
        destination.mkdir(parents=True, exist_ok=True)
        return destination.resolve()

    async def create_branch(
        self,
        *,
        repo_path: Path,
        base_branch: str,
        branch_name: str,
    ) -> str:
        del repo_path, base_branch
        validate_repair_branch(branch_name)
        return branch_name

    async def commit_and_open_pull_request(
        self,
        *,
        repo_url: str,
        repo_path: Path,
        base_branch: str,
        branch_name: str,
        title: str,
        body: str,
        changed_files: list[str],
    ) -> PullRequestResult:
        del repo_path, base_branch, title, body, changed_files
        owner, repository = parse_github_repo_url(repo_url)
        digest = hashlib.sha256(branch_name.encode("utf-8")).hexdigest()
        return PullRequestResult(
            branch_name=branch_name,
            commit_sha=digest[:40],
            pull_request_url=f"https://github.local/{owner}/{repository}/pull/1",
        )


class SubprocessGitHubClient:
    """Git-backed implementation with an injected GitHub REST transport."""

    def __init__(
        self,
        *,
        token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token:
            raise ValueError("GitHub token is required")
        self._token = token
        self._owns_client = http_client is None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._client = http_client or httpx.AsyncClient(
            base_url="https://api.github.com",
            headers=headers,
            timeout=httpx.Timeout(20.0),
        )
        if http_client is not None:
            self._client.headers.update(headers)

    async def clone_repository(
        self, repo_url: str, branch: str, destination: Path
    ) -> Path:
        parse_github_repo_url(repo_url)
        validate_base_branch(branch)
        target = destination.resolve()
        if target.exists() and any(target.iterdir()):
            raise GitHubIntegrationError("Clone destination must be empty")
        target.parent.mkdir(parents=True, exist_ok=True)
        await self._git(
            target.parent,
            [
                "clone",
                "--branch",
                branch,
                "--single-branch",
                "--",
                repo_url,
                str(target),
            ],
            operation="clone",
            timeout=300,
        )
        return target

    async def create_branch(
        self,
        *,
        repo_path: Path,
        base_branch: str,
        branch_name: str,
    ) -> str:
        validate_base_branch(base_branch)
        validate_repair_branch(branch_name)
        status = await self._git(repo_path, ["status", "--porcelain"], operation="status")
        if status.strip():
            raise GitHubIntegrationError(
                "Real repair requires a clean cloned workspace before patching"
            )
        current = await self._git(
            repo_path, ["branch", "--show-current"], operation="branch inspection"
        )
        if current.strip() != base_branch:
            await self._git(
                repo_path,
                ["switch", base_branch],
                operation="base branch checkout",
            )
        await self._git(
            repo_path,
            ["switch", "-c", branch_name],
            operation="repair branch creation",
        )
        return branch_name

    async def commit_and_open_pull_request(
        self,
        *,
        repo_url: str,
        repo_path: Path,
        base_branch: str,
        branch_name: str,
        title: str,
        body: str,
        changed_files: list[str],
    ) -> PullRequestResult:
        owner, repository = parse_github_repo_url(repo_url)
        validate_base_branch(base_branch)
        validate_repair_branch(branch_name)
        current = await self._git(
            repo_path, ["branch", "--show-current"], operation="branch inspection"
        )
        if current.strip() != branch_name:
            raise GitHubIntegrationError("Refusing to commit from an unexpected branch")
        safe_files = _validate_changed_files(repo_path, changed_files)
        if not safe_files:
            raise GitHubIntegrationError("No controlled changed files are available to commit")
        await self._git(
            repo_path,
            ["add", "--", *safe_files],
            operation="staging",
        )
        commit_message = redact_sensitive_text(title).splitlines()[0].strip()[:120]
        if not commit_message:
            raise ValueError("Commit title must not be empty")
        await self._git(
            repo_path,
            [
                "-c",
                "user.name=RepairBot",
                "-c",
                "user.email=repairbot@users.noreply.github.com",
                "commit",
                "-m",
                commit_message,
            ],
            operation="commit",
        )
        commit_sha = (
            await self._git(repo_path, ["rev-parse", "HEAD"], operation="commit lookup")
        ).strip()
        await self._git(
            repo_path,
            ["push", "-u", "origin", f"HEAD:refs/heads/{branch_name}"],
            operation="branch push",
            timeout=300,
        )
        payload = build_github_pull_request_payload(
            title=title,
            body=body,
            head_branch=branch_name,
            base_branch=base_branch,
        )
        response = await self._request(
            "POST",
            f"/repos/{quote(owner)}/{quote(repository)}/pulls",
            json=payload,
            operation="pull-request creation",
        )
        try:
            response_body = response.json()
        except ValueError as exc:
            raise GitHubIntegrationError(
                "GitHub pull-request creation returned invalid JSON"
            ) from exc
        pull_request_url = _validate_github_response_url(
            str(response_body.get("html_url", ""))
        )
        return PullRequestResult(
            branch_name=branch_name,
            commit_sha=commit_sha,
            pull_request_url=pull_request_url,
        )

    async def check_repository_access(
        self, repo_url: str
    ) -> GitHubRepositoryStatus:
        """Check REST access without performing any repository mutation."""

        owner, repository = parse_github_repo_url(repo_url)
        response = await self._request(
            "GET",
            f"/repos/{quote(owner)}/{quote(repository)}",
            operation="repository access check",
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise GitHubIntegrationError(
                "GitHub repository access check returned invalid JSON"
            ) from exc
        permissions = body.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
        return GitHubRepositoryStatus(
            full_name=str(body.get("full_name") or f"{owner}/{repository}"),
            private=bool(body.get("private", False)),
            permissions={
                str(name): bool(allowed)
                for name, allowed in permissions.items()
                if isinstance(name, str)
            },
        )

    async def check_git_remote(self, repo_url: str) -> bool:
        """Use the production Git transport auth path for a read-only check."""

        parse_github_repo_url(repo_url)
        await self._git(
            Path.cwd(),
            ["ls-remote", "--exit-code", "--", repo_url, "HEAD"],
            operation="ls-remote",
            timeout=120,
        )
        return True

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _git(
        self,
        cwd: Path,
        args: list[str],
        *,
        operation: str,
        timeout: int = 120,
    ) -> str:
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                ["git", *args],
                cwd=cwd,
                env=self._git_environment(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise GitHubIntegrationError(
                self._safe_git_failure(
                    operation=operation,
                    args=args,
                    detail="could not complete",
                )
            ) from None
        if completed.returncode != 0:
            raise GitHubIntegrationError(
                self._safe_git_failure(
                    operation=operation,
                    args=args,
                    exit_code=completed.returncode,
                    stderr=completed.stderr,
                )
            )
        return completed.stdout

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise GitHubIntegrationError(
                f"GitHub {operation} failed before a response was received"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise GitHubIntegrationError(
                f"GitHub {operation} failed with HTTP {response.status_code}"
            )
        return response

    def _git_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        basic = base64.b64encode(
            f"x-access-token:{self._token}".encode("utf-8")
        ).decode("ascii")
        environment.update(
            {
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
                "GIT_CONFIG_KEY_1": "credential.helper",
                "GIT_CONFIG_VALUE_1": "",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        return environment

    def _safe_git_failure(
        self,
        *,
        operation: str,
        args: list[str],
        exit_code: int | None = None,
        stderr: str | None = None,
        detail: str | None = None,
    ) -> str:
        safe_operation = redact_sensitive_text(operation).strip() or "operation"
        parts = [f"GitHub git {safe_operation} failed"]
        repo_url = next(
            (
                value
                for value in args
                if value.startswith(("https://github.com/", "https://www.github.com/"))
            ),
            None,
        )
        if repo_url:
            try:
                owner, repository = parse_github_repo_url(repo_url)
                parts.append(f"repository=https://github.com/{owner}/{repository}.git")
            except ValueError:
                parts.append("repository=<invalid-or-redacted>")
        if exit_code is not None:
            parts.append(f"exit code={exit_code}")
        if detail:
            parts.append(redact_sensitive_text(detail))
        safe_stderr = self._sanitize_git_stderr(stderr)
        if safe_stderr:
            parts.append(f"stderr: {' | '.join(safe_stderr)}")
        parts.append("run github-check for credential-safe diagnostics")
        return "; ".join(parts)

    def _sanitize_git_stderr(self, stderr: str | None) -> list[str]:
        if not stderr:
            return []
        basic = base64.b64encode(
            f"x-access-token:{self._token}".encode("utf-8")
        ).decode("ascii")
        safe = stderr.replace(self._token, "<redacted>").replace(
            basic, "<redacted>"
        )
        safe = redact_sensitive_text(safe)
        return [line.strip()[:500] for line in safe.splitlines() if line.strip()][:4]


class RealGitHubClient(SubprocessGitHubClient):
    """Explicit real adapter selected only by reviewed configuration and runtime gates."""


def build_github_pull_request_payload(
    *,
    title: str,
    body: str,
    head_branch: str,
    base_branch: str,
) -> dict[str, Any]:
    validate_repair_branch(head_branch)
    validate_base_branch(base_branch)
    safe_title = redact_sensitive_text(title).strip()[:256]
    safe_body = redact_sensitive_text(body).strip()[:20_000]
    if not safe_title:
        raise ValueError("Pull-request title must not be empty")
    return {
        "title": safe_title,
        "body": safe_body,
        "head": head_branch,
        "base": base_branch,
    }


def parse_github_repo_url(repo_url: str) -> tuple[str, str]:
    candidate = repo_url.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Repository URL must be a credential-free GitHub HTTPS URL")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise ValueError("Repository URL must identify one GitHub repository")
    owner, repository = parts
    repository = repository.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9-]+", owner) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", repository
    ):
        raise ValueError("Repository URL contains unsupported characters")
    return owner, repository


def validate_repair_branch(branch_name: str) -> str:
    candidate = branch_name.strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9._/-]{1,180}", candidate)
        or ".." in candidate
        or candidate.endswith("/")
        or "//" in candidate
    ):
        raise ValueError("Repair branch name is unsafe")
    assert_safe_branch_name(candidate)
    if candidate in PROTECTED_BRANCHES:
        raise ValueError("Protected branches cannot be repair targets")
    return candidate


def validate_base_branch(branch_name: str) -> str:
    candidate = branch_name.strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9._/-]{1,180}", candidate)
        or ".." in candidate
        or candidate.startswith("-")
        or candidate.endswith("/")
    ):
        raise ValueError("Base branch name is unsafe")
    return candidate


def _validate_changed_files(repo_path: Path, changed_files: list[str]) -> list[str]:
    root = repo_path.resolve()
    safe: list[str] = []
    for relative in changed_files:
        candidate = Path(relative)
        if candidate.is_absolute():
            raise ValueError("Changed file paths must be repository-relative")
        resolved = assert_write_allowed(root, candidate)
        safe.append(resolved.relative_to(root).as_posix())
    return sorted(set(safe))


def _validate_github_response_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in {
        "github.com",
        "www.github.com",
    }:
        raise GitHubIntegrationError(
            "GitHub pull-request response did not include a valid browser URL"
        )
    return value


def build_github_client(
    mode: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> GitHubClient:
    if mode == "fake":
        return FakeGitHubClient()
    if mode != "real":
        raise ValueError("GitHub mode must be 'fake' or 'real'")
    values = environ if environ is not None else os.environ
    token = values.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("Real GitHub mode requires environment credential: GITHUB_TOKEN")
    return RealGitHubClient(token=token)


__all__ = [
    "FakeGitHubClient",
    "GitHubClient",
    "GitHubIntegrationError",
    "GitHubRepositoryStatus",
    "PullRequestResult",
    "RealGitHubClient",
    "SubprocessGitHubClient",
    "build_github_client",
    "build_github_pull_request_payload",
    "parse_github_repo_url",
    "validate_base_branch",
    "validate_repair_branch",
]
