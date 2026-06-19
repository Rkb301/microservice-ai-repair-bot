"""GitHub REST API helpers."""

from __future__ import annotations

import os

import requests


class GitHubError(RuntimeError):
    """Raised when the GitHub API request fails."""


def create_pull_request(
    *,
    github_repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
    token: str | None = None,
    timeout: int = 30,
) -> str:
    token = token or os.getenv("GITHUB_TOKEN")
    if not token:
        raise GitHubError("GITHUB_TOKEN is required to open a pull request")
    if "/" not in github_repo:
        raise GitHubError("github_repo must look like 'owner/repo'")

    response = requests.post(
        f"https://api.github.com/repos/{github_repo}/pulls",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2026-03-10",
        },
        json={"title": title, "body": body, "head": head, "base": base},
        timeout=timeout,
    )
    if response.status_code not in {200, 201}:
        raise GitHubError(
            f"GitHub PR creation failed ({response.status_code}): {response.text}"
        )
    data = response.json()
    return str(data.get("html_url") or data.get("url"))
