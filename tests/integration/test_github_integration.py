from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from repairbot.integrations.github import RealGitHubClient, build_github_client


REQUIRED = (
    "GITHUB_TOKEN",
    "GITHUB_TEST_REPO_URL",
    "GITHUB_TEST_BASE_BRANCH",
)
ENABLED = os.getenv("RUN_GITHUB_INTEGRATION_TESTS") == "1" and all(
    os.getenv(name) for name in REQUIRED
)

pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="GitHub integration requires an explicit flag and environment settings",
)


def test_real_github_clone_push_and_pull_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = build_github_client("real")
        assert isinstance(client, RealGitHubClient)
        marker = uuid4().hex[:10]
        branch = f"bugfix/integration-{marker}"
        clone = tmp_path / "repo"
        try:
            await client.clone_repository(
                os.environ["GITHUB_TEST_REPO_URL"],
                os.environ["GITHUB_TEST_BASE_BRANCH"],
                clone,
            )
            await client.create_branch(
                repo_path=clone,
                base_branch=os.environ["GITHUB_TEST_BASE_BRANCH"],
                branch_name=branch,
            )
            target = clone / "repairbot-integration" / f"{marker}.txt"
            target.parent.mkdir(exist_ok=True)
            target.write_text("Automated GitHub integration verification.\n", encoding="utf-8")
            result = await client.commit_and_open_pull_request(
                repo_url=os.environ["GITHUB_TEST_REPO_URL"],
                repo_path=clone,
                base_branch=os.environ["GITHUB_TEST_BASE_BRANCH"],
                branch_name=branch,
                title=f"[automated integration] RepairBot {marker}",
                body="Credential-gated clone, branch, commit, push, and PR verification.",
                changed_files=[target.relative_to(clone).as_posix()],
            )
            assert result.pull_request_url.startswith("https://github.com/")
            assert result.branch_name == branch
            assert len(result.commit_sha) == 40
        finally:
            await client.aclose()

    asyncio.run(scenario())
