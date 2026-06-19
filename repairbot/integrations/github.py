"""GitHub clone and pull-request abstractions."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class PullRequestResult:
    branch_name: str
    commit_sha: str
    pull_request_url: str


class GitHubClient(Protocol):
    async def clone_repository(self, repo_url: str, branch: str, destination: Path) -> Path: ...

    async def commit_and_open_pull_request(
        self,
        *,
        repo_url: str,
        repo_path: Path,
        base_branch: str,
        branch_name: str,
        title: str,
        body: str,
    ) -> PullRequestResult: ...


class FakeGitHubClient:
    """Deterministic fake; it never invokes git or makes a network request."""

    async def clone_repository(self, repo_url: str, branch: str, destination: Path) -> Path:
        del repo_url, branch
        destination.mkdir(parents=True, exist_ok=True)
        return destination.resolve()

    async def commit_and_open_pull_request(
        self,
        *,
        repo_url: str,
        repo_path: Path,
        base_branch: str,
        branch_name: str,
        title: str,
        body: str,
    ) -> PullRequestResult:
        del repo_url, repo_path, base_branch, title, body
        digest = hashlib.sha256(branch_name.encode("utf-8")).hexdigest()
        return PullRequestResult(
            branch_name=branch_name,
            commit_sha=digest[:40],
            pull_request_url=f"https://example.invalid/github/pull/{digest[:10]}",
        )


class SubprocessGitHubClient:
    """Production clone scaffold. Real PR creation must be supplied by an adapter."""

    def __init__(self, *, token: str | None = None) -> None:
        self._token = token

    async def clone_repository(self, repo_url: str, branch: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            subprocess.run,
            ["git", "clone", "--branch", branch, "--single-branch", "--", repo_url, str(destination)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )
        return destination.resolve()

    async def commit_and_open_pull_request(self, **_: object) -> PullRequestResult:
        raise NotImplementedError(
            "Configure a reviewed GitHub PR adapter before enabling real pull-request creation"
        )


class RealGitHubClient(SubprocessGitHubClient):
    """Explicit production scaffold selected only by reviewed configuration."""


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
