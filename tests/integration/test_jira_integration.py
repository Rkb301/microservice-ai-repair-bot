from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest

from repairbot.integrations.jira import RealJiraClient, build_jira_client


REQUIRED = (
    "JIRA_BASE_URL",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "JIRA_TEST_PROJECT_KEY",
)
ENABLED = os.getenv("RUN_JIRA_INTEGRATION_TESTS") == "1" and all(
    os.getenv(name) for name in REQUIRED
)

pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="Jira integration requires an explicit flag and environment settings",
)


def test_real_jira_issue_and_backlink() -> None:
    async def scenario() -> None:
        client = build_jira_client("real")
        assert isinstance(client, RealJiraClient)
        marker = uuid4().hex[:10]
        try:
            created = await client.create_or_record_ticket(
                project_key=os.environ["JIRA_TEST_PROJECT_KEY"],
                issue_type="Bug",
                title=f"[automated integration] RepairBot {marker}",
                description="Credential-gated Jira issue creation verification",
                solution="Verify the issue and backlink REST paths",
                incident_id=f"inc_{marker}",
                service_name="user-service",
                repo_url="https://github.com/Rkb301/user-service",
                run_id=f"run_{marker}",
            )
            assert created.jira_key
            assert created.jira_ticket_url.endswith(created.jira_key)
            await client.update_ticket_with_pull_request(
                created.jira_key,
                f"https://github.com/Rkb301/user-service/pull/{marker}",
            )
        finally:
            await client.aclose()

    asyncio.run(scenario())
