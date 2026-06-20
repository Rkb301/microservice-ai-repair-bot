from __future__ import annotations

import asyncio
import base64
from pathlib import Path
import subprocess

import httpx
import pytest
from typer.testing import CliRunner

from repairbot.cli import app as cli_app
from repairbot.integrations.github import (
    GitHubIntegrationError,
    GitHubRepositoryStatus,
    RealGitHubClient,
)


def test_git_clone_and_push_share_basic_auth_without_secret_in_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = "unit-test-token"
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(command, **kwargs):
        calls.append((list(command), dict(kwargs["env"])))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = RealGitHubClient(token=token)

    async def scenario() -> None:
        await client.clone_repository(
            "https://github.com/Rkb301/user-service.git",
            "main",
            tmp_path / "clone",
        )
        await client._git(
            tmp_path,
            ["push", "-u", "origin", "HEAD:refs/heads/bugfix/test"],
            operation="branch push",
        )
        await client.aclose()

    asyncio.run(scenario())
    assert len(calls) == 2
    for command, environment in calls:
        assert all(token not in argument for argument in command)
        assert environment["GIT_CONFIG_KEY_0"] == "http.extraHeader"
        assert environment["GIT_CONFIG_KEY_1"] == "credential.helper"
        assert environment["GIT_CONFIG_VALUE_1"] == ""
        scheme, encoded = environment["GIT_CONFIG_VALUE_0"].split()[-2:]
        assert scheme == "Basic"
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, decoded_token = decoded.split(":", 1)
        assert username == "x-access-token"
        assert decoded_token == token
        assert "Bearer" not in environment["GIT_CONFIG_VALUE_0"]
    clone_command = calls[0][0]
    assert "https://github.com/Rkb301/user-service.git" in clone_command
    assert all("@github.com" not in argument for argument in clone_command)


def test_rest_repository_check_uses_bearer_auth() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "full_name": "Rkb301/user-service",
                "private": False,
                "permissions": {"pull": True, "push": True},
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.github.com",
        ) as http_client:
            client = RealGitHubClient(token="unit-test-token", http_client=http_client)
            status = await client.check_repository_access(
                "https://github.com/Rkb301/user-service.git"
            )
        assert status.full_name == "Rkb301/user-service"

    asyncio.run(scenario())
    assert len(captured) == 1
    assert captured[0].headers["Authorization"] == "Bearer unit-test-token"
    assert not captured[0].headers["Authorization"].startswith("Basic ")


def test_git_failure_is_useful_and_redacts_raw_and_encoded_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = "unit-test-token"
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            128,
            stdout="",
            stderr=(
                f"remote: credential={token}\n"
                f"fatal: Authentication failed with Authorization: Basic {encoded}\n"
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = RealGitHubClient(token=token)
    with pytest.raises(GitHubIntegrationError) as raised:
        asyncio.run(
            client.clone_repository(
                "https://github.com/Rkb301/user-service.git",
                "main",
                tmp_path / "clone",
            )
        )
    asyncio.run(client.aclose())
    message = str(raised.value)
    assert "GitHub git clone failed" in message
    assert "repository=https://github.com/Rkb301/user-service.git" in message
    assert "exit code=128" in message
    assert "Authentication failed" in message
    assert "github-check" in message
    assert token not in message
    assert encoded not in message


def test_github_check_is_offline_by_default_and_reports_live_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "unit-test-token")
    runner = CliRunner()
    offline = runner.invoke(cli_app, ["github-check"])
    assert offline.exit_code == 0
    assert "REST authentication: Bearer" in offline.output
    assert "git authentication: HTTP Basic" in offline.output
    assert "network check: skipped" in offline.output
    assert "unit-test-token" not in offline.output

    async def fake_repo_check(self, repo_url: str) -> GitHubRepositoryStatus:
        assert repo_url == "https://github.com/Rkb301/user-service.git"
        return GitHubRepositoryStatus(
            full_name="Rkb301/user-service",
            private=False,
            permissions={"pull": True, "push": True},
        )

    async def fake_git_check(self, repo_url: str) -> bool:
        assert repo_url == "https://github.com/Rkb301/user-service.git"
        return True

    monkeypatch.setattr(RealGitHubClient, "check_repository_access", fake_repo_check)
    monkeypatch.setattr(RealGitHubClient, "check_git_remote", fake_git_check)
    live = runner.invoke(
        cli_app,
        [
            "github-check",
            "--allow-network",
            "--repo-url",
            "https://github.com/Rkb301/user-service.git",
        ],
    )
    assert live.exit_code == 0
    assert "repo full name: Rkb301/user-service" in live.output
    assert "visibility: public" in live.output
    assert "permissions: pull=true, push=true" in live.output
    assert "git ls-remote: passed" in live.output
    assert "unit-test-token" not in live.output
